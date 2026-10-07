"""Hole-making tables from wiki/hole-making.md, as data (inches).

Used by checks.features (to name holes) and checks.process_lint (to judge the process).
Every table cites the wiki section it copies; change the wiki first, then this file.
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CRIB = PROJECT / "tooling" / "crib.json"
MM = 25.4

# wiki/hole-making.md sec. 7, "Tap drill table, Unified (#4 to 1/2)": thread -> (cut-tap drill name, dia, form-tap dia)
UNIFIED_TAP_DRILLS = {
    "#4-40": ("#43", 0.0890, 0.1010), "#4-48": ("#42", 0.0935, 0.1028),
    "#5-40": ("#38", 0.1015, 0.1140), "#5-44": ("#37", 0.1040, 0.1150),
    "#6-32": ("#36", 0.1065, 0.1243), "#6-40": ("#33", 0.1130, 0.1270),
    "#8-32": ("#29", 0.1360, 0.1503), "#8-36": ("#29", 0.1360, 0.1518),
    "#10-24": ("#25", 0.1495, 0.1717), "#10-32": ("#21", 0.1590, 0.1763),
    "#12-24": ("#16", 0.1770, 0.1977), "#12-28": ("#14", 0.1820, 0.2003),
    "1/4-20": ("#7", 0.2010, 0.2280), "1/4-28": ("#3", 0.2130, 0.2343),
    "5/16-18": ("F", 0.2570, 0.2879), "5/16-24": ("I", 0.2720, 0.2941),
    "3/8-16": ("5/16", 0.3125, 0.3474), "3/8-24": ("Q", 0.3320, 0.3566),
    "7/16-14": ("U", 0.3680, 0.4059), "7/16-20": ("25/64", 0.3906, 0.4154),
    "1/2-13": ("27/64", 0.4219, 0.4660), "1/2-20": ("29/64", 0.4531, 0.4779),
}
# sec. 7, metric coarse and fine: thread -> (cut-tap drill mm, form-tap drill mm)
METRIC_TAP_DRILLS_MM = {
    "M3x0.5": (2.5, 2.8), "M4x0.7": (3.3, 3.7), "M5x0.8": (4.2, 4.6), "M6x1": (5.0, 5.6),
    "M8x1.25": (6.8, 7.4), "M8x1": (7.0, 7.5), "M10x1.5": (8.5, 9.3), "M10x1.25": (8.75, 9.4),
    "M10x1": (9.0, 9.5), "M12x1.75": (10.2, 11.1), "M12x1.5": (10.5, 11.25), "M12x1.25": (10.75, 11.4),
    "M14x2": (12.0, 13.0), "M14x1.5": (12.5, 13.25), "M16x2": (14.0, 15.0), "M16x1.5": (14.5, 15.25),
}
# Pipe threads (not in the wiki table; MH standard tap drills) [judgment: MH p2185 NPT table]
PIPE_TAP_DRILLS = {"1/16-27 NPT": ("C", 0.242), "1/8-27 NPT": ("R", 0.339), "1/4-18 NPT": ("7/16", 0.4375),
                   "3/8-18 NPT": ("37/64", 0.5781), "1/2-14 NPT": ("23/32", 0.7188)}

# sec. 4 reaming allowance on diameter [MH p2171]: (reamer dia upper bound in, low, high)
REAM_ALLOWANCE_IN = [(0.125, 0.003, 0.005), (0.25, 0.004, 0.008), (0.375, 0.006, 0.010),
                     (0.5, 0.010, 0.015), (0.75, 0.015, 0.030), (1.0, 0.023, 0.030)]
# Smid gives 3 % of dia and TBM 0.2-0.4 mm; accept the union of MH and Smid [hole-making sec. 4]
REAM_ALLOWANCE_SMID_IN = [(0.125, 0.001, 0.004), (0.25, 0.004, 0.008), (0.375, 0.008, 0.011),
                          (0.5, 0.011, 0.015), (0.75, 0.015, 0.023), (1.0, 0.023, 0.030)]

# sec. 3 point length factor K (P = D x K) by point angle
POINT_K = {90: 0.5, 118: 0.300, 135: 0.207, 140: 0.180}

# sec. 9 SHCS counterbore table (inch): screw -> (close drill, normal drill, cbore dia)
SHCS_INCH = {"#4": (0.125, 0.136, 0.21875), "#6": (0.154, 0.170, 0.28125), "#8": (0.180, 0.194, 0.3125),
             "#10": (0.206, 0.221, 0.375), "1/4": (0.2656, 0.28125, 0.4375), "5/16": (0.328, 0.344, 0.53125),
             "3/8": (0.3906, 0.40625, 0.625), "1/2": (0.5156, 0.53125, 0.8125)}

UNIFIED_MAJOR = {"#0": 0.060, "#1": 0.073, "#2": 0.086, "#3": 0.099, "#4": 0.112, "#5": 0.125, "#6": 0.138,
                 "#8": 0.164, "#10": 0.190, "#12": 0.216}


def point_k(angle_deg: float | None) -> float:
    if not angle_deg:
        return 0.3
    return 0.5 / math.tan(math.radians(angle_deg / 2.0))


def ream_allowance(d_in: float) -> tuple[float, float]:
    """(low, high) stock on diameter for a reamer of d_in, union of MH and Smid [hole-making sec. 4]."""
    for (ub, lo, hi), (_, slo, shi) in zip(REAM_ALLOWANCE_IN, REAM_ALLOWANCE_SMID_IN):
        if d_in <= ub + 1e-9:
            return min(lo, slo), max(hi, shi)
    return 0.023, 0.030 + 0.01 * (d_in - 1.0)  # above 1 in: TBM 0.3-0.4 mm, grow slowly [judgment]


def parse_thread(text: str) -> dict | None:
    """'1/4-20 UNC', '#10-32 UNF', '10-32', 'M6x1', 'M8 x 1.25', '1/8-27 NPT' -> {name, major_in, tpi, pitch_in, kind}."""
    if not text:
        return None
    t = text.upper().replace(" ", "").replace("×", "X")
    m = re.search(r"(\d+/\d+)-(\d+)NPT", t)
    if m:
        name = f"{m.group(1)}-{m.group(2)} NPT"
        return {"name": name, "major_in": None, "tpi": float(m.group(2)), "pitch_in": 1 / float(m.group(2)),
                "kind": "npt", "table_key": name}
    m = re.search(r"M(\d+(?:\.\d+)?)(?:X(\d+(?:\.\d+)?))?", t)
    if m and (t.startswith("M") or "METRIC" in t):
        d = float(m.group(1))
        coarse = {3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75, 14: 2.0, 16: 2.0}
        p = float(m.group(2)) if m.group(2) else coarse.get(int(d), None)
        if p is None:
            return None
        key = f"M{d:g}x{p:g}"
        return {"name": key, "major_in": d / MM, "tpi": MM / p, "pitch_in": p / MM, "kind": "metric", "table_key": key}
    m = re.search(r"(#?\d+(?:/\d+)?)-(\d+)", t)
    if m:
        size, tpi = m.group(1), float(m.group(2))
        if "/" in size:
            a, b = size.split("/")
            major = float(a) / float(b)
        else:
            key = size if size.startswith("#") else "#" + size
            if key not in UNIFIED_MAJOR:
                return None
            size, major = key, UNIFIED_MAJOR[key]
        key = f"{size}-{int(tpi)}"
        return {"name": key, "major_in": major, "tpi": tpi, "pitch_in": 1 / tpi, "kind": "unified", "table_key": key}
    return None


def tap_drill_for(thread: dict, form_tap: bool = False) -> tuple[str, float] | None:
    """(drill name, dia in) for a thread from parse_thread [hole-making sec. 7]."""
    if not thread:
        return None
    k = thread["table_key"]
    if thread["kind"] == "unified" and k in UNIFIED_TAP_DRILLS:
        name, d, form = UNIFIED_TAP_DRILLS[k]
        return ("form", form) if form_tap else (name, d)
    if thread["kind"] == "metric" and k in METRIC_TAP_DRILLS_MM:
        cut, form = METRIC_TAP_DRILLS_MM[k]
        return (f"{form if form_tap else cut} mm", (form if form_tap else cut) / MM)
    if thread["kind"] == "npt" and k in PIPE_TAP_DRILLS:
        return PIPE_TAP_DRILLS[k]
    if thread.get("major_in"):  # formula, 75 % thread [hole-making sec. 7, MH p2171]
        return ("calc", thread["major_in"] - 1.08253 * 0.75 / thread["tpi"])
    return None


def threads_for_tap_drill(d_in: float, tol: float = 0.0015) -> list[str]:
    """Threads whose cut-tap drill (or pipe tap drill) is d_in."""
    out = [k for k, (_, d, _) in UNIFIED_TAP_DRILLS.items() if abs(d - d_in) <= tol]
    out += [k for k, (c, _) in METRIC_TAP_DRILLS_MM.items() if abs(c / MM - d_in) <= tol]
    out += [k for k, (_, d) in PIPE_TAP_DRILLS.items() if abs(d - d_in) <= tol]
    return out


@lru_cache(maxsize=1)
def crib_tools() -> list[dict]:
    try:
        rows = json.loads(CRIB.read_text())["data"]
    except Exception:
        return []
    out = []
    for t in rows:
        g = t.get("geometry", {})
        f = 1.0 if str(t.get("unit", "inches")).startswith("inch") else 1 / MM
        out.append({"type": t.get("type", ""), "description": t.get("description", ""),
                    "diameter": (g.get("DC") or 0) * f, "flutes": g.get("NOF"), "flute_length": (g.get("LCF") or 0) * f,
                    "stickout": (g.get("LB") or 0) * f, "corner_radius": (g.get("RE") or 0) * f,
                    "tip_angle": g.get("SIG"), "material": t.get("BMC", "")})
    return out


def crib_sizes(tool_type: str) -> list[float]:
    return sorted({round(t["diameter"], 4) for t in crib_tools() if t["type"] == tool_type and t["diameter"] > 0})


def drill_exists(d_in: float, tol: float = 0.0015) -> float | None:
    """Closest crib drill within tol of d_in (inch), else None."""
    best = None
    for s in crib_sizes("drill"):
        if abs(s - d_in) <= tol and (best is None or abs(s - d_in) < abs(best - d_in)):
            best = s
    return best


def reamer_exists(d_in: float, tol: float = 0.0006) -> float | None:
    for s in crib_sizes("reamer"):
        if abs(s - d_in) <= tol:
            return s
    return None


def endmill_exists(d_in: float, tol: float = 0.001) -> float | None:
    for s in crib_sizes("flat end mill"):
        if abs(s - d_in) <= tol:
            return s
    return None
