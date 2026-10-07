"""Process lint: machinist-judgment rules from the wiki, checked on a programmed job.

    python -m checks.process_lint <job_dir> [--out DIR] [--material 6061-T6] [--write-ops]

Inputs found in <job_dir> (or its checks_job/ and parent folder, which is how harness runs are laid out):
- features.json  (checks.features output; computed from part.step when missing)
- tools.json     (camkit.tools_used(), the native checks format, or raw Fusion tool JSON)
- operations.json (harness/fusion_side/export_ops.py; schema below). When it is missing the operation list is
  reconstructed from the posted NC (op comments, tool changes, S/F words, canned cycles, circular moves) and
  marked "source": "reconstructed_nc": strategy then comes from the op name, and engagement is unknown.
- nc/*.nc or *.nc (posted Haas programs), job.json (checks job: per-setup part->WCS transforms and stock)
- spec.json (material, feature tolerances) when available

Output: <out>/process_lint.json and process_lint.md (default out = <job_dir>/process_lint/). Exit 1 on any fail.

Rules (id, severity, wiki reference) are listed in RULES below. Each finding names the feature and operation.

operations.json schema ("helios.cam.operations/1"), lengths in `units`:
{
  "schema": "helios.cam.operations/1", "source": "fusion_export" | "reconstructed_nc", "units": "inch",
  "document": "<name>", "material": "<text or null>",
  "setups": [{"name": "OP1", "wcs": "G54", "program": "1001", "nc": ["1001.nc"],
              "matrix": [[4x4 row-major part/model -> WCS]] | null, "tool_axis_part": [x, y, z] | null,
              "stock": {"min": [..], "max": [..]} | null}],
  "operations": [{
     "id": "OP1-02", "setup": "OP1", "name": "Adaptive walls", "order": 2,
     "strategy": "adaptive2d" | "contour2d" | "face" | "drill" | "bore" | "circular" | "thread" | "chamfer2d" | ...,
     "tool": {"number": 2, "type": "flat end mill", "diameter": .625, "flutes": 4, "corner_radius": 0,
              "flute_length": 1.25, "stickout": 1.875, "tip_angle": null, "material": "carbide", "description": ".."},
     "rpm": 2445, "feed_cutting": 36, "feed_plunge": 12, "feed_per_rev": null,
     "stepdown": .5, "stepover": null, "optimal_load": .06, "stock_to_leave": .01, "axial_stock_to_leave": .005,
     "cycle": "G83" | "deep-drilling" | null, "peck": .15, "dwell": .2,
     "feature_refs": ["H03"], "geometry_refs": ["face:12"],
     "nc": {"file": "1001.nc", "first_line": 38, "last_line": 313},
     "parameters": {"<fusion parameter name>": "<expression>"}   (optional, raw)
  }]
}
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import hole_tables as HT

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
from tooling import feeds as FEEDS  # noqa: E402

MAX_TOOLS = 20
RAPID_FEED = 649.0      # the Haas post writes G1 F650. for positioning; treat >= this as rapid

RULES = {
    "hole_endmilled": ("fail", "hole-making s0, s13.1; milling-tools R3 (POSTMORTEM: 'drill instead of the endmill')"),
    "thin_wall_drilled": ("warn", "hole-making s11b (thin walls: interpolate, or small supported drill at reduced feed)"),
    "thin_wall_large_drill": ("fail", "hole-making s11b ('never drill a large hole > ~.375 through a thin wall')"),
    "thin_wall_ream": ("warn", "hole-making s11b ('never ream through a thin wall unsupported')"),
    "thin_wall_backed": ("info", "hole-making s11b (the wall is still backed by solid stock when the hole is made: "
                                 "the thin-wall rules do not apply; process-planning: holes before the pocket)"),
    "tap_drill_wrong": ("fail", "hole-making s7 tap drill tables"),
    "tap_no_spot": ("warn", "hole-making s1 (spot/chamfer -> tap drill -> tap), s11b"),
    "tap_too_deep": ("fail", "hole-making s7 blind-hole depth (tap Z < drill full depth)"),
    "tap_feed": ("fail", "hole-making s7 / cd s8.3 (G84 F = rpm x pitch exactly)"),
    "tight_hole_no_finish": ("fail", "hole-making s1 (H7: drill -> ream, or bore); tolerances s4.2, s8"),
    "ream_allowance": ("warn", "hole-making s4 reaming allowance (MH p2171, Smid p231, TBM p333)"),
    "ream_rapid_out": ("fail", "hole-making s4 (feed in and out, G85/G89; never rapid out)"),
    "corner_radius": ("fail", "milling-tools s4.1 (finishing tool radius < corner radius; Rt <= 0.8 Rc)"),
    "corner_radius_margin": ("warn", "milling-tools s4.1 default margin Rt <= 0.8 x Rc"),
    "stickout": ("warn", "cd s6 stick-out (L/D 4-6: sfm -20 %, fz -40 %, ap <= 1 D); milling-tools s4.2"),
    "stickout_extreme": ("fail", "cd s6 (L/D > 6: rethink the tool)"),
    "cutting_data": ("warn", "cutting-data-and-materials s4, s5, s8 (via tooling/feeds.py)"),
    "preset_feeds": ("fail", "PROGRAMMER.md / hole-making s0 (library presets 5000 rpm / 39 ipm are placeholders)"),
    "power": ("warn", "cd s3/s7 VF-2 power and torque"),
    "peck": ("warn", "hole-making s3 'Peck or not' (> 3 x D: peck)"),
    "drill_order": ("warn", "hole-making s11b (drill the larger/longer hole first, then break in)"),
    "chamfer_tool_profiling": ("fail", "milling-tools R8 (chamfer mills / spot drills: chamfers, spots, edge breaks only)"),
    "face_mill_edge": ("warn", "milling-tools s2 / cd s6 (never run the face-mill centreline on the stock edge)"),
    "countersink_angle": ("fail", "hole-making s9 (82° inch / 90° metric countersinks; tool angle = feature angle)"),
    "tool_count": ("fail", "milling-tools s4.3 (VF-2 carries 20 tools)"),
    "setup_split": ("warn", "tolerances-fits-finish s6.3 (features toleranced to each other in one setup)"),
    "unmapped": ("info", "process_lint (a hole or op could not be linked; rules for it were skipped)"),
}


# ------------------------------------------------------------------------------------------------ loading
def _first(*paths):
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None


def locate(job_dir: Path) -> dict:
    j = Path(job_dir)
    cj = j / "checks_job"
    return {
        "operations": _first(j / "operations.json"),
        "features": _first(j / "features.json", cj / "features.json"),
        "tools": _first(j / "tools.json", cj / "tools.json"),
        "job": _first(j / "job.json", cj / "job.json"),
        "spec": _first(j / "spec.json", j.parent / "spec.json"),
        "step": _first(j / "part.step", cj / "part.step", j.parent / "part.step"),
        "plan": _first(j / "process_plan.md"),
        "nc_dir": _first(j / "nc", cj) or j,
    }


def norm_tool(t: dict) -> dict:
    """Any tools.json row -> {number, kind, type, diameter, flutes, stickout, flute_length, corner_radius,
    tip_angle, material, description} (inches)."""
    if "geometry" in t:  # raw Fusion tool JSON
        g, pp = t["geometry"], t.get("post-process", {})
        f = 1.0 if str(t.get("unit", "inches")).startswith("inch") else 1 / 25.4
        row = {"number": pp.get("number"), "type": t.get("type"), "diameter": (g.get("DC") or 0) * f,
               "flutes": g.get("NOF"), "stickout": (g.get("LB") or 0) * f, "flute_length": (g.get("LCF") or 0) * f,
               "corner_radius": (g.get("RE") or 0) * f, "tip_angle": g.get("SIG") or (2 * g["TA"] if g.get("TA") else None),
               "material": t.get("BMC"), "description": t.get("description", "")}
    else:
        g = lambda *ks: next((t[k] for k in ks if t.get(k) is not None), None)  # noqa: E731
        row = {"number": g("number", "T", "tool_number"), "type": g("type"), "diameter": g("diameter_in", "diameter"),
               "flutes": g("flutes", "NOF"), "stickout": g("stickout_in", "stickout"),
               "flute_length": g("flute_length_in", "flute_length"), "corner_radius": g("corner_radius_in", "corner_radius"),
               "tip_angle": g("tip_angle_deg", "point_angle_deg", "point_angle", "tip_angle"),
               "material": g("material"), "description": g("description", "crib_description", "name") or ""}
    desc = (row.get("description") or "").lower()
    kind = FEEDS.tool_kind(row.get("type") or "flat")
    mat = str(row.get("material") or "").lower()
    if "carbide" in desc:
        mat = "carbide"
    elif "hss" in desc or "cobalt" in desc:
        mat = "hss"
    elif mat not in ("carbide", "hss"):
        mat = "hss" if kind in ("drill", "tap") else "carbide"
    row["material"] = mat
    row["kind"] = kind
    for k in ("diameter", "stickout", "flute_length", "corner_radius"):
        row[k] = float(row[k]) if row.get(k) is not None else None
    return row


TOOL_LENGTHS = ("diameter", "stickout", "flute_length", "corner_radius", "diameter_in", "stickout_in",
                "flute_length_in", "corner_radius_in", "tip_diameter", "shank_diameter")


def load_tools(path: Path | None) -> dict[int, dict]:
    """tools.json -> {number: normalised row (inches)}. Native rows in a file with "units": "mm" are
    converted (raw Fusion JSON carries its own unit)."""
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    rows = data.get("tools", data.get("data")) if isinstance(data, dict) else data
    if isinstance(data, dict) and str(data.get("units", "inch")).lower() in ("mm", "millimeter", "millimeters"):
        rows = [r if (not isinstance(r, dict) or "geometry" in r or "tools" in r) else
                {**r, **{k: r[k] / 25.4 for k in TOOL_LENGTHS if isinstance(r.get(k), (int, float))}}
                for r in rows or []]
    flat = []
    for r in rows or []:
        if isinstance(r, dict) and "tools" in r and "setup" in r:
            flat += r["tools"]
        else:
            flat.append(r)
    out = {}
    for r in flat:
        t = norm_tool(r)
        if t["number"] is not None:
            out.setdefault(int(t["number"]), t)
    return out


def nc_header_tools(text: str) -> dict[int, dict]:
    """(T1 D=2. CR=0. - ZMIN=0. - face mill) lines from the Fusion Haas post."""
    out = {}
    for m in re.finditer(r"^\(T(\d+) D=([\d.]+) CR=([\d.]+)(?: TAPER=([\d.]+)deg)? - ZMIN=([-\d.]+) - ([^)]+)\)", text, re.M):
        out[int(m.group(1))] = norm_tool({"number": int(m.group(1)), "type": m.group(6).strip(),
                                          "diameter": float(m.group(2)), "corner_radius": float(m.group(3)),
                                          "tip_angle": float(m.group(4)) if m.group(4) else None})
    return out


# ------------------------------------------------------------------------------------------------ NC reconstruction
WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


def _words(line: str) -> dict:
    s = re.sub(r"\([^)]*\)", " ", line).split(";")[0].upper()
    out = defaultdict(list)
    for k, v in WORD.findall(s):
        out[k].append(float(v))
    return out


def segment_nc(path: Path) -> list[dict]:
    """Operations of a posted program: an op starts at a comment-only line after a blank line."""
    lines = Path(path).read_text(errors="replace").splitlines()
    starts = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if re.fullmatch(r"\([^)]*\)", s) and i > 0 and not lines[i - 1].strip():
            starts.append((i + 1, s[1:-1].strip()))
    if not starts:  # no op comments: one op per tool change
        for i, ln in enumerate(lines):
            if re.search(r"\bM0?6\b", ln):
                t = _words(ln).get("T")
                starts.append((i + 1, f"T{int(t[0])}" if t else "tool change"))
    ops = []
    for k, (ln, name) in enumerate(starts):
        end = starts[k + 1][0] - 1 if k + 1 < len(starts) else len(lines)
        ops.append({"name": name, "file": Path(path).name, "first_line": ln, "last_line": end})
    return ops


def analyze_segment(path: Path, seg: dict, active_tool: int | None, active_rpm: float | None = None) -> dict:
    """Tool, rpm, feeds, canned cycles and hole positions (WCS) of one op from the raw NC lines."""
    lines = Path(path).read_text(errors="replace").splitlines()[seg["first_line"] - 1: seg["last_line"]]
    tool, rpm = active_tool, None
    feeds, cycles, zs = [], [], []
    modal_g, pos = None, {"X": None, "Y": None, "Z": None}
    cyc = None
    for off, raw in enumerate(lines):
        lineno = seg["first_line"] + off
        w = _words(raw)
        if not w:
            continue
        gs = [int(g) for g in w.get("G", [])]
        if "M" in w and any(int(m) == 6 for m in w["M"]) and "T" in w:
            tool = int(w["T"][0])
        if "S" in w:
            rpm = max(rpm or 0, w["S"][0])
        for g in gs:
            if g in (0, 1, 2, 3):
                modal_g = g
            if g == 80:
                cyc = None
        cg = [g for g in gs if g in (73, 74, 76, 81, 82, 83, 84, 85, 86, 87, 88, 89)]
        if cg:
            cyc = {"code": f"G{cg[0]}", "Z": w.get("Z", [None])[0], "R": w.get("R", [None])[0],
                   "Q": w.get("Q", [None])[0], "P": w.get("P", [None])[0], "F": w.get("F", [None])[0],
                   "K": w.get("K", [None])[0], "line": lineno}
        for ax in ("X", "Y", "Z"):
            if ax in w and not (cyc and ax == "Z" and cg):
                pos[ax] = w[ax][0]
        if cyc and ("X" in w or "Y" in w or cg):
            x = w["X"][0] if "X" in w else pos["X"]
            y = w["Y"][0] if "Y" in w else pos["Y"]
            if "Z" in w and not cg:
                cyc["Z"] = w["Z"][0]
            cycles.append({**cyc, "x": x, "y": y, "line": lineno})
            continue
        if "F" in w and not cyc:
            f = w["F"][0]
            if modal_g in (1, 2, 3) and f < RAPID_FEED:
                feeds.append(f)
    if rpm is None and tool == active_tool:
        rpm = active_rpm
    return {"tool": tool, "rpm": rpm, "feeds": feeds, "cycles": cycles}


def _circles(moves, tool_r):
    """Full circular interpolation (sum of arc sweep >= 330 deg about one centre and radius), WCS."""
    groups = {}
    for m in moves:
        if m.kind != "arc" or m.plane != 17 or m.center is None:
            continue
        cx, cy = m.center[0], m.center[1]
        r = math.hypot(m.start[0] - cx, m.start[1] - cy)
        if r < 1e-4:
            continue
        a0 = math.atan2(m.start[1] - cy, m.start[0] - cx)
        a1 = math.atan2(m.end[1] - cy, m.end[0] - cx)
        sweep = (a0 - a1) if m.cw else (a1 - a0)
        sweep = sweep % (2 * math.pi)
        if sweep < 1e-6:
            sweep = 2 * math.pi
        key = (round(cx, 3), round(cy, 3), round(r, 3))
        g = groups.setdefault(key, {"center": [cx, cy], "path_radius": r, "sweep": 0.0, "zmin": 1e9, "zmax": -1e9,
                                    "helical": False, "lines": []})
        g["sweep"] += sweep
        g["zmin"] = min(g["zmin"], m.start[2], m.end[2])
        g["zmax"] = max(g["zmax"], m.start[2], m.end[2])
        if abs(m.start[2] - m.end[2]) > 1e-4:
            g["helical"] = True
        g["lines"].append(m.line)
    out = []
    for g in groups.values():
        if g["sweep"] >= math.radians(330):
            g["cut_diameter"] = 2 * (g["path_radius"] + tool_r)
            g["turns"] = g["sweep"] / (2 * math.pi)
            g["lines"] = [min(g["lines"]), max(g["lines"])]
            del g["sweep"]
            out.append(g)
    return out


def guess_strategy(name: str, tool: dict | None, cycles) -> str:
    n = name.lower()
    k = (tool or {}).get("kind", "")
    if cycles:
        return "drill"
    if re.fullmatch(r"T\d+|tool change", name.strip()):
        return "unknown"          # no op name in the NC: engagement unknown, widest (roughing) data band
    for pat, s in ((r"\bface", "face"), (r"thread", "thread"), (r"chamfer|edge break|break", "chamfer2d"),
                   (r"helical|interp|cbore|counterbore|\bbore", "circular"), (r"adaptive|rough", "adaptive2d"),
                   (r"slot", "slot"), (r"pocket", "pocket2d"), (r"finish|profile|wall|contour|floor", "contour2d")):
        if re.search(pat, n):
            return s
    if k == "face":
        return "face"
    if k in ("chamfer", "spot"):
        return "chamfer2d"
    return "contour2d"


def reconstruct_ops(nc_files: list[Path], setups: list[dict], tools: dict[int, dict], units="inch") -> dict:
    from .gcode import parse_files
    ops, order = [], 0
    for s in setups:
        active, arpm = None, None
        for f in s["nc_paths"]:
            prog = parse_files([f], job_units=units)
            for seg in segment_nc(f):
                info = analyze_segment(f, seg, active, arpm)
                active, arpm = info["tool"], info["rpm"]
                tool = tools.get(active) if active is not None else None
                mv = [m for m in prog.moves if seg["first_line"] <= m.line <= seg["last_line"]]
                cut = [m for m in mv if m.kind in ("feed", "arc") and 0 < m.feed < RAPID_FEED and m.source == ""]
                lat = [m for m in cut if abs(m.start[2] - m.end[2]) < 1e-5 and
                       math.hypot(m.end[0] - m.start[0], m.end[1] - m.start[1]) > 1e-4]
                zlev = sorted({round(m.start[2], 4) for m in lat})
                order += 1
                op = {"id": seg["name"].split()[0] if re.match(r"OP\d+-\d+", seg["name"]) else f"{s['name']}-{order:02d}",
                      "setup": s["name"], "name": seg["name"], "order": order,
                      "tool": tool, "tool_number": active, "rpm": info["rpm"],
                      "feed_cutting": max(info["feeds"]) if info["feeds"] else None,
                      "feeds_seen": sorted(set(info["feeds"]))[:12],
                      "cycles": info["cycles"], "cycle": info["cycles"][0]["code"] if info["cycles"] else None,
                      "peck": info["cycles"][0]["Q"] if info["cycles"] else None,
                      "circles": _circles(cut, (tool or {}).get("diameter", 0) / 2),
                      "lateral_z_levels": zlev,
                      "z_min": min((min(m.start[2], m.end[2]) for m in cut), default=None),
                      "nc": {"file": seg["file"], "first_line": seg["first_line"], "last_line": seg["last_line"]}}
                op["strategy"] = guess_strategy(seg["name"], tool, info["cycles"])
                if len(zlev) > 1:
                    op["stepdown"] = float(np.median(np.diff(zlev)))
                ops.append(op)
    return {"schema": "helios.cam.operations/1", "source": "reconstructed_nc", "units": units,
            "setups": [{k: v for k, v in s.items() if k != "nc_paths"} for s in setups], "operations": ops}


def attach_nc(opsdoc: dict, nc_by_name: dict[str, Path], tools: dict[int, dict], units="inch"):
    """For a Fusion export: add cycles/circles/Z levels from the posted NC by matching op names."""
    from .gcode import parse_files
    segs = {}
    for fname, path in nc_by_name.items():
        prog = parse_files([path], job_units=units)
        active, arpm = None, None
        for seg in segment_nc(path):
            info = analyze_segment(path, seg, active, arpm)
            active, arpm = info["tool"], info["rpm"]
            segs[seg["name"].strip().lower()] = (path, seg, info, prog)
    for op in opsdoc["operations"]:
        key = (op.get("name") or "").strip().lower()
        cand = [v for k, v in segs.items() if k == key or k.endswith(key) or key.endswith(k)]
        if not cand:
            continue
        path, seg, info, prog = cand[0]
        mv = [m for m in prog.moves if seg["first_line"] <= m.line <= seg["last_line"]]
        cut = [m for m in mv if m.kind in ("feed", "arc") and 0 < m.feed < RAPID_FEED and m.source == ""]
        lat = [m for m in cut if abs(m.start[2] - m.end[2]) < 1e-5]
        op.setdefault("nc", {"file": seg["file"], "first_line": seg["first_line"], "last_line": seg["last_line"]})
        op["cycles"] = info["cycles"]
        op.setdefault("cycle", info["cycles"][0]["code"] if info["cycles"] else op.get("cycle"))
        op["circles"] = _circles(cut, (op.get("tool") or {}).get("diameter", 0) / 2)
        op["lateral_z_levels"] = sorted({round(m.start[2], 4) for m in lat})
        op["z_min"] = min((min(m.start[2], m.end[2]) for m in cut), default=None)
        if op.get("rpm") is None:
            op["rpm"] = info["rpm"]


# ------------------------------------------------------------------------------------------------ geometry mapping
def setup_frames(job_path: Path | None, nc_dir: Path, opsdoc: dict | None = None) -> list[dict]:
    """Setups with nc paths and part->WCS matrix (job units)."""
    from .job import _setup_matrix
    if opsdoc and opsdoc.get("setups"):
        out = []
        files = sorted(Path(nc_dir).glob("*.nc"))
        for i, s in enumerate(opsdoc["setups"]):
            M = np.array(s["matrix"], float) if s.get("matrix") else None
            names = s.get("nc") or ([f"{s['program']}.nc"] if s.get("program") else [])
            paths = [nc_dir / n for n in names if (nc_dir / n).exists()]
            if not paths and not names and i < len(files) and len(files) == len(opsdoc["setups"]):
                paths = [files[i]]   # one program per setup, in order
            out.append({**s, "matrix": M.tolist() if M is not None else None, "nc_paths": paths})
        return out
    if job_path:
        data = json.loads(job_path.read_text())
        f = job_length_factor(data)
        out = []
        for i, s in enumerate(data["setups"]):
            nc = s.get("nc", [])
            nc = [nc] if isinstance(nc, str) else nc
            M = _setup_matrix(s, f)
            paths = [(nc_dir / n) if (nc_dir / n).exists() else (job_path.parent / n) for n in nc]
            out.append({"name": s.get("name", f"OP{i + 1}"), "wcs": s.get("wcs"), "nc": nc, "matrix": M.tolist(),
                        "nc_paths": [p for p in paths if p.exists()], "stock_part": _scale_stock(data.get("stock"), f)})
        return out
    files = sorted(Path(nc_dir).glob("*.nc"))
    return [{"name": f"OP{i + 1}", "nc": [f.name], "matrix": None, "nc_paths": [f]} for i, f in enumerate(files)]


def job_length_factor(data: dict) -> float:
    """Factor from a job.json's length units to inches (process_lint works in inches; mm jobs are converted)."""
    u = str(data.get("units", "inch")).lower()
    return 1.0 / 25.4 if u in ("mm", "millimeter", "millimeters", "metric") else 1.0


