"""Height-field (Z-map) kernels: tool sweep material removal and top-down mesh rasterization.

Grid convention: H[iy, ix] is the height at cell centre (x0 + (ix + .5) * res, y0 + (iy + .5) * res).
Air is -1e30. Kernels are compiled with numba when available (pure-Python fallback is ~100x slower).
"""
from __future__ import annotations

import math

import numpy as np

try:
    from numba import njit
except ImportError:  # pragma: no cover
    def njit(*a, **k):
        if a and callable(a[0]):
            return a[0]
        return lambda f: f

AIR = -1e30


@njit(cache=True, fastmath=False)
def _h(kind, R, a, b, d):
    """Height of the tool's cutting surface above its tip at radial distance d (d <= R)."""
    if kind == 0:
        return 0.0
    if kind == 1:
        q = R * R - d * d
        return R - math.sqrt(q if q > 0 else 0.0)
    if kind == 2:
        c = R - a
        if d <= c:
            return 0.0
        e = d - c
        q = a * a - e * e
        return a - math.sqrt(q if q > 0 else 0.0)
    # cone: a = cot(half angle), b = tip flat radius
    if d <= b:
        return 0.0
    return (d - b) * a


@njit(cache=True)
def sweep(H, last, x0, y0, res, segs, seg_tool, seg_rapid,
          t_kind, t_R, t_a, t_b, t_flute, cyl_off, cyl_r, zmax, holder_tol, ztol,
          out_depth, out_vol, out_holder, out_holder_k, out_flute):
    """Sweep every segment in order through the height field.

    segs: (N, 6) x0 y0 z0 x1 y1 z1 of the tool tip. seg_tool: index into the t_* arrays (-1 = skip).
    cyl_off / cyl_r: (tools, K) shank/holder cylinders (offset above the tip, radius; radius 0 = unused).
    Per segment outputs: max depth removed, removed volume, max shank/holder intrusion and which cylinder,
    max depth beyond flutes.
    `last` records the index of the last segment that lowered each cell.
    """
    ny, nx = H.shape
    cell_area = res * res
    for s in range(segs.shape[0]):
        ti = seg_tool[s]
        if ti < 0:
            continue
        ax, ay, az, bx, by, bz = segs[s, 0], segs[s, 1], segs[s, 2], segs[s, 3], segs[s, 4], segs[s, 5]
        kind, R, a, b = t_kind[ti], t_R[ti], t_a[ti], t_b[ti]
        dx, dy, dz = bx - ax, by - ay, bz - az
        L2 = dx * dx + dy * dy
        L = math.sqrt(L2)
        zmin_seg = min(az, bz)
        # ---- material removal
        if zmin_seg < zmax and R > 0:
            i0 = max(int(math.floor((min(ax, bx) - R - x0) / res)), 0)
            i1 = min(int(math.ceil((max(ax, bx) + R - x0) / res)), nx - 1)
            j0 = max(int(math.floor((min(ay, by) - R - y0) / res)), 0)
            j1 = min(int(math.ceil((max(ay, by) + R - y0) / res)), ny - 1)
            maxd = 0.0
            vol = 0.0
            for j in range(j0, j1 + 1):
                py = y0 + (j + 0.5) * res
                for i in range(i0, i1 + 1):
                    px = x0 + (i + 0.5) * res
                    old = H[j, i]
                    if old <= AIR * 0.5:
                        continue
                    if L2 < 1e-18:
                        d = math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
                        if d > R:
                            continue
                        zb = zmin_seg + _h(kind, R, a, b, d)
                    else:
                        tl = ((px - ax) * dx + (py - ay) * dy) / L2
                        tc = min(max(tl, 0.0), 1.0)
                        ex, ey = ax + tc * dx - px, ay + tc * dy - py
                        dmin = math.sqrt(ex * ex + ey * ey)
                        if dmin > R:
                            continue
                        # lower bound: lowest tip height with the closest approach
                        if zmin_seg + _h(kind, R, a, b, dmin) >= old:
                            continue
                        if abs(dz) < 1e-12:
                            zb = az + _h(kind, R, a, b, dmin)
                        else:
                            qx, qy = ax + tl * dx - px, ay + tl * dy - py
                            dp2 = qx * qx + qy * qy
                            hw = math.sqrt(max(R * R - dp2, 0.0)) / L
                            ta = max(tl - hw, 0.0)
                            tb = min(tl + hw, 1.0)
                            if kind == 0:
                                zb = min(az + dz * ta, az + dz * tb)
                            else:
                                # f(t) = z(t) + h(d(t)) is convex on [ta, tb]: golden-section search
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
                                za = az + dz * ta + _h(kind, R, a, b, min(math.sqrt(dp2 + ea * ea), R))
                                zc = az + dz * tb + _h(kind, R, a, b, min(math.sqrt(dp2 + eb * eb), R))
                                zb = min(zb, za, zc)
                    if zb < old:
                        dd = old - zb
                        if dd > maxd:
                            maxd = dd
                        vol += dd
                        H[j, i] = zb
                        last[j, i] = s
            out_depth[s] = maxd
            out_vol[s] = vol * cell_area
            if seg_rapid[s] == 0 and maxd > t_flute[ti]:
                out_flute[s] = maxd - t_flute[ti]
        # ---- shank / holder: stacked cylinders `cyl_off` above the tip
        worst = 0.0
        worst_k = -1
        for k in range(cyl_r.shape[1]):
            Rh = cyl_r[ti, k]
            off = cyl_off[ti, k]
            if Rh <= 0 or zmin_seg + off >= zmax:
                continue
            i0 = max(int(math.floor((min(ax, bx) - Rh - x0) / res)), 0)
            i1 = min(int(math.ceil((max(ax, bx) + Rh - x0) / res)), nx - 1)
            j0 = max(int(math.floor((min(ay, by) - Rh - y0) / res)), 0)
            j1 = min(int(math.ceil((max(ay, by) + Rh - y0) / res)), ny - 1)
            for j in range(j0, j1 + 1):
                py = y0 + (j + 0.5) * res
                for i in range(i0, i1 + 1):
                    hz = H[j, i]
                    if hz <= AIR * 0.5 or hz <= zmin_seg + off + holder_tol:
                        continue
                    px = x0 + (i + 0.5) * res
                    if L2 < 1e-18:
                        tc = 0.0
                    else:
                        tc = min(max(((px - ax) * dx + (py - ay) * dy) / L2, 0.0), 1.0)
                    qx, qy = ax + tc * dx - px, ay + tc * dy - py
                    if qx * qx + qy * qy > Rh * Rh:
                        continue
                    zh = az + dz * tc + off
                    if hz > zh + holder_tol and hz - zh > worst:
                        worst = hz - zh
                        worst_k = k
        out_holder[s] = worst
        out_holder_k[s] = worst_k


