"""Co-sim gates of the zcu216-ion build (the trapped-ion port of an ARTIQ/Urukul experiment).

- A FINE-frequency beam (freq_bits 32) held far longer than its envelope RAM (the address wraps over
  a uniform RAM — the continuous-wave `sw.on()`), cut mid-tile by a fire of an amplitude-0 slot
  (`sw.off()`): bit-exact against the golden fine composition over the whole on-window, exactly zero
  after the cut — so an AOM tone is on for exactly [on, off) at a 1.86 Hz-grid frequency.
- The gated PMT counter in loopback (TTL in 8 <- out 0): a kernel opens the gate, plays n TTL
  "photons" on out 0, closes the gate, and the halting pop_event returns exactly n, stamped with the
  close batch — the ARTIQ `gate_rising(); fetch_count()` contract."""

import numpy as np
import pytest

from riscq import run as rq
from riscq.lang import Array, DioTable, ParamTable, compile_kernel, kernel
from riscq.map import LEAD
from riscq.pulses import Pulse, envelopes, golden, units

pytestmark = pytest.mark.cosim

F_729 = 148.282e6          # off the coarse 122 kHz grid, so only the fine word can reach it


@kernel
def k_cw(beam: ParamTable, out: Array, hold: int, cut: int):
    init_pulse_params(beam.pulses)                       # noqa: F821
    set_freq(beam, beam.freq)                            # noqa: F821
    set_dur(beam, beam["on"], hold << 16)                # noqa: F821  — longer than the RAM: wraps
    t = now() + LEAD                                     # noqa: F821
    out[0] = t
    play(beam, beam["on"], t)                            # noqa: F821
    play(beam, beam["off"], t + cut)                     # noqa: F821  — amp 0: the switch-off
    wait_until(t + hold + 16)                            # noqa: F821


def _beam_table(m):
    ch = m.channel_named("u1c1", 0)
    # one uniform RAM (both slots constant full-scale): a wrapped read stays on the same samples
    on = Pulse(envelopes.square(ch.env_depth - 1), freq_hz=F_729, amp=0.5)
    off = Pulse(envelopes.square(1), freq_hz=F_729, amp=0.0)
    return ParamTable(ch, F_729, {"on": on, "off": off}), on


def test_fine_cw_beam_wraps_and_cuts_exactly(cosim_ion):
    drv, m = cosim_ion
    table, on = _beam_table(m)
    hold, cut = 3000, 2500                               # 2.4 RAM wraps before the cut
    prog = compile_kernel(k_cw, m, tables={"beam": table}, out=Array(1), hold=hold, cut=cut)
    rq.reset(drv, m, on=True)
    rq.load_program(drv, m, 0, prog.image)
    rq.check_magic(drv, m, 0, prog)
    rq.load_envelopes(drv, m, 0, prog)
    rq.load_tables(drv, m, 0, prog)
    ch = m.channel_named("u1c1", 0)
    h = drv.sim.dac_capture_arm(ch.dac, 4200)
    rq.reset(drv, m, on=False)
    rq.poll_done(drv, m, [0], timeout=4_000_000)
    t_on = int(rq.read_array(drv, m, 0, prog, "out")[0])
    rq.reset(drv, m, on=True)
    t0, cap = drv.sim.dac_capture_get(h)
    cap = np.asarray(cap)
    i = t_on - t0
    assert 0 < i and i + cut + 50 <= len(cap), f"window [{t_on}, +{cut}) outside capture [{t0}, +{len(cap)})"
    line = on.packed_lines(m, ch.index)[0:1]
    lines = np.repeat(line, cut, axis=0)
    code = units.fine_freq_to_code(F_729, m.params)
    gold = golden.pulse_window(lines, on.amp_code(), code, on.phase_code(), t_on, cut, freq_bits=32)
    assert not cap[:i].any(), "DAC not silent before the switch-on"
    bad = np.argwhere(cap[i:i + cut] != gold)
    assert bad.size == 0, f"fine CW differs from golden at batch/lane {bad[:4].tolist()} (of {len(bad)})"
    assert not cap[i + cut:i + cut + 50].any(), "DAC not silent after the switch-off"
    # and it is NOT the coarse-grid tone: the fine word moved the phase off the 16-bit golden
    gold_coarse = golden.pulse_window(lines[:64], on.amp_code(), units._freq_code(F_729, m.params),
                                      on.phase_code(), t_on, 64)
    assert not np.array_equal(cap[i:i + 64], gold_coarse)


