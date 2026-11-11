"""test_pmt.py (ARTIQ 8) on RISC-Q — the host side of the port.

The ARTIQ experiment split in two, the way ARTIQ itself splits it:
- what ARTIQ evaluates on the host or in float kernel arithmetic (arguments, prepare(), frequencies,
  attenuations, delay() / seconds_to_mu() conversions) is evaluated HERE with ARTIQ 8's own formulas
  and handed to the core as an int32 parameter table;
- the timeline (the @kernel methods) runs on the RISC-Q core: scan729spec.h (method for method) on the
  ion.h runtime, compiled by riscq.lang.

Device map (zcu216-ion build): urukul{0,1}_ch{0..3} -> the fine-frequency RF channels u{0,1}c{0..3};
ttl4..7 -> TTL bank output lines 4..7 (PMOD0); ttl0..3 (+ their _counter) -> input lines 8..11
(PMOD1 0..3). The RF switch of an Urukul channel is the channel's held CW tone (ion.h).

Units (ARTIQ 8, then RISC-Q):
- AD9910 FTW = round(f · 2^32 / 1 GHz); the RISC-Q fine word synthesizes the SAME tone the DDS does:
  code = round(FTW / 8)  (8 GS/s DAC, 2^32 grid) — within 0.93 Hz of the AD9910's realized f.
- AD9910 ASF = round(amp · 0x3fff); RISC-Q amplitude code = round(ASF / 0x3fff · AMP_SCALE).
- Urukul attenuator: att_mu = 255 − round(dB · 8), realized dB = (255 − att_mu) / 8; RISC-Q scales
  the amplitude by attq / 2^15, attq = round(2^15 · 10^(−dB/20)).
- delay(x) -> round(x / 1 ns) half away from zero (the ARTIQ compiler); seconds_to_mu(x) (gate_rising)
  -> floor(x / 1 ns) — 2*ms gates are 1999999 ns in ARTIQ, and here."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from riscq import run as rq
from riscq.lang import Array, DioTable, ParamTable, compile_kernel, kernel
from riscq.map import SocMap
from riscq.pulses import Pulse, envelopes, golden

HERE = Path(__file__).resolve().parent
MHz, kHz, us, ms, dB = 1e6, 1e3, 1e-6, 1e-3, 1.0

BEAMS = ("u1c1", "u1c2", "u0c2", "u1c0", "u1c3", "u0c0", "u0c1", "u0c3")      # ion.h beam index order
ARTIQ_DDS = {"urukul1_ch1": "u1c1", "urukul1_ch2": "u1c2", "urukul0_ch2": "u0c2", "urukul1_ch0": "u1c0",
             "urukul1_ch3": "u1c3", "urukul0_ch0": "u0c0", "urukul0_ch1": "u0c1", "urukul0_ch3": "u0c3"}
ARTIQ_TTL_OUT = {"ttl4": 4, "ttl5": 5, "ttl6": 6, "ttl7": 7}
ARTIQ_TTL_IN = {"ttl0_counter": 8, "ttl1_counter": 9, "ttl2_counter": 10, "ttl3_counter": 11}
PMT_LINE = 8
AMP_SCALE = golden.AMP_SCALE


# ── ARTIQ 8 arithmetic ──
def delay_mu_of(seconds: float) -> int:
    x = float(seconds) / 1e-9
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def seconds_to_mu(seconds: float) -> int:
    return int(np.int64(float(seconds) // 1e-9))


def ftw_9910(f_hz: float) -> int:
    return int(np.int32(round((1 << 32) / 1e9 * f_hz)))


def fcode_of_ftw(ftw: int) -> int:
    """The RISC-Q fine word of an AD9910 FTW: the same tone, round(FTW / 8) (half up)."""
    c = ((ftw & 0xFFFFFFFF) + 4) >> 3
    return ((c + (1 << 31)) & 0xFFFFFFFF) - (1 << 31)


def fcode_9910(f_hz: float) -> int:
    return fcode_of_ftw(ftw_9910(f_hz))


def ftw_9912(f_hz: float, sysclk: float = 1e9) -> int:
    """AD9912.frequency_to_ftw: round(f · 2^48 / sysclk) (48-bit)."""
    return int(np.int64(round(f_hz * (1 << 48) / sysclk)))


def fcode_9912(f_hz: float) -> int:
    """The RISC-Q fine word of an AD9912 tone: round(FTW48 / 2^19) — the 1 GHz 2^48 grid onto 8 GS/s 2^32."""
    c = (ftw_9912(f_hz) + (1 << 18)) >> 19
    return ((c + (1 << 31)) & 0xFFFFFFFF) - (1 << 31)


def pow16_9910(turns: float) -> int:
    return int(np.int32(round((1 << 16) * turns))) & 0xFFFF


def pow16_9912(turns: float) -> int:
    return ((int(np.int32(round((1 << 14) * turns))) & 0x3FFF) << 2) & 0xFFFF


def acode_of_asf(asf: int) -> int:
    return int(round(asf / 0x3FFF * AMP_SCALE))


def acode_9910(amp: float) -> int:
    return acode_of_asf(int(round(amp * 0x3FFF)))


def att_db_realized(db: float) -> float:
    mu = 255 - int(round(db * 8))
    return (255 - max(0, min(255, mu))) / 8


def attq_of_db(db_realized: float) -> int:
    return int(round((1 << 15) * 10 ** (-db_realized / 20)))


def attq(db: float) -> int:
    return attq_of_db(att_db_realized(db))


# ── the RISC-Q build ──
def beam_tables(m: SocMap) -> dict[str, ParamTable]:
    tables = {}
    for i, name in enumerate(BEAMS):
        ch = m.channel_named(name, 0)
        tables[f"b{i}"] = ParamTable(ch, 0.0, {"on": Pulse(envelopes.square(ch.env_depth - 1), amp=1.0),
                                               "off": Pulse(envelopes.square(1), amp=0.0)})
    tables["ttl"] = DioTable(m.channel_named("ttl", 0), {"x": (0, 0, 2)})
    return tables


def _defines(names) -> str:
    lines = [f"#define {n} {i}" for i, n in enumerate(names)]
    lines += [f"#define {b.upper()} {i}" for i, b in enumerate(BEAMS)]
    lines += [f"#define PMT {PMT_LINE}"]
    return "\n".join(lines) + "\n"


@dataclass
class RunResult:
    pmt: list[int]
    log: np.ndarray
    stat: dict
    params: dict = field(default_factory=dict)


@kernel
def k_experiment(b0: ParamTable, b1: ParamTable, b2: ParamTable, b3: ParamTable, b4: ParamTable,
                 b5: ParamTable, b6: ParamTable, b7: ParamTable, ttl: ParamTable,
                 P: Array, span: Array, emu: Array, pmt: Array, log: Array, stat: Array,
                 npts: int, reps: int, nemu: int, logw: int):
    """The one RISC-Q entry of every ported experiment: load the beam / TTL tables, start the ion.h
    runtime, run the experiment header's experiment_run(), report."""
    init_pulse_params(b0.pulses)                             # noqa: F821
    init_pulse_params(b1.pulses)                             # noqa: F821
    init_pulse_params(b2.pulses)                             # noqa: F821
    init_pulse_params(b3.pulses)                             # noqa: F821
    init_pulse_params(b4.pulses)                             # noqa: F821
    init_pulse_params(b5.pulses)                             # noqa: F821
    init_pulse_params(b6.pulses)                             # noqa: F821
    init_pulse_params(b7.pulses)                             # noqa: F821
    init_pulse_params(ttl.pulses)                            # noqa: F821
    ion_init(log, logw, emu, nemu)                           # noqa: F821
    experiment_run(P, span, npts, reps, pmt)                 # noqa: F821
    ion_finish(stat)                                         # noqa: F821


