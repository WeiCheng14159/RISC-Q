"""Compare a RISC-Q run of the port against the ARTIQ 8 reference (riscq/ion/artiq_ref.py).

What is compared is what the apparatus sees, not how the drivers spelled it:
- every beam's RF output vs time — on/off, the realized frequency, the effective amplitude (DDS ASF ×
  attenuator) — as change points;
- every TTL output level; every PMT gate [open, close) and its count; the PMT dataset.

Timeline segments. ARTIQ's break_realtime / reset re-anchor `now` to the RTIO counter (real time), so
absolute times after a break depend on how fast the CPU ran — ARTIQ hardware and the reference model
differ there too. Both timelines are therefore cut at every break (both sides log every call), and
inside a segment every change point is compared relative to the segment's anchor: EXACT in ns (the
port's cursor runs ARTIQ's arithmetic), frequency within the RISC-Q/AD9910 grid difference (≤ 0.93 Hz),
amplitude within one RISC-Q amplitude code, counts exactly. A break that moved the cursor on one side
and not on the other is reported (a slack difference), not a mismatch."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from riscq.ion.port import AMP_SCALE, ARTIQ_DDS, ARTIQ_TTL_IN, ARTIQ_TTL_OUT, BEAMS

F_TOL_HZ = 1.0
A_TOL = 1.5 / AMP_SCALE
FS = 8e9

OP_SW, OP_DDS, OP_ATT, OP_GATE, OP_COUNT, OP_BREAK, OP_TTL, OP_RESET, OP_X = 1, 2, 3, 4, 5, 6, 7, 8, 10
X_IDS = {1: "Sampler.set_gain_mu", 2: "Sampler.sample", 3: "AD53xx.set_dac"}    # ion.h ion_unsupported ids
X_TOP = set(X_IDS.values())                  # top-level calls (nested driver calls are not compared)


@dataclass
class Timeline:
    """Segments of change points: seg -> device -> [(t_rel_ns, state)], gates [(seg, line, open, close, n)]."""
    segs: list = field(default_factory=lambda: [defaultdict(list)])
    anchors: list = field(default_factory=lambda: [0])
    moved: list = field(default_factory=lambda: [None])
    gates: list = field(default_factory=list)

    def cut(self, t_anchor, moved):
        self.segs.append(defaultdict(list))
        self.anchors.append(int(t_anchor))
        self.moved.append(moved)

    def add(self, dev, t, state):
        """A change point when — and only when — the device's output changes (tracked across segments;
        beams start dark, TTLs low)."""
        last = self.__dict__.setdefault("_last", {})
        if last.get(dev, (0, None, None) if not dev.startswith(("ttl", "x:")) else 0) == state:
            return
        seg = len(self.segs) - 1
        self.segs[seg][dev].append((int(t) - self.anchors[seg], state))
        last[dev] = state


class _Beam:
    """A beam's output: (on, f, a) with a = amp × attenuation; off reads (0, None, None)."""

    def __init__(self):
        self.on, self.f, self.a, self.att = 0, None, 1.0, 1.0

    def out(self):
        return (1, self.f, round(self.a * self.att, 7)) if self.on else (0, None, None)


def _all_beams():
    """Every beam tracked from the start (a dark beam's settings are not change points)."""
    return {b: _Beam() for b in BEAMS}


def from_artiq(ref: dict) -> Timeline:
    tl, beams, ttl = Timeline(), _all_beams(), {}
    for e in ref["events"]:
        k, t = e["k"], e["t"]
        if k in ("break", "init"):
            tl.cut(t, e.get("t", 0) != e.get("before", e.get("t", 0)) if k == "break" else None)
            continue
        if k in ("sw", "dds", "att"):
            b = ARTIQ_DDS.get(e["ch"])
            if b is None:
                continue
            st = beams[b]
            if k == "sw":
                st.on = int(e["on"])
            elif k == "dds":
                st.f, st.a = float(e["f"]), float(e["amp"])
            else:
                st.att = 10 ** (-float(e["db"]) / 20)
            tl.add(b, t, st.out())
        elif k == "ttl" and e["ch"] in ARTIQ_TTL_OUT:
            line = ARTIQ_TTL_OUT[e["ch"]]
            ttl[line] = int(e["on"])
            tl.add(f"ttl{line}", t, ttl[line])
        elif k == "x" and e["fn"] in X_TOP:
            n = tl.__dict__.setdefault("_xn", defaultdict(int))
            n[e["fn"]] += 1
            tl.add(f"x:{e['fn']}", t, n[e["fn"]])
        elif k == "dac":
            n = tl.__dict__.setdefault("_xn", defaultdict(int))
            n["AD53xx.set_dac"] += 1
            tl.add("x:AD53xx.set_dac", e.get("start", t), n["AD53xx.set_dac"])
        elif k == "gate":
            seg = len(tl.segs) - 1
            tl.gates.append((seg, ARTIQ_TTL_IN.get(e["ch"], e["ch"]), int(t) - tl.anchors[seg],
                             int(e["close"]) - tl.anchors[seg], int(e["count"])))
    return tl


def _unwrap(lo32: np.ndarray) -> np.ndarray:
    """The log keeps the low 32 bits of the ns cursor; it advances (bar ±8 ns re-alignments)."""
    out = np.empty(len(lo32), dtype=np.int64)
    base, prev = 0, None
    for i, v in enumerate(lo32.astype(np.int64) & 0xFFFFFFFF):
        if prev is not None and v - prev < -(1 << 31):
            base += 1 << 32
        elif prev is not None and v - prev > (1 << 31):
            base -= 1 << 32
        out[i] = base + v
        prev = v
    return out


