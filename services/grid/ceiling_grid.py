"""
services/grid/ceiling_grid.py

625mm ceiling tile grid engine for Rossmann retail luminaire placement.

Domain facts (from DXF semantic audit and technical documents):
  • The ceiling module is 625mm × 625mm ("Referenzmaß Rasterdecke")
  • Luminaires sit at defined sub-tile positions within each 625mm tile:
      A_center  — tile centre at (312.5, 312.5) mm within the tile (most common)
      B_corner  — corner/edge offset at (150, 150) mm within the tile
      C_pair    — two luminaires per tile at (150, 312.5) and (475, 312.5)
  • Typical inter-luminaire spacing along a shelf run: 1250mm = 2 tiles
  • Grid origin and direction are planner-confirmed or auto-detected from MF_Raster lines
  • Exterior wall shelves (≤ 625mm from outer building contour) get the
    high-beam luminaire variant (*U1549), interior shelves get the standard (*U1528)

Architecture:
  generate_shelf_row_candidates()  — grid positions along detected shelf runs
  classify_wall_relation()         — exterior / sales-area-boundary / interior
  filter_candidates()              — hard exclusion rules
"""
from __future__ import annotations
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from shapely.geometry import Point, Polygon, MultiPoint

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from services.log import get_logger

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TILE_MM = 625.0          # fundamental ceiling tile size

# Sub-positions within a 625mm tile (offsets from tile origin corner)
TILE_SUBPOSITIONS: dict[str, list[tuple[float, float]]] = {
    'A_center': [(312.5, 312.5)],           # tile centre — most common interior
    'B_corner': [(150.0, 150.0)],           # corner/edge offset
    'C_pair':   [(150.0, 312.5), (475.0, 312.5)],  # paired luminaires
    'D_special': [(312.5, 150.0)],          # special/checkout offset
    'E_special': [(475.0, 475.0)],          # column-adjacent offset
}

# Exterior wall threshold: luminaire position within this distance of the outer
# building contour → exterior-wall shelf classification → high-beam luminaire.
# Calibrated for wall-gondola-pass placement:
# Wall gondola candidates are placed AT the gondola tile (not aisle midpoint),
# so the A_center sits 312mm inside the gondola tile.  For a gondola 64mm from
# the north wall (Bad Nenndorf north) A_center is 248mm from the wall → AW.
# For a gondola 257mm from the south wall A_center is 569mm → misses at 535mm.
# 580mm bridges the gap for the south wall case without over-capturing interior
# lights (second-nearest gondola rows are 1200mm+ from any wall).
EXTERIOR_WALL_THRESHOLD_MM = 580.0

# Luminaire spacing along shelf runs (2 tiles = 1250mm inter-luminaire distance)
INTER_LUMINAIRE_TILES = 2       # every N tiles along the shelf axis
INTER_LUMINAIRE_MM    = TILE_MM * INTER_LUMINAIRE_TILES  # 1250mm


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class GridCandidate:
    x: float
    y: float
    tile_i: int                 # tile column index from origin
    tile_j: int                 # tile row index from origin
    subposition: str            # 'A_center', 'B_corner', etc.
    wall_relation: str = 'interior'   # 'exterior_wall' | 'sales_area_boundary' | 'interior'
    shelf_dist_mm: float = float('inf')
    assortment: str = ''
    legal: bool = True


# ── Grid generators ───────────────────────────────────────────────────────────

def grid_origin_from_lines(
    grid_lines: list,           # list of CeilingGridLine objects
    pitch: float = TILE_MM,
) -> tuple[float, float]:
    """
    Detect grid origin (ox, oy) from parsed MF_Raster ceiling grid lines.
    Returns the fractional offset of the grid from the drawing origin.
    """
    h_ys = sorted({round(l.start[1]) for l in grid_lines
                   if abs(l.end[1] - l.start[1]) < abs(l.end[0] - l.start[0])})
    v_xs = sorted({round(l.start[0]) for l in grid_lines
                   if abs(l.end[0] - l.start[0]) < abs(l.end[1] - l.start[1])})

    ox = (v_xs[0] % pitch) if v_xs else 0.0
    oy = (h_ys[0] % pitch) if h_ys else 0.0
    return round(ox), round(oy)


