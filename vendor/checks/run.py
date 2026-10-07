"""Run nc_lint and stock_sim on a job folder and write report.json + report.md."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import nc_lint, stock_sim
from .gcode import Interpreter
from .job import Job, load_job


def _home_z(job: Job, setup) -> float:
    lo, hi = job.stock_box_wcs(setup)
    return float(hi[2] + job.machine["home_z_above_stock"])


def check_setup(job: Job, setup, *, sim=True, res=None, out_dir=None, png=False, column=None,
                expect_finish=True) -> dict:
    lo, hi = job.stock_box_wcs(setup)
    home_z = _home_z(job, setup)
    min_z = setup.min_tool_z
    lint_res, moves = [], []
    stock_at = (lambda f, ln: setup.stock_box_at(Path(f).name, ln)) if setup.stock_boxes else None
    for f in setup.nc_files:
        prog = Interpreter(job.units, home_z).run_file(f).program()
        r = nc_lint.lint(prog, units=job.units, tools=job.tools, machine=job.machine, tol=job.tol,
                         lint_cfg=job.lint, stock_box_wcs=(lo, hi), min_tool_z=min_z, home_z=home_z,
                         stock_at=stock_at)
        # the setup's WCS must match what the program selects
        wcs_events = {e.data["wcs"] for e in prog.events if e.kind == "wcs"}
        if wcs_events and setup.wcs not in wcs_events:
            r["findings"].insert(0, {"severity": "error", "rule": "wcs", "file": f.name, "line": 0, "count": 1,
                                     "lines": [], "message": f"program uses {sorted(wcs_events)}, job setup "
                                     f"expects {setup.wcs}"})
        lint_res.append({"file": f.name, **r})
        moves.extend(prog.moves)

    failures, warnings = [], []
    for lr in lint_res:
        for fd in lr["findings"]:
            tgt = failures if fd["severity"] == "error" else warnings if fd["severity"] == "warning" else None
            if tgt is not None:
                tgt.append(f"nc_lint {fd['rule']}: {fd['message']} ({fd['file']}:{fd['line']}"
                           + (f", x{fd['count']}" if fd["count"] > 1 else "") + ")")

    sim_res = None
    if sim:
        sim_res = stock_sim.simulate_setup(job, setup, moves, res=res, save_dir=out_dir, png=png, column=column)
        u = job.units
        if sim_res["missing_tools"]:
            failures.append(f"stock_sim: tools missing from the tool list: {sim_res['missing_tools']}")
        if sim_res["tools_not_simulated"]:
            warnings.append(f"stock_sim: undercut tools not simulated (Z-map): {sim_res['tools_not_simulated']}")
        if sim_res["tools_without_holder"]:
            warnings.append(f"stock_sim: no holder data, holder collisions not checked: "
                            f"{sim_res['tools_without_holder']}")
        for key in ("rapid_through_stock", "holder_collision", "beyond_flute_length", "thread_tool_in_solid"):
            r = sim_res[key]
            if r["count"]:
                w = r["worst"][0]
                failures.append(f"stock_sim {key}: {r['count']} line(s); worst {w['max']:.4f} {u} at "
                                f"{w['file']}:{w['line']} T{w['tool']}"
                                + (f" ({w['part']})" if w.get("part") else "") + f" `{w['text']}`")
        if "gouge" in sim_res:
            g = sim_res["gouge"]
            if g["cells"]:
                where = g["regions"][0]["worst_xy"] if g["regions"] else None
                lines = ", ".join(f"{x['file']}:{x['line']}" for x in g["lines"][:3])
                failures.append(f"stock_sim gouge: max {g['max']:.4f} {u}, {g['area_pct']}% of part area, "
                                f"worst at XY {where}; lines {lines}")
            lf = sim_res["leftover"]
            t = job.tol
            big = [r for r in lf["regions"] if r["max"] > t.get("leftover_max", 4 * t["leftover"])
                   and r["cells"] >= t.get("leftover_min_region_cells", 9)]
            if lf["cells"]:
                msg = (f"stock_sim leftover: max {lf['max']:.4f} {u}, {lf['area_pct']}% of part area, "
                       f"worst at XY {lf['regions'][0]['worst_xy'] if lf['regions'] else None}")
                if expect_finish and (lf["area_pct"] > t["leftover_area_pct_max"] or big):
                    failures.append(msg)
                else:
                    warnings.append(msg)
    return {"setup": setup.name, "wcs": setup.wcs, "nc_files": [f.name for f in setup.nc_files],
            "status": "fail" if failures else "pass", "failures": failures, "warnings": warnings,
            "nc_lint": lint_res, "stock_sim": sim_res}


def run_job(job_dir, out_dir=None, *, sim=True, res=None, png=False) -> dict:
    t0 = time.perf_counter()
    job = load_job(job_dir)
    out_dir = Path(out_dir) if out_dir else Path(job_dir) / "check_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    multi = len(job.setups) > 1
    combined = sim and multi and job.part_path is not None and all(
        stock_sim.z_sign(s) is not None for s in job.setups)
    column = stock_sim.ColumnStock(job, res or job.sim["resolution"]) if sim else None
    setups = []
    for s in job.setups:
        expect = s.expect_finish if s.expect_finish is not None else not combined
        setups.append(check_setup(job, s, sim=sim, res=res, out_dir=out_dir, png=png, column=column,
                                  expect_finish=expect))
    report = {"job": str(Path(job_dir).resolve()), "units": job.units, "status": "pass",
              "runtime_s": 0.0, "setups": setups}
    if combined:
        fin = stock_sim.final_check(job, column, save_dir=out_dir, png=png)
        fin["failures"] = []
        lf, t = fin["leftover"], job.tol
        big = [r for r in lf["regions"] if r["max"] > t["leftover_max"] and r["cells"] >= t["leftover_min_region_cells"]]
        if lf["cells"] and (lf["area_pct"] > t["leftover_area_pct_max"] or big):
            fin["failures"].append(
                f"final leftover after all setups: max {lf['max']:.4f} {job.units}, {lf['area_pct']}% of part "
                f"area, worst at part XY {lf['regions'][0]['worst_xy']} (from top {fin['from_top_max']}, "
                f"from bottom {fin['from_bottom_max']}, outside part {fin['outside_part_max']})")
        report["final"] = fin
    elif sim and multi:
        report["final"] = {"note": "combined leftover skipped: a setup is not parallel to part +/-Z", "failures": []}
    if sim and job.part_path is not None:
        # 3D voxel simulation across all setups (side/tilted included); authoritative for the final part.
        try:
            from . import voxel_sim
            vx = voxel_sim.run_voxel(job_dir, out_dir, res=res, png=png)
            report["voxel"] = {k: vx.get(k) for k in ("status", "failures", "warnings", "final", "runtime_s") if k in vx}
        except Exception as exc:  # never lose the Z-map report because the voxel sim broke
            report["voxel"] = {"status": "error", "failures": [f"voxel_sim crashed: {exc}"]}
    if report.get("voxel", {}).get("status") == "pass":
        # The 3D voxel result (edge-break aware, exact final comparison) overrides Z-map gouge/leftover calls.
        for s in setups:
            keep = [f for f in s.get("failures", []) if not f.startswith(("stock_sim gouge", "stock_sim leftover"))]
            moved = [f for f in s.get("failures", []) if f not in keep]
            if moved:
                s.setdefault("warnings", []).extend(f"(voxel_sim passed) {f}" for f in moved)
                s["failures"] = keep
                s["status"] = "fail" if keep else "pass"
        if report.get("final", {}).get("failures"):
            report["final"]["warnings"] = report["final"].pop("failures"); report["final"]["failures"] = []
    fails = any(s["status"] == "fail" for s in setups) or bool(report.get("final", {}).get("failures")) \
        or report.get("voxel", {}).get("status") in ("fail", "error")
    report["status"] = "fail" if fails else "pass"
    report["runtime_s"] = round(time.perf_counter() - t0, 2)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (out_dir / "report.md").write_text(markdown(report))
    return report


def markdown(report: dict) -> str:
    L = [f"# CNC check: {report['status'].upper()}", "",
         f"Job `{report['job']}` ({report['units']}), {report['runtime_s']} s", ""]
    for s in report["setups"]:
        L.append(f"## {s['setup']} ({s['wcs']}): {s['status'].upper()}")
        L.append(f"Files: {', '.join(s['nc_files'])}")
        for lr in s["nc_lint"]:
            st = lr["stats"]
            L.append(f"- `{lr['file']}`: {st['moves']} moves, tools {st['tools_used']}, "
                     f"est. cycle {st['cycle_time_min_est']} min")
        sim = s["stock_sim"]
        if sim:
            part = (f"gouge max {sim['gouge']['max']} ({sim['gouge']['area_pct']}%), leftover max "
                    f"{sim['leftover']['max']} ({sim['leftover']['area_pct']}%), ") if "gouge" in sim else "no part model, "
            L.append(f"- stock_sim at {sim['resolution']} ({sim['initial_stock']}): {part}rapids through stock "
                     f"{sim['rapid_through_stock']['count']}, holder hits {sim['holder_collision']['count']}, "
                     f"beyond flute {sim['beyond_flute_length']['count']}, thread tool in solid "
                     f"{sim['thread_tool_in_solid']['count']}; {sim['timing_s']['total']} s")
        if s["failures"]:
            L += ["", "Failures:"] + [f"- {x}" for x in s["failures"]]
        if s["warnings"]:
            L += ["", "Warnings:"] + [f"- {x}" for x in s["warnings"][:20]]
            if len(s["warnings"]) > 20:
                L.append(f"- ... {len(s['warnings']) - 20} more in report.json")
        L.append("")
    fin = report.get("final")
    if fin:
        L.append("## Final (all setups, part frame)")
        if "leftover" in fin:
            lf = fin["leftover"]
            L.append(f"- leftover max {lf['max']} ({lf['area_pct']}%): from top {fin['from_top_max']}, "
                     f"from bottom {fin['from_bottom_max']}, outside part {fin['outside_part_max']}")
        if fin.get("note"):
            L.append(f"- {fin['note']}")
        L += [f"- FAIL: {x}" for x in fin.get("failures", [])]
        L.append("")
    return "\n".join(L)
