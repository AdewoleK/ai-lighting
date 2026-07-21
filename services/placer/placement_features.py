"""
services/placer/placement_features.py

Feature extraction for the ML luminaire placement classifier.

Each candidate position (GridCandidate) is described by a fixed-length float
vector.  The same vector is used both for inference (predict type for a new
floor plan) and for building the training set from processed plans.

Feature vector — 19 dimensions
───────────────────────────────
 0  dist_to_boundary_m       — distance to zone polygon boundary (m, capped 30)
 1  dist_to_gondola_m        — distance to nearest shelf object (m, capped 30)
 2  dist_to_column_m         — distance to nearest structural column (m, capped 30)
 3  is_wall_tagged            — 1.0 if ceiling_grid pre-tagged as exterior_wall
 4  norm_x                   — (x - bbox_minx) / bbox_width  ∈ [0, 1]
 5  norm_y                   — (y - bbox_miny) / bbox_height ∈ [0, 1]
 6  zone_area_m2_log         — log10(zone_area_m2 + 1)
 7  shelving_density         — shelving_count / zone_area_m2 (capped 1)
 8  n_checkout_norm          — checkout_count / 10 (capped 1)
 9  zone_type_enc            — 0=sales_floor 1=checkout 2=corridor 3=entrance
                               4=storage 5=office 6=service_area  /  6
10  is_near_column           — 1.0 if dist_to_column < 1875 mm (3 tiles)
11  boundary_log             — log10(dist_to_boundary_mm + 1) / log10(30001)
12  gondola_log              — log10(dist_to_gondola_mm + 1) / log10(30001)
13  tile_phase_x             — tile_i % 2  (grid phase — 0 or 1)
14  tile_phase_y             — tile_j % 2
15  n_shelving_norm          — shelving_count / 300 (capped 1)
16  shelf_depth_norm         — nearest shelf primary depth / 100.0  (0.27…0.77)
17  is_wall_assortment       — 1.0 if nearest shelf's assortment is a wall category
18  is_large_depth           — 1.0 if nearest shelf depth ≥ 67 mm (always wall)
"""
from __future__ import annotations
import math, re as _re
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from services.grid.ceiling_grid import GridCandidate

FEATURE_DIM = 19

# Known wall-gondola assortment names (product categories that always map to
# exterior/wall shelf placement regardless of geometric distance to envelope).
# Confirmed from 4-store sample: Hamburg 3600, Puderbach 4073, Bad Nenndorf 1786.
WALL_ASSORTMENT_NAMES: frozenset = frozenset({
    'WPR', 'PAPIER', 'DAMENHYGIENE', 'WATTE',
    'KOSM. TÜCHER', 'KOSM.TÜCHER', 'KOSM TÜCHER',
    'KONSUMDUFT', 'PARFÜM', 'IDEENWELT', 'WEIN', 'KERZEN',
    'DEKO', 'RAHMEN', 'ZUSATZ DUFT', 'ZUSATZDUFT', 'DÜFTE',
})

_ZONE_ENC = {
    'sales_floor': 0, 'checkout_zone': 1, 'corridor': 2,
    'entrance': 3, 'storage': 4, 'office': 5, 'service_area': 6,
}

LUMI_TYPES  = ['A', 'AW', 'C', 'E', 'skip']
LUMI_LABEL  = {t: i for i, t in enumerate(LUMI_TYPES)}


def _min_dist(point_xy, objects, cap_mm=30_000):
    """Return minimum distance from point_xy to any object in objects (by .position)."""
    px, py = point_xy
    best = cap_mm
    for obj in objects:
        ox, oy = obj.position[0], obj.position[1]
        d = math.sqrt((px - ox) ** 2 + (py - oy) ** 2)
        if d < best:
            best = d
    return best


def _parse_depth_mm(depth_code: str) -> int:
    """Extract first numeric depth value from code like '57/47' → 57."""
    m = _re.match(r'(\d+)', depth_code or '')
    return int(m.group(1)) if m else 0


