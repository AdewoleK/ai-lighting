"""
lighting-ai/services/parser/dwg_parser.py

Layer 1 — DWG Import & Parsing (M1)

Reads a DXF/DWG file and extracts:
  - Room boundary polygons (from closed polylines)
  - Furniture block inserts (name, position, rotation)
  - Ceiling grid lines + grid origin
  - Doors, windows, annotations
  - Exclusion zones (escalators, lifts, voids)

Binary DWG files are automatically converted to DXF via the ODA converter
before parsing (see services/converter/dwg_converter.py).

All geometry is returned in model-space millimetres.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import ezdxf
import numpy as np
from shapely.geometry import Polygon, LineString, MultiPolygon, Point, box as shapely_box
from shapely.ops import unary_union

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import DEFAULT_LAYER_MAP

# Block/layer names that represent exclusion zones
_EXCLUSION_BLOCKS  = {'rolltreppe','escalator','aufzug','lift','elevator',
                      'treppenhaus','staircase','schacht','shaft','fahrkorb'}
_EXCLUSION_LAYERS  = {'ROLLTREPPE','ESCALATOR','AUFZUG','LIFT','TREPPENHAUS',
                      'STAIRCASE','SCHACHT','LIFT-SHAFT','VOID'}


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FurnitureInsert:
    block_name: str
    position: tuple[float, float]   # (x, y) in mm
    rotation: float                 # degrees
    layer: str
    inferred_type: str = "unknown"  # set by block_name_to_type()


@dataclass
class CeilingGridLine:
    start: tuple[float, float]
    end:   tuple[float, float]
    layer: str


@dataclass
class ParsedPlan:
    source_file: str
    room_polygons: list[Polygon]            = field(default_factory=list)
    furniture: list[FurnitureInsert]        = field(default_factory=list)
    grid_lines: list[CeilingGridLine]       = field(default_factory=list)
    door_positions: list[tuple]             = field(default_factory=list)
    window_positions: list[tuple]           = field(default_factory=list)
    annotations: list[dict]                 = field(default_factory=list)
    ceiling_height_mm: float                = 3000.0
    layer_map: dict                         = field(default_factory=dict)
    bounds: Optional[tuple]                 = None  # (minx, miny, maxx, maxy)
    # Detected ceiling grid start offset (mm)
    grid_origin_mm: tuple                   = field(default_factory=lambda: (0.0, 0.0))
    grid_pitch_mm: float                    = 1250.0
    # Polygons where luminaire placement is forbidden
    exclusion_zones: list                   = field(default_factory=list)
    # Zone labels extracted from text (same format as pdf_parser)
    zone_labels: list                       = field(default_factory=list)
    shelf_runs: list                        = field(default_factory=list)
    scale: str                              = "1:50"

    def summary(self) -> str:
        return (
            f"ParsedPlan({Path(self.source_file).name}): "
            f"{len(self.room_polygons)} rooms, "
            f"{len(self.furniture)} furniture items, "
            f"{len(self.grid_lines)} grid lines, "
            f"grid_origin=({self.grid_origin_mm[0]:.0f},{self.grid_origin_mm[1]:.0f})mm, "
            f"exclusions={len(self.exclusion_zones)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Block name → furniture type mapping
# ─────────────────────────────────────────────────────────────────────────────

BLOCK_TYPE_MAP: dict[str, str] = {
    # Checkout / POS
    "checkout": "checkout", "pos": "checkout", "cashier": "checkout",
    "kasse": "checkout", "kassenstuhl": "checkout", "kassentisch": "checkout",
    # Shelving / racks
    "shelf": "shelving", "shelving": "shelving", "rack": "shelving",
    "gondola": "shelving", "regal": "shelving", "ausstattung": "shelving",
    "tier": "shelving",
    # Service / office
    "desk": "desk", "counter": "desk", "service": "desk",
    "theke": "desk",
    # Doors / windows (block-style)
    "door": "door", "tur": "door", "tür": "door",
    "window": "window", "fenster": "window",
    # Storage
    "storage": "storage", "pallet": "storage", "euro": "storage",
}

# Rossmann-specific: shelf block names encode a zone-depth code as a numeric segment.
# Block names like 'T57m_14', 'I1 47_29', 'AU343H-22-11x0' contain these codes.
_ROSSMANN_SHELF_CODES = {'27', '37', '47', '57', '67', '77', '97', '127'}


def block_name_to_type(block_name: str) -> str:
    import re
    name_lower = block_name.lower()
    for key, ftype in BLOCK_TYPE_MAP.items():
        if key in name_lower:
            return ftype
    # Rossmann shelf units embed a depth/zone code as a standalone numeric segment
    numeric_parts = re.split(r'[^0-9]+', block_name)
    if any(p in _ROSSMANN_SHELF_CODES for p in numeric_parts if p):
        return 'shelving'
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Layer helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_layer_set(layer_map: dict) -> dict[str, set[str]]:
    """Return {category: {layer_name_upper, ...}} for fast membership tests."""
    return {
        cat: {l.upper() for l in layers}
        for cat, layers in layer_map.items()
    }


def entity_category(entity, layer_sets: dict[str, set[str]]) -> str:
    layer = entity.dxf.layer.upper()
    for cat, names in layer_sets.items():
        if layer in names:
            return cat
        # prefix match: layer "A-WALL-DEMO" still matches "A-WALL"
        for name in names:
            if layer.startswith(name):
                return cat
    return "other"


# ─────────────────────────────────────────────────────────────────────────────
# Polyline → Shapely polygon
# ─────────────────────────────────────────────────────────────────────────────

def lwpolyline_to_polygon(entity) -> Optional[Polygon]:
    """Convert a LWPOLYLINE to a Shapely Polygon if it is closed."""
    try:
        pts = [(p[0], p[1]) for p in entity.get_points()]
        if len(pts) < 3:
            return None
        is_closed = entity.closed or (pts[0] == pts[-1])
        if not is_closed:
            # Auto-close if start/end are within 1 mm
            if math.dist(pts[0], pts[-1]) < 1.0:
                is_closed = True
        if not is_closed:
            return None
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        return poly if poly.area > 100 else None   # ignore < 100 mm² noise
    except Exception:
        return None


def lines_to_polygons(lines: list) -> list[Polygon]:
    """
    Attempt to close open line-segment loops into polygons.
    Used when room boundaries are drawn as individual LINE entities
    rather than closed LWPOLYLINE.
    """
    from shapely.ops import polygonize
    geoms = [LineString([(l.dxf.start.x, l.dxf.start.y),
                         (l.dxf.end.x,   l.dxf.end.y)]) for l in lines]
    polys = list(polygonize(geoms))
    return [p for p in polys if p.area > 10_000]  # > 0.01 m²


def _detect_grid_origin_from_lines(
        grid_lines: list[CeilingGridLine],
        pitch_hint: float = 1250.0) -> tuple[tuple, float]:
    """
    Compute the ceiling grid start offset and pitch from grid line positions.

    Returns ((origin_x_mm, origin_y_mm), pitch_mm).

    Method:
      - Separate horizontal (H) and vertical (V) lines.
      - Measure spacings between consecutive parallel lines → median = pitch.
      - The fractional part of the first line position (mod pitch) = origin offset.
    """
    h_ys = sorted({round(l.start[1]) for l in grid_lines
                   if abs(l.end[1]-l.start[1]) < abs(l.end[0]-l.start[0])})
    v_xs = sorted({round(l.start[0]) for l in grid_lines
                   if abs(l.end[0]-l.start[0]) < abs(l.end[1]-l.start[1])})

    def _pitch(coords):
        gaps = [abs(coords[i]-coords[i-1]) for i in range(1, len(coords))]
        valid = [g for g in gaps if 500 < g < 2000]
        return float(np.median(valid)) if valid else pitch_hint

    pitch_x = _pitch(v_xs) if len(v_xs) >= 2 else pitch_hint
    pitch_y = _pitch(h_ys) if len(h_ys) >= 2 else pitch_hint
    pitch   = round((pitch_x + pitch_y) / 2 / 25) * 25  # snap to 25mm

    ox = (v_xs[0] % pitch) if v_xs else 0.0
    oy = (h_ys[0] % pitch) if h_ys else 0.0

    return (round(ox), round(oy)), pitch


# ─────────────────────────────────────────────────────────────────────────────
# Main parser
# ─────────────────────────────────────────────────────────────────────────────

class DWGParser:
    def __init__(self, layer_map: dict = None):
        self.layer_map = layer_map or DEFAULT_LAYER_MAP
        self._layer_sets = build_layer_set(self.layer_map)

    # ── Public API ────────────────────────────────────────────────────────────

    def parse(self, filepath: str | Path) -> ParsedPlan:
        """
        Parse a DXF/DWG file and return a ParsedPlan.

        Supports:
          - DXF R12 through R2018+
          - Binary DWG — automatically converted via ODA File Converter or
            ezdxf recovery mode (see services/converter/dwg_converter.py)
        """
        filepath = Path(filepath)

        # Convert binary DWG → DXF if needed
        if filepath.suffix.lower() == '.dwg':
            with open(filepath, 'rb') as f:
                hdr = f.read(6)
            is_ascii = hdr[:2] in (b'  ', b'\r\n') or b'SECTION' in hdr
            if not is_ascii:
                from services.converter.dwg_converter import convert_dwg_to_dxf
                filepath = convert_dwg_to_dxf(filepath)

        plan = ParsedPlan(
            source_file=str(filepath),
            layer_map=self.layer_map,
        )

        try:
            doc = ezdxf.readfile(str(filepath))
        except ezdxf.DXFStructureError:
            # Try recovery mode for damaged files
            doc, _ = ezdxf.recover.readfile(str(filepath))

        msp = doc.modelspace()

        wall_lines: list = []
        raw_polys: list[Polygon] = []
        exclusion_polys: list[Polygon] = []

        for entity in msp:
            etype = entity.dxftype()
            cat   = entity_category(entity, self._layer_sets)
            layer_up = entity.dxf.layer.upper()

            # ── Closed polylines → room boundaries or exclusions ──────────
            if etype == "LWPOLYLINE":
                poly = lwpolyline_to_polygon(entity)
                if poly:
                    if layer_up in _EXCLUSION_LAYERS:
                        exclusion_polys.append(poly)
                    elif cat in ("walls", "other", "ceiling"):
                        raw_polys.append(poly)

            elif etype == "POLYLINE":
                pts = [(v.dxf.location.x, v.dxf.location.y)
                       for v in entity.vertices]
                if len(pts) >= 3:
                    try:
                        poly = Polygon(pts)
                        if poly.is_valid and poly.area > 100:
                            if layer_up in _EXCLUSION_LAYERS:
                                exclusion_polys.append(poly)
                            else:
                                raw_polys.append(poly)
                    except Exception:
                        pass

            # ── Individual lines → collect for polygonisation / grid ───────
            elif etype == "LINE":
                if cat in ("walls", "other"):
                    wall_lines.append(entity)
                elif cat in ("grid", "ceiling"):
                    plan.grid_lines.append(CeilingGridLine(
                        start=(entity.dxf.start.x, entity.dxf.start.y),
                        end=(entity.dxf.end.x, entity.dxf.end.y),
                        layer=entity.dxf.layer,
                    ))

            # ── Block inserts → furniture or exclusions ────────────────────
            elif etype == "INSERT":
                bname = entity.dxf.name
                pos   = (entity.dxf.insert.x, entity.dxf.insert.y)
                rot   = getattr(entity.dxf, "rotation", 0.0)
                ftype = block_name_to_type(bname)
                fi    = FurnitureInsert(
                    block_name=bname,
                    position=pos,
                    rotation=rot,
                    layer=entity.dxf.layer,
                    inferred_type=ftype,
                )
                plan.furniture.append(fi)
                # Block names matching escalators/lifts → mark as exclusion
                if any(k in bname.lower() for k in _EXCLUSION_BLOCKS):
                    exclusion_polys.append(
                        shapely_box(pos[0]-2000, pos[1]-2000,
                                    pos[0]+2000, pos[1]+2000))

            # ── Text/Mtext → annotations + zone labels ────────────────────
            elif etype in ("TEXT", "MTEXT"):
                if etype == "TEXT":
                    text = getattr(entity.dxf, 'text', '') or ''
                else:
                    # ezdxf 1.x renamed plain_mtext() → plain_text()
                    if hasattr(entity, 'plain_text'):
                        text = entity.plain_text()
                    elif hasattr(entity, 'plain_mtext'):
                        text = entity.plain_mtext()
                    else:
                        text = getattr(entity, 'text', '') or ''
                try:
                    pos = (entity.dxf.insert.x, entity.dxf.insert.y)
                except Exception:
                    continue
                plan.annotations.append({"text": text, "position": pos,
                                          "layer": entity.dxf.layer})
                # Infer zone label from German text
                from services.parser.pdf_parser import (
                    _zone_from_label, _is_exclusion_label, SHELF_LABELS)
                text_stripped = text.strip()
                if text_stripped in SHELF_LABELS:
                    plan.furniture.append(FurnitureInsert(
                        f"SHELF_{text_stripped}", pos, 0.0,
                        entity.dxf.layer, "shelving"))
                elif _is_exclusion_label(text_stripped):
                    exclusion_polys.append(
                        shapely_box(pos[0]-1500, pos[1]-1500,
                                    pos[0]+1500, pos[1]+1500))
                else:
                    zt = _zone_from_label(text_stripped)
                    if zt != 'unknown':
                        plan.zone_labels.append({
                            'text': text_stripped[:80],
                            'zone_type': zt,
                            'area_m2': None,
                            'x_mm': pos[0],
                            'y_mm': pos[1],
                        })

        # ── Polygonise loose wall lines ────────────────────────────────────
        if wall_lines:
            raw_polys.extend(lines_to_polygons(wall_lines))

        # ── Deduplicate & filter room polygons ─────────────────────────────
        plan.room_polygons   = self._clean_polygons(raw_polys)
        plan.exclusion_zones = exclusion_polys

        # ── Ceiling grid origin & pitch ────────────────────────────────────
        if plan.grid_lines:
            plan.grid_origin_mm, plan.grid_pitch_mm = \
                _detect_grid_origin_from_lines(plan.grid_lines)

        # ── Bounding box ───────────────────────────────────────────────────
        if plan.room_polygons:
            all_geom = unary_union(plan.room_polygons)
            plan.bounds = all_geom.bounds
        elif plan.furniture:
            xs = [f.position[0] for f in plan.furniture]
            ys = [f.position[1] for f in plan.furniture]
            plan.bounds = (min(xs), min(ys), max(xs), max(ys))

        return plan

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _clean_polygons(raw: list[Polygon]) -> list[Polygon]:
        """
        Remove duplicates, nested small polygons, and invalid geometry.
        Keeps the largest enclosing polygon when one fully contains another.
        """
        valid = [p for p in raw if p.is_valid and p.area > 1_000]
        valid.sort(key=lambda p: p.area, reverse=True)

        kept: list[Polygon] = []
        for poly in valid:
            dominated = False
            for big in kept:
                if big.contains(poly) and big.area / poly.area > 0.95:
                    dominated = True
                    break
            if not dominated:
                kept.append(poly)
        return kept


# ─────────────────────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        # Create a synthetic test DXF if no file is provided
        print("No file given — running synthetic test...")
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()

        # Draw a simple rectangular room (10m × 8m)
        room_pts = [(0,0),(10000,0),(10000,8000),(0,8000),(0,0)]
        msp.add_lwpolyline(room_pts, close=True,
                           dxfattribs={"layer": "WALLS"})

        # Add a shelf block reference
        msp.add_blockref("SHELF_1200",
                         insert=(2000, 2000),
                         dxfattribs={"layer": "FURNITURE"})

        # Add ceiling grid lines
        for x in range(0, 10001, 600):
            msp.add_line((x, 0), (x, 8000),
                         dxfattribs={"layer": "CEILING-GRID"})
        for y in range(0, 8001, 600):
            msp.add_line((0, y), (10000, y),
                         dxfattribs={"layer": "CEILING-GRID"})

        test_path = "/tmp/test_plan.dxf"
        doc.saveas(test_path)
        filepath = test_path
    else:
        filepath = sys.argv[1]

    parser = DWGParser()
    plan   = parser.parse(filepath)
    print(plan.summary())
    print(f"  Bounds: {plan.bounds}")
    for i, poly in enumerate(plan.room_polygons):
        print(f"  Room {i}: area={poly.area/1e6:.2f} m²")
    for fi in plan.furniture[:5]:
        print(f"  Furniture: {fi.block_name} → {fi.inferred_type} @ {fi.position}")
    print(f"  Grid lines: {len(plan.grid_lines)}")