def snap_to_grid(x: float, y: float,
                 ox: float, oy: float,
                 pitch: float = TILE_MM) -> tuple[float, float]:
    """Snap (x, y) to the nearest 625mm grid tile origin."""
    gx = round((x - ox) / pitch) * pitch + ox
    gy = round((y - oy) / pitch) * pitch + oy
    return gx, gy


def snap_to_subposition(x: float, y: float,
                         ox: float, oy: float,
                         pitch: float = TILE_MM,
                         subpos: str = 'A_center') -> tuple[float, float]:
    """
    Return the exact sub-position coordinate within the 625mm tile containing (x, y).

    Tile origin = lower-left corner of the tile.
    Sub-position offsets are from that corner (not from tile centre).
    For A_center: offset (312.5, 312.5) → exact tile centre.
    """
    offsets = TILE_SUBPOSITIONS.get(subpos, [(312.5, 312.5)])
    # Find the tile that contains (x, y)
    i = math.floor((x - ox) / pitch)
    j = math.floor((y - oy) / pitch)
    tile_ox = ox + i * pitch
    tile_oy = oy + j * pitch
    # Use the first offset (for single-luminaire subpositions)
    off_x, off_y = offsets[0]
    return tile_ox + off_x, tile_oy + off_y


# ── Shelf-run candidate generator ────────────────────────────────────────────

