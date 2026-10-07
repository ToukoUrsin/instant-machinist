"""voxel_sim: 3D voxel material removal of the posted G-code for every setup, compared with the part in 3D.

The in-process stock is one boolean voxel grid in the PART frame (default 0.01 in). Every setup is
simulated along its own tool axis, so top, flipped, side/end (90 deg) and general 3+2 setups all carry the
stock from the previous setups. After the last setup the stock is compared with the voxelised part:

- gouge: stock removed inside the part (exact distance to the part mesh, so tessellation and arc chords
  do not count). Shallow removal next to sharp convex edges is an `edge_break` warning (chamfers/deburrs
  the model does not show).
- leftover: stock outside the part that remains (thickness, % of part volume, regions with location).
- per setup: rapids into material, shank/holder collisions, cuts deeper than the flute length and taps or
  thread mills cutting solid, each traced to NC file:line.

How a setup is simulated:
- Setups whose WCS axes are a signed permutation of the part axes (top, flip, 90 deg side/end, rotations
  about Z by 90 deg) are exact: the part grid is transposed/flipped so the tool axis runs along the
  contiguous array axis, the sweep runs, and the grid is transposed back.
- Other (general 3+2) setups are resampled: the stock is sampled into a grid aligned with the setup's WCS,
  swept there, and the removal is mapped back to the part grid with an exact point-in-swept-tool test
  against the segments that cut the neighbouring cells.
- The sweep kernel works on tool-axis columns. For every column within the tool radius it finds the lowest
  point of the cutting profile over the segment (same maths as zmap.sweep) and removes the voxels from
  there up to the top of the tool body (stickout). A per-column upper bound of the highest material voxel
  skips columns the tool cannot reach, which keeps it fast.

CLI: python -m checks.voxel_sim JOB_DIR [--res 0.01] [--png] [--out DIR]
     writes JOB_DIR/check_out/voxel_report.{json,md} (+ voxel_final.npz, voxel_final.png)
Python: run_voxel(job_dir, out_dir=None, res=None, png=False, overrides=None, write=True) -> report dict.

Settings: VOXEL_DEFAULTS_INCH below, overridable in job.json under "voxel" (lengths in job units).
job.json "tolerances" gouge / leftover / rapid_contact / holder_clearance are shared with stock_sim.
Pass/fail: any gouge voxel (after the edge-break filter), any rapid into stock, holder/shank hit, cut
beyond the flute length, thread tool in solid or missing tool; leftover fails when a region is thicker
than leftover_max (measured from the part surface) with >= leftover_min_region_voxels voxels, or when
leftover exceeds leftover_volume_pct_max % of the part volume. Depth/thickness values are +-res/2.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from scipy import ndimage

from . import stock_sim
from .gcode import Interpreter
from .job import Job, Setup, load_job, unit_factor
from .zmap import _h

try:
    from numba import njit
except ImportError:  # pragma: no cover
    def njit(*a, **k):
        if a and callable(a[0]):
            return a[0]
        return lambda f: f

# Defaults in inches (scaled for mm jobs). Override in job.json under "voxel" (job units).
VOXEL_DEFAULTS_INCH = {
    "resolution": 0.01,
    "pad_voxels": 2,                 # empty voxels around stock + part
    "edge_break_dist": 0.035,        # removed voxel within this of a sharp convex edge ...
    "edge_break_depth": 0.025,       # ... and at most this deep = edge_break warning, not gouge
    "sharp_edge_deg": 30.0,          # dihedral angle above which a mesh edge is a sharp edge
    "distance_cap": 0.05,            # exact mesh distance up to this; deeper values from a distance transform
    "leftover_max": 0.02,            # a leftover region thicker than this ...
    "leftover_min_region_voxels": 20,  # ... with at least this many voxels fails
    "leftover_volume_pct_max": 0.5,  # or total leftover above this % of the part volume
    "gouge_min_voxels": 1,           # gouge voxels (after edge-break filtering) needed to fail
    "edt_margin": 1.0,               # crop margin for the distance transform
    "buffer_capacity": 4_000_000,    # gouge culprit records per setup
}
_LENGTHS = {"resolution", "edge_break_dist", "edge_break_depth", "distance_cap", "leftover_max", "edt_margin"}


def voxel_config(job: Job, overrides: dict | None = None) -> dict:
    f = unit_factor("inch", job.units)
    cfg = {k: (v * f if k in _LENGTHS else v) for k, v in VOXEL_DEFAULTS_INCH.items()}
    try:
        raw = json.loads((Path(job.root) / "job.json").read_text()).get("voxel") or {}
    except (OSError, ValueError):
        raw = {}
    cfg.update(raw)
    cfg.update(overrides or {})
    return cfg


# ----------------------------------------------------------------------------------------------- kernels
@njit(cache=True)
def _col(px, py, ax, ay, az, dx, dy, dz, L2, L, kind, R, a, b, ztol, zcap, body, body_r, flute):
    """Tool-axis column at lateral (px, py) against one tip segment.
    Returns (hit, zb, zt): the swept tool occupies [zb, zt] along the axis in this column
    (zb = lowest point of the cutting profile over the segment, zt = top of the tool body).
    Misses when the column is outside the tool or the lowest possible profile point is above zcap."""
    if dz >= 0:
        zlo, zhi = az, az + dz
    else:
        zlo, zhi = az + dz, az
    if L2 < 1e-18:
        d = math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
        if d > R:
            return False, 0.0, 0.0
        zb = zlo + _h(kind, R, a, b, d)
        if zb > zcap:
            return False, 0.0, 0.0
        return True, zb, zhi + (body if d <= body_r else flute)
    tl = ((px - ax) * dx + (py - ay) * dy) / L2
    tc = min(max(tl, 0.0), 1.0)
    ex, ey = ax + tc * dx - px, ay + tc * dy - py
    dmin = math.sqrt(ex * ex + ey * ey)
    if dmin > R:
        return False, 0.0, 0.0
    if zlo + _h(kind, R, a, b, dmin) > zcap:
        return False, 0.0, 0.0
    up = body if dmin <= body_r else flute
    if abs(dz) < 1e-12:
        return True, az + _h(kind, R, a, b, dmin), az + up
    qx, qy = ax + tl * dx - px, ay + tl * dy - py
    dp2 = qx * qx + qy * qy
    hw = math.sqrt(max(R * R - dp2, 0.0)) / L
    ta = max(tl - hw, 0.0)
    tb = min(tl + hw, 1.0)
    za = az + dz * ta
    zc = az + dz * tb
    zt = max(za, zc) + up
    if kind == 0:
        return True, min(za, zc), zt
    lo, hi = ta, tb
    scale = L + abs(dz)
    g = 0.3819660112501051
    m1 = lo + g * (hi - lo)
    m2 = hi - g * (hi - lo)
    e1 = (m1 - tl) * L
    e2 = (m2 - tl) * L
    f1 = az + dz * m1 + _h(kind, R, a, b, min(math.sqrt(dp2 + e1 * e1), R))
    f2 = az + dz * m2 + _h(kind, R, a, b, min(math.sqrt(dp2 + e2 * e2), R))
    while (hi - lo) * scale > ztol:
        if f1 < f2:
            hi = m2
            m2 = m1
            f2 = f1
            m1 = lo + g * (hi - lo)
            e1 = (m1 - tl) * L
            f1 = az + dz * m1 + _h(kind, R, a, b, min(math.sqrt(dp2 + e1 * e1), R))
        else:
            lo = m1
            m1 = m2
            f1 = f2
            m2 = hi - g * (hi - lo)
            e2 = (m2 - tl) * L
            f2 = az + dz * m2 + _h(kind, R, a, b, min(math.sqrt(dp2 + e2 * e2), R))
    zb = min(f1, f2)
    ea = (ta - tl) * L
    eb = (tb - tl) * L
    zb = min(zb, za + _h(kind, R, a, b, min(math.sqrt(dp2 + ea * ea), R)),
             zc + _h(kind, R, a, b, min(math.sqrt(dp2 + eb * eb), R)))
    return True, zb, zt


@njit(cache=True)
def column_tops(G, top):
    nx, ny, nz = G.shape
    for i in range(nx):
        for j in range(ny):
            k = nz - 1
            while k >= 0 and G[i, j, k] == 0:
                k -= 1
            top[i, j] = k


@njit(cache=True)
def _max2(top):
    m = -1
    for i in range(top.shape[0]):
        for j in range(top.shape[1]):
            if top[i, j] > m:
                m = top[i, j]
    return m


@njit(cache=True)
def sweep_voxels(G, top, P, use_p, last, use_last, w0x, w0y, w0z, res, segs, seg_tool, seg_rapid,
                 t_kind, t_R, t_a, t_b, t_flute, t_body, t_body_r, cyl_off, cyl_r, holder_tol, ztol,
                 out_depth, out_vol, out_holder, out_holder_k, out_flute, buf_idx, buf_seg, buf_n):
    """Sweep tip segments (WCS) through the tool-frame voxel grid G[i, j, k] (k along the tool axis).
    top[i, j] is an upper bound of the highest material voxel per column (-1 = empty).
    Per segment: max material thickness removed in one column, removed volume, max material height above
    the flute length, and max shank/holder intrusion (+ which cylinder).
    Removed voxels inside the part (P) are recorded in buf_idx/buf_seg (flat tool-grid index, segment)."""
    nx, ny, nz = G.shape
    cap = buf_idx.shape[0]
    vox = res * res * res
    gtop = _max2(top)
    for s in range(segs.shape[0]):
        if s > 0 and (s & 255) == 0:
            gtop = _max2(top)
        ti = seg_tool[s]
        if ti < 0 or gtop < 0:
            continue
        ax, ay, az, bx, by, bz = segs[s, 0], segs[s, 1], segs[s, 2], segs[s, 3], segs[s, 4], segs[s, 5]
        kind, R, a, b = t_kind[ti], t_R[ti], t_a[ti], t_b[ti]
        body, body_r, flute = t_body[ti], t_body_r[ti], t_flute[ti]
        dx, dy, dz = bx - ax, by - ay, bz - az
        L2 = dx * dx + dy * dy
        L = math.sqrt(L2)
        zmin_seg = min(az, bz)
        gz = w0z + (gtop + 0.5) * res
        if zmin_seg <= gz and R > 0:
            i0 = max(int(math.floor((min(ax, bx) - R - w0x) / res)), 0)
            i1 = min(int(math.ceil((max(ax, bx) + R - w0x) / res)), nx - 1)
            j0 = max(int(math.floor((min(ay, by) - R - w0y) / res)), 0)
            j1 = min(int(math.ceil((max(ay, by) + R - w0y) / res)), ny - 1)
            maxd = 0.0
            cnt = 0
            beyond = 0.0
            for i in range(i0, i1 + 1):
                px = w0x + (i + 0.5) * res
                for j in range(j0, j1 + 1):
                    tk = top[i, j]
                    if tk < 0:
                        continue
                    py = w0y + (j + 0.5) * res
                    zc = w0z + (tk + 0.5) * res
                    hit, zb, zt = _col(px, py, ax, ay, az, dx, dy, dz, L2, L, kind, R, a, b, ztol, zc,
                                       body, body_r, flute)
                    if not hit:
                        continue
                    kb = int(math.ceil((zb - w0z) / res - 0.5))
                    if kb < 0:
                        kb = 0
                    kt = int(math.floor((zt - w0z) / res - 0.5))
                    if kt > tk:
                        kt = tk
                    if kb > kt:
                        continue
                    c = 0
                    hik = -1
                    for k in range(kb, kt + 1):
                        if G[i, j, k] != 0:
                            G[i, j, k] = 0
                            c += 1
                            hik = k
                            if use_last:
                                last[i, j, k] = s
                            if use_p and P[i, j, k] != 0:
                                n = buf_n[0]
                                if n < cap:
                                    buf_idx[n] = (i * ny + j) * nz + k
                                    buf_seg[n] = s
                                buf_n[0] = n + 1
                    if kt >= tk:
                        top[i, j] = kb - 1
                    if c > 0:
                        cnt += c
                        th = c * res
                        if th > maxd:
                            maxd = th
                        bey = w0z + (hik + 0.5) * res - zb - flute
                        if bey > beyond:
                            beyond = bey
            out_depth[s] = maxd
            out_vol[s] = cnt * vox
            if seg_rapid[s] == 0 and beyond > 0:
                out_flute[s] = beyond
        # ---- shank / holder cylinders above the tip (treated as unbounded upward, like zmap)
        worst = 0.0
        worst_k = -1
        for c in range(cyl_r.shape[1]):
            Rh = cyl_r[ti, c]
            off = cyl_off[ti, c]
            if Rh <= 0 or zmin_seg + off + holder_tol >= gz:
                continue
            i0 = max(int(math.floor((min(ax, bx) - Rh - w0x) / res)), 0)
            i1 = min(int(math.ceil((max(ax, bx) + Rh - w0x) / res)), nx - 1)
            j0 = max(int(math.floor((min(ay, by) - Rh - w0y) / res)), 0)
            j1 = min(int(math.ceil((max(ay, by) + Rh - w0y) / res)), ny - 1)
            for i in range(i0, i1 + 1):
                px = w0x + (i + 0.5) * res
                for j in range(j0, j1 + 1):
                    tk = top[i, j]
                    if tk < 0:
                        continue
                    zc = w0z + (tk + 0.5) * res
                    if zc <= zmin_seg + off + holder_tol:
                        continue
                    py = w0y + (j + 0.5) * res
                    if L2 < 1e-18:
                        tc = 0.0
                    else:
                        tc = min(max(((px - ax) * dx + (py - ay) * dy) / L2, 0.0), 1.0)
                    qx, qy = ax + tc * dx - px, ay + tc * dy - py
                    if qx * qx + qy * qy > Rh * Rh:
                        continue
                    zh = az + dz * tc + off
                    if zc <= zh + holder_tol:
                        continue
                    k = tk
                    while k >= 0 and G[i, j, k] == 0:
                        k -= 1
                    top[i, j] = k
                    if k < 0:
                        continue
                    u = w0z + (k + 0.5) * res
                    if u > zh + holder_tol and u - zh > worst:
                        worst = u - zh
                        worst_k = c
        out_holder[s] = worst
        out_holder_k[s] = worst_k


@njit(cache=True)
def _sample_to_tool(V, o, res, Rm, t, w0, G):
    """G[i, j, k] = V at the part voxel containing the tool-grid cell centre (nearest sampling)."""
    nx, ny, nz = G.shape
    px_, py_, pz_ = V.shape
    for i in range(nx):
        wx = w0[0] + (i + 0.5) * res - t[0]
        for j in range(ny):
            wy = w0[1] + (j + 0.5) * res - t[1]
            for k in range(nz):
                wz = w0[2] + (k + 0.5) * res - t[2]
                x = Rm[0, 0] * wx + Rm[1, 0] * wy + Rm[2, 0] * wz
                y = Rm[0, 1] * wx + Rm[1, 1] * wy + Rm[2, 1] * wz
                z = Rm[0, 2] * wx + Rm[1, 2] * wy + Rm[2, 2] * wz
                a = int(math.floor((x - o[0]) / res))
                b = int(math.floor((y - o[1]) / res))
                c = int(math.floor((z - o[2]) / res))
                if 0 <= a < px_ and 0 <= b < py_ and 0 <= c < pz_:
                    G[i, j, k] = V[a, b, c]
                else:
                    G[i, j, k] = 0


@njit(cache=True)
def _map_back(V, P, o, res, Rm, t, w0, last, segs, seg_tool, t_kind, t_R, t_a, t_b, t_flute, t_body,
              t_body_r, ztol, buf_idx, buf_seg, buf_n):
    """Resampled setups: clear part voxels that lie inside the swept tool of a segment that removed one of
    the 8 surrounding tool-grid cells (exact point test). Records removed in-part voxels (part flat index)."""
    nx, ny, nz = V.shape
    tx, ty, tz = last.shape
    cap = buf_idx.shape[0]
    cand = np.empty(8, np.int64)
    removed = 0
    for i in range(nx):
        x = o[0] + (i + 0.5) * res
        for j in range(ny):
            y = o[1] + (j + 0.5) * res
            for k in range(nz):
                if V[i, j, k] == 0:
                    continue
                z = o[2] + (k + 0.5) * res
                wx = Rm[0, 0] * x + Rm[0, 1] * y + Rm[0, 2] * z + t[0]
                wy = Rm[1, 0] * x + Rm[1, 1] * y + Rm[1, 2] * z + t[1]
                wz = Rm[2, 0] * x + Rm[2, 1] * y + Rm[2, 2] * z + t[2]
                fx = (wx - w0[0]) / res - 0.5
                fy = (wy - w0[1]) / res - 0.5
                fz = (wz - w0[2]) / res - 0.5
                a0 = int(math.floor(fx))
                b0 = int(math.floor(fy))
                c0 = int(math.floor(fz))
                nc = 0
                for da in range(2):
                    aa = a0 + da
                    if aa < 0 or aa >= tx:
                        continue
                    for db in range(2):
                        bb = b0 + db
                        if bb < 0 or bb >= ty:
                            continue
                        for dc in range(2):
                            cc = c0 + dc
                            if cc < 0 or cc >= tz:
                                continue
                            sg = last[aa, bb, cc]
                            if sg < 0:
                                continue
                            dup = False
                            for q in range(nc):
                                if cand[q] == sg:
                                    dup = True
                            if not dup:
                                cand[nc] = sg
                                nc += 1
                for q in range(nc):
                    s = cand[q]
                    ti = seg_tool[s]
                    if ti < 0:
                        continue
                    ax, ay, az = segs[s, 0], segs[s, 1], segs[s, 2]
                    dx, dy, dz = segs[s, 3] - ax, segs[s, 4] - ay, segs[s, 5] - az
                    L2 = dx * dx + dy * dy
                    hit, zb, zt = _col(wx, wy, ax, ay, az, dx, dy, dz, L2, math.sqrt(L2), t_kind[ti], t_R[ti],
                                       t_a[ti], t_b[ti], ztol, 1e30, t_body[ti], t_body_r[ti], t_flute[ti])
                    if hit and zb <= wz <= zt:
                        V[i, j, k] = 0
                        removed += 1
                        if P[i, j, k] != 0:
                            n = buf_n[0]
                            if n < cap:
                                buf_idx[n] = (i * ny + j) * nz + k
                                buf_seg[n] = s
                            buf_n[0] = n + 1
                        break
    return removed


# ---- part voxelisation: winding number along +Z rays through the voxel column centres
@njit(cache=True)
def _vox_count(tris, x0, y0, res, jx, jy, counts):
    nx, ny = counts.shape
    for m in range(tris.shape[0]):
        ax, ay = tris[m, 0, 0], tris[m, 0, 1]
        bx, by = tris[m, 1, 0], tris[m, 1, 1]
        cx, cy = tris[m, 2, 0], tris[m, 2, 1]
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-18:
            continue
        i0 = max(int(math.ceil((min(ax, bx, cx) - jx - x0) / res - 0.5)), 0)
        i1 = min(int(math.floor((max(ax, bx, cx) - jx - x0) / res - 0.5)), nx - 1)
        j0 = max(int(math.ceil((min(ay, by, cy) - jy - y0) / res - 0.5)), 0)
        j1 = min(int(math.floor((max(ay, by, cy) - jy - y0) / res - 0.5)), ny - 1)
        for i in range(i0, i1 + 1):
            px = x0 + (i + 0.5) * res + jx
            for j in range(j0, j1 + 1):
                py = y0 + (j + 0.5) * res + jy
                w1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
                w2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
                if w1 < 0 or w2 < 0 or 1.0 - w1 - w2 < 0:
                    continue
                counts[i, j] += 1


@njit(cache=True)
def _vox_fill(tris, x0, y0, res, jx, jy, starts, pos, zs, sg):
    nx, ny = pos.shape
    for m in range(tris.shape[0]):
        ax, ay, az = tris[m, 0, 0], tris[m, 0, 1], tris[m, 0, 2]
        bx, by, bz = tris[m, 1, 0], tris[m, 1, 1], tris[m, 1, 2]
        cx, cy, cz = tris[m, 2, 0], tris[m, 2, 1], tris[m, 2, 2]
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-18:
            continue
        nzs = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        sgn = 1 if nzs < 0 else -1  # facing down: a ray going up enters the solid
        i0 = max(int(math.ceil((min(ax, bx, cx) - jx - x0) / res - 0.5)), 0)
        i1 = min(int(math.floor((max(ax, bx, cx) - jx - x0) / res - 0.5)), nx - 1)
        j0 = max(int(math.ceil((min(ay, by, cy) - jy - y0) / res - 0.5)), 0)
        j1 = min(int(math.floor((max(ay, by, cy) - jy - y0) / res - 0.5)), ny - 1)
        for i in range(i0, i1 + 1):
            px = x0 + (i + 0.5) * res + jx
            for j in range(j0, j1 + 1):
                py = y0 + (j + 0.5) * res + jy
                w1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
                w2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
                w3 = 1.0 - w1 - w2
                if w1 < 0 or w2 < 0 or w3 < 0:
                    continue
                q = starts[i * ny + j] + pos[i, j]
                zs[q] = w1 * az + w2 * bz + w3 * cz
                sg[q] = sgn
                pos[i, j] += 1


@njit(cache=True)
def _vox_columns(counts, starts, zs, sg, z0, res, P, bad):
    nx, ny, nz = P.shape
    for i in range(nx):
        for j in range(ny):
            n = counts[i, j]
            if n == 0:
                continue
            s0 = starts[i * ny + j]
            zz = zs[s0:s0 + n]
            ss = sg[s0:s0 + n]
            order = np.argsort(zz)
            w = 0
            for q in range(n):
                w += ss[order[q]]
                if q < n - 1 and w > 0:
                    za = zz[order[q]]
                    zb = zz[order[q + 1]]
                    k0 = max(int(math.ceil((za - z0) / res - 0.5)), 0)
                    k1 = min(int(math.floor((zb - z0) / res - 0.5)), nz - 1)
                    for k in range(k0, k1 + 1):
                        P[i, j, k] = 1
            if w != 0:
                bad[i, j] = 1


# ---- exact distances to the mesh / to sharp edges through a bucket grid
@njit(cache=True)
def _bucket(lo, hi, pn, pd, use_plane, o, cell, dims, counts_only, starts, items):
    """Register primitives (AABB lo/hi, optional plane filter) into cells. Two passes: counts then fill."""
    nxc, nyc, nzc = dims[0], dims[1], dims[2]
    fillpos = np.zeros(nxc * nyc * nzc, np.int64)
    counts = np.zeros(nxc * nyc * nzc, np.int64)
    half = 0.8660254037844386 * cell
    for m in range(lo.shape[0]):
        if use_plane and pn[m, 0] == 0.0 and pn[m, 1] == 0.0 and pn[m, 2] == 0.0:
            continue
        a0 = max(int(math.floor((lo[m, 0] - o[0]) / cell)), 0)
        a1 = min(int(math.floor((hi[m, 0] - o[0]) / cell)), nxc - 1)
        b0 = max(int(math.floor((lo[m, 1] - o[1]) / cell)), 0)
        b1 = min(int(math.floor((hi[m, 1] - o[1]) / cell)), nyc - 1)
        c0 = max(int(math.floor((lo[m, 2] - o[2]) / cell)), 0)
        c1 = min(int(math.floor((hi[m, 2] - o[2]) / cell)), nzc - 1)
        for a in range(a0, a1 + 1):
            for b in range(b0, b1 + 1):
                for c in range(c0, c1 + 1):
                    if use_plane:
                        cx = o[0] + (a + 0.5) * cell
                        cy = o[1] + (b + 0.5) * cell
                        cz = o[2] + (c + 0.5) * cell
                        if abs(pn[m, 0] * cx + pn[m, 1] * cy + pn[m, 2] * cz - pd[m]) > half:
                            continue
                    f = (a * nyc + b) * nzc + c
                    if counts_only:
                        counts[f] += 1
                    else:
                        items[starts[f] + fillpos[f]] = m
                        fillpos[f] += 1
    return counts


@njit(cache=True)
def _d2_tri(px, py, pz, T, m):
    """Squared distance from a point to triangle m (Ericson, closest point on triangle)."""
    ax, ay, az = T[m, 0, 0], T[m, 0, 1], T[m, 0, 2]
    bx, by, bz = T[m, 1, 0], T[m, 1, 1], T[m, 1, 2]
    cx, cy, cz = T[m, 2, 0], T[m, 2, 1], T[m, 2, 2]
    abx, aby, abz = bx - ax, by - ay, bz - az
    acx, acy, acz = cx - ax, cy - ay, cz - az
    apx, apy, apz = px - ax, py - ay, pz - az
    d1 = abx * apx + aby * apy + abz * apz
    d2 = acx * apx + acy * apy + acz * apz
    if d1 <= 0 and d2 <= 0:
        return apx * apx + apy * apy + apz * apz
    bpx, bpy, bpz = px - bx, py - by, pz - bz
    d3 = abx * bpx + aby * bpy + abz * bpz
    d4 = acx * bpx + acy * bpy + acz * bpz
    if d3 >= 0 and d4 <= d3:
        return bpx * bpx + bpy * bpy + bpz * bpz
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        v = d1 / (d1 - d3)
        qx, qy, qz = ax + v * abx - px, ay + v * aby - py, az + v * abz - pz
        return qx * qx + qy * qy + qz * qz
    cpx, cpy, cpz = px - cx, py - cy, pz - cz
    d5 = abx * cpx + aby * cpy + abz * cpz
    d6 = acx * cpx + acy * cpy + acz * cpz
    if d6 >= 0 and d5 <= d6:
        return cpx * cpx + cpy * cpy + cpz * cpz
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        w = d2 / (d2 - d6)
        qx, qy, qz = ax + w * acx - px, ay + w * acy - py, az + w * acz - pz
        return qx * qx + qy * qy + qz * qz
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        qx = bx + w * (cx - bx) - px
        qy = by + w * (cy - by) - py
        qz = bz + w * (cz - bz) - pz
        return qx * qx + qy * qy + qz * qz
    den = 1.0 / (va + vb + vc)
    v = vb * den
    w = vc * den
    qx = ax + abx * v + acx * w - px
    qy = ay + aby * v + acy * w - py
    qz = az + abz * v + acz * w - pz
    return qx * qx + qy * qy + qz * qz


@njit(cache=True)
def _d2_seg(px, py, pz, S, m):
    ax, ay, az = S[m, 0, 0], S[m, 0, 1], S[m, 0, 2]
    dx, dy, dz = S[m, 1, 0] - ax, S[m, 1, 1] - ay, S[m, 1, 2] - az
    L2 = dx * dx + dy * dy + dz * dz
    t = 0.0
    if L2 > 0:
        t = min(max(((px - ax) * dx + (py - ay) * dy + (pz - az) * dz) / L2, 0.0), 1.0)
    qx, qy, qz = ax + t * dx - px, ay + t * dy - py, az + t * dz - pz
    return qx * qx + qy * qy + qz * qz


@njit(cache=True)
def _query(pts, prims, is_tri, o, cell, dims, starts, items, cap, out):
    nxc, nyc, nzc = dims[0], dims[1], dims[2]
    for q in range(pts.shape[0]):
        px, py, pz = pts[q, 0], pts[q, 1], pts[q, 2]
        a = int(math.floor((px - o[0]) / cell))
        b = int(math.floor((py - o[1]) / cell))
        c = int(math.floor((pz - o[2]) / cell))
        best = cap * cap
        for aa in range(a - 1, a + 2):
            if aa < 0 or aa >= nxc:
                continue
            for bb in range(b - 1, b + 2):
                if bb < 0 or bb >= nyc:
                    continue
                for cc in range(c - 1, c + 2):
                    if cc < 0 or cc >= nzc:
                        continue
                    f = (aa * nyc + bb) * nzc + cc
                    for r in range(starts[f], starts[f + 1]):
                        m = items[r]
                        d2 = _d2_tri(px, py, pz, prims, m) if is_tri else _d2_seg(px, py, pz, prims, m)
                        if d2 < best:
                            best = d2
        out[q] = math.sqrt(best)


class DistanceField:
    """Exact unsigned distance (capped) from points to triangles or segments, via a bucket grid."""

    def __init__(self, prims: np.ndarray, cap: float, triangles=True):
        self.prims = np.ascontiguousarray(prims, dtype=np.float64)
        self.cap = float(cap)
        self.is_tri = triangles
        self.cell = self.cap
        if len(self.prims) == 0:
            self.dims = np.array([1, 1, 1], np.int64)
            self.o = np.zeros(3)
            self.starts = np.zeros(2, np.int64)
            self.items = np.zeros(0, np.int64)
            return
        lo = self.prims.min(axis=1)
        hi = self.prims.max(axis=1)
        self.o = lo.min(axis=0) - self.cell
        self.dims = (np.ceil((hi.max(axis=0) - self.o) / self.cell).astype(np.int64) + 2)
        if triangles:
            n = np.cross(self.prims[:, 1] - self.prims[:, 0], self.prims[:, 2] - self.prims[:, 0])
            ln = np.linalg.norm(n, axis=1)
            ok = ln > 1e-30
            pn = np.zeros_like(n)
            pn[ok] = n[ok] / ln[ok, None]
            pd = np.einsum("ij,ij->i", pn, self.prims[:, 0])
        else:
            pn = np.zeros((len(self.prims), 3))
            pd = np.zeros(len(self.prims))
        dummy = np.zeros(1, np.int64)
        counts = _bucket(lo, hi, pn, pd, triangles, self.o, self.cell, self.dims, True, dummy, dummy)
        self.starts = np.zeros(len(counts) + 1, np.int64)
        np.cumsum(counts, out=self.starts[1:])
        self.items = np.zeros(int(self.starts[-1]), np.int64)
        _bucket(lo, hi, pn, pd, triangles, self.o, self.cell, self.dims, False, self.starts, self.items)

    def __call__(self, pts: np.ndarray) -> np.ndarray:
        pts = np.ascontiguousarray(pts, dtype=np.float64).reshape(-1, 3)
        out = np.full(len(pts), self.cap)
        if len(pts) and len(self.items):
            _query(pts, self.prims, self.is_tri, self.o, self.cell, self.dims, self.starts, self.items,
                   self.cap, out)
        return out


def sharp_convex_edges(tris: np.ndarray, angle_deg=30.0, weld=1e-6):
    """Mesh edges (E, 2, 3) where two faces meet at a convex dihedral angle above angle_deg.
    Returns (edges, stats). Vertices are welded by rounding (OCCT triangulates shared edges conformingly)."""
    V = tris.reshape(-1, 3)
    key = np.round(V / weld).astype(np.int64)
    _, vid = np.unique(key, axis=0, return_inverse=True)
    vid = vid.reshape(-1, 3)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(n, axis=1)
    good = ln > 1e-14
    nrm = np.zeros_like(n)
    nrm[good] = n[good] / ln[good, None]
    tri_idx = np.nonzero(good)[0]
    e = []
    for a, b, c in ((0, 1, 2), (1, 2, 0), (2, 0, 1)):
        e.append(np.stack([vid[tri_idx, a], vid[tri_idx, b], tri_idx, np.full(len(tri_idx), c),
                           np.full(len(tri_idx), a), np.full(len(tri_idx), b)], axis=1))
    e = np.concatenate(e)
    lo = np.minimum(e[:, 0], e[:, 1])
    hi = np.maximum(e[:, 0], e[:, 1])
    order = np.lexsort((hi, lo))
    lo, hi, e = lo[order], hi[order], e[order]
    brk = np.ones(len(e), bool)
    brk[1:] = (lo[1:] != lo[:-1]) | (hi[1:] != hi[:-1])
    gid = np.cumsum(brk) - 1
    cnt = np.bincount(gid)
    first = np.nonzero(brk)[0]
    pair = first[cnt[gid[first]] == 2]
    t1, t2 = e[pair, 2], e[pair + 1, 2]
    cosang = np.einsum("ij,ij->i", nrm[t1], nrm[t2])
    sharp = cosang < math.cos(math.radians(angle_deg))
    opp2 = tris[t2, e[pair + 1, 3]]
    p0 = tris[t1, e[pair, 4]]
    size = max(float(np.ptp(V, axis=0).max()), 1e-9)
    convex = np.einsum("ij,ij->i", opp2 - p0, nrm[t1]) < -1e-9 * size
    sel = pair[sharp & convex]
    edges = np.stack([tris[e[sel, 2], e[sel, 4]], tris[e[sel, 2], e[sel, 5]]], axis=1)
    stats = {"edges": int(len(first)), "manifold_pairs": int(len(pair)),
             "boundary_edges": int((cnt == 1).sum()), "sharp_convex": int(len(sel))}
    return edges, stats


# ----------------------------------------------------------------------------------------------- grid
class VGrid:
    """Part-frame voxel grid: voxel (i, j, k) centre = o + (idx + 0.5) * res. Arrays are [x, y, z] (z contiguous)."""

    def __init__(self, lo, hi, res, pad=2):
        self.res = float(res)
        lo, hi = np.asarray(lo, float), np.asarray(hi, float)
        self.o = (np.floor(lo / self.res + 1e-9) - pad) * self.res
        self.n = (np.ceil((hi - self.o) / self.res - 1e-9) + pad).astype(np.int64)

    @property
    def shape(self):
        return tuple(int(v) for v in self.n)

    def centers(self, idx):
        return self.o + (np.asarray(idx, float) + 0.5) * self.res

    def box_mask(self, lo, hi):
        ax = [self.o[a] + (np.arange(self.n[a]) + 0.5) * self.res for a in range(3)]
        m = [(ax[a] >= lo[a]) & (ax[a] <= hi[a]) for a in range(3)]
        return (m[0][:, None, None] & m[1][None, :, None] & m[2][None, None, :]).astype(np.uint8)


def voxelize_part(tris: np.ndarray, grid: VGrid) -> tuple[np.ndarray, int]:
    """Inside test by the winding number along +Z rays through the voxel centres. Returns (P uint8, bad)."""
    tris = np.ascontiguousarray(tris, dtype=np.float64)
    nx, ny, nz = grid.shape
    res = grid.res
    P = np.zeros(grid.shape, np.uint8)
    bad_all = None
    for jx, jy in ((res * 7.3e-5, res * 3.1e-5), (-res * 4.7e-5, res * 8.9e-5)):
        counts = np.zeros((nx, ny), np.int64)
        _vox_count(tris, grid.o[0], grid.o[1], res, jx, jy, counts)
        starts = np.zeros(nx * ny + 1, np.int64)
        np.cumsum(counts.ravel(), out=starts[1:])
        zs = np.zeros(int(starts[-1]))
        sg = np.zeros(int(starts[-1]), np.int64)
        _vox_fill(tris, grid.o[0], grid.o[1], res, jx, jy, starts, np.zeros((nx, ny), np.int64), zs, sg)
        Pk = np.zeros(grid.shape, np.uint8)
        bad = np.zeros((nx, ny), np.uint8)
        _vox_columns(counts, starts, zs, sg, grid.o[2], res, Pk, bad)
        if bad_all is None:
            P, bad_all = Pk, bad
        else:
            fix = (bad_all == 1) & (bad == 0)
            P[fix] = Pk[fix]
            bad_all = bad_all & bad
    nbad = int(bad_all.sum())
    if nbad:  # still inconsistent: copy the nearest good column along X
        for i, j in zip(*np.nonzero(bad_all)):
            for di in (1, -1, 2, -2, 3, -3):
                ii = i + di
                if 0 <= ii < nx and not bad_all[ii, j]:
                    P[i, j] = P[ii, j]
                    break
    return P, nbad


# ----------------------------------------------------------------------------------------------- frames
class Frame:
    """Maps the part grid into a setup's tool frame (WCS, tool axis = +Z = last array axis) and back."""

    def __init__(self, setup: Setup, grid: VGrid):
        self.grid = grid
        M = setup.matrix
        self.R = np.ascontiguousarray(M[:3, :3], dtype=np.float64)
        self.t = np.ascontiguousarray(M[:3, 3], dtype=np.float64)
        A = np.abs(self.R)
        perm = A.argmax(axis=1)
        self.aligned = bool(np.allclose(A.max(axis=1), 1.0, atol=1e-7) and len(set(perm.tolist())) == 3
                            and np.allclose(np.sort(A, axis=1)[:, :2], 0.0, atol=1e-7))
        self.tool_axis_part = self.R[2].copy()  # part-frame direction of the WCS +Z (spindle) axis
        if self.aligned:
            self.perm = perm
            self.sgn = np.sign(self.R[np.arange(3), perm]).astype(int)
            res = grid.res
            self.w0 = np.array([grid.o[perm[a]] + self.t[a] if self.sgn[a] > 0
                                else self.t[a] - grid.o[perm[a]] - grid.n[perm[a]] * res for a in range(3)])
            self.shape = tuple(int(grid.n[perm[a]]) for a in range(3))
        else:
            c = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], float)
            corners = grid.o + c * grid.n * grid.res
            w = corners @ self.R.T + self.t
            lo, hi = w.min(axis=0), w.max(axis=0)
            res = grid.res
            self.w0 = np.floor(lo / res) * res - res
            self.shape = tuple(int(v) for v in np.ceil((hi - self.w0) / res).astype(np.int64) + 1)

    def describe(self):
        ax = self.tool_axis_part
        names = "XYZ"
        if self.aligned:
            k = int(np.argmax(np.abs(ax)))
            return f"exact (tool axis = part {'+' if ax[k] > 0 else '-'}{names[k]})"
        return "resampled 3+2 (tool axis = part [{:.3f}, {:.3f}, {:.3f}])".format(*ax)

    def to_tool(self, V):
        if self.aligned:
            G = np.transpose(V, self.perm)
            for a in range(3):
                if self.sgn[a] < 0:
                    G = np.flip(G, a)
            return np.ascontiguousarray(G)
        G = np.zeros(self.shape, np.uint8)
        _sample_to_tool(V, self.grid.o, self.grid.res, self.R, self.t, self.w0, G)
        return G

    def to_part(self, G):
        assert self.aligned
        for a in range(3):
            if self.sgn[a] < 0:
                G = np.flip(G, a)
        return np.ascontiguousarray(np.transpose(G, np.argsort(self.perm)))

    def tool_flat_to_part_flat(self, idx):
        idx = np.asarray(idx, np.int64)
        tijk = np.stack(np.unravel_index(idx, self.shape), axis=1)
        pijk = np.zeros_like(tijk)
        for a in range(3):
            p = self.perm[a]
            pijk[:, p] = tijk[:, a] if self.sgn[a] > 0 else self.grid.n[p] - 1 - tijk[:, a]
        return np.ravel_multi_index(pijk.T, self.grid.shape)