def _nearest_shelf_context(
    point_xy: tuple,
    shelf_objs: list,
    cap_mm: float = 2500.0,
) -> tuple:
    """
    Return (depth_norm, is_wall_assortment, is_large_depth) for the nearest shelf.

    depth_norm        = primary depth_mm / 100.0  (e.g. 0.57 for 57mm)
    is_wall_assortment = 1.0 if assortment is in WALL_ASSORTMENT_NAMES
    is_large_depth    = 1.0 if depth >= 67 mm (WPR / DAMENHYGIENE always-wall signal)

    Returns (0.0, 0.0, 0.0) when no shelf is within cap_mm or no depth/assortment
    data is available (e.g. when called on a purely area-based candidate).
    """
    px, py = point_xy
    best_d, best_s = cap_mm, None
    for s in shelf_objs:
        ox, oy = s.position[0], s.position[1]
        d = math.sqrt((px - ox) ** 2 + (py - oy) ** 2)
        if d < best_d:
            best_d, best_s = d, s
    if best_s is None:
        return 0.0, 0.0, 0.0
    depth_mm = _parse_depth_mm(getattr(best_s, 'depth_code', ''))
    assort   = getattr(best_s, 'assortment', '').strip().upper()
    is_wall  = 1.0 if assort in WALL_ASSORTMENT_NAMES else 0.0
    is_large = 1.0 if depth_mm >= 67 else 0.0
    depth_n  = float(depth_mm) / 100.0
    return depth_n, is_wall, is_large


def extract(candidate,
            zone,
            shelf_objs: list,
            column_objs: list) -> np.ndarray:
    """
    Build a 19-d feature vector for one GridCandidate in a given zone.

    Parameters
    ----------
    candidate   : GridCandidate  (from ceiling_grid.py)
    zone        : ZoneResult     (from room_classifier_real.py)
    shelf_objs  : list of FurnitureInsert with inferred_type == 'shelving'
    column_objs : list of FurnitureInsert with inferred_type == 'column'
    """
    from shapely.geometry import Point as SPoint

    pt = SPoint(candidate.x, candidate.y)

    # Distances (mm)
    dist_boundary_mm = pt.distance(zone.polygon.boundary)
    dist_gondola_mm  = _min_dist((candidate.x, candidate.y), shelf_objs)
    dist_column_mm   = _min_dist((candidate.x, candidate.y), column_objs)

    # Cap & convert to metres
    cap = 30_000.0
    dist_b_m = min(dist_boundary_mm, cap) / 1000.0
    dist_g_m = min(dist_gondola_mm,  cap) / 1000.0
    dist_c_m = min(dist_column_mm,   cap) / 1000.0

    is_wall = 1.0 if getattr(candidate, 'wall_relation', 'interior') == 'exterior_wall' else 0.0

    # Normalised position within zone bounding box
    bb = zone.polygon.bounds          # (minx, miny, maxx, maxy)
    bw = max(bb[2] - bb[0], 1.0)
    bh = max(bb[3] - bb[1], 1.0)
    norm_x = (candidate.x - bb[0]) / bw
    norm_y = (candidate.y - bb[1]) / bh

    # Zone metrics
    area_m2     = max(zone.area_m2, 0.1)
    fc          = getattr(zone, 'furniture_counts', {})
    n_shelving  = fc.get('shelving', 0)
    n_checkout  = fc.get('checkout', 0)
    s_density   = min(n_shelving / area_m2, 1.0)
    zt_enc      = _ZONE_ENC.get(zone.zone_type, 0) / 6.0

    is_near_col = 1.0 if dist_column_mm < 1875.0 else 0.0

    # Log-scale distance features (normalised to [0,1])
    _log_cap = math.log10(cap + 1)
    b_log = math.log10(dist_boundary_mm + 1) / _log_cap
    g_log = math.log10(dist_gondola_mm  + 1) / _log_cap

    tile_phase_x = float(getattr(candidate, 'tile_i', 0) % 2)
    tile_phase_y = float(getattr(candidate, 'tile_j', 0) % 2)

    n_shelv_norm = min(n_shelving / 300.0, 1.0)

    depth_norm, is_wall_assort, is_large_depth = _nearest_shelf_context(
        (candidate.x, candidate.y), shelf_objs)

    return np.array([
        dist_b_m,
        dist_g_m,
        dist_c_m,
        is_wall,
        norm_x,
        norm_y,
        math.log10(area_m2 + 1),
        s_density,
        min(n_checkout / 10.0, 1.0),
        zt_enc,
        is_near_col,
        b_log,
        g_log,
        tile_phase_x,
        tile_phase_y,
        n_shelv_norm,
        depth_norm,        # 16 — nearest shelf primary depth / 100
        is_wall_assort,    # 17 — nearest shelf is a known wall assortment
        is_large_depth,    # 18 — nearest shelf depth >= 67mm (always-wall signal)
    ], dtype=np.float32)