def generate_shelf_row_candidates(
    shelf_objects: list,        # list of FurnitureInsert with .position, .rotation
    zone_poly:     Polygon,
    ox: float, oy: float,
    pitch: float        = TILE_MM,
    spacing_mm: float   = INTER_LUMINAIRE_MM,
    subpos: str         = 'A_center',
    clearance_mm: float = 200.0,
) -> list[GridCandidate]:
    """
    Generate luminaire candidate positions along detected shelf rows.

    Algorithm:
    1. Snap each shelf INSERT to the 625mm grid.
    2. Group shelves by orientation (horizontal / vertical) detected from block rotation.
    3. For horizontal shelves: group by snapped Y row, collect unique X positions,
       generate positions from min_x to max_x at `spacing_mm` intervals.
    4. For vertical shelves: same logic transposed.
    5. Apply the requested tile sub-position offset within each 625mm tile.
    6. Keep only candidates that fall inside (or within clearance of) the zone polygon.

    Returns:
        List of GridCandidate objects, one per valid luminaire position.
    """
    if not shelf_objects:
        return []

    # Slightly-buffered zone for containment check
    zone_buf = zone_poly.buffer(clearance_mm)

    h_rows: dict[float, list[float]] = {}   # {snapped_gy: [snapped_gx ...]}
    v_cols: dict[float, list[float]] = {}   # {snapped_gx: [snapped_gy ...]}

    # For PDF-parsed shelves (rotation=0, unknown direction) detect whether the
    # store's gondola layout is predominantly E-W (h_rows) or N-S (v_cols) using
    # the ratio of the shelf span in each direction.  Register shelves only in
    # the dominant direction to avoid the opposite direction generating spurious
    # candidates for every grid row.
    all_xs = [f.position[0] for f in shelf_objects]
    all_ys = [f.position[1] for f in shelf_objects]
    x_span = max(all_xs) - min(all_xs) if all_xs else 1.0
    y_span = max(all_ys) - min(all_ys) if all_ys else 1.0
    # Dominant direction: if shelves spread ≥ 5% more in X than Y → E-W gondolas
    # (standard Rossmann layout — gondola rows run along the long axis).
    # Register rotation-unknown PDF shelves only in h_rows to avoid the vertical pass
    # generating a duplicate set of candidates for every E-W gondola row.
    # Threshold 1.05 catches all stores where X ≥ Y; only genuinely taller-than-wide
    # footprints (very unusual in retail) use dual-registration.
    h_dominant = x_span >= y_span * 1.05

    for f in shelf_objects:
        gx, gy = snap_to_grid(f.position[0], f.position[1], ox, oy, pitch)
        rot = getattr(f, 'rotation', 0.0) % 180  # collapse 0/180 and 90/270
        horizontal = rot < 45 or rot > 135        # within ±45° of 0° → horizontal
        rotation_unknown = (rot == 0.0 and not getattr(f, '_rotation_known', False))

        if horizontal or rotation_unknown:
            h_rows.setdefault(gy, [])
            if gx not in h_rows[gy]:
                h_rows[gy].append(gx)
        # For rotation-unknown shelves in an H-dominant store, skip v_cols to
        # prevent every Y annotation row from generating a spurious column aisle.
        if not horizontal or (rotation_unknown and not h_dominant):
            v_cols.setdefault(gx, [])
            if gy not in v_cols[gx]:
                v_cols[gx].append(gy)

    # ── Merge adjacent rows that are within 1 tile of each other ─────────────
    # A single physical gondola contributes multiple INSERT rows (frame,
    # internal structure, facing panels) all 625mm apart on the grid.  We must
    # collapse these into one representative row per gondola.
    #
    # Algorithm:
    #   1. Pre-filter: discard any row with fewer than min_pre shelf positions.
    #      Structural/frame blocks typically appear 1-5 times; real shelf runs
    #      appear 6+ times.  This breaks the mega-chains that otherwise swallow
    #      multiple separate gondola rows into one component.
    #   2. Union-find on the survivors: any two rows ≤ threshold apart belong
    #      to the same gondola face and share one luminaire row.
    #   3. Pick the row with the most shelf positions as the representative.
    #
    # Use union-find (not greedy "mark used") so that A↔B and B↔C all end up
    # in one cluster even when B is adjacent to both A and C.
    def _cluster_rows(raw: dict[float, list[float]],
                      threshold: float = pitch * 0.4,
                      min_pre: int = 2,
                      min_nodes: int = 3) -> dict[float, list[float]]:
        if not raw:
            return raw
        # Pre-filter: discard rows too sparse to be a real shelf run
        filtered = {k: v for k, v in raw.items() if len(v) >= min_pre}
        if not filtered:
            return {}
        keys = sorted(filtered.keys())

        # Span-limited greedy scan: group consecutive annotation rows that belong
        # to the same physical gondola. A gondola spans at most 2 grid tiles
        # (1250mm). We break a cluster when:
        #   (a) the next row is > pitch away (clear gap, new gondola), OR
        #   (b) adding it would push the cluster span beyond 2×pitch (1250mm)
        # This prevents PDF annotation densities (every 625mm) from chaining all
        # rows into one mega-cluster via transitive union-find, while still
        # merging the two annotation rows of one back-to-back gondola face pair.
        MAX_SPAN = pitch * 2.0   # 1250mm — max Y (or X) span of one gondola

        clusters: list[list[float]] = []
        cur: list[float] = [keys[0]]

        for i in range(1, len(keys)):
            gap_from_prev  = keys[i] - keys[i - 1]
            span_if_added  = keys[i] - cur[0]
            if gap_from_prev <= pitch and span_if_added <= MAX_SPAN:
                cur.append(keys[i])
            else:
                clusters.append(cur)
                cur = [keys[i]]
        clusters.append(cur)

        clustered: dict[float, list[float]] = {}
        for grp in clusters:
            total_nodes = sum(len(filtered[k]) for k in grp)
            if total_nodes < min_nodes:
                continue
            # Representative = key with the most shelf positions
            rep = max(grp, key=lambda k: len(filtered[k]))
            merged: list[float] = []
            seen: set[float] = set()
            for k in grp:
                for x in filtered[k]:
                    if x not in seen:
                        merged.append(x)
                        seen.add(x)
            clustered[rep] = merged

        return clustered

    _h_raw_count = len(h_rows)
    _v_raw_count = len(v_cols)
    h_rows = _cluster_rows(h_rows)
    v_cols = _cluster_rows(v_cols)

    log.info(f"Shelf rows (H): raw={_h_raw_count} → clustered={len(h_rows)} gondola rows")
    for _gy, _xs in sorted(h_rows.items()):
        log.debug(f"  H-row y={_gy/1000:.2f} m | {len(_xs)} positions  "
                  f"x: {min(_xs)/1000:.1f}→{max(_xs)/1000:.1f} m")
    log.info(f"Shelf cols (V): raw={_v_raw_count} → clustered={len(v_cols)} gondola cols")

    candidates: list[GridCandidate] = []
    seen_keys: set = set()

    step_tiles = max(1, round(spacing_mm / pitch))   # 2 for 1250mm/625mm
    step = step_tiles * pitch                         # 1250mm

    def _run_positions_phased(coords: list[float], axis_origin: float) -> list[float]:
        """
        Generate luminaire positions along one axis of a shelf run.

        The 1250mm inter-luminaire sub-grid has TWO possible phases relative to
        the 625mm ceiling grid (even tiles: 0,2,4... or odd tiles: 1,3,5...).
        We determine which phase best matches the actual shelf positions and
        generate candidates in that phase, extending the run from the first to
        the last shelf node (inclusive).

        When the row spans shelves with gaps (discontinuous runs), we generate
        across gaps smaller than 3 × step to avoid large voids.
        """
        if not coords:
            return []
        sorted_c = sorted(set(coords))
        first    = sorted_c[0]
        last     = sorted_c[-1]

        if step < pitch:
            return sorted_c  # dense: every position is valid

        # Determine which phase of the 1250mm sub-grid the shelf nodes prefer.
        # Tile index of each shelf node along this axis:
        tile_idxs = [round((c - axis_origin) / pitch) for c in sorted_c]
        even_count = sum(1 for t in tile_idxs if t % step_tiles == 0)
        odd_count  = len(tile_idxs) - even_count

        # Start phase: 0-based tile index of the first luminaire
        if even_count >= odd_count:
            # Phase 0: start at the first even-tiled shelf position
            phase_start = next((c for c in sorted_c
                                if round((c - axis_origin) / pitch) % step_tiles == 0),
                               first)
        else:
            # Phase 1: start at the first odd-tiled shelf position
            phase_start = next((c for c in sorted_c
                                if round((c - axis_origin) / pitch) % step_tiles == 1),
                               first)

        # Generate positions from phase_start to last, stepping by `step`
        result = []
        pos    = phase_start
        while pos <= last + step * 0.5:
            result.append(pos)
            pos += step

        # Extend backwards if there are shelves before phase_start
        pos = phase_start - step
        while pos >= first - step:
            result.append(pos)
            pos -= step

        return sorted(set(result))

    # ── Aisle-based candidate generation (horizontal shelf rows) ─────────────
    # Lights must be in the AISLE between facing gondola rows, not on the
    # gondola itself.  Cross-section spec: luminaire ~800 mm from the shelf
    # face (INNEN: equidistant between two facing gondolas; AUSSEN: 800 mm
    # from single-face wall shelf).  Reference §7.4 median shelf-axis distance
    # is 430 mm.  Placing at gondola Y gives 0 mm offset — wrong.
    #
    # For each adjacent pair of clustered gondola rows, snap the midpoint to
    # the nearest 625 mm tile.  The A_center (312.5, 312.5) offset within that
    # aisle tile lands roughly 800 mm from each shelf face for the typical
    # Rossmann aisle width (1400–1800 mm ≈ 2–3 grid tiles gondola-to-gondola).
    # X candidates come from the intersection of both flanking rows' x-extents:
    # lights are only placed where gondola rows face each other on both sides.
    #
    # MIN_AISLE_GAP: skip gondola pairs that are within 2 tile-widths of each
    # other (1250 mm) — these represent the same gondola structure (frame +
    # shelf panel, or two structural blocks), NOT a real walkable aisle.
    gondola_ys = sorted(h_rows.keys())
    aisle_rows: dict[float, list[float]] = {}
    _MIN_AISLE_GAP = pitch * 2.0   # 1250 mm — minimum separation for a real aisle
    # (1250mm = exactly 2 grid tiles; back-to-back gondola faces that ended up in
    # separate clusters after span-limited clustering are ≤ 625mm apart and get
    # filtered here; real aisles are ≥ 1250mm wide)

    if len(gondola_ys) >= 2:
        for idx in range(len(gondola_ys) - 1):
            y1 = gondola_ys[idx]
            y2 = gondola_ys[idx + 1]
            if y2 - y1 < _MIN_AISLE_GAP:
                log.debug(f"  Skip pair y={y1/1000:.2f}↔{y2/1000:.2f} m: "
                          f"gap={y2-y1:.0f} mm < {_MIN_AISLE_GAP:.0f} mm (same gondola)")
                continue
            j_mid   = round(((y1 + y2) / 2.0 - oy) / pitch)
            y_aisle = j_mid * pitch + oy
            _xs1, _xs2 = h_rows[y1], h_rows[y2]
            _x_lo = max(min(_xs1), min(_xs2))
            _x_hi = min(max(_xs1), max(_xs2))
            if _x_lo >= _x_hi:
                log.debug(f"  Skip pair y={y1/1000:.2f}↔{y2/1000:.2f} m: "
                          f"no shared x-range")
                continue
            xs: list[float] = [x for x in {*_xs1, *_xs2} if _x_lo <= x <= _x_hi]
            log.debug(f"  Aisle y={y_aisle/1000:.2f} m | between gondola rows "
                      f"{y1/1000:.2f}↔{y2/1000:.2f} m | gap={y2-y1:.0f} mm | "
                      f"x: {_x_lo/1000:.1f}→{_x_hi/1000:.1f} m")
            if y_aisle in aisle_rows:
                xs = list({*aisle_rows[y_aisle], *xs})
            aisle_rows[y_aisle] = xs
    elif gondola_ys:
        log.info("Single gondola row detected — using direct placement (no aisle pairing)")
        aisle_rows = dict(h_rows)

    log.info(f"Aisles (H): {len(aisle_rows)} aisle rows detected")

    for gy, xs in aisle_rows.items():
        for lx in _run_positions_phased(xs, ox):
            cx, cy = snap_to_subposition(lx, gy, ox, oy, pitch, subpos)
            key = (round(cx), round(cy))
            if key in seen_keys:
                continue
            if zone_buf.contains(Point(cx, cy)):
                i = round((lx - ox) / pitch)
                j = round((gy - oy) / pitch)
                seen_keys.add(key)
                candidates.append(GridCandidate(
                    x=round(cx, 1), y=round(cy, 1),
                    tile_i=i, tile_j=j, subposition=subpos))

    log.info(f"Candidates generated: {len(candidates)} total (H-aisle pass)")

    # Wall gondola pass removed: previously placed candidates AT the gondola Y
    # (on the shelf body), which caused lights to appear inside gondola furniture.
    # The aisle loop above already handles wall shelves correctly: the midpoint
    # between the wall gondola row and the adjacent interior gondola lands in the
    # aisle and is classified as AW by classify_wall_relation (proximity to the
    # building envelope) or by the depth/assortment signal in _shelf_wall_signal.

    # ── Aisle-based candidate generation (vertical shelf columns / specialty zone)
    # For specialty zones where gondolas run N-S (V-columns), aisles are E-W
    # gaps between adjacent column pairs.  Mirror of the H-row aisle loop above.
    # We skip pairs whose gap exceeds _MAX_V_AISLE_GAP to avoid bridging across
    # entirely different store sections (e.g. main gondola area ↔ specialty zone).
    zone_boundary    = zone_poly.boundary
    gondola_xs       = sorted(v_cols.keys())
    v_aisle_cols: dict[float, list[float]] = {}   # {x_aisle: [ys...]}
    _MAX_V_AISLE_GAP = pitch * 10.0   # 6250mm — skip inter-zone gaps

    if len(gondola_xs) >= 2:
        for idx in range(len(gondola_xs) - 1):
            x1 = gondola_xs[idx]
            x2 = gondola_xs[idx + 1]
            if x2 - x1 < _MIN_AISLE_GAP:
                continue   # too close — same gondola structure
            if x2 - x1 > _MAX_V_AISLE_GAP:
                continue   # too wide — different store section, not a real aisle
            i_mid   = round(((x1 + x2) / 2.0 - ox) / pitch)
            x_aisle = i_mid * pitch + ox
            ys: list[float] = list({*v_cols[x1], *v_cols[x2]})
            if x_aisle in v_aisle_cols:
                ys = list({*v_aisle_cols[x_aisle], *ys})
            v_aisle_cols[x_aisle] = ys

    for gx, ys in v_aisle_cols.items():
        for ly in _run_positions_phased(ys, oy):
            cx, cy = snap_to_subposition(gx, ly, ox, oy, pitch, subpos)
            key = (round(cx), round(cy))
            if key in seen_keys:
                continue
            if zone_buf.contains(Point(cx, cy)):
                i = round((gx - ox) / pitch)
                j = round((ly - oy) / pitch)
                seen_keys.add(key)
                candidates.append(GridCandidate(
                    x=round(cx, 1), y=round(cy, 1),
                    tile_i=i, tile_j=j, subposition=subpos))

    # Wall column pass removed for the same reason as the wall gondola pass:
    # it placed candidates AT the gondola column X position (on the shelf body).
    # V-aisle midpoints near the building envelope are classified as AW by
    # classify_wall_relation, so no separate wall-column pass is needed.

    return candidates