# ----------------------------------------------------------------------------------------------- simulation
def _tool_kernel_arrays(job: Job, tool_ids):
    kind, R, a, b, flute, cyl_off, cyl_r = stock_sim.tool_arrays(job.tools, tool_ids, job.drill_model)
    n = len(tool_ids)
    body, body_r = np.zeros(n), np.zeros(n)
    for i, t in enumerate(tool_ids):
        tl = job.tools[t]
        body[i] = max(tl.stickout if tl.stickout > 0 else tl.flute_length, tl.flute_length)
        br = R[i]
        if 0 < tl.shank_diameter < tl.diameter and tl.sim_mode == "normal":
            br = min(br, tl.shank_diameter / 2.0)
        body_r[i] = br
    return dict(t_kind=kind, t_R=R, t_a=a, t_b=b, t_flute=flute, t_body=body, t_body_r=body_r,
                cyl_off=np.ascontiguousarray(cyl_off), cyl_r=np.ascontiguousarray(cyl_r))


def _parse_setup(job: Job, setup: Setup):
    lo, hi = job.stock_box_wcs(setup)
    home_z = float(hi[2] + job.machine["home_z_above_stock"])
    moves = []
    for f in setup.nc_files:
        moves.extend(Interpreter(job.units, home_z).run_file(f).program().moves)
    return moves


