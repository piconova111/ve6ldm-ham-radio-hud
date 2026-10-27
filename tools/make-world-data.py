#!/usr/bin/env python3
"""Builds the land data for the station display's maps (run once, on a build machine).

Inputs:
  - Natural Earth 1:50m land polygons (public domain): ne_50m_land.geojson
  - the flat map's dot grid, app/static/map.svg (its colours say which continent each dot is on)
Outputs (in app/static):
  - world.png: a 1440 x 720 picture of the world, 4 pixels per degree, plate carree
    (x = longitude from -180, y = latitude from +90). Each pixel's grey level / 32 is
    0 for sea or 1-7 for the continent (the same colours as the flat map, 7 = Antarctica).
  - world.json: the flat map's dot grid, the continent colours, and small islands as points
    (the globe and beam map space their dots further apart than these islands are wide).

Usage: make-world-data.py NE_50M_LAND_GEOJSON MAP_SVG OUT_DIR
"""
import json
import math
import re
import sys
from collections import deque

import numpy as np
from PIL import Image, ImageDraw

LAND_FILE, SVG_FILE, OUT = sys.argv[1:4]

# ---- the flat map's grid (must match hud.js: Miller, left edge 170 W, 84 N to 60 S) ----
MAP_W, LON0 = 1180, -170
COLS, ROWS, X0, Y0, STEP = 147, 75, 6.0, 4.5, 8.0
COLOURS = {"#D98A4E": 1, "#CDB95A": 2, "#D07FA6": 3, "#5E97CE": 4}  # North America, South America, Europe, Asia
PALETTE = ["", "#D98A4E", "#CDB95A", "#D07FA6", "#5E97CE", "#6EAE6A", "#6EAE6A", "#9FB3C8"]
#           sea  N America  S America  Europe     Asia       Africa     Oceania    Antarctica


def ym(lat):
    return 1.25 * math.log(math.tan(math.pi / 4 + 0.4 * math.radians(lat)))


YT, K = ym(84), MAP_W / (2 * math.pi)

svg = open(SVG_FILE, encoding="utf-8").read()
svg = re.sub(r"<metadata>.*?</metadata>", "", svg, flags=re.S)
grid = np.zeros((ROWS, COLS), dtype=np.uint8)
green = 0
for fill, body in re.findall(r'<g fill="(#[0-9A-Fa-f]{6})">(.*?)</g>', svg, flags=re.S):
    fill = fill.upper()
    if fill == "#172539":
        continue  # sea dots
    if fill in COLOURS:
        index = COLOURS[fill]
    else:  # the two green groups: Africa first, then Oceania
        green += 1
        index = 4 + green
    for cx, cy in re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', body):
        grid[round((float(cy) - Y0) / STEP), round((float(cx) - X0) / STEP)] = index
assert green == 2, "expected two green groups (Africa, Oceania)"

# Nearest continent for every grid cell (breadth-first from the land dots; wraps east-west).
near = grid.copy()
queue = deque((r, c) for r in range(ROWS) for c in range(COLS) if grid[r, c])
while queue:
    r, c = queue.popleft()
    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        rr, cc = r + dr, (c + dc) % COLS
        if 0 <= rr < ROWS and not near[rr, cc]:
            near[rr, cc] = near[r, c]
            queue.append((rr, cc))


def continent(lat, lon):
    if lat < -60:
        return 7
    lat = min(84.0, lat)
    x = (((lon - LON0) % 360) + 360) % 360 * MAP_W / 360
    y = K * (YT - ym(lat))
    r = min(ROWS - 1, max(0, round((y - Y0) / STEP)))
    c = round((x - X0) / STEP) % COLS
    return int(near[r, c])


# ---- rasterise the land: 16 pixels per degree, then 4 x 4 blocks give each cell's land cover ----
SUB, W, H = 16, 1440, 720
big = Image.new("L", (360 * SUB, 180 * SUB), 0)
draw = ImageDraw.Draw(big)
land = json.load(open(LAND_FILE, encoding="utf-8"))
small = []  # (lat, lon, area) of small islands


def ring_xy(ring):
    return [((lon + 180) * SUB, (90 - lat) * SUB) for lon, lat in ring]


def ring_area_centroid(ring):
    """Planar area (square degrees, shrunk by cos(lat)) and centroid of a lon/lat ring."""
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a) < 1e-12:
        xs, ys = zip(*ring)
        return 0.0, sum(ys) / len(ys), sum(xs) / len(xs)
    cx /= 3 * a
    cy /= 3 * a
    return abs(a) / 2 * math.cos(math.radians(cy)), cy, cx


for feature in land["features"]:
    geom = feature["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        outer = [tuple(p[:2]) for p in poly[0]]
        draw.polygon(ring_xy(outer), fill=255)
        for hole in poly[1:]:
            draw.polygon(ring_xy([tuple(p[:2]) for p in hole]), fill=0)
        area, lat, lon = ring_area_centroid(outer)
        if area < 2.0:
            small.append((lat, lon, area))

cover = np.asarray(big, dtype=np.float32).reshape(H, SUB // 4, W, SUB // 4).mean(axis=(1, 3)) / 255
is_land = cover >= 0.4

lats = 90 - (np.arange(H) + 0.5) / 4
lons = -180 + (np.arange(W) + 0.5) / 4
mask = np.zeros((H, W), dtype=np.uint8)
for r in range(H):
    cols = np.nonzero(is_land[r])[0]
    for c in cols:
        mask[r, c] = continent(lats[r], lons[c])
Image.fromarray(mask * 32, mode="L").save(f"{OUT}/world.png", optimize=True)

# ---- small islands as single points, one per 1-degree square ----
seen = set()
islands = []
for lat, lon, area in sorted(small, key=lambda s: -s[2]):
    key = (math.floor(lat), math.floor(lon))
    if key in seen:
        continue
    seen.add(key)
    islands.append([round(lat, 2), round(lon, 2), continent(lat, lon)])

flat = "".join(str(int(v)) for v in grid.flatten())
json.dump({
    "about": "Land for the station display's maps. Natural Earth 1:50m land (public domain); "
             "continents follow the flat map's colours.",
    "colors": PALETTE,
    "mask": {"src": "world.png", "w": W, "h": H, "per_degree": 4, "level": 32},
    "flat": {"cols": COLS, "rows": ROWS, "x0": X0, "y0": Y0, "step": STEP, "w": MAP_W, "h": 601, "grid": flat},
    "islands": islands,
}, open(f"{OUT}/world.json", "w", encoding="utf-8"), separators=(",", ":"))

print("land cells:", int(is_land.sum()), "of", W * H, "| small islands:", len(small), "kept:", len(islands))
for i in range(1, 8):
    print(" continent", i, PALETTE[i], int((mask == i).sum()), "cells")