def generate_area_candidates(
    zone_poly:    Polygon,
    ox: float, oy: float,
    pitch: float        = TILE_MM,
    spacing_mm: float   = INTER_LUMINAIRE_MM,
    subpos: str         = 'A_center',
    clearance_mm: float = 300.0,
) -> list[GridCandidate]:
    """
    Fallback: tessellate the entire zone polygon with grid candidates
    at `spacing_mm` intervals (no shelf guidance needed).
    Used for storage, ancillary rooms, or when no shelf data is available.
    """
    inset = zone_poly.buffer(-clearance_mm)
    if inset.is_empty:
        inset = zone_poly

    step = round(spacing_mm / pitch) * pitch
    if step < pitch:
        step = pitch

    b   = inset.bounds
    offsets = TILE_SUBPOSITIONS.get(subpos, [(312.5, 312.5)])
    off_x, off_y = offsets[0]

    # Align start to grid
    sx = math.ceil((b[0] - ox) / step) * step + ox
    sy = math.ceil((b[1] - oy) / step) * step + oy

    candidates: list[GridCandidate] = []
    gx = sx
    while gx <= b[2] + 1:
        gy = sy
        while gy <= b[3] + 1:
            # The actual luminaire sits at the subposition offset within the tile
            tile_ox = math.floor((gx - ox) / pitch) * pitch + ox
            tile_oy = math.floor((gy - oy) / pitch) * pitch + oy
            cx = tile_ox + off_x
            cy = tile_oy + off_y
            if inset.contains(Point(cx, cy)):
                i = round((gx - ox) / pitch)
                j = round((gy - oy) / pitch)
                candidates.append(GridCandidate(
                    x=round(cx, 1), y=round(cy, 1),
                    tile_i=i, tile_j=j, subposition=subpos))
            gy += step
        gx += step
    return candidates


