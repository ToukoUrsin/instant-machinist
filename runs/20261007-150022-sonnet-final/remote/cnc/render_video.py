"""Render a material-removal video of a posted NC program with the Helios checks simulator.

Every frame is the real Z-map stock after sweeping the program's moves up to that point (checks.zmap.sweep,
the same kernel the verifier uses). The HUD reads the active move (tool, feed/rapid, G-code line) and a cycle
time estimate from the move lengths and feeds. The last frames paint gouges red and leftover blue from the
part model and circle the worst region.

    python render_video.py JOB_DIR OUT_DIR --title "Attempt 1" [--frames 160]

Writes OUT_DIR/frames/f00000.png ... and OUT_DIR/summary.json; ffmpeg turns frames into an mp4.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

for _root in (Path(__file__).resolve().parent / "vendor", Path(__file__).resolve().parent.parent / "vendor"):
    if (_root / "checks").is_dir():
        sys.path.insert(0, str(_root))
        break

from checks import stock_sim, zmap  # noqa: E402
from checks.gcode import Interpreter  # noqa: E402
from checks.job import load_job  # noqa: E402

BG = "#0b0f17"
FONT = "DejaVu Sans"
MONO = "DejaVu Sans Mono"
ALU_TOP = np.array([0.82, 0.84, 0.88])
ALU_FLOOR = np.array([0.50, 0.55, 0.62])
ALU_SIDE = np.array([0.60, 0.63, 0.68])
HOLE = np.array([0.03, 0.04, 0.06])
RED = np.array([0.96, 0.22, 0.22])
BLUE = np.array([0.23, 0.51, 0.96])
MAX_QUADS = 320          # display grid cells along the long side (sim grid stays full res)
AZ0, AZ1, ELEV = -65.0, -45.0, 34.0


def _pool(a, st, fn):
    """Block-reduce a 2D array by st (pads the edge with the last row/col)."""
    if st == 1:
        return a
    ny, nx = a.shape
    py, px = (-ny) % st, (-nx) % st
    a = np.pad(a, ((0, py), (0, px)), mode="edge")
    return fn(fn(a.reshape(a.shape[0] // st, st, a.shape[1] // st, st), axis=3), axis=1)


def _unit(v):
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("out")
    ap.add_argument("--title", default="")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--frames", type=int, default=160)
    ap.add_argument("--hold", type=int, default=45)
    ap.add_argument("--res", type=float, default=0.02)
    a = ap.parse_args()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource
    from matplotlib.patches import FancyBboxPatch, Rectangle
    from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection
    from scipy import ndimage

    job = load_job(a.job)
    setup = job.setups[0]
    out = Path(a.out)
    (out / "frames").mkdir(parents=True, exist_ok=True)
    for f in (out / "frames").glob("*.png"):
        f.unlink()

    lo, hi = job.stock_box_wcs(setup)
    home_z = float(hi[2] + job.machine["home_z_above_stock"])
    moves = []
    for f in setup.nc_files:
        moves += Interpreter(job.units, home_z).run_file(f).program().moves
    segs, stool, srapid, smove, tool_ids = stock_sim.build_segments(moves, job.tools, job.sim["arc_tolerance"])
    kind, R, ta, tb, flute, cyl_off, cyl_r = stock_sim.tool_arrays(job.tools, tool_ids, job.drill_model)

    m = job.sim["margin"]
    grid = zmap.Grid(lo[0] - m, lo[1] - m, hi[0] + m, hi[1] + m, a.res)
    xs, ys = grid.centers()
    inside = (xs[None, :] >= lo[0]) & (xs[None, :] <= hi[0]) & (ys[:, None] >= lo[1]) & (ys[:, None] <= hi[1])
    H = np.where(inside, hi[2], zmap.AIR)
    last = np.full(H.shape, -1, np.int64)
    zmax = float(hi[2])
    zlo = float(lo[2])

    # part map for the final gouge/leftover paint
    tris = stock_sim.part_triangles(job)
    tw = setup.to_wcs(tris.reshape(-1, 3)).reshape(-1, 3, 3)
    P = grid.empty()
    zmap.raster_top(P, grid.x0, grid.y0, grid.res, np.ascontiguousarray(tw))
    has_part = P > zmap.AIR * 0.5

    rows = np.nonzero(inside.any(1))[0]
    cols = np.nonzero(inside.any(0))[0]
    crop = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
    cny, cnx = rows[-1] - rows[0] + 1, cols[-1] - cols[0] + 1
    st = max(1, int(math.ceil(max(cny, cnx) / MAX_QUADS)))

    # display grid: block centres of the crop, padded with a skirt ring at the stock bottom (the side walls)
    xd = _pool(xs[crop[1]][None, :], st, np.mean)[0]
    yd = _pool(ys[crop[0]][:, None], st, np.mean)[:, 0]
    xd = np.concatenate([[lo[0]], np.clip(xd, lo[0], hi[0]), [hi[0]]])
    yd = np.concatenate([[lo[1]], np.clip(yd, lo[1], hi[1]), [hi[1]]])
    xd[1], xd[-2], yd[1], yd[-2] = lo[0], hi[0], lo[1], hi[1]
    XD, YD = np.meshgrid(xd, yd)
    dny, dnx = XD.shape
    skirt = np.zeros((dny - 1, dnx - 1), bool)
    skirt[0, :] = skirt[-1, :] = skirt[:, 0] = skirt[:, -1] = True

    # brushed-aluminium streaks along X (fixed seed: identical texture every frame)
    rng = np.random.default_rng(7)
    streak = ndimage.gaussian_filter1d(rng.normal(size=(dny - 1, dnx - 1)), 30, axis=1, mode="wrap")
    streak += 0.5 * ndimage.gaussian_filter1d(rng.normal(size=streak.shape), 1.2, axis=0)
    streak = 1.0 + 0.045 * streak / max(np.abs(streak).max(), 1e-9)

    LIGHT = _unit([-0.45, -0.75, 1.0])

    def display_z(Hs):
        Z = _pool(np.maximum(Hs[crop], zlo), st, np.min)
        Zp = np.full((dny, dnx), zlo)
        Zp[1:-1, 1:-1] = Z
        return Zp

    def display_mask(mask):
        M = _pool(mask[crop].astype(np.uint8), st, np.max).astype(bool)
        Mp = np.zeros((dny, dnx), bool)
        Mp[1:-1, 1:-1] = M
        return Mp[:-1, :-1] | Mp[1:, :-1] | Mp[:-1, 1:] | Mp[1:, 1:]

    def view_dir(azim, elev):
        az, el = math.radians(azim), math.radians(elev)
        return np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])

    def shade(n, base, V, spec=0.35, power=28, amb=0.38):
        lam = np.clip(n @ LIGHT, 0, 1)
        hv = _unit(LIGHT + V)
        sp = np.clip(n @ hv, 0, 1) ** power
        return np.clip(base * (amb + (1 - amb) * lam)[..., None] + spec * sp[..., None], 0, 1)

    def stock_polys(Zp, V, paint):
        ztopf = float(Zp[1:-1, 1:-1].max())
        c00 = np.stack([XD[:-1, :-1], YD[:-1, :-1], Zp[:-1, :-1]], -1)
        c01 = np.stack([XD[:-1, 1:], YD[:-1, 1:], Zp[:-1, 1:]], -1)
        c11 = np.stack([XD[1:, 1:], YD[1:, 1:], Zp[1:, 1:]], -1)
        c10 = np.stack([XD[1:, :-1], YD[1:, :-1], Zp[1:, :-1]], -1)
        verts = np.stack([c00, c01, c11, c10], 2)
        n = np.cross(c11 - c00, c10 - c01)
        nn = np.linalg.norm(n, axis=-1, keepdims=True)
        n = np.where(nn > 1e-12, n / np.maximum(nn, 1e-12), [0, 0, 1])
        n = n * np.sign(n[..., 2:3] + 1e-9)  # flip toward +Z; vertical walls keep a horizontal normal
        flat_up = np.abs(n[..., 2]) > 0.96
        zq = verts[..., 2]
        zmin, zmean = zq.min(-1), zq.mean(-1)
        depth = np.clip((hi[2] - zmean) / max(hi[2] - zlo, 1e-9), 0, 1)
        base = np.empty(zmean.shape + (3,))
        base[:] = ALU_SIDE
        top = flat_up & (zmin >= ztopf - 1e-4)
        floor = flat_up & ~top & (zmean > zlo + 1e-6)
        # flat faces: brushed aluminium, darker the deeper the cut floor
        dk = np.clip(depth / 0.5, 0, 1)
        base[top] = ALU_TOP * streak[top][:, None]
        base[floor] = (ALU_TOP * (1.0 - 0.55 * dk[floor])[:, None] * (0.6 + 0.4 * streak[floor][:, None])
                       * np.array([0.93, 0.96, 1.0]))
        wall = ~flat_up & ~skirt
        base[wall] = ALU_SIDE * (0.95 - 0.4 * depth[wall])[:, None]
        hole = (zq.max(-1) <= zlo + 1e-6) & ~skirt
        rgb = shade(n, base, V)
        # outer sides: flat machined faces, no specular streaks
        rgb[skirt] = shade(n[skirt], ALU_SIDE * 0.92, V, spec=0.12, power=8, amb=0.42)
        rgb[hole] = HOLE
        for mask, col in paint or ():
            qm = display_mask(mask)
            rgb[qm] = col * (0.55 + 0.45 * np.clip(n[qm] @ LIGHT, 0, 1))[:, None] + 0.12
        rgb = np.clip(rgb, 0, 1)
        return verts.reshape(-1, 4, 3), rgb.reshape(-1, 3)

    # ---- tool geometry (from job.tools; real diameter, flute, stickout and holder)
    ztop = float(hi[2] + 1.9)

    HOLE_TOOLS = ("drill", "spot", "tap", "reamer", "center_drill", "countersink", "counter_sink", "chamfer",
                  "boring", "counterbore", "tap_right_hand", "tap_left_hand")

    def tool_polys(tnum, tip, V, Hs):
        tl = job.tools.get(tnum)
        if tl is None:
            return [], [], []
        r = max(tl.diameter / 2.0, 1e-3)
        tx, ty, tz = tip
        nf = 28
        th = np.linspace(0, 2 * np.pi, nf + 1)
        cs, sn = np.cos(th), np.sin(th)
        polys, cols = [], []
        kind_s = tl.kind
        zclip = tz
        if kind_s in HOLE_TOOLS:
            # the part of a hole-making tool inside its hole is hidden by the surrounding material
            i0 = max(int((tx - 1.4 * r - grid.x0) / grid.res), 0); i1 = min(int((tx + 1.4 * r - grid.x0) / grid.res) + 1, Hs.shape[1])
            j0 = max(int((ty - 1.4 * r - grid.y0) / grid.res), 0); j1 = min(int((ty + 1.4 * r - grid.y0) / grid.res) + 1, Hs.shape[0])
            if i1 > i0 and j1 > j0:
                cx_, cy_ = np.meshgrid(xs[i0:i1], ys[j0:j1])
                ring = (np.hypot(cx_ - tx, cy_ - ty) > 1.1 * r) & (np.hypot(cx_ - tx, cy_ - ty) < 1.4 * r)
                if ring.any():
                    zclip = max(tz, float(Hs[j0:j1, i0:i1][ring].max()))
        cone_h = 0.0
        if kind_s in ("drill", "spot", "chamfer", "countersink", "counter_sink", "center_drill"):
            half = math.radians(max(min(getattr(tl, "point_angle", 118.0) or 118.0, 179.0), 1.0) / 2.0)
            cone_h = r / math.tan(half)
        flute_top = tz + max(tl.flute_length, cone_h + 1e-3)
        shank_top = tz + max(tl.stickout, tl.flute_length)
        hold_top = shank_top + min(tl.holder_length, 0.32)
        hr = max(tl.holder_diameter / 2.0, r * 1.6)
        carbide = np.array([0.70, 0.72, 0.76])
        flutec = np.array([0.40, 0.42, 0.47])
        shankc = np.array([0.66, 0.68, 0.72])
        holder = np.array([0.20, 0.22, 0.27])
        nflutes = 2 if kind_s == "drill" else 3

        def band(z0, z1, rad0, rad1, base, rows=1):
            if z1 <= zclip:
                return
            if z0 < zclip:  # cut the band at the clip plane (keeps the cone taper right)
                rad0 = rad0 + (rad1 - rad0) * (zclip - z0) / max(z1 - z0, 1e-9)
                z0 = zclip
            z0, z1 = max(z0, tz), min(z1, ztop)
            if z1 <= z0:
                return
            zz = np.linspace(z0, z1, rows + 1)
            rr = np.linspace(rad0, rad1, rows + 1)
            for k in range(rows):
                for i in range(nf):
                    v = [(tx + rr[k] * cs[i], ty + rr[k] * sn[i], zz[k]),
                         (tx + rr[k] * cs[i + 1], ty + rr[k] * sn[i + 1], zz[k]),
                         (tx + rr[k + 1] * cs[i + 1], ty + rr[k + 1] * sn[i + 1], zz[k + 1]),
                         (tx + rr[k + 1] * cs[i], ty + rr[k + 1] * sn[i], zz[k + 1])]
                    mid = 0.5 * (th[i] + th[i + 1])
                    slope = (rad0 - rad1) / max(z1 - z0, 1e-9)
                    nrm = _unit([math.cos(mid), math.sin(mid), slope])
                    b = base
                    polys.append(v)
                    cols.append(shade(nrm[None], b, V, spec=0.55, power=20, amb=0.30)[0])

        def disc(z, rad, base):
            if z > ztop or z < zclip - 1e-9:
                return
            polys.append([(tx + rad * c, ty + rad * s, z) for c, s in zip(cs[:-1], sn[:-1])])
            cols.append(shade(np.array([[0, 0, 1.0]]), base, V, spec=0.3)[0])

        if cone_h > 0:
            band(tz, tz + cone_h, 1e-4, r, carbide, rows=1)
            band(tz + cone_h, flute_top, r, r, carbide)
        else:
            disc(tz, r, carbide * 0.8)
            band(tz, flute_top, r, r, carbide)
        band(flute_top, shank_top, r, r, shankc)
        band(shank_top, hold_top, hr * 0.8, hr, holder)
        disc(min(hold_top, ztop), hr, holder * 1.3)
        # helical flute edges (30 deg helix), front-facing half only
        lines = []
        fz0, fz1 = max(tz + cone_h * 0.6, zclip), min(flute_top, ztop)
        if fz1 > fz0:
            u = np.linspace(0, 1, 40)
            zz = fz0 + (fz1 - fz0) * u
            lead = math.pi * 2 * r / math.tan(math.radians(30))
            for f in range(nflutes):
                ang = 2 * math.pi * f / nflutes + 2 * math.pi * (zz - tz) / lead
                p3 = np.stack([tx + 1.02 * r * np.cos(ang), ty + 1.02 * r * np.sin(ang), zz], 1)
                vis = np.cos(ang) * V[0] + np.sin(ang) * V[1] > 0.08
                for q in range(len(u) - 1):
                    if vis[q] and vis[q + 1]:
                        lines.append(p3[q:q + 2])
        return polys, cols, lines

    # ---- per-segment HUD data (real move data; time = length / programmed feed, rapids at machine rapid)
    n = len(segs)
    seg_len = np.linalg.norm(segs[:, 3:] - segs[:, :3], axis=1) if n else np.zeros(0)
    seg_feed = np.array([moves[i].feed for i in smove], float) if n else np.zeros(0)
    seg_feed = np.where(seg_feed > 0, seg_feed, float(job.machine["max_feed"]))
    seg_t = np.where(srapid == 1, seg_len / float(job.machine["max_rapid"]), seg_len / seg_feed)
    t_cum = np.cumsum(seg_t) if n else np.zeros(0)
    total_t = float(t_cum[-1]) if n else 0.0
    ulabel = "in/min" if job.units.startswith("in") else "mm/min"

    cut_idx = np.nonzero(srapid == 0)[0]
    # frame boundaries spread over cutting segments so the video spends time where material comes off
    bounds = [0] + [int(cut_idx[min(len(cut_idx) - 1, int(len(cut_idx) * k / a.frames))]) + 1
                    for k in range(1, a.frames)] + [n] if len(cut_idx) else [0, n]
    bounds = sorted(set(bounds))
    nfr = max(len(bounds) - 1, 1)

    sx, sy = hi[0] - lo[0], hi[1] - lo[1]
    zr = ztop - zlo

    def mmss(minutes):
        s = int(round(minutes * 60))
        return f"{s // 60:02d}:{s % 60:02d}"

    def frame(i, Hs, azim, tool=None, trail=None, hud=None, paint=None, banner=None, banner_color="#ffffff",
              verdict=None):
        fig = plt.figure(figsize=(12.8, 7.2), dpi=100, facecolor=BG)
        ax = fig.add_axes([0.40 - 0.421875, 0.54 - 0.75, 0.84375, 1.5], projection="3d", facecolor=BG)
        ax.computed_zorder = False
        V = view_dir(azim, ELEV)
        verts, rgb = stock_polys(display_z(Hs), V, paint)
        pc = Poly3DCollection(verts, facecolors=rgb, edgecolors=rgb, linewidths=0.6, antialiased=False)
        pc.set_zorder(1)
        ax.add_collection3d(pc)
        # crisp top outline of the stock
        ox = [lo[0], hi[0], hi[0], lo[0], lo[0]]
        oy = [lo[1], lo[1], hi[1], hi[1], lo[1]]
        zt = float(np.max(Hs[inside]))
        ax.plot(ox, oy, [zt] * 5, color="#e2e8f0", lw=0.8, alpha=0.5, zorder=2)
        if trail is not None and len(trail[0]):
            pts, rap = trail
            sg = np.stack([pts[:-1], pts[1:]], 1)
            age = np.linspace(0.08, 1.0, len(sg))
            keep = sg[:, :, 2].max(1) <= ztop  # moves up to home Z leave the frame: don't draw them
            sg, age, rap = sg[keep], age[keep], rap[keep]
            colr = np.where(rap[:, None] == 1, [[0.98, 0.75, 0.25]], [[0.22, 0.74, 0.97]])
            rgba = np.concatenate([colr, (age * np.where(rap == 1, 0.35, 0.85))[:, None]], 1)
            lc = Line3DCollection(sg, colors=rgba, linewidths=np.where(rap == 1, 0.6, 1.3))
            lc.set_zorder(3)
            ax.add_collection3d(lc)
        if tool is not None:
            tp, tc, tl3 = tool_polys(tool[0], tool[1], V, Hs)
            if tp:
                tpc = Poly3DCollection(tp, facecolors=tc, edgecolors=tc, linewidths=0.3)
                tpc.set_zorder(5)
                ax.add_collection3d(tpc)
            if tl3:
                hl = Line3DCollection(tl3, colors=[(0.16, 0.18, 0.22, 0.9)], linewidths=2.2)
                hl.set_zorder(6)
                ax.add_collection3d(hl)
        if verdict and verdict.get("ring") is not None:
            gx, gy, rad, col, label, zr0 = verdict["ring"]
            t = np.linspace(0, 2 * np.pi, 80)
            for w, al in ((7, 0.18), (2.6, 0.95)):
                ax.plot(gx + rad * np.cos(t), gy + rad * np.sin(t), np.full_like(t, zr0), color=col,
                        lw=w, alpha=al, zorder=6)
            ax.text(gx, gy, zr0 + 0.55, label, color="white", fontsize=13, weight="bold", ha="center",
                    family=FONT, zorder=7,
                    bbox=dict(boxstyle="round,pad=0.35", fc=col, ec="none", alpha=0.92))
            ax.plot([gx, gx], [gy, gy], [zr0, zr0 + 0.48], color=col, lw=1.6, zorder=6)
        pad = 0.05
        ax.set_xlim(lo[0] - pad, hi[0] + pad)
        ax.set_ylim(lo[1] - pad, hi[1] + pad)
        ax.set_zlim(zlo, ztop)
        ax.set_box_aspect((sx + 2 * pad, sy + 2 * pad, zr), zoom=0.76)
        ax.view_init(elev=ELEV, azim=azim)
        ax.set_axis_off()

        # ---- header
        fig.text(0.03, 0.935, a.title, color="white", fontsize=24, weight="bold", family=FONT)
        fig.text(0.03, 0.895, a.subtitle, color="#94a3b8", fontsize=13, family=FONT)
        if banner:
            fig.text(0.97, 0.935, banner, color=banner_color, fontsize=19, weight="bold", ha="right",
                     family=FONT)
        else:
            fig.text(0.97, 0.94, "●  SIMULATING", color="#38bdf8", fontsize=12, weight="bold", ha="right",
                     family=FONT)
        # ---- HUD (bottom-left)
        if hud:
            fig.patches.append(FancyBboxPatch((0.64, 0.035), 0.335, 0.20, boxstyle="round,pad=0.0,rounding_size=0.012",
                                              transform=fig.transFigure, fc="#111827", ec="#1f2937", lw=1.0,
                                              alpha=0.88, zorder=0))
            x0 = 0.655
            fig.text(x0, 0.205, hud["tool"], color="white", fontsize=13, weight="bold", family=FONT)
            fig.text(x0, 0.172, hud["mode"], color=hud["mode_color"], fontsize=11, weight="bold", family=MONO)
            fig.text(x0, 0.142, hud["line"], color="#94a3b8", fontsize=10, family=MONO)
            fig.text(x0, 0.105, "SIM TIME", color="#64748b", fontsize=9, family=FONT)
            fig.text(x0 + 0.075, 0.105, hud["time"], color="white", fontsize=10, weight="bold", family=MONO)
            fig.text(x0 + 0.20, 0.105, "MOVES", color="#64748b", fontsize=9, family=FONT)
            fig.text(x0 + 0.255, 0.105, f"{hud['pct']:3.0f}%", color="white", fontsize=10, weight="bold",
                     family=MONO)
            fig.patches.append(Rectangle((x0, 0.062), 0.305, 0.012, transform=fig.transFigure, fc="#1f2937",
                                         ec="none"))
            fig.patches.append(Rectangle((x0, 0.062), 0.305 * hud["pct"] / 100.0, 0.012, transform=fig.transFigure,
                                         fc="#38bdf8", ec="none"))
        fig.text(0.03, 0.035, f"real Z-map stock-removal simulation  ·  {i}/{nfr}", color="#475569", fontsize=10,
                 ha="left", family=FONT)

        if verdict and verdict.get("inset") is not None:
            img, ext, title, col, (cx, cy, cr) = verdict["inset"]
            ia = fig.add_axes([0.745, 0.47, 0.225, 0.33])
            ia.imshow(img, origin="lower", extent=ext, interpolation="nearest")
            t = np.linspace(0, 2 * np.pi, 80)
            ia.plot(cx + (cr + 0.04) * np.cos(t), cy + (cr + 0.04) * np.sin(t), color=col, lw=2.2)
            ia.set_xlim(ext[0], ext[1]); ia.set_ylim(ext[2], ext[3])
            ia.set_xticks([]); ia.set_yticks([])
            for s in ia.spines.values():
                s.set_color(col); s.set_linewidth(2)
            ia.set_title(title, color="white", fontsize=11, weight="bold", family=FONT, loc="left", pad=6)
        if verdict and verdict.get("stamp"):
            al = verdict["stamp"]
            fig.text(0.80, 0.25, "VERIFIED", color="#22c55e", fontsize=46, weight="bold", ha="center",
                     va="center", rotation=8, family=FONT, alpha=al,
                     bbox=dict(boxstyle="round,pad=0.30", fc=(0.13, 0.77, 0.37, 0.12 * al), ec=(0.13, 0.77, 0.37, al),
                               lw=4))
            fig.text(0.80, 0.105, verdict["stamp_sub"], color="#86efac", fontsize=12, ha="center", family=FONT,
                     alpha=al)
        fig.savefig(out / "frames" / f"f{frame.k:05d}.png", facecolor=BG)
        plt.close(fig)
        frame.k += 1

    frame.k = 0
    pts = np.concatenate([segs[:, :3], segs[-1:, 3:]]) if n else np.zeros((0, 3))
    az_cut_end = AZ1
    for i in range(1, len(bounds)):
        s0, s1 = bounds[i - 1], bounds[i]
        k = s1 - s0
        if k <= 0:
            continue
        od, ov, oh, ofl = (np.zeros(k) for _ in range(4))
        ohk = np.full(k, -1, np.int64)
        zmap.sweep(H, last, grid.x0, grid.y0, grid.res, np.ascontiguousarray(segs[s0:s1]),
                   np.ascontiguousarray(stool[s0:s1]), np.ascontiguousarray(srapid[s0:s1]), kind, R, ta, tb, flute,
                   cyl_off, cyl_r, zmax, float(job.tol["holder_clearance"]), float(job.sim["search_tolerance"]),
                   od, ov, oh, ohk, ofl)
        mv = moves[int(smove[s1 - 1])]
        tl = job.tools.get(mv.tool)
        rapid = srapid[s1 - 1] == 1
        hud = {
            "tool": f"T{mv.tool}  ·  {tl.name if tl is not None and getattr(tl, 'name', '') else 'tool'}",
            "mode": "RAPID  G0" if rapid else f"FEED   F{mv.feed:g} {ulabel}",
            "mode_color": "#fbbf24" if rapid else "#38bdf8",
            "line": (f"N{mv.line:<4d} {mv.text.strip()}" if mv.text else f"line {mv.line}")[:44],
            "time": f"{mmss(t_cum[s1 - 1])} / {mmss(total_t)}",
            "pct": 100.0 * (int(smove[s1 - 1]) + 1) / max(len(moves), 1),
        }
        j0 = max(0, s1 - 400)
        azim = AZ0 + (AZ1 - AZ0) * (i / nfr)
        frame(i, H, azim, tool=(mv.tool, tuple(segs[s1 - 1, 3:])), trail=(pts[j0:s1 + 1], srapid[j0:s1]), hud=hud)
        az_cut_end = azim

    # final verdict frames from the same maps the verifier uses
    gdepth = np.where(has_part & inside, P - H, 0.0)
    gouge = gdepth > job.tol["gouge"]
    left = np.where(has_part & inside, H - P, 0.0) > job.tol["leftover"] * 4
    g_cells, l_cells = int(gouge.sum()), int(left.sum())
    ok = g_cells == 0 and l_cells < 0.005 * max(int(has_part.sum()), 1)
    banner = "VERIFIED  ✓  no gouge, no leftover" if ok else f"GOUGE  ✗  {g_cells} cells cut into the part"
    if not ok and g_cells == 0:
        banner = f"LEFTOVER  ✗  {l_cells} cells of material left"
    col = "#22c55e" if ok else "#ef4444"
    gpaint = ndimage.binary_dilation(gouge, iterations=2) if g_cells else gouge
    paint = [(gpaint, RED), (left, BLUE)]

    verdict = {}
    if not ok:
        focus = gouge if g_cells else left
        ring_col = "#ef4444" if g_cells else "#3b82f6"
        lab, nl = ndimage.label(focus)
        score = ndimage.sum(gdepth if g_cells else focus, lab, np.arange(1, nl + 1))
        reg = lab == (int(np.argmax(score)) + 1)
        cy, cx = ndimage.center_of_mass(reg)
        gx, gy = grid.x0 + (cx + 0.5) * grid.res, grid.y0 + (cy + 0.5) * grid.res
        ry, rx = np.nonzero(reg)
        ext_r = 0.5 * grid.res * max(np.ptp(rx) + 1, np.ptp(ry) + 1)
        rad = max(0.28, ext_r + 0.15)
        if g_cells:
            label = f"GOUGE  ·  {int(reg.sum())} cells"
        else:
            label = f"LEFTOVER  {int(reg.sum())} cells"
        # full-res top-down detail of the region, straight from the sim height field
        w = max(0.3, ext_r * 4 + 0.12)
        i0 = max(int((gx - w - grid.x0) / grid.res), 0); i1 = min(int((gx + w - grid.x0) / grid.res), H.shape[1])
        j0 = max(int((gy - w * 0.75 - grid.y0) / grid.res), 0)
        j1 = min(int((gy + w * 0.75 - grid.y0) / grid.res), H.shape[0])
        Hc = np.maximum(H[j0:j1, i0:i1], zlo)
        base = np.ones(Hc.shape + (3,)) * ALU_TOP
        dep = (hi[2] - Hc) / max(hi[2] - zlo, 1e-9)
        base = base * (1 - 0.45 * dep[..., None])
        base[Hc <= zlo + 1e-6] = HOLE
        img = LightSource(azdeg=315, altdeg=35).shade_rgb(base, Hc, vert_exag=8, blend_mode="soft")
        img[gouge[j0:j1, i0:i1]] = RED
        img[left[j0:j1, i0:i1]] = BLUE
        ext = (grid.x0 + i0 * grid.res, grid.x0 + i1 * grid.res, grid.y0 + j0 * grid.res, grid.y0 + j1 * grid.res)
        title = f"{'GOUGE' if g_cells else 'LEFTOVER'} DETAIL  ·  full-res sim ({grid.res:g})"
        zr0 = float(P[reg].max() if g_cells else H[reg].max()) + 0.01
        verdict = {"ring": (gx, gy, rad, ring_col, label, zr0), "inset": (img, ext, title, ring_col, (gx, gy, ext_r))}
    for h in range(a.hold):
        v = dict(verdict)
        if ok:
            v["stamp"] = min(1.0, 0.25 + h / 6.0)
            v["stamp_sub"] = f"0 gouge cells  ·  {l_cells} leftover cells  ·  cycle {mmss(total_t)}"
        elif v.get("ring"):
            gx, gy, rad, rc, label, zr0 = v["ring"]
            v["ring"] = (gx, gy, rad * (1.0 + 0.08 * math.sin(h * 0.5)), rc, label, zr0)
        azim = az_cut_end + 4.0 * (h / max(a.hold, 1))
        frame(nfr, H, azim, paint=paint, banner=banner, banner_color=col, verdict=v)
    (out / "summary.json").write_text(json.dumps({"frames": frame.k, "segments": n, "gouge_cells": g_cells,
                                                  "leftover_cells": l_cells, "verified": ok}, indent=2))
    print(json.dumps({"frames": frame.k, "verified": ok, "gouge_cells": g_cells, "leftover_cells": l_cells}))


if __name__ == "__main__":
    main()