def _moves_report(moves, smove, mask, values, what, job=None, part=None):
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


def simulate_setup(job: Job, setup: Setup, V: np.ndarray, P: np.ndarray | None, grid: VGrid, cfg: dict,
                   buf_idx, buf_seg, moves=None) -> tuple[np.ndarray, dict, dict]:
    """Sweep one setup through the part-frame stock V. Returns (V, result, record) where record holds the
    removed in-part voxels (part flat index) with the segment and move mapping for culprit lookup."""
    t0 = time.perf_counter()
    tol = job.tol
    if moves is None:
        moves = _parse_setup(job, setup)
    t_parse = time.perf_counter() - t0
    missing = sorted({m.tool for m in moves if m.tool and m.tool not in job.tools})
    segs, stool, srapid, smove, tool_ids = stock_sim.build_segments(moves, job.tools, job.sim["arc_tolerance"])
    n = len(segs)
    frame = Frame(setup, grid)
    ta = _tool_kernel_arrays(job, tool_ids)
    out_depth, out_vol, out_holder, out_flute = (np.zeros(n) for _ in range(4))
    out_holder_k = np.full(n, -1, np.int64)
    buf_n = np.zeros(1, np.int64)
    t1 = time.perf_counter()
    G = frame.to_tool(V)
    top = np.zeros(G.shape[:2], np.int64)
    column_tops(G, top)
    use_p = P is not None and frame.aligned
    Pt = frame.to_tool(P) if use_p else np.zeros((1, 1, 1), np.uint8)
    last = np.full(G.shape, -1, np.int32) if not frame.aligned else np.zeros((1, 1, 1), np.int32)
    before = int(np.count_nonzero(G)) if frame.aligned else int(np.count_nonzero(V))
    t_frame = time.perf_counter() - t1
    t2 = time.perf_counter()
    if n:
        sweep_voxels(G, top, Pt, use_p, last, not frame.aligned, frame.w0[0], frame.w0[1], frame.w0[2],
                     grid.res, np.ascontiguousarray(segs), stool, srapid, ta["t_kind"], ta["t_R"], ta["t_a"],
                     ta["t_b"], ta["t_flute"], ta["t_body"], ta["t_body_r"], ta["cyl_off"], ta["cyl_r"],
                     float(tol["holder_clearance"]), float(job.sim["search_tolerance"]),
                     out_depth, out_vol, out_holder, out_holder_k, out_flute, buf_idx, buf_seg, buf_n)
    t_sweep = time.perf_counter() - t2
    t3 = time.perf_counter()
    if frame.aligned:
        V = frame.to_part(G)
        nrec = int(min(buf_n[0], len(buf_idx)))
        rec_idx = frame.tool_flat_to_part_flat(buf_idx[:nrec]) if nrec else np.zeros(0, np.int64)
        rec_seg = buf_seg[:nrec].copy()
    else:
        del G
        Pm = P if P is not None else np.zeros(V.shape, np.uint8)
        _map_back(V, Pm, grid.o, grid.res, frame.R, frame.t, frame.w0, last, np.ascontiguousarray(segs), stool,
                  ta["t_kind"], ta["t_R"], ta["t_a"], ta["t_b"], ta["t_flute"], ta["t_body"], ta["t_body_r"],
                  float(job.sim["search_tolerance"]), buf_idx, buf_seg, buf_n)
        nrec = int(min(buf_n[0], len(buf_idx)))
        rec_idx = buf_idx[:nrec].copy()
        rec_seg = buf_seg[:nrec].copy()
    after = int(np.count_nonzero(V))
    t_back = time.perf_counter() - t3
    vox = grid.res ** 3

    rapid = _moves_report(moves, smove, (srapid == 1) & (out_depth > tol["rapid_contact"]), out_depth,
                          "rapid removes material (rapid into stock); max = material thickness in one column")
    holder = _moves_report(moves, smove, out_holder > 0, out_holder,
                           "shank or holder inside remaining material", job=job, part=out_holder_k)
    flutes = _moves_report(moves, smove, out_flute > tol["gouge"], out_flute,
                           "material above the flute length (cut deeper than the flutes)")
    thread_idx = [i for i, t in enumerate(tool_ids) if job.tools[t].sim_mode == "thread"]
    thread = _moves_report(moves, smove, np.isin(stool, thread_idx) & (srapid == 0) & (out_depth > tol["rapid_contact"]),
                           out_depth, "tap/thread mill cut material inside 0.75 x its diameter (hole not drilled?)")
    per_tool = {}
    for i, t in enumerate(tool_ids):
        sel = (stool == i) & (srapid == 0)
        if sel.any():
            lens = np.linalg.norm(segs[sel, 3:] - segs[sel, :3], axis=1)
            cutting = lens[out_vol[sel] > 0].sum()
            per_tool[f"T{t}"] = {"removed_volume": round(float(out_vol[sel].sum()), 4),
                                 "feed_length": round(float(lens.sum()), 3),
                                 "air_feed_pct": round(100 * (1 - cutting / max(lens.sum(), 1e-12)), 1)}
    result = {
        "setup": setup.name, "wcs": setup.wcs, "nc_files": [f.name for f in setup.nc_files],
        "frame": frame.describe(), "tool_axis_part": [round(float(v), 6) for v in frame.tool_axis_part],
        "moves": len(moves), "segments": n,
        "removed_volume": round((before - after) * vox, 5),
        "missing_tools": missing,
        "rapid_through_stock": rapid, "holder_collision": holder, "beyond_flute_length": flutes,
        "thread_tool_in_solid": thread, "per_tool": per_tool,
        "tools_not_simulated": sorted({f"T{t}" for t in tool_ids if job.tools[t].sim_mode == "skip"}),
        "tools_without_holder": sorted({f"T{t}" for t in tool_ids if not job.tools[t].assembly()}),
        "in_part_removals_recorded": int(buf_n[0]),
        "timing_s": {"parse": round(t_parse, 3), "frame": round(t_frame, 3), "sweep": round(t_sweep, 3),
                     "map_back": round(t_back, 3), "total": round(time.perf_counter() - t0, 3)},
    }
    if buf_n[0] > len(buf_idx):
        result["note"] = f"culprit buffer full: {int(buf_n[0])} in-part removals, first {len(buf_idx)} traced"
    record = {"idx": rec_idx, "seg": rec_seg, "smove": smove, "moves": moves}
    return V, result, record


