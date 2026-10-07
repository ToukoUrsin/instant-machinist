"""Job folder loading: job.json, tools json, units and per-setup transforms.

All geometry inside the checks is expressed in the job's units ("inch" or "mm").
See checks/README.md for the job.json format.
"""
from __future__ import annotations

import json
import re
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MM_PER_INCH = 25.4


def unit_factor(src: str, dst: str) -> float:
    """Multiply a length in `src` units by this to get `dst` units."""
    src, dst = _norm_unit(src), _norm_unit(dst)
    if src == dst:
        return 1.0
    return 1.0 / MM_PER_INCH if src == "mm" else MM_PER_INCH


def _norm_unit(u: str) -> str:
    u = u.lower().strip()
    if u in ("in", "inch", "inches"):
        return "inch"
    if u in ("mm", "millimeter", "millimeters", "metric"):
        return "mm"
    raise ValueError(f"unknown unit {u!r}")


# Haas VF-2 defaults, in inches (converted when the job is metric).
MACHINE_DEFAULTS_INCH = {
    "name": "Haas VF-2",
    "max_feed": 650.0,       # cutting feed, per min
    "max_rapid": 1000.0,     # rapid traverse, per min (used for cycle time)
    "max_rpm": 8100.0,
    "home_z_above_stock": 5.0,  # where G28/G53 Z0 is assumed to put the tool tip
}

TOLERANCE_DEFAULTS_INCH = {
    "gouge": 0.002,            # removed below the part surface by more than this = gouge
    "leftover": 0.005,         # material above the part surface by more than this = leftover
    "leftover_area_pct_max": 0.5,  # fail if leftover covers more than this % of the part area ...
    "leftover_max": 0.02,          # ... or any leftover region is taller than this
    "leftover_min_region_cells": 9,  # ... and at least this many grid cells
    "rapid_contact": 0.0005,   # a rapid removing more than this = rapid through stock
    "holder_clearance": 0.0,   # holder must stay this far above material
    "rapid_clearance": 0.04,   # lint: clearance plane = stock top (in WCS) + this
}

SIM_DEFAULTS_INCH = {
    "resolution": 0.01,
    "margin": 0.75,            # grid margin around the stock footprint
    "edge_band_cells": 1,      # gouge/leftover ignore steps narrower than this (vertical walls)
    "arc_tolerance": 0.001,    # chord error when linearizing arcs
    "mesh_tolerance": 0.0005,  # STEP tessellation linear deflection
    "search_tolerance": 0.00002,  # golden-section stop for ball/bull/cone moves with Z change
}

LINT_DEFAULTS = {
    "rapid_below_clearance": "warning",  # Fusion keeps the tool down with G0 in cleared areas; stock_sim still errors on a rapid into material
    "coolant": "warning",
    "below_min_z": "warning",
}


HOLDER_CYLINDERS = 8  # holder/shank profile is simplified to at most this many stacked cylinders


