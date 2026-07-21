"""
lighting-ai/services/placer/real_placer.py

Luminaire placer for real Rossmann plans.

Architecture (from technical documents):
  The task is NOT free coordinate generation.  It is:
    "Selection and typing of legal luminaire positions on a known ceiling grid."

  Four-layer decision logic:
    1. Candidate generation   — tessellate 625mm grid, extract shelf-row positions
    2. Candidate scoring      — shelf proximity, wall relation, assortment context
    3. Selection              — hard rules first, soft spacing preferences
    4. Write-back             — correct block/layer/type per Rossmann profile

Grid facts (from DXF semantic audit):
  • Tile module: 625mm × 625mm
  • A_center sub-position: luminaire at tile centre (312.5, 312.5) within tile
  • B_corner sub-position: luminaire at corner offset (150, 150) within tile
  • Typical inter-luminaire spacing: 1250mm = 2 tiles along shelf run
  • Exterior-wall shelves (≤ 625mm from outer contour) → Beam-M-high luminaire
  • Interior shelves → Beam-M-standard luminaire

Luminaire type mapping (from DXF semantic audit of Bad Nenndorf output):
  A  (*U1528) — MIKA80-E 40° Beam-M-600ma — interior shelf standard
  AW (*U1549) — MIKA80-E 40° Beam-M-high  — exterior / wall-boundary shelf
  B  (*U1546) — MIKA80-E 60° Beam-F-3200  — wide-beam supplement / fill
  D  (*U1545) — MIKA80-E 60° Beam-F-3200  — checkout-area task lighting
  E  (*U1548) — track spotlight            — shop window / Schaufenster
  C  — relabelled corner A-lights (3 per plan, at shelf-domain corners)
  W  — anti-glare honeycomb for cosmetics areas

Position class counts in Bad Nenndorf reference (DXF audit ground truth):
  Aufteilung-A: 130   Aufteilung_B: 37   Aufteilung_D: 34
  Aufteilung_E: 18    Aufteilung_C: 3
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import sys

from shapely.geometry import Point, MultiPoint, Polygon
from shapely.geometry import box as shapely_box

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from services.parser.pdf_parser import ParsedPlan
from services.classifier.room_classifier_real import ClassifiedPlan, ZoneResult
from services.lighting.calculator import (
    zone_spec as _zone_spec, ZoneLightingSpec,
    BASE_GRID_PITCH_MM as BASE_PITCH,
    NO_LIGHTING_ZONE_TYPES, LUMINAIRE_CATALOG,
    ZONE_LIGHTING_STRATEGY,
)
from services.grid.ceiling_grid import (
    TILE_MM, INTER_LUMINAIRE_MM,
    generate_shelf_row_candidates,
    generate_area_candidates,
    classify_wall_relation,
    snap_to_grid, snap_to_subposition,
)
from services.placer.placement_features import (
    extract as _extract_features,
    WALL_ASSORTMENT_NAMES as _WALL_ASSORT_NAMES,
)

# ── Grid / geometry constants ─────────────────────────────────────────────────

PITCH_MM        = TILE_MM          # 625mm — fundamental ceiling tile
INTER_LUMI_MM   = INTER_LUMINAIRE_MM  # 1250mm — typical inter-luminaire spacing
HULL_BUFFER_MM  = 1025
PERIM_SHRINK_MM = 1600
OUTPUT_SCALE    = 75
COS_A=-0.0176; SIN_A=-0.9998; TX_MM=3930.0; TY_MM=59414.0

# ── Luminaire type catalog ────────────────────────────────────────────────────
#
# A   (*U1528) — MIKA80-E 40° Beam-M-600ma  — interior shelf standard (178 in BN)
# AW  (*U1549) — MIKA80-E 40° Beam-M-high   — exterior / wall shelf   (38 in BN)
# B   (*U1546) — MIKA80-E 60° Beam-F-3200   — wide-beam supplement
# D   (*U1545) — MIKA80-E 60° checkout      — task lighting at checkout
# E   (*U1548) — track spotlight             — Schaufenster
# C   — same spec as A, relabelled at shelf-domain corners
# W   — anti-glare honeycomb (cosmetics areas)
# P   — narrow 24° poster accent

TYPE_A = dict(
    product_code  = "MIKA80-E-WS-930-PH-PS7HE+-L22-2400-40RF-DV2.5-EN*",
    description   = "MIKA80-E K1 Regalbeleuchtung 15W 40° 2400lm 3000K",
    manufacturer  = "MAX FRANKE.led",
    wattage=15, lux_output=2400, beam_angle_deg=40.0,
    mounting_type = "grid_recessed", cutout_mm=128, embed_depth_mm=110,
    ip_rating="IP20", dimmable=True, cri=90, cct_k=3000, lumi_type="A")

# Exterior-wall / wall-boundary shelf luminaire (Beam-M-high driver)
TYPE_AW = dict(
    product_code  = "MIKA80-E-WS-930-PH-PS7HE+-L22-high-40RF-DV2.5-EN*",
    description   = "MIKA80-E K1 Regal Außenwand 40° Beam-M-high 3000K",
    manufacturer  = "MAX FRANKE.led",
    wattage=20, lux_output=3200, beam_angle_deg=40.0,
    mounting_type = "grid_recessed", cutout_mm=128, embed_depth_mm=110,
    ip_rating="IP20", dimmable=True, cri=90, cct_k=3000, lumi_type="AW")

TYPE_B = dict(
    product_code  = "MIKA80-E-WS-930-PH-PS7HE+-L22-3200-60RF-DV2.5-EN*",
    description   = "MIKA80-E K4 Ergänzungsbeleuchtung 20W 60° 3200lm 3000K",
    manufacturer  = "MAX FRANKE.led",
    wattage=20, lux_output=3200, beam_angle_deg=60.0,
    mounting_type = "grid_recessed", cutout_mm=128, embed_depth_mm=110,
    ip_rating="IP20", dimmable=True, cri=90, cct_k=3000, lumi_type="B")

TYPE_C = dict(
    product_code  = "MIKA80-E-WS-930-PH-PS7HE+-L22-2400-40RF-DV2.5-EN*",
    description   = "MIKA80-E K3 Regalbeleuchtung Rand 15W 40° 2400lm 3000K",
    manufacturer  = "MAX FRANKE.led",
    wattage=15, lux_output=2400, beam_angle_deg=40.0,
    mounting_type = "grid_recessed", cutout_mm=128, embed_depth_mm=110,
    ip_rating="IP20", dimmable=True, cri=90, cct_k=3000, lumi_type="C")

TYPE_D = dict(
    product_code  = "MIKA80-E-WS-930-PH-PS7HE+-L22-3200-40RF-DV2.5-EN*",
    description   = "MIKA80-E K2 Checkout/Service 20W 40° 3200lm 3000K",
    manufacturer  = "MAX FRANKE.led",
    wattage=20, lux_output=3200, beam_angle_deg=40.0,
    mounting_type = "grid_recessed", cutout_mm=128, embed_depth_mm=110,
    ip_rating="IP20", dimmable=True, cri=90, cct_k=3000, lumi_type="D")

TYPE_E = dict(
    product_code  = "NEO85-SX-WS-930-PH-PS7HE+-L22-3200-60RF-EN+",
    description   = "NEO85-SX K6 Schaufenster-Strahler 20W 60° 3200lm Track",
    manufacturer  = "MAX FRANKE.led",
    wattage=20, lux_output=3200, beam_angle_deg=60.0,
    mounting_type = "track_3phase", cutout_mm=85, embed_depth_mm=146,
    ip_rating="IP20", dimmable=True, cri=90, cct_k=3000, lumi_type="E")

TYPE_W = dict(
    product_code  = "MIKA80-E-WS-930-PH-PS7HE+-L22-1700-36PP-W-DV2.5-EN*",
    description   = "MIKA80-E Wabeneinsatz 20W 36° 1700lm Anti-Glare 3000K",
    manufacturer  = "MAX FRANKE.led",
    wattage=20, lux_output=1700, beam_angle_deg=36.0,
    mounting_type = "grid_recessed", cutout_mm=128, embed_depth_mm=110,
    ip_rating="IP20", dimmable=True, cri=90, cct_k=3000, lumi_type="W")

TYPE_P = dict(
    product_code  = "MIKA80-E-WS-930-PH-PS7HE+-L15-2100-24PP-DV2.5-EN*",
    description   = "MIKA80-E K5 Plakate 16W 24° 2100lm Power-Linse 3000K",
    manufacturer  = "MAX FRANKE.led",
    wattage=16, lux_output=2100, beam_angle_deg=24.0,
    mounting_type = "grid_recessed", cutout_mm=128, embed_depth_mm=110,
    ip_rating="IP20", dimmable=True, cri=90, cct_k=3000, lumi_type="P")

_TYPE_MAP = {
    'A': TYPE_A, 'AW': TYPE_AW, 'B': TYPE_B, 'C': TYPE_C,
    'D': TYPE_D, 'E': TYPE_E,   'W': TYPE_W, 'P': TYPE_P,
}

# ── ML placement model (loaded once) ─────────────────────────────────────────
import os as _os, pickle as _pickle
from config import MODELS_DIR as _MODELS_DIR, ANNOTATIONS_DIR as _ANNO_DIR

_PLACER_MODEL      = None
_PLACER_MODEL_PATH = _MODELS_DIR / "placer_model.pkl"
_COLLECT_TRAINING  = _os.environ.get("COLLECT_TRAINING", "1") == "1"

def _load_placer_model():
    global _PLACER_MODEL
    if _PLACER_MODEL is None and _PLACER_MODEL_PATH.exists():
        try:
            with open(_PLACER_MODEL_PATH, 'rb') as _f:
                _PLACER_MODEL = _pickle.load(_f)
        except Exception:
            _PLACER_MODEL = None
    return _PLACER_MODEL

def _ml_predict_type(features_vec) -> str | None:
    """
    Predict luminaire type from feature vector.
    Returns type string ('A', 'AW', 'C', 'E') or None if model unavailable.
    """
    clf = _load_placer_model()
    if clf is None:
        return None
    import numpy as np
    try:
        pred = clf.predict(features_vec.reshape(1, -1))[0]
        return pred if pred != 'skip' else None
    except Exception:
        return None

def _save_training_sample(features_vec, lumi_type: str):
    """Append one (features, lumi_type) pair to the placement training JSONL."""
    if not _COLLECT_TRAINING:
        return
    try:
        _ANNO_DIR.mkdir(parents=True, exist_ok=True)
        path = _ANNO_DIR / "placements.jsonl"
        import json as _json
        with open(path, 'a') as _f:
            _f.write(_json.dumps({
                "features":   features_vec.tolist(),
                "lumi_type":  lumi_type,
            }) + "\n")
    except Exception:
        pass


import re as _re_placer

def _parse_depth_mm_p(depth_code: str) -> int:
    """Extract first numeric depth value from code like '57/47' → 57."""
    m = _re_placer.match(r'(\d+)', depth_code or '')
    return int(m.group(1)) if m else 0


def _build_shelf_exclusion(shelf_objs: list, pitch: float = PITCH_MM):
    """
    Return a Shapely union polygon covering all shelf gondola bodies.

    Each shelf INSERT is approximated as a rectangle:
      • Along-run: pitch (625mm — one module width)
      • Perpendicular: depth_code × 10 mm (shelf depth, default 470mm)

    Used to filter luminaire candidates that would land inside a gondola body
    rather than in the ceiling grid above the aisle.
    """
    from shapely.geometry import box as _shbox
    from shapely.ops import unary_union as _uu
    polys = []
    for f in shelf_objs:
        depth_mm = _parse_depth_mm_p(getattr(f, 'depth_code', '') or '') * 10
        if depth_mm <= 0:
            depth_mm = 470   # typical Rossmann gondola depth
        rot     = getattr(f, 'rotation', 0.0) % 180
        sx, sy  = f.position
        half_d  = depth_mm / 2.0
        half_w  = pitch / 2.0
        if rot < 45 or rot > 135:   # horizontal shelf: depth along Y
            polys.append(_shbox(sx - half_w, sy - half_d, sx + half_w, sy + half_d))
        else:                        # vertical shelf: depth along X
            polys.append(_shbox(sx - half_d, sy - half_w, sx + half_d, sy + half_w))
    if not polys:
        return None
    return _uu(polys)


def _shelf_wall_signal(cx: float, cy: float, shelf_objs: list,
                       envelope, cap_mm: float = 2500.0) -> str:
    """
    Determine wall relation from the nearest shelf's assortment and depth code.

    Returns:
      'exterior_wall' — depth >= 67mm OR assortment is a known wall category
      'interior'      — known non-wall assortment + standard depth + far from wall
      ''              — no confident signal; defer to geometric classification

    This overrides the purely geometric wall detection which can be wrong for:
      • Shallow-depth wall assortments (PARFÜM 47mm, KONSUMDUFT 47mm)
      • Interior gondolas near zone boundaries in compact stores (Bad Nenndorf)
    """
    best_d, best_s = cap_mm, None
    for s in shelf_objs:
        ox, oy = s.position[0], s.position[1]
        d = math.sqrt((cx - ox) ** 2 + (cy - oy) ** 2)
        if d < best_d:
            best_d, best_s = d, s
    if best_s is None:
        return ''
    depth_mm = _parse_depth_mm_p(getattr(best_s, 'depth_code', ''))
    assort   = getattr(best_s, 'assortment', '').strip().upper()
    # Strong wall indicators — always AW regardless of geometry
    if depth_mm >= 67:
        return 'exterior_wall'
    # Wall assortment: only promote to exterior_wall when real envelope geometry is
    # available.  Without it (PDF input), sales_area_poly.boundary is used as the
    # exterior wall proxy; the assortment signal would fire for interior gondolas
    # that carry wall-category products (WPR, PARFÜM etc.), overcounting AW by 3×.
    if assort in _WALL_ASSORT_NAMES and envelope is not None:
        return 'exterior_wall'
    # Confirmed interior: known non-wall assortment + standard depth + far from wall
    if depth_mm > 0 and depth_mm < 67 and assort and assort not in _WALL_ASSORT_NAMES:
        if envelope is not None:
            env_dist = Point(cx, cy).distance(envelope.boundary)
            if env_dist > 1250:   # more than 2 tiles from actual building wall
                return 'interior'
    return ''


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class PlacedLuminaire:
    x: float; y: float
    product_code: str; description: str; manufacturer: str
    wattage: float; lux_output: float; zone_type: str
    mounting_type: str; lumi_type: str
    cutout_mm: float = 128.0; embed_depth_mm: float = 110.0
    ip_rating: str = "IP20"; dimmable: bool = True
    cri: int = 90; cct_k: int = 3000; beam_angle_deg: float = 40.0
    rotation: float = 0.0; grid_snapped: bool = True; shelf_aligned: bool = True


@dataclass
class ZoneLightingReport:
    zone_type:           str
    area_m2:             float
    room_width_m:        float
    room_depth_m:        float
    ceiling_height_m:    float
    room_index_k:        float
    utilisation_factor:  float
    target_lux:          int
    required_count:      int
    placed_count:        int
    grid_pitch_mm:       int
    maintained_lux:      float
    luminaire_type:      str
    luminaire_flux_lm:   int

    def maintained_lux_actual(self) -> float:
        A = self.area_m2
        if A <= 0:
            return 0.0
        return round(
            (self.luminaire_flux_lm * self.placed_count *
             self.utilisation_factor * 0.80) / A, 1)

    def summary(self) -> str:
        em_actual = self.maintained_lux_actual()
        return (
            f"{self.zone_type:16s} {self.area_m2:6.1f}m²  "
            f"k={self.room_index_k:.2f}  η={self.utilisation_factor:.2f}  "
            f"pitch={self.grid_pitch_mm}mm  "
            f"req={self.required_count}  placed={self.placed_count}  "
            f"Em={em_actual:.0f}lux (target {self.target_lux}lux)"
        )


@dataclass
class PlacementResult:
    source_file: str
    placed:       list = field(default_factory=list)
    corrections:  list = field(default_factory=list)
    zone_reports: list = field(default_factory=list)

    def total_wattage(self): return sum(p.wattage for p in self.placed)
    def by_type(self, t):    return [p for p in self.placed if p.lumi_type == t]
    def by_zone(self, z):    return [p for p in self.placed if p.zone_type == z]

    def summary(self):
        from collections import Counter
        tc = Counter(p.lumi_type for p in self.placed)
        zc = Counter(p.zone_type for p in self.placed)
        return (f"PlacementResult: {len(self.placed)} luminaires "
                f"{self.total_wattage():.0f}W | Types:{dict(tc)} | Zones:{dict(zc)}")

    def lighting_report(self) -> str:
        lines = [
            "Zone Lighting Report",
            f"{'Zone':16s} {'Area':>7s} {'k':>5s} {'η':>5s} "
            f"{'Pitch':>6s} {'Req':>4s} {'Placed':>6s} {'Em':>6s} {'Target':>7s}",
            "-" * 75,
        ]
        for r in self.zone_reports:
            em = r.maintained_lux_actual()
            ok = "✓" if em >= r.target_lux * 0.80 else "!"
            lines.append(
                f"{r.zone_type:16s} {r.area_m2:6.1f}m² "
                f"{r.room_index_k:5.2f} {r.utilisation_factor:5.3f} "
                f"{r.grid_pitch_mm:5d}mm {r.required_count:4d} "
                f"{r.placed_count:6d} {em:5.0f}lx "
                f"{r.target_lux:5d}lx {ok}"
            )
        return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make(x, y, zone_type, lumi_type, shelf_aligned=True, **kw) -> PlacedLuminaire:
    spec = _TYPE_MAP.get(lumi_type, TYPE_A).copy()
    spec.update(kw)
    return PlacedLuminaire(x=round(x, 1), y=round(y, 1),
                           zone_type=zone_type, shelf_aligned=shelf_aligned, **spec)


def _snap(x, y, pitch, ox, oy):
    return round((x - ox) / pitch) * pitch + ox, round((y - oy) / pitch) * pitch + oy


def _grid_pts(polygon, pitch, ox, oy, clr=300):
    """Generate grid points at `pitch` spacing that fall inside *polygon*."""
    inset = polygon.buffer(-clr)
    if inset.is_empty:
        inset = polygon
    b  = inset.bounds
    sx = math.ceil((b[0] - ox) / pitch) * pitch + ox
    sy = math.ceil((b[1] - oy) / pitch) * pitch + oy
    pts = []
    x = sx
    while x <= b[2] + 1:
        y = sy
        while y <= b[3] + 1:
            if inset.contains(Point(x, y)):
                pts.append((x, y))
            y += pitch
        x += pitch
    return pts


def _is_excluded(x, y, exclusion_zones: list) -> bool:
    if not exclusion_zones:
        return False
    pt = Point(x, y)
    return any(ez.contains(pt) for ez in exclusion_zones)


def _mad_filter(shelf_objs):
    """Reject shelf positions that are extreme statistical outliers (MAD × 8)."""
    if len(shelf_objs) < 3:
        return shelf_objs
    s_xs = sorted(f.position[0] for f in shelf_objs)
    s_ys = sorted(f.position[1] for f in shelf_objs)
    mid  = len(s_xs) // 2
    med_x, med_y = s_xs[mid], s_ys[mid]
    mad_x = max(sorted(abs(x - med_x) for x in s_xs)[mid], 500.0)
    mad_y = max(sorted(abs(y - med_y) for y in s_ys)[mid], 500.0)
    filtered = [f for f in shelf_objs
                if abs(f.position[0] - med_x) <= 8 * mad_x
                and abs(f.position[1] - med_y) <= 8 * mad_y]
    return filtered or shelf_objs


def _out_to_in(ox_mm, oy_mm):
    dx = ox_mm - TX_MM; dy = oy_mm - TY_MM
    return COS_A * dx + SIN_A * dy, -SIN_A * dx + COS_A * dy


def _build_hull(calib_path: Optional[Path] = None):
    import json
    if calib_path and calib_path.exists():
        d    = json.loads(calib_path.read_text())
        hull = Polygon([(c[0], c[1]) for c in d['hull_coords_mm']])
        return hull, hull.buffer(d.get('hull_buffer_mm', HULL_BUFFER_MM)), \
               hull.buffer(-d.get('perimeter_shrink_mm', PERIM_SHRINK_MM))
    ref = Path("/mnt/user-data/uploads/Ro_Hamburg_Jungfernstieg_3600_20260113-EG-DRP.pdf")
    if ref.exists():
        return _hull_from_pdf(ref)
    return None, None, None


def _hull_from_pdf(pdf_path):
    from services.parser.pdf_parser import _open_pdf
    doc  = _open_pdf(str(pdf_path)); page = doc[0]; ph = page.rect.height
    paths = page.get_drawings(); PT2MM = (25.4 / 72.0) * OUTPUT_SCALE

    def cluster(pts, thr=5):
        used = set(); out = []
        for i, p in enumerate(pts):
            if i in used: continue
            grp = [p]
            for j, q in enumerate(pts):
                if j != i and j not in used and math.dist(p, q) < thr:
                    grp.append(q); used.add(j)
            used.add(i)
            out.append((sum(g[0] for g in grp) / len(grp),
                        sum(g[1] for g in grp) / len(grp)))
        return out

    A_c, B_c = [], []
    for path in paths:
        col = path.get('color'); r = path['rect']
        if col and abs(col[0]-1.0)<0.01 and abs(col[1]-0.0)<0.01 and abs(col[2]-1.0)<0.01:
            if 22 < r.width < 26 and 22 < r.height < 26:
                A_c.append(((r.x0+r.x1)/2, (r.y0+r.y1)/2))
        elif col and abs(col[0]-1.0)<0.01 and abs(col[1]-0.0)<0.01 and \
                (len(col) < 3 or abs(col[2]-0.0) < 0.01):
            if 22 < r.width < 26 and 22 < r.height < 26:
                B_c.append(((r.x0+r.x1)/2, (r.y0+r.y1)/2))

    lA = [(x*PT2MM, (ph-y)*PT2MM) for x, y in cluster(A_c, 5)]
    lB = [(x*PT2MM, (ph-y)*PT2MM) for x, y in cluster(B_c, 5)]
    real_in = [_out_to_in(x, y) for x, y in lA + lB]
    hull    = MultiPoint(real_in).convex_hull
    hb      = hull.buffer(HULL_BUFFER_MM)
    inner   = hull.buffer(-PERIM_SHRINK_MM)

    calib_dir = Path(__file__).parent.parent.parent / "data/annotations"
    calib_dir.mkdir(parents=True, exist_ok=True)
    import json
    json.dump({
        "hull_coords_mm": [[round(x,1), round(y,1)] for x, y in hull.exterior.coords],
        "hull_buffer_mm": HULL_BUFFER_MM, "perimeter_shrink_mm": PERIM_SHRINK_MM,
        "result": {"total": len(lA)+len(lB), "type_A": len(lA), "type_B": len(lB)},
        "target": {"total": 167, "type_A": 106, "type_B": 61}
    }, open(calib_dir / "calibration_rossmann_eg.json", "w"), indent=2)
    return hull, hb, inner


# ── Main placer ───────────────────────────────────────────────────────────────

class RealLuminairePlacer:
    CALIB_PATH = Path(__file__).parent.parent.parent / \
                 "data/annotations/calibration_rossmann_eg.json"

    def __init__(self):
        self._hull, self._hb, self._inner = _build_hull(self.CALIB_PATH)

    def place_all(self, plan: ParsedPlan, classified: ClassifiedPlan,
                  active_zone_types='all') -> PlacementResult:
        # Grid origin: from plan (auto-detected from MF_Raster or set by annotation)
        grid_origin = getattr(plan, 'grid_origin_mm', (0.0, 0.0))
        if grid_origin != (0.0, 0.0):
            ox, oy = grid_origin
        else:
            sf_z = next((z for z in classified.zones if z.zone_type == 'sales_floor'), None)
            if sf_z is None:
                sf_z = max(classified.zones, key=lambda z: z.area_m2) if classified.zones else None
            georef = (sf_z is not None and
                      any(abs(c) > 10_000_000 for c in sf_z.polygon.bounds))
            if georef:
                ox, oy = 0.0, 0.0
            elif sf_z is not None:
                zb = sf_z.polygon.bounds
                ox = zb[0] + 1000.0
                oy = zb[1] + 2000.0
            elif getattr(plan, 'bounds', None):
                ox = plan.bounds[0] + 1000.0
                oy = plan.bounds[1] + 2000.0
            else:
                ox, oy = 1160.0, 500.0

        base_pitch  = PITCH_MM           # 625mm tile — hard-configured
        ceiling_mm  = int(getattr(plan, 'ceiling_height_mm', 3000) or 3000)

        result   = PlacementResult(source_file=plan.source_file)
        excl     = getattr(plan, 'exclusion_zones', [])
        envelope = getattr(plan, 'building_envelope', None)

        for zone in classified.zones:
            zt = zone.zone_type

            if active_zone_types != 'all' and zt not in active_zone_types:
                continue

            if zt in NO_LIGHTING_ZONE_TYPES:
                continue

            if zone.area_m2 < 10:
                continue

            if zt != 'sales_floor' and self._hb is not None:
                frac = (self._hb.intersection(zone.polygon).area /
                        max(zone.polygon.area, 1))
                if frac > 0.4:
                    continue

            spec = _zone_spec(
                zt, zone.area_m2, zone.polygon.bounds,
                ceiling_mm=getattr(zone, 'ceiling_height_mm', ceiling_mm),
                base_pitch=base_pitch,
            )
            zone_pitch = spec.grid_pitch_mm

            if zt == 'checkout_zone':
                zone_pitch = INTER_LUMI_MM   # 1250mm standard for checkout
                # Mis-detection guard
                shelf_positions = [f.position for f in plan.furniture
                                   if f.inferred_type == 'shelving']
                if envelope is not None:
                    _ep = envelope.buffer(2000)
                    _sp = [p for p in shelf_positions if _ep.covers(Point(*p))]
                    if _sp:
                        shelf_positions = _sp
                if shelf_positions:
                    from shapely.geometry import MultiPoint as _MP
                    nearest_shelf_dist = zone.polygon.centroid.distance(
                        _MP(shelf_positions))
                    if nearest_shelf_dist > 20_000:
                        result.corrections.append(
                            f"Checkout zone skipped: centroid is "
                            f"{nearest_shelf_dist/1000:.0f}m from nearest shelf")
                        continue

            effective_excl = excl[:]
            if zt == 'sales_floor':
                for other in classified.zones:
                    if other.zone_type in ('checkout_zone', 'service_area',
                                           'office', 'storage'):
                        effective_excl.append(other.polygon)
            elif zt == 'checkout_zone':
                # Prevent D-lights from spilling into the shelf-occupied sales floor.
                # ONLY add the exclusion when the checkout zone is mostly OUTSIDE
                # the sales floor (i.e. a truly separate zone).  When checkout is
                # largely inside the sales floor polygon (> 40% overlap — common in
                # Rossmann layouts where checkouts sit at the front of the sales
                # floor), adding the whole sales-floor polygon as exclusion would
                # wipe out all D-light candidates.
                for other in classified.zones:
                    if other.zone_type == 'sales_floor':
                        checkout_area = zone.polygon.area
                        if checkout_area > 0:
                            overlap = zone.polygon.intersection(other.polygon).area
                            if overlap / checkout_area < 0.4:
                                effective_excl.append(other.polygon)

            placed_before = len(result.placed)
            result.placed.extend(
                self._place_zone(zone, plan, effective_excl, zone_pitch, ox, oy))
            placed_count = len(result.placed) - placed_before

            result.zone_reports.append(ZoneLightingReport(
                zone_type          = zt,
                area_m2            = zone.area_m2,
                room_width_m       = spec.room_width_m,
                room_depth_m       = spec.room_depth_m,
                ceiling_height_m   = spec.ceiling_height_m,
                room_index_k       = spec.room_index_k,
                utilisation_factor = spec.utilisation_factor,
                target_lux         = spec.target_lux,
                required_count     = spec.required_count,
                placed_count       = placed_count,
                grid_pitch_mm      = zone_pitch,
                maintained_lux     = spec.maintained_lux,
                luminaire_type     = spec.luminaire_type,
                luminaire_flux_lm  = spec.luminaire_flux_lm,
            ))

        # ── Building-envelope boundary guard ─────────────────────────────────
        # Guard must use REAL wall geometry only — not synthetic zone polygons.
        # Synthetic zones (especially checkout, built from furniture + 2500mm buffer)
        # extend outside actual walls and defeat the guard if included.
        #
        # Priority:
        #   1. building_envelope  — detected from actual DXF closed polylines (best)
        #   2. plan.room_polygons — all closed LWPOLYLINE shapes from the DXF
        #   3. zone polygons derived from actual DXF room shapes (method = 'label'/'rule')
        from shapely.ops import unary_union as _uu
        _guard_polys = []
        if envelope is not None:
            _guard_polys.append(envelope.buffer(300))   # 300mm tolerance for wall-edge lights
        # Add actual room polygons — but filter out:
        #   - Drawing frames / title blocks (area > 1000 m²)
        #   - Tiny furniture footprints (area < 5 m²) — shelf units, counter modules
        _room_polys = [rp for rp in getattr(plan, 'room_polygons', [])
                       if 5e6 <= rp.area <= 1000e6]
        for _rp in _room_polys:
            _guard_polys.append(_rp.buffer(200))
        # Add the CONVEX HULL of all real rooms + envelope.  This fills in gaps that
        # exist between named rooms in the DXF (e.g. SB checkout vestibule, Windfang)
        # which are physically inside the building but have no own room polygon.
        _hull_src = _room_polys[:]
        if envelope is not None:
            _hull_src.append(envelope)
        if _hull_src:
            _building_hull = _uu(_hull_src).convex_hull.buffer(500)
            _guard_polys.append(_building_hull)
        if not _guard_polys:
            # Last resort: zone polygons that came from real DXF shapes, tight buffer.
            # Skip for PDF input (source_file ends in .pdf) — zone polygons there are
            # built from text labels and furniture; using them as wall boundaries would
            # strip checkout D-lights that sit outside the sales-floor polygon.
            _is_pdf = str(getattr(plan, 'source_file', '')).lower().endswith('.pdf')
            if not _is_pdf:
                for _z in classified.zones:
                    if getattr(_z, 'method', '') in ('label', 'rule'):
                        _guard_polys.append(_z.polygon.buffer(200))
        if _guard_polys:
            _env_guard = _uu(_guard_polys)
            before = len(result.placed)
            result.placed = [l for l in result.placed
                             if _env_guard.covers(Point(l.x, l.y))]
            clipped = before - len(result.placed)
            if clipped > 0:
                result.corrections.append(
                    f"Boundary guard removed {clipped} lights outside floor-plan walls")

        return result

    def _place_zone(self, zone: ZoneResult, plan: ParsedPlan,
                    excl: list, pitch: float, ox: float, oy: float) -> list:
        zt = zone.zone_type
        if zt == 'sales_floor':
            return self._place_sales(zone, plan, excl, ox, oy)
        elif zt == 'checkout_zone':
            return self._place_checkout(zone, plan, excl, pitch, ox, oy)
        elif zt == 'storage':
            return self._place_storage(zone, excl, ox, oy)
        elif zt == 'display_window':
            return self._place_display_window(zone, excl, ox, oy)
        elif zt in ('corridor', 'entrance'):
            return self._place_corridor(zone, excl, pitch, ox, oy)
        elif zt in ('service_area', 'office'):
            return self._place_service(zone, excl, pitch, ox, oy)
        return self._place_grid_default(zone, excl, INTER_LUMI_MM, ox, oy)

    # ── Sales floor ───────────────────────────────────────────────────────────

    def _place_sales(self, zone: ZoneResult, plan: ParsedPlan,
                     excl: list, ox: float, oy: float) -> list:
        """
        Grid-based luminaire placement for the Verkaufsfläche.

        Core principle (from technical documents):
          "Not: furniture A gets luminaire X.
           But: every legal grid position is scored for every allowed type."

        Algorithm:
          1. Collect shelf INSERT objects; filter by building envelope + MAD
          2. Group shelves by orientation (horizontal / vertical from block rotation)
          3. For each shelf row: generate positions at 1250mm intervals
             (every 2nd 625mm tile — the standard Rossmann inter-luminaire spacing)
          4. Classify each position:
             exterior wall (≤ 625mm from outer contour) → TYPE_AW (high beam)
             interior                                   → TYPE_A  (standard)
          5. B-lights: positions at the outer boundary of the A-light domain
             (within 1 grid-step of the zone edge but NOT classified as AW)
          6. C-lights: relabel 3 corner A-lights at the shelf-domain corners
          7. E-lights: column-adjacent grid nodes
          8. W-lights: replace with anti-glare variant at cosmetics label positions
        """
        pitch = PITCH_MM        # 625mm tile
        step  = INTER_LUMI_MM   # 1250mm inter-luminaire spacing (2 tiles)

        # ── 1. Collect and filter shelf objects ────────────────────────────
        raw_objs = [f for f in plan.furniture if f.inferred_type == 'shelving']
        if not raw_objs:
            return self._place_grid_default(zone, excl, INTER_LUMI_MM, ox, oy)

        envelope = getattr(plan, 'building_envelope', None)
        if envelope is not None:
            _env_buf = envelope.buffer(2000)
            _in_env  = [f for f in raw_objs if _env_buf.covers(Point(*f.position))]
            if _in_env:
                raw_objs = _in_env

        shelf_objs = _mad_filter(raw_objs)

        # Restrict shelf objects to those inside (or within 300mm of) the sales floor
        # zone polygon.  This prevents back-room or storage shelves from generating
        # candidates that land outside the sales floor area.
        _sf_buf = zone.polygon.buffer(300)
        _sf_shelves = [f for f in shelf_objs if _sf_buf.covers(Point(*f.position))]
        if _sf_shelves:
            shelf_objs = _sf_shelves

        # ── 2. Generate shelf-row candidates at 1250mm intervals ──────────
        candidates = generate_shelf_row_candidates(
            shelf_objects = shelf_objs,
            zone_poly     = zone.polygon,
            ox=ox, oy=oy,
            pitch         = pitch,
            spacing_mm    = step,
            subpos        = 'A_center',
            clearance_mm  = pitch * 1.5,  # 937mm — covers wall gondola edge candidates
        )

        if not candidates:
            # Fallback: if shelf detection failed (PDF input or unusual DXF),
            # use a uniform area grid at 1250mm spacing
            candidates = generate_area_candidates(
                zone_poly    = zone.polygon,
                ox=ox, oy=oy,
                pitch        = pitch,
                spacing_mm   = step,
                subpos       = 'A_center',
                clearance_mm = 300.0,
            )

        # ── Shelf-body exclusion ──────────────────────────────────────────────
        # Remove any candidate that falls inside a gondola body polygon.
        # This is the final safety net: candidates should already be placed in
        # aisles (midpoints between gondola rows), but any stray on-shelf
        # candidate generated by legacy code paths is discarded here.
        _shelf_excl_union = _build_shelf_exclusion(shelf_objs, pitch)
        if _shelf_excl_union is not None:
            _before = len(candidates)
            candidates = [c for c in candidates
                          if not _shelf_excl_union.contains(Point(c.x, c.y))]
            _removed = _before - len(candidates)
            if _removed > 0:
                pass  # silently removed; these were on-shelf positions

        # ── 3. Classify wall relation for each candidate ──────────────────
        positions = [(c.x, c.y) for c in candidates]
        wall_rels = classify_wall_relation(
            positions         = positions,
            building_envelope = envelope,
            sales_area_poly   = zone.polygon,
        )

        # Column objects for feature extraction
        col_objs = [f for f in plan.furniture if f.inferred_type == 'column']

        # ── 4. Place A-lights and AW-lights via ML (with rule fallback) ───
        visited: set = set()
        placed:  list = []
        use_ml = _load_placer_model() is not None

        for c, wall_rel in zip(candidates, wall_rels):
            # Candidates pre-tagged as exterior_wall in ceiling_grid (wall gondola
            # pass for outermost rows) override the distance-based result.
            if getattr(c, 'wall_relation', 'interior') == 'exterior_wall':
                wall_rel = 'exterior_wall'

            # Assortment / depth signal — overrides geometric classification:
            #   • depth >= 67mm or wall assortment → force exterior_wall
            #   • known non-wall assortment + standard depth + far from wall →
            #     correct false-positive exterior_wall (fixes Bad Nenndorf +15 B)
            _sig = _shelf_wall_signal(c.x, c.y, shelf_objs, envelope)
            if _sig == 'exterior_wall':
                wall_rel = 'exterior_wall'
            elif _sig == 'interior' and wall_rel == 'exterior_wall':
                wall_rel = 'interior'

            # ── Rule-based type (always computed as fallback / training label) ──
            rule_type = 'AW' if wall_rel == 'exterior_wall' else 'A'

            # ── ML-based type prediction ──────────────────────────────────────
            if use_ml:
                fv = _extract_features(c, zone, shelf_objs, col_objs)
                ml_pred = _ml_predict_type(fv)
                if wall_rel == 'exterior_wall':
                    # Geometry says this candidate is at the building wall — only let
                    # ML confirm AW; any other ML prediction demotes to rule_type=AW
                    # to avoid ML overriding correct geometric classification.
                    lumi_type = 'AW' if ml_pred == 'AW' else rule_type
                else:
                    lumi_type = ml_pred if ml_pred in ('A', 'AW') else rule_type
            else:
                fv = None
                lumi_type = rule_type

            # Collect training data: save feature vector with rule label
            # (rule label is used as teacher signal until real corrections arrive)
            if fv is not None:
                _save_training_sample(fv, rule_type)

            # AW sits at B_corner; A/C/E sit at A_center
            subpos = 'B_corner' if lumi_type == 'AW' else 'A_center'
            cx, cy = snap_to_subposition(c.x, c.y, ox, oy, pitch, subpos)
            key = (round(cx), round(cy))
            if key in visited:
                continue
            if _is_excluded(cx, cy, excl):
                continue
            visited.add(key)
            placed.append(_make(cx, cy, zone.zone_type, lumi_type,
                                shelf_aligned=True))

        if not placed:
            return []

        a_keys = frozenset((round(p.x), round(p.y))
                           for p in placed if p.lumi_type in ('A', 'AW'))

        # ── 5. E-lights — column-adjacent nodes ──────────────────────────
        col_raw = [(f.position[0], f.position[1])
                   for f in plan.furniture if f.inferred_type == 'column']

        col_dedup: list = []
        for cp in col_raw:
            if not any(math.sqrt((cp[0]-u[0])**2 + (cp[1]-u[1])**2) < 300.0
                       for u in col_dedup):
                col_dedup.append(cp)

        zone_near = zone.polygon.buffer(200)
        col_in_zone = [cp for cp in col_dedup
                       if zone_near.covers(Point(cp[0], cp[1]))]

        if col_in_zone:
            for col_x, col_y in col_in_zone:
                snap_cx = round((col_x - ox) / pitch) * pitch + ox
                snap_cy = round((col_y - oy) / pitch) * pitch + oy
                near = sorted(
                    [(snap_cx + di * pitch, snap_cy + dj * pitch)
                     for di in range(-2, 3) for dj in range(-2, 3)
                     if math.sqrt((snap_cx + di*pitch - col_x)**2 +
                                  (snap_cy + dj*pitch - col_y)**2) < pitch * 1.5],
                    key=lambda p: math.sqrt((p[0] - col_x)**2 + (p[1] - col_y)**2),
                )
                placed_for_col = 0
                for gx, gy in near:
                    if placed_for_col >= 2:
                        break
                    # Use E_special sub-position within the tile
                    cx, cy = snap_to_subposition(gx, gy, ox, oy, pitch, 'E_special')
                    key = (round(cx), round(cy))
                    if key not in visited and not _is_excluded(cx, cy, excl):
                        visited.add(key)
                        placed.append(_make(cx, cy, zone.zone_type, 'E',
                                            shelf_aligned=False))
                        placed_for_col += 1
        else:
            # Area-based E-light estimate for stores with many shelves
            if len(placed) >= 50:
                target_e = max(0, round(zone.area_m2 * 0.038))
                reachable: set = set(a_keys)
                for ax, ay in a_keys:
                    for dx, dy in [(step, 0), (-step, 0), (0, step), (0, -step)]:
                        reachable.add((round(ax + dx), round(ay + dy)))
                e_cands = sorted(
                    [(gx, gy) for gx, gy in reachable
                     if (round(gx), round(gy)) not in visited
                     and not _is_excluded(gx, gy, excl)
                     and zone.polygon.buffer(200).contains(Point(gx, gy))],
                    key=lambda p: (round((p[1] - oy) / pitch),
                                   round((p[0] - ox) / pitch)),
                )
                n = len(e_cands)
                step_e = max(1, n // max(target_e, 1))
                e_placed = 0
                for i in range(0, n, step_e):
                    if e_placed >= target_e:
                        break
                    gx, gy = e_cands[i]
                    cx, cy = snap_to_subposition(gx, gy, ox, oy, pitch, 'E_special')
                    visited.add((round(cx), round(cy)))
                    placed.append(_make(cx, cy, zone.zone_type, 'E', shelf_aligned=False))
                    e_placed += 1

        # ── 7. C-lights — relabel 3 corner A-lights ──────────────────────
        shelf_pts = [f.position for f in shelf_objs]
        if shelf_pts:
            sx_list = [p[0] for p in shelf_pts]
            sy_list = [p[1] for p in shelf_pts]
            corners = [
                (min(sx_list), min(sy_list)), (max(sx_list), min(sy_list)),
                (max(sx_list), max(sy_list)),
            ]
            relabeled_c: set = set()
            for cx, cy in corners:
                a_cands = [(i, p) for i, p in enumerate(placed)
                           if p.lumi_type in ('A', 'AW') and i not in relabeled_c]
                if not a_cands:
                    break
                ni = min(a_cands,
                         key=lambda ip: math.sqrt((ip[1].x - cx) ** 2 +
                                                  (ip[1].y - cy) ** 2))[0]
                relabeled_c.add(ni)
                old = placed[ni]
                placed[ni] = _make(old.x, old.y, old.zone_type, 'C', shelf_aligned=False)

        # ── 8. W — replace at cosmetics/perfume label positions ───────────
        cosmetics = [lbl for lbl in getattr(plan, 'zone_labels', [])
                     if any(k in lbl.get('text', '').lower()
                            for k in ('kosmetik', 'parfüm', 'duft', 'gobo',
                                      'beauty', 'mood'))]
        for cz in cosmetics[:6]:
            cx, cy = snap_to_subposition(
                cz['x_mm'], cz['y_mm'], ox, oy, pitch, 'A_center')
            key = (round(cx), round(cy))
            if key in visited:
                placed = [p for p in placed
                          if not (round(p.x) == round(cx) and
                                  round(p.y) == round(cy))]
                placed.append(_make(cx, cy, zone.zone_type, 'W', shelf_aligned=False))

        return placed

    # ── Checkout zone ─────────────────────────────────────────────────────────

    def _place_checkout(self, zone: ZoneResult, plan: ParsedPlan,
                        excl: list, pitch: float, ox: float, oy: float) -> list:
        poly = zone.polygon

        # Clip synthetic checkout polygon to real building geometry.
        # The checkout zone is built from furniture + 3000mm buffer and bleeds
        # through exterior walls. Intersecting with the building convex hull
        # (envelope + real room polygons) keeps D/E lights inside the walls.
        from shapely.ops import unary_union as _uu_co
        _envelope = getattr(plan, 'building_envelope', None)
        _rp_co = [rp for rp in getattr(plan, 'room_polygons', [])
                  if 2e6 <= rp.area <= 1000e6]
        _hull_src = _rp_co[:]
        if _envelope is not None:
            _hull_src.append(_envelope)
        if _hull_src:
            _bldg = _uu_co(_hull_src).convex_hull.buffer(600)
            _clipped = poly.intersection(_bldg)
            if not _clipped.is_empty:
                poly = _clipped

        placed = []; visited = set()

        # D-lights must snap to the D_special sub-position (312.5, 150) within
        # each 625mm ceiling tile — NOT to the raw grid / tile origin.
        for f in plan.furniture:
            if f.inferred_type != 'checkout':
                continue
            if not poly.contains(Point(f.position)):
                continue
            gx, gy = _snap(*f.position, INTER_LUMI_MM, ox, oy)
            cx, cy = snap_to_subposition(gx, gy, ox, oy, PITCH_MM, 'D_special')
            key = (round(cx), round(cy))
            if key in visited or _is_excluded(cx, cy, excl):
                continue
            visited.add(key)
            placed.append(_make(cx, cy, zone.zone_type, 'D', shelf_aligned=False))

        for x, y in _grid_pts(poly, INTER_LUMI_MM, ox, oy, clr=200):
            cx, cy = snap_to_subposition(x, y, ox, oy, PITCH_MM, 'D_special')
            key = (round(cx), round(cy))
            if key not in visited and not _is_excluded(cx, cy, excl):
                visited.add(key)
                placed.append(_make(cx, cy, zone.zone_type, 'D', shelf_aligned=False))

        return placed

    # ── Storage ───────────────────────────────────────────────────────────────

    def _place_storage(self, zone: ZoneResult, excl: list,
                       ox: float, oy: float) -> list:
        return [_make(x, y, zone.zone_type, 'B', shelf_aligned=False)
                for x, y in _grid_pts(zone.polygon, INTER_LUMI_MM, ox, oy, clr=300)
                if not _is_excluded(x, y, excl)]

    # ── Window display ────────────────────────────────────────────────────────

    def _place_display_window(self, zone: ZoneResult, excl: list,
                              ox: float, oy: float) -> list:
        poly = zone.polygon; b = poly.bounds; pts = []
        TRACK_OFFSET_MM = 250

        width  = b[2] - b[0]
        height = b[3] - b[1]

        if width >= height:
            track_y = b[1] + TRACK_OFFSET_MM
            if not poly.contains(Point((b[0]+b[2])/2, track_y)):
                track_y = b[3] - TRACK_OFFSET_MM
            x = math.ceil((b[0] - ox) / INTER_LUMI_MM) * INTER_LUMI_MM + ox
            while x <= b[2]:
                if poly.contains(Point(x, track_y)) and not _is_excluded(x, track_y, excl):
                    pts.append((x, track_y))
                x += INTER_LUMI_MM
        else:
            track_x = b[0] + TRACK_OFFSET_MM
            if not poly.contains(Point(track_x, (b[1]+b[3])/2)):
                track_x = b[2] - TRACK_OFFSET_MM
            y = math.ceil((b[1] - oy) / INTER_LUMI_MM) * INTER_LUMI_MM + oy
            while y <= b[3]:
                if poly.contains(Point(track_x, y)) and not _is_excluded(track_x, y, excl):
                    pts.append((track_x, y))
                y += INTER_LUMI_MM

        return [_make(x, y, zone.zone_type, 'E', shelf_aligned=False) for x, y in pts]

    # ── Corridor / entrance ────────────────────────────────────────────────────

    def _place_corridor(self, zone: ZoneResult, excl: list,
                        pitch: float, ox: float, oy: float) -> list:
        poly = zone.polygon; b = poly.bounds; pts = []
        lumi_type = 'B' if zone.zone_type == 'entrance' else 'A'
        if (b[2] - b[0]) >= (b[3] - b[1]):
            cy = (b[1] + b[3]) / 2
            x  = math.ceil((b[0] - ox) / INTER_LUMI_MM) * INTER_LUMI_MM + ox
            while x <= b[2]:
                if poly.contains(Point(x, cy)) and not _is_excluded(x, cy, excl):
                    pts.append((x, cy))
                x += INTER_LUMI_MM
        else:
            cx = (b[0] + b[2]) / 2
            y  = math.ceil((b[1] - oy) / INTER_LUMI_MM) * INTER_LUMI_MM + oy
            while y <= b[3]:
                if poly.contains(Point(cx, y)) and not _is_excluded(cx, y, excl):
                    pts.append((cx, y))
                y += INTER_LUMI_MM
        return [_make(x, y, zone.zone_type, lumi_type) for x, y in pts]

    # ── Service / office ──────────────────────────────────────────────────────

    def _place_service(self, zone: ZoneResult, excl: list,
                       pitch: float, ox: float, oy: float) -> list:
        lumi_type = 'B'
        if zone.area_m2 < 5:
            b = zone.polygon.bounds
            cx = (b[0] + b[2]) / 2; cy = (b[1] + b[3]) / 2
            if _is_excluded(cx, cy, excl):
                return []
            return [_make(cx, cy, zone.zone_type, lumi_type)]
        return [_make(x, y, zone.zone_type, lumi_type, shelf_aligned=False)
                for x, y in _grid_pts(zone.polygon, INTER_LUMI_MM, ox, oy, clr=300)
                if not _is_excluded(x, y, excl)]

    # ── Generic grid fallback ─────────────────────────────────────────────────

    def _place_grid_default(self, zone: ZoneResult, excl: list,
                            pitch: float, ox: float, oy: float) -> list:
        inner = zone.polygon.buffer(-PERIM_SHRINK_MM)
        return [_make(x, y, zone.zone_type,
                      'A' if (not inner.is_empty and inner.contains(Point(x, y))) else 'C')
                for x, y in _grid_pts(zone.polygon, INTER_LUMI_MM, ox, oy)
                if not _is_excluded(x, y, excl)]


# ── CLI self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from services.parser.pdf_parser import RealPlanParser
    from services.classifier.room_classifier_real import RealRoomClassifier
    UP = Path("/mnt/user-data/uploads")
    plan       = RealPlanParser().parse(UP / "3600_HH_Jungfernstieg_EG_SB_Kassen_20240506.pdf")
    classified = RealRoomClassifier().classify(plan)
    result     = RealLuminairePlacer().place_all(plan, classified)
    print(result.summary())
    print()
    print(result.lighting_report())
