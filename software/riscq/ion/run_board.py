"""Run a ported test_pmt.py experiment on a board (or co-sim) and compare it with the ARTIQ 8 reference.

    python -m riscq.ion.run_board <host> <ref.json> <out.json> [--emulate]       # a board server
    python -m riscq.ion.run_board --cosim <config.json> <ref.json> <out.json> [--npts N --reps R]

The experiment class, its arguments and datasets come from the reference (artiq_ref.py output), so
both sides run the same configuration. --emulate feeds the reference's photon counts through the TTL
loopback (out 0 -> PMT counter); without it the counter reads the real PMOD1 input."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from riscq.ion import compare, port
from riscq.map import SocMap, SocParams


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("target"); ap.add_argument("ref"); ap.add_argument("out")
    ap.add_argument("--cosim", action="store_true", help="target is a SocParams config: run in co-sim")
    ap.add_argument("--emulate", action="store_true")
    ap.add_argument("--npts", type=int); ap.add_argument("--reps", type=int)
    ap.add_argument("--log-words", type=int, default=1 << 21)
    a = ap.parse_args(argv)
    ref = json.loads(Path(a.ref).read_text())
    if a.cosim:
        from riscq.sim import server
        cfg = Path(a.target).resolve()
        drv = server.start(cfg, Path("build").resolve() / f"sim-{cfg.stem.split('-', 1)[-1]}")
        params = drv.sim.get_params()
    else:
        from riscq.driver.remote import RemoteDriver
        drv = RemoteDriver(a.target, 9091)
        params = drv.board.get_params()
    m = SocMap(SocParams.from_json(params))
    args = dict(ref["arguments"])
    if ref.get("paused"):                       # a loop-until-paused experiment: the same number of passes
        args["iterations"] = len(ref["gates"])
    exp = port.EXPERIMENTS[ref["experiment"]](args)
    exp.prepare()
    npts = a.npts if a.npts is not None else len(exp.span())
    reps = a.reps if a.reps is not None else exp.reptime
    emu = [g["count"] for g in ref["gates"]] if (a.emulate or a.cosim) else None
    t0 = time.time()
    r = exp.run(drv, m, emu_counts=emu, npts=npts, reps=reps, log_words=a.log_words)
    wall = time.time() - t0
    full = npts == len(exp.span()) and reps == exp.reptime
    R = compare.from_riscq(r.log)
    res = compare.compare(ref, r.log, r.pmt, max_segments=None if full else len(R.segs) - 1)
    res.update({"wall_s": round(wall, 2), "stat": r.stat, "npts": npts, "reps": reps,
                "emulated_pmt": emu is not None})
    if hasattr(exp, "analyze") and full:
        res["analysis_riscq"] = exp.analyze(r.pmt)
        res["analysis_artiq"] = exp.analyze(ref["datasets"].get("PMT", []))
    Path(a.out).write_text(json.dumps({"result": res, "pmt": r.pmt, "log": r.log.tolist()}))
    print(json.dumps(res, indent=1, default=str)[:4000])
    if a.cosim:
        from riscq.sim import server
        server.stop(drv)
    else:
        drv.close()


if __name__ == "__main__":
    main()