def from_riscq(log: np.ndarray) -> Timeline:
    tl, beams, ttl = Timeline(), _all_beams(), {}
    log = np.asarray(log, dtype=np.int64)
    if len(log) == 0:
        return tl
    ops, idx = log[:, 0] & 0xFF, (log[:, 0] >> 8) & 0xFF
    ts = _unwrap(log[:, 1])
    pending_gate = {}
    for op, i, t, a, c in zip(ops, idx, ts, log[:, 2], log[:, 3]):
        if op in (OP_BREAK, OP_RESET):
            m = int(t) + (((int(a) & 0xFFFFFFFF) - (int(t) & 0xFFFFFFFF) + (1 << 31)) % (1 << 32)) - (1 << 31)
            anchor = max(int(t), m) if op == OP_BREAK else m
            tl.cut(anchor, bool(i) if op == OP_BREAK else None)
            continue
        if op in (OP_SW, OP_DDS, OP_ATT):
            b = BEAMS[int(i)]
            st = beams[b]
            if op == OP_SW:
                st.on = int(a)
            elif op == OP_DDS:
                st.f, st.a = int(a) * FS / 2 ** 32, int(c) / AMP_SCALE
                st._acode = int(c)
            else:
                st.att = int(a) / (1 << 15)
                st._attq = int(a)
            # the hardware amplitude is the integer product, (acode·attq + 2^14) >> 15
            ac, aq = getattr(st, "_acode", None), getattr(st, "_attq", 1 << 15)
            if ac is not None:
                st.a, st.att = ((ac * aq + (1 << 14)) >> 15) / AMP_SCALE, 1.0
            tl.add(b, t, st.out())
        elif op == OP_X:
            n = tl.__dict__.setdefault("_xn", defaultdict(int))
            fn = X_IDS.get(int(i), f"x{int(i)}")
            n[fn] += 1
            tl.add(f"x:{fn}", t, n[fn])
        elif op == OP_TTL:
            ttl[int(i)] = int(a)
            tl.add(f"ttl{int(i)}", t, int(a))
        elif op == OP_GATE:
            seg = len(tl.segs) - 1
            close = int(t) + ((int(a) - int(t)) & 0xFFFFFFFF)
            pending_gate[int(i)] = (seg, int(i), int(t) - tl.anchors[seg], close - tl.anchors[seg])
        elif op == OP_COUNT:
            g = pending_gate.pop(int(i))
            tl.gates.append((*g, int(a)))
    return tl


def _state_eq(sa, sb):
    if not isinstance(sa, tuple) or not isinstance(sb, tuple):   # TTL levels, unsupported-call counts
        return sa == sb
    if sa[0] != sb[0]:
        return False
    if sa[0] == 0:
        return True
    if (sa[1] is None) != (sb[1] is None):
        return False
    return (sa[1] is None or abs(sa[1] - sb[1]) <= F_TOL_HZ) and abs(sa[2] - sb[2]) <= A_TOL


def compare(ref: dict, riscq_log, riscq_pmt=None, max_segments: int | None = None) -> dict:
    A, R = from_artiq(ref), from_riscq(riscq_log)
    nseg = min(len(A.segs), len(R.segs)) if max_segments is None else max_segments
    mism, n_pts, worst_df = [], 0, 0.0
    slack = sum(1 for s in range(1, nseg) if A.moved[s] is not None and R.moved[s] is not None
                and A.moved[s] != R.moved[s])
    for s in range(nseg):
        devs = set(A.segs[s]) | set(R.segs[s])
        for d in sorted(devs):
            pa, pr = A.segs[s].get(d, []), R.segs[s].get(d, [])
            if len(pa) != len(pr):
                mism.append({"seg": s, "dev": d, "why": f"{len(pa)} vs {len(pr)} change points",
                             "artiq": pa[:4], "riscq": pr[:4]})
                continue
            for (ta, sa), (tr, sr) in zip(pa, pr):
                n_pts += 1
                if isinstance(sa, tuple) and isinstance(sr, tuple) and sa[0] and sr[0] \
                        and sa[1] is not None and sr[1] is not None:
                    worst_df = max(worst_df, abs(sa[1] - sr[1]))
                if ta != tr or not _state_eq(sa, sr):
                    mism.append({"seg": s, "dev": d, "artiq": (ta, sa), "riscq": (tr, sr)})
    ga = [g for g in A.gates if g[0] < nseg]
    gr = [g for g in R.gates if g[0] < nseg]
    gate_mism = [{"artiq": a, "riscq": b} for a, b in zip(ga, gr) if a[1:] != b[1:]]
    if len(ga) != len(gr):
        gate_mism.append({"why": f"{len(ga)} vs {len(gr)} gates"})
    res = {"segments": nseg, "segments_artiq": len(A.segs), "segments_riscq": len(R.segs),
           "change_points": n_pts, "mismatches": len(mism), "first_mismatches": mism[:8],
           "gates": len(ga), "gate_mismatches": len(gate_mism), "first_gate_mismatches": gate_mism[:4],
           "worst_freq_diff_hz": worst_df, "slack_differences": slack}
    if riscq_pmt is not None:
        pa = list(ref["datasets"].get("PMT", []))[:len(riscq_pmt)]
        res["pmt_equal"] = pa == list(riscq_pmt)
        res["pmt_compared"] = len(pa)
    res["match"] = (res["mismatches"] == 0 and res["gate_mismatches"] == 0
                    and res.get("pmt_equal", True))
    return res


def load(path):
    return json.loads(open(path).read())
