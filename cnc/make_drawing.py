"""Render the customer's 2D drawing for PLATE-104 (what a real RFQ arrives with) to job/drawing.png + .pdf.

    uv run --with matplotlib python3 cnc/make_drawing.py
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle

OUT = Path(__file__).resolve().parent / "job"
INK, THIN, HID = "#111", 0.7, "#111"
FS = 9

fig = plt.figure(figsize=(17, 11), dpi=200)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, 17)
ax.set_ylim(0, 11)
ax.set_aspect("equal")
ax.axis("off")


def line(x0, y0, x1, y1, lw=THIN, **k):
    ax.plot([x0, x1], [y0, y1], color=INK, lw=lw, solid_capstyle="butt", **k)


def text(x, y, s, size=FS, **k):
    k.setdefault("color", INK)
    ax.text(x, y, s, fontsize=size, family="DejaVu Sans", **k)


def arrow(x, y, dx, dy):
    ax.annotate("", xy=(x, y), xytext=(x - dx, y - dy),
                arrowprops=dict(arrowstyle="-|>,head_length=0.5,head_width=0.18", color=INK, lw=THIN))


def hdim(x0, x1, y, yref0, yref1, label, s):
    """Horizontal dimension between part x0..x1 (drawing coords), dim line at y, extension lines from yref."""
    for x, yr in ((x0, yref0), (x1, yref1)):
        g = 0.06 if y > yr else -0.06
        line(x, yr + g, x, y + (0.08 if y > yr else -0.08), lw=0.5)
    line(x0, y, x1, y, lw=0.5)
    arrow(x0, y, -0.12, 0)
    arrow(x1, y, 0.12, 0)
    text((x0 + x1) / 2, y + 0.05, label, ha="center", va="bottom", size=s,
         bbox=dict(fc="white", ec="none", pad=0.6))


def vdim(y0, y1, x, xref0, xref1, label, s):
    for yy, xr in ((y0, xref0), (y1, xref1)):
        g = 0.06 if x > xr else -0.06
        line(xr + g, yy, x + (0.08 if x > xr else -0.08), yy, lw=0.5)
    line(x, y0, x, y1, lw=0.5)
    arrow(x, y0, 0, -0.12)
    arrow(x, y1, 0, 0.12)
    text(x - 0.05, (y0 + y1) / 2, label, ha="right", va="center", size=s, rotation=90,
         bbox=dict(fc="white", ec="none", pad=0.6))


# Sheet border + zones
ax.add_patch(Rectangle((0.3, 0.3), 16.4, 10.4, fill=False, lw=1.6, ec=INK))
ax.add_patch(Rectangle((0.5, 0.5), 16.0, 10.0, fill=False, lw=0.8, ec=INK))
for i, c in enumerate("87654321"):
    text(0.5 + 2 * i + 1, 10.56, c, size=7, ha="center")
for i, c in enumerate("FEDCBA"):
    text(0.38, 10.5 - (i + 0.5) * 10 / 6, c, size=7, va="center")

# ---------------- TOP VIEW (scale 1:1), part origin lower-left at (1.6, 4.9)
S, OX, OY = 1.0, 1.6, 4.9
P = lambda x, y: (OX + x * S, OY + y * S)
ax.add_patch(Rectangle(P(0, 0), 6 * S, 4 * S, fill=False, lw=1.5, ec=INK))
ax.add_patch(FancyBboxPatch((P(1.5, 1.0)[0] + 0.3125, P(1.5, 1.0)[1] + 0.3125), 3 * S - 0.625, 2 * S - 0.625,
                            boxstyle="round,pad=0.3125", fill=False, lw=1.5, ec=INK))
holes = [(0.5, 0.5), (5.5, 0.5), (5.5, 3.5), (0.5, 3.5)]
for hx, hy in holes:
    cx, cy = P(hx, hy)
    ax.add_patch(Circle((cx, cy), 0.125 * S, fill=False, lw=1.4, ec=INK))
    line(cx - 0.22, cy, cx + 0.22, cy, lw=0.4, ls=(0, (6, 2, 1, 2)))
    line(cx, cy - 0.22, cx, cy + 0.22, lw=0.4, ls=(0, (6, 2, 1, 2)))
cx, cy = P(3, 2)
line(cx - 1.75, cy, cx + 1.75, cy, lw=0.4, ls=(0, (8, 2, 1, 2)))
line(cx, cy - 1.25, cx, cy + 1.25, lw=0.4, ls=(0, (8, 2, 1, 2)))
# section cut line A-A through Y2.000
line(OX - 0.45, cy, OX - 0.1, cy, lw=1.6)
line(OX + 6.1, cy, OX + 6.45, cy, lw=1.6)
for x in (OX - 0.3, OX + 6.3):
    arrow(x, cy - 0.38, 0, -0.3)
    text(x, cy - 0.62, "A", size=12, ha="center", weight="bold")
text(OX + 3, OY + 4.95, "TOP VIEW", size=11, ha="center", weight="bold")

# dimensions, top view (ordinate-style from datum lower-left)
hdim(OX, OX + 6, OY + 4.55, OY + 4, OY + 4, "6.000", 10)
vdim(OY, OY + 4, OX - 0.85, OX, OX, "4.000", 10)
hdim(OX, OX + 0.5, OY - 0.4, OY, OY + 0.5, ".500", 8)
vdim(OY, OY + 0.5, OX + 6.45, OX + 6, OX + 6.5, "", 8)
text(OX + 6.6, OY + 0.25, ".500", size=8, va="center")
hdim(OX + 5.5, OX + 6, OY - 0.4, OY + 0.5, OY, ".500", 8)
hdim(OX + 1.5, OX + 4.5, OY - 0.85, OY + 1.0, OY + 1.0, "3.000", 9)
hdim(OX, OX + 1.5, OY - 1.3, OY, OY + 1.0, "1.500", 8)
vdim(OY + 1.0, OY + 3.0, OX + 7.1, OX + 4.5, OX + 4.5, "2.000", 9)
vdim(OY, OY + 1.0, OX + 7.6, OX + 6, OX + 4.5, "1.000", 8)
# hole + radius callouts
hx, hy = P(5.5, 3.5)
line(hx + 0.09, hy + 0.09, hx + 0.6, hy + 0.55, lw=0.5)
line(hx + 0.6, hy + 0.55, hx + 1.0, hy + 0.55, lw=0.5)
arrow(hx + 0.09, hy + 0.09, -0.05, -0.045)
text(hx + 1.05, hy + 0.55, "4X  Ø.250 THRU", size=9, va="center")
rx, ry = P(1.5 + 0.0915, 3.0 - 0.0915)
line(rx, ry, rx - 0.55, ry + 0.75, lw=0.5)
line(rx - 0.55, ry + 0.75, rx - 0.85, ry + 0.75, lw=0.5)
arrow(rx, ry, 0.04, -0.055)
text(rx - 0.9, ry + 0.75, "4X  R.3125", size=9, va="center", ha="right")
px, py = P(2.2, 2.6)
text(px, py, "POCKET\n.500 DEEP\nFLAT FLOOR", size=8, ha="center", va="center", style="italic")

# ---------------- SECTION A-A, below top view
SX, SY = OX, 1.55
ax.add_patch(Rectangle((SX, SY), 6, 1.0, fill=False, lw=1.5, ec=INK))
# pocket cut from top
ax.add_patch(Rectangle((SX + 1.5, SY + 0.5), 3.0, 0.5, fc="white", ec="none", zorder=3))
for x0, x1 in ((SX, SX + 1.5), (SX + 4.5, SX + 6)):
    pass
line(SX + 1.5, SY + 1.0, SX + 1.5, SY + 0.5, lw=1.5)
line(SX + 1.5, SY + 0.5, SX + 4.5, SY + 0.5, lw=1.5)
line(SX + 4.5, SY + 0.5, SX + 4.5, SY + 1.0, lw=1.5)
ax.add_patch(Rectangle((SX + 1.5, SY + 0.985), 3.0, 0.03, fc="white", ec="none", zorder=4))
# hatching on cut material
import numpy as np

def hatch(x0, y0, x1, y1):
    for k in np.arange(-(y1 - y0), x1 - x0, 0.12):
        a = max(0, -k)
        b = min(y1 - y0, x1 - x0 - k)
        if b > a:
            line(x0 + k + a, y0 + a, x0 + k + b, y0 + b, lw=0.35)

hatch(SX, SY, SX + 1.5, SY + 1.0)
hatch(SX + 4.5, SY, SX + 6, SY + 1.0)
hatch(SX + 1.5, SY, SX + 4.5, SY + 0.5)
text(SX + 3, SY - 0.55, "SECTION  A-A", size=11, ha="center", weight="bold")
vdim(SY, SY + 1.0, SX - 0.45, SX, SX, "1.000", 9)
vdim(SY + 0.5, SY + 1.0, SX + 6.5, SX + 4.5, SX + 6, ".500", 9)

# ---------------- isometric-ish note panel / notes
NX, NY = 10.6, 9.9
text(NX, NY, "NOTES:", size=11, weight="bold", va="top")
notes = [
    "1.  MATERIAL: ALUMINUM 6061-T6 PLATE.",
    "2.  QUANTITY: 25 PCS.",
    "3.  INTERPRET PER ASME Y14.5-2018.",
    "4.  UNLESS OTHERWISE SPECIFIED:",
    "      .XXX  ±.005      .XX  ±.010      ANGLES ±0.5°",
    "5.  ALL SURFACES 125 Ra MAX; POCKET FLOOR 63 Ra.",
    "6.  POCKET WALLS VERTICAL; FLOOR FLAT.",
    "7.  BREAK ALL SHARP EDGES .005–.015.",
    "8.  REMOVE ALL BURRS. NO TOOL MARKS ON TOP FACE.",
    "9.  3D MODEL PLATE-104.STEP SUPPLIED FOR REFERENCE;",
    "      THIS DRAWING GOVERNS.",
]
for i, n in enumerate(notes):
    text(NX + 0.05, NY - 0.45 - i * 0.32, n, size=9.5, va="top")

# third-angle projection symbol
tx, ty = 14.2, 3.55
ax.add_patch(Rectangle((tx, ty - 0.18), 0.6, 0.36, fill=False, lw=0.8, ec=INK))
ax.add_patch(Circle((tx + 1.05, ty), 0.18, fill=False, lw=0.8, ec=INK))
ax.add_patch(Circle((tx + 1.05, ty), 0.08, fill=False, lw=0.8, ec=INK))

# ---------------- title block
TX, TY, TW, TH = 10.6, 0.5, 5.9, 2.75
ax.add_patch(Rectangle((TX, TY), TW, TH, fill=False, lw=1.2, ec=INK))
rows = [TY + 0.55, TY + 1.1, TY + 1.65, TY + 2.2]
for r in rows:
    line(TX, r, TX + TW, r, lw=0.8)
line(TX + 2.0, TY, TX + 2.0, TY + 2.2, lw=0.8)
line(TX + 4.0, TY, TX + 4.0, TY + 1.1, lw=0.8)


def cell(x, y, lab, val, vs=10):
    text(x + 0.07, y + 0.48, lab, size=6.5, va="top", color="#444")
    text(x + 0.07, y + 0.07, val, size=vs, va="bottom", weight="bold")


text(TX + 0.1, TY + 2.48, "BAYLINE AUTOMATION, INC.", size=12, weight="bold", va="center")
text(TX + TW - 0.1, TY + 2.48, "Fremont, CA", size=8, ha="right", va="center")
cell(TX, rows[2], "TITLE", "MOUNTING PLATE", 12)
cell(TX + 2.0, rows[2], "", "", 10)
cell(TX, rows[1], "MATERIAL", "AL 6061-T6")
cell(TX + 2.0, rows[1], "FINISH", "AS MACHINED", 10)
cell(TX, rows[0], "DWG NO.", "PLATE-104", 13)
cell(TX + 2.0, rows[0], "REV", "A", 13)
cell(TX + 4.0, rows[0], "QTY", "25")
cell(TX, TY, "UNITS / SCALE", "INCH   1:1")
cell(TX + 2.0, TY, "DRAWN / DATE", "J. ORTEGA  10/02/26", 8.5)
cell(TX + 4.0, TY, "SHEET", "1 OF 1")
text(TX + 2.07, rows[2] + 0.48, "THIRD ANGLE PROJECTION", size=6.5, va="top", color="#444")

fig.savefig(OUT / "drawing.png", dpi=200, facecolor="white")
fig.savefig(OUT / "drawing.pdf", facecolor="white")
print("wrote", OUT / "drawing.png", OUT / "drawing.pdf")
