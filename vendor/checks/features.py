"""Feature recognition from a STEP solid (OCP / OpenCascade B-rep), for process checks.

    python -m checks.features part.step [--units inch|mm] [--spec spec.json] > features.json
    python -m checks.features --recall parts/          # recall table against parts/*/spec.json

What it finds (all lengths in the output units, directions in part/model coordinates):
- holes: coaxial stacks of concave cylinders and cones whose axis runs through air. Per hole: axis,
  entry point and direction, main diameter, full-diameter depth, through / blind / breaks-into, the stack
  from the entry (counterbore, countersink, entry chamfer, drill point), holes it intersects, the minimum
  wall around it and the material it pierces along its axis (thinnest pierced wall), standard drill /
  tap-drill / reamer matches from tooling/crib.json and wiki/hole-making.md.
- pockets, steps and slots: planar floors below the outermost face in each of the 6 principal directions.
  Closed floor = pocket, open on a side = step, narrow and long = slot, annular floor = annular groove.
  Floor level, depth, width (closest opposing walls), minimum vertical corner radius, floor fillet.
  Through slots (no floor) from pairs of half-cylinder ends.
- outside profile (rectangular / round, size, vertical corner radii), bosses (convex cylinders).
- fillets (tori, partial non-hole cylinders) and chamfers (45-degree planes, cones not in holes).
- thin walls and floors: opposing planar faces with material between, thinner than --thin (default .10 in).
- bounding box and stock suggestions.

STEP units: OCCT converts every STEP file to millimetres. The file's own unit is read from its header and
reported; the design unit (inch or mm) is inferred from which unit makes the dimensions round. Output
units default to the design unit.

Limits: B-rep analytic surfaces only (plane, cylinder, cone, torus). Freeform (B-spline) faces are counted
but not classified. Holes that break out sideways through a wall (less than 300 degrees of cylinder)
are reported as partial cylinders, not holes. Only principal-direction floors are recognised as pockets.
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

MM = 25.4
TOL = 0.01  # mm, geometric comparison tolerance
PRINCIPAL = {"+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0), "-Y": (0, -1, 0), "+Z": (0, 0, 1), "-Z": (0, 0, -1)}


# ----------------------------------------------------------------------------------------- loading

def read_step(path):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    r = STEPControl_Reader()
    if r.ReadFile(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"cannot read STEP {path}")
    r.TransferRoots()
    shape = r.OneShape()
    solids = []
    ex = TopExp_Explorer(shape, TopAbs_SOLID)
    while ex.More():
        solids.append(TopoDS.Solid_s(ex.Current()))
        ex.Next()
    if not solids:
        raise RuntimeError(f"no solid in {path}")
    if len(solids) > 1:  # keep the largest solid
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        def vol(s):
            p = GProp_GProps(); BRepGProp.VolumeProperties_s(s, p); return p.Mass()
        solids.sort(key=vol, reverse=True)
    return solids[0], len(solids)


def step_file_unit(path) -> str:
    head = Path(path).read_bytes()[:400000].decode("latin-1", "replace").upper()
    if re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'INCH'", head):
        return "inch"
    if "SI_UNIT(.MILLI.,.METRE.)" in head.replace(" ", ""):
        return "mm"
    if "SI_UNIT(.CENTI.,.METRE.)" in head.replace(" ", ""):
        return "cm"
    if "SI_UNIT($,.METRE.)" in head.replace(" ", ""):
        return "m"
    return "unknown"


def _v(p):
    return np.array([p.X(), p.Y(), p.Z()], dtype=float)


def _principal(d, tol=1e-6):
    for k, v in PRINCIPAL.items():
        if np.dot(d, v) > 1 - tol:
            return k
    return None


def _canon(d):
    d = np.asarray(d, float)
    d = d / np.linalg.norm(d)
    for c in d:
        if abs(c) > 1e-9:
            return d if c > 0 else -d
    return d


class Part:
    """B-rep solid with face data and helpers (all in mm)."""

    def __init__(self, path):
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
        from OCP.TopExp import TopExp
        from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape
        self.path = Path(path)
        self.solid, self.n_solids = read_step(path)
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(self.solid, box, False, False)
        x0, y0, z0, x1, y1, z1 = box.Get()
        self.bmin, self.bmax = np.array([x0, y0, z0]), np.array([x1, y1, z1])
        self.diag = float(np.linalg.norm(self.bmax - self.bmin))
        self.fmap = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(self.solid, TopAbs_FACE, self.fmap)
        self.emap = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(self.solid, TopAbs_EDGE, TopAbs_FACE, self.emap)
        self.faces = [self._info(i) for i in range(1, self.fmap.Extent() + 1)]
        self.adj = defaultdict(set)
        for ei in range(1, self.emap.Extent() + 1):
            lst = self.emap.FindFromIndex(ei)
            ids = [self.fmap.FindIndex(f) - 1 for f in lst]
            for a in ids:
                for b in ids:
                    if a != b:
                        self.adj[a].add(b)
        from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
        self._isect = IntCurvesFace_ShapeIntersector()
        self._isect.Load(self.solid, 1e-4)
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        BRepMesh_IncrementalMesh(self.solid, 0.05, False, 0.5, True)

    def inner_point(self, fid):
        """A point inside the face (centroid of its largest mesh triangle)."""
        from OCP.BRep import BRep_Tool
        from OCP.TopLoc import TopLoc_Location
        fi = self.faces[fid]
        if "_inner" in fi:
            return fi["_inner"]
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(fi["face"], loc)
        best, pt = -1.0, fi["pmid"]
        if tri is not None:
            trsf = loc.Transformation()
            for k in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(k).Get()
                P = [_v(tri.Node(x).Transformed(trsf)) for x in (a, b, c)]
                ar = float(np.linalg.norm(np.cross(P[1] - P[0], P[2] - P[0])))
                if ar > best:
                    best, pt = ar, (P[0] + P[1] + P[2]) / 3
        fi["_inner"] = pt
        return pt

    def open_along(self, pt, n, eps=0.05) -> bool:
        """True when the ray from pt along n leaves the part without meeting material."""
        L = self.diag * 1.5
        runs = self.line_runs(np.asarray(pt, float), np.asarray(n, float), eps, L)
        return not runs

    # -- per-face data
    def _info(self, i):
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepGProp import BRepGProp, BRepGProp_Face
        from OCP.BRepTools import BRepTools
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Torus
        from OCP.GProp import GProp_GProps
        from OCP.TopoDS import TopoDS
        from OCP.gp import gp_Pnt, gp_Vec
        f = TopoDS.Face_s(self.fmap.FindKey(i))
        a = BRepAdaptor_Surface(f)
        t = a.GetType()
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, props)
        u0, u1, v0, v1 = BRepTools.UVBounds_s(f)
        p, n = gp_Pnt(), gp_Vec()
        BRepGProp_Face(f).Normal((u0 + u1) / 2, (v0 + v1) / 2, p, n)
        nv = _v(n)
        nv = nv / (np.linalg.norm(nv) or 1)
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(f, box, False, False)
        bx = box.Get()
        info = {"id": i - 1, "face": f, "area": props.Mass(), "centroid": _v(props.CentreOfMass()),
                "uv": (u0, u1, v0, v1), "pmid": _v(p), "nmid": nv,
                "bmin": np.array(bx[:3]), "bmax": np.array(bx[3:])}
        if t == GeomAbs_Plane:
            info.update(kind="plane", normal=nv)
        elif t in (GeomAbs_Cylinder, GeomAbs_Cone):
            g = a.Cylinder() if t == GeomAbs_Cylinder else a.Cone()
            ax = g.Axis()
            d, o = _v(ax.Direction()), _v(ax.Location())
            q = o + d * np.dot(info["pmid"] - o, d)  # axis point level with the mid point
            concave = float(np.dot(nv, q - info["pmid"])) > 0
            info.update(kind="cylinder" if t == GeomAbs_Cylinder else "cone", axis_dir=d, axis_loc=o,
                        concave=concave, span=u1 - u0)
            if t == GeomAbs_Cylinder:
                info.update(radius=g.Radius(), v_range=(v0, v1))
            else:
                sa = g.SemiAngle()
                r0 = g.RefRadius()
                info.update(semi_angle=math.degrees(abs(sa)),
                            radii=(r0 + v0 * math.sin(sa), r0 + v1 * math.sin(sa)),
                            ax_range=(v0 * math.cos(sa), v1 * math.cos(sa)))
        elif t == GeomAbs_Torus:
            g = a.Torus()
            ax = g.Axis()
            d, o = _v(ax.Direction()), _v(ax.Location())
            info.update(kind="torus", axis_dir=d, axis_loc=o, major=g.MajorRadius(), minor=g.MinorRadius())
            # concave (fillet) when the normal points toward the tube centre circle
            rvec = info["pmid"] - o - d * np.dot(info["pmid"] - o, d)
            cc = o + d * np.dot(info["pmid"] - o, d) + rvec / (np.linalg.norm(rvec) or 1) * g.MajorRadius()
            info["concave"] = float(np.dot(nv, cc - info["pmid"])) < 0
        else:
            info.update(kind="other")
        return info

    # -- axial range of a cylinder/cone face on a canonical line (base, c)
    def axial_range(self, fi, base, c):
        d, o = fi["axis_dir"], fi["axis_loc"]
        s0 = float(np.dot(o - base, c))
        k = float(np.dot(d, c))
        if fi["kind"] == "cylinder":
            a, b = fi["v_range"]
        else:
            a, b = fi["ax_range"]
        lo, hi = sorted((s0 + a * k, s0 + b * k))
        return lo, hi

    # -- point classification and line intersection
    def classify(self, pt) -> str:
        """'in' / 'on' / 'out' of the solid. One classifier is loaded per part and reused: building it per
        point re-scans every face (13 s of 15 s on the engraved t1-04; same results, 8x faster)."""
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier
        from OCP.TopAbs import TopAbs_IN, TopAbs_ON
        from OCP.gp import gp_Pnt
        c = self.__dict__.get("_classifier")
        if c is None:
            c = self._classifier = BRepClass3d_SolidClassifier(self.solid)
        c.Perform(gp_Pnt(*map(float, pt)), 1e-5)
        s = c.State()
        return "in" if s == TopAbs_IN else ("on" if s == TopAbs_ON else "out")

    def line_runs(self, base, c, lo, hi):
        """Material (IN) intervals [(a, b)] along base + s c for s in [lo, hi]."""
        from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
        lin = gp_Lin(gp_Pnt(*map(float, base)), gp_Dir(*map(float, c)))
        self._isect.Perform(lin, float(lo), float(hi))
        ws = sorted({round(self._isect.WParameter(i), 6) for i in range(1, self._isect.NbPnt() + 1)})
        pts = [lo] + [w for w in ws if lo < w < hi] + [hi]
        runs = []
        for a, b in zip(pts[:-1], pts[1:]):
            if b - a < 1e-6:
                continue
            m = (a + b) / 2
            if self.classify(base + c * m) == "in":
                if runs and abs(runs[-1][1] - a) < 1e-6:
                    runs[-1] = (runs[-1][0], b)
                else:
                    runs.append((a, b))
        return runs

    def min_distance(self, ids_a, ids_b) -> float | None:
        from OCP.BRep import BRep_Builder
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        from OCP.TopoDS import TopoDS_Compound
        if not ids_a or not ids_b:
            return None
        bld = BRep_Builder()
        ca, cb = TopoDS_Compound(), TopoDS_Compound()
        bld.MakeCompound(ca)
        bld.MakeCompound(cb)
        for i in ids_a:
            bld.Add(ca, self.faces[i]["face"])
        for i in ids_b:
            bld.Add(cb, self.faces[i]["face"])
        d = BRepExtrema_DistShapeShape(ca, cb)
        return float(d.Value()) if d.IsDone() else None

    def outer_wire_neighbors(self, fid):
        from OCP.BRepTools import BRepTools
        from OCP.TopAbs import TopAbs_EDGE
        from OCP.TopExp import TopExp_Explorer
        w = BRepTools.OuterWire_s(self.faces[fid]["face"])
        out = set()
        ex = TopExp_Explorer(w, TopAbs_EDGE)
        while ex.More():
            idx = self.emap.FindIndex(ex.Current())
            if idx:
                for f in self.emap.FindFromIndex(idx):
                    j = self.fmap.FindIndex(f) - 1
                    if j != fid:
                        out.add(j)
            ex.Next()
        return out

    def face_nodes(self, fid):
        """Mesh nodes of a face (exact vertices plus interior samples)."""
        from OCP.BRep import BRep_Tool
        from OCP.TopLoc import TopLoc_Location
        fi = self.faces[fid]
        if "_nodes" not in fi:
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(fi["face"], loc)
            if tri is None:
                fi["_nodes"] = np.array([[x, y, z] for x in (fi["bmin"][0], fi["bmax"][0])
                                         for y in (fi["bmin"][1], fi["bmax"][1]) for z in (fi["bmin"][2], fi["bmax"][2])])
            else:
                trsf = loc.Transformation()
                fi["_nodes"] = np.array([_v(tri.Node(i).Transformed(trsf)) for i in range(1, tri.NbNodes() + 1)])
        return fi["_nodes"]

    def extent_along(self, fid, n):
        s = self.face_nodes(fid) @ np.asarray(n, float)
        return float(s.min()), float(s.max())

    def top_along(self, n):
        n = np.asarray(n, float)
        if not hasattr(self, "_allnodes"):
            self._allnodes = np.concatenate([self.face_nodes(i) for i in range(len(self.faces))])
        return float(np.max(self._allnodes @ n))


# ----------------------------------------------------------------------------------------- holes

def _group_axes(part):
    """Concave cylinders/cones grouped by axis line."""
    groups = []
    for fi in part.faces:
        if fi["kind"] not in ("cylinder", "cone") or not fi.get("concave"):
            continue
        c = _canon(fi["axis_dir"])
        o = fi["axis_loc"]
        base = o - c * np.dot(o, c)  # point on the line closest to the origin
        for g in groups:
            if abs(np.dot(g["c"], c)) > 1 - 1e-6 and np.linalg.norm(np.cross(base - g["base"], g["c"])) < TOL:
                g["faces"].append(fi["id"])
                break
        else:
            groups.append({"c": c, "base": base, "faces": [fi["id"]]})
    return groups


def _elements(part, g):
    """Axial elements (cylinder bands, cones) of a group, sorted along the axis."""
    c, base = g["c"], g["base"]
    bands = []
    for fid in g["faces"]:
        fi = part.faces[fid]
        lo, hi = part.axial_range(fi, base, c)
        if fi["kind"] == "cylinder":
            r = fi["radius"]
            for b in bands:
                if b["kind"] == "cyl" and abs(b["r"] - r) < TOL and abs(b["lo"] - lo) < TOL and abs(b["hi"] - hi) < TOL:
                    b["span"] += fi["span"]
                    b["faces"].append(fid)
                    break
            else:
                bands.append({"kind": "cyl", "r": r, "lo": lo, "hi": hi, "span": fi["span"], "faces": [fid]})
        else:
            # radius at each axial end
            r_a, r_b = fi["radii"]
            ax_a, ax_b = fi["ax_range"]
            k = float(np.dot(fi["axis_dir"], c))
            s0 = float(np.dot(fi["axis_loc"] - base, c))
            ends = sorted([(s0 + ax_a * k, abs(r_a)), (s0 + ax_b * k, abs(r_b))])
            for b in bands:
                if b["kind"] == "cone" and abs(b["lo"] - ends[0][0]) < TOL and abs(b["hi"] - ends[1][0]) < TOL:
                    b["span"] += fi["span"]
                    b["faces"].append(fid)
                    break
            else:
                bands.append({"kind": "cone", "lo": ends[0][0], "hi": ends[1][0], "r_lo": ends[0][1],
                              "r_hi": ends[1][1], "semi": fi["semi_angle"], "span": fi["span"], "faces": [fid]})
    # merge cylinder bands of equal radius that touch (split faces along the axis)
    bands.sort(key=lambda b: (b["lo"], b["hi"]))
    return bands


def _axis_air(part, base, c, s):
    return part.classify(base + c * s) != "in"


def _bore_air(part, base, c, s, e):
    """Air from the axis out to (just inside) the element's wall at level s: a real hole, not the outer wall of
    an annular groove around an island."""
    r = e["r"] if e["kind"] == "cyl" else min(e["r_lo"], e["r_hi"])
    perp = np.cross(c, [0, 0, 1.0])
    if np.linalg.norm(perp) < 1e-6:
        perp = np.cross(c, [1.0, 0, 0])
    perp /= np.linalg.norm(perp)
    perp2 = np.cross(c, perp)
    p0 = base + c * s
    for frac in (0.0, 0.35, 0.7, 0.93):
        for dv in ((perp,) if frac == 0 else (perp, -perp, perp2, -perp2)):
            if part.classify(p0 + dv * r * frac) == "in":
                return False
    return True


def find_holes(part):
    holes, partial = [], []
    for g in _group_axes(part):
        c, base = g["c"], g["base"]
        els = _elements(part, g)
        full, parts_ = [], []
        for e in els:
            cover = e["span"] >= math.radians(300)
            mid = (e["lo"] + e["hi"]) / 2
            (full if cover and _bore_air(part, base, c, mid, e) else parts_).append(e)
        # a band with partial cover next to a full band of the same radius (split by a cross hole) joins it
        for e in list(parts_):
            if e["kind"] == "cyl" and e["span"] >= math.radians(150) and any(
                    f["kind"] == "cyl" and abs(f["r"] - e["r"]) < TOL for f in full) and \
                    _axis_air(part, base, c, (e["lo"] + e["hi"]) / 2):
                # both halves of a band cut by a cross hole appear as separate bands with the same range;
                # _elements merged them already, so this is a genuine partial: keep only if the other half exists
                pass
        for e in parts_:
            partial.append({**e, "c": c, "base": base})
        if not full:
            continue
        full.sort(key=lambda e: e["lo"])
        # chain elements into holes; a gap that is air along the axis keeps them in one hole
        chains = [[full[0]]]
        for e in full[1:]:
            prev_hi = max(x["hi"] for x in chains[-1])
            gap = e["lo"] - prev_hi
            if gap <= TOL * 5 or all(_axis_air(part, base, c, prev_hi + gap * f) for f in (0.25, 0.5, 0.75)):
                chains[-1].append(e)
            else:
                chains.append([e])
        for ch in chains:
            holes.append(_hole_record(part, c, base, ch))
    return holes, partial


def _hole_record(part, c, base, els):
    lo = min(e["lo"] for e in els)
    hi = max(e["hi"] for e in els)
    L = part.diag * 1.5
    runs = part.line_runs(base, c, lo - L, hi + L)

    def end_state(s, sign):
        eps = 0.05
        probe = s + sign * eps
        if part.classify(base + c * probe) == "in" or any(a - 1e-6 <= probe <= b + 1e-6 for a, b in runs):
            return "blind"
        beyond = [r for r in runs if (r[0] > s + 1e-6 if sign > 0 else r[1] < s - 1e-6)]
        return "breaks_into" if beyond else "open"

    st_lo, st_hi = end_state(lo, -1), end_state(hi, +1)
    cyls = [e for e in els if e["kind"] == "cyl"]
    rmax = max([e["r"] for e in cyls] + [e.get("r_hi", 0) for e in els if e["kind"] == "cone"] + [0])

    def end_radius(s):
        best = None
        for e in els:
            if abs(e["lo"] - s) < TOL * 5:
                r = e["r"] if e["kind"] == "cyl" else e["r_lo"]
            elif abs(e["hi"] - s) < TOL * 5:
                r = e["r"] if e["kind"] == "cyl" else e["r_hi"]
            else:
                continue
            best = r if best is None else max(best, r)
        return best or 0.0

    # entry end: an open end; with two open ends the larger diameter, then the +Z/+X/+Y side
    opens = [(lo, -1) for _ in [0] if st_lo == "open"] + [(hi, +1) for _ in [0] if st_hi == "open"]
    if not opens:
        opens = [(lo, -1) if st_lo != "blind" else (hi, +1)]
    if len(opens) == 2:
        ra, rb = end_radius(lo), end_radius(hi)

        def gap(o):  # air from the end to the bounding box (0 = the end is on the outer skin)
            p0 = base + c * o[0]
            ext = [(part.bmax[k] - p0[k]) / (c[k] * o[1]) if c[k] * o[1] > 1e-9 else
                   ((part.bmin[k] - p0[k]) / (c[k] * o[1]) if c[k] * o[1] < -1e-9 else np.inf) for k in range(3)]
            return round(min(ext), 2)
        opens.sort(key=lambda o: (0 if abs(ra - rb) <= TOL else -end_radius(o[0]), gap(o),
                                  -float(np.dot(c * o[1], [0.001, 0.0001, 1.0]))))
    s_entry, sign = opens[0]
    entry_dir = c * sign                     # points out of the part at the entry
    s_exit = hi if sign < 0 else lo
    depth_total = hi - lo
    # elements ordered from the entry
    ordered = sorted(els, key=lambda e: (e["lo"] if sign < 0 else -e["hi"]))

    def dist_from_entry(s):
        return abs(s - s_entry)

    stack = []
    for e in ordered:
        a, b = dist_from_entry(e["lo"]), dist_from_entry(e["hi"])
        a, b = min(a, b), max(a, b)
        if e["kind"] == "cyl":
            stack.append({"type": "cyl", "d": 2 * e["r"], "from": a, "to": b, "faces": e["faces"]})
        else:
            r_near = e["r_lo"] if dist_from_entry(e["lo"]) < dist_from_entry(e["hi"]) else e["r_hi"]
            r_far = e["r_hi"] if r_near == e["r_lo"] else e["r_lo"]
            stack.append({"type": "cone", "d_near": 2 * r_near, "d_far": 2 * r_far, "included_angle": 2 * e["semi"],
                          "from": a, "to": b, "faces": e["faces"]})
    cyl_st = [s for s in stack if s["type"] == "cyl"]
    main = cyl_st[-1] if cyl_st else None
    # full-diameter depth = to the end of the last cylinder
    full_depth = max(s["to"] for s in cyl_st) if cyl_st else depth_total
    exit_state = st_hi if sign < 0 else st_lo
    if exit_state == "open":
        # line of sight to the outside, but the exit is inside the part envelope (a pocket, maybe looking
        # through another hole): the hole exits into a cavity
        pe = base + c * (hi if sign < 0 else lo)
        out_dir = c * (1 if sign < 0 else -1)
        ext = [(part.bmax[k] - pe[k]) / out_dir[k] if out_dir[k] > 1e-9 else
               ((part.bmin[k] - pe[k]) / out_dir[k] if out_dir[k] < -1e-9 else np.inf) for k in range(3)]
        if min(ext) > 0.5:
            exit_state = "breaks_into"
    kind = "blind" if exit_state == "blind" else "through"
    hole = {"axis": c.tolist(), "entry_dir": entry_dir.tolist(), "tool_axis": (-entry_dir).tolist(),
            "entry_point": (base + c * s_entry).tolist(), "exit_point": (base + c * s_exit).tolist(),
            "diameter": main["d"] if main else 0.0, "depth": full_depth, "depth_total": depth_total,
            "kind": kind, "exits_into": {"open": "outside", "breaks_into": "cavity", "blind": None}[exit_state],
            "stack": stack,
            "faces": sorted({f for e in els for f in e["faces"]})}
    # classification of the stack
    tags = []
    if cyl_st and len({round(s["d"], 3) for s in cyl_st}) > 1 and cyl_st[0]["d"] > cyl_st[-1]["d"] + TOL:
        tags.append("counterbore")
        hole["counterbore"] = {"d": cyl_st[0]["d"], "depth": cyl_st[0]["to"]}
    if stack and stack[0]["type"] == "cone" and stack[0]["d_near"] > stack[0]["d_far"]:
        cone = stack[0]
        size = (cone["d_near"] - cone["d_far"]) / 2
        if cone["d_near"] >= 1.35 * cone["d_far"] and size > 0.6:  # > ~.025 in radial: a countersink
            tags.append("countersink")
            hole["countersink"] = {"d": cone["d_near"], "included_angle": round(cone["included_angle"], 1)}
        else:
            tags.append("entry_chamfer")
            hole["entry_chamfer"] = {"size": size, "included_angle": round(cone["included_angle"], 1)}
    if stack and stack[-1]["type"] == "cone" and stack[-1]["d_near"] > stack[-1]["d_far"] and kind != "through":
        tags.append("drill_point")
        hole["point_angle"] = round(stack[-1]["included_angle"], 1)
    elif kind == "blind":
        tags.append("flat_bottom")
    for s in stack[1:]:
        if s["type"] == "cone" and s is not stack[-1] and s["d_near"] < s["d_far"]:
            tags.append("exit_chamfer")
    for k in range(1, len(cyl_st) - 1):
        if cyl_st[k]["d"] > cyl_st[k - 1]["d"] + TOL and cyl_st[k]["d"] > cyl_st[k + 1]["d"] + TOL:
            tags.append("internal_groove")
            hole.setdefault("internal_grooves", []).append(
                {"d": cyl_st[k]["d"], "width": cyl_st[k]["to"] - cyl_st[k]["from"], "at": cyl_st[k]["from"]})
    hole["tags"] = tags
    # the other end is also open for through holes
    hole["access_dirs"] = [entry_dir.tolist()] + ([(-entry_dir).tolist()] if kind == "through" else [])
    # material pierced along the axis (radius just outside the hole)
    r0 = (main["d"] / 2 if main else rmax) + 0.2
    perp = np.cross(c, [0, 0, 1.0])
    if np.linalg.norm(perp) < 1e-6:
        perp = np.cross(c, [1.0, 0, 0])
    perp /= np.linalg.norm(perp)
    perp2 = np.cross(c, perp)
    best = None
    for dvec in (perp, -perp, perp2, -perp2):
        rr = part.line_runs(base + dvec * r0, c, lo - L, hi + L)
        rr = [(a, b) for a, b in rr if a >= lo - 0.05 and b <= hi + 0.05]  # walls the hole passes through
        tot = sum(b - a for a, b in rr)
        if best is None or tot > best[0]:
            best = (tot, rr)
    hole["pierced_runs"] = [b - a for a, b in best[1]] if best else []
    hole["min_wall_pierced"] = min(hole["pierced_runs"]) if hole["pierced_runs"] else None
    hole["_axis"] = (c, base, lo, hi)
    return hole


def _hole_relations(part, holes):
    for i, h in enumerate(holes):
        h["intersects"] = []
        h["breaks_into"] = []
    for i, a in enumerate(holes):
        ca, ba, la, ha = a["_axis"]
        ra = max([s["d"] for s in a["stack"] if s["type"] == "cyl"] + [a["diameter"]]) / 2
        for j, b in enumerate(holes):
            if j <= i:
                continue
            cb, bb, lb, hb = b["_axis"]
            rb = max([s["d"] for s in b["stack"] if s["type"] == "cyl"] + [b["diameter"]]) / 2
            n = np.cross(ca, cb)
            if np.linalg.norm(n) < 1e-6:
                continue  # parallel axes (coaxial holes were merged)
            # closest points of the two lines
            w = ba - bb
            A, B, C = np.dot(ca, ca), np.dot(ca, cb), np.dot(cb, cb)
            D, E = np.dot(ca, w), np.dot(cb, w)
            den = A * C - B * B
            sa = (B * E - C * D) / den
            sb = (A * E - B * D) / den
            dist = float(np.linalg.norm((ba + ca * sa) - (bb + cb * sb)))
            if dist < ra + rb - TOL and la - rb <= sa <= ha + rb and lb - ra <= sb <= hb + ra:
                a["intersects"].append(j)
                b["intersects"].append(i)
                # the smaller hole breaks into the larger; with equal size the shorter into the longer
                small, big = (i, j) if (a["diameter"], a["depth"]) < (b["diameter"], b["depth"]) else (j, i)
                holes[small]["breaks_into"].append(big)


def _hole_walls(part, holes):
    hole_face_sets = [set(h["faces"]) for h in holes]
    all_ids = set(range(len(part.faces)))
    for i, h in enumerate(holes):
        own = hole_face_sets[i]
        nb = set()
        for f in own:
            nb |= part.adj[f]
        crossing = set()
        for j in h["intersects"]:
            crossing |= hole_face_sets[j]
        others = sorted(all_ids - own - nb - crossing)
        cyl_faces = [f for f in own if part.faces[f]["kind"] == "cylinder"]
        h["min_wall_around"] = part.min_distance(cyl_faces or sorted(own), others)


def _hole_names(h, units_in):
    d_in = h["diameter"] / MM
    out = {}
    dr = HT.drill_exists(d_in)
    out["crib_drill"] = dr
    out["standard_drill"] = dr is not None
    out["tap_drill_for"] = HT.threads_for_tap_drill(d_in)
    out["crib_reamer"] = HT.reamer_exists(d_in)
    for s in h["stack"]:
        if s["type"] == "cyl":
            s["tap_drill_for"] = HT.threads_for_tap_drill(s["d"] / MM)
            s["crib_drill"] = HT.drill_exists(s["d"] / MM)
    if "counterbore" in h.get("tags", []):
        cb = h["counterbore"]["d"] / MM
        out["counterbore_endmill"] = HT.endmill_exists(cb)
        out["shcs"] = [k for k, (c1, c2, cd) in HT.SHCS_INCH.items() if abs(cd - cb) < 0.002]
    return out


# ----------------------------------------------------------------------------------------- floors

def find_floors(part, hole_faces):
    """Pockets, steps, slots and annular grooves from planar floors (any direction, reachable from it)."""
    out = []
    used = set()
    for fi in part.faces:
        if fi["kind"] != "plane":
            continue
        key = _principal(fi["normal"])
        n = np.array(PRINCIPAL[key], float) if key else np.asarray(fi["normal"], float)
        if key is None:
            # oblique floors only when the face is a real facet-normal floor (not a 45-degree chamfer strip)
            if min(fi["uv"][1] - fi["uv"][0], fi["uv"][3] - fi["uv"][2]) < 0.15 * MM:
                continue
            key = "N(" + ",".join(f"{x:.3f}" for x in n) + ")"
        level = float(np.dot(fi["centroid"], n))
        top = part.top_along(n)
        if level > top - 0.05:
            continue  # outermost face in this direction
        if not part.open_along(part.inner_point(fi["id"]), n):
            continue  # not reachable from this direction (a wall seen from inside, a slot side)
        nbrs = part.outer_wire_neighbors(fi["id"])
        allnb = part.adj[fi["id"]]
        if nbrs and nbrs <= hole_faces:
            continue  # counterbore shoulder / hole bottom
        rising, falling = [], []
        for j in nbrs:
            lo, hi = part.extent_along(j, n)
            (rising if hi > level + 0.02 else falling).append(j)
        if not rising:
            continue  # a plateau, not a floor
        # walls: rising neighbors, and the walls behind floor fillets
        walls = set(rising)
        for j in rising:
            if part.faces[j]["kind"] in ("torus", "cylinder") and not _vertical_wall(part.faces[j], n):
                walls |= {k for k in part.adj[j] if k != fi["id"] and part.extent_along(k, n)[1] > level + 0.02}
        wall_top = max(part.extent_along(j, n)[1] for j in walls)
        rec = {"direction": key, "normal": n.tolist(), "floor_level": level, "floor_face": fi["id"],
               "depth": wall_top - level, "depth_to_outer_face": top - level,
               "open_sides": len(falling) > 0, "walls": sorted(walls)}
        # corner radii: vertical concave partial cylinders among the walls (and their neighbours)
        cand = set(walls)
        for j in list(walls):
            cand |= part.adj[j]
        radii = sorted({round(part.faces[j]["radius"], 4) for j in cand
                        if part.faces[j]["kind"] == "cylinder" and part.faces[j]["concave"] and j not in hole_faces
                        and abs(abs(np.dot(part.faces[j]["axis_dir"], n)) - 1) < 1e-6
                        and part.faces[j]["span"] < math.radians(300)})
        rec["corner_radii"] = radii
        rec["min_corner_radius"] = radii[0] if radii else None
        ff = sorted({round(part.faces[j]["minor"] if part.faces[j]["kind"] == "torus" else part.faces[j]["radius"], 4)
                     for j in allnb if part.faces[j]["kind"] in ("torus", "cylinder") and part.faces[j].get("concave")
                     and j not in hole_faces and not _vertical_wall(part.faces[j], n)})
        rec["floor_fillet"] = ff[0] if ff else None
        # width: closest pair of opposing planar walls facing each other; annulus: cylinder radii
        planes = [part.faces[j] for j in walls if part.faces[j]["kind"] == "plane"]
        widths = []
        for a in planes:
            for b in planes:
                if a["id"] < b["id"] and np.dot(a["normal"], b["normal"]) < -1 + 1e-6 and abs(np.dot(a["normal"], n)) < 1e-6:
                    gap = float(np.dot(a["normal"], b["centroid"] - a["centroid"]))
                    if gap > 0:
                        widths.append(gap)
        cyl_walls = [part.faces[j] for j in part.adj[fi["id"]] if part.faces[j]["kind"] == "cylinder"
                     and abs(abs(np.dot(part.faces[j]["axis_dir"], n)) - 1) < 1e-6 and j not in hole_faces]
        conc = [c for c in cyl_walls if c["concave"] and c["span"] >= math.radians(300)]
        conv = [c for c in cyl_walls if not c["concave"] and c["span"] >= math.radians(300)]
        kind = "pocket" if not rec["open_sides"] else "step"
        if conc and conv:
            ro, ri = max(c["radius"] for c in conc), max(c["radius"] for c in conv)
            if ro > ri:
                kind = "annular_groove"
                widths.append(ro - ri)
                rec["groove_id"] = 2 * ri
                rec["groove_od"] = 2 * ro
        elif conc and not planes:
            widths.append(2 * max(c["radius"] for c in conc))
            kind = "circular_pocket" if kind == "pocket" else kind
        # slot-end half cylinders among the walls
        halfs = [part.faces[j] for j in walls if part.faces[j]["kind"] == "cylinder" and part.faces[j]["concave"]
                 and math.radians(150) <= part.faces[j]["span"] <= math.radians(210)]
        if halfs:
            widths.append(2 * min(h["radius"] for h in halfs))
        rec["width"] = min(widths) if widths else None
        u0, u1, v0, v1 = fi["uv"]
        ext = sorted([u1 - u0, v1 - v0])
        rec["floor_extent"] = [ext[1], ext[0]]
        length = ext[1]
        if kind in ("pocket", "step") and rec["width"] and rec["width"] <= 1.2 * MM + TOL and length >= 2.0 * rec["width"]:
            kind = "slot" if kind == "pocket" else "open_slot"
        rec["kind"] = kind
        # size of a rectangular pocket: from wall planes along the two in-plane axes
        out.append(rec)
        used |= walls | {fi["id"]}
    return out, used


def _vertical_wall(fi, n):
    """Cylinder/torus whose axis is parallel to n (a wall), not a floor fillet."""
    if fi["kind"] == "cylinder":
        return abs(abs(np.dot(fi["axis_dir"], n)) - 1) < 1e-6
    return False


def find_through_slots(part, partial, used):
    """Slots with no floor: two half-cylinder ends of equal radius on parallel axes."""
    halfs = [p for p in partial if p["kind"] == "cyl" and math.radians(150) <= p["span"] <= math.radians(210)
             and not set(p["faces"]) & used]
    out, taken = [], set()
    for i, a in enumerate(halfs):
        for j, b in enumerate(halfs):
            if j <= i or i in taken or j in taken:
                continue
            if abs(a["r"] - b["r"]) > TOL or abs(abs(np.dot(a["c"], b["c"])) - 1) > 1e-6:
                continue
            dist = float(np.linalg.norm(np.cross(b["base"] - a["base"], a["c"])))
            if dist < TOL:
                continue
            key = _principal(a["c"]) or "oblique"
            lo, hi = a["lo"], a["hi"]
            through = abs((hi - lo) - float(np.dot(part.bmax - part.bmin, np.abs(a["c"])))) < 0.05
            out.append({"kind": "slot", "through": through, "axis": key.replace("-", "+"), "width": 2 * a["r"],
                        "length": dist + 2 * a["r"], "depth": hi - lo,
                        "centre": ((a["base"] + b["base"]) / 2 + a["c"] * (lo + hi) / 2).tolist(),
                        "faces": a["faces"] + b["faces"]})
            taken |= {i, j}
    return out


# ----------------------------------------------------------------------------------------- the rest

def find_outside(part, hole_faces):
    z = np.array([0, 0, 1.0])
    conv = [f for f in part.faces if f["kind"] == "cylinder" and not f["concave"] and f["id"] not in hole_faces]
    vert = [f for f in conv if abs(abs(np.dot(f["axis_dir"], z)) - 1) < 1e-6]
    full = [f for f in vert if f["span"] >= math.radians(300)]
    corner = sorted({round(f["radius"], 4) for f in vert if f["span"] < math.radians(300)})
    size = (part.bmax - part.bmin)
    shape = "rectangular"
    od = None
    if full:
        big = max(full, key=lambda f: f["radius"])
        if abs(2 * big["radius"] - max(size[0], size[1])) < 0.1:
            shape, od = "round", 2 * big["radius"]
    bosses = []
    for f in full:
        if od is not None and abs(2 * f["radius"] - od) < TOL:
            continue
        lo, hi = part.extent_along(f["id"], z)
        bosses.append({"diameter": 2 * f["radius"], "height": hi - lo, "top": hi,
                       "axis_point": f["axis_loc"].tolist()})
    return {"shape": shape, "size_xy": [float(size[0]), float(size[1])], "height": float(size[2]),
            "round_od": od, "vertical_corner_radii": corner}, bosses


def find_outer_walls(part):
    """Vertical planar faces reachable from outside along their normal: {normal, distance from the Z axis}."""
    out = []
    for f in part.faces:
        if f["kind"] != "plane" or abs(f["normal"][2]) > 1e-6:
            continue
        if not part.open_along(part.inner_point(f["id"]), f["normal"]):
            continue
        lo, hi = part.extent_along(f["id"], [0, 0, 1])
        out.append({"normal": [round(float(x), 4) for x in f["normal"]], "offset": float(np.dot(f["centroid"], f["normal"])),
                    "z_range": [lo, hi], "area": f["area"]})
    return out


def find_fillets_chamfers(part, hole_faces):
    fillets, chamfers = [], []
    for f in part.faces:
        if f["id"] in hole_faces:
            continue
        if f["kind"] == "torus":
            fillets.append({"radius": f["minor"], "concave": bool(f["concave"]), "face": f["id"], "type": "torus"})
        elif f["kind"] == "cylinder" and f["span"] < math.radians(120):
            vert = abs(abs(f["axis_dir"][2]) - 1) < 1e-6
            if not vert:
                fillets.append({"radius": f["radius"], "concave": bool(f["concave"]), "face": f["id"], "type": "edge"})
        elif f["kind"] == "plane" and _principal(f["normal"]) is None:
            n = np.abs(f["normal"])
            s = sorted(n)
            if abs(s[-1] - s[-2]) < 0.02 and s[0] < 0.02:  # 45 degrees between two principal axes
                u0, u1, v0, v1 = f["uv"]
                w = min(u1 - u0, v1 - v0)
                chamfers.append({"size": w / math.sqrt(2), "angle": 45.0, "face": f["id"], "on": "edge"})
        elif f["kind"] == "cone" and not f["concave"]:
            r0, r1 = f["radii"]
            a0, a1 = f["ax_range"]
            chamfers.append({"size": abs(a1 - a0), "angle": round(f["semi_angle"], 1), "face": f["id"], "on": "convex_cone"})
        elif f["kind"] == "cone" and f["concave"]:
            a0, a1 = f["ax_range"]
            chamfers.append({"size": abs(a1 - a0), "angle": round(f["semi_angle"], 1), "face": f["id"], "on": "concave_cone"})
    return fillets, chamfers


def find_thin(part, limit_mm):
    out = []
    planes = [f for f in part.faces if f["kind"] == "plane" and _principal(f["normal"])]
    for i, a in enumerate(planes):
        for b in planes[i + 1:]:
            if np.dot(a["normal"], b["normal"]) > -1 + 1e-6:
                continue
            t = -float(np.dot(a["normal"], b["centroid"] - a["centroid"]))
            if not (TOL < t < limit_mm):
                continue
            ax = int(np.argmax(np.abs(a["normal"])))
            others = [k for k in range(3) if k != ax]
            lo = np.maximum(a["bmin"], b["bmin"])
            hi = np.minimum(a["bmax"], b["bmax"])
            if any(hi[k] - lo[k] <= TOL for k in others):
                continue
            mid = (lo + hi) / 2
            mid[ax] = (float(np.dot(a["centroid"], np.abs(a["normal"]))) + float(np.dot(b["centroid"], np.abs(b["normal"])))) / 2
            if part.classify(mid) != "in":
                continue
            kind = "floor" if ax == 2 else "wall"
            span = {k: float(hi[k] - lo[k]) for k in others}
            rec = {"kind": kind, "thickness": t, "normal_axis": "XYZ"[ax], "faces": [a["id"], b["id"]],
                   "centre": mid.tolist()}
            if kind == "wall":
                rec["height"] = span[2]
                rec["length"] = span[[k for k in others if k != 2][0]]
            else:
                rec["extent"] = [span[others[0]], span[others[1]]]
            out.append(rec)
    return out


def find_partial_bores(part, partial, used, corner_faces):
    """Concave partial cylinders grouped by axis line (grip bores, reliefs, notches, open arcs)."""
    groups = []
    for e in partial:
        if e["kind"] != "cyl" or set(e["faces"]) & (used | corner_faces):
            continue
        for g in groups:
            if abs(g["r"] - e["r"]) < TOL and abs(abs(np.dot(g["c"], e["c"])) - 1) < 1e-6 and \
                    np.linalg.norm(np.cross(e["base"] - g["base"], g["c"])) < TOL:
                g["els"].append(e)
                break
        else:
            groups.append({"r": e["r"], "c": e["c"], "base": e["base"], "els": [e]})
    out = []
    for g in groups:
        span = max(x["span"] for x in g["els"])
        lo, hi = min(x["lo"] for x in g["els"]), max(x["hi"] for x in g["els"])
        if span <= math.radians(95) and g["r"] <= 0.25 * MM and len(g["els"]) == 1:
            continue  # a blend fillet, reported under fillets
        out.append({"radius": g["r"], "diameter": 2 * g["r"], "max_span_deg": math.degrees(span),
                    "axis": _principal(g["c"]) or g["c"].tolist(), "axis_point": g["base"].tolist(),
                    "length": hi - lo, "faces": [f for x in g["els"] for f in x["faces"]]})
    return out


def find_v_grooves(part, hole_faces):
    """Annular V grooves: coaxial concave cones outside holes, paired flank to flank."""
    cones = [f for f in part.faces if f["kind"] == "cone" and f["concave"] and f["id"] not in hole_faces]
    groups = []
    for f in cones:
        c = _canon(f["axis_dir"])
        base = f["axis_loc"] - c * np.dot(f["axis_loc"], c)
        for g in groups:
            if abs(np.dot(g["c"], c)) > 1 - 1e-6 and np.linalg.norm(np.cross(base - g["base"], g["c"])) < TOL \
                    and abs(g["semi"] - f["semi_angle"]) < 0.5:
                g["faces"].append(f)
                break
        else:
            groups.append({"c": c, "base": base, "semi": f["semi_angle"], "faces": [f]})
    out = []
    for g in groups:
        if len(g["faces"]) < 2:
            continue
        up = sum(1 for f in g["faces"] if np.dot(f["axis_dir"], g["c"]) > 0)
        down = len(g["faces"]) - up
        out.append({"kind": "annular_v_grooves", "axis": _principal(g["c"]) or g["c"].tolist(),
                    "axis_point": g["base"].tolist(), "included_angle": round(2 * (90 - g["semi"]), 1),
                    "grooves": min(up, down), "flanks": len(g["faces"])})
    return out


def find_serrations(part):
    """Sets of parallel inclined planar faces (V serrations): mirrored normals, >= 4 faces each."""
    inc = [f for f in part.faces if f["kind"] == "plane" and _principal(f["normal"]) is None]
    groups = []
    for f in inc:
        for g in groups:
            if np.dot(g["n"], f["normal"]) > 1 - 1e-4:
                g["faces"].append(f)
                break
        else:
            groups.append({"n": f["normal"], "faces": [f]})
    big = [g for g in groups if len(g["faces"]) >= 4]
    out, taken = [], set()
    for i, a in enumerate(big):
        for j, b in enumerate(big):
            if j <= i or i in taken or j in taken:
                continue
            ang = math.degrees(math.acos(max(-1, min(1, float(np.dot(a["n"], b["n"]))))))
            if ang < 20:
                continue
            axis = a["n"] - b["n"]
            axis /= np.linalg.norm(axis)
            offs = sorted({round(float(np.dot(a["n"], f["centroid"])), 3) for f in a["faces"]})
            pos = sorted({round(float(np.dot(axis, f["centroid"])), 2) for f in a["faces"]})
            pitch = float(np.median(np.diff(offs))) / abs(float(np.dot(a["n"], axis))) if len(offs) > 1 else None
            out.append({"kind": "serrations", "included_angle": round(180 - ang, 1), "pitch": pitch,
                        "across": _principal(axis) or axis.tolist(), "grooves": len(offs),
                        "faces": len(a["faces"]) + len(b["faces"]), "segments_per_side": len(a["faces"])})
            taken |= {i, j}
    return out


def find_gaps(part, limit_mm):
    """Slits and narrow slots: opposing planar faces facing each other with air between, closer than limit."""
    out = []
    planes = [f for f in part.faces if f["kind"] == "plane"]
    for i, a in enumerate(planes):
        for b in planes[i + 1:]:
            if np.dot(a["normal"], b["normal"]) > -1 + 1e-6:
                continue
            g = float(np.dot(a["normal"], b["centroid"] - a["centroid"]))
            if not (TOL < g < limit_mm):
                continue
            pa = part.inner_point(a["id"])
            if part.classify(pa + a["normal"] * g / 2) == "in":
                continue
            # the faces must overlap when projected along the normal
            na = part.face_nodes(a["id"]); nb = part.face_nodes(b["id"])
            n = a["normal"]
            u = np.cross(n, [0, 0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0, 0])
            u /= np.linalg.norm(u)
            w = np.cross(n, u)
            ov = [min((na @ ax).max(), (nb @ ax).max()) - max((na @ ax).min(), (nb @ ax).min()) for ax in (u, w)]
            if min(ov) <= TOL:
                continue
            out.append({"gap": g, "normal": (_principal(n) or n.round(4).tolist()), "overlap": [float(x) for x in ov],
                        "faces": [a["id"], b["id"]]})
    return out


def design_units(values_mm) -> str:
    vals = [v for v in values_mm if v > 0.5]
    if not vals:
        return "mm"
    def inch_round(v):
        t = v / MM * 1000
        return abs(t - round(t / 5) * 5) < 0.35 or abs(v / MM * 64 - round(v / MM * 64)) < 0.02
    def mm_round(v):
        return abs(v * 2 - round(v * 2)) < 0.02
    si = sum(inch_round(v) for v in vals)
    sm = sum(mm_round(v) for v in vals)
    return "inch" if si > sm else "mm"


# ----------------------------------------------------------------------------------------- driver

LEN_KEYS = {"at", "gap", "offset", "pitch", "diameter", "depth", "depth_total", "d", "from", "to", "d_near", "d_far", "size", "floor_level", "depth_to_outer_face",
            "min_corner_radius", "floor_fillet", "width", "radius", "length", "thickness", "height", "top", "round_od",
            "min_wall_pierced", "min_wall_around", "groove_id", "groove_od"}
LIST_LEN_KEYS = {"entry_point", "exit_point", "corner_radii", "pierced_runs", "floor_extent", "size_xy",
                 "vertical_corner_radii", "centre", "axis_point", "z_range", "overlap", "extent", "min", "max", "size", "pre_squared", "saw_cut"}


def _scale(obj, f, key=None):
    if isinstance(obj, dict):
        return {k: _scale(v, f, k) for k, v in obj.items() if not k.startswith("_") and k != "face"}
    if isinstance(obj, list):
        if key in LIST_LEN_KEYS:
            return [_scale(v, f, key) if isinstance(v, (list, dict)) else (round(v * f, 5) if isinstance(v, (int, float)) and not isinstance(v, bool) else v) for v in obj]
        return [_scale(v, f, key) for v in obj]
    if isinstance(obj, (float, np.floating)):
        return round(float(obj) * f, 5) if key in LEN_KEYS else round(float(obj), 6)
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


def analyze(path, units: str | None = None, thin_in: float = 0.10) -> dict:
    part = Part(path)
    holes, partial = find_holes(part)
    _hole_relations(part, holes)
    _hole_walls(part, holes)
    hole_faces = {f for h in holes for f in h["faces"]}
    floors, used = find_floors(part, hole_faces)
    slots = find_through_slots(part, partial, used | hole_faces)
    outside, bosses = find_outside(part, hole_faces)
    outside["outer_walls"] = find_outer_walls(part)
    fillets, chamfers = find_fillets_chamfers(part, hole_faces)
    gaps = find_gaps(part, 0.30 * MM + TOL)
    webs = find_thin(part, 0.30 * MM + TOL)
    thin = [w for w in webs if w["thickness"] <= thin_in * MM + TOL]
    corner_faces = set()
    for fl in floors:
        for j in part.faces:
            pass
    partial_bores = find_partial_bores(part, partial, used | hole_faces | {f for s_ in slots for f in s_["faces"]}, set())
    pb_faces = {f for pb in partial_bores for f in pb["faces"]}
    fillets = [x for x in fillets if x["face"] not in pb_faces]
    v_grooves = find_v_grooves(part, hole_faces)
    vg_faces = set()
    chamfers = [x for x in chamfers if not (x["on"] == "concave_cone" and any(
        abs(x["angle"] - (90 - g["included_angle"] / 2)) < 0.5 for g in v_grooves))]
    serr = find_serrations(part)
    dims = [h["diameter"] for h in holes] + [h["depth"] for h in holes] + list(part.bmax - part.bmin)
    dims += [f["depth"] for f in floors]
    du = design_units(dims)
    out_units = units or du
    f = 1 / MM if out_units == "inch" else 1.0

    for i, h in enumerate(holes):
        h["id"] = f"H{i + 1:02d}"
        h["standard"] = _hole_names(h, out_units)
        for s in h["stack"]:
            s.pop("faces", None)
    for h in holes:
        h["intersects"] = [holes[j]["id"] for j in h["intersects"]]
        h["breaks_into"] = [holes[j]["id"] for j in h["breaks_into"]]
        h["entry_face"] = _principal(np.array(h["entry_dir"])) or "oblique"
    pockets = [x for x in floors if x["kind"] in ("pocket", "circular_pocket")]
    steps = [x for x in floors if x["kind"] == "step"]
    slot_list = [x for x in floors if x["kind"] in ("slot", "open_slot", "annular_groove")] + slots
    for pre, lst in (("P", pockets), ("ST", steps), ("SL", slot_list), ("FI", fillets), ("CH", chamfers),
                     ("TW", thin), ("WB", webs), ("GP", gaps), ("B", bosses), ("PB", partial_bores), ("VG", v_grooves), ("SR", serr)):
        for i, x in enumerate(lst):
            x["id"] = f"{pre}{i + 1:02d}"
            x.pop("walls", None)
    size = part.bmax - part.bmin
    size_in = size / MM
    def up(v, step):
        return math.ceil(v / step - 1e-9) * step
    stock = {"pre_squared_in": [round(size_in[0], 4), round(size_in[1], 4), round(size_in[2] + 0.030, 4)],
             "saw_cut_in": [up(size_in[0] + 0.25, 0.125), up(size_in[1] + 0.25, 0.125), up(size_in[2] + 0.2, 0.125)],
             "note": "pre-squared: finish XY, +.030 top to face; saw-cut: +.125/side XY, +.100 top and bottom, "
                     "rounded up to 1/8 [judgment; process-planning.md]"}
    if outside["shape"] == "round":
        stock["round_bar_in"] = up(outside["round_od"] / MM + 0.125, 0.125)
    res = {
        "schema": "helios.cam.features/1",
        "source": str(path),
        "step_file_unit": step_file_unit(path),
        "kernel_unit": "mm",
        "design_units_inferred": du,
        "units": out_units,
        "solids_in_file": part.n_solids,
        "bounding_box": {"min": part.bmin.tolist(), "max": part.bmax.tolist(), "size": size.tolist()},
        "stock_suggestion": stock,
        "face_count": len(part.faces),
        "face_kinds": {k: sum(1 for x in part.faces if x["kind"] == k) for k in ("plane", "cylinder", "cone", "torus", "other")},
        "holes": holes,
        "pockets": pockets,
        "steps": steps,
        "slots": slot_list,
        "outside_profile": outside,
        "bosses": bosses,
        "fillets": fillets,
        "chamfers": chamfers,
        "thin_walls": thin,
        "webs": webs,
        "gaps": gaps,
        "partial_bores": partial_bores,
        "v_grooves": v_grooves,
        "serrations": serr,
        "thin_limit": thin_in * (1 if out_units == "inch" else MM),
    }
    out = _scale(res, f)
    out["thin_limit"] = thin_in * (1 if out_units == "inch" else MM)
    out["stock_suggestion"] = stock
    return out


# ----------------------------------------------------------------------------------------- recall vs spec

NUM = r"(\d*\.\d+|\d+(?:\.\d+)?)"


def _nums(s):
    return [float(x) for x in re.findall(r"(?<![\w/])" + NUM + r"(?![\w/])", s)]


def _dia_values(s):
    return [float(x) for x in re.findall(r"Ø\s*" + NUM, s)]


def _to_inch_text(text: str, metric: bool) -> str:
    """Spec text in inches: metric specs drop the [inch] brackets and scale every number by 1/25.4."""
    if not metric:
        return text
    t = re.sub(r"\[[^\]]*\]", "", text)
    # scale numbers, including after R and Ø, but not thread sizes (M6) or other letter-prefixed tokens
    return re.sub(r"(?<![0-9.A-QS-Za-z_])(\d*\.\d+|\d+)(?![\d])", lambda m: f"{float(m.group(1)) / MM:.4f}", t)


def _expected_hole_dia(t, kd):
    m = re.search(r"tap drill [^(]*\((\d*\.\d+)\)", kd) or re.search(r"drill [#\w/]+ \((\d*\.\d+)\)", kd)
    if m:
        return float(m.group(1))
    th = HT.parse_thread(kd) if t == "tapped_hole" else None
    if th:
        td = HT.tap_drill_for(th)
        if td:
            return td[1]
    ds = _dia_values(kd)
    return ds[0] if ds else None


def _face_dir(loc):
    loc = loc or ""
    for key, pat in (("+X", r"\+X (end )?face"), ("-X", r"-X (end )?face"), ("+Y", r"\+Y face"), ("-Y", r"-Y face"),
                     ("+Z", r"top face"), ("-Z", r"bottom face")):
        if re.search(pat, loc):
            return key
    return None


def _close(a, b, tol=0.005):
    return a is not None and b is not None and abs(a - b) <= tol


def compare_with_spec(feat: dict, spec: dict) -> dict:
    """Instance-level recall of spec.json features (answer key) by the recognised features (inch).

    Matching is by size and access direction, not by location: a spec feature is found when enough recognised
    features of the right kind and size exist."""
    assert feat["units"] == "inch", "compare in inch"
    metric = "mm" in str(spec.get("units", {}).get("design", "")).lower()
    rows = []
    holes = feat["holes"]
    floors = feat["pockets"] + feat["steps"] + feat["slots"]
    for sf in spec["features"]:
        t, qty = sf["type"], int(sf.get("qty", 1))
        kd = _to_inch_text(sf["key_dims"], metric)
        loc = sf.get("location", "")
        found, ids, note = 0, [], ""
        if t == "face":
            rows.append({"feature_id": sf["feature_id"], "type": t, "expected": 0, "found": 0, "ids": [],
                         "note": "process feature, not counted"})
            continue
        hole_like = t in ("through_hole", "blind_hole", "tapped_hole", "counterbore", "countersink", "window")
        circ_pocket = t == "pocket" and re.match(r"\s*Ø", kd)
        if hole_like or circ_pocket:
            d = _expected_hole_dia(t, kd)
            dset = ([d] if d else []) + _dia_values(kd)
            fdir = _face_dir(loc)
            if t == "counterbore":
                cands = [h for h in holes if any(_close(s["d"], x, 0.003) for s in h["stack"] if s["type"] == "cyl"
                                                  for x in dset) and (len([s for s in h["stack"] if s["type"] == "cyl"]) >= 2
                                                                     or "flat_bottom" in h["tags"])]
            elif t == "countersink":
                cands = [h for h in holes if "countersink" in h["tags"] and any(_close(h["diameter"], x, 0.003) for x in dset)]
            else:
                cands = [h for h in holes if any(_close(s["d"], x, 0.003) for s in h["stack"] if s["type"] == "cyl"
                                                  for x in dset)]
            if fdir:
                cands = [h for h in cands if fdir in [(_principal(np.array(a)) or "") for a in h["access_dirs"]]]
            # a bore through two ears across a gap is one recognised hole with two pierced walls
            n = sum(max(1, len(h.get("pierced_runs") or [])) if h["kind"] == "through" and t in ("through_hole", "window")
                    else 1 for h in cands)
            found, ids = min(qty, n), [h["id"] for h in cands]
            note = f"expected Ø{d:.4f}" if d else "no diameter parsed"
        elif t == "pocket":
            m = re.search(NUM + r"\s*deep", kd)
            depth = float(m.group(1)) if m else None
            rm = re.search(r"R\s*" + NUM, kd)
            rc = float(rm.group(1)) if rm else None
            cands = [p for p in floors if (depth is None or _close(p["depth"], depth, 0.01)
                                           or _close(p.get("depth_to_outer_face"), depth, 0.01))
                     and (rc is None or (p.get("min_corner_radius") is not None and _close(p["min_corner_radius"], rc))
                          or _close(p.get("width"), 2 * rc if rc else None))]
            found, ids = min(qty, len(cands)), [p["id"] for p in cands]
        elif t == "slot":
            m = re.search(NUM + r"\s*wide", kd) or re.search(r"width\s*" + NUM, kd)
            w = float(m.group(1)) if m else None
            rs = [float(x) for x in re.findall(r"R\s*" + NUM, kd)] + [d / 2 for d in _dia_values(kd)]
            cands = [x for x in floors if w and _close(x.get("width"), w)]
            gp = [x for x in feat["gaps"] if w and _close(x["gap"], w)]
            grooves = [h for h in holes for g in h.get("internal_grooves", []) if w and _close(g["width"], w, 0.003)]
            if cands:
                found = min(qty, len(cands))
            elif grooves:
                cands = grooves
                found = min(qty, sum(len(h["internal_grooves"]) for h in {h["id"]: h for h in grooves}.values()))
            elif gp:
                cands = gp
                found = min(qty, len(gp))
            elif re.search(r"serration", kd):
                cands = feat["serrations"]
                found = min(qty, sum(x["segments_per_side"] for x in cands))
            elif re.search(r"groove", kd) and feat["v_grooves"]:
                cands = feat["v_grooves"]
                found = min(qty, sum(x["grooves"] for x in cands))
            elif rs:
                cands = [x for x in feat["partial_bores"] if any(_close(x["radius"], r, 0.003) for r in rs)]
                found = min(qty, len({tuple(np.round(x["axis_point"], 3)) for x in cands}))
            ids = [x["id"] for x in cands]
        elif t == "step":
            if "profile" in kd:
                found, ids = _match_profile(feat, kd)
            else:
                vals = [float(x) for x in re.findall(NUM + r"\s*(?:tall|deep)", kd)] or _nums(kd)
                cands = [x for x in floors if x["kind"] in ("step", "open_slot") and any(
                    _close(x["depth"], v) or _close(abs(x["floor_level"]), v) or _close(x.get("width"), v) for v in vals)]
                found, ids = min(qty, len(cands)), [x["id"] for x in cands]
        elif t == "profile":
            found, ids = _match_profile(feat, kd)
        elif t in ("fin", "rib"):
            m = re.search(NUM + r"\s*thick", kd)
            th = float(m.group(1)) if m else None
            cands = [w for w in feat["webs"] if _close(w["thickness"], th)]
            found, ids = min(qty, len(cands)), [w["id"] for w in cands]
        elif t == "chamfer":
            m = re.search(NUM + r"\s*x\s*45", kd)
            c = float(m.group(1)) if m else None
            cands = [x for x in feat["chamfers"] if _close(x["size"], c, 0.004)]
            cands += [h for h in holes if c and "entry_chamfer" in h["tags"] and _close(h["entry_chamfer"]["size"], c, 0.004)]
            found, ids = (1 if cands else 0), [x["id"] for x in cands][:12]
        elif t == "fillet":
            m = re.search(r"R\s*" + NUM, kd)
            r = float(m.group(1)) if m else None
            cands = [x for x in feat["fillets"] if _close(x["radius"], r, 0.003)]
            found, ids = (1 if cands else 0), [x["id"] for x in cands]
        elif t == "boss":
            ds = _dia_values(kd)
            cands = [b for b in feat["bosses"] if ds and _close(b["diameter"], ds[0])]
            if not cands and ds and _close(feat["outside_profile"].get("round_od"), ds[0]):
                cands = [{"id": "outside_profile"}]
            found, ids = (1 if cands else 0), [b["id"] for b in cands]
        expected = qty if t not in ("chamfer", "fillet", "boss", "profile") else 1
        rows.append({"feature_id": sf["feature_id"], "type": t, "expected": expected, "found": min(found, expected),
                     "ids": ids, "note": note})
    exp = sum(r["expected"] for r in rows)
    got = sum(r["found"] for r in rows)
    return {"recall": round(got / exp, 3) if exp else None, "expected": exp, "found": got, "rows": rows}


def _match_profile(feat, kd):
    op = feat["outside_profile"]
    ds = _dia_values(kd)
    if ds and "A/F" not in kd:
        ok = op["round_od"] is not None and _close(op["round_od"], ds[0])
        return (1 if ok else 0), (["outside_profile"] if ok else [])
    walls = op.get("outer_walls", [])
    # across-flats distances of opposing outer walls
    af = []
    for i, a in enumerate(walls):
        for b in walls[i + 1:]:
            if np.dot(a["normal"], b["normal"]) < -0.999:
                af.append(a["offset"] + b["offset"])
    if "octagon" in kd:
        m = re.search(NUM + r"\s*A/F", kd)
        v = float(m.group(1)) if m else None
        normals = {tuple(np.round(w["normal"], 2)) for w in walls if v and any(_close(x, v) for x in af)}
        ok = v is not None and sum(1 for x in af if _close(x, v)) >= 4
        return (1 if ok else 0), (["outer_walls"] if ok else [])
    xy = re.search(NUM + r"\s*(?:\(X\)\s*)?x\s*" + NUM, kd)
    if xy:
        vals = [float(xy.group(1)), float(xy.group(2))]
        size = feat["bounding_box"]["size"]
        ok = all(any(_close(x, v) for x in af + list(size)) for v in vals)
        if ok and (rm := re.search(r"corners R\s*" + NUM, kd)):
            ok = any(_close(r, float(rm.group(1))) for r in op["vertical_corner_radii"])
        return (1 if ok else 0), (["outside_profile"] if ok else [])
    return 0, []


def recall_table(parts_dir) -> dict:
    out = {}
    for d in sorted(Path(parts_dir).glob("*/")):
        step, spec = d / "part.step", d / "spec.json"
        if not step.exists() or not spec.exists():
            continue
        feat = analyze(step, units="inch")
        out[d.name] = compare_with_spec(feat, json.loads(spec.read_text()))
    tot_e = sum(v["expected"] for v in out.values())
    tot_f = sum(v["found"] for v in out.values())
    return {"parts": out, "total": {"expected": tot_e, "found": tot_f, "recall": round(tot_f / tot_e, 3) if tot_e else None}}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Feature recognition from a STEP file (JSON on stdout).")
    ap.add_argument("step", nargs="?", help="part.step")
    ap.add_argument("--units", choices=["inch", "mm"], help="output units (default: inferred design units)")
    ap.add_argument("--thin", type=float, default=0.10, help="thin wall limit, inch (default .10)")
    ap.add_argument("--spec", help="spec.json: add recall against its feature list")
    ap.add_argument("--recall", metavar="PARTS_DIR", help="recall table for PARTS_DIR/*/part.step vs spec.json")
    a = ap.parse_args(argv)
    if a.recall:
        tab = recall_table(a.recall)
        for k, v in tab["parts"].items():
            miss = [f"{r['feature_id']} {r['type']} {r['found']}/{r['expected']}" for r in v["rows"] if r["found"] < r["expected"]]
            print(f"{k:28s} recall {v['recall']:.2f} ({v['found']}/{v['expected']})" + (f"  missed: {'; '.join(miss)}" if miss else ""),
                  file=sys.stderr)
        print(f"TOTAL recall {tab['total']['recall']} ({tab['total']['found']}/{tab['total']['expected']})", file=sys.stderr)
        json.dump(tab, sys.stdout, indent=1)
        return 0
    if not a.step:
        ap.error("give a STEP file or --recall")
    res = analyze(a.step, a.units, a.thin)
    if a.spec:
        spec = json.loads(Path(a.spec).read_text())
        res["spec_recall"] = compare_with_spec(res if res["units"] == "inch" else analyze(a.step, "inch", a.thin), spec)
    json.dump(res, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
