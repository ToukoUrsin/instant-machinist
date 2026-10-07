"""stock_sim: 3-axis Z-map material removal of the posted G-code, compared with the part.

Per setup: stock box -> height field in the setup WCS; every move swept with its tool's shape;
part mesh rasterized top-down in the same WCS; gouge / leftover maps; rapid-through-stock,
holder collision and depth-beyond-flute detection per move.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
from scipy import ndimage

from . import zmap
from .gcode import arc_points
from .job import Job, Setup, unit_factor
from .part_model import load_triangles

_MESH_CACHE: dict = {}


def part_triangles(job: Job) -> np.ndarray | None:
    if job.part_path is None:
        return None
    key = (str(job.part_path), job.sim["mesh_tolerance"], job.units)
    if key not in _MESH_CACHE:
        tol_native = job.sim["mesh_tolerance"] * unit_factor(job.units, job.part_units)
        tris = load_triangles(job.part_path, tolerance_mm=tol_native)
        _MESH_CACHE[key] = tris * unit_factor(job.part_units, job.units)
    return _MESH_CACHE[key]


def build_segments(moves, tools, arc_tol):
    """Flatten moves into tool-tip segments. Returns arrays + mapping to (file, line)."""
    tool_ids = sorted(tools)
    tindex = {t: i for i, t in enumerate(tool_ids)}
    segs, stool, srapid, smove = [], [], [], []
    for mi, m in enumerate(moves):
        if m.source == "unknown_xy" or any(math.isnan(v) for v in m.start + m.end):
            continue
        pts = [m.start] + (arc_points(m, arc_tol) if m.kind == "arc" else [m.end])
        ti = tindex.get(m.tool, -1)
        for a, b in zip(pts, pts[1:]):
            if a == b:
                continue
            segs.append((*a, *b))
            stool.append(ti)
            srapid.append(1 if m.kind == "rapid" else 0)
            smove.append(mi)
    segs = np.asarray(segs, dtype=np.float64).reshape(-1, 6)
    return segs, np.asarray(stool, np.int64), np.asarray(srapid, np.int64), np.asarray(smove, np.int64), tool_ids


def tool_arrays(tools, tool_ids, drill_model):
    """Kernel arrays: kind, R, a, b, flute, cyl_off (n, K), cyl_r (n, K)."""
    from .job import HOLDER_CYLINDERS
    n = len(tool_ids)
    kind = np.zeros(n, np.int64)
    R, a, b, flute = (np.zeros(n) for _ in range(4))
    cyl_off = np.zeros((n, HOLDER_CYLINDERS))
    cyl_r = np.zeros((n, HOLDER_CYLINDERS))
    for i, t in enumerate(tool_ids):
        tl = tools[t]
        k, pa, pb = tl.profile(drill_model)
        kind[i] = k
        R[i] = tl.sim_radius
        a[i] = pa if k != 1 else tl.radius
        b[i] = pb
        flute[i] = tl.sim_flute_length
        for j, (off, r, _) in enumerate(tl.assembly()[:HOLDER_CYLINDERS]):
            cyl_off[i, j] = off
            cyl_r[i, j] = r
    return kind, R, a, b, flute, cyl_off, cyl_r


def _regions(mask, values, grid, top=5):
    lab, n = ndimage.label(mask)
    if n == 0:
        return []
    idx = np.arange(1, n + 1)
    cnt = ndimage.sum(np.ones_like(values), lab, idx)
    mx = ndimage.maximum(values, lab, idx)
    pos = ndimage.maximum_position(values, lab, idx)
    order = np.argsort(-np.asarray(mx))[:top]
    out = []
    for k in order:
        iy, ix = pos[k]
        x, y = grid.xy(iy, ix)
        out.append({"max": round(float(mx[k]), 5), "area": round(float(cnt[k]) * grid.res ** 2, 5),
                    "cells": int(cnt[k]), "worst_xy": [round(float(x), 4), round(float(y), 4)]})
    return out


def inprocess_heights(setup: Setup, grid: zmap.Grid):
    """(top H, underside far) height fields in the setup WCS of the in-process stock mesh the job provides
    (``setup.inprocess_stock``: npz with vertices in part coords / job units and faces), or None."""
    p = getattr(setup, "inprocess_stock", None)
    if p is None or not Path(p).exists():
        return None
    d = np.load(p)
    V = setup.to_wcs(np.asarray(d["vertices"], float))
    tris = np.ascontiguousarray(V[np.asarray(d["faces"], np.int64)])
    top = grid.empty()
    zmap.raster_top(top, grid.x0, grid.y0, grid.res, tris)
    flipped = tris.copy()
    flipped[..., 2] *= -1
    bot = grid.empty()
    zmap.raster_top(bot, grid.x0, grid.y0, grid.res, np.ascontiguousarray(flipped))
    has = top > zmap.AIR * 0.5
    H = np.where(has, top, zmap.AIR)
    far = np.where(has, -bot, np.inf)
    return H, far


def z_sign(setup: Setup):
    """+1 / -1 if the setup's tool axis is the part's +Z / -Z (top / flipped), else None (tilted)."""
    Rm = setup.matrix[:3, :3]
    if abs(Rm[2, 0]) < 1e-9 and abs(Rm[2, 1]) < 1e-9 and abs(abs(Rm[2, 2]) - 1) < 1e-9:
        return 1 if Rm[2, 2] > 0 else -1
    return None


class ColumnStock:
    """In-process stock carried between setups: one material interval [bot, top] per vertical column
    of the part frame. Exact for setups machining from +Z and -Z of the part (top and flip); tilted
    (3+2) setups start from the raw stock box and do not update it."""

    def __init__(self, job: Job, res: float):
        lo, hi = job.stock_min, job.stock_max
        m = job.sim["margin"]
        self.grid = zmap.Grid(lo[0] - m, lo[1] - m, hi[0] + m, hi[1] + m, res)
        xs, ys = self.grid.centers()
        inside = (xs[None, :] >= lo[0]) & (xs[None, :] <= hi[0]) & (ys[:, None] >= lo[1]) & (ys[:, None] <= hi[1])
        self.top = np.where(inside, hi[2], -np.inf)
        self.bot = np.where(inside, lo[2], np.inf)
        self.setups_done = 0

    def _sample(self, arr, g: zmap.Grid, x, y, fill):
        ix = np.floor((x - g.x0) / g.res).astype(np.int64)
        iy = np.floor((y - g.y0) / g.res).astype(np.int64)
        ok = (ix >= 0) & (ix < g.nx) & (iy >= 0) & (iy < g.ny)
        out = np.full(x.shape, fill, dtype=float)
        out[ok] = arr[iy[ok], ix[ok]]
        return out

    def initial(self, setup: Setup, grid: zmap.Grid) -> np.ndarray:
        s = z_sign(setup)
        Rm, t = setup.matrix[:3, :3], setup.matrix[:3, 3]
        xs, ys = grid.centers()
        X, Y = np.meshgrid(xs, ys)
        w = np.stack([X - t[0], Y - t[1], np.zeros_like(X)], axis=-1)
        p = w @ Rm  # R^T (w - t), z column ignored
        top = self._sample(self.top, self.grid, p[..., 0], p[..., 1], -np.inf)
        bot = self._sample(self.bot, self.grid, p[..., 0], p[..., 1], np.inf)
        empty = ~(top > bot)
        face = top if s > 0 else bot
        H = s * face + t[2]
        return np.where(empty, zmap.AIR, H)

    def far_end(self, setup: Setup, grid: zmap.Grid) -> np.ndarray:
        """WCS Z of the material's far (underside) end per cell; material exists between it and H."""
        s = z_sign(setup)
        Rm, t = setup.matrix[:3, :3], setup.matrix[:3, 3]
        xs, ys = grid.centers()
        X, Y = np.meshgrid(xs, ys)
        p = np.stack([X - t[0], Y - t[1], np.zeros_like(X)], axis=-1) @ Rm
        top = self._sample(self.top, self.grid, p[..., 0], p[..., 1], -np.inf)
        bot = self._sample(self.bot, self.grid, p[..., 0], p[..., 1], np.inf)
        far = bot if s > 0 else top
        with np.errstate(invalid="ignore"):
            out = s * far + t[2]
        return np.where(top > bot, out, np.inf)

    def update(self, setup: Setup, grid: zmap.Grid, H: np.ndarray):
        s = z_sign(setup)
        Rm, t = setup.matrix[:3, :3], setup.matrix[:3, 3]
        xs, ys = self.grid.centers()
        X, Y = np.meshgrid(xs, ys)
        w = np.stack([X, Y, np.zeros_like(X)], axis=-1) @ Rm.T + t
        h = self._sample(H, grid, w[..., 0], w[..., 1], zmap.AIR)
        valid = h > zmap.AIR * 0.5
        zp = (h - t[2]) / s
        if s > 0:
            self.top = np.where(valid, np.minimum(self.top, zp), self.top)
        else:
            self.bot = np.where(valid, np.maximum(self.bot, zp), self.bot)
        self.setups_done += 1