class Experiment:
    """One test_pmt.py class on RISC-Q: `header` (the ported @kernel methods), `params()` (ARTIQ's host
    arithmetic), `span()` (the scanned codes), `n_gates()`; run() compiles and runs it."""

    header = ""
    reptime = 1

    def __init__(self, arguments: dict | None = None, datasets: dict | None = None):
        self.args = {**self.defaults(), **(arguments or {})}
        self.datasets = {"resonance_point": 35.0, "spinpol_freq": 145.280, "qubit_freq": 148.280,
                         "DPQL_freq": 148.063, **(datasets or {})}

    def defaults(self) -> dict:
        return {}

    def prepare(self):
        pass

    def params(self) -> dict[str, int]:
        raise NotImplementedError

    def span(self) -> list[int]:
        return []

    def program(self, m: SocMap, npts: int, reps: int, n_emu: int, log_words: int):
        P = self.params()
        hdr = _defines(P) + (HERE / "bhbase.h").read_text() + (HERE / self.header).read_text()
        span = self.span()
        prog = compile_kernel(
            k_experiment, m, tables=beam_tables(m),
            include=[("ion.h", (HERE / "ion.h").read_text()), ("ion_exp.h", hdr)],
            P=Array(len(P), input=True), span=Array(max(1, len(span)), input=True),
            emu=Array(max(1, n_emu), input=True), pmt=Array(max(1, npts * reps), host=True),
            log=Array(log_words, host=True), stat=Array(5),
            npts=npts, reps=reps, nemu=n_emu, logw=log_words)
        return prog, P, span

    def run(self, drv, m: SocMap, emu_counts=None, npts: int | None = None, reps: int | None = None,
            log_words: int = 1 << 20, timeout: int = 2_000_000_000) -> RunResult:
        """The kernel on core 0. `emu_counts`: the photon count of each gate, emitted as TTL pulses
        looped back into the PMT counter (board / co-sim self-test); None = the real PMOD1 input."""
        npts = len(self.span()) if npts is None else npts
        reps = self.reptime if reps is None else reps
        emu = list(emu_counts or [])
        prog, P, span = self.program(m, npts, reps, len(emu), log_words)
        out = rq.run(drv, m, {0: prog}, arrays={0: {"P": list(P.values()), "span": span or [0],
                                                      "emu": emu or [0]}},
                     results=["pmt", "log", "stat"], timeout=timeout)[0]
        stat = dict(zip(("log_words", "late", "tight", "emu_trunc", "gates"), (int(v) for v in out["stat"])))
        log = np.asarray(out["log"][:stat["log_words"]], dtype=np.int64).reshape(-1, 4)
        return RunResult(pmt=[int(v) for v in out["pmt"][:npts * reps]], log=log, stat=stat, params=P)