@dataclass
class Tool:
    number: int
    type: str = "flat"          # flat | ball | bull | drill | spot | chamfer | tap | thread_mill | reamer |
                                # boring | face | counterbore | slot | lollipop | dovetail
    diameter: float = 0.5
    corner_radius: float = 0.0
    flute_length: float = 1.0
    stickout: float = 2.0       # tip to holder face (Fusion LB)
    holder_diameter: float = 1.75
    holder_length: float = 2.0
    holder_segments: list | None = None  # [(height, lower_d, upper_d)] bottom-up from the holder face
    shank_diameter: float = 0.0          # 0 = same as diameter
    shoulder_length: float = 0.0         # where the shank starts above the tip (0 = flute_length)
    point_angle: float = 118.0  # drills / spot / chamfer / countersink (included angle)
    tip_diameter: float = 0.0   # chamfer mills / countersinks: small diameter at the tip
    name: str = ""
    pitch: float = 0.0          # taps / thread mills (job units); 0 = from the name ('20 TPI', 'M8x1.25')
    units: str = "inch"         # job units of the lengths above (set by load_tools)

    @property
    def radius(self) -> float:
        return self.diameter / 2.0

    @property
    def kind(self) -> str:
        return self.type.lower().replace(" ", "_").replace("-", "_")

    @property
    def sim_mode(self) -> str:
        """normal: remove material with the full profile; thread: taps/thread mills remove with 0.75 x radius
        (below any tap drill) and any removal is reported; skip: undercut tools a Z-map cannot represent."""
        k = self.kind
        if k in ("tap", "thread_mill", "tap_right_hand", "tap_left_hand"):
            return "thread"
        if k in ("slot", "slot_mill", "lollipop", "lollipop_mill", "dovetail", "dovetail_mill", "keyseat"):
            return "skip"
        return "normal"

    def thread_pitch(self, units: str | None = None) -> float | None:
        """Thread pitch in the tool's length units: the pitch field, else parsed from the name."""
        units = units or self.units
        if self.pitch and self.pitch > 0:
            return self.pitch
        t = (self.name or "").upper()
        m = re.search(r"(\d+(?:\.\d+)?)\s*TPI", t) or re.search(r"\d[\d/#.]*-(\d+(?:\.\d+)?)(?:\b|\s|UN|NPT)", t)
        if m and 4 <= float(m.group(1)) <= 100:
            return (1.0 if units == "inch" else 25.4) / float(m.group(1))
        m = re.search(r"M\d+(?:\.\d+)?\s*[X×]\s*(\d+(?:\.\d+)?)", t)
        if m and 0.2 <= float(m.group(1)) <= 6:
            return float(m.group(1)) / (25.4 if units == "inch" else 1.0)
        return None

    @property
    def sim_radius(self) -> float:
        """Radius the simulations sweep. Taps: 0.75 x radius (inside the tap drill; any removal = no pre-hole).
        Thread mills are NOT taps: they orbit the bore, so their core is 0.75 x the tooth-root radius
        (radius - 5H/8 of a 60° thread, H = 0.866 P; pitch from the tool, else root = 0.7 x radius)."""
        mode = self.sim_mode
        if mode == "thread" and self.kind == "thread_mill":
            r = self.radius
            p = self.thread_pitch()
            h = min(0.541266 * p, 0.45 * r) if p else 0.3 * r
            return 0.75 * (r - h)
        return {"normal": self.radius, "thread": 0.75 * self.radius, "skip": 0.0}[mode]

    @property
    def sim_flute_length(self) -> float:
        """Height above which removal counts as 'beyond the flute length'. Single-form thread mills have a
        short tooth on a neck narrower than their core path: only the core matters, so the whole reach."""
        if self.kind == "thread_mill":
            return max(self.stickout, self.flute_length)
        return self.flute_length

    def profile(self, drill_model: str = "cone"):
        """(kind, a, b) for the simulation kernel.
        kind 0 flat, 1 ball, 2 bull (a = corner radius), 3 cone (a = 1/tan(half angle), b = tip radius)."""
        t = self.kind
        r = self.radius
        if self.sim_mode != "normal":
            return 0, 0.0, 0.0
        if t in ("ball", "ball_end_mill"):
            return 1, r, 0.0
        if t in ("bull", "bullnose", "bull_nose", "torus", "bull_nose_end_mill") or (
                t in ("flat", "flat_end_mill", "face", "face_mill") and self.corner_radius > 0):
            rc = min(max(self.corner_radius, 0.0), r)
            if rc <= 0:
                return 0, 0.0, 0.0
            if rc >= r - 1e-9:
                return 1, r, 0.0
            return 2, rc, 0.0
        if t in ("drill", "spot", "spot_drill", "center", "center_drill", "chamfer", "chamfer_mill",
                 "countersink", "counter_sink") and not (t == "drill" and drill_model == "flat"):
            half = math.radians(max(min(self.point_angle, 179.0), 1.0) / 2.0)
            return 3, 1.0 / math.tan(half), min(self.tip_diameter / 2.0, r)
        return 0, 0.0, 0.0

    def assembly(self):
        """[(offset above tip, radius, label)] cylinders that must stay out of the material."""
        out = []
        if self.shank_diameter > self.diameter + 1e-9:
            out.append((self.shoulder_length or self.flute_length, self.shank_diameter / 2.0, "shank"))
        bands = []
        if self.holder_segments:
            z = self.stickout
            for h, dl, du in self.holder_segments:
                bands.append([z, z + h, max(dl, du) / 2.0])
                z += h
        elif self.holder_diameter > 0:
            bands.append([self.stickout, self.stickout + self.holder_length, self.holder_diameter / 2.0])
        # merge equal neighbours, then the shortest neighbours (conservative: keep the max radius)
        merged = []
        for b in bands:
            if merged and abs(merged[-1][2] - b[2]) < 1e-9:
                merged[-1][1] = b[1]
            else:
                merged.append(list(b))
        limit = HOLDER_CYLINDERS - len(out)
        while len(merged) > limit:
            i = min(range(len(merged) - 1), key=lambda k: merged[k + 1][1] - merged[k][0])
            a, b = merged[i], merged.pop(i + 1)
            a[1], a[2] = b[1], max(a[2], b[2])
        out += [(b[0], b[2], "holder") for b in merged]
        return out


