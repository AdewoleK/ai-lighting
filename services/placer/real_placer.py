"""
lighting-ai/services/placer/real_placer.py

Luminaire placer for real Rossmann plans.

Algorithm:
  1. Compute per-zone lighting specification (EN 12464-1 lumen method).
     Each zone type has a target lux level; room dimensions determine the
     Room Index k, which drives the utilisation factor η and therefore the
     required luminaire count n and optimal grid spacing.
  2. Shelf-guided placement for sales floors with sufficient shelf data.
     Falls back to uniform lux-calculated grid when shelf data is sparse.
  3. Inner nodes → Type A (15W 40°), perimeter nodes → Type B (20W 60°).
  4. Special positions: Type C (accent), D (IP44 entrance), E (pendant).
  5. All candidate points checked against exclusion zones.
  6. Grid origin read from plan.grid_origin_mm (auto-detected or from LISP).
"""
from __future__ import annotations
import json, math
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

# ── Calibrated geometry constants ─────────────────────────────────────────────
PITCH_MM        = BASE_PITCH   # 1250 — Rossmann Startmaß (fallback)
HULL_BUFFER_MM  = 1025
PERIM_SHRINK_MM = 1600
OUTPUT_SCALE    = 75
COS_A=-0.0176; SIN_A=-0.9998; TX_MM=3930.0; TY_MM=59414.0

# ── Luminaire type specs ──────────────────────────────────────────────────────
# All types sourced from the Rossmann Hamburg Jungfernstieg 3600 lighting plans
# (MAX FRANKE.led, Jan 2026).  K-codes match the plan legend categories.

TYPE_A = dict(product_code="MIKA80-E-WS-930-PH-PS7HE+-L22-2400-40RF-DV2.5-EN*",
              description="MIKA80-E K1 Regalbeleuchtung 15W 40° 2400lm 3000K",
              manufacturer="MAX FRANKE.led", wattage=15, lux_output=2400,
              mounting_type="grid_recessed", cutout_mm=128, embed_depth_mm=110,
              ip_rating="IP20", dimmable=True, cri=90, cct_k=3000,
              beam_angle_deg=40.0, lumi_type="A")

# K4 — wide-angle supplemental fill (areas not covered by shelf-guided K1)
TYPE_B = dict(product_code="MIKA80-E-WS-930-PH-PS7HE+-L22-3200-60RF-DV2.5-EN*",
              description="MIKA80-E K4 Ergänzungsbeleuchtung 20W 60° 3200lm 3000K",
              manufacturer="MAX FRANKE.led", wattage=20, lux_output=3200,
              mounting_type="grid_recessed", cutout_mm=128, embed_depth_mm=110,
              ip_rating="IP20", dimmable=True, cri=90, cct_k=3000,
              beam_angle_deg=60.0, lumi_type="B")

# K3 — shelf edge / perimeter rows (same physical spec as K1, different position)
TYPE_C = dict(product_code="MIKA80-E-WS-930-PH-PS7HE+-L22-2400-40RF-DV2.5-EN*",
              description="MIKA80-E K3 Regalbeleuchtung Rand 15W 40° 2400lm 3000K",
              manufacturer="MAX FRANKE.led", wattage=15, lux_output=2400,
              mounting_type="grid_recessed", cutout_mm=128, embed_depth_mm=110,
              ip_rating="IP20", dimmable=True, cri=90, cct_k=3000,
              beam_angle_deg=40.0, lumi_type="C")

# K2 — stronger 40° downlight for checkout counters and service areas
TYPE_D = dict(product_code="MIKA80-E-WS-930-PH-PS7HE+-L22-3200-40RF-DV2.5-EN*",
              description="MIKA80-E K2 Checkout/Service 20W 40° 3200lm 3000K",
              manufacturer="MAX FRANKE.led", wattage=20, lux_output=3200,
              mounting_type="grid_recessed", cutout_mm=128, embed_depth_mm=110,
              ip_rating="IP20", dimmable=True, cri=90, cct_k=3000,
              beam_angle_deg=40.0, lumi_type="D")

