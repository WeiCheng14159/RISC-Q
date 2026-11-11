"""ARTIQ 8 reference run: execute an ARTIQ experiment (e.g. test_pmt.py) UNMODIFIED through ARTIQ 8's own
driver code (urukul / ad9910 / ad9912 / ttl / edge_counter / ad53xx / sampler / dma) on a host-executing
core, and record the exact event timeline ARTIQ would emit — the target the RISC-Q port must reach.

    ~/git/artiq-8/.venv/bin/python riscq/ion/artiq_ref.py <experiment.py> <Class> <out.json> \\
        [--set "argument name=value" ...] [--seed N]

Run with the ARTIQ 8 checkout on PYTHONPATH (it imports only artiq + numpy, not riscq). What it models:

- the core: `@kernel` functions execute in Python (as artiq.sim does), the RTIO syscalls (rtio_output,
  rtio_input_data, rtio_get_counter, the DMA syscalls, …) are rebound in every driver module, and the
  time cursor is ARTIQ's own (now_mu / delay / at_mu, 1 mu = 1 ns; delay(s) = round(s / ref_period)).
- the RTIO counter (the core's "wall clock"): the CPU is taken as infinitely fast, so the counter only
  advances when the kernel BLOCKS — fetching a count (to the gate close) or wait_until_mu. break_realtime
  / reset then put now at counter + 125000 mu exactly as ARTIQ's Core does. Absolute times after a
  break are therefore model-dependent; every break is recorded as a timeline SEGMENT boundary.
- device init routines (CPLD/AD9910/AD9912/Zotino/Sampler .init) are recorded as boundaries and not
  emulated — they boot chips the RISC-Q build does not have.
- the PMT: an EdgeCounter gate [open, close) returns a count from a deterministic trapped-ion model
  (IonModel below — Ca+ 397 fluorescence, 729 shelving with Rabi lineshapes, 854/866 repump) driven by
  the recorded beam state, with a seeded RNG. The count list is saved: the RISC-Q run is fed the SAME
  photons (riscq.ion), so datasets and threshold branches must match exactly.

Output JSON: `events` (decoded: ttl / sw / dds / att / gate / dac / sample / init / break / dma),
`gates` (open, close, count), `datasets`, and `segments`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
import types
from pathlib import Path

import numpy as np

# The ARTIQ 8 compiler is imported (artiq.coredevice.core) but never used — kernels run in Python.
# llvmlite >= 0.45 turned the compiler's module-level llvm.initialize() into an error: make it a no-op.
try:
    import llvmlite.binding as _llvm
    for _f in ("initialize", "initialize_native_target", "initialize_native_asmprinter",
               "initialize_all_targets", "initialize_all_asmprinters"):
        if hasattr(_llvm, _f):
            setattr(_llvm, _f, lambda *a, **k: None)
except ImportError:
    pass

from artiq.language import core as core_language
from artiq.language.environment import ProcessArgumentManager
from artiq.master.worker_db import DeviceManager
from artiq.tools import file_import, get_experiment

HERE = Path(__file__).resolve().parent
NS = 1e-9


# ───────────────────────────── the time manager (ARTIQ's cursor) ─────────────────────────────
class TimeManager:
    """ARTIQ's now_mu cursor with sequential/parallel blocks, in machine units (ns)."""

    def __init__(self):
        self.stack = [[0, 0]]          # per block: [cursor, block_end]; parallel blocks track their end

    @property
    def now(self) -> int:
        return self.stack[-1][0]

    def enter_sequential(self):
        self.stack.append([self.now, self.now, "seq"])

    def enter_parallel(self):
        self.stack.append([self.now, self.now, "par", self.now])

    def exit(self):
        blk = self.stack.pop()
        end = max(blk[0], blk[1]) if blk[2] == "seq" else blk[1]
        self._advance_to(end)

    def _advance_to(self, t):
        top = self.stack[-1]
        if len(top) > 2 and top[2] == "par":
            top[1] = max(top[1], t)            # a parallel block: children start at its start
        else:
            top[0] = t
            top[1] = max(top[1], t)

    def take_time_mu(self, d):
        top = self.stack[-1]
        if len(top) > 2 and top[2] == "par":
            top[1] = max(top[1], top[3] + int(d))
        else:
            top[0] += int(d)
            top[1] = max(top[1], top[0])

    def take_time(self, seconds):
        self.take_time_mu(delay_mu_of(seconds))

    def get_time_mu(self):
        top = self.stack[-1]
        return top[3] if len(top) > 2 and top[2] == "par" else top[0]

    def set_time_mu(self, t):
        top = self.stack[-1]
        if len(top) > 2 and top[2] == "par":
            raise NotImplementedError("at_mu inside a parallel block")
        top[0] = int(t)
        top[1] = max(top[1], top[0])


