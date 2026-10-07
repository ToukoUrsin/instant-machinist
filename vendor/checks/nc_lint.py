"""nc_lint: static safety checks on posted G-code (no geometry simulation).

Rules (ids used in the report):
  safe_start, units, wcs, toolchange, tool_unknown, g43_h, spindle, rpm, feed, coolant,
  rapid_below_clearance, below_min_z, canned_cycle, arc, cutter_comp, program_end,
  unsupported, unknown_code, no_decimal, g28

Stock for the clearance / R-plane rules: the in-process stock box before each operation when the job gives one
(``stock_at(file, line)``, e.g. from helioscam verify's primary simulation: faced surfaces and earlier setups
included). Without it the raw blank box is used and those findings are downgraded (R plane -> info, rapid ->
warning) with an explicit "raw-blank assumption" note: after facing or in a later setup the raw blank is too
high and the rule would report rapids that are in air.
"""
from __future__ import annotations

import math
from collections import OrderedDict

from .gcode import Program, arc_length

SEV_ORDER = {"error": 0, "warning": 1, "info": 2}


class Findings:
    def __init__(self):
        self._d: OrderedDict = OrderedDict()

    def add(self, severity, rule, message, file="", line=0, key=None):
        k = (rule, key if key is not None else message)
        if k in self._d:
            f = self._d[k]
            f["count"] += 1
            if len(f["lines"]) < 8:
                f["lines"].append(f"{file}:{line}")
            return
        self._d[k] = {"severity": severity, "rule": rule, "message": message, "file": file,
                      "line": line, "count": 1, "lines": [f"{file}:{line}"]}

    def list(self):
        return sorted(self._d.values(), key=lambda f: (SEV_ORDER[f["severity"]], f["file"], f["line"]))


