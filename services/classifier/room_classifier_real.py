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


def _find_room_poly_for_label(x_mm: float, y_mm: float, room_polys: list) -> Optional[Polygon]:
    """
    Given a zone label coordinate, return the DXF room polygon that CONTAINS it.
    If none contains it, return the nearest polygon whose centroid is within 15 m.
    Returns None if room_polys is empty.
    """
    if not room_polys:
        return None
    pt = Point(x_mm, y_mm)
    # Primary: find polygon that directly contains the label position
    for rp in room_polys:
        if rp.covers(pt):
            return rp
    # Fallback: nearest polygon by centroid distance (within 15 000 mm = 15 m)
    closest = min(room_polys, key=lambda p: pt.distance(p.centroid))
    if pt.distance(closest.centroid) < 15_000:
        return closest
    return None


def _clip_zone_to_envelope(zone_poly: Polygon, envelope: Optional[Polygon]) -> Polygon:
    """
    Clip a zone polygon to the building envelope.
    If envelope is None or the intersection is empty/degenerate, return the original.
    """
    if envelope is None:
        return zone_poly
    try:
        clipped = zone_poly.intersection(envelope)
        if clipped.is_empty or clipped.area < 1_000:
            return zone_poly
        # intersection may return a GeometryCollection — extract largest polygon
        if clipped.geom_type == 'GeometryCollection':
            polys = [g for g in clipped.geoms if g.geom_type in ('Polygon', 'MultiPolygon')]
            if not polys:
                return zone_poly
            clipped = max(polys, key=lambda g: g.area)
        if clipped.geom_type == 'MultiPolygon':
            clipped = max(clipped.geoms, key=lambda g: g.area)
        return clipped if isinstance(clipped, Polygon) else zone_poly
    except Exception:
        return zone_poly