def delay_mu_of(seconds) -> int:
    """ARTIQ 8's delay(): the compiler lowers it to delay_mu(round(x / ref_period)) with LLVM's
    round — half AWAY from zero (Python's round() is half-to-even)."""
    x = float(seconds) / NS
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


TM = TimeManager()


# ───────────────────────────── the trapped-ion model (the PMT) ───────────────────────────────
class IonModel:
    """A deterministic Ca+ model, enough to give the experiment physically shaped data and
    data-dependent branches. Beam roles follow test_pmt.py's comments: urukul1_ch2 = 397 (cooling /
    detection, double-pass), urukul1_ch1 = 729 (S1/2 -> D5/2, scanned), urukul0_ch2 = the 854 repump
    (D5/2 -> S1/2; test_pmt.py turns it OFF during detection). The 866 is taken as always on.

    - 397 fluorescence while the ion is in S and the 397 is on: R = R_MAX · s/(1+s+(2δ/Γ)²),
      s = S0 · amp² · 10^(−att/10), δ = 2·(f_aom − f_res) (double pass), f_res = respoint/2 + 90 MHz.
    - 729 shelving at each 729 switch-off: p = Ω²/(Ω²+Δ²) · sin²(√(Ω²+Δ²)·T/2) per resonance line,
      Ω = Ω0 · strength · amp · 10^(−att/20); a draw moves S -> D. Repump on -> S.
    - counts ~ Poisson((R + R_BG) · T_gate), one seeded RNG drawn in event order."""

    R_MAX, R_BG, GAMMA, S0 = 200e3, 1.5e3, 21.6e6, 1.0   # bright ~55 / dark ~3 counts in a 2 ms gate
    OMEGA0 = 2 * math.pi * 50e3
    LINES = ((148.280e6, 1.0), (145.450e6, 0.6))      # AOM frequencies of two 729 components

    def __init__(self, respoint_mhz: float, seed: int):
        self.f397_res = (respoint_mhz / 2 + 90) * 1e6
        self.rng = np.random.default_rng(seed)
        self.shelved = False

    def on_729_pulse(self, f, amp, att_db, dur_s):
        if self.shelved or dur_s <= 0:
            return
        p_tot = 0.0
        for f0, strength in self.LINES:
            om = self.OMEGA0 * strength * amp * 10 ** (-att_db / 20)
            d = 2 * math.pi * (f - f0)
            w = math.sqrt(om * om + d * d)
            p_tot += (om * om / (w * w)) * math.sin(w * dur_s / 2) ** 2 if w > 0 else 0.0
        if self.rng.random() < min(p_tot, 1.0):
            self.shelved = True

    def repump(self):
        self.shelved = False

    def count(self, beam397, repump_on, dur_s):
        if repump_on:                     # an 854 left on through the gate empties D: the ion reads bright
            self.shelved = False
        rate = self.R_BG
        on, f, amp, att = beam397
        if on and not self.shelved:
            s = self.S0 * amp * amp * 10 ** (-att / 10)
            det = 2 * (f - self.f397_res)
            rate += self.R_MAX * s / (1 + s + (2 * det / self.GAMMA) ** 2)
        return int(self.rng.poisson(rate * dur_s))