def _scale_stock(st, f: float):
    if not isinstance(st, dict) or "min" not in st or f == 1.0:
        return st
    return {**st, "min": [v * f for v in st["min"]], "max": [v * f for v in st["max"]]}


def _inv(M):
    return np.linalg.inv(np.array(M, float))


def tool_axis_part(setup) -> np.ndarray | None:
    """Direction the tool points (tip direction) in part coordinates: -WCS Z."""
    if setup.get("tool_axis_part"):
        return np.array(setup["tool_axis_part"], float)
    if setup.get("matrix") is None:
        return None
    R = np.array(setup["matrix"], float)[:3, :3]
    return -(R.T @ np.array([0, 0, 1.0]))


def wcs_to_part(setup, p):
    M = setup.get("matrix")
    if M is None:
        return None
    return (_inv(M) @ np.array([p[0], p[1], p[2], 1.0]))[:3]


def part_to_wcs(setup, p):
    M = setup.get("matrix")
    if M is None:
        return None
    return (np.array(M, float) @ np.array([p[0], p[1], p[2], 1.0]))[:3]


def match_hole(feat, setup, x, y, tol=0.012):
    """Hole whose axis passes through WCS (x, y) along the setup's tool axis."""
    ax = tool_axis_part(setup)
    if ax is None or x is None or y is None:
        return None
    p = wcs_to_part(setup, (x, y, 0.0))
    best = None
    for h in feat["holes"]:
        a = np.array(h["axis"], float)
        if abs(abs(float(np.dot(a, ax))) - 1) > 1e-3:
            continue
        e = np.array(h["entry_point"], float)
        d = float(np.linalg.norm(np.cross(p - e, a)))
        if d < tol and (best is None or d < best[0]):
            best = (d, h)
    return best[1] if best else None