# K6 — NEO85-SX track spotlight for window display (Schaufenster)
TYPE_E = dict(product_code="NEO85-SX-WS-930-PH-PS7HE+-L22-3200-60RF-EN+",
              description="NEO85-SX K6 Schaufenster-Strahler 20W 60° 3200lm Track",
              manufacturer="MAX FRANKE.led", wattage=20, lux_output=3200,
              mounting_type="track_3phase", cutout_mm=85, embed_depth_mm=146,
              ip_rating="IP20", dimmable=True, cri=90, cct_k=3000,
              beam_angle_deg=60.0, lumi_type="E")

# Wabeneinsatz — honeycomb anti-glare for cosmetics and glare-sensitive areas
TYPE_W = dict(product_code="MIKA80-E-WS-930-PH-PS7HE+-L22-1700-36PP-W-DV2.5-EN*",
              description="MIKA80-E Wabeneinsatz 20W 36° 1700lm Anti-Glare 3000K",
              manufacturer="MAX FRANKE.led", wattage=20, lux_output=1700,
              mounting_type="grid_recessed", cutout_mm=128, embed_depth_mm=110,
              ip_rating="IP20", dimmable=True, cri=90, cct_k=3000,
              beam_angle_deg=36.0, lumi_type="W")

# K5 Plakate — narrow 24° power-lens for poster / promotional banner illumination
TYPE_P = dict(product_code="MIKA80-E-WS-930-PH-PS7HE+-L15-2100-24PP-DV2.5-EN*",
              description="MIKA80-E K5 Plakate 16W 24° 2100lm Power-Linse 3000K",
              manufacturer="MAX FRANKE.led", wattage=16, lux_output=2100,
              mounting_type="grid_recessed", cutout_mm=128, embed_depth_mm=110,
              ip_rating="IP20", dimmable=True, cri=90, cct_k=3000,
              beam_angle_deg=24.0, lumi_type="P")

SHELF_LABELS = {'57','47','77','37','67','27','57/47','47/37','77/57','57/37'}


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class PlacedLuminaire:
    x:float; y:float; product_code:str; description:str; manufacturer:str
    wattage:float; lux_output:float; zone_type:str; mounting_type:str; lumi_type:str
    cutout_mm:float=128.0; embed_depth_mm:float=110.0; ip_rating:str="IP20"
    dimmable:bool=True; cri:int=90; cct_k:int=3000; beam_angle_deg:float=40.0
    rotation:float=0.0; grid_snapped:bool=True; shelf_aligned:bool=True


@dataclass
class ZoneLightingReport:
    """Per-zone lighting calculation result stored on PlacementResult."""
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
    maintained_lux:      float   # achieved Em with actual placed count
    luminaire_type:      str
    luminaire_flux_lm:   int

    def maintained_lux_actual(self) -> float:
        """Recalculate Em from the actual placed_count."""
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
    zone_reports: list = field(default_factory=list)  # list[ZoneLightingReport]

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
            "Zone Lighting Report (EN 12464-1 Lumen Method)",
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


# ── Geometry helpers ──────────────────────────────────────────────────────────

_TYPE_MAP = {
    'A': TYPE_A, 'B': TYPE_B, 'C': TYPE_C,
    'D': TYPE_D, 'E': TYPE_E, 'W': TYPE_W, 'P': TYPE_P,
}

def _make(x, y, zone_type, lumi_type, shelf_aligned=True, **kw) -> PlacedLuminaire:
    spec = _TYPE_MAP.get(lumi_type, TYPE_A).copy()
    spec.update(kw)
    return PlacedLuminaire(x=round(x, 1), y=round(y, 1),
                           zone_type=zone_type, shelf_aligned=shelf_aligned, **spec)


def _snap(x, y, pitch, ox, oy):
    return round((x - ox) / pitch) * pitch + ox, round((y - oy) / pitch) * pitch + oy


