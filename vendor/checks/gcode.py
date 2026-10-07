"""Fanuc/Haas-style G-code interpreter for 3-axis milling.

Produces a flat list of Move records (tool tip positions in the program's WCS, converted to the
job's units) plus Events (tool changes, M codes, unsupported words) that the lint rules and the
stock simulation consume. Arcs (G2/G3 with IJK or R, planes G17/G18/G19, helical) are kept as arcs
and can be linearized with `linearize`. Canned cycles (G73 G76 G81 G82 G83 G84 G85 G86 G89; G73/G83 with Q or
Haas I/J/K decreasing pecks) are
expanded into their rapid/feed moves.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .job import unit_factor

WORD_RE = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
CANNED = {73, 74, 76, 81, 82, 83, 84, 85, 86, 87, 88, 89}
KNOWN_G = {0, 1, 2, 3, 4, 9, 10, 17, 18, 19, 20, 21, 28, 40, 41, 42, 43, 44, 49, 53, 54, 55, 56, 57, 58, 59,
           12, 13, 47, 51, 61, 64, 65, 66, 68, 69, 70, 71, 72, 80, 90, 91, 93, 94, 95, 98, 99, 100, 103,
           150, 154, 187} | CANNED
KNOWN_M = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 19, 30, 31, 33, 34, 35, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59,
           61, 62, 63, 64, 65, 66, 67, 68, 88, 89, 154, 155}


@dataclass
class Event:
    kind: str       # toolchange, spindle, coolant, end, unsupported, unknown, parse, g28, units, ...
    line: int
    file: str
    data: dict = field(default_factory=dict)


@dataclass
class Move:
    kind: str                   # "rapid" | "feed" | "arc"
    start: tuple
    end: tuple
    line: int
    file: str
    tool: int
    feed: float                 # job units / min (0 if unset)
    rpm: float
    spindle_on: bool
    coolant: bool
    tlc: int | None             # active G43/G44 H number, None if G49
    comp: int = 40              # 40/41/42
    source: str = ""            # "", "cycle", "home"
    cycle: int | None = None
    # arcs
    center: tuple | None = None
    cw: bool = False
    plane: int = 17
    text: str = ""


@dataclass
class Program:
    moves: list[Move]
    events: list[Event]
    units_seen: set
    initial_state: dict


def _strip(line: str) -> str:
    line = re.sub(r"\([^)]*\)", " ", line)
    line = line.split(";", 1)[0]
    return line.strip().upper()


class Interpreter:
    def __init__(self, job_units: str = "inch", home_z: float = 10.0):
        self.job_units = job_units
        self.home_z = home_z
        self.moves: list[Move] = []
        self.events: list[Event] = []
        self.units_seen: set = set()
        # modal state
        self.motion = None
        self.plane = 17
        self.absolute = True
        self.prog_units = None       # "inch" / "mm"
        self.wcs = None
        self.tlc = None
        self.comp = 40
        self.retract98 = True
        self.feed = 0.0              # job units / min
        self.rpm = 0.0
        self.spindle_on = False
        self.coolant = False
        self.tool = 0
        self.pending_tool = None
        self.pos = [math.nan, math.nan, home_z]   # unknown XY at start, Z at home
        self.cycle_initial_z = None
        self.cycle_r = None
        self.cycle_z = None
        self.cycle_q = 0.0
        self.cycle_ijk = None        # Haas G73/G83 decreasing pecks: (first I, reduction J, minimum K), job units
        self.cycle_active = False
        self.file = ""
        self.lineno = 0
        self.text = ""
        self.seen_modal = set()      # G codes seen before first motion (for safe start check)
        self.first_motion_line = None

    # ------------------------------------------------------------------ helpers
    def ev(self, kind, **data):
        self.events.append(Event(kind, self.lineno, self.file, data))

    def f(self) -> float:
        return unit_factor(self.prog_units or self.job_units, self.job_units)

    def emit(self, kind, end, source="", cycle=None, center=None, cw=False):
        start = tuple(self.pos)
        # unknown XY (after G28 X/Y) -> treat the move as starting straight above/below its end XY
        if math.isnan(start[0]) or math.isnan(start[1]):
            start = (end[0], end[1], start[2])
        if (math.isnan(end[0]) or math.isnan(end[1])) and source != "home":
            source = "unknown_xy"  # e.g. G43 H1 Z1. right after a tool change: Z known, XY not
        if self.first_motion_line is None and source != "home":
            self.first_motion_line = (self.file, self.lineno)
        self.moves.append(Move(
            kind=kind, start=start, end=tuple(end), line=self.lineno, file=self.file, tool=self.tool,
            feed=self.feed, rpm=self.rpm, spindle_on=self.spindle_on, coolant=self.coolant, tlc=self.tlc,
            comp=self.comp, source=source, cycle=cycle, center=center, cw=cw, plane=self.plane,
            text=self.text))
        self.pos = list(end)

    # ------------------------------------------------------------------ main
    def run_file(self, path: Path | str):
        path = Path(path)
        self.file = path.name
        for i, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
            self.lineno = i
            self.text = raw.strip()
            self.block(raw)
        return self

    def run_text(self, text: str, name: str = "<text>"):
        self.file = name
        for i, raw in enumerate(text.splitlines(), 1):
            self.lineno = i
            self.text = raw.strip()
            self.block(raw)
        return self

    def program(self) -> Program:
        return Program(self.moves, self.events, self.units_seen,
                       {"seen_modal": self.seen_modal, "first_motion": self.first_motion_line})

    def block(self, raw: str):
        s = _strip(raw)
        if not s or s == "%":
            return
        if s.startswith("/"):
            self.ev("block_delete")
            s = s[1:]
        if s.startswith("O") and re.fullmatch(r"O\s*\d+.*", s) and "G" not in s:
            return
        if "#" in s or "[" in s:
            self.ev("unsupported", what="macro variables/expressions", text=s)
            return
        words = WORD_RE.findall(s)
        leftover = WORD_RE.sub("", s).replace(" ", "")
        if leftover:
            self.ev("parse", text=s, leftover=leftover)
        gs, ms = [], []
        w: dict[str, float] = {}
        wraw: dict[str, str] = {}
        for letter, num in words:
            if letter == "G":
                gs.append(round(float(num), 1))
            elif letter == "M":
                ms.append(int(float(num)))
            elif letter == "N":
                continue
            else:
                w[letter] = float(num)
                wraw[letter] = num

        # integer axis words without a decimal point are ambiguous on Fanuc (1 = 0.0001 in)
        for a in ("X", "Y", "Z", "I", "J", "K", "R"):
            if a in wraw and "." not in wraw[a] and wraw[a].lstrip("+-") not in ("0",):
                self.ev("no_decimal", word=a + wraw[a])

        gset = set()
        for g in gs:
            gi = int(g) if g == int(g) else g
            gset.add(gi)
            if gi not in KNOWN_G:
                self.ev("unknown", code=f"G{g:g}")
        for m in ms:
            if m not in KNOWN_M:
                self.ev("unknown", code=f"M{m}")
        if self.first_motion_line is None:
            self.seen_modal |= {g for g in gset if isinstance(g, int)}

        # ---- modal / setup words (order roughly per Fanuc execution order)
        for g, what in ((51, "G51 scaling"), (68, "G68 coordinate rotation"), (65, "G65 macro call"),
                        (66, "G66 modal macro"), (47, "G47 engraving"), (12, "G12 circular pocket"),
                        (13, "G13 circular pocket"), (150, "G150 pocket milling"), (70, "G70 bolt circle"),
                        (71, "G71 bolt arc"), (72, "G72 holes along angle")):
            if g in gset:
                self.ev("unsupported", what=what)
        if 93 in gset or 95 in gset:
            self.ev("unsupported", what="G93/G95 feed mode (sim assumes G94 per-minute feed)")
        if 20 in gset:
            self.prog_units = "inch"; self.units_seen.add("inch"); self.ev("units", units="inch")
        if 21 in gset:
            self.prog_units = "mm"; self.units_seen.add("mm"); self.ev("units", units="mm")
        for p in (17, 18, 19):
            if p in gset:
                self.plane = p
        if 90 in gset:
            self.absolute = True
        if 91 in gset:
            self.absolute = False
        for c in (54, 55, 56, 57, 58, 59, 154):
            if c in gset:
                self.wcs = f"G{c}" + (f" P{int(w['P'])}" if c == 154 and "P" in w else "")
                self.ev("wcs", wcs=self.wcs)
        if 40 in gset:
            self.comp = 40
        for c in (41, 42):
            if c in gset:
                self.comp = c
                self.ev("cutter_comp", code=f"G{c}", d=w.get("D"))
        if 49 in gset:
            self.tlc = None
        if 98 in gset:
            self.retract98 = True
        if 99 in gset:
            self.retract98 = False
        if "F" in w:
            self.feed = w["F"] * self.f()
            self.ev("feed", value=self.feed)
        if "S" in w:
            self.rpm = w["S"]
            self.ev("rpm", value=self.rpm)
        if "T" in w:
            self.pending_tool = int(w["T"])
        if 6 in ms:
            if self.pending_tool is None:
                self.ev("toolchange_no_t")
            else:
                self.tool = self.pending_tool
            self.spindle_on = False     # Haas stops the spindle for a tool change
            self.pos[0] = self.pos[1] = math.nan
            self.pos[2] = self.home_z
            self.cycle_active = False
            self.ev("toolchange", tool=self.tool)
        if 3 in ms or 4 in ms:
            self.spindle_on = True
            self.ev("spindle", on=True, rpm=self.rpm, dir="CW" if 3 in ms else "CCW")
        if 5 in ms:
            self.spindle_on = False
            self.ev("spindle", on=False)
        if any(m in ms for m in (7, 8, 88)):
            self.coolant = True
            self.ev("coolant", on=True)
        if 9 in ms or 89 in ms:
            self.coolant = False
            self.ev("coolant", on=False)
        if 30 in ms or 2 in ms:
            self.ev("end", code=f"M{[m for m in ms if m in (30, 2)][0]}", spindle_on=self.spindle_on,
                    coolant=self.coolant, z=self.pos[2])
        for m in (0, 1):
            if m in ms:
                self.ev("stop", code=f"M0{m}")
        if 97 in ms or 98 in ms or 99 in ms:
            self.ev("unsupported", what=f"subprogram call/return M{[m for m in ms if m in (97, 98, 99)][0]}")
        if 43 in gset or 44 in gset:
            self.tlc = int(w.get("H", self.tool))
            self.ev("tlc", h=self.tlc, tool=self.tool)
        if 10 in gset:
            self.ev("unsupported", what="G10 data setting")
            return
        if 4 in gset:
            return  # dwell

        # ---- non-modal positioning
        if (28 in gset or 53 in gset) and 0 in gset:
            self.motion = 0
        if 28 in gset:
            self.ev("g28", absolute=self.absolute, words={k: v for k, v in w.items() if k in "XYZ"})
            if self.absolute and any(k in w for k in "XYZ"):
                # intermediate point in absolute mode
                inter = [w[a] * self.f() if a in w else self.pos[i] for i, a in enumerate("XYZ")]
                self.emit("rapid", inter, source="home")
            end = list(self.pos)
            if "Z" in w:
                end[2] = self.home_z
            self.emit("rapid", end, source="home")
            if "X" in w or "Y" in w:
                self.pos[0] = self.pos[1] = math.nan
            return
        if 53 in gset:
            end = list(self.pos)
            if "Z" in w:
                end[2] = self.home_z
            self.emit("rapid", end, source="home")
            if "X" in w or "Y" in w:
                self.pos[0] = self.pos[1] = math.nan
            self.ev("g53", words={k: v for k, v in w.items() if k in "XYZ"})
            return

        # ---- motion mode
        motion_codes = [g for g in gset if g in (0, 1, 2, 3, 80) or g in CANNED]
        if motion_codes:
            m = motion_codes[-1]
            if m == 80:
                self.motion = None if self.motion in CANNED else self.motion
                self.cycle_active = False
                self.cycle_q = 0.0       # G80 clears the canned-cycle data
                self.cycle_ijk = None
                self.ev("g80")
                others = [g for g in motion_codes if g != 80]
                if others:
                    m = others[-1]
                else:
                    m = None
            if m is not None:
                if m in CANNED:
                    if not self.cycle_active or self.motion != m:
                        self.cycle_initial_z = self.pos[2]
                    self.cycle_active = True
                    self.ev("cycle", code=m)
                elif self.cycle_active:
                    self.ev("cycle_cancel_by_motion", code=m)
                    self.cycle_active = False
                self.motion = m

        has_axis = any(a in w for a in "XYZ")
        if self.motion in CANNED and self.cycle_active:
            if "R" in w:
                self.cycle_r = w["R"] * self.f()
            if "Z" in w:
                self.cycle_z = w["Z"] * self.f()
            if "Q" in w:
                self.cycle_q = w["Q"] * self.f()
            if self.motion in (73, 83) and "I" in w:
                i_ = abs(w["I"]) * self.f()
                self.cycle_ijk = (i_, abs(w.get("J", 0.0)) * self.f(), abs(w.get("K", w["I"])) * self.f())
            if "P" in w:
                self.ev("dwell", p=w["P"])
            if any(a in w for a in "XY") or any(g in CANNED for g in gset):
                # Haas uses K on G73/G83 as a peck parameter (e.g. full-retract depth), not a repeat count.
                k_is_repeat = self.motion not in (73, 83)
                reps = int(w.get("L", w.get("K", 1) if k_is_repeat else 1))
                for _ in range(max(reps, 0)):
                    self._cycle(w)
            return
        if not has_axis and not (self.motion in (2, 3) and any(a in w for a in "IJK")):
            return
        if self.motion is None:
            self.ev("motion_without_mode")
            return
        end = self._target(w)
        if self.motion == 0:
            self.emit("rapid", end)
        elif self.motion == 1:
            self.emit("feed", end)
        elif self.motion in (2, 3):
            self._arc(w, end)

    def _target(self, w):
        k = self.f()
        end = list(self.pos)
        for i, a in enumerate("XYZ"):
            if a in w:
                v = w[a] * k
                if self.absolute:
                    end[i] = v
                else:
                    base = end[i] if not math.isnan(end[i]) else 0.0
                    end[i] = base + v
        return end

    def _arc(self, w, end):
        k = self.f()
        ax = {17: (0, 1, 2), 18: (2, 0, 1), 19: (1, 2, 0)}[self.plane]  # (u, v, normal)
        ijk = {0: "I", 1: "J", 2: "K"}
        s = list(self.pos)
        if math.isnan(s[0]) or math.isnan(s[1]):
            self.ev("arc_from_unknown")
            self.pos = list(end)
            return
        su, sv = s[ax[0]], s[ax[1]]
        eu, ev_ = end[ax[0]], end[ax[1]]
        cw = self.motion == 2
        if "R" in w:
            r = w["R"] * k
            dx, dy = eu - su, ev_ - sv
            d = math.hypot(dx, dy)
            if d < 1e-9:
                self.ev("arc_error", msg="R-format arc with start == end (full circle needs I/J/K)")
                return
            if abs(r) * 2 < d - 1e-6:
                self.ev("arc_error", msg=f"R{r:.4f} too small for chord {d:.4f}")
                r = math.copysign(d / 2, r)
            h = math.sqrt(max(r * r - d * d / 4, 0.0))
            mu, mv = (su + eu) / 2, (sv + ev_) / 2
            # left-hand normal of chord direction
            nu, nv = -dy / d, dx / d
            # for CW small arc the center lies to the right of the chord
            sign = -1 if cw else 1
            if r < 0:
                sign = -sign
            cu, cv = mu + sign * h * nu, mv + sign * h * nv
        else:
            cu = su + w.get(ijk[ax[0]], 0.0) * k
            cv = sv + w.get(ijk[ax[1]], 0.0) * k
            r0 = math.hypot(su - cu, sv - cv)
            r1 = math.hypot(eu - cu, ev_ - cv)
            if abs(r0 - r1) > 0.001 * unit_factor("inch", self.job_units):
                self.ev("arc_error", msg=f"radius mismatch start {r0:.4f} end {r1:.4f}")
        center = [0.0, 0.0, 0.0]
        center[ax[0]], center[ax[1]] = cu, cv
        center[ax[2]] = s[ax[2]]
        self.emit("arc", end, center=tuple(center), cw=cw)

    def _cycle(self, w):
        k = self.f()
        if self.cycle_r is None or self.cycle_z is None:
            self.ev("cycle_error", msg="canned cycle without R or Z")
            return
        init = self.cycle_initial_z if self.cycle_initial_z is not None else self.pos[2]
        # G91: R is incremental from the initial level, Z from R
        r = (init + self.cycle_r) if not self.absolute else self.cycle_r
        z = (r + self.cycle_z) if not self.absolute else self.cycle_z
        tgt = list(self.pos)
        for i, a in enumerate("XY"):
            if a in w:
                v = w[a] * k
                tgt[i] = v if self.absolute else (0.0 if math.isnan(tgt[i]) else tgt[i]) + v
        code = self.motion
        self.ev("cycle_hole", code=code, r=r, z=z, init=init, q=self.cycle_q, ijk=self.cycle_ijk,
                retract98=self.retract98)
        # 1. rapid XY at current level, 2. rapid to R
        self.emit("rapid", [tgt[0], tgt[1], self.pos[2]], source="cycle", cycle=code)
        if abs(self.pos[2] - r) > 1e-9:
            self.emit("rapid", [tgt[0], tgt[1], r], source="cycle", cycle=code)
        x, y = tgt[0], tgt[1]
        q = abs(self.cycle_q)
        ijk = self.cycle_ijk if code in (83, 73) else None
        if code in (83, 73) and (q > 0 or ijk):
            # Haas: I/J/K (first peck, reduction per peck, minimum peck) when given, else constant Q
            step, red, kmin = ijk if ijk else (q, 0.0, q)
            kmin = kmin if kmin > 0 else step
            depth = r
            n = 0
            while depth - step > z + 1e-9 and n < 100000:
                depth -= step
                n += 1
                step = max(step - red, kmin)
                self.emit("feed", [x, y, depth], source="cycle", cycle=code)
                if code == 83:
                    self.emit("rapid", [x, y, r], source="cycle", cycle=code)
                    self.emit("rapid", [x, y, depth + 0.01 * unit_factor("inch", self.job_units)],
                              source="cycle", cycle=code)
            self.emit("feed", [x, y, z], source="cycle", cycle=code)
        else:
            self.emit("feed", [x, y, z], source="cycle", cycle=code)
        ret = init if self.retract98 else r
        if code in (84, 85, 89, 74):
            self.emit("feed", [x, y, r], source="cycle", cycle=code)
            if ret > r:
                self.emit("rapid", [x, y, ret], source="cycle", cycle=code)
        else:
            self.emit("rapid", [x, y, ret], source="cycle", cycle=code)


def parse_files(paths, job_units="inch", home_z=10.0) -> Program:
    it = Interpreter(job_units, home_z)
    for p in paths:
        it.run_file(p)
    return it.program()


def parse_text(text, job_units="inch", home_z=10.0, name="<text>") -> Program:
    return Interpreter(job_units, home_z).run_text(text, name).program()


def arc_points(m: Move, tol: float):
    """Points along an arc move (excluding start, including end)."""
    ax = {17: (0, 1, 2), 18: (2, 0, 1), 19: (1, 2, 0)}[m.plane]
    s, e, c = m.start, m.end, m.center
    su, sv = s[ax[0]] - c[ax[0]], s[ax[1]] - c[ax[1]]
    eu, ev = e[ax[0]] - c[ax[0]], e[ax[1]] - c[ax[1]]
    r = math.hypot(su, sv)
    a0, a1 = math.atan2(sv, su), math.atan2(ev, eu)
    if m.cw:
        sweep = a0 - a1
        if sweep <= 1e-9:
            sweep += 2 * math.pi
        sweep = -sweep
    else:
        sweep = a1 - a0
        if sweep <= 1e-9:
            sweep += 2 * math.pi
    if r < 1e-9:
        return [e]
    step = 2 * math.acos(max(min(1 - tol / r, 1.0), -1.0)) if tol < r else math.pi / 4
    n = max(2, int(math.ceil(abs(sweep) / max(step, 1e-3))))
    pts = []
    for i in range(1, n + 1):
        t = i / n
        a = a0 + sweep * t
        p = [0.0, 0.0, 0.0]
        p[ax[0]] = c[ax[0]] + r * math.cos(a)
        p[ax[1]] = c[ax[1]] + r * math.sin(a)
        p[ax[2]] = s[ax[2]] + (e[ax[2]] - s[ax[2]]) * t
        pts.append(tuple(p))
    pts[-1] = tuple(e)
    return pts


def arc_length(m: Move) -> float:
    pts = [m.start] + arc_points(m, 1e-4)
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