# ------------------------------------------------------------------------------------------------ spec parsing
def spec_tolerances(spec: dict | None) -> list[dict]:
    """Diameter tolerances (band) and relational tolerances from spec.json features."""
    out = []
    if not spec:
        return out
    for f in spec.get("features", []):
        tol = f.get("tolerance") or ""
        kd = f.get("key_dims", "")
        m = re.search(r"Ø\s*(\d*\.\d+)\s*\+\s*(\d*\.\d+)\s*/\s*-\s*(\d*\.\d+)", tol)
        band = None
        if m:
            band = float(m.group(2)) + float(m.group(3))
        rel = []
        for kind in ("perpendicular", "parallel", "position", "coaxial", "concentric"):
            mm = re.search(kind + r"[^;,]*?(Ø?\s*\d*\.\d+)?", tol, re.I)
            if mm and kind in tol.lower():
                datum = None
                dm = re.search(kind + r"\s+to\s+([a-z ]+?)(?:\s+\d|\s*$|\s*\.)", tol, re.I)
                if dm:
                    datum = dm.group(1).strip().lower()
                rel.append({"kind": kind, "value": mm.group(1), "datum": datum})
        if band is not None or rel or "H7" in tol or "H7" in kd:
            out.append({"feature_id": f["feature_id"], "type": f["type"], "key_dims": kd, "location": f.get("location", ""),
                        "diameter": float(m.group(1)) if m else None, "band": band,
                        "h7": "H7" in tol or "H7" in kd or (band is not None and band <= 0.0012), "relations": rel})
    return out