def _grid_pts(polygon, pitch, ox, oy, clr=400):
    """Generate all ceiling-grid points that fall inside *polygon* (inset by clr mm)."""
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


def _out_to_in(ox_mm, oy_mm):
    dx = ox_mm - TX_MM; dy = oy_mm - TY_MM
    return COS_A * dx + SIN_A * dy, -SIN_A * dx + COS_A * dy


def _build_hull(calib_path: Optional[Path] = None):
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
        """
        Place luminaires in every zone using per-zone EN 12464-1 calculations.

        active_zone_types — set of zone type strings, or the string 'all'.
        """
        # Grid origin: auto-detected from drawing > Rossmann Startmaß from zone bounds > last resort
        # IMPORTANT: plan.bounds may equal the full drawing-sheet extent (including title block).
        # Always derive the grid origin from the sales_floor zone polygon bounds — that polygon
        # was selected by the classifier to match the actual building footprint.
        if getattr(plan, 'grid_origin_mm', (0.0, 0.0)) != (0.0, 0.0):
            ox, oy = plan.grid_origin_mm
        else:
            sf_z = next((z for z in classified.zones if z.zone_type == 'sales_floor'), None)
            if sf_z is None:
                sf_z = max(classified.zones, key=lambda z: z.area_m2) if classified.zones else None
            if sf_z is not None:
                zb = sf_z.polygon.bounds
                ox = zb[0] + 1000.0   # Rossmann Startmaß: 1.00m from left wall
                oy = zb[1] + 2000.0   # Rossmann Startmaß: 2.00m from bottom wall
            elif getattr(plan, 'bounds', None):
                ox = plan.bounds[0] + 1000.0
                oy = plan.bounds[1] + 2000.0
            else:
                ox, oy = 1160.0, 500.0
        base_pitch = int(getattr(plan, 'grid_pitch_mm', PITCH_MM) or PITCH_MM)
        ceiling_mm = int(getattr(plan, 'ceiling_height_mm', 3000) or 3000)

        result = PlacementResult(source_file=plan.source_file)
        excl   = getattr(plan, 'exclusion_zones', [])

        for zone in classified.zones:
            zt = zone.zone_type

            # Zone filter
            if active_zone_types != 'all' and zt not in active_zone_types:
                continue

            # ── Zones that never receive ceiling luminaires ────────────────
            # Sources: plan annotation "Direkte Beleuchtung nicht möglich"
            # (Windfang), structural exclusions (Rolltreppe, Aufzug), and
            # out-of-scope areas (WC, Technik).
            if zt in NO_LIGHTING_ZONE_TYPES:
                continue

            # Skip rooms too small to warrant dedicated luminaires
            if zone.area_m2 < 10:
                continue
            # Skip non-sales zones that overlap heavily with the calibrated hull
            if zt != 'sales_floor' and self._hb is not None:
                frac = (self._hb.intersection(zone.polygon).area /
                        max(zone.polygon.area, 1))
                if frac > 0.4:
                    continue

            # ── Per-zone EN 12464-1 calculation ──────────────────────────────
            spec = _zone_spec(
                zt, zone.area_m2, zone.polygon.bounds,
                ceiling_mm=getattr(zone, 'ceiling_height_mm', ceiling_mm),
                base_pitch=base_pitch,
            )
            zone_pitch = spec.grid_pitch_mm
            # Checkout always runs on the standard ceiling grid (1250 mm) regardless
            # of what the lumen method calculates — task lighting at every grid node.
            if zt == 'checkout_zone':
                zone_pitch = base_pitch

            # For sales floor: exclude task-lighting sub-zones (checkout, service)
            # so those positions receive D lights only, not A/B/C on top.
            effective_excl = excl[:]
            if zt == 'sales_floor':
                for other in classified.zones:
                    if other.zone_type in ('checkout_zone', 'service_area', 'office'):
                        effective_excl.append(other.polygon)

            # Checkout zone: clip its polygon to the building boundary so that
            # D lights cannot land outside the entrance walls.
            if zt == 'checkout_zone':
                sf_poly = next(
                    (z.polygon for z in classified.zones if z.zone_type == 'sales_floor'),
                    None)
                if sf_poly is not None:
                    import dataclasses as _dc
                    clipped = zone.polygon.intersection(sf_poly)
                    if not clipped.is_empty and clipped.area > 1e6:
                        zone = _dc.replace(zone, polygon=clipped)

            # Place luminaires using zone-specific pitch
            placed_before = len(result.placed)
            result.placed.extend(
                self._place_zone(zone, plan, effective_excl, zone_pitch, ox, oy))
            placed_count = len(result.placed) - placed_before

            # Record calculation report for this zone
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

        return result

    def _place_zone(self, zone: ZoneResult, plan: ParsedPlan,
                    excl: list, pitch: float, ox: float, oy: float) -> list:
        zt = zone.zone_type
        if zt == 'sales_floor':
            return self._place_sales(zone, plan, excl, pitch, ox, oy)
        elif zt == 'checkout_zone':
            return self._place_checkout(zone, plan, excl, pitch, ox, oy)
        elif zt == 'storage':
            return self._place_storage(zone, excl, pitch, ox, oy)
        elif zt == 'display_window':
            return self._place_display_window(zone, excl, pitch, ox, oy)
        elif zt in ('corridor', 'entrance'):
            return self._place_corridor(zone, excl, pitch, ox, oy)
        elif zt in ('service_area', 'office'):
            return self._place_service(zone, excl, pitch, ox, oy)
        # Unknown / fallback
        return self._place_grid_default(zone, excl, pitch, ox, oy)

    # ── Sales floor ───────────────────────────────────────────────────────────

    def _place_sales(self, zone: ZoneResult, plan: ParsedPlan,
                     excl: list, pitch: float, ox: float, oy: float) -> list:
        shelf_pts = [f.position for f in plan.furniture
                     if f.inferred_type == 'shelving']

        # Shelf-guided placement needs at least 1 point per 100 m²
        # to be representative.  Sparse data → uniform lux grid.
        min_shelf = max(3, zone.area_m2 / 100)
        if len(shelf_pts) < min_shelf:
            return self._place_grid_default(zone, excl, pitch, ox, oy)

        # Use the shelf convex hull as the placement boundary.
        # It naturally excludes back rooms (Lager, offices, WC) because those
        # rooms have no shelves — the hull wraps only the actual sales floor.
        # zone.polygon (building outline) is preserved for grid-origin derivation
        # so the AutoLISP ceiling grid and Python light positions stay aligned.
        if len(shelf_pts) >= 3:
            zone_hull = MultiPoint(shelf_pts).convex_hull
        else:
            zone_hull = zone.polygon.buffer(0)
        inner_hull = zone_hull.buffer(-PERIM_SHRINK_MM)
        if inner_hull.is_empty:
            inner_hull = zone_hull.buffer(-600)
        if inner_hull.is_empty:
            inner_hull = zone_hull

        placed = []; visited = set()

        for sx, sy in shelf_pts:
            # Use covers() not contains() — boundary points (outermost shelf row)
            # are excluded by contains() since they lie exactly on the hull edge.
            if not zone_hull.covers(Point(sx, sy)):
                continue
            gx, gy = _snap(sx, sy, pitch, ox, oy)
            # Guard: snapped grid point must also be inside the zone polygon.
            # Without this check, shelf positions near the zone boundary snap to
            # grid nodes outside the store walls.
            if not zone_hull.covers(Point(gx, gy)):
                continue
            key = (round(gx), round(gy))
            if key in visited or _is_excluded(gx, gy, excl):
                continue
            visited.add(key)
            # K3 (Type C) classification requires the calibrated Hamburg inner hull.
            # For DWG input (self._inner is None) there is no reliable perimeter-row
            # detection from geometry alone, so all shelf-guided positions → Type A.
            is_inner = (self._inner is None or
                        (not inner_hull.is_empty and
                         inner_hull.contains(Point(gx, gy))))
            placed.append(_make(gx, gy, zone.zone_type,
                                'A' if is_inner else 'C', shelf_aligned=True))

        # Supplement: TYPE_B (K4, 60° wide) fills aisle gaps between shelf rows.
        # Area-based cap: ~10.9 B lights per 100 m² of VKF, matching professional plans
        # (Puderbach 680 m² → 72B, Eisleben 545 m² → 59B).
        # Use the labeled zone area (not polygon area) so the cap isn't inflated by
        # back rooms inside the building polygon.
        max_b   = max(4, round(zone.area_m2 * 0.109))
        b_count = 0
        grid_all = _grid_pts(zone.polygon, pitch, ox, oy)
        for gx, gy in grid_all:
            if b_count >= max_b:
                break
            key = (round(gx), round(gy))
            if key in visited or _is_excluded(gx, gy, excl):
                continue
            if zone_hull.covers(Point(gx, gy)):
                visited.add(key)
                placed.append(_make(gx, gy, zone.zone_type, 'B', shelf_aligned=False))
                b_count += 1

        # ── Type E (Sonder-Position) ──────────────────────────────────────────
        # MAX FRANKE.led professional plans show ~4.8 E lights per 100 m² of VKF.
        # E positions supplement A and B throughout the zone.
        # Use labeled zone.area_m2 (not polygon.area) to avoid inflating count from
        # back rooms that exist inside the building polygon but outside the VKF.
        target_e = max(0, round(zone.area_m2 * 0.050))
        if target_e > 0:
            e_candidates = []
            for gx, gy in grid_all:
                key = (round(gx), round(gy))
                if key not in visited and not _is_excluded(gx, gy, excl):
                    if zone_hull.covers(Point(gx, gy)):
                        e_candidates.append((gx, gy))
            if e_candidates:
                # Sort row-by-row for consistent spatial coverage across the zone
                e_candidates.sort(
                    key=lambda p: (round((p[1] - oy) / pitch), round((p[0] - ox) / pitch))
                )
                n = len(e_candidates)
                step = max(1, n // target_e)
                e_placed = 0
                for i in range(0, n, step):
                    if e_placed >= target_e:
                        break
                    gx, gy = e_candidates[i]
                    visited.add((round(gx), round(gy)))
                    placed.append(_make(gx, gy, zone.zone_type, 'E', shelf_aligned=False))
                    e_placed += 1

        # ── Type C (corner/perimeter accent) ─────────────────────────────────
        # ~0.6 C lights per 100 m² VKF; placed at the 4 extreme shelf-area corners.
        # These are the outermost grid nodes at each quadrant of the shelf footprint,
        # matching the "boundary accent" positions in MAX FRANKE.led professional plans.
        target_c = max(0, round(zone.area_m2 * 0.006))  # ~4 for 680 m²
        if target_c > 0 and shelf_pts:
            shelf_xs = [p[0] for p in shelf_pts]
            shelf_ys = [p[1] for p in shelf_pts]
            corners = [
                (min(shelf_xs), min(shelf_ys)),
                (max(shelf_xs), min(shelf_ys)),
                (max(shelf_xs), max(shelf_ys)),
                (min(shelf_xs), max(shelf_ys)),
            ]
            # C lights use the building polygon (zone.polygon) for containment,
            # not zone_hull. The shelf hull's convex edges may exclude the nearest
            # grid node at extreme shelf corners — the building polygon does not.
            c_check_poly = zone.polygon
            c_placed = 0
            for cx, cy in corners:
                if c_placed >= target_c:
                    break
                # Search within 2 grid cells of each corner for a free slot
                for dr in range(3):
                    found = False
                    for dx in range(-dr, dr + 1):
                        for dy in range(-dr, dr + 1):
                            gx = _snap(cx, cy, pitch, ox, oy)[0] + dx * pitch
                            gy = _snap(cx, cy, pitch, ox, oy)[1] + dy * pitch
                            key = (round(gx), round(gy))
                            if key not in visited and not _is_excluded(gx, gy, excl):
                                if c_check_poly.covers(Point(gx, gy)):
                                    visited.add(key)
                                    placed.append(_make(gx, gy, zone.zone_type, 'C', shelf_aligned=False))
                                    c_placed += 1
                                    found = True
                                    break
                        if found:
                            break
                    if found:
                        break

        # Honeycomb (TYPE_W) at cosmetics labels — anti-glare for beauty products
        cosmetics = [lbl for lbl in getattr(plan, 'zone_labels', [])
                     if any(k in lbl.get('text', '').lower()
                            for k in ('kosmetik', 'parfüm', 'duft', 'gobo',
                                      'beauty', 'mood'))]
        for cz in cosmetics[:6]:
            cx, cy = cz['x_mm'], cz['y_mm']
            gx, gy = _snap(cx, cy, pitch, ox, oy)
            key = (round(gx), round(gy))
            if key not in visited and not _is_excluded(gx, gy, excl):
                # Replace the existing downlight at this position with TYPE_W
                placed = [p for p in placed
                          if not (round(p.x) == round(gx) and round(p.y) == round(gy))]
                visited.add(key)
                placed.append(_make(gx, gy, zone.zone_type, 'W', shelf_aligned=False))

        return placed

    # ── Checkout zone ─────────────────────────────────────────────────────────

    def _place_checkout(self, zone: ZoneResult, plan: ParsedPlan,
                        excl: list, pitch: float, ox: float, oy: float) -> list:
        # Checkout uses TYPE_D (K2, 20W 40° 3200lm) — stronger focused beam
        # for task lighting above checkout counters per EN 12464-1 §5.37 (500 lux)
        poly = zone.polygon; placed = []; visited = set()
        for f in plan.furniture:
            if f.inferred_type != 'checkout':
                continue
            if not poly.contains(Point(f.position)):
                continue
            gx, gy = _snap(*f.position, pitch, ox, oy)
            key = (round(gx), round(gy))
            if key in visited or _is_excluded(gx, gy, excl):
                continue
            visited.add(key)
            placed.append(_make(gx, gy, zone.zone_type, 'D', shelf_aligned=False))

        # Fill the full checkout zone grid — checkout counters need complete coverage.
        # Use 200 mm clearance (tighter than sales floor 400 mm) to ensure positions
        # at the zone edge (counter ends) are included.
        for x, y in _grid_pts(poly, pitch, ox, oy, clr=200):
            key = (round(x), round(y))
            if key not in visited and not _is_excluded(x, y, excl):
                visited.add(key)
                placed.append(_make(x, y, zone.zone_type, 'D', shelf_aligned=False))
        return placed

    # ── Storage ───────────────────────────────────────────────────────────────

    def _place_storage(self, zone: ZoneResult, excl: list,
                       pitch: float, ox: float, oy: float) -> list:
        # Storage: uniform grid, Type B only (wider beam, cost-effective)
        return [_make(x, y, zone.zone_type, 'B', shelf_aligned=False)
                for x, y in _grid_pts(zone.polygon, pitch, ox, oy, clr=300)
                if not _is_excluded(x, y, excl)]

    # ── Window display (Schaufenster) — NEO85-SX K6 track spotlights ─────────

    def _place_display_window(self, zone: ZoneResult, excl: list,
                              pitch: float, ox: float, oy: float) -> list:
        """
        Place NEO85-SX K6 track spotlights (TYPE_E) along the façade.

        Rule from plan: "Idealer Abstand zwischen Schaufenster und
        3-Phasen-Stromschiene = 25cm". Track runs parallel to the long wall,
        250 mm inset from the glass. Spotlights spaced at pitch intervals.
        """
        poly = zone.polygon; b = poly.bounds; pts = []
        TRACK_OFFSET_MM = 250   # 25 cm from glass — per plan specification

        # Determine orientation: track runs along the longer axis of the zone
        width  = b[2] - b[0]
        height = b[3] - b[1]

        if width >= height:
            # Track along X-axis, positioned near the shorter (Y) edge
            track_y = b[1] + TRACK_OFFSET_MM
            if not poly.contains(Point((b[0]+b[2])/2, track_y)):
                track_y = b[3] - TRACK_OFFSET_MM
            x = math.ceil((b[0] - ox) / pitch) * pitch + ox
            while x <= b[2]:
                if poly.contains(Point(x, track_y)) and not _is_excluded(x, track_y, excl):
                    pts.append((x, track_y))
                x += pitch
        else:
            # Track along Y-axis
            track_x = b[0] + TRACK_OFFSET_MM
            if not poly.contains(Point(track_x, (b[1]+b[3])/2)):
                track_x = b[2] - TRACK_OFFSET_MM
            y = math.ceil((b[1] - oy) / pitch) * pitch + oy
            while y <= b[3]:
                if poly.contains(Point(track_x, y)) and not _is_excluded(track_x, y, excl):
                    pts.append((track_x, y))
                y += pitch

        return [_make(x, y, zone.zone_type, 'E', shelf_aligned=False) for x, y in pts]

    # ── Corridor / entrance ────────────────────────────────────────────────────

    def _place_corridor(self, zone: ZoneResult, excl: list,
                        pitch: float, ox: float, oy: float) -> list:
        """Single centreline row of lights along the long axis."""
        poly = zone.polygon; b = poly.bounds; pts = []
        # Entrance lobby: TYPE_B (K4 60° wide beam — open transition space)
        # Corridor:       TYPE_A (K1 40° standard)
        lumi_type = 'B' if zone.zone_type == 'entrance' else 'A'
        if (b[2] - b[0]) >= (b[3] - b[1]):   # landscape: row along X
            cy = (b[1] + b[3]) / 2
            x  = math.ceil((b[0] - ox) / pitch) * pitch + ox
            while x <= b[2]:
                if poly.contains(Point(x, cy)) and not _is_excluded(x, cy, excl):
                    pts.append((x, cy))
                x += pitch
        else:                                  # portrait: row along Y
            cx = (b[0] + b[2]) / 2
            y  = math.ceil((b[1] - oy) / pitch) * pitch + oy
            while y <= b[3]:
                if poly.contains(Point(cx, y)) and not _is_excluded(cx, y, excl):
                    pts.append((cx, y))
                y += pitch
        return [_make(x, y, zone.zone_type, lumi_type) for x, y in pts]

    # ── Service / office ──────────────────────────────────────────────────────

    def _place_service(self, zone: ZoneResult, excl: list,
                       pitch: float, ox: float, oy: float) -> list:
        # Service area + office: TYPE_D (K2, 20W 40° 3200lm) — task lighting
        lumi_type = 'D'
        if zone.area_m2 < 5:
            b = zone.polygon.bounds
            cx = (b[0] + b[2]) / 2; cy = (b[1] + b[3]) / 2
            if _is_excluded(cx, cy, excl):
                return []
            return [_make(cx, cy, zone.zone_type, lumi_type)]
        return [_make(x, y, zone.zone_type, lumi_type, shelf_aligned=False)
                for x, y in _grid_pts(zone.polygon, pitch, ox, oy, clr=300)
                if not _is_excluded(x, y, excl)]

    # ── Generic grid fallback (lux-calculated pitch already applied) ──────────

    def _place_grid_default(self, zone: ZoneResult, excl: list,
                            pitch: float, ox: float, oy: float) -> list:
        inner = zone.polygon.buffer(-PERIM_SHRINK_MM)
        # Inner positions → K1 (TYPE_A), edge/perimeter → K3 (TYPE_C)
        return [_make(x, y, zone.zone_type,
                      'A' if (not inner.is_empty and inner.contains(Point(x, y))) else 'C')
                for x, y in _grid_pts(zone.polygon, pitch, ox, oy)
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