def final_check(job: Job, column: ColumnStock, save_dir=None, png=False) -> dict:
    """Leftover after all setups: part top from +Z, part bottom from -Z, nothing outside the part."""
    tris = part_triangles(job)
    g = column.grid
    Pt = g.empty()
    zmap.raster_top(Pt, g.x0, g.y0, g.res, np.ascontiguousarray(tris))
    flipped = tris.copy()
    flipped[..., 2] *= -1
    Pb = g.empty()
    zmap.raster_top(Pb, g.x0, g.y0, g.res, np.ascontiguousarray(flipped))
    part = Pt > zmap.AIR * 0.5
    Pb = -Pb
    size = 2 * int(job.sim["edge_band_cells"]) + 1
    Ttop = ndimage.maximum_filter(np.where(part, Pt, -np.inf), size=size, mode="nearest")
    Tbot = ndimage.minimum_filter(np.where(part, Pb, np.inf), size=size, mode="nearest")
    top, bot = column.top, column.bot
    filled = top > bot
    near = np.isfinite(Ttop)
    with np.errstate(invalid="ignore"):
        left_top = np.where(filled & near, top - Ttop, 0.0)
        left_bot = np.where(filled & near, Tbot - bot, 0.0)
        left_out = np.where(filled & ~near, top - bot, 0.0)
    left = np.maximum(np.maximum(left_top, left_bot), left_out)
    tol = job.tol
    lmask = left > tol["leftover"]
    part_cells = max(int(part.sum()), 1)
    res = {"leftover": {"max": round(float(left.max(initial=0.0)), 5),
                        "area_pct": round(100 * lmask.sum() / part_cells, 3), "cells": int(lmask.sum()),
                        "regions": _regions(lmask, left, g), "frame": "part"},
           "from_top_max": round(float(left_top.max(initial=0.0)), 5),
           "from_bottom_max": round(float(left_bot.max(initial=0.0)), 5),
           "outside_part_max": round(float(left_out.max(initial=0.0)), 5)}
    if save_dir is not None:
        np.savez_compressed(Path(save_dir) / "final_columns.npz", top=np.where(filled, top, np.nan),
                            bot=np.where(filled, bot, np.nan), leftover=left, extent=np.array(g.extent()))
    return res