# ───────────────────────────── the recorder (RTIO + decode) ─────────────────────────────────
class Recorder:
    def __init__(self, ddb: dict, ion: IonModel):
        self.events: list[dict] = []
        self.gates: list[dict] = []
        self.counter = 0                         # the RTIO counter (wall clock), see the module doc
        self.ion = ion
        self.dev_of_channel: dict[int, tuple[str, str]] = {}
        for name, d in ddb.items():
            if isinstance(d, dict) and d.get("type") == "local" and "channel" in d.get("arguments", {}):
                self.dev_of_channel[d["arguments"]["channel"]] = (name, d["class"])
        self.ttl: dict[str, int] = {}
        self.dds: dict[str, list] = {}           # urukulN_chM -> [f_hz, amp, phase_turns]
        self.pending: dict[str, dict[str, list]] = {}   # urukulN -> {chan: pending profile}
        self.att: dict[str, float] = {}
        self.on_since: dict[str, int] = {}
        self.gate_open: dict[str, tuple[int, tuple, bool]] = {}
        self.counts_ready: dict[int, list[int]] = {}
        self.dma_buf = None
        self.dma: dict[str, tuple[int, list]] = {}
        self.segment = 0

    def emit(self, kind, t, **kw):
        ev = {"k": kind, "t": int(t), "seg": self.segment, **kw}
        self.events.append(ev)
        return ev

    def boundary(self, kind, t, **kw):
        self.segment += 1
        self.emit(kind, t, **kw)

    # urukul channel <-> its switch TTL
    @staticmethod
    def sw_channel(ttl_name):
        if ttl_name.startswith("ttl_urukul") and "_sw" in ttl_name:
            u, sw = ttl_name[len("ttl_"):].split("_sw")
            return f"{u}_ch{sw}"
        return None

    def beam(self, ch):
        f, amp, _ = self.dds.get(ch, [0.0, 0.0, 0.0])
        on = bool(self.ttl.get(f"ttl_{ch.replace('_ch', '_sw')}", 0))
        return on, f, amp, self.att.get(ch, 31.5)

    # ── raw RTIO output ──
    def rtio_output(self, target, data):
        t = TM.get_time_mu()
        if self.dma_buf is not None:
            self.dma_buf.append((t, int(target), int(data)))
            return
        self._output(t, int(target), int(data))

    def _output(self, t, target, data):
        ch, addr = target >> 8, target & 0xFF
        name, cls = self.dev_of_channel.get(ch, (f"ch{ch}", "?"))
        if cls in ("TTLOut", "TTLInOut") and addr == 0:
            level = data & 1
            prev = self.ttl.get(name, 0)
            self.ttl[name] = level
            u = self.sw_channel(name)
            if u is not None:
                self.emit("sw", t, ch=u, on=level)
                self._beam_switch(u, level, t)
            elif name.endswith("_io_update"):
                if level and not prev:
                    self._io_update(name[len("ttl_"):-len("_io_update")], t)
            else:
                self.emit("ttl", t, ch=name, on=level)
        elif cls == "EdgeCounter":
            self._counter_config(name, t, data)

    def _beam_switch(self, ch, on, t):
        if on:
            self.on_since.setdefault(ch, t)
            if ch == "urukul0_ch2":
                self.ion.repump()
        else:
            t0 = self.on_since.pop(ch, None)
            if t0 is not None and ch == "urukul1_ch1":
                _, f, amp, att = self.beam(ch)
                self.ion.on_729_pulse(f, amp, att, (t - t0) * NS)

    def _io_update(self, urukul, t):
        for chan, prof in self.pending.pop(urukul, {}).items():
            self.dds[chan] = prof
            self.emit("dds", t, ch=chan, f=prof[0], amp=prof[1], phase=prof[2])

    def _counter_config(self, name, t, cfg):
        if cfg & 0x1:                                    # CONFIG_COUNT_RISING: the gate opens
            self.gate_open[name] = (t, self.beam("urukul1_ch2"), bool(self.ttl.get("ttl_urukul0_sw2", 0)))
        elif cfg & 0x4 and name in self.gate_open:       # CONFIG_SEND_COUNT_EVENT: closes, posts
            t0, b397, rep = self.gate_open.pop(name)
            n = self.ion.count(b397, rep, (t - t0) * NS)
            self.gates.append({"ch": name, "open": t0, "close": t, "count": n})
            self.emit("gate", t0, ch=name, close=t, count=n)
            ch = next(c for c, (nm, _) in self.dev_of_channel.items() if nm == name)
            self.counts_ready.setdefault(ch, []).append((t, n))

    # ── raw RTIO input ──
    def rtio_input_data(self, channel):
        q = self.counts_ready.get(channel)
        if q:
            t, n = q.pop(0)
            self.advance_counter(t)
            return n
        return 0                                         # SPI reads (sampler, status): zero

    def advance_counter(self, t):
        """The RTIO counter reaches t: it counts coarse (8 mu) cycles, so it reads t rounded up to 8."""
        self.counter = max(self.counter, (int(t) + 7) & ~7)

    def rtio_input_timestamp(self, timeout_mu, channel):
        self.advance_counter(timeout_mu)
        return -1

    def rtio_input_timestamped_data(self, timeout_mu, channel):
        self.advance_counter(timeout_mu)
        return (-1, 0)