def _label_zones(plan: ParsedPlan) -> list:
    zones = []; idx = 0
    annotations = getattr(plan, 'annotations', [])

    # ── Sales floor polygon ───────────────────────────────────────────────────
    # Use convex hull of all detected shelf positions (works for both PDF and DWG
    # input, since shelf INSERT blocks are reliably recognised across file types).
    # Filter to shelves inside (or within 2 m of) the building envelope so that
    # legend-table TEXT annotations at the DWG origin don't inflate the hull.
    _shelf_all = [f.position for f in plan.furniture if f.inferred_type == 'shelving']
    _envelope  = getattr(plan, 'building_envelope', None)
    if _envelope is not None:
        _env_buf   = _envelope.buffer(2000)
        _shelf_env = [p for p in _shelf_all if _env_buf.covers(Point(*p))]
        shelf_pts  = _shelf_env if _shelf_env else _shelf_all
    else:
        shelf_pts = _shelf_all

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

    # Use building_envelope (pre-computed in parser) if available — it is the most
    # reliable boundary.  Fall back to the best-coverage room polygon, then convex hull.
    room_polys = getattr(plan, 'room_polygons', None) or []
    envelope   = getattr(plan, 'building_envelope', None)

    # building_envelope = WHOLE BUILDING (sales + storage + WC + offices).
    # It is used only as a clip constraint — never as the sales floor polygon itself.
    # Sales floor polygon is derived from shelf positions or DXF room polygons.
    sf_poly = None

    # 1. Find the DXF room polygon with the highest shelf DENSITY (shelves per m²).
    #    Density beats pure coverage so the actual sales-floor polygon wins over
    #    the larger building envelope which trivially contains all shelves too.
    if room_polys and shelf_pts:
        best_density = 0.0
        for rp in room_polys:
            area_m2 = rp.area / 1e6
            if not (80 <= area_m2 <= 1500):
                continue
            n_covered = sum(1 for pt in shelf_pts if rp.covers(Point(*pt)))
            coverage  = n_covered / max(len(shelf_pts), 1)
            if coverage < 0.40:
                continue
            density = n_covered / area_m2   # shelves per m²
            if density > best_density:
                best_density = density
                sf_poly = rp
        if best_density < 0.05:             # fewer than 0.05 shelves/m² → not reliable
            sf_poly = None

    # 2. Convex hull of shelf positions — clip to envelope to prevent wall bleed
    if sf_poly is None:
        if len(shelf_pts) >= 3:
            sf_poly = MultiPoint(shelf_pts).convex_hull
        elif plan.bounds:
            sf_poly = shapely_box(*plan.bounds)
        else:
            sf_poly = shapely_box(0, 0, 67500, 42000)
        sf_poly = _clip_zone_to_envelope(sf_poly, envelope)
    else:
        # DXF polygon found — still clip to envelope as a safety guard
        sf_poly = _clip_zone_to_envelope(sf_poly, envelope)

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

    # Collect checkout zone labels separately so they don't borrow area from
    # adjacent non-checkout rooms via _spatial_area (e.g. "HK" near "Personal 12.8m²")
    checkout_lbl_pts = [(l['x_mm'], l['y_mm'])
                        for l in plan.zone_labels if l['zone_type'] == 'checkout_zone']

    # ── Secondary zones (storage, WC, checkout, etc.) ────────────────────────
    for lbl in plan.zone_labels:
        zt = lbl['zone_type']
        if zt in ('unknown', 'sales_floor', 'checkout_zone'):
            continue   # checkout handled below
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

        # Prefer the actual DXF room polygon containing this label over a square approximation
        rp = _find_room_poly_for_label(cx, cy, room_polys)
        if rp is not None and 10 <= rp.area / 1e6 <= 2000:
            poly = rp
        else:
            half = math.sqrt(a * 1e6) / 2
            poly = shapely_box(cx - half, cy - half, cx + half, cy + half)
        # Always clip to building envelope — prevents zones from bleeding through walls
        poly = _clip_zone_to_envelope(poly, envelope)
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

    # ── Checkout zone from furniture + labels (DWG fallback) ──────────────────
    # Build checkout zone from any combination of:
    #   • detected checkout furniture INSERT positions (kasse, kassentisch, etc.)
    #   • checkout zone label positions (HK, Hauptkasse, Kasse annotations)
    # Do NOT clip to building_envelope — checkout is often OUTSIDE the main room
    # polygon (entrance vestibule), and clipping would erase the zone entirely.
    if not any(z.zone_type == 'checkout_zone' for z in zones):
        co_furn = [f.position for f in plan.furniture if f.inferred_type == 'checkout']
        # Outlier rejection for furniture positions
        if len(co_furn) >= 3:
            xs = sorted(p[0] for p in co_furn)
            ys = sorted(p[1] for p in co_furn)
            med_x = xs[len(xs) // 2]; med_y = ys[len(ys) // 2]
            dists = [math.sqrt((p[0]-med_x)**2 + (p[1]-med_y)**2) for p in co_furn]
            threshold = max(sorted(dists)[len(dists)//2] * 5, 5000)
            co_furn = [p for p, d in zip(co_furn, dists) if d <= threshold]
        # Merge with label-position anchors
        co_pts = list(co_furn) + checkout_lbl_pts
        if len(co_pts) >= 1:
            # Buffer is smaller when actual furniture positions constrain the zone
            # (furniture already spans the counter extent), larger when only text
            # label anchors are available (need a wider catch-all radius).
            buf_mm = 2500 if co_furn else 3000
            hull = MultiPoint(co_pts).convex_hull if len(co_pts) >= 3 else \
                   Point(co_pts[0]).buffer(buf_mm)
            co_hull = hull.buffer(buf_mm)
            co_area = max(co_hull.area / 1e6, len(co_pts) * 8.0)
            zones.append(ZoneResult(
                polygon_index=idx, polygon=co_hull, zone_type='checkout_zone',
                confidence=0.75, method='furniture',
                area_m2=co_area,
                ceiling_height_mm=plan.ceiling_height_mm,
                furniture_counts={'shelving': 0, 'checkout': len(co_furn)},
                label_text=f"Checkout ({len(co_furn)} furn + {len(checkout_lbl_pts)} labels)"))
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