def simulate_setup(job: Job, setup: Setup, moves, *, res=None, save_dir=None, png=False,
                   column: ColumnStock | None = None) -> dict:
    t_start = time.perf_counter()
    res = res or job.sim["resolution"]
    tol = job.tol
    lo, hi = job.stock_box_wcs(setup)
    margin = job.sim["margin"]
    grid = zmap.Grid(lo[0] - margin, lo[1] - margin, hi[0] + margin, hi[1] + margin, res)
    xs, ys = grid.centers()
    inside = (xs[None, :] >= lo[0]) & (xs[None, :] <= hi[0]) & (ys[:, None] >= lo[1]) & (ys[:, None] <= hi[1])
    carried = column is not None and z_sign(setup) is not None
    stock_label = ("in-process stock from previous setups" if carried and column.setups_done else "raw stock box")
    H = column.initial(setup, grid) if carried else np.where(inside, hi[2], zmap.AIR)
    if getattr(setup, "initial_stock", None) == "part" and part_triangles(job) is not None:
        # Side/tilted setup done after the other faces are finished: start from the finished part seen
        # from this setup's tool axis instead of the raw stock box (features cut here are then leftover-free
        # only if the model already contains them, so leftover is checked against the part as usual).
        tw0 = setup.to_wcs(part_triangles(job).reshape(-1, 3)).reshape(-1, 3, 3)
        P0 = grid.empty()
        zmap.raster_top(P0, grid.x0, grid.y0, grid.res, np.ascontiguousarray(tw0))
        H = np.where(P0 > zmap.AIR * 0.5, P0, zmap.AIR)
        carried = False
        stock_label = "finished part (initial_stock: part)"
    far = column.far_end(setup, grid) if carried else np.full(H.shape, float(lo[2]))
    ip = inprocess_heights(setup, grid) if getattr(setup, "initial_stock", None) != "part" else None
    if ip is not None:
        # exact in-process stock from the primary simulation (faced surfaces, side setups, earlier setups)
        H, far = ip
        stock_label = "in-process stock (primary simulation)"
    elif setup is not job.setups[0]:
        stock_label += " [raw-blank assumption]" if not carried else ""
    last = np.full(H.shape, -1, np.int64)

    missing = sorted({m.tool for m in moves if m.tool and m.tool not in job.tools})
    segs, stool, srapid, smove, tool_ids = build_segments(moves, job.tools, job.sim["arc_tolerance"])
    n = len(segs)
    out_depth, out_vol, out_holder, out_flute = (np.zeros(n) for _ in range(4))
    out_holder_k = np.full(n, -1, np.int64)
    kind, R, a, b, flute, cyl_off, cyl_r = tool_arrays(job.tools, tool_ids, job.drill_model)
    t0 = time.perf_counter()
    if n:
        zmax = float(max(hi[2], H.max()))
        zmap.sweep(H, last, grid.x0, grid.y0, grid.res, segs, stool, srapid, kind, R, a, b, flute, cyl_off, cyl_r,
                   zmax, float(tol["holder_clearance"]), float(job.sim["search_tolerance"]),
                   out_depth, out_vol, out_holder, out_holder_k, out_flute)
    t_sweep = time.perf_counter() - t0
    if carried:
        column.update(setup, grid, H)

    def moves_report(mask, values, what, part=None):
        idx = np.nonzero(mask)[0]
        rows = {}
        for s in idx:
            m = moves[smove[s]]
            k = (m.file, m.line)
            v = float(values[s])
            if k not in rows or v > rows[k]["max"]:
                rows[k] = {"file": m.file, "line": m.line, "tool": m.tool, "max": round(v, 5), "text": m.text}
                if part is not None and m.tool in job.tools:
                    asm = job.tools[m.tool].assembly()
                    kk = int(part[s])
                    rows[k]["part"] = asm[kk][2] if 0 <= kk < len(asm) else "?"
        rows = sorted(rows.values(), key=lambda r: -r["max"])
        return {"count": len(rows), "worst": rows[:10], "what": what}

    rapid = moves_report((srapid == 1) & (out_depth > tol["rapid_contact"]), out_depth,
                         "rapid removes material (rapid through stock)")
    holder = moves_report(out_holder > 0, out_holder, "shank or holder below the top of remaining material",
                          part=out_holder_k)
    flutes = moves_report(out_flute > tol["gouge"], out_flute, "cut deeper than the tool's flute length")
    thread_idx = [i for i, t in enumerate(tool_ids) if job.tools[t].sim_mode == "thread"]
    thread = moves_report(np.isin(stool, thread_idx) & (srapid == 0) & (out_depth > tol["rapid_contact"]),
                          out_depth, "tap/thread mill cut material inside 0.75 x its diameter (hole not drilled?)")
    not_simulated = sorted({f"T{t}" for t in tool_ids if job.tools[t].sim_mode == "skip"})
    no_holder = sorted({f"T{t}" for t in tool_ids if not job.tools[t].assembly()})

    # per-tool removed volume and air cutting share
    per_tool = {}
    for i, t in enumerate(tool_ids):
        sel = (stool == i) & (srapid == 0)
        if sel.any():
            lens = np.linalg.norm(segs[sel, 3:] - segs[sel, :3], axis=1)
            cutting = lens[out_vol[sel] > 0].sum()
            per_tool[f"T{t}"] = {"removed_volume": round(float(out_vol[sel].sum()), 4),
                                 "feed_length": round(float(lens.sum()), 3),
                                 "air_feed_pct": round(100 * (1 - cutting / max(lens.sum(), 1e-12)), 1)}

    result = {"setup": setup.name, "resolution": res, "grid": [grid.nx, grid.ny], "segments": n,
              "initial_stock": stock_label,
              "missing_tools": missing, "rapid_through_stock": rapid, "holder_collision": holder,
              "beyond_flute_length": flutes, "thread_tool_in_solid": thread, "per_tool": per_tool,
              "tools_not_simulated": not_simulated, "tools_without_holder": no_holder}

    # ---- compare with the part
    tris = part_triangles(job)
    if tris is not None:
        t1 = time.perf_counter()
        tw = setup.to_wcs(tris.reshape(-1, 3)).reshape(-1, 3, 3)
        P = grid.empty()
        zmap.raster_top(P, grid.x0, grid.y0, grid.res, np.ascontiguousarray(tw))
        t_raster = time.perf_counter() - t1
        has_part = P > zmap.AIR * 0.5
        k = int(job.sim["edge_band_cells"])
        size = 2 * k + 1
        big = 1e29
        P_lo = ndimage.minimum_filter(np.where(has_part, P, -big), size=size, mode="nearest")
        floor_z = setup.leftover_floor_z
        if floor_z is None:
            floor_z = float(tw[..., 2].min())
        T = np.where(has_part, P, floor_z)
        T_hi = ndimage.maximum_filter(T, size=size, mode="nearest")
        material = H > far + 1e-9   # far = stock bottom, or the underside carried from earlier setups
        Hc = np.maximum(H, np.where(np.isfinite(far), far, zmap.AIR))  # cutting below the underside removes nothing
        gouge = np.where(has_part & inside & (P_lo > -big / 2), P_lo - Hc, 0.0)
        gmask = gouge > tol["gouge"]
        left = np.where(material & inside, H - np.maximum(T_hi, np.where(np.isfinite(far), far, T_hi)), 0.0)
        lmask = left > tol["leftover"]
        part_cells = max(int(has_part.sum()), 1)

        culprits = {}
        for s in last[gmask]:
            if s >= 0:
                m = moves[smove[s]]
                key = (m.file, m.line)
                culprits[key] = culprits.get(key, 0) + 1
        top_culprits = sorted(culprits.items(), key=lambda kv: -kv[1])[:10]
        result["gouge"] = {
            "max": round(float(gouge.max(initial=0.0)), 5),
            "area_pct": round(100 * gmask.sum() / part_cells, 3),
            "cells": int(gmask.sum()),
            "regions": _regions(gmask, gouge, grid),
            "lines": [{"file": f, "line": ln, "cells": c, "text": moves_by_line(moves, f, ln)}
                      for (f, ln), c in top_culprits],
        }
        result["leftover"] = {
            "max": round(float(left.max(initial=0.0)), 5),
            "area_pct": round(100 * lmask.sum() / part_cells, 3),
            "cells": int(lmask.sum()),
            "regions": _regions(lmask, left, grid),
            "floor_z": round(floor_z, 5),
        }
        result["part_cells"] = part_cells
        result["timing_s"] = {"sweep": round(t_sweep, 3), "raster": round(t_raster, 3)}
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(save_dir / f"{_safe(setup.name)}_maps.npz", sim=np.where(material, H, np.nan),
                                part=np.where(has_part, P, np.nan), gouge=gouge, leftover=left,
                                extent=np.array(grid.extent()))
            if png:
                _png(save_dir / f"{_safe(setup.name)}_maps.png", grid, H, material, P, has_part, gouge, left,
                     tol, setup.name, job.units)
    else:
        result["timing_s"] = {"sweep": round(t_sweep, 3)}
    result["timing_s"]["total"] = round(time.perf_counter() - t_start, 3)
    return result