REC: Recorder | None = None


# ───────────────────────────── syscalls, rebound per driver module ──────────────────────────
def _sys_rtio_output(target, data): REC.rtio_output(target, data)
def _sys_rtio_output_wide(target, data):
    for d in data:
        REC.rtio_output(target, d)
def _sys_rtio_input_data(channel): return REC.rtio_input_data(channel)
def _sys_rtio_input_timestamp(timeout_mu, channel): return REC.rtio_input_timestamp(timeout_mu, channel)
def _sys_rtio_input_timestamped_data(timeout_mu, channel): return REC.rtio_input_timestamped_data(timeout_mu, channel)
def _sys_rtio_get_counter(): return REC.counter
def _sys_rtio_init(): pass
def _sys_dma_record_start(name):
    REC.dma_buf = []
    REC._dma_name = name
def _sys_dma_record_stop(duration, enable_ddma):
    REC.dma[REC._dma_name] = (int(duration), REC.dma_buf)
    REC.dma_buf = None
def _sys_dma_erase(name): REC.dma.pop(name, None)
def _sys_dma_retrieve(name):
    dur, _ = REC.dma[name]
    return (dur, list(REC.dma).index(name), False)
def _sys_dma_playback(timestamp, ptr, enable_ddma):
    name = list(REC.dma)[ptr]
    dur, buf = REC.dma[name]
    REC.emit("dma", timestamp, name=name, dur=dur, n=len(buf))
    for t_rel, target, data in buf:
        REC._output(int(timestamp) + t_rel, target, data)

SYSCALLS = {n[len("_sys_"):]: f for n, f in globals().items() if n.startswith("_sys_")}
DRIVERS = ("core", "rtio", "ttl", "spi2", "urukul", "ad9910", "ad9912", "edge_counter", "ad53xx",
           "zotino", "sampler", "dma", "kasli_i2c", "i2c")


def patch_syscalls():
    for mod in DRIVERS:
        m = importlib.import_module(f"artiq.coredevice.{mod}")
        for name, f in SYSCALLS.items():
            if hasattr(m, name):
                setattr(m, name, f)


# ───────────────────────────── the core ─────────────────────────────────────────────────────
# driver methods whose CALLS are recorded at a high level (their arguments are the meaning)
INIT_STUBS = {"CPLD.init", "AD9910.init", "AD9912.init", "AD53xx.init", "Zotino.init", "Sampler.init",
              "AD9910.tune_io_update_delay", "AD9910.tune_sync_delay"}
BREAKS = {"Core.break_realtime", "Core.reset"}
# calls on devices the RISC-Q build has no counterpart for: recorded with their timeline cost
UNSUPPORTED = {"Sampler.sample", "Sampler.sample_mu", "Sampler.set_gain_mu", "AD53xx.write_dac",
               "AD53xx.write_dac_mu", "AD53xx.load"}


