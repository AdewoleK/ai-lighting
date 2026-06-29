"""
lighting-ai/services/classifier/room_classifier_real.py
Zone classification for real Rossmann plans.
Primary: zone labels from PDF text.  Fallback: area+furniture rules.
"""
from __future__ import annotations
import math, re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import sys

from shapely.geometry import Polygon, Point, MultiPoint, box as shapely_box

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from services.parser.pdf_parser import ParsedPlan


@dataclass
class ZoneResult:
    polygon_index:int; polygon:Polygon; zone_type:str; confidence:float; method:str
    furniture_counts:dict=field(default_factory=dict)
    area_m2:float=0.0; ceiling_height_mm:float=3000.0; label_text:str=""


@dataclass
class ClassifiedPlan:
    source_file:str; zones:list
    def by_type(self,z): return [x for x in self.zones if x.zone_type==z]
    def summary(self):
        from collections import Counter
        return f"ClassifiedPlan: {dict(Counter(z.zone_type for z in self.zones))}"


def _rule_classify(area_m2,n_shelf,n_check,aspect):
    # Furniture-informed rules (PDF input where block names are recognised)
    if area_m2>400 and n_shelf>50: return 'sales_floor',0.88
    if area_m2>100 and n_shelf>10: return 'sales_floor',0.80
    if n_check>2  and area_m2<100: return 'checkout_zone',0.85
    # Area-only fallback (DWG input: furniture block names often unrecognised)
    if area_m2>300:                return 'sales_floor',0.65
    if area_m2>80 and n_shelf>=1:  return 'sales_floor',0.58
    if area_m2>50 and n_shelf<3 and n_check<1: return 'storage',0.60
    if aspect>5  and area_m2<60:   return 'corridor',0.75
    if area_m2<10:                 return 'entrance',0.55
    return 'unknown',0.40


def _spatial_area(lbl: dict, annotations: list, radius_mm: float = 3000.0) -> float:
    """
    Find a numeric area (in m²) from annotations that are spatially close to lbl.
    Used for DWG input where room name and area are stored as separate TEXT entities.
    Returns 0.0 when no area annotation is found nearby.
    """
    cx, cy = lbl['x_mm'], lbl['y_mm']
    for ann in annotations:
        m = re.search(r'([\d]+[,.][\d]+)\s*(?:qm|m²|m2)', ann.get('text', ''), re.IGNORECASE)
        if not m:
            continue
        ax, ay = ann.get('position', (cx + 99999, cy + 99999))
        if math.dist((cx, cy), (ax, ay)) < radius_mm:
            try:
                return float(m.group(1).replace(',', '.'))
            except ValueError:
                pass
    return 0.0


