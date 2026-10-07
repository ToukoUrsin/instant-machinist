"""Load the part as a triangle mesh (STEP via OCP/CadQuery, STL/OBJ/PLY via trimesh)."""
from __future__ import annotations

from pathlib import Path

import numpy as np


def load_triangles(path: Path | str, tolerance_mm: float = 0.01, angular_tol: float = 0.1) -> np.ndarray:
    """Return (M, 3, 3) float64 triangles in the file's native units (STEP -> millimetres)."""
    path = Path(path)
    suf = path.suffix.lower()
    if suf in (".step", ".stp"):
        return _step_triangles(path, tolerance_mm, angular_tol)
    import trimesh
    mesh = trimesh.load(path, force="mesh")
    return np.asarray(mesh.triangles, dtype=np.float64)


def _step_triangles(path: Path, tol: float, ang: float) -> np.ndarray:
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"cannot read STEP {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    BRepMesh_IncrementalMesh(shape, tol, False, ang, True)
    out = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            nodes = np.array([[p.X(), p.Y(), p.Z()] for p in
                              (tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1))])
            idx = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)]) - 1
            if face.Orientation() == TopAbs_REVERSED:
                idx = idx[:, ::-1]
            out.append(nodes[idx])
        exp.Next()
    if not out:
        raise RuntimeError(f"no faces in {path}")
    return np.concatenate(out).astype(np.float64)