EDT_MAX_VOXELS = 24_000_000  # larger crops are block-reduced before the distance transform


def _block_reduce(m: np.ndarray, f: int, how: str) -> np.ndarray:
    pad = [(0, (-n) % f) for n in m.shape]
    mp = np.pad(m, pad, constant_values=(how == "all"))
    sh = []
    for n in mp.shape:
        sh += [n // f, f]
    r = mp.reshape(sh)
    return r.any(axis=(1, 3, 5)) if how == "any" else r.all(axis=(1, 3, 5))


def _edt_to(target: np.ndarray, pts_idx: np.ndarray, res: float, margin_vox: int) -> np.ndarray:
    """Euclidean distance from voxel centres pts_idx (N, 3) to the nearest True voxel centre of `target`,
    computed on a crop around the points (values >= margin are lower bounds). Large crops are reduced
    in blocks of f voxels first (distance then within about f * res)."""
    if len(pts_idx) == 0:
        return np.zeros(0)
    lo = np.maximum(pts_idx.min(axis=0) - margin_vox, 0)
    hi = np.minimum(pts_idx.max(axis=0) + margin_vox + 1, np.array(target.shape))
    crop = target[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]].astype(bool)
    if not crop.any():
        return np.full(len(pts_idx), margin_vox * res)
    f = max(1, int(math.ceil((crop.size / EDT_MAX_VOXELS) ** (1 / 3))))
    q = pts_idx - lo
    if f == 1:
        d = ndimage.distance_transform_edt(~crop, sampling=res)
        return d[q[:, 0], q[:, 1], q[:, 2]]
    c = _block_reduce(crop, f, "any")
    d = ndimage.distance_transform_edt(~c, sampling=res * f).astype(np.float32)
    q //= f
    return d[q[:, 0], q[:, 1], q[:, 2]].astype(float)