FUSION_TYPES = {
    "flat end mill": "flat", "ball end mill": "ball", "bull nose end mill": "bull", "face mill": "face",
    "drill": "drill", "spot drill": "spot", "center drill": "center", "counter sink": "countersink",
    "chamfer mill": "chamfer", "tap right hand": "tap", "tap left hand": "tap", "thread mill": "thread_mill",
    "reamer": "reamer", "counter bore": "counterbore", "boring bar": "boring", "slot mill": "slot",
    "lollipop mill": "lollipop", "dovetail mill": "dovetail", "radius mill": "flat", "tapered mill": "chamfer",
    "engrave": "chamfer",
}


def _from_fusion(t: dict) -> tuple[dict, str]:
    """Raw Fusion tool JSON (geometry / holder / post-process) or camkit.tools_used() rows -> Tool kwargs."""
    if "geometry" in t:
        g, h, pp = t["geometry"], t.get("holder") or {}, t.get("post-process") or {}
        unit = "mm" if str(t.get("unit", "inches")).startswith("milli") else "inch"
        hunit = "mm" if str(h.get("unit", t.get("unit", "inches"))).startswith("milli") else "inch"
        hf = unit_factor(hunit, unit)
        segs = [(s["height"] * hf, s["lower-diameter"] * hf, s["upper-diameter"] * hf) for s in h.get("segments", [])]
        typ = t.get("type", "flat end mill")
        num = pp.get("number", t.get("number"))
        kw = {"diameter": g.get("DC"), "corner_radius": g.get("RE"), "flute_length": g.get("LCF"),
              "stickout": g.get("LB"), "shank_diameter": g.get("SFDM"), "shoulder_length": g.get("shoulder-length"),
              "tip_diameter": g.get("tip-diameter"), "holder_segments": segs or None, "name": t.get("description", "")}
        angle_sig, angle_ta = g.get("SIG"), g.get("TA")
    else:  # camkit.tools_used(): *_in keys, inches
        unit = "inch"
        typ = t.get("type", "flat end mill")
        num = t.get("number")
        segs = [(s["height"], s["lower-diameter"], s["upper-diameter"]) for s in (t.get("holder_segments") or [])]
        kw = {"diameter": t.get("diameter_in"), "corner_radius": t.get("corner_radius_in"),
              "flute_length": t.get("flute_length_in"), "stickout": t.get("stickout_in"),
              "shank_diameter": t.get("shank_diameter_in"), "tip_diameter": t.get("tip_diameter_in"),
              "holder_segments": segs or None, "name": t.get("description") or ""}
        angle_sig, angle_ta = t.get("tip_angle_deg"), None
    kind = FUSION_TYPES.get(str(typ).lower(), str(typ).lower())
    if kind in ("chamfer",) and angle_ta:
        kw["point_angle"] = 2 * angle_ta          # Fusion TA is the half (taper) angle
    elif angle_sig:
        kw["point_angle"] = angle_sig
    if kind == "chamfer" and "point_angle" not in kw:
        kw["point_angle"] = 90.0
    kw = {k: v for k, v in kw.items() if v is not None}
    kw["type"] = kind
    kw["number"] = num
    if not kw.get("holder_segments"):
        kw["holder_diameter"] = 0.0  # no holder in Fusion: holder collisions cannot be checked
    return kw, unit


