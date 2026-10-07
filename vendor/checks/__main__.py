"""CLI: python -m checks run JOB_DIR [--out DIR] [--res 0.01] [--png] [--no-sim]
        python -m checks lint FILE.nc [--units inch|mm]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m checks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="nc_lint + stock_sim on a job folder")
    r.add_argument("job")
    r.add_argument("--out", help="output folder (default JOB/check_out)")
    r.add_argument("--res", type=float, help="Z-map resolution in job units")
    r.add_argument("--png", action="store_true", help="write gouge/leftover map images")
    r.add_argument("--no-sim", action="store_true", help="lint only")
    l_ = sub.add_parser("lint", help="nc_lint on NC files without a job (no stock geometry rules)")
    l_.add_argument("files", nargs="+")
    l_.add_argument("--units", default="inch")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        from .run import run_job
        rep = run_job(a.job, a.out, sim=not a.no_sim, res=a.res, png=a.png)
        out = Path(a.out) if a.out else Path(a.job) / "check_out"
        print((out / "report.md").read_text())
        return 0 if rep["status"] == "pass" else 1
    from . import nc_lint
    from .gcode import Interpreter
    bad = False
    for f in a.files:
        prog = Interpreter(a.units).run_file(f).program()
        res = nc_lint.lint(prog, units=a.units)
        print(json.dumps({"file": f, **res}, indent=2))
        bad |= any(x["severity"] == "error" for x in res["findings"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