class Scan729spec(Experiment):
    """test_pmt.py Scan729spec: build() arguments (ARTIQ defaults), prepare(), run(), analyze()."""

    header = "scan729spec.h"
    reptime = 100                                            # spec729(..., reptime=100) in run()

    def defaults(self):
        return {"pulse length": 120 * us, "AOM attenuation": 20 * dB, "Scan qubit range?": True,
                "Scan spinpol range?": True, "Custom range?": False, "Use spin polarization?": False,
                "qubit scan Start": 148.20 * MHz, "qubit Scan End": 148.30 * MHz, "qubit Scan Step": 0.002 * MHz,
                "spinpol scan Start": 145.40 * MHz, "spinpol Scan End": 145.50 * MHz,
                "spinpol Scan Step": 0.002 * MHz, "custom scan Start": 145.40 * MHz,
                "custom Scan End": 145.50 * MHz, "custom Scan Step": 0.002 * MHz}

    def prepare(self):
        a = self.args
        self.slack_spi = 200 * us
        self.spec_729_period = a["pulse length"]
        self.para_att_729AOM = a["AOM attenuation"]
        qs = np.arange(a["qubit scan Start"], a["qubit Scan End"], a["qubit Scan Step"])
        sps = np.arange(a["spinpol scan Start"], a["spinpol Scan End"], a["spinpol Scan Step"])
        cs = np.arange(a["custom scan Start"], a["custom Scan End"], a["custom Scan Step"])
        if a["Scan qubit range?"] or a["Scan spinpol range?"]:
            self.specrange = np.append(sps, qs)
        if a["Custom range?"]:
            self.specrange = cs
        self.respoint = self.datasets["resonance_point"]
        self.spinpol_729_period = 20
        self.spinpol_729_freq = self.datasets["spinpol_freq"]
        self.repumpingperiod = 20

    def span(self):
        return [fcode_9910(f) for f in self.specrange]

    def params(self) -> dict[str, int]:
        resonantfreq = self.respoint / 2 + 90
        return {
            "P_D5US": delay_mu_of(5 * us), "P_D10US": delay_mu_of(10 * us), "P_D20US": delay_mu_of(20 * us),
            "P_D40US": delay_mu_of(40 * us), "P_D50US": delay_mu_of(50 * us), "P_D70US": delay_mu_of(70 * us),
            "P_D100US": delay_mu_of(100 * us), "P_D200US": delay_mu_of(200 * us),
            "P_D_SLACK_SPI": delay_mu_of(self.slack_spi),
            "P_D_COOLINGTIME": delay_mu_of(1000 * us),                    # Dopplercooling(coolingtime=1000)
            "P_D_SPEC_PERIOD": delay_mu_of(self.spec_729_period),
            "P_D_SPINPOL_PERIOD": delay_mu_of(self.spinpol_729_period * us),
            "P_D_REPUMP_PERIOD": delay_mu_of(self.repumpingperiod * us),
            "P_GATE_DETECT": seconds_to_mu(2 * ms),                       # pmt_number(2): gate_rising(2*ms)
            "P_SPINPOL_ROUNDS": 10,
            "P_F_COOL": fcode_9910((resonantfreq - 6.5) * MHz),           # detuning=6.5
            "P_F_CHECK": fcode_9910((resonantfreq - 4) * MHz),
            "P_F_SPINPOL": fcode_9910(self.spinpol_729_freq * MHz),
            "P_F_100MHZ": fcode_9910(100 * MHz),
            "P_F_END": fcode_9910((90 + self.respoint / 2 - 10) * MHz),
            "P_A_FULL": acode_9910(1.0),
            "P_ATT_COOL": attq(16 * dB), "P_ATT_6": attq(6 * dB), "P_ATT_10": attq(10 * dB),
            "P_ATT_11": attq(11 * dB), "P_ATT_729": attq(self.para_att_729AOM * dB),
            "P_ATT_CSP_854": attq(25 * dB),
            "P_FLAG_SPINPOL": int(bool(self.args["Use spin polarization?"])), "P_FLAG_CSPINPOL": 0,
        }

    def analyze(self, pmt, reptime: int | None = None, thresh: int = 25):
        """P(D) per scan point, exactly as the ARTIQ analyze(): count < thresh -> dark (shelved)."""
        reptime = self.reptime if reptime is None else reptime
        d = (np.array(pmt) < thresh).astype(float)
        return [float(d[j:j + reptime].sum() / reptime) for j in range(0, len(d), reptime)]