LENGTH_KEYS = ("diameter", "corner_radius", "flute_length", "stickout", "holder_diameter", "holder_length",
               "tip_diameter", "shank_diameter", "shoulder_length")


def load_tools(path: Path, job_units: str) -> dict[int, Tool]:
    """Native tools.json ({"units", "tools": [...]}), a list from camkit.tools_used(), or raw Fusion tool
    JSON (a list, or a library export with "data")."""
    data = json.loads(Path(path).read_text())
    rows = data.get("tools", data.get("data")) if isinstance(data, dict) else data
    file_units = data.get("units", job_units) if isinstance(data, dict) else job_units
    tools = {}
    for t in rows:
        if "geometry" in t or any(k.endswith("_in") for k in t):
            kw, u = _from_fusion(t)
        else:
            kw, u = dict(t), file_units
        f = unit_factor(u, job_units)
        if kw.get("number") is None:
            continue
        num = int(kw.pop("number"))
        for k in LENGTH_KEYS:
            if kw.get(k) is not None:
                kw[k] = float(kw[k]) * f
        if kw.get("holder_segments"):
            segs = []
            for seg in kw["holder_segments"]:
                if isinstance(seg, dict):
                    d = seg.get("diameter")
                    h, dl, du = seg["height"], seg.get("lower-diameter", d), seg.get("upper-diameter", d)
                else:
                    h, dl, du = seg
                segs.append((float(h) * f, float(dl) * f, float(du) * f))
            kw["holder_segments"] = segs
        if not kw.get("name") and kw.get("description"):
            kw["name"] = str(kw["description"])
        if kw.get("pitch") is not None:
            kw["pitch"] = float(kw["pitch"]) * f
        kw["units"] = job_units
        known = {k: v for k, v in kw.items() if k in Tool.__dataclass_fields__}
        tools[num] = Tool(number=num, **known)
    return tools


def rotation_matrix(rot_deg) -> np.ndarray:
    """Extrinsic rotations about X, then Y, then Z (degrees)."""
    rx, ry, rz = (math.radians(a) for a in rot_deg)
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    M = Rz @ Ry @ Rx
    M[np.abs(M) < 1e-12] = 0.0
    return M


@dataclass
class Setup:
    name: str
    nc_files: list[Path]
    matrix: np.ndarray            # 4x4, part/model coords (job units) -> this setup's WCS
    wcs: str = "G54"
    expect_finish: bool | None = None  # leftover in this setup's view fails (default: single-setup jobs)
    leftover_floor_z: float | None = None  # WCS Z below which material outside the part may stay
    min_tool_z: float | None = None        # WCS Z the tool tip must not go below (parallels, vise)
    initial_stock: str | None = None       # "part": start this setup from the finished part (side setups)
    # In-process stock from an exact upstream simulation (helioscam verify writes these):
    inprocess_stock: Path | None = None    # .npz {vertices (part coords, job units), faces}: stock at setup start
    stock_boxes: list | None = None        # [[file, line, [lo xyz], [hi xyz]], ...] WCS (job units): the in-process
    #                                        stock box before the op that starts at file:line (faced surfaces, ...)

    def stock_box_at(self, file: str, line: int):
        """In-process stock box (lo, hi) in WCS before NC file:line, or None when not known."""
        best = None
        for f, ln, lo, hi in self.stock_boxes or []:
            if f == file and ln <= line and (best is None or ln >= best[0]):
                best = (ln, lo, hi)
        if best is None:
            return None
        return np.asarray(best[1], float), np.asarray(best[2], float)

    def to_wcs(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, dtype=float)
        return pts @ self.matrix[:3, :3].T + self.matrix[:3, 3]


@dataclass
class Job:
    root: Path
    units: str
    part_path: Path | None
    part_units: str
    stock_min: np.ndarray         # part coords
    stock_max: np.ndarray
    tools: dict[int, Tool]
    setups: list[Setup]
    machine: dict = field(default_factory=dict)
    tol: dict = field(default_factory=dict)
    sim: dict = field(default_factory=dict)
    lint: dict = field(default_factory=dict)
    drill_model: str = "cone"

    def stock_corners(self) -> np.ndarray:
        lo, hi = self.stock_min, self.stock_max
        return np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])

    def stock_box_wcs(self, setup: Setup):
        """Axis-aligned stock box in the setup WCS (exact for 90-degree rotations)."""
        c = setup.to_wcs(self.stock_corners())
        return c.min(axis=0), c.max(axis=0)