def _local_thickness(ids: np.ndarray, res: float) -> float:
    """2 x the largest inscribed radius of one voxel region (skin / web thickness)."""
    lo = ids.min(axis=0) - 1
    shape = ids.max(axis=0) - lo + 2
    f = max(1, int(math.ceil((float(np.prod(shape)) / EDT_MAX_VOXELS) ** (1 / 3))))
    m = np.zeros(tuple(shape), bool)
    q = ids - lo
    m[q[:, 0], q[:, 1], q[:, 2]] = True
    if f > 1:
        m = _block_reduce(m, f, "all")
    if not m.any():
        return float(f * res)
    return float(2 * ndimage.distance_transform_edt(m, sampling=res * f).max() - res * f)


_NB13 = np.array([(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1)
                  if (a, b, c) > (0, 0, 0)], np.int64)


def _label(mask_idx: np.ndarray) -> np.ndarray:
    """Connected-component id (26-connectivity) of voxels given as (N, 3) indices; memory O(N)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = len(mask_idx)
    if n == 0:
        return np.zeros(0, np.int64)
    lo = mask_idx.min(axis=0) - 1
    q = mask_idx - lo
    shape = q.max(axis=0) + 2
    flat = np.ravel_multi_index(q.T, shape)
    order = np.argsort(flat)
    fs = flat[order]
    rows, cols = [], []
    for off in _NB13:
        nf = np.ravel_multi_index((q + off).T, shape)
        pos = np.minimum(np.searchsorted(fs, nf), n - 1)
        hit = fs[pos] == nf
        rows.append(np.nonzero(hit)[0])
        cols.append(order[pos[hit]])
    r = np.concatenate(rows)
    c = np.concatenate(cols)
    g = coo_matrix((np.ones(len(r), np.int8), (r, c)), shape=(n, n))
    _, lab = connected_components(g, directed=False)
    return lab.astype(np.int64)


def _regions(mask_idx: np.ndarray, values: np.ndarray, grid: VGrid, top=8, extra=None):
    """Connected regions (26-connectivity) of voxels given as (N, 3) indices, sorted by max value."""
    if len(mask_idx) == 0:
        return [], np.zeros(0, np.int64)
    lid = _label(mask_idx)
    order = np.argsort(-values)
    out = []
    seen = set()
    for p in order:
        r = int(lid[p])
        if r in seen:
            continue
        seen.add(r)
        sel = lid == r
        ids = mask_idx[sel]
        c = grid.centers(ids)
        row = {"max": round(float(values[p]), 5), "voxels": int(sel.sum()),
               "volume": round(float(sel.sum()) * grid.res ** 3, 6),
               "worst_xyz": [round(float(v), 4) for v in grid.centers(mask_idx[p])],
               "bbox_min": [round(float(v), 4) for v in c.min(axis=0) - grid.res / 2],
               "bbox_max": [round(float(v), 4) for v in c.max(axis=0) + grid.res / 2]}
        if extra is not None:
            row.update(extra(sel))
        out.append(row)
        if len(out) >= top:
            break
    return out, lid


def compare_final(job: Job, grid: VGrid, S, P, V, tris, cfg, records) -> dict:
    """Gouge / edge-break / leftover in the part frame after all setups."""
    res = grid.res
    tol = job.tol
    cap = max(float(cfg["distance_cap"]), 3 * res, float(cfg["edge_break_depth"]) + res)
    margin = max(int(math.ceil(cfg["edt_margin"] / res)), 4)
    t0 = time.perf_counter()
    dist_mesh = DistanceField(tris, cap, triangles=True)
    edges, estats = sharp_convex_edges(tris, cfg["sharp_edge_deg"], weld=1e-7 * max(float(np.ptp(tris)), 1.0))
    eb_cap = max(float(cfg["edge_break_dist"]), res) * 1.0001
    dist_edge = DistanceField(edges, eb_cap, triangles=False) if len(edges) else None
    t_fields = time.perf_counter() - t0

    part_vox = int(P.sum())
    part_vol = part_vox * res ** 3
    Sb, Pb, Vb = S.view(bool), P.view(bool), V.view(bool)

    # culprit lookup: part flat index -> list of (setup, file, line)
    rec_idx = np.concatenate([r["idx"] for r in records]) if records else np.zeros(0, np.int64)
    rec_setup = np.concatenate([np.full(len(r["idx"]), k) for k, r in enumerate(records)]) if records else np.zeros(0, np.int64)
    rec_seg = np.concatenate([r["seg"] for r in records]) if records else np.zeros(0, np.int64)

    def lines_for(flat):
        if len(flat) == 0 or len(rec_idx) == 0:
            return []
        sel = np.isin(rec_idx, flat)
        cnt = {}
        for k, s in zip(rec_setup[sel], rec_seg[sel]):
            r = records[int(k)]
            m = r["moves"][r["smove"][int(s)]]
            key = (int(k), m.file, m.line)
            if key not in cnt:
                cnt[key] = [0, m.text, m.tool]
            cnt[key][0] += 1
        rows = sorted(cnt.items(), key=lambda kv: -kv[1][0])[:10]
        return [{"setup": job.setups[k].name, "file": f, "line": ln, "voxels": c, "tool": tl, "text": tx}
                for (k, f, ln), (c, tx, tl) in rows]

    # ---- gouge / edge break
    gi = np.argwhere(Sb & Pb & ~Vb)
    gc = grid.centers(gi)
    gd = dist_mesh(gc)
    deep = gd >= cap - 1e-12
    if deep.any():
        gd[deep] = np.maximum(cap, _edt_to(~Pb, gi[deep], res, margin) - res / 2)
    real = gd > tol["gouge"]
    ed = dist_edge(gc) if dist_edge is not None else np.full(len(gc), np.inf)
    eb_vox = real & (gd <= cfg["edge_break_depth"]) & (ed <= cfg["edge_break_dist"])
    # a connected removed region is an edge break only if all of it lies in the edge band
    eb = np.zeros(len(gi), bool)
    ri = np.nonzero(real)[0]
    if len(ri):
        lid = _label(gi[ri])
        all_eb = np.ones(lid.max() + 1, bool)
        np.logical_and.at(all_eb, lid, eb_vox[ri])
        eb[ri] = all_eb[lid]
    gouge = real & ~eb
    flat_all = np.ravel_multi_index(gi.T, grid.shape) if len(gi) else np.zeros(0, np.int64)

    def region_lines(flat_sel):
        return lambda sel: {"lines": lines_for(flat_sel[sel])[:5]}

    g_idx, g_val, g_flat = gi[gouge], gd[gouge] + res / 2, flat_all[gouge]
    g_regions, _ = _regions(g_idx, g_val, grid, extra=region_lines(g_flat))
    e_idx, e_val, e_flat = gi[eb], gd[eb] + res / 2, flat_all[eb]
    e_regions, _ = _regions(e_idx, e_val, grid, top=5, extra=region_lines(e_flat))
    gouge_res = {"voxels": int(gouge.sum()), "max_depth": round(float(g_val.max(initial=0.0)), 5),
                 "volume": round(float(gouge.sum()) * res ** 3, 6),
                 "volume_pct": round(100 * float(gouge.sum()) / max(part_vox, 1), 4),
                 "regions": g_regions, "lines": lines_for(g_flat),
                 "ignored_below_tol": int((~real).sum())}
    eb_res = {"voxels": int(eb.sum()), "max_depth": round(float(e_val.max(initial=0.0)), 5),
              "regions": e_regions, "lines": lines_for(e_flat)}

    # ---- leftover
    li = np.argwhere(Vb & ~Pb)
    lc = grid.centers(li)
    ld = dist_mesh(lc)
    deep = ld >= cap - 1e-12
    if deep.any():
        ld[deep] = np.maximum(cap, _edt_to(Pb, li[deep], res, margin) - res / 2)
    lreal = ld > tol["leftover"]
    l_idx, l_val = li[lreal], ld[lreal] + res / 2
    # local thickness of the leftover itself (skin / web): 2 x inscribed radius, per reported region
    l_regions, _ = _regions(l_idx, l_val, grid, top=10,
                            extra=lambda sel: {"local_thickness": round(_local_thickness(l_idx[sel], res), 5)})
    lvol = float(lreal.sum()) * res ** 3
    left_res = {"voxels": int(lreal.sum()), "max_thickness": round(float(l_val.max(initial=0.0)), 5),
                "max_local_thickness": max([r["local_thickness"] for r in l_regions], default=0.0),
                "volume": round(lvol, 6), "volume_pct": round(100 * lvol / max(part_vol, 1e-12), 4),
                "regions": l_regions, "ignored_below_tol": int((~lreal).sum())}
    missing = int((Pb & ~Sb).sum())
    return {"gouge": gouge_res, "edge_break": eb_res, "leftover": left_res,
            "part_voxels": part_vox, "part_volume": round(part_vol, 5),
            "part_outside_stock_voxels": missing, "mesh_edges": estats,
            "distance_cap": round(cap, 5), "timing_s": {"fields": round(t_fields, 3),
                                                        "total": round(time.perf_counter() - t0, 3)},
            "_arrays": {"gouge": (g_idx, g_val), "edge_break": (e_idx, e_val), "leftover": (l_idx, l_val)}}


def run_voxel(job_dir, out_dir=None, *, res=None, png=False, overrides=None, write=True) -> dict:
    t0 = time.perf_counter()
    job = load_job(job_dir)
    cfg = voxel_config(job, overrides)
    res = float(res or cfg["resolution"])
    tris = stock_sim.part_triangles(job)
    lo, hi = job.stock_min.copy(), job.stock_max.copy()
    if tris is not None:
        lo = np.minimum(lo, tris.reshape(-1, 3).min(axis=0))
        hi = np.maximum(hi, tris.reshape(-1, 3).max(axis=0))
    grid = VGrid(lo, hi, res, int(cfg["pad_voxels"]))
    S = grid.box_mask(job.stock_min, job.stock_max)
    t1 = time.perf_counter()
    P, nbad = voxelize_part(tris, grid) if tris is not None else (None, 0)
    t_vox = time.perf_counter() - t1
    V = S.copy()
    cap = int(cfg["buffer_capacity"])
    buf_idx = np.zeros(cap, np.int64)
    buf_seg = np.zeros(cap, np.int64)
    setups, records = [], []
    for s in job.setups:
        V, r, rec = simulate_setup(job, s, V, P, grid, cfg, buf_idx, buf_seg)
        setups.append(r)
        records.append(rec)
    u = job.units
    report = {"job": str(Path(job_dir).resolve()), "units": u, "status": "pass", "resolution": res,
              "grid": {"origin": [round(float(v), 6) for v in grid.o], "shape": list(grid.shape),
                       "voxels": int(np.prod(grid.shape)),
                       "memory_mb_per_grid": round(float(np.prod(grid.shape)) / 2 ** 20, 1)},
              "stock_volume": round(float(S.sum()) * res ** 3, 5), "setups": setups,
              "config": {k: v for k, v in cfg.items()},
              "timing_s": {"voxelize_part": round(t_vox, 3)}}
    if nbad:
        report["voxelize_warning"] = f"{nbad} ray columns had an unbalanced winding (mesh not watertight?)"
    fails, warns = [], []
    for r in setups:
        tag = r["setup"]
        if r["missing_tools"]:
            fails.append(f"{tag}: tools missing from the tool list: {r['missing_tools']}")
        if r["tools_not_simulated"]:
            warns.append(f"{tag}: undercut tools not simulated: {r['tools_not_simulated']}")
        if r["tools_without_holder"]:
            warns.append(f"{tag}: no holder data, holder collisions not checked: {r['tools_without_holder']}")
        for key in ("rapid_through_stock", "holder_collision", "beyond_flute_length", "thread_tool_in_solid"):
            x = r[key]
            if x["count"]:
                w = x["worst"][0]
                fails.append(f"{tag} {key}: {x['count']} line(s); worst {w['max']:.4f} {u} at {w['file']}:"
                             f"{w['line']} T{w['tool']}" + (f" ({w['part']})" if w.get("part") else "")
                             + f" `{w['text']}`")
        if not r["frame"].startswith("exact"):
            warns.append(f"{tag}: {r['frame']}: removal mapped back with an exact point test, but detections "
                         f"(rapids, holder, flutes) are on a resampled grid (+-1 voxel)")
    if tris is not None:
        fin = compare_final(job, grid, S, P, V, tris, cfg, records)
        arrays = fin.pop("_arrays")
        # attribute gouge / edge-break lines to setups
        for r in setups:
            r["gouge_lines"] = [x for x in fin["gouge"]["lines"] if x["setup"] == r["setup"]]
            r["edge_break_lines"] = [x for x in fin["edge_break"]["lines"] if x["setup"] == r["setup"]]
        g, e, lf = fin["gouge"], fin["edge_break"], fin["leftover"]
        if g["voxels"] >= int(cfg["gouge_min_voxels"]):
            rg = g["regions"][0] if g["regions"] else {}
            lines = ", ".join(f"{x['setup']} {x['file']}:{x['line']}" for x in g["lines"][:3])
            fails.append(f"gouge: max {g['max_depth']:.4f} {u} (+-{res / 2:g}), {g['voxels']} voxels "
                         f"({g['volume_pct']}% of part volume), worst at part XYZ {rg.get('worst_xyz')}; lines {lines}")
        if e["voxels"]:
            lines = ", ".join(f"{x['setup']} {x['file']}:{x['line']}" for x in e["lines"][:3])
            warns.append(f"edge_break: {e['voxels']} voxels removed within {cfg['edge_break_dist']:g} {u} of sharp "
                         f"edges, max {e['max_depth']:.4f} deep ({len(e['regions'])}+ regions); lines {lines}. "
                         f"Chamfer/deburr not in the model?")
        big = [x for x in lf["regions"] if x["max"] > cfg["leftover_max"]
               and x["voxels"] >= int(cfg["leftover_min_region_voxels"])]
        if lf["voxels"]:
            rg = lf["regions"][0] if lf["regions"] else {}
            msg = (f"leftover: max {lf['max_thickness']:.4f} {u} thick (+-{res / 2:g}), {lf['volume_pct']}% of "
                   f"part volume, {len(big)} region(s) over {cfg['leftover_max']:g}; worst at part XYZ "
                   f"{rg.get('worst_xyz')}")
            (fails if (big or lf["volume_pct"] > cfg["leftover_volume_pct_max"]) else warns).append(msg)
        if fin["part_outside_stock_voxels"]:
            warns.append(f"{fin['part_outside_stock_voxels']} part voxels lie outside the stock box")
        report["final"] = fin
    else:
        arrays = None
        warns.append("no part model: gouge/leftover not compared")
    report["failures"], report["warnings"] = fails, warns
    report["status"] = "fail" if fails else "pass"
    report["runtime_s"] = round(time.perf_counter() - t0, 2)
    if write:
        out = Path(out_dir) if out_dir else Path(job_dir) / "check_out"
        out.mkdir(parents=True, exist_ok=True)
        if arrays is not None:
            np.savez_compressed(out / "voxel_final.npz", res=res, origin=grid.o,
                                **{f"{k}_xyz": grid.centers(v[0]).astype(np.float32) for k, v in arrays.items()},
                                **{f"{k}_value": v[1].astype(np.float32) for k, v in arrays.items()})
            if png:
                _png(out / "voxel_final.png", grid, P, V, S, arrays, u, report)
                report["png"] = str(out / "voxel_final.png")
        (out / "voxel_report.json").write_text(json.dumps(report, indent=2, default=_json_default))
        (out / "voxel_report.md").write_text(markdown(report))
    return report


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def markdown(rep: dict) -> str:
    u = rep["units"]
    L = [f"# Voxel check: {rep['status'].upper()}", "",
         f"Job `{rep['job']}` ({u}), {rep['resolution']:g} {u} voxels, grid {rep['grid']['shape']} "
         f"({rep['grid']['voxels'] / 1e6:.1f} M), {rep['runtime_s']} s", ""]
    for s in rep["setups"]:
        L.append(f"## {s['setup']} ({s['wcs']}): {s['frame']}")
        L.append(f"- {', '.join(s['nc_files'])}: {s['moves']} moves, {s['segments']} segments, removed "
                 f"{s['removed_volume']} {u}^3, sweep {s['timing_s']['sweep']} s")
        L.append(f"- rapids into stock {s['rapid_through_stock']['count']}, holder/shank hits "
                 f"{s['holder_collision']['count']}, beyond flute {s['beyond_flute_length']['count']}, "
                 f"thread tool in solid {s['thread_tool_in_solid']['count']}")
        for key in ("rapid_through_stock", "holder_collision", "beyond_flute_length", "thread_tool_in_solid"):
            for w in s[key]["worst"][:3]:
                L.append(f"  - {key}: {w['max']:.4f} at {w['file']}:{w['line']} T{w['tool']}"
                         + (f" ({w['part']})" if w.get("part") else "") + f" `{w['text']}`")
        for w in s.get("gouge_lines", [])[:3]:
            L.append(f"  - gouge: {w['voxels']} voxels from {w['file']}:{w['line']} T{w['tool']} `{w['text']}`")
        L.append("")
    fin = rep.get("final")
    if fin:
        g, e, lf = fin["gouge"], fin["edge_break"], fin["leftover"]
        L.append("## Final part vs. stock (part frame, all setups)")
        L.append(f"- gouge: {g['voxels']} voxels, max depth {g['max_depth']} {u}, {g['volume_pct']}% of part volume")
        for r in g["regions"][:5]:
            L.append(f"  - {r['voxels']} vox, max {r['max']} at {r['worst_xyz']}; "
                     + ", ".join(f"{x['setup']} {x['file']}:{x['line']}" for x in r.get("lines", [])[:3]))
        L.append(f"- edge break (warning): {e['voxels']} voxels, max depth {e['max_depth']} {u}")
        L.append(f"- leftover: {lf['voxels']} voxels, max {lf['max_thickness']} {u} from the part surface, "
                 f"local thickness up to {lf['max_local_thickness']} {u}, {lf['volume_pct']}% of part volume")
        for r in lf["regions"][:6]:
            L.append(f"  - {r['voxels']} vox ({r['volume']} {u}^3), max {r['max']} from part (local "
                     f"{r['local_thickness']}) at {r['worst_xyz']}, bbox {r['bbox_min']}..{r['bbox_max']}")
        L.append("")
    if rep["failures"]:
        L += ["## Failures"] + [f"- {x}" for x in rep["failures"]] + [""]
    if rep["warnings"]:
        L += ["## Warnings"] + [f"- {x}" for x in rep["warnings"]] + [""]
    return "\n".join(L)


def _png(path, grid, P, V, S, arrays, units, rep):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    res = grid.res
    ext_axes = [(grid.o[a], grid.o[a] + grid.n[a] * res) for a in range(3)]
    views = [("top (XY)", 2, (0, 1)), ("front (XZ)", 1, (0, 2)), ("side (YZ)", 0, (1, 2))]
    fig = plt.figure(figsize=(16, 10), constrained_layout=True)
    gs = fig.add_gridspec(2, 3)
    g_idx, g_val = arrays["gouge"]
    l_idx, l_val = arrays["leftover"]
    e_idx, _ = arrays["edge_break"]
    for c, (title, axis, (h, v)) in enumerate(views):
        ax = fig.add_subplot(gs[0, c])
        sil = P.any(axis=axis).T.astype(float)
        extent = [*ext_axes[h], *ext_axes[v]]
        ax.imshow(np.where(sil > 0, 0.25, np.nan), origin="lower", extent=extent, cmap="Greys", vmin=0, vmax=1)
        for idx, val, cmap in ((l_idx, l_val, "Blues"), (g_idx, g_val, "Reds")):
            if len(idx):
                img = np.full((grid.n[h], grid.n[v]), np.nan)
                np.fmax.at(img, (idx[:, h], idx[:, v]), val)
                ax.imshow(img.T, origin="lower", extent=extent, cmap=cmap, vmin=0,
                          vmax=max(float(np.nanmax(img)), 1e-9), alpha=0.9)
        if len(e_idx):
            cc = grid.centers(e_idx)
            ax.scatter(cc[:, h], cc[:, v], s=1, c="orange", alpha=0.5)
        ax.set_title(f"{title}: leftover (blue), gouge (red), edge break (orange)", fontsize=9)
        ax.set_xlabel("XYZ"[h] + f" ({units})")
        ax.set_ylabel("XYZ"[v] + f" ({units})")
        ax.set_aspect("equal")
    ax3 = fig.add_subplot(gs[1, :], projection="3d")
    rng = np.random.default_rng(0)
    for idx, val, color, label in ((l_idx, l_val, "tab:blue", "leftover"), (g_idx, g_val, "tab:red", "gouge"),
                                   (e_idx, None, "orange", "edge break")):
        if len(idx):
            take = idx if len(idx) <= 40000 else idx[rng.choice(len(idx), 40000, replace=False)]
            cc = grid.centers(take)
            ax3.scatter(cc[:, 0], cc[:, 1], cc[:, 2], s=1, c=color, label=f"{label} ({len(idx)} vox)")
    # part outline: surface voxels, subsampled
    surf = P.astype(bool) & ~ndimage.binary_erosion(P.astype(bool))
    si = np.argwhere(surf)
    if len(si):
        si = si[rng.choice(len(si), min(len(si), 30000), replace=False)]
        cc = grid.centers(si)
        ax3.scatter(cc[:, 0], cc[:, 1], cc[:, 2], s=0.3, c="0.7", alpha=0.15)
    lo = grid.o
    hi = grid.o + grid.n * res
    span = (hi - lo).max()
    mid = (hi + lo) / 2
    ax3.set_xlim(mid[0] - span / 2, mid[0] + span / 2)
    ax3.set_ylim(mid[1] - span / 2, mid[1] + span / 2)
    ax3.set_zlim(mid[2] - span / 2, mid[2] + span / 2)
    ax3.view_init(elev=28, azim=-55)
    ax3.legend(loc="upper left", fontsize=8)
    ax3.set_title(f"isometric, part frame ({rep['status'].upper()})", fontsize=10)
    fig.savefig(path, dpi=100)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m checks.voxel_sim")
    ap.add_argument("job")
    ap.add_argument("--res", type=float, help="voxel size in job units (default 0.01 in)")
    ap.add_argument("--png", action="store_true", help="write voxel_final.png")
    ap.add_argument("--out", help="output folder (default JOB/check_out)")
    a = ap.parse_args(argv)
    rep = run_voxel(a.job, a.out, res=a.res, png=a.png)
    out = Path(a.out) if a.out else Path(a.job) / "check_out"
    print((out / "voxel_report.md").read_text())
    return 0 if rep["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