class Ticklescan(Experiment):
    """test_pmt.py Ticklescan. As in ARTIQ, prepare() leaves `ticklerange` unset unless one of the
    three range flags is on (the ARTIQ class then fails with AttributeError — so does this one)."""

    header = "ticklescan.h"

    def defaults(self):
        return {"General range?": False, "nn=3.5 range?": False, "nn=7.0 range?": False,
                "G Scan Start": 300. * kHz, "G Scan End": 310. * kHz, "G Scan Step": 0.1 * kHz,
                "3.5 Scan Start": 273. * kHz, "3.5 Scan End": 278. * kHz, "3.5 Scan Step": 0.1 * kHz,
                "single ion Scan Start": 400. * kHz, "single ion Scan End": 450. * kHz,
                "single ion Scan Step": 0.1 * kHz}

    def prepare(self):
        a = self.args
        gs = np.arange(a["G Scan Start"], a["G Scan End"], a["G Scan Step"])
        is_ = np.arange(a["3.5 Scan Start"], a["3.5 Scan End"], a["3.5 Scan Step"])
        si = np.arange(a["single ion Scan Start"], a["single ion Scan End"], a["single ion Scan Step"])
        if a["General range?"]:
            self.ticklerange = gs
        if a["nn=3.5 range?"]:
            self.ticklerange = is_
        if a["nn=7.0 range?"]:
            self.ticklerange = si
        self.respoint = self.datasets["resonance_point"]

    def span(self):
        return [fcode_9912(f) for f in self.ticklerange]          # urukul0_ch1.set(freq * Hz)

    def params(self) -> dict[str, int]:
        return {
            "P_D10US": delay_mu_of(10 * us), "P_D100US": delay_mu_of(100 * us),
            "P_D200US": delay_mu_of(200 * us), "P_D250US": delay_mu_of(250 * us),
            "P_D1MS": delay_mu_of(1 * ms), "P_GATE10": seconds_to_mu(10 * ms),     # pmt_number(10)
            "P_ATT_9_5": attq(9.5 * dB), "P_ATT_0": attq(0 * dB),
            "P_F_397": fcode_9910((90 + self.respoint / 2 - 2.5) * MHz), "P_F_100MHZ": fcode_9910(100 * MHz),
            "P_A_FULL": acode_9910(1.0),
        }


class pulser_delay(Experiment):
    """test_pmt.py pulser_delay (the PMT monitor), `iterations` loop passes (ARTIQ: until paused)."""

    header = "pulser_delay.h"
    X_COST = {"Sampler.set_gain_mu": 1096, "Sampler.sample": 8960}   # ARTIQ 8 costs (artiq_ref 'x' events)

    def defaults(self):
        return {}

    def prepare(self):
        self.iterations = int(self.args.get("iterations", 1))

    def span(self):
        return [0] * self.iterations

    def params(self) -> dict[str, int]:
        return {"P_D500US": delay_mu_of(500 * us), "P_D1MS": delay_mu_of(1 * ms), "P_D10MS": delay_mu_of(10 * ms),
                "P_GATE100": seconds_to_mu(100 * ms),                       # pmt_number(100)
                "P_X_SET_GAIN": self.X_COST["Sampler.set_gain_mu"], "P_X_SAMPLE": self.X_COST["Sampler.sample"]}


class Ionchainmanipulation(Scan729spec):
    """test_pmt.py Ionchainmanipulation: Scan729spec's kernels verbatim, plus a 'Displacement
    parameters' argument the class declares but never uses."""

    def defaults(self):
        return {**super().defaults(), "Displacement parameters": 490 * kHz}


EXPERIMENTS = {"Scan729spec": Scan729spec, "Ionchainmanipulation": Ionchainmanipulation,
               "Ticklescan": Ticklescan, "pulser_delay": pulser_delay}
