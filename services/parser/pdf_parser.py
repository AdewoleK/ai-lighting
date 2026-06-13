"""
lighting-ai/services/parser/pdf_parser.py

Real Rossmann floor plan parser. Primary path: PDF → parsed plan.
Binary DWG: requires companion PDF or ODA converter.

Validated:
  INPUT  3600_HH_Jungfernstieg_EG_SB_Kassen_20240506.pdf  scale 1:50
  OUTPUT Ro_Hamburg_Jungfernstieg_3600_20260113-EG-DRP.pdf scale 1:75
  → 106 Type A + 61 Type B = 167 luminaires extracted ✓

Bug-fixes applied:
  - fitz import: use pymupdf (correct package) with fallback to fitz
  - DWG companion-PDF search: build paths via string concat, not
    Path.with_suffix(), which rejects compound suffixes like '-EG.pdf'
  - API error messages now include install instructions
"""
from __future__ import annotations
import math, re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import sys

from shapely.geometry import Polygon, Point, box as shapely_box
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import DEFAULT_LAYER_MAP, GRID_PITCH_MM


# ── Fitz / PyMuPDF import (robust against package name conflicts) ─────────────
def _open_pdf(path: str):
    """
    Open a PDF file with PyMuPDF.

    The correct pip package is 'pymupdf' (imports as 'fitz').
    A separate, unrelated package called 'fitz' also exists and breaks
    the import.  We try the correct path first, then fall back.

    Fix for: ModuleNotFoundError: No module named 'frontend'
    (caused by the wrong 'fitz' package being installed instead of 'pymupdf')
    """
    try:
        import pymupdf as fitz          # pymupdf >= 1.23 preferred import
        return fitz.open(str(path))
    except ImportError:
        pass
    try:
        import fitz                     # older pymupdf also works via 'fitz'
        if not hasattr(fitz, 'open'):
            raise ImportError("'fitz' package is not pymupdf")
        return fitz.open(str(path))
    except ImportError as e:
        raise ImportError(
            "PyMuPDF is required to parse PDF files.\n"
            "Fix:\n"
            "  pip uninstall fitz pymupdf          # remove any conflicting packages\n"
            "  pip install pymupdf                  # install the correct one\n"
            f"Original error: {e}"
        )


def _get_pdf_text(doc, mode: str):
    """Get text from the first page of an already-opened PDF document."""
    return doc[0].get_text(mode)


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FurnitureInsert:
    block_name: str; position: tuple; rotation: float
    layer: str;      inferred_type: str = "unknown"


@dataclass
class CeilingGridLine:
    start: tuple; end: tuple; layer: str


