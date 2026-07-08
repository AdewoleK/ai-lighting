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

# ── Shelf height code extractor ───────────────────────────────────────────────

def _shelf_height_code(fi) -> str:
    """
    Return the Rossmann shelf height code for a FurnitureInsert.

    PDF parser sets block_name = 'SHELF_57', 'SHELF_47', etc.
    DWG parser embeds the code in the block name: 'T57m_14', 'I1_47_29', etc.
    """
    import re as _re
    bn = fi.block_name
    if bn.startswith('SHELF_'):
        return bn[6:]          # strip prefix → '57', '47/37', etc.
    parts = set(_re.split(r'[^0-9]+', bn))
    for code in ('77', '67', '57', '47', '37', '27'):
        if code in parts:
            return code
    return ''


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
        grid_origin = getattr(plan, 'grid_origin_mm', (0.0, 0.0))
        if grid_origin != (0.0, 0.0):
            ox, oy = grid_origin
        else:
            # If the plan is in a real-world (georeferenced) coordinate system
            # — absolute values > 10 km — the DXF origin (0,0) already gives the
            # correct 1250mm modular alignment.  Do not shift to zone bounds.
            sf_z = next((z for z in classified.zones if z.zone_type == 'sales_floor'), None)
            if sf_z is None:
                sf_z = max(classified.zones, key=lambda z: z.area_m2) if classified.zones else None
            georef = (sf_z is not None and
                      any(abs(c) > 10_000_000 for c in sf_z.polygon.bounds))
            if georef:
                ox, oy = 0.0, 0.0   # keep DXF origin — correct modular grid
            elif sf_z is not None:
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
                # Mis-detection guard: if the checkout zone centroid is > 20 m from the
                # nearest shelf position, it was likely detected from a legend / detail
                # block drawn far from the actual store footprint.  Skip it silently.
                shelf_positions = [f.position for f in plan.furniture
                                   if f.inferred_type == 'shelving']
                if shelf_positions:
                    from shapely.geometry import MultiPoint as _MP
                    nearest_shelf_dist = zone.polygon.centroid.distance(
                        _MP(shelf_positions))
                    if nearest_shelf_dist > 20_000:   # 20 m in mm
                        result.corrections.append(
                            f"Checkout zone skipped: centroid is "
                            f"{nearest_shelf_dist/1000:.0f}m from nearest shelf "
                            f"(likely a legend/detail block)")
                        continue

            # For sales floor: exclude task-lighting sub-zones (checkout, service)
            # so those positions receive D lights only, not A/B/C on top.
            effective_excl = excl[:]
            if zt == 'sales_floor':
                for other in classified.zones:
                    if other.zone_type in ('checkout_zone', 'service_area', 'office'):
                        effective_excl.append(other.polygon)

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

        # Hard boundary filter removed: shelf-anchored placement guarantees that
        # every sales floor light is derived from a detected shelf position, so it
        # cannot escape outside the building.  Checkout zone mis-detection is handled
        # by the proximity guard above (> 20 m from nearest shelf → skip).
        # Non-sales zones (checkout grid, storage rows) use the zone polygon directly
        # so their lights are already contained by construction.

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
        """
        Shelf-anchored placement: every light position is derived from a
        detected shelf INSERT, never from a zone-polygon boundary.

        A = one per unique shelf-snapped grid node
        B = fill 4-connected neighbor nodes of A lights (aisle / perimeter fill)
        E = up to 2 per structural column, within the shelf domain
        C = relabel the 4 corner-nearest A lights

        Lights cannot escape the store boundary because they only exist where
        shelves — or their immediate ceiling-tile neighbors — are detected.
        No wall polygon, no building envelope, no clipping required.
        """

        # ── 1. Collect shelves; reject extreme outliers ───────────────────
        raw_objs = [f for f in plan.furniture if f.inferred_type == 'shelving']
        if not raw_objs:
            return self._place_grid_default(zone, excl, pitch, ox, oy)

        # Median + 8×MAD filter: removes INSERT positions millions of mm away
        # (e.g. Bad Nenndorf outlier at +176M mm) while keeping all real shelves.
        s_xs = sorted(f.position[0] for f in raw_objs)
        s_ys = sorted(f.position[1] for f in raw_objs)
        mid  = len(s_xs) // 2
        med_x, med_y = s_xs[mid], s_ys[mid]
        mad_x = max(sorted(abs(x - med_x) for x in s_xs)[mid], 500.0)
        mad_y = max(sorted(abs(y - med_y) for y in s_ys)[mid], 500.0)
        shelf_objs = ([f for f in raw_objs
                       if abs(f.position[0] - med_x) <= 8 * mad_x
                       and abs(f.position[1] - med_y) <= 8 * mad_y]
                      or raw_objs)
        shelf_pts = [f.position for f in shelf_objs]

        # ── 2. A lights — one per unique shelf-snapped grid node ──────────
        visited: set = set()
        placed:  list = []

        for sx, sy in shelf_pts:
            gx, gy = _snap(sx, sy, pitch, ox, oy)
            key = (round(gx), round(gy))
            if key in visited or _is_excluded(gx, gy, excl):
                continue
            visited.add(key)
            placed.append(_make(gx, gy, zone.zone_type, 'A', shelf_aligned=True))

        if not placed:
            return []

        a_keys = frozenset(visited)

        # ── 3. B lights — 4-connected neighbors of A lights ───────────────
        # Each B candidate is exactly one grid step (1250 mm) from an A light.
        # Aisles between shelf rows and the 1–2 m perimeter walkway are one step
        # from the nearest shelf row → fully covered. Nodes further away
        # (outside the building where there are no shelves) are never reached.
        all_shelf_objs = [f for f in plan.furniture if f.inferred_type == 'shelving']
        high_shelves   = [f for f in all_shelf_objs
                          if _shelf_height_code(f) in ('57', '67')]
        f57     = len(high_shelves) / max(len(all_shelf_objs), 1)
        b_ratio = 0.269 + f57 * 0.239
        max_b   = max(4, round(len(placed) * b_ratio))

        # Build candidate list in row-major order (reproducible, spread-out fill)
        b_cands: list = []
        seen_b:  set  = set()
        for ax, ay in sorted(a_keys, key=lambda k: (k[1], k[0])):
            for dx, dy in [(pitch, 0), (-pitch, 0), (0, pitch), (0, -pitch)]:
                nk = (round(ax + dx), round(ay + dy))
                if nk not in visited and nk not in seen_b:
                    seen_b.add(nk)
                    b_cands.append(nk)

        # Spread B lights evenly: take every step-th candidate, then fill remainder
        step = max(1, len(b_cands) // max(max_b, 1))
        b_count = 0
        for i in range(0, len(b_cands), step):
            if b_count >= max_b:
                break
            bx, by = b_cands[i]
            if not _is_excluded(bx, by, excl):
                visited.add((bx, by))
                placed.append(_make(bx, by, zone.zone_type, 'B', shelf_aligned=False))
                b_count += 1
        # Fill any shortfall (step may cause under-placement)
        if b_count < max_b:
            for bx, by in b_cands:
                if b_count >= max_b:
                    break
                if (bx, by) not in visited and not _is_excluded(bx, by, excl):
                    visited.add((bx, by))
                    placed.append(_make(bx, by, zone.zone_type, 'B', shelf_aligned=False))
                    b_count += 1

        # ── 4. E lights — column-adjacent nodes ──────────────────────────────
        col_raw = [(f.position[0], f.position[1])
                   for f in plan.furniture if f.inferred_type == 'column']

        # Deduplicate: same physical column often appears on multiple DXF layers
        col_dedup: list = []
        for cp in col_raw:
            if not any(math.sqrt((cp[0]-u[0])**2 + (cp[1]-u[1])**2) < 300.0
                       for u in col_dedup):
                col_dedup.append(cp)

        # Filter to columns inside (or within 200 mm of) the sales floor zone.
        # Structural columns are always inside the building by definition, so this
        # simply removes legend/title-block elements that appear far from the store.
        zone_near = zone.polygon.buffer(200)
        col_in_zone = [cp for cp in col_dedup
                       if zone_near.covers(Point(cp[0], cp[1]))]

        if col_in_zone:
            for col_x, col_y in col_in_zone:
                # Search grid nodes centred on the snapped column position.
                # Structural columns are inside the building → nearby grid nodes are too.
                snap_cx = round(col_x / pitch) * pitch
                snap_cy = round(col_y / pitch) * pitch
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
                    key = (round(gx), round(gy))
                    if key not in visited and not _is_excluded(gx, gy, excl):
                        visited.add(key)
                        placed.append(_make(gx, gy, zone.zone_type, 'E',
                                           shelf_aligned=False))
                        placed_for_col += 1
        else:
            # No structural columns detected inside the zone.
            # For large stores (many A lights = real full-size plan) use an area-based
            # estimate: calibrated from Puderbach (660 m² → 36 E, ratio = 0.055/m²).
            # For small/test plans (< 50 A lights) skip E entirely — they likely have no
            # real structural columns, and zone.area_m2 may be a Hamburg fallback value.
            if len(placed) >= 50:
                target_e = max(0, round(zone.area_m2 * 0.055))
                reachable: set = set(a_keys)
                for ax, ay in a_keys:
                    for dx, dy in [(pitch, 0), (-pitch, 0), (0, pitch), (0, -pitch)]:
                        reachable.add((round(ax + dx), round(ay + dy)))
                e_cands = sorted(
                    [(gx, gy) for gx, gy in reachable
                     if (round(gx), round(gy)) not in visited
                     and not _is_excluded(gx, gy, excl)],
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
                    visited.add((round(gx), round(gy)))
                    placed.append(_make(gx, gy, zone.zone_type, 'E', shelf_aligned=False))
                    e_placed += 1

        # ── 5. C lights — relabel 4 corner-nearest A lights ───────────────
        if shelf_pts:
            sx_list = [p[0] for p in shelf_pts]
            sy_list = [p[1] for p in shelf_pts]
            corners = [
                (min(sx_list), min(sy_list)), (max(sx_list), min(sy_list)),
                (max(sx_list), max(sy_list)), (min(sx_list), max(sy_list)),
            ]
            relabeled_c: set = set()
            for cx, cy in corners:
                a_cands = [(i, p) for i, p in enumerate(placed)
                           if p.lumi_type == 'A' and i not in relabeled_c]
                if not a_cands:
                    break
                ni = min(a_cands,
                         key=lambda ip: math.sqrt((ip[1].x - cx) ** 2 +
                                                  (ip[1].y - cy) ** 2))[0]
                relabeled_c.add(ni)
                old = placed[ni]
                placed[ni] = _make(old.x, old.y, old.zone_type, 'C', shelf_aligned=False)

        # ── 6. W (Wabeneinsatz) — replace at cosmetics label positions ────
        cosmetics = [lbl for lbl in getattr(plan, 'zone_labels', [])
                     if any(k in lbl.get('text', '').lower()
                            for k in ('kosmetik', 'parfüm', 'duft', 'gobo',
                                      'beauty', 'mood'))]
        for cz in cosmetics[:6]:
            cx, cy = cz['x_mm'], cz['y_mm']
            gx, gy = _snap(cx, cy, pitch, ox, oy)
            key = (round(gx), round(gy))
            if key in visited:   # only replace an existing light, never add outside domain
                placed = [p for p in placed
                          if not (round(p.x) == round(gx) and round(p.y) == round(gy))]
                placed.append(_make(gx, gy, zone.zone_type, 'W', shelf_aligned=False))

        return placed

    # ── Checkout zone ─────────────────────────────────────────────────────────

    def _place_checkout(self, zone: ZoneResult, plan: ParsedPlan,
                        excl: list, pitch: float, ox: float, oy: float) -> list:
        # Checkout uses TYPE_D (K2, 20W 40° 3200lm) — stronger focused beam
        # for task lighting above checkout counters per EN 12464-1 §5.37 (500 lux)
        poly = zone.polygon; placed = []; visited = set()

        # Always place D at every detected checkout counter furniture position
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

        # Fill the checkout zone grid — every ceiling tile in the checkout strip
        # gets a D light.  200 mm clearance (vs 400 mm on the sales floor) ensures
        # counter-end tiles at the zone boundary are included.
        # "Deckenraster anpassen" notes appear in BOTH grid-fill and non-grid-fill
        # stores (Puderbach D=29 and Hamburg D=2 both carry that annotation), so
        # that flag is not a reliable differentiator — always grid-fill here.
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
        # Service area + office: TYPE_B (K4, 20W 60° wide beam) — general overhead fill.
        # Back-of-house areas do not receive task-lighting K2 (TYPE_D); that is reserved
        # for checkout counters only.
        lumi_type = 'B'
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