def material_of(spec, opsdoc, plan_path, override):
    for cand in (override, (opsdoc or {}).get("material"), (spec or {}).get("material")):
        if cand:
            try:
                return FEEDS.material_key(cand)
            except ValueError:
                pass
    if plan_path:
        txt = plan_path.read_text(errors="replace")[:4000]
        for pat in (r"6061|7075|1018|1045|4140|304|316|17-4|brass|delrin|acetal|uhmw",):
            m = re.search(pat, txt, re.I)
            if m:
                return FEEDS.material_key(m.group(0))
    return None


# ------------------------------------------------------------------------------------------------ the linter
class Lint:
    def __init__(self):
        self.findings = []

    def add(self, rule, msg, *, feature=None, op=None, severity=None, **extra):
        sev, ref = RULES[rule]
        f = {"rule": rule, "severity": severity or sev, "message": msg, "feature": feature,
             "operation": (op or {}).get("id") if isinstance(op, dict) else op,
             "op_name": (op or {}).get("name") if isinstance(op, dict) else None,
             "setup": (op or {}).get("setup") if isinstance(op, dict) else None,
             "nc": ((op or {}).get("nc") if isinstance(op, dict) else None), "wiki": ref}
        f.update(extra)
        self.findings.append(f)


def op_kind(op) -> str:
    """Feeds.py operation for an op."""
    t = (op.get("tool") or {}).get("kind", "")
    s = (op.get("strategy") or "").lower()
    n = (op.get("name") or "").lower()
    cyc = (op.get("cycle") or "").upper()
    if t == "tap" or cyc in ("G84", "G74") or "tapping" in cyc.lower():
        return "tap"
    if t == "reamer":
        return "ream"
    if t == "boring":
        return "bore"
    if t == "spot":
        return "spot"
    if t == "drill":
        return "drill"
    if t == "thread_mill" or s == "thread":
        return "thread_mill"
    if t == "chamfer" or s in ("chamfer2d", "chamfer"):
        return "chamfer"
    if t == "face" or s == "face":
        return "face"
    if op.get("cycles") and t in ("flat", "bull", "ball", "counterbore"):
        return "plunge"
    if s in ("slot",):
        return "slot"
    if s in ("adaptive2d", "adaptive") or re.search(r"adaptive|rough", n):
        return "adaptive"
    if s in ("pocket2d", "pocket_clearing", "unknown"):
        return "rough"
    if s in ("circular", "bore", "helical"):
        return "slot" if "rough" in n else "finish"   # helical roughing of a bore is full-width
    return "finish"