class RecCore:
    """A host-executing ARTIQ 8 core: kernels run in Python against the recorder."""

    kernel_invariants = {"core", "ref_period", "coarse_ref_period", "ref_multiplier"}

    def __init__(self, dmgr, ref_period=1e-9, ref_multiplier=8, **_):
        from artiq.coredevice.core import Core
        self._Core = Core
        self.ref_period = ref_period
        self.ref_multiplier = ref_multiplier
        self.coarse_ref_period = ref_period * ref_multiplier
        self.core = self
        self.dmgr = dmgr
        self.target_cls = None

    def _devname(self, obj):
        cache = self.__dict__.setdefault("_names", {})
        if id(obj) not in cache:
            ddb = self.dmgr.get_device_db()
            for desc, o in self.dmgr.active_devices:
                if o is obj:
                    cache[id(obj)] = next((n for n, d in ddb.items() if d == desc), type(obj).__name__)
                    break
            else:
                return type(obj).__name__
        return cache[id(obj)]

    def run(self, k_function, k_args, k_kwargs):
        fn = k_function.artiq_embedded.function
        qual = fn.__qualname__
        if qual in INIT_STUBS:
            REC.boundary("init", TM.get_time_mu(), fn=qual, dev=self._devname(k_args[0]))
            return None
        if qual in BREAKS:
            before = TM.get_time_mu()
            r = getattr(self._Core, qual.split(".")[1]).artiq_embedded.function(self, *k_args[1:], **k_kwargs)
            REC.boundary("break", TM.get_time_mu(), fn=qual, before=before)
            return r
        if qual == "CPLD.set_all_att_mu":
            t_call = TM.get_time_mu()
            r = fn(*k_args, **k_kwargs)
            cpld = self._devname(k_args[0])[:-len("_cpld")]
            reg = int(k_args[1] if len(k_args) > 1 else k_kwargs["att_reg"])
            for c in range(4):
                db = (255 - ((reg >> (8 * c)) & 0xFF)) * 0.125
                chan = f"{cpld}_ch{c}"
                if REC.att.get(chan) != db:
                    REC.att[chan] = db
                    REC.emit("att", TM.get_time_mu(), ch=chan, db=db, t0=t_call)
            return r
        if qual in ("AD9910.set_mu", "AD9912.set_mu"):
            dev = k_args[0]
            name = self._devname(dev)
            ftw = int(k_args[1] if len(k_args) > 1 else k_kwargs.get("ftw", 0))
            pow_ = int(k_args[2] if len(k_args) > 2 else k_kwargs.get("pow_", 0))
            if qual == "AD9910.set_mu":
                asf = int(k_args[3] if len(k_args) > 3 else k_kwargs.get("asf", 0x3FFF))
                prof = [dev.ftw_to_frequency(ftw), dev.asf_to_amplitude(asf), dev.pow_to_turns(pow_)]
            else:
                prof = [dev.ftw_to_frequency(ftw), 1.0, dev.pow_to_turns(pow_)]
            REC.pending.setdefault(name.split("_ch")[0], {})[name] = prof
            t_call, n_ev = TM.get_time_mu(), len(REC.events)
            r = fn(*k_args, **k_kwargs)
            for e in REC.events[n_ev:]:                   # the io_update this call pulsed
                if e["k"] == "dds" and e["ch"] == name:
                    e.update(t0=t_call, t1=TM.get_time_mu(), ftw=ftw, pow=pow_,
                             **({"asf": asf} if qual == "AD9910.set_mu" else {}))
            return r
        if qual == "AD53xx.set_dac":
            t = TM.get_time_mu()
            r = fn(*k_args, **k_kwargs)
            volts, chans = k_args[1], (k_args[2] if len(k_args) > 2 else list(range(len(k_args[1]))))
            REC.emit("dac", TM.get_time_mu(), dev=self._devname(k_args[0]), start=t,
                     ch=[int(c) for c in chans], v=[float(v) for v in volts])
            return r
        if qual in UNSUPPORTED:
            t0 = TM.get_time_mu()
            r = fn(*k_args, **k_kwargs)
            REC.emit("x", t0, fn=qual, dev=self._devname(k_args[0]), t1=TM.get_time_mu())
            return r
        return fn(*k_args, **k_kwargs)

    # Core API used by experiments (each a kernel on the real Core; run() handles the breaks)
    def seconds_to_mu(self, seconds):
        return np.int64(seconds // self.ref_period)

    def mu_to_seconds(self, mu):
        return mu * self.ref_period

    def reset(self):
        return self.run(self._Core.reset, (self,), {})

    def break_realtime(self):
        return self.run(self._Core.break_realtime, (self,), {})

    def get_rtio_counter_mu(self):
        return REC.counter

    def wait_until_mu(self, cursor_mu):
        REC.advance_counter(cursor_mu)

    def close(self):
        pass


class Paused(Exception):
    """The experiment was paused by the (modelled) scheduler: the end of a looping experiment."""


class RecScheduler:
    """check_pause() turns True after `pause_after` calls (None: never); pause() then ends the run —
    an ARTIQ experiment that loops until paused (pulser_delay) is run for a fixed number of checks."""
    rid, pipeline_name, priority, expid = 0, "main", 0, None

    def __init__(self, pause_after=None):
        self.pause_after, self.checks = pause_after, 0

    def check_pause(self, rid=None):
        self.checks += 1
        return self.pause_after is not None and self.checks > self.pause_after

    def pause(self):
        raise Paused()

    def submit(self, *a, **k):
        return 0


class RecDatasets:
    """In-memory DatasetManager (set / mutate / append_to / get / get_metadata)."""

    def __init__(self, initial: dict):
        self.data = {k: v for k, v in initial.items()}

    def set(self, key, value, metadata, broadcast, persist, archive):
        self.data[key] = value

    def mutate(self, key, index, value):
        self.data[key][index] = value

    def append_to(self, key, value):
        self.data.setdefault(key, []).append(value)

    def get(self, key, archive=False):
        return self.data[key]

    def get_metadata(self, key):
        return {}


class RecDeviceDB:
    def __init__(self, ddb):
        self.ddb = ddb

    def get_device_db(self):
        return self.ddb

    def get(self, key, resolve_alias=False):
        desc = self.ddb[key]
        while resolve_alias and isinstance(desc, str):
            desc = self.ddb[desc]
        return desc


def load_device_db() -> dict:
    ns: dict = {}
    exec((HERE / "device_db_ref.py").read_text(), ns)
    ddb = ns["device_db"]
    ddb["core"] = {"type": "local", "module": __name__, "class": "RecCore",
                   "arguments": {"ref_period": 1e-9, "ref_multiplier": 8}}
    return ddb


# datasets a fresh test_pmt.py run reads (UpdateParameters' defaults)
DEFAULT_DATASETS = {"resonance_point": 35.0, "qubit_freq": 148.280, "spinpol_freq": 145.280,
                    "DPQL_freq": 148.063}


def _jsonable(v):
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def run(exp_file: str, cls: str, arguments: dict, seed: int = 1, datasets: dict | None = None,
        no_analyze: bool = True, pause_after: int | None = None) -> dict:
    global REC
    sys.modules.setdefault(__name__, sys.modules[__name__])
    patch_syscalls()
    core_language.set_time_manager(TM)
    ddb = load_device_db()
    initial = {**DEFAULT_DATASETS, **(datasets or {})}
    REC = Recorder(ddb, IonModel(initial["resonance_point"], seed))
    dmgr = DeviceManager(RecDeviceDB(ddb), virtual_devices={"scheduler": RecScheduler(pause_after),
                                                            "ccb": types.SimpleNamespace(issue=lambda *a, **k: None)})
    dsets = RecDatasets(initial)
    module = file_import(exp_file, prefix="artiq_ref_")
    exp = get_experiment(module, cls)((dmgr, dsets, ProcessArgumentManager(arguments), {}))
    exp.prepare()
    paused = False
    try:
        exp.run()
    except Paused:
        paused = True
    if not no_analyze:
        exp.analyze()
    return {"experiment": cls, "file": str(exp_file), "arguments": arguments, "seed": seed,
            "events": REC.events, "gates": REC.gates,
            "datasets": {k: _jsonable(v) for k, v in dsets.data.items()},
            "segments": REC.segment + 1, "paused": paused}


def _parse_value(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file"); ap.add_argument("cls"); ap.add_argument("out")
    ap.add_argument("--set", action="append", default=[], help='"argument name=value" (JSON value)')
    ap.add_argument("--dataset", action="append", default=[], help='"key=value" initial dataset')
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--pause-after", type=int, default=None, help="scheduler.check_pause() calls before a pause")
    a = ap.parse_args(argv)
    args = {k: _parse_value(v) for k, v in (s.split("=", 1) for s in a.set)}
    dsets = {k: _parse_value(v) for k, v in (s.split("=", 1) for s in a.dataset)}
    res = run(a.file, a.cls, args, a.seed, dsets, pause_after=a.pause_after)
    Path(a.out).write_text(json.dumps(res))
    kinds: dict[str, int] = {}
    for e in res["events"]:
        kinds[e["k"]] = kinds.get(e["k"], 0) + 1
    print(f"[artiq_ref] {a.cls}: {len(res['events'])} events {kinds}, {len(res['gates'])} gates, "
          f"{res['segments']} segments -> {a.out}")


if __name__ == "__main__":
    main()