@njit(cache=True)
def raster_top(H, x0, y0, res, tris):
    """Top-down Z-buffer: H = max(H, z of any triangle covering the cell centre). tris: (M, 3, 3)."""
    ny, nx = H.shape
    for k in range(tris.shape[0]):
        ax, ay, az = tris[k, 0, 0], tris[k, 0, 1], tris[k, 0, 2]
        bx, by, bz = tris[k, 1, 0], tris[k, 1, 1], tris[k, 1, 2]
        cx, cy, cz = tris[k, 2, 0], tris[k, 2, 1], tris[k, 2, 2]
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-14:
            continue
        i0 = max(int(math.floor((min(ax, bx, cx) - x0) / res - 0.5)), 0)
        i1 = min(int(math.ceil((max(ax, bx, cx) - x0) / res - 0.5)), nx - 1)
        j0 = max(int(math.floor((min(ay, by, cy) - y0) / res - 0.5)), 0)
        j1 = min(int(math.ceil((max(ay, by, cy) - y0) / res - 0.5)), ny - 1)
        eps = 1e-9
        for j in range(j0, j1 + 1):
            py = y0 + (j + 0.5) * res
            for i in range(i0, i1 + 1):
                px = x0 + (i + 0.5) * res
                w1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
                w2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
                w3 = 1.0 - w1 - w2
                if w1 < -eps or w2 < -eps or w3 < -eps:
                    continue
                z = w1 * az + w2 * bz + w3 * cz
                if z > H[j, i]:
                    H[j, i] = z


class Grid:
    def __init__(self, xmin, ymin, xmax, ymax, res):
        self.res = float(res)
        self.x0 = math.floor(xmin / res) * res
        self.y0 = math.floor(ymin / res) * res
        self.nx = int(math.ceil((xmax - self.x0) / res))
        self.ny = int(math.ceil((ymax - self.y0) / res))

    def centers(self):
        xs = self.x0 + (np.arange(self.nx) + 0.5) * self.res
        ys = self.y0 + (np.arange(self.ny) + 0.5) * self.res
        return xs, ys

    def empty(self, value=AIR):
        return np.full((self.ny, self.nx), value, dtype=np.float64)

    def xy(self, iy, ix):
        return self.x0 + (ix + 0.5) * self.res, self.y0 + (iy + 0.5) * self.res

    def extent(self):
        return [self.x0, self.x0 + self.nx * self.res, self.y0, self.y0 + self.ny * self.res]