# ── Wall-relation classifier ──────────────────────────────────────────────────

def classify_wall_relation(
    positions:          list[tuple[float, float]],
    building_envelope:  Optional[Polygon],
    sales_area_poly:    Optional[Polygon] = None,
    exterior_threshold: float = EXTERIOR_WALL_THRESHOLD_MM,
    boundary_threshold: float = 1250.0,   # sales-area internal boundary
) -> list[str]:
    """
    For each position, classify wall relationship as:
      'exterior_wall'         — within exterior_threshold of outer building contour
      'sales_area_boundary'   — within boundary_threshold of sales-area edge (internal wall)
      'interior'              — everything else

    Mirrors the methodology from the DXF semantic audit:
      Real exterior wall (52 shelf modules): distance to outer building contour ≤ 625mm
      Sales-area wall boundary (69 modules): distance to sales-area edge ≤ 1250mm
    """
    result = []
    envelope_boundary   = building_envelope.boundary if building_envelope else None
    sales_area_boundary = sales_area_poly.boundary   if sales_area_poly   else None

    # For PDF input building_envelope is often unavailable.  Use the sales-floor
    # polygon boundary as a proxy: in most Rossmann stores the outer gondola rows
    # run directly against the building walls, so the sales-area edge ≈ exterior.
    effective_exterior = envelope_boundary if envelope_boundary is not None else sales_area_boundary

    for pos in positions:
        pt  = Point(*pos)
        rel = 'interior'

        if effective_exterior is not None:
            if pt.distance(effective_exterior) <= exterior_threshold:
                rel = 'exterior_wall'
            elif sales_area_boundary is not None and envelope_boundary is not None:
                if pt.distance(sales_area_boundary) <= boundary_threshold:
                    rel = 'sales_area_boundary'

        result.append(rel)
    return result