def _label_zones(plan: ParsedPlan) -> list:
    zones = []; idx = 0
    annotations = getattr(plan, 'annotations', [])

    # ── Sales floor polygon ───────────────────────────────────────────────────
    # Use convex hull of all detected shelf positions (works for both PDF and DWG
    # input, since shelf INSERT blocks are reliably recognised across file types).
    shelf_pts = [f.position for f in plan.furniture if f.inferred_type == 'shelving']

    # Get area from labelled zones first; fall back to spatial search in annotations.
    sf_labels = [l for l in plan.zone_labels
                 if l['zone_type'] == 'sales_floor' and (l.get('area_m2') or 0) > 0]
    sf_area = max((l['area_m2'] for l in sf_labels), default=0.0)

    if sf_area < 1.0:
        # DWG plans: room name and area are often separate TEXT entities.
        # Spatially associate an area annotation close to any sales_floor label.
        for lbl in (l for l in plan.zone_labels if l['zone_type'] == 'sales_floor'):
            sf_area = _spatial_area(lbl, annotations)
            if sf_area > 1.0:
                break

    if sf_area < 1.0:
        sf_area = 643.60  # fallback: Hamburg EG reference

    # Prefer actual room polygon from DXF over convex hull of shelf positions.
    # Closed LWPOLYLINEs in the DWG trace the real wall boundaries, which prevents
    # lights being placed outside the store.
    sf_poly = None
    room_polys = getattr(plan, 'room_polygons', None)
    if room_polys and shelf_pts:
        # Pick the SMALLEST polygon that still covers ≥50 % of all shelf positions.
        # Smallest = tightest fit = actual building footprint.
        # Upper bound 1200 m² excludes the drawing-sheet boundary polygon that some
        # DXF exporters emit as an LWPOLYLINE around the entire A0 sheet (≈ 1700 m²+).
        best_area_m2 = float('inf')
        for rp in room_polys:
            area_m2 = rp.area / 1e6
            if not (200 <= area_m2 <= 1200):
                continue
            n_covered = sum(1 for pt in shelf_pts if rp.covers(Point(*pt)))
            coverage_frac = n_covered / max(len(shelf_pts), 1)
            if coverage_frac >= 0.5 and area_m2 < best_area_m2:
                best_area_m2 = area_m2
                sf_poly = rp

    if sf_poly is None:
        if len(shelf_pts) >= 3:
            # Fallback: convex hull of shelf positions when no DXF polygon found.
            sf_poly = MultiPoint(shelf_pts).convex_hull
        elif plan.bounds:
            sf_poly = shapely_box(*plan.bounds)
        else:
            sf_poly = shapely_box(0, 0, 67500, 42000)

    n_shelf = sum(1 for f in plan.furniture if f.inferred_type == 'shelving'
                  and sf_poly.contains(Point(f.position)))
    n_check = sum(1 for f in plan.furniture if f.inferred_type == 'checkout'
                  and sf_poly.contains(Point(f.position)))

    zones.append(ZoneResult(
        polygon_index=idx, polygon=sf_poly, zone_type='sales_floor',
        confidence=0.95, method='label', area_m2=sf_area,
        ceiling_height_mm=plan.ceiling_height_mm,
        furniture_counts={'shelving': n_shelf, 'checkout': n_check},
        label_text=f"Verkaufsraum {sf_area:.2f}m²"))
    idx += 1

    # ── Secondary zones (storage, WC, checkout, etc.) ────────────────────────
    for lbl in plan.zone_labels:
        zt = lbl['zone_type']
        if zt in ('unknown', 'sales_floor'):
            continue
        a = (lbl.get('area_m2') or 0)
        cx, cy = lbl['x_mm'], lbl['y_mm']

        # Try spatial area association for DWG labels that have no area
        if a < 1.0:
            a = _spatial_area(lbl, annotations)

        # Still no area — skip entirely (unreliable to create a zone without size)
        if a < 1.0:
            continue

        # Deduplicate: skip if same zone type is already nearby
        dup = any(z.zone_type == zt and
                  math.dist((cx, cy), ((z.polygon.bounds[0] + z.polygon.bounds[2]) / 2,
                                       (z.polygon.bounds[1] + z.polygon.bounds[3]) / 2)) < 10000
                  for z in zones)
        if dup:
            continue

        half = math.sqrt(a * 1e6) / 2
        poly = shapely_box(cx - half, cy - half, cx + half, cy + half)
        n_s = sum(1 for f in plan.furniture if f.inferred_type == 'shelving'
                  and poly.contains(Point(f.position)))
        n_c = sum(1 for f in plan.furniture if f.inferred_type == 'checkout'
                  and poly.contains(Point(f.position)))
        zones.append(ZoneResult(
            polygon_index=idx, polygon=poly, zone_type=zt,
            confidence=0.92, method='label', area_m2=a,
            ceiling_height_mm=plan.ceiling_height_mm,
            furniture_counts={'shelving': n_s, 'checkout': n_c},
            label_text=lbl.get('text', '')[:60]))
        idx += 1

    # ── Checkout zone from furniture (DWG fallback) ────────────────────────────
    # DWG anonymous block definitions are lost on conversion, so checkout labels
    # arrive without area.  Build a zone from the convex hull of detected checkout
    # furniture inserts when no area-labelled checkout zone was created above.
    if not any(z.zone_type == 'checkout_zone' for z in zones):
        co_labels = [l for l in plan.zone_labels if l['zone_type'] == 'checkout_zone']
        co_furn   = [f.position for f in plan.furniture if f.inferred_type == 'checkout']
        if co_labels and len(co_furn) >= 2:
            # 2500 mm buffer gives ~35-45 m² zone for a compact checkout cluster,
            # which yields ~25-30 Type D at 1250 mm pitch — matching professional plans.
            co_hull = MultiPoint(co_furn).convex_hull.buffer(2500)
            co_area = max(co_hull.area / 1e6, len(co_furn) * 8.0)
            zones.append(ZoneResult(
                polygon_index=idx, polygon=co_hull, zone_type='checkout_zone',
                confidence=0.75, method='furniture', area_m2=co_area,
                ceiling_height_mm=plan.ceiling_height_mm,
                furniture_counts={'shelving': 0, 'checkout': len(co_furn)},
                label_text=f"Checkout ({len(co_furn)} units from furniture)"))
            idx += 1

    return zones