@kernel
def k_count(ttl: ParamTable, out: Array, n: int, period: int):
    init_pulse_params(ttl.pulses)                        # noqa: F821
    set_dc_offset(ttl, 256 << 16)                        # noqa: F821  — loopback: in 8 <- out 0
    t = now() + LEAD                                     # noqa: F821
    play(ttl, ttl["gate_on"], t)                         # noqa: F821
    for i in range(n):
        tp = t + 4 + period * i
        wait_until(tp - 80)                              # noqa: F821  — 4-deep queue: ≤ 2 pairs ahead
        play(ttl, ttl["hi"], tp)                         # noqa: F821
        play(ttl, ttl["lo"], tp + 2)                     # noqa: F821
    tc = t + 4 + period * n + 4
    play(ttl, ttl["gate_off"], tc)                       # noqa: F821
    w = pop_event(ttl.sink)                              # noqa: F821  — HALTS until the count posts
    out[0] = w
    out[1] = event_time(ttl.sink)                        # noqa: F821
    out[2] = tc


def test_pmt_counter_loopback_counts_photon_train(cosim_ion):
    drv, m = cosim_ion
    ttl = m.channel_named("ttl", 0)
    table = DioTable(ttl, {"gate_on": (0x0100, 0x0100, 2), "gate_off": (0x0100, 0x0000, 2),
                           "hi": (0x0001, 0x0001, 2), "lo": (0x0001, 0x0000, 2)})
    n = 7
    # 100 batches apart: issuing a TTL pair costs the core ~40 cycles, so a faster train than the
    # queue's lookahead allows would land late (the entries fire on arrival — measured, not dropped)
    prog = compile_kernel(k_count, m, tables={"ttl": table}, out=Array(3), n=n, period=100)
    out = rq.run(drv, m, {0: prog}, results=["out"], timeout=4_000_000)[0]["out"]
    word, t_ev, tc = (int(v) & 0xFFFFFFFF for v in out)
    assert word == (0x0100 << 16) | n, f"count event {word:#x}, want line 8 count {n}"
    assert t_ev == tc, f"count stamped {t_ev}, gate closed at {tc}"


@kernel
def k_hold(beam: ParamTable, out: Array):
    init_pulse_params(beam.pulses)                       # noqa: F821
    set_freq(beam, beam.freq)                            # noqa: F821
    set_dur(beam, beam["on"], 65535 << 16)               # noqa: F821  — all-ones: HOLD (holdMaxDur)
    t = now() + LEAD                                     # noqa: F821
    out[0] = t
    play(beam, beam["on"], t)                            # noqa: F821
    wait_until(t + 8)                                    # noqa: F821  — exit with the beam ON


@kernel
def k_off(beam: ParamTable, out: Array):
    init_pulse_params(beam.pulses)                       # noqa: F821
    t = now() + LEAD                                     # noqa: F821
    out[0] = t
    play(beam, beam["off"], t)                           # noqa: F821
    wait_until(t + 8)                                    # noqa: F821


def test_beam_holds_across_kernels(cosim_ion):
    """sw.on() persists like an Urukul RF switch: a dur-0xFFFF fire on a fine channel holds past the
    65535-batch counter, after its kernel has exited (core halted), until the next kernel's OFF."""
    drv, m = cosim_ion
    table, on = _beam_table(m)
    ch = m.channel_named("u1c1", 0)
    p_on = compile_kernel(k_hold, m, tables={"beam": table}, out=Array(1))
    t_on = int(rq.run(drv, m, {0: p_on}, results=["out"], timeout=2_000_000)[0]["out"][0])
    late = max(t_on + 70_000, drv.sim.batch_time() + 400)   # past one full count-down of the dur field
    assert late - t_on > 65535
    h = drv.sim.dac_capture_arm(ch.dac, 64, start_batch=late)
    t0, cap = drv.sim.dac_capture_get(h)
    cap = np.asarray(cap)
    line = on.packed_lines(m, ch.index)[0:1]
    code = units.fine_freq_to_code(F_729, m.params)
    gold = golden.pulse_window(np.repeat(line, len(cap), axis=0), on.amp_code(), code, on.phase_code(),
                               t0, len(cap), freq_bits=32)
    assert np.array_equal(cap, gold), "beam not holding (bit-exact CW) 70000 batches after its kernel exited"
    p_off = compile_kernel(k_off, m, tables={"beam": table}, out=Array(1))
    t_off = int(rq.run(drv, m, {0: p_off}, results=["out"], timeout=2_000_000)[0]["out"][0])
    h = drv.sim.dac_capture_arm(ch.dac, 64, start_batch=max(t_off + 4, drv.sim.batch_time() + 400))
    _, cap = drv.sim.dac_capture_get(h)
    assert not np.asarray(cap).any(), "the next kernel's OFF did not switch the held beam off"