# ------------------------------------------------------------------------------------- in-process material
def backing_points(h: dict) -> list[np.ndarray]:
    """Part points (job inch) just behind the exit of a hole through a wall, where the material that backs the
    wall sits until the cavity behind it is machined: a ring just outside the hole (D/2 + .03) .03 in past the
    exit along the drilling direction (the hole's own drill/ream does not remove it)."""
    a = np.array(h.get("tool_axis") or h["axis"], float)
    a /= np.linalg.norm(a)
    e = np.array(h["exit_point"], float) + a * 0.03
    u = np.cross(a, [0, 0, 1.0])
    if np.linalg.norm(u) < 1e-6:
        u = np.cross(a, [1.0, 0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(a, u)
    r = 0.5 * float(h.get("diameter") or 0.0) + 0.03
    return [e + r * d for d in (u, -u, v, -v)]


class SweptMaterial:
    """Fallback in-process material test from the posted NC alone: a part point is gone before an operation when
    an earlier feed move of any tool came within the tool radius of it in XY (that setup's WCS) with the tool tip
    at or below it (within the flute length). Coarse but conservative for 'is this wall still backed'."""

    def __init__(self, setups: list[dict], tools: dict[int, dict], ops: list[dict], units: str = "inch"):
        from .gcode import parse_files
        self.ops = ops
        self.segs: dict[int, tuple] = {}      # op order -> (setup, A (n,3), B (n,3), radius (n,), flute (n,))
        sfr = {s["name"]: s for s in setups}
        moves_by_file = {}
        for s in setups:
            for f in s.get("nc_paths", []):
                moves_by_file[Path(f).name] = parse_files([f], job_units=units).moves
        for op in ops:
            nc = op.get("nc") or {}
            mv = moves_by_file.get(nc.get("file"))
            s = sfr.get(op.get("setup"))
            if not mv or s is None or s.get("matrix") is None:
                continue
            A, B, R, FL, TAN = [], [], [], [], []
            for m in mv:
                if not nc["first_line"] <= m.line <= nc["last_line"] or m.kind == "rapid":
                    continue
                if any(math.isnan(v) for v in m.start + m.end) or m.feed >= RAPID_FEED:
                    continue
                t = tools.get(m.tool) or op.get("tool") or {}
                d = float(t.get("diameter") or 0.0)
                if d <= 0:
                    continue
                A.append(m.start)
                B.append(m.end)
                R.append(d / 2)
                FL.append(float(t.get("flute_length") or 4 * d))
                # pointed tools cut only their cone near the tip: radius at height h above the tip = h x tan
                pointed = t.get("kind") in ("spot", "chamfer", "drill", "center", "countersink")
                ang = float(t.get("tip_angle") or (118.0 if t.get("kind") == "drill" else 90.0))
                TAN.append(math.tan(math.radians(ang / 2)) if pointed else math.inf)
            if A:
                self.segs[op["order"]] = (s, np.array(A), np.array(B), np.array(R), np.array(FL), np.array(TAN))

    def present_before(self, op: dict, p_part) -> bool:
        for order, (s, A, B, R, FL, TAN) in self.segs.items():
            if order >= op["order"]:
                continue
            q = part_to_wcs(s, p_part)
            ab = B[:, :2] - A[:, :2]
            L2 = (ab ** 2).sum(1)
            t = np.where(L2 > 1e-12, ((q[:2] - A[:, :2]) * ab).sum(1) / np.maximum(L2, 1e-12), 0.0).clip(0, 1)
            c = A[:, :2] + ab * t[:, None]
            dxy = np.hypot(q[0] - c[:, 0], q[1] - c[:, 1])
            zlo = np.minimum(A[:, 2], B[:, 2])
            zhi = np.maximum(A[:, 2], B[:, 2])
            with np.errstate(invalid="ignore"):
                reff = np.minimum(R, np.where(np.isinf(TAN), R, np.maximum(q[2] - zlo, 0.0) * TAN))
            if np.any((dxy <= reff + 1e-4) & (q[2] >= zlo - 1e-4) & (q[2] <= zhi + FL)):
                return False
        return True


def run_lint(job_dir, out_dir=None, material=None, write_ops=False, features=None, backed=None) -> dict:
    """``backed(op, hole, points_part_inch) -> bool | None``: is the material behind the hole's wall still there
    when ``op`` runs (helioscam verify passes one built on its exact in-process stock). Default: the NC sweep
    approximation (SweptMaterial)."""
    job_dir = Path(job_dir)
    loc = locate(job_dir)
    L = Lint()
    tools = load_tools(loc["tools"])
    spec = json.loads(loc["spec"].read_text()) if loc["spec"] else None
    opsdoc = json.loads(loc["operations"].read_text()) if loc["operations"] else None
    nc_dir = loc["nc_dir"]
    setups = setup_frames(loc["job"], nc_dir, opsdoc)
    nc_files = [p for s in setups for p in s["nc_paths"]]
    for p in nc_files:
        for n, t in nc_header_tools(p.read_text(errors="replace")).items():
            tools.setdefault(n, t)
    if opsdoc is None:
        opsdoc = reconstruct_ops(nc_files, setups, tools)
    else:
        attach_nc(opsdoc, {p.name: p for p in nc_files}, tools)
        for op in opsdoc["operations"]:
            if op.get("tool"):
                op["tool"] = norm_tool(op["tool"]) if "kind" not in op["tool"] else op["tool"]
    ops = opsdoc["operations"]
    for i, op in enumerate(ops):
        op.setdefault("order", i + 1)
        if not op.get("tool") and op.get("tool_number") in tools:
            op["tool"] = tools[op["tool_number"]]
    ops.sort(key=lambda o: o["order"])
    if features is None:
        if loc["features"]:
            features = json.loads(loc["features"].read_text())
        elif loc["step"]:
            from .features import analyze
            features = analyze(loc["step"], units="inch")
    if features and features.get("units") != "inch":
        from .features import analyze
        features = analyze(loc["step"], units="inch") if loc["step"] else None
    mat = material_of(spec, opsdoc, loc["plan"], material)
    sfr = {s["name"]: s for s in setups}
    tols = spec_tolerances(spec)

    # ---- link ops to holes
    hole_ops = defaultdict(list)   # hole id -> [(op, how, detail)]
    holes = {h["id"]: h for h in (features or {}).get("holes", [])}
    for op in ops:
        s = sfr.get(op.get("setup"))
        tool = op.get("tool") or {}
        op.setdefault("feature_refs", [])
        if not s or not features:
            continue
        for c in op.get("cycles", []):
            h = match_hole(features, s, c["x"], c["y"])
            if h is None:
                continue
            ent = part_to_wcs(s, h["entry_point"])
            depth = (ent[2] - c["Z"]) if (ent is not None and c.get("Z") is not None) else None
            hole_ops[h["id"]].append((op, "cycle", {"cycle": c, "depth_tip": depth}))
            if h["id"] not in op["feature_refs"]:
                op["feature_refs"].append(h["id"])
        for c in op.get("circles", []):
            h = match_hole(features, s, c["center"][0], c["center"][1], tol=0.02)
            if h is None:
                continue
            ent = part_to_wcs(s, h["entry_point"])
            depth = (ent[2] - c["zmin"]) if ent is not None else None
            hole_ops[h["id"]].append((op, "interp", {"circle": c, "depth_tip": depth}))
            if h["id"] not in op["feature_refs"]:
                op["feature_refs"].append(h["id"])

    # ---- in-process material: is a thin wall still backed by solid when its hole is made?
    swept = None

    def wall_backed(op, h) -> tuple[bool, str]:
        nonlocal swept
        pts = backing_points(h)
        if backed is not None:
            r = backed(op, h, pts)
            if r is not None:
                return bool(r), "in-process stock"
        if swept is None:
            swept = SweptMaterial(setups, tools, ops)
        return all(swept.present_before(op, p) for p in pts), "NC sweep"

    # ---- hole rules
    for hid, h in holes.items():
        seq = sorted(hole_ops.get(hid, []), key=lambda x: x[0]["order"])
        main_d = h["diameter"]
        cyl_ds = [s_["d"] for s_ in h["stack"] if s_["type"] == "cyl"]
        drills = [(o, d) for o, how, d in seq if how == "cycle" and (o.get("tool") or {}).get("kind") == "drill"]
        interp = [(o, d) for o, how, d in seq if how == "interp" and (o.get("tool") or {}).get("kind") in
                  ("flat", "bull", "ball", "counterbore")]
        reams = [(o, d) for o, how, d in seq if (o.get("tool") or {}).get("kind") == "reamer"]
        bores = [(o, d) for o, how, d in seq if (o.get("tool") or {}).get("kind") == "boring"]
        taps = [(o, d) for o, how, d in seq if (o.get("tool") or {}).get("kind") == "tap"]
        spots = [(o, d) for o, how, d in seq if (o.get("tool") or {}).get("kind") in ("spot", "chamfer")]
        thin_w = h.get("min_wall_pierced")
        # hole-making s11b: wall < ~.10, or < ~0.5 x D for a wall standing free (the hole exits into a cavity)
        thin = thin_w is not None and (thin_w < 0.10 + 1e-3 or
                                       (thin_w < 0.5 * main_d and h.get("exits_into") == "cavity"))
        drilled_main = [(o, d) for o, d in drills if abs(o["tool"]["diameter"] - main_d) <= 0.004
                        or (reams and o["tool"]["diameter"] < main_d)
                        or (taps and HT.threads_for_tap_drill(o["tool"]["diameter"], 0.004))]
        if not seq:
            L.add("unmapped", f"{hid} Ø{main_d:.4f} {h['kind']} from {h['entry_face']}: no operation linked "
                              f"(not machined, or WCS transform unknown)", feature=hid)
            continue
        # R1: standard hole end-milled
        cut_main = [(o, d) for o, d in interp if abs(d["circle"]["cut_diameter"] - main_d) < 0.03]
        if cut_main and not drilled_main:
            std = HT.drill_exists(main_d, 0.0015) or HT.reamer_exists(main_d)
            if std and main_d <= 0.75 and not thin:
                o = cut_main[0][0]
                L.add("hole_endmilled", f"{hid} Ø{main_d:.4f} is a standard drill size (crib drill {std:.4f}) but is "
                                        f"cut by circular interpolation with a Ø{o['tool']['diameter']:.3f} end mill; "
                                        f"spot + drill{' + ream' if HT.reamer_exists(main_d) else ''} instead",
                      feature=hid, op=o)
        # R2: thin walls (only while the wall stands free: operation order decides)
        if thin and h.get("exits_into") == "cavity" and h.get("exit_point") is not None:
            def _free(o, what):
                ok, how = wall_backed(o, h)
                if ok:
                    L.add("thin_wall_backed", f"{hid}: {what} through the {thin_w:.3f} wall while it is still backed "
                                              f"by solid stock ({how}): thin-wall rules not applied", feature=hid, op=o)
                return not ok
            drills_tw = [(o, d) for o, d in drills if _free(o, f"Ø{o['tool']['diameter']:.4f} drill")]
            reams_tw = [(o, d) for o, d in reams if _free(o, "ream")]
        else:
            drills_tw, reams_tw = drills, reams
        if thin:
            for o, d in drills_tw:
                td = o["tool"]["diameter"]
                if td > 0.375 + 1e-3:
                    L.add("thin_wall_large_drill", f"{hid}: Ø{td:.4f} drill through a {thin_w:.3f} wall "
                                                   f"(thin: < .10 or < 0.5 x D); helical-interpolate it with an end mill",
                          feature=hid, op=o)
                elif td >= 0.248:
                    L.add("thin_wall_drilled", f"{hid}: Ø{td:.4f} drill through a {thin_w:.3f} wall; prefer helical "
                                               f"interpolation, or a sharp stub drill at ~50 % feed with support behind",
                          feature=hid, op=o)
            for o, d in reams_tw:
                L.add("thin_wall_ream", f"{hid}: reaming through a {thin_w:.3f} wall; say how it is supported, or "
                                        f"finish by interpolation/boring", feature=hid, op=o)
        # R3: taps
        for o, d in taps:
            desc = o["tool"].get("description", "")
            th = HT.parse_thread(desc)
            if th is None and spec:
                for sf in spec.get("features", []):
                    if sf["type"] == "tapped_hole":
                        t2 = HT.parse_thread(sf["key_dims"])
                        if t2 and HT.tap_drill_for(t2) and abs(HT.tap_drill_for(t2)[1] - main_d) < 0.004:
                            th = t2
            if th is None:
                continue
            want = HT.tap_drill_for(th)
            form = HT.tap_drill_for(th, form_tap=True)
            prior = [(od, dd) for od, dd in drills if od["order"] < o["order"]]
            if want and prior:
                got = max(od["tool"]["diameter"] for od, _ in prior)
                ok = abs(got - want[1]) <= 0.004 or (form and abs(got - form[1]) <= 0.004)
                if not ok:
                    L.add("tap_drill_wrong", f"{hid} {th['name']}: tap drill Ø{got:.4f}, table says {want[0]} "
                                             f"(Ø{want[1]:.4f})" + (f" or Ø{form[1]:.4f} for a form tap" if form else ""),
                          feature=hid, op=o)
            if not [1 for so, _ in spots if so["order"] < o["order"]] and "countersink" not in h["tags"] \
                    and "entry_chamfer" not in h["tags"]:
                L.add("tap_no_spot", f"{hid} {th['name']}: no spot/chamfer op before tapping (thread start burr)",
                      feature=hid, op=o)
            if prior and d.get("depth_tip") is not None and h["kind"] == "blind":
                deepest = max(prior, key=lambda x: x[1].get("depth_tip") or 0)
                dd, ang = deepest[1].get("depth_tip"), deepest[0]["tool"].get("tip_angle")
                if dd is not None:
                    full = dd - deepest[0]["tool"]["diameter"] * HT.point_k(ang or 118)
                    if d["depth_tip"] > full + 0.002:
                        L.add("tap_too_deep", f"{hid}: tap to {d['depth_tip']:.3f} deep but the drilled full-diameter "
                                              f"depth is {full:.3f} (drill tip {dd:.3f})", feature=hid, op=o)
            rpm, F = o.get("rpm"), (d.get("cycle") or {}).get("F")
            if rpm and F and th.get("pitch_in"):
                if abs(F - rpm * th["pitch_in"]) > 0.01 * rpm * th["pitch_in"] + 1e-3:
                    L.add("tap_feed", f"{hid}: G84 F{F:g} at S{rpm:g} but rpm x pitch = {rpm * th['pitch_in']:.3f}",
                          feature=hid, op=o)
        # R4: tight holes and reaming
        tight = [t for t in tols if t["h7"] and t.get("diameter") and any(abs(t["diameter"] - x) < 0.003 for x in cyl_ds)]
        if tight:
            seg_d = tight[0]["diameter"]
            finished = [o for o, _ in reams if abs(o["tool"]["diameter"] - seg_d) < 0.003] + [o for o, _ in bores]
            if not finished:
                how = "interpolation with an end mill" if [1 for o, d in interp if abs(d["circle"]["cut_diameter"] - seg_d) < 0.03] \
                    else "no finishing op found"
                L.add("tight_hole_no_finish", f"{hid} Ø{seg_d:.4f} (band {tight[0]['band'] or 0:.4f}, {tight[0]['feature_id']}) "
                                              f"is finished by {how}; H7/IT7 needs a reamer or a fine boring head",
                      feature=hid, op=(interp[-1][0] if interp else None))
        for o, d in reams:
            rd = o["tool"]["diameter"]
            prior = [od["tool"]["diameter"] for od, _ in drills if od["order"] < o["order"] and od["tool"]["diameter"] < rd]
            if prior:
                allow = rd - max(prior)
                lo, hi = HT.ream_allowance(rd)
                lo, hi = min(lo, 0.008 if rd < 0.787 else 0.012), max(hi, 0.016 if rd < 0.787 else 0.016)
                if not (lo - 1e-4 <= allow <= hi + 1e-4):
                    L.add("ream_allowance", f"{hid}: ream Ø{rd:.4f} after Ø{max(prior):.4f} leaves {allow:.4f} on "
                                            f"diameter; wiki range {lo:.3f}-{hi:.3f}", feature=hid, op=o)
            code = (d.get("cycle") or {}).get("code")
            if code in ("G81", "G82", "G73", "G83", "G86"):
                L.add("ream_rapid_out", f"{hid}: reamer in {code} rapids out of the hole; use G85 (or G89)",
                      feature=hid, op=o)
        # countersink angle vs tool
        if "countersink" in h["tags"]:
            want = h["countersink"]["included_angle"]
            for o, how, d in seq:
                t = o.get("tool") or {}
                if t.get("kind") == "chamfer" and t.get("tip_angle") and abs(t["tip_angle"] - want) > 2:
                    L.add("countersink_angle", f"{hid}: {want:.0f}° countersink cut with a {t['tip_angle']:.0f}° tool "
                                               f"({t.get('description') or ''})", feature=hid, op=o)
        # R8: peck
        for o, d in drills:
            c = d.get("cycle") or {}
            td = o["tool"]["diameter"]
            depth = d.get("depth_tip")
            if depth is None and c.get("Z") is not None:
                depth = -c["Z"]
            if depth is None or td <= 0:
                continue
            full = depth - td * HT.point_k(o["tool"].get("tip_angle") or 118)
            if full > 3 * td + 1e-3 and c.get("code") in ("G81", "G82", "G85", "G86"):
                L.add("peck", f"{hid}: {full / td:.1f} x D deep with {c['code']} (no peck); use G83 (G73 in 3-5 x D "
                              f"if chips break)", feature=hid, op=o)

    # R9: drill order for intersecting holes (the drill that makes the main diameter, which reaches the crossing)
    def first_drill(hid_):
        return min((o["order"] for o, how, d in hole_ops.get(hid_, []) if how == "cycle"
                    and (o.get("tool") or {}).get("kind") == "drill"
                    and abs(o["tool"]["diameter"] - holes[hid_]["diameter"]) < 0.02), default=None)
    late = defaultdict(list)
    for hid, h in holes.items():
        for big in h.get("breaks_into", []):
            fs, fb = first_drill(hid), first_drill(big)
            if fs is not None and fb is not None and fb > fs:
                late[big].append((hid, fs))
    for big, smalls in late.items():
        ob = next(o for o in ops if o["order"] == first_drill(big))
        names = ", ".join(f"{hid} (Ø{holes[hid]['diameter']:.4f}, {next(o for o in ops if o['order'] == fs)['id']})"
                          for hid, fs in smalls)
        L.add("drill_order", f"{big} (Ø{holes[big]['diameter']:.4f}, {holes[big]['depth']:.2f} deep) is drilled in "
                             f"{ob['id']} across holes already drilled: {names}; drill {big} first, then break into it "
                             f"with the smaller holes at reduced feed (or pilot past each crossing)",
              feature=big, op=ob, related=[hid for hid, _ in smalls])

    # ---- pocket corner radius
    for p in (features or {}).get("pockets", []) + [s for s in (features or {}).get("slots", []) if s.get("floor_face") is not None]:
        rc = p.get("min_corner_radius")
        if not rc:
            continue
        n = np.array(p["normal"], float)
        finishers = []
        for op in ops:
            t = op.get("tool") or {}
            if t.get("kind") not in ("flat", "bull", "ball") or op.get("cycles"):
                continue
            s = sfr.get(op.get("setup"))
            ax = tool_axis_part(s) if s else None
            if ax is None or float(np.dot(ax, -n)) < 0.999:
                continue
            # the op reaches below the pocket top (in this setup's WCS)
            if op.get("z_min") is None:
                continue
            ztip = op["z_min"]
            top_part = None
            # pocket top level along n: floor level + depth
            lvl_top = p["floor_level"] + p["depth"]
            pt_top = None
            M = s.get("matrix")
            # WCS z of the pocket top: any point at that level along n
            base = n * lvl_top
            wz = part_to_wcs(s, base)[2]
            if ztip < wz - 0.01:
                finishers.append(op)
        if not finishers:
            continue
        fin = min(finishers, key=lambda o: (o["tool"]["diameter"], -o["order"]))
        rt = fin["tool"]["diameter"] / 2
        if rt >= rc - 1e-4:
            L.add("corner_radius", f"{p['id']} corner R{rc:.3f}: smallest tool in it is Ø{2 * rt:.4f} (R{rt:.4f} >= Rc); "
                                   f"it cannot finish the corner (rubs/chatters at Rt = Rc)", feature=p["id"], op=fin)
        elif rt > 0.8 * rc + 1e-4:
            L.add("corner_radius_margin", f"{p['id']} corner R{rc:.3f}: finisher R{rt:.4f} = {rt / rc:.2f} x Rc "
                                          f"(> 0.8); a smaller finisher cuts instead of rubbing", feature=p["id"], op=fin)

    # ---- per-op rules
    seen_tools = set()
    for op in ops:
        t = op.get("tool") or {}
        if op.get("tool_number") is not None:
            seen_tools.add(op["tool_number"])
        elif t.get("number") is not None:
            seen_tools.add(t["number"])
        k = t.get("kind")
        D = t.get("diameter") or 0
        rpm = op.get("rpm")
        F = op.get("feed_cutting")
        # preset feeds
        if rpm and abs(rpm - 5000) < 1 and F and abs(F - 39.37) < 0.1:
            L.add("preset_feeds", "rpm 5000 / 39.4 ipm: library preset left in place", op=op)
        # chamfer / spot used on profiles
        if k in ("spot", "chamfer"):
            strat = (op.get("strategy") or "").lower()
            bulk = strat in ("adaptive2d", "adaptive", "pocket2d", "pocket_clearing", "face", "slot", "parallel",
                             "scallop", "steep_and_shallow", "contour") and not re.search(r"chamfer|break|deburr|spot", (op.get("name") or "").lower())
            zl = op.get("lateral_z_levels") or []
            cone = (D / 2 - 0.0) / math.tan(math.radians((t.get("tip_angle") or 90) / 2)) if D else 0
            multi = len(zl) >= 3 and (max(zl) - min(zl)) > 0.05
            if bulk or multi:
                L.add("chamfer_tool_profiling", f"{t.get('description') or k} Ø{D:.3f} runs "
                                                f"{'a ' + strat + ' toolpath' if bulk else f'{len(zl)} lateral Z levels over {max(zl) - min(zl):.3f}'}"
                                                f": chamfer/spot tools cut chamfers and edge breaks only", op=op)
        # stick-out
        if k in ("flat", "bull", "ball") and D and t.get("stickout"):
            ld = t["stickout"] / D
            if ld > 6:
                L.add("stickout_extreme", f"T{t.get('number')} Ø{D:.3f} at {t['stickout']:.2f} stick-out = {ld:.1f} x D (> 6)",
                      op=op)
            elif ld > 4 and rpm and F:
                rec = FEEDS.calc(mat or "6061-T6", {**t, "stickout": None, "type": k}, op_kind(op),
                                 ae=op.get("optimal_load"), ap=op.get("stepdown"))
                z = t.get("flutes") or 2
                fz = F / (rpm * z)
                sfm = rpm * D * 0.262
                reduced = fz <= 0.75 * rec["fz_recommended"] or (sfm <= 0.85 * rec["sfm_recommended"] and not rec.get("rpm_capped"))
                if not reduced:
                    L.add("stickout", f"T{t.get('number')} Ø{D:.3f} at L/D {ld:.1f} runs full data (fz {fz:.4f} vs "
                                      f"{rec['fz_recommended']:.4f}, {sfm:.0f} sfm); wiki: sfm -20 %, fz -40 %, ap <= 1 D",
                          op=op)
                if op.get("stepdown") and op["stepdown"] > D + 1e-3:
                    L.add("stickout", f"T{t.get('number')}: stepdown {op['stepdown']:.3f} > 1 x D at L/D {ld:.1f}", op=op)
        # cutting data (spot/chamfer tools milling edges: effective diameter unknown, not checked)
        if mat and rpm and D and k not in (None, "slot_mill") and not (k in ("spot", "chamfer") and not op.get("cycles")):
            _check_data(L, op, t, mat, rpm, F)
        # face mill on stock edge
        if k == "face":
            _face_edge(L, op, sfr.get(op.get("setup")), t, loc)
    if len(seen_tools) > MAX_TOOLS:
        L.add("tool_count", f"{len(seen_tools)} tools > {MAX_TOOLS} pockets on the VF-2")

    # ---- setups: related tolerances split
    _setup_split(L, tols, features, hole_ops, ops, sfr)

    L.findings = consolidate(L.findings)
    fails = sum(1 for f in L.findings if f["severity"] == "fail")
    warns = sum(1 for f in L.findings if f["severity"] == "warn")
    report = {"schema": "helios.cam.process_lint/1", "job": str(job_dir), "material": mat,
              "ops_source": opsdoc.get("source"), "operations": len(ops), "tools": sorted(seen_tools),
              "inputs": {k: str(v) if v else None for k, v in loc.items()},
              "linked_holes": {hid: [o["id"] for o, _, _ in v] for hid, v in hole_ops.items()},
              "unlinked_holes": [hid for hid in holes if hid not in hole_ops],
              "summary": {"fail": fails, "warn": warns, "info": sum(1 for f in L.findings if f["severity"] == "info")},
              "status": "fail" if fails else ("warn" if warns else "pass"),
              "findings": L.findings,
              "rules": {k: {"severity": v[0], "wiki": v[1]} for k, v in RULES.items()}}
    out = Path(out_dir) if out_dir else job_dir / "process_lint"
    out.mkdir(parents=True, exist_ok=True)
    (out / "process_lint.json").write_text(json.dumps(report, indent=1, default=_js))
    (out / "process_lint.md").write_text(markdown(report))
    if write_ops:
        (out / "operations.reconstructed.json").write_text(json.dumps(opsdoc, indent=1, default=_js))
    return report


def consolidate(findings):
    """One finding per rule/operation/message pattern, listing every feature (e.g. 8 countersinks, one op)."""
    out, idx = [], {}
    for f in findings:
        pat = re.sub(r"\b(H|P|SL|ST)\d+\b", "#", f["message"])
        key = (f["rule"], f.get("operation"), pat)
        if key in idx and f.get("feature"):
            g = idx[key]
            g["features"].append(f["feature"])
            g["feature"] = ", ".join(g["features"])
            continue
        f = dict(f)
        f["features"] = [f["feature"]] if f.get("feature") else []
        idx[key] = f
        out.append(f)
    for f in out:
        if len(f["features"]) > 1:
            f["message"] = re.sub(r"^(H|P|SL|ST)\d+", f"{len(f['features'])} holes" if f["features"][0].startswith("H")
                                  else f"{len(f['features'])} features", f["message"])
    return out


def _js(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    return str(o)


def _check_data(L, op, t, mat, rpm, F):
    k = t.get("kind")
    kind = op_kind(op)
    D = t["diameter"]
    cyc = op.get("cycles") or []
    try:
        if kind in ("drill", "ream", "spot", "plunge", "bore") and cyc:
            Fc = cyc[0].get("F") or F
            if not Fc:
                return
            ipr = Fc / rpm
            eff = None
            if kind == "spot" and cyc[0].get("Z") is not None:
                eff = min(D, max(0.05, 2 * abs(cyc[0]["Z"]) * math.tan(math.radians((t.get("tip_angle") or 90) / 2))))
            rec = FEEDS.calc(mat, {**t, "type": k}, kind, sfm=rpm * (eff or D) * 0.262, ipr=ipr, effective_diameter=eff)
            rng = rec.get("range", {})
            _band(L, op, "sfm", rpm * (eff or D) * 0.262, rng.get("sfm"), rec, rpm)
            if rng.get("ipr"):
                _band(L, op, "ipr", ipr, rng["ipr"], rec, rpm)
            return
        if kind == "tap":
            rec = FEEDS.calc(mat, {**t, "type": "tap"}, "tap", sfm=rpm * D * 0.262)
            _band(L, op, "sfm", rpm * D * 0.262, rec["range"]["sfm"], rec, rpm)
            return
        if not F:
            return
        z = t.get("flutes") or 2
        eff = None
        if kind == "chamfer":
            eff = 0.5 * D
        rec = FEEDS.calc(mat, {**t, "type": k}, kind, ae=op.get("optimal_load"), ap=op.get("stepdown"),
                         sfm=rpm * (eff or D) * 0.262, fz=F / (rpm * z), effective_diameter=eff)
        _band(L, op, "sfm", rpm * (eff or D) * 0.262, rec["range"]["sfm"], rec, rpm)
        _band(L, op, "fz", F / (rpm * z), rec["range"]["fz"], rec, rpm)
        if rec.get("power_ok") is False and op.get("optimal_load") and op.get("stepdown"):
            L.add("power", f"{rec['power_hp_motor']:.1f} hp needed vs {rec['available_hp']:.1f} available at {rpm:.0f} rpm",
                  op=op)
    except (ValueError, KeyError) as e:  # material or tool not in the wiki tables
        L.add("unmapped", f"cutting data not checked: {e}", op=op)


def _band(L, op, name, val, rng, rec, rpm):
    if not rng or val is None:
        return
    lo, hi = rng
    if name == "sfm" and val < lo / 1.25 and rpm >= 0.97 * FEEDS.MAX_RPM:
        return  # spindle-capped
    if val > hi * 1.25 or val < lo / 1.25:
        t = op.get("tool") or {}
        unit = {"sfm": "sfm", "fz": "in/tooth", "ipr": "in/rev"}[name]
        L.add("cutting_data", f"T{t.get('number')} {t.get('description') or t.get('kind')}: {name} {val:.4g} {unit} is "
                              f"{'above' if val > hi else 'below'} the wiki range {lo:.4g}-{hi:.4g} by more than 25 % "
                              f"({rec.get('material')}, {rec.get('op')}; recommended "
                              f"{rec.get('sfm_recommended') if name == 'sfm' else rec.get('fz_recommended', rec.get('ipr_recommended'))})",
              op=op, value=val, range=[lo, hi])


def _face_edge(L, op, s, t, loc):
    if not s or not s.get("matrix") or not loc.get("job"):
        return
    data = json.loads(loc["job"].read_text())
    st = _scale_stock(data.get("stock"), job_length_factor(data))
    if not isinstance(st, dict) or "min" not in st:
        return
    M = np.array(s["matrix"], float)
    corners = np.array([[x, y, z] for x in (st["min"][0], st["max"][0]) for y in (st["min"][1], st["max"][1])
                        for z in (st["min"][2], st["max"][2])])
    w = corners @ M[:3, :3].T + M[:3, 3]
    lo, hi = w.min(axis=0), w.max(axis=0)
    from .gcode import parse_files
    nc = op.get("nc") or {}
    path = next((p for p in s.get("nc_paths", []) if p.name == nc.get("file")), None)
    if not path:
        return
    prog = parse_files([path])
    D = t["diameter"]
    hits = set()
    for m in prog.moves:
        if not (nc["first_line"] <= m.line <= nc["last_line"]) or m.kind != "feed" or m.feed >= RAPID_FEED:
            continue
        if abs(m.start[2] - m.end[2]) > 1e-4:
            continue
        dx, dy = abs(m.end[0] - m.start[0]), abs(m.end[1] - m.start[1])
        if dx > D and dy < 1e-4:
            yv = m.start[1]
            for edge in (lo[1], hi[1]):
                if abs(yv - edge) < 0.05 * D:
                    hits.add(("Y", round(edge, 3), m.line))
        elif dy > D and dx < 1e-4:
            xv = m.start[0]
            for edge in (lo[0], hi[0]):
                if abs(xv - edge) < 0.05 * D:
                    hits.add(("X", round(edge, 3), m.line))
    if hits:
        ax, edge, line = sorted(hits)[0]
        L.add("face_mill_edge", f"face-mill centreline runs along the stock edge {ax}={edge} (line {line}, "
                                f"{len(hits)} pass(es)): ae = 50 % D, inserts exit at the thinnest chip; shift the passes",
              op=op)


def _setup_split(L, tols, features, hole_ops, ops, sfr):
    if not tols or not features:
        return
    holes = features.get("holes", [])
    last_setup = {}
    for hid, lst in hole_ops.items():
        if lst:
            last_setup[hid] = max(lst, key=lambda x: x[0]["order"])[0].get("setup")
    faces_dir = {"bottom": np.array([0, 0, -1.0]), "top": np.array([0, 0, 1.0]), "back": np.array([0, 0, -1.0]),
                 "front": np.array([0, 0, 1.0]), "flange bottom": np.array([0, 0, -1.0])}
    for t in tols:
        if not t["relations"]:
            continue
        cand = [h for h in holes if t.get("diameter") and any(abs(s_["d"] - t["diameter"]) < 0.003
                                                                for s_ in h["stack"] if s_["type"] == "cyl")]
        if not cand:
            m = re.search(r"Ø\s*(\d*\.\d+)", t["key_dims"])
            if m:
                d = float(m.group(1))
                cand = [h for h in holes if abs(h["diameter"] - d) < 0.003]
        for rel in t["relations"]:
            for h in cand:
                s_name = last_setup.get(h["id"])
                if not s_name:
                    continue
                s = sfr.get(s_name)
                ax = tool_axis_part(s) if s else None
                if rel["kind"] in ("perpendicular", "parallel") and rel.get("datum"):
                    n = next((v for k, v in faces_dir.items() if k in rel["datum"]), None)
                    if n is None or ax is None:
                        continue
                    if abs(float(np.dot(ax, n))) < 0.999:   # datum neither on the locating face nor cut here
                        L.add("setup_split", f"{t['feature_id']} ({h['id']}) {rel['kind']} to {rel['datum']}: finished in "
                                             f"{s_name}, where the datum face is not on the locating surface",
                              feature=h["id"])
                elif rel["kind"] in ("position", "coaxial", "concentric"):
                    # compare with the largest bore (primary datum candidate) or the coaxial partner
                    ref = max(holes, key=lambda x: x["diameter"])
                    if ref["id"] == h["id"]:
                        continue
                    rs = last_setup.get(ref["id"])
                    if rs and rs != s_name:
                        L.add("setup_split", f"{t['feature_id']} ({h['id']}) {rel['kind']} tolerance: finished in {s_name}, "
                                             f"the main bore {ref['id']} Ø{ref['diameter']:.4f} in {rs}; one setup keeps "
                                             f"the relation (or re-probe the datum)", feature=h["id"])


def markdown(r: dict) -> str:
    jp = Path(r["job"])
    name = jp.parent.name if jp.name in ("out", "checks_job") else jp.name
    lines = [f"# Process lint: {name}", "",
             f"Status **{r['status'].upper()}**: {r['summary']['fail']} fail, {r['summary']['warn']} warn, "
             f"{r['summary']['info']} info. Material {r['material']}. {r['operations']} operations "
             f"({r['ops_source']}), tools {r['tools']}.", ""]
    if r["unlinked_holes"]:
        lines.append(f"Holes with no linked operation: {', '.join(r['unlinked_holes'])}.")
        lines.append("")
    lines += ["| Sev | Rule | Feature | Operation | Finding | Wiki |", "|---|---|---|---|---|---|"]
    order = {"fail": 0, "warn": 1, "info": 2}
    for f in sorted(r["findings"], key=lambda f: (order[f["severity"]], f["rule"])):
        nm = f.get("op_name") or ""
        opname = nm if f["operation"] and nm.startswith(f["operation"]) else f"{f['operation'] or ''} {nm}".strip()
        nc = f.get("nc") or {}
        if nc:
            opname += f" ({nc.get('file')}:{nc.get('first_line')})"
        lines.append(f"| {f['severity']} | {f['rule']} | {f['feature'] or ''} | {opname} | {f['message']} | {f['wiki']} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wiki process rules on a programmed job.")
    ap.add_argument("job_dir")
    ap.add_argument("--out", help="output folder (default <job_dir>/process_lint)")
    ap.add_argument("--material", help="override material (wiki key or text)")
    ap.add_argument("--write-ops", action="store_true", help="also write the (reconstructed) operations list")
    a = ap.parse_args(argv)
    r = run_lint(a.job_dir, a.out, a.material, a.write_ops)
    print(markdown(r))
    return 1 if r["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