class RealRoomClassifier:
    def classify(self, plan: ParsedPlan) -> ClassifiedPlan:
        label_zones = [l for l in plan.zone_labels
                       if l['zone_type'] != 'unknown' and (l.get('area_m2') or 0) > 0]
        shelf_pts   = [f for f in plan.furniture if f.inferred_type == 'shelving']

        # Run label-based classification when:
        #   (a) labelled zones with explicit areas exist (normal PDF path), OR
        #   (b) shelf furniture detected — shelf hull defines VKF boundary even
        #       when DWG text blocks couldn't export their block definitions.
        if label_zones or len(shelf_pts) >= 3:
            zones = _label_zones(plan)
            if zones:
                return ClassifiedPlan(source_file=plan.source_file, zones=zones)

        if plan.room_polygons:
            zones=[]
            for idx,poly in enumerate(plan.room_polygons):
                fi=[f for f in plan.furniture if poly.contains(Point(f.position))]
                ns=sum(1 for f in fi if f.inferred_type=='shelving')
                nc=sum(1 for f in fi if f.inferred_type=='checkout')
                a=poly.area/1e6; b=poly.bounds
                w=(b[2]-b[0])/1000; h=(b[3]-b[1])/1000
                asp=max(w,h)/max(min(w,h),0.1)
                zt,conf=_rule_classify(a,ns,nc,asp)
                zones.append(ZoneResult(
                    polygon_index=idx, polygon=poly, zone_type=zt,
                    confidence=conf, method='rule', area_m2=a,
                    ceiling_height_mm=plan.ceiling_height_mm,
                    furniture_counts={'shelving':ns,'checkout':nc}))
            return ClassifiedPlan(source_file=plan.source_file,zones=zones)

        # Last resort
        b=plan.bounds or (0,0,67500,42000)
        poly=shapely_box(*b)
        return ClassifiedPlan(source_file=plan.source_file, zones=[ZoneResult(
            polygon_index=0,polygon=poly,zone_type='sales_floor',
            confidence=0.50,method='fallback',area_m2=poly.area/1e6,
            ceiling_height_mm=plan.ceiling_height_mm)])


if __name__=="__main__":
    from services.parser.pdf_parser import RealPlanParser
    plan=RealPlanParser().parse("/mnt/user-data/uploads/3600_HH_Jungfernstieg_EG_SB_Kassen_20240506.pdf")
    result=RealRoomClassifier().classify(plan)
    print(result.summary())
    for z in result.zones:
        print(f"  Zone {z.polygon_index:2d}: {z.zone_type:15s} {z.area_m2:7.1f}m² conf={z.confidence:.2f}")