def _scaled(defaults_inch: dict, units: str, overrides: dict | None, length_keys) -> dict:
    f = unit_factor("inch", units)
    out = {k: (v * f if k in length_keys else v) for k, v in defaults_inch.items()}
    out.update(overrides or {})
    return out


def _setup_matrix(s: dict, units_factor: float) -> np.ndarray:
    M = np.eye(4)
    if "matrix" in s:
        M = np.array(s["matrix"], dtype=float).reshape(4, 4)
        M[:3, 3] *= units_factor
        return M
    M[:3, :3] = rotation_matrix(s.get("rotation_deg", [0, 0, 0]))
    M[:3, 3] = np.array(s.get("translation", [0, 0, 0]), dtype=float) * units_factor
    return M


def load_job(root: Path | str) -> Job:
    root = Path(root)
    data = json.loads((root / "job.json").read_text())
    units = _norm_unit(data.get("units", "inch"))
    lf = 1.0  # lengths in job.json are in job units

    part = data.get("part")
    part_path = (root / part) if part else None
    part_units = data.get("part_units")
    if part_units is None:
        # OCCT converts STEP files to millimetres on import.
        part_units = "mm" if part_path and part_path.suffix.lower() in (".step", ".stp") else units
    tools = load_tools(root / data.get("tools", "tools.json"), units) if (root / data.get("tools", "tools.json")).exists() else {}

    st = data["stock"]
    if isinstance(st, str):  # path to a json file (e.g. camkit.stock_box() output)
        st = json.loads((root / st).read_text())
    if "stockXLow" in st:  # camkit.stock_box(): inches
        f = unit_factor("inch", units)
        st = {"min": [st["stockXLow"] * f, st["stockYLow"] * f, st["stockZLow"] * f],
              "max": [st["stockXHigh"] * f, st["stockYHigh"] * f, st["stockZHigh"] * f]}
    if "min" in st:
        smin, smax = np.array(st["min"], float), np.array(st["max"], float)
    else:  # size + origin (min corner) in part coords
        smin = np.array(st.get("origin", [0, 0, 0]), float)
        smax = smin + np.array(st["size"], float)

    setups = []
    for i, s in enumerate(data["setups"]):
        nc = s.get("nc", [])
        nc = [nc] if isinstance(nc, str) else nc
        setups.append(Setup(
            name=s.get("name", f"setup{i + 1}"),
            nc_files=[root / p for p in nc],
            matrix=_setup_matrix(s, lf),
            wcs=s.get("wcs", "G54"),
            expect_finish=s.get("expect_finish"),
            leftover_floor_z=s.get("leftover_floor_z"),
            min_tool_z=s.get("min_tool_z"),
            initial_stock=s.get("initial_stock"),
            inprocess_stock=(root / s["inprocess_stock"]) if s.get("inprocess_stock") else None,
            stock_boxes=s.get("stock_boxes"),
        ))

    length_tol = {"gouge", "leftover", "leftover_max", "rapid_contact", "holder_clearance", "rapid_clearance"}
    length_sim = {"resolution", "margin", "arc_tolerance", "mesh_tolerance", "search_tolerance"}
    machine = _scaled(MACHINE_DEFAULTS_INCH, units, data.get("machine"),
                      {"max_feed", "max_rapid", "home_z_above_stock"})
    return Job(
        root=root, units=units, part_path=part_path, part_units=_norm_unit(part_units),
        stock_min=smin, stock_max=smax, tools=tools, setups=setups, machine=machine,
        tol=_scaled(TOLERANCE_DEFAULTS_INCH, units, data.get("tolerances"), length_tol),
        sim=_scaled(SIM_DEFAULTS_INCH, units, data.get("sim"), length_sim),
        lint={**LINT_DEFAULTS, **(data.get("lint") or {})},
        drill_model=data.get("drill_model", "cone"),
    )
