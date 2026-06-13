"""
lighting-ai/services/classifier/room_classifier_real.py
Zone classification for real Rossmann plans.
Primary: zone labels from PDF text.  Fallback: area+furniture rules.
"""
from __future__ import annotations
import math
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


def _label_zones(plan:ParsedPlan)->list:
    zones=[]; idx=0

    # Sales floor polygon = convex hull of all shelf label positions
    shelf_pts=[f.position for f in plan.furniture if f.inferred_type=='shelving']
    sf_labels=[l for l in plan.zone_labels
               if l['zone_type']=='sales_floor' and (l.get('area_m2') or 0)>0]
    sf_area=max((l['area_m2'] for l in sf_labels),default=643.60)

    if len(shelf_pts)>=3:
        sf_poly=MultiPoint(shelf_pts).convex_hull.buffer(1250)
    elif plan.bounds:
        sf_poly=shapely_box(*plan.bounds)
    else:
        sf_poly=shapely_box(0,0,67500,42000)

    n_shelf=sum(1 for f in plan.furniture if f.inferred_type=='shelving'
                and sf_poly.contains(Point(f.position)))
    n_check=sum(1 for f in plan.furniture if f.inferred_type=='checkout'
                and sf_poly.contains(Point(f.position)))

    zones.append(ZoneResult(
        polygon_index=idx, polygon=sf_poly, zone_type='sales_floor',
        confidence=0.95, method='label', area_m2=sf_area,
        ceiling_height_mm=plan.ceiling_height_mm,
        furniture_counts={'shelving':n_shelf,'checkout':n_check},
        label_text=f"Verkaufsraum-EG {sf_area:.2f}m²"))
    idx+=1

    for lbl in plan.zone_labels:
        zt=lbl['zone_type']
        if zt in ('unknown','sales_floor'): continue
        a=(lbl.get('area_m2') or 0)
        if a<1.0: continue
        cx,cy=lbl['x_mm'],lbl['y_mm']
        # Deduplicate: skip if same zone type is already nearby
        dup=any(z.zone_type==zt and
                math.dist((cx,cy),((z.polygon.bounds[0]+z.polygon.bounds[2])/2,
                                   (z.polygon.bounds[1]+z.polygon.bounds[3])/2))<10000
                for z in zones)
        if dup: continue
        half=math.sqrt(a*1e6)/2
        poly=shapely_box(cx-half,cy-half,cx+half,cy+half)
        n_s=sum(1 for f in plan.furniture if f.inferred_type=='shelving' and poly.contains(Point(f.position)))
        n_c=sum(1 for f in plan.furniture if f.inferred_type=='checkout' and poly.contains(Point(f.position)))
        zones.append(ZoneResult(
            polygon_index=idx, polygon=poly, zone_type=zt,
            confidence=0.92, method='label', area_m2=a,
            ceiling_height_mm=plan.ceiling_height_mm,
            furniture_counts={'shelving':n_s,'checkout':n_c},
            label_text=lbl.get('text','')[:60]))
        idx+=1
    return zones


class RealRoomClassifier:
    def classify(self,plan:ParsedPlan)->ClassifiedPlan:
        label_zones=[l for l in plan.zone_labels
                     if l['zone_type']!='unknown' and (l.get('area_m2') or 0)>0]
        if label_zones:
            zones=_label_zones(plan)
            if zones: return ClassifiedPlan(source_file=plan.source_file,zones=zones)

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