def moves_by_line(moves, f, ln):
    for m in moves:
        if m.file == f and m.line == ln:
            return m.text
    return ""


def _safe(s):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


def _png(path, grid, H, material, P, has_part, gouge, left, tol, name, units):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ext = grid.extent()
    fig, axs = plt.subplots(1, 3, figsize=(15, 4.6), constrained_layout=True)
    im = axs[0].imshow(np.where(material, H, np.nan), origin="lower", extent=ext, cmap="viridis")
    axs[0].set_title(f"{name}: simulated stock Z")
    fig.colorbar(im, ax=axs[0], shrink=0.8, label=units)
    g = np.where(gouge > tol["gouge"], gouge, np.nan)
    axs[1].imshow(np.where(has_part, 0.0, np.nan), origin="lower", extent=ext, cmap="Greys", vmin=-1, vmax=1)
    im = axs[1].imshow(g, origin="lower", extent=ext, cmap="Reds", vmin=0)
    axs[1].set_title(f"gouge > {tol['gouge']:g} (max {np.nanmax(gouge, initial=0):.4f})")
    fig.colorbar(im, ax=axs[1], shrink=0.8, label=units)
    lft = np.where(left > tol["leftover"], left, np.nan)
    axs[2].imshow(np.where(has_part, 0.0, np.nan), origin="lower", extent=ext, cmap="Greys", vmin=-1, vmax=1)
    im = axs[2].imshow(lft, origin="lower", extent=ext, cmap="Blues", vmin=0)
    axs[2].set_title(f"leftover > {tol['leftover']:g} (max {np.nanmax(left, initial=0):.4f})")
    fig.colorbar(im, ax=axs[2], shrink=0.8, label=units)
    for ax in axs:
        ax.set_aspect("equal")
    fig.savefig(path, dpi=110)
    plt.close(fig)