def _seg_hits_rect(p0, p1, lo, hi) -> bool:
    """Liang-Barsky: does the XY segment p0-p1 touch the rectangle [lo, hi]?"""
    t0, t1 = 0.0, 1.0
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    for p, q in ((-dx, p0[0] - lo[0]), (dx, hi[0] - p0[0]), (-dy, p0[1] - lo[1]), (dy, hi[1] - p0[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


RAW_BLANK_NOTE = ("[raw-blank assumption: stock top from the raw blank box; faced surfaces and earlier setups "
                  "unknown]")


def lint(prog: Program, *, units="inch", tools=None, machine=None, tol=None, lint_cfg=None,
         stock_box_wcs=None, min_tool_z=None, home_z=10.0, stock_at=None) -> dict:
    """Return {"findings": [...], "stats": {...}}. stock_box_wcs = (min xyz, max xyz) in the WCS (raw blank).
    ``stock_at(file, line)`` -> (lo, hi) WCS box of the in-process stock at that NC line (or None): when given,
    the clearance and R-plane rules use it instead of the raw blank."""
    from .job import LINT_DEFAULTS, MACHINE_DEFAULTS_INCH, TOLERANCE_DEFAULTS_INCH, unit_factor

    uf = unit_factor("inch", units)
    machine = machine or {k: (v * uf if k in ("max_feed", "max_rapid") else v)
                          for k, v in MACHINE_DEFAULTS_INCH.items()}
    tol = tol or {k: v * uf if isinstance(v, float) else v for k, v in TOLERANCE_DEFAULTS_INCH.items()}
    cfg = {**LINT_DEFAULTS, **(lint_cfg or {})}
    tools = tools or {}
    F = Findings()
    eps = 1e-4 * uf
    raw_box = None if stock_box_wcs is None else [[float(v) for v in b] for b in stock_box_wcs]

    def box_at(file, line):
        """(box, raw_blank_assumed) for an NC line."""
        if stock_at is not None:
            b = stock_at(file, line)
            if b is not None:
                return [[float(v) for v in b[0]], [float(v) for v in b[1]]], False
        return raw_box, raw_box is not None

    # ---------------------------------------------------------------- events
    for e in prog.events:
        k = e.kind
        if k == "unsupported":
            F.add("error", "unsupported", f"cannot verify: {e.data.get('what')}", e.file, e.line)
        elif k == "unknown":
            F.add("warning", "unknown_code", f"unknown code {e.data['code']}", e.file, e.line,
                  key=e.data["code"])
        elif k == "parse":
            F.add("warning", "parse", f"unparsed text {e.data['leftover']!r}", e.file, e.line)
        elif k == "no_decimal":
            F.add("warning", "no_decimal",
                  "axis word without decimal point (Fanuc reads X1 as 0.0001 in; Haas as 1.0)",
                  e.file, e.line, key="no_decimal")
        elif k == "arc_error":
            F.add("error", "arc", e.data["msg"], e.file, e.line)
        elif k == "arc_from_unknown":
            F.add("error", "arc", "arc from an unknown position (after G28/tool change)", e.file, e.line)
        elif k == "cycle_error":
            F.add("error", "canned_cycle", e.data["msg"], e.file, e.line)
        elif k == "toolchange_no_t":
            F.add("error", "toolchange", "M06 without a T word", e.file, e.line)
        elif k == "toolchange":
            t = e.data["tool"]
            if tools and t not in tools:
                F.add("error", "tool_unknown", f"T{t} is not in the tool list", e.file, e.line, key=t)
        elif k == "cutter_comp":
            F.add("warning", "cutter_comp",
                  f"{e.data['code']} D{e.data.get('d')}: simulation follows the programmed path, not the "
                  "compensated one", e.file, e.line, key="comp")
        elif k == "motion_without_mode":
            F.add("error", "safe_start", "axis words with no motion mode (G0/G1) set", e.file, e.line)
        elif k == "g28":
            if e.data["absolute"] and any(abs(v) > 0 for v in e.data["words"].values()):
                F.add("warning", "g28", "G28 in G90 with non-zero axis words moves through an absolute "
                      "intermediate point; use G28 G91 Z0. or G53", e.file, e.line)
        elif k == "rpm" and e.data["value"] > machine["max_rpm"]:
            F.add("error", "rpm", f"S{e.data['value']:.0f} exceeds machine max {machine['max_rpm']:.0f} rpm",
                  e.file, e.line)
        elif k == "spindle" and e.data["on"] and e.data["rpm"] <= 0:
            F.add("error", "spindle", "spindle start with S0/no S word", e.file, e.line)
        elif k == "cycle_hole":
            r, z, init, code = e.data["r"], e.data["z"], e.data["init"], e.data["code"]
            if r <= z + eps:
                F.add("error", "canned_cycle", f"G{code}: R{r:.4f} is not above Z{z:.4f}", e.file, e.line)
            if e.data["retract98"] and init < r - eps:
                F.add("error", "canned_cycle", f"G{code} G98: initial level {init:.4f} is below R{r:.4f}",
                      e.file, e.line)
            if code in (83, 73) and e.data["q"] <= 0 and not e.data.get("ijk"):
                F.add("error", "canned_cycle", f"G{code} without a positive Q peck (or Haas I/J/K)", e.file, e.line)
            box, raw = box_at(e.file, e.line)
            if box is not None:
                top = box[1][2]
                if r < top - eps:
                    if raw:
                        F.add("info", "canned_cycle",
                              f"G{code}: R{r:.4f} is below the raw blank top {top:.4f} {RAW_BLANK_NOTE}",
                              e.file, e.line, key=("r_low_raw", code))
                    else:
                        F.add("warning", "canned_cycle",
                              f"G{code}: R{r:.4f} is below the in-process stock top {top:.4f}; moves between "
                              "holes rapid at R (check rapid_through_stock)", e.file, e.line, key=("r_low", code))
        elif k == "cycle_cancel_by_motion":
            F.add("warning", "canned_cycle", f"canned cycle cancelled by G{e.data['code']} instead of G80",
                  e.file, e.line)

    # ---------------------------------------------------------------- safe start
    seen = prog.initial_state["seen_modal"]
    fm = prog.initial_state["first_motion"]
    fmf, fml = fm if fm else ("", 0)
    if fm:
        if 90 not in seen:
            F.add("error", "safe_start", "G90 not set before the first move", fmf, fml)
        if not ({20, 21} & seen):
            F.add("error", "units", "G20/G21 not set before the first move", fmf, fml)
        if not ({54, 55, 56, 57, 58, 59, 154} & seen):
            F.add("error", "wcs", "no work offset (G54-G59/G154) before the first move", fmf, fml)
        missing = [f"G{g}" for g in (17, 40, 49, 80) if g not in seen]
        if missing:
            F.add("warning", "safe_start", f"safe start line missing {' '.join(missing)}", fmf, fml)
    if len(prog.units_seen) > 1:
        F.add("error", "units", "program switches between G20 and G21")

    # ---------------------------------------------------------------- moves
    rapid_sev = cfg["rapid_below_clearance"]
    feed_len = rapid_len = feed_time = 0.0
    coolant_warned_tools = set()
    for m in prog.moves:
        loc = (m.file, m.line)
        if any(math.isnan(v) for v in m.start + m.end):
            L = abs(m.end[2] - m.start[2])
        else:
            L = arc_length(m) if m.kind == "arc" else math.dist(m.start, m.end)
        tool = tools.get(m.tool)
        r = tool.radius if tool else 0.0
        z_lo = min(m.start[2], m.end[2])
        if m.kind == "rapid":
            rapid_len += L
        else:
            feed_len += L
            if m.feed > 0:
                feed_time += L / m.feed

        # XY positioning at machine home Z (e.g. Fusion's high-feed G1 before G43) is not cutting
        at_home = z_lo >= home_z - eps
        goes_down = m.end[2] < m.start[2] - eps or (m.kind != "rapid" and not at_home)
        if m.source != "home" and goes_down:
            if m.tool == 0:
                F.add("error", "toolchange", "motion before any tool change (T# M06)", *loc, key="no_tool")
            elif m.tlc is None:
                F.add("error", "g43_h", f"T{m.tool} moves down/cuts without G43 tool length compensation",
                      *loc, key=("nog43", m.tool))
            elif m.tlc != m.tool:
                F.add("error", "g43_h", f"G43 H{m.tlc} does not match T{m.tool}", *loc, key=("h", m.tool, m.tlc))

        if m.kind != "rapid" and not at_home:
            if not m.spindle_on:
                F.add("error", "spindle", f"feed move with spindle off (T{m.tool})", *loc, key=("off", m.tool))
            elif m.rpm <= 0:
                F.add("error", "spindle", "feed move with S0", *loc, key=("s0", m.tool))
            if m.feed <= 0:
                F.add("error", "feed", "feed move with no F word set", *loc, key="nof")
            elif m.feed > machine["max_feed"] + 1e-6:
                F.add("error", "feed", f"F{m.feed:.1f} exceeds machine max cutting feed "
                      f"{machine['max_feed']:.0f}/min", *loc, key=("fmax", round(m.feed, 1)))
            if not m.coolant and m.tool not in coolant_warned_tools and cfg["coolant"] != "off":
                coolant_warned_tools.add(m.tool)
                F.add(cfg["coolant"], "coolant", f"T{m.tool} cuts with coolant off (M08/M88 missing)", *loc)
        if min_tool_z is not None and z_lo < min_tool_z - eps and m.source != "home":
            F.add(cfg["below_min_z"], "below_min_z", f"tool tip goes to Z{z_lo:.4f}, below the setup "
                  f"min_tool_z {min_tool_z:.4f} (parallels/vise)", *loc, key=("minz", m.tool))

        box, raw = box_at(m.file, m.line) if m.kind == "rapid" else (None, False)
        if box is not None and m.source not in ("home", "unknown_xy"):
            lo, hi = box
            clearance = hi[2] + tol["rapid_clearance"]
            s, e = m.start, m.end
            vertical = abs(s[0] - e[0]) < eps and abs(s[1] - e[1]) < eps
            pure_up = vertical and e[2] >= s[2] - eps
            peck = vertical and m.source == "cycle"  # G83 re-entry into its own hole
            if not pure_up and not peck and z_lo < clearance - eps:
                # part of the move that is below the clearance plane
                if abs(e[2] - s[2]) < eps:
                    t0, t1 = 0.0, 1.0
                else:
                    tc = (clearance - s[2]) / (e[2] - s[2])
                    t0, t1 = (max(tc, 0.0), 1.0) if e[2] < s[2] else (0.0, min(tc, 1.0))
                p0 = [s[i] + (e[i] - s[i]) * t0 for i in range(2)]
                p1 = [s[i] + (e[i] - s[i]) * t1 for i in range(2)]
                if _seg_hits_rect(p0, p1, (lo[0] - r, lo[1] - r), (hi[0] + r, hi[1] + r)):
                    if raw:
                        F.add("warning" if rapid_sev == "error" else rapid_sev, "rapid_below_clearance",
                              f"G0 below the clearance plane Z{clearance:.4f} over the raw blank "
                              f"(to Z{e[2]:.4f}, not a pure retract) {RAW_BLANK_NOTE}", *loc, key=("rapid", m.tool))
                    else:
                        F.add(rapid_sev, "rapid_below_clearance",
                              f"G0 below the clearance plane Z{clearance:.4f} over the in-process stock "
                              f"(to Z{e[2]:.4f}, not a pure retract)", *loc, key=("rapid", m.tool))

    # ---------------------------------------------------------------- program end
    ends = [e for e in prog.events if e.kind == "end"]
    if not ends:
        last = prog.moves[-1] if prog.moves else None
        F.add("error", "program_end", "no program end (M30/M02)", last.file if last else "",
              last.line if last else 0)
    else:
        e = ends[-1]
        if e.data["spindle_on"]:
            F.add("info", "program_end", "spindle not stopped (M05) before program end", e.file, e.line)
        if e.data["coolant"]:
            F.add("info", "program_end", "coolant not turned off (M09) before program end", e.file, e.line)
        if prog.moves and prog.moves[-1].end[2] < home_z - eps:
            F.add("warning", "program_end", "no Z retract to home (G28 G91 Z0. / G53 Z0.) before end",
                  e.file, e.line)
    return {"findings": F.list(), "stats": {
        "moves": len(prog.moves),
        "feed_length": round(feed_len, 3),
        "rapid_length": round(rapid_len, 3),
        "cycle_time_min_est": round(feed_time + rapid_len / machine["max_rapid"], 2),
        "tools_used": sorted({m.tool for m in prog.moves if m.tool}),
    }}