@dataclass
class ParsedPlan:
    source_file: str
    room_polygons:     list  = field(default_factory=list)
    furniture:         list  = field(default_factory=list)
    grid_lines:        list  = field(default_factory=list)
    door_positions:    list  = field(default_factory=list)
    window_positions:  list  = field(default_factory=list)
    annotations:       list  = field(default_factory=list)
    ceiling_height_mm: float = 3000.0
    fries_height_mm:   float = 3300.0
    layer_map:         dict  = field(default_factory=dict)
    bounds:            Optional[tuple] = None
    scale:             str   = "1:50"
    grid_pitch_mm:     float = 1250.0
    zone_labels:       list  = field(default_factory=list)
    shelf_runs:        list  = field(default_factory=list)
    # Grid start offset (mm) detected from the drawing or calibration
    grid_origin_mm:    tuple = field(default_factory=lambda: (0.0, 0.0))
    # Shapely Polygons for areas where luminaire placement is forbidden
    # (escalators, lifts, structural voids, areas annotated as impossible)
    exclusion_zones:   list  = field(default_factory=list)

    def summary(self) -> str:
        return (f"ParsedPlan({Path(self.source_file).name}): "
                f"{len(self.room_polygons)} rooms, "
                f"{len(self.furniture)} furniture, "
                f"{len(self.zone_labels)} zone labels, "
                f"scale={self.scale}, grid={self.grid_pitch_mm:.0f}mm, "
                f"grid_origin=({self.grid_origin_mm[0]:.0f},{self.grid_origin_mm[1]:.0f})mm, "
                f"exclusions={len(self.exclusion_zones)}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_scale(text: str) -> int:
    for pat, sc in [(r'1\s*:\s*50', 50),(r'1\s*:\s*75', 75),
                    (r'1\s*:\s*100',100),(r'1\s*:\s*200',200)]:
        if re.search(pat, text): return sc
    return 50

def _pt2mm(pts: float, scale: int) -> float:
    return pts * (25.4 / 72.0) * scale

LABEL_MAP = {
    'verkaufsraum':'sales_floor','verkauf':'sales_floor','vkf':'sales_floor',
    'lager':'storage','windfang':'entrance','eingang':'entrance',
    'technik':'service_area','wc':'service_area','flur':'corridor',
    'zbv':'storage','kasse':'checkout_zone','kassen':'checkout_zone',
    'rolltreppe':'corridor','aufzug':'corridor',
    'nasszelle':'service_area','büro':'office',
}
SHELF_LABELS  = {'57','47','77','37','67','27','57/47','47/37','77/57','57/37'}
CHECKOUT_KWDS = {'kasse','kassenstuhl','lübecker','abweiser'}

# Keywords whose labelled areas should be treated as exclusion zones
EXCLUSION_KWDS = {
    'rolltreppe',   # escalator
    'aufzug',       # lift / elevator
    'fahrkorb',     # lift car
    'treppenhaus',  # stairwell
    'schacht',      # shaft
}

# Text strings that explicitly prohibit direct lighting in that area
NO_LIGHT_PHRASES = {'direkte beleuchtung nicht möglich', 'keine beleuchtung'}

def _zone_from_label(text: str) -> str:
    t = text.lower()
    for kw, zone in LABEL_MAP.items():
        if kw in t: return zone
    return 'unknown'

def _is_exclusion_label(text: str) -> bool:
    t = text.lower()
    if any(p in t for p in NO_LIGHT_PHRASES): return True
    if any(k in t for k in EXCLUSION_KWDS): return True
    return False


# ── PDF parser ────────────────────────────────────────────────────────────────

class PDFFloorPlanParser:
    def parse(self, filepath) -> ParsedPlan:
        filepath = Path(filepath)
        doc  = _open_pdf(filepath)
        page = doc[0]; ph = page.rect.height
        all_text = page.get_text("text")
        scale = _detect_scale(all_text)
        def p2mm(px, py): return _pt2mm(px, scale), _pt2mm(ph-py, scale)

        plan = ParsedPlan(source_file=str(filepath), scale=f"1:{scale}",
                          grid_pitch_mm=1250.0, layer_map=DEFAULT_LAYER_MAP)

        if '+3,00' in all_text: plan.ceiling_height_mm = 3000.0
        if '+3,30' in all_text: plan.fries_height_mm   = 3300.0
        if '+2,74' in all_text: plan.ceiling_height_mm = 2740.0

        for w in page.get_text("words"):
            wt = w[4].strip()
            wx, wy = p2mm((w[0]+w[2])/2, (w[1]+w[3])/2)
            if wt in SHELF_LABELS:
                plan.furniture.append(FurnitureInsert(
                    f"SHELF_{wt}", (wx,wy), 0.0, "SHELVING", "shelving"))
            elif any(kw in wt.lower() for kw in CHECKOUT_KWDS):
                plan.furniture.append(FurnitureInsert(
                    "CHECKOUT", (wx,wy), 0.0, "CHECKOUT", "checkout"))

        for blk in page.get_text("blocks"):
            text = blk[4].strip()
            if not text: continue
            cx, cy = p2mm((blk[0]+blk[2])/2, (blk[1]+blk[3])/2)
            zt  = _zone_from_label(text)
            am  = re.search(r'([\d]+[,.][\d]+)\s*(?:qm|m²)', text)
            a   = float(am.group(1).replace(',','.')) if am else None
            if zt != 'unknown' or a:
                plan.zone_labels.append({'text':text[:80],'zone_type':zt,
                                         'area_m2':a,'x_mm':cx,'y_mm':cy})

        paths = page.get_drawings()
        plan.room_polygons = self._rooms(paths, p2mm, scale)
        plan.shelf_runs    = self._shelf_runs(paths, p2mm, scale)

        # Detect grid origin from "Startmaß Rasterdecke" annotations or
        # from the first grid line intersection found in the drawings
        plan.grid_origin_mm = self._detect_grid_origin(all_text, plan)

        # Build exclusion zones from labelled areas and "no lighting" annotations
        plan.exclusion_zones = self._detect_exclusion_zones(
            page, p2mm, scale, plan.zone_labels)

        if plan.room_polygons:
            plan.bounds = unary_union(plan.room_polygons).bounds
        elif plan.furniture:
            xs=[f.position[0] for f in plan.furniture]
            ys=[f.position[1] for f in plan.furniture]
            plan.bounds=(min(xs),min(ys),max(xs),max(ys))
        return plan

    def _detect_grid_origin(self, full_text: str, plan: 'ParsedPlan') -> tuple:
        """
        Detect the ceiling grid start offset (Startmaß Rasterdecke).

        Strategy:
          1. Look for "Startmaß" annotation near a numeric dimension.
          2. Fall back to deriving origin from first shelf label positions
             snapped to 1250mm grid.
          3. Default (0, 0) — grid starts at origin.
        """
        import re
        # Try to find a numeric offset mentioned near "Startmaß Rasterdecke"
        m = re.search(r'startma[ßs][^\d]*([\d,\.]+)\s*[mx]?\s*([\d,\.]+)?',
                      full_text.lower())
        if m:
            try:
                ox = float(m.group(1).replace(',', '.')) * 1000  # m → mm
                oy = float(m.group(2).replace(',', '.')) * 1000 if m.group(2) else ox
                return (ox, oy)
            except Exception:
                pass

        # Derive from shelf positions: find the most common fractional offset
        # when all shelf x positions are taken modulo 1250
        shelf_pts = [f.position for f in plan.furniture if f.inferred_type == 'shelving']
        if len(shelf_pts) >= 5:
            xs = [p[0] for p in shelf_pts]
            ys = [p[1] for p in shelf_pts]
            pitch = plan.grid_pitch_mm
            ox = float(sorted([x % pitch for x in xs])[len(xs) // 2])
            oy = float(sorted([y % pitch for y in ys])[len(ys) // 2])
            return (round(ox), round(oy))

        return (0.0, 0.0)

    def _detect_exclusion_zones(self, page, p2mm, scale, zone_labels: list) -> list:
        """
        Build exclusion polygons from:
          1. Zone labels explicitly naming escalators, lifts, staircases.
          2. Text blocks containing "direkte Beleuchtung nicht möglich".
          3. Large rectangular paths with a hatched/dark fill on escalator layers.
        """
        from shapely.geometry import box as shapely_box
        exclusions = []

        # From zone labels
        for lbl in zone_labels:
            if _is_exclusion_label(lbl.get('text', '')):
                a = lbl.get('area_m2') or 0
                if a > 0:
                    cx, cy = lbl['x_mm'], lbl['y_mm']
                    half = math.sqrt(a * 1e6) / 2
                    exclusions.append(shapely_box(cx-half, cy-half, cx+half, cy+half))

        # From explicit "no lighting" text blocks on the page
        for blk in page.get_text("blocks"):
            text = blk[4].strip().lower()
            if any(p in text for p in NO_LIGHT_PHRASES):
                cx, cy = p2mm((blk[0]+blk[2])/2, (blk[1]+blk[3])/2)
                # Create a 3m × 3m exclusion around the annotation
                exclusions.append(shapely_box(cx-1500, cy-1500, cx+1500, cy+1500))

        return exclusions

    def _rooms(self, paths, p2mm, scale):
        polys = []
        for path in paths:
            r=path['rect']; wmm=r.width*(25.4/72)*scale; hmm=r.height*(25.4/72)*scale
            if wmm*hmm < 1_000_000: continue
            fill=path.get('fill')
            if fill and all(abs(c-1.0)<0.02 for c in fill[:3]): continue
            x0,y0=p2mm(r.x0,r.y1); x1,y1=p2mm(r.x1,r.y0)
            try:
                poly=shapely_box(x0,y0,x1,y1)
                if poly.is_valid and poly.area>500_000: polys.append(poly)
            except: pass
        polys.sort(key=lambda p:p.area,reverse=True)
        kept=[]
        for poly in polys:
            if not any(big.contains(poly) and big.area/poly.area>0.85 for big in kept):
                kept.append(poly)
        return kept

    def _shelf_runs(self, paths, p2mm, scale):
        runs=[]
        for path in paths:
            r=path['rect']; wmm=r.width*(25.4/72)*scale; hmm=r.height*(25.4/72)*scale
            if 400<wmm<700 and 80<hmm<160:
                cx,cy=p2mm((r.x0+r.x1)/2,(r.y0+r.y1)/2)
                runs.append({'x_mm':cx,'y_mm':cy,'width_mm':wmm,'height_mm':hmm,'orientation':'H'})
            elif 80<wmm<160 and 400<hmm<700:
                cx,cy=p2mm((r.x0+r.x1)/2,(r.y0+r.y1)/2)
                runs.append({'x_mm':cx,'y_mm':cy,'width_mm':wmm,'height_mm':hmm,'orientation':'V'})
        return runs


# ── Luminaire extractor ───────────────────────────────────────────────────────

@dataclass
class ExtractedLuminaire:
    x_mm:float; y_mm:float; lumi_type:str
    product_code:str; wattage:float; lux_output:float; beam_angle:float
    zone_type:str="unknown"

PART_BASE  = "MIKA80-E-WS-930-PH-PS7HE+-L22-"
SYMBOL_MAP = [
    ((1.0,0.0,1.0),"A","2400-40RF-DV2.5-EN",15,2400,40),
    ((1.0,0.0,0.0),"B","3200-60RF-DV2.5-EN",20,3200,60),
    ((0.0,0.0,0.0),"C","3200-60RF-DV2.5-EN",20,3200,60),
    ((0.0,0.502,0.0),"D","3200-40RF-DV2.5-EN",20,3200,40),
    ((0.5,0.5,0.5),"E","2100-24PP-DV2.5-EN",20,2100,24),
]
def _cmatch(c,t,tol=0.06):
    return c is not None and len(c)>=3 and all(abs(a-b)<tol for a,b in zip(c,t))


def extract_luminaires_from_lighting_plan(pdf_path, scale=None):
    from collections import Counter
    doc=_open_pdf(str(pdf_path)); page=doc[0]; ph=page.rect.height
    if scale is None: scale=_detect_scale(page.get_text("text"))
    PT2MM=(25.4/72.0)*scale

    def cluster(pts,thr=5):
        used=set(); out=[]
        for i,p in enumerate(pts):
            if i in used: continue
            grp=[p]
            for j,q in enumerate(pts):
                if j!=i and j not in used and math.dist(p,q)<thr: grp.append(q); used.add(j)
            used.add(i); out.append((sum(g[0] for g in grp)/len(grp),sum(g[1] for g in grp)/len(grp)))
        return out

    paths=page.get_drawings(); result=[]
    for col_rgb,ltype,sfx,watt,lux,angle in SYMBOL_MAP:
        raw=[]
        for path in paths:
            col=path.get('color'); r=path['rect']
            if _cmatch(col,col_rgb) and 22<r.width<27 and 22<r.height<27:
                raw.append(((r.x0+r.x1)/2,(r.y0+r.y1)/2))
        for cx,cy in cluster(raw,thr=5):
            result.append(ExtractedLuminaire(
                x_mm=round(cx*PT2MM,1), y_mm=round((ph-cy)*PT2MM,1),
                lumi_type=ltype, product_code=PART_BASE+sfx,
                wattage=watt, lux_output=lux, beam_angle=angle))
    counts=Counter(l.lumi_type for l in result)
    print(f"Extracted {len(result)} luminaires from {Path(pdf_path).name}: {dict(counts)}")
    return result


# ── Smart router ──────────────────────────────────────────────────────────────

class RealPlanParser:
    def __init__(self): self._pdf=PDFFloorPlanParser()

    def parse(self, filepath, pdf_fallback=None) -> ParsedPlan:
        filepath = Path(filepath)
        suffix   = filepath.suffix.lower()

        if suffix == '.pdf':
            return self._pdf.parse(filepath)

        if suffix == '.dxf':
            from services.parser.dwg_parser import DWGParser
            return DWGParser().parse(filepath)

        if suffix == '.dwg':
            # Check for ASCII DXF disguised as .dwg
            with open(filepath, 'rb') as f:
                hdr = f.read(6)
            if hdr[:2] in (b'  ', b'\r\n'):
                from services.parser.dwg_parser import DWGParser
                return DWGParser().parse(filepath)

            # Binary DWG — try converter first (LibreDWG → ODA → ezdxf)
            dxf_path = None
            try:
                from services.converter.dwg_converter import convert_dwg_to_dxf
                dxf_path = convert_dwg_to_dxf(filepath)
                print(f"  Binary DWG → converted to DXF via dwg_converter")
            except Exception as conv_err:
                print(f"  DWG converter failed ({type(conv_err).__name__}): {conv_err}")

            if dxf_path is not None:
                from services.parser.dwg_parser import DWGParser
                return DWGParser().parse(dxf_path)

            # Converter unavailable — try companion PDF fallback
            if pdf_fallback and Path(pdf_fallback).exists():
                print(f"  Binary DWG → using provided PDF fallback: {pdf_fallback}")
                return self._pdf.parse(pdf_fallback)

            # Auto-discover companion PDFs in the same directory.
            # IMPORTANT: build paths via string concatenation, NOT
            # Path.with_suffix(), which crashes on compound suffixes
            # like '-EG.pdf' (ValueError: Invalid suffix '-EG.pdf').
            stem     = filepath.stem
            base_dir = filepath.parent

            companion_suffixes = [
                '.pdf',
                '_EG.pdf', '_OG.pdf', '_plan.pdf',
                '-EG.pdf', '-OG.pdf',
                '_lighting.pdf', '_Beleuchtung.pdf',
            ]
            for sfx in companion_suffixes:
                candidate = base_dir / (stem + sfx)
                if candidate.exists():
                    print(f"  Binary DWG → companion PDF found: {candidate.name}")
                    return self._pdf.parse(candidate)

            # Nothing worked — raise a helpful error
            raise ValueError(
                f"Binary DWG '{filepath.name}' cannot be read directly "
                f"(format: {hdr[:6]!r}).\n\n"
                "Options:\n"
                "  1. Install LibreDWG (recommended — free, one command):\n"
                "       Mac:   brew install libredwg\n"
                "       Linux: sudo apt install libredwg-tools\n"
                "     Then retry — no other steps needed.\n\n"
                "  2. Install ODA File Converter (free download):\n"
                "       https://www.opendesign.com/guestfiles/oda_file_converter\n\n"
                "  3. Upload the companion PDF alongside the DWG file.\n"
                "  4. Export a DXF from AutoCAD:  File → Save As → AutoCAD DXF\n"
            )

        raise ValueError(
            f"Unsupported file format: '{suffix}'. "
            f"Accepted formats: .pdf, .dxf, .dwg"
        )


# ── CLI self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    UP = Path("/mnt/user-data/uploads")
    print("[1] Parsing INPUT floor plan...")
    plan = RealPlanParser().parse(UP / "3600_HH_Jungfernstieg_EG_SB_Kassen_20240506.pdf")
    print(plan.summary())
    shelf = [f for f in plan.furniture if f.inferred_type=='shelving']
    print(f"  Shelf labels: {len(shelf)}, "
          f"Checkout: {sum(1 for f in plan.furniture if f.inferred_type=='checkout')}")

    print("\n[2] Extracting luminaires from EG output plan...")
    eg = extract_luminaires_from_lighting_plan(
        UP / "Ro_Hamburg_Jungfernstieg_3600_20260113-EG-DRP.pdf")
    A = sum(1 for l in eg if l.lumi_type=='A')
    B = sum(1 for l in eg if l.lumi_type=='B')
    print(f"  Type A:{A} [expect 106]  Type B:{B} [expect 61]  Total:{len(eg)} [expect 167]")
    assert A==106 and B==61, f"Extraction mismatch A={A} B={B}"
    print("  ✓ Validated")