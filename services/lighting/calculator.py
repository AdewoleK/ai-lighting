"""
services/lighting/calculator.py

EN 12464-1 Lumen Method (flux method) for Rossmann retail floor plans.

Formula:
    n = (E_m × A) / (Φ × η × MF)

    E_m  — maintained illuminance target [lux]
    A    — room area [m²]
    Φ    — luminous flux per luminaire [lm]
    η    — utilisation factor (from Room Index + surface reflectances)
    MF   — maintenance factor
    n    — required number of luminaires

Room Index:
    k = (l × b) / (h_m × (l + b))
    where h_m = mounting height above work plane [m]

References:
  EN 12464-1:2021  Light and lighting — Lighting of work places — Indoor
  DIN 5035-3       Retail and wholesale
  CIE 97:2005      Maintenance of indoor electric lighting systems
"""
from __future__ import annotations
import math
from dataclasses import dataclass

# ── Maintained illuminance targets (EN 12464-1 Table 5.37 + DIN 5035-3) ──────

TARGET_LUX: dict[str, int] = {
    'sales_floor':    750,   # EN 12464-1 §5.37 — general retail
    'checkout_zone':  500,   # EN 12464-1 §5.37 — checkout counter
    'storage':        200,   # EN 12464-1 §5.7  — storage areas
    'corridor':       200,   # general circulation
    'entrance':       300,   # transition zone (200–500 lux)
    'display_window': 500,   # window display — accent lighting
    'service_area':   500,   # service / office counter
    'office':         500,   # EN 12464-1 §5.3  — office
    'unknown':        300,   # conservative default
    # No-lighting zones (placeholder — never actually computed)
    'windfang':         0,
    'escalator':        0,
    'elevator':         0,
    'wc':               0,
    'technical':        0,
}

# ── No-lighting zone types ────────────────────────────────────────────────────
#
# These zone types NEVER receive luminaires. Source:
#   Rossmann EG plan annotation: "Direkte Beleuchtung nicht möglich" (Windfang)
#   Structural exclusion: escalator shaft, elevator cabin
#   Separate scope: WC (IP65 wet-room fixtures), technical (utility fixtures)

NO_LIGHTING_ZONE_TYPES: frozenset = frozenset({
    'windfang',      # entrance vestibule — "Direkte Beleuchtung nicht möglich"
    'escalator',     # escalator shaft — structural exclusion (Rolltreppe)
    'elevator',      # elevator cabin — has own integrated lighting (Aufzug)
    'wc',            # restrooms — IP65 wet-room fixtures, separate scope
    'technical',     # utility/technical rooms — different fixture type
    'service_area',  # back-of-house counters — non-MIKA80 fixtures, separate scope
    'office',        # back-of-house office — non-MIKA80 fixtures, separate scope
})

# ── Rossmann standard constants ───────────────────────────────────────────────

MAINTENANCE_FACTOR:  float = 0.80   # quarterly cleaning, LED source (CIE 97)
WORK_PLANE_MM:       int   = 850    # EN 12464-1 standard work plane
DEFAULT_CEILING_MM:  int   = 3000   # Rossmann UK Rasterdecke standard
BASE_GRID_PITCH_MM:  int   = 1250   # Startmaß Rasterdecke (confirmed on all plans)

# ── Complete luminaire catalog ────────────────────────────────────────────────
#
# Based on MAX FRANKE.led MIKA80-E and NEO85-SX product families as specified
# in the Rossmann Hamburg Jungfernstieg 3600 lighting plans (Jan 2026).
#
# K-codes = Rossmann plan category codes; letter codes = internal system types.
#
# Type | K-code | Product                          | W  | lm   | Beam | Use
# -----|--------|----------------------------------|----|------|------|---------------------------
#  A   | K1     | MIKA80-E 2400-40 DV2.5           | 15 | 2400 | 40°  | Shelf inner aisle
#  C   | K3     | MIKA80-E 2400-40 DV2.5 (Rand)    | 15 | 2400 | 40°  | Shelf edge / perimeter
#  B   | K4     | MIKA80-E 3200-60 DV2.5           | 20 | 3200 | 60°  | Supplement / wide-angle
#  D   | K2     | MIKA80-E 3200-40 DV2.5           | 20 | 3200 | 40°  | Checkout / service strong
#  P   | K5p    | MIKA80-E 2100-24 PL DV2.5        | 16 | 2100 | 24°  | Poster / K5 Plakate
#  W   | —      | MIKA80-E 1700-36 Wabe DV2.5      | 20 | 1700 | 36°  | Cosmetics anti-glare
#  E   | K6     | NEO85-SX 3200-60 track           | 20 | 3200 | 60°  | Window display (Schaufenster)

LUMINAIRE_CATALOG: dict[str, dict] = {
    'A': {
        'product': 'MIKA80-E-WS-930-PH-PS7HE+-L22-2400-40RF-DV2.5-EN*',
        'description': 'MIKA80-E K1 Regalbeleuchtung 15W 40° 2400lm 3000K',
        'watt': 15, 'flux_lm': 2400, 'beam_deg': 40,
        'cutout_mm': 128, 'embed_mm': 110, 'outer_mm': 140,
        'mounting': 'grid_recessed', 'ip': 'IP20', 'cri': 90, 'cct_k': 3000,
        'k_code': 'K1', 'use': 'Shelf inner aisle',
    },
    'C': {
        'product': 'MIKA80-E-WS-930-PH-PS7HE+-L22-2400-40RF-DV2.5-EN*',
        'description': 'MIKA80-E K3 Regalbeleuchtung Rand 15W 40° 2400lm 3000K',
        'watt': 15, 'flux_lm': 2400, 'beam_deg': 40,
        'cutout_mm': 128, 'embed_mm': 110, 'outer_mm': 140,
        'mounting': 'grid_recessed', 'ip': 'IP20', 'cri': 90, 'cct_k': 3000,
        'k_code': 'K3', 'use': 'Shelf edge / perimeter row',
    },
    'B': {
        'product': 'MIKA80-E-WS-930-PH-PS7HE+-L22-3200-60RF-DV2.5-EN*',
        'description': 'MIKA80-E K4 Ergänzungsbeleuchtung 20W 60° 3200lm 3000K',
        'watt': 20, 'flux_lm': 3200, 'beam_deg': 60,
        'cutout_mm': 128, 'embed_mm': 110, 'outer_mm': 140,
        'mounting': 'grid_recessed', 'ip': 'IP20', 'cri': 90, 'cct_k': 3000,
        'k_code': 'K4', 'use': 'Supplemental / wide-angle fill',
    },
    'D': {
        'product': 'MIKA80-E-WS-930-PH-PS7HE+-L22-3200-40RF-DV2.5-EN*',
        'description': 'MIKA80-E K2 Checkout/Service 20W 40° 3200lm 3000K',
        'watt': 20, 'flux_lm': 3200, 'beam_deg': 40,
        'cutout_mm': 128, 'embed_mm': 110, 'outer_mm': 140,
        'mounting': 'grid_recessed', 'ip': 'IP20', 'cri': 90, 'cct_k': 3000,
        'k_code': 'K2', 'use': 'Checkout counter / service task lighting',
    },
    'P': {
        'product': 'MIKA80-E-WS-930-PH-PS7HE+-L15-2100-24PP-DV2.5-EN*',
        'description': 'MIKA80-E K5 Plakate 16W 24° 2100lm Power-Linse 3000K',
        'watt': 16, 'flux_lm': 2100, 'beam_deg': 24,
        'cutout_mm': 128, 'embed_mm': 110, 'outer_mm': 140,
        'mounting': 'grid_recessed', 'ip': 'IP20', 'cri': 90, 'cct_k': 3000,
        'k_code': 'K5p', 'use': 'Poster / promotional banner accent',
    },
    'W': {
        'product': 'MIKA80-E-WS-930-PH-PS7HE+-L22-1700-36PP-W-DV2.5-EN*',
        'description': 'MIKA80-E Wabeneinsatz 20W 36° 1700lm Anti-Glare 3000K',
        'watt': 20, 'flux_lm': 1700, 'beam_deg': 36,
        'cutout_mm': 128, 'embed_mm': 110, 'outer_mm': 140,
        'mounting': 'grid_recessed', 'ip': 'IP20', 'cri': 90, 'cct_k': 3000,
        'k_code': 'W', 'use': 'Cosmetics / glare-sensitive special positions',
    },
    'E': {
        'product': 'NEO85-SX-WS-930-PH-PS7HE+-L22-3200-60RF-EN+',
        'description': 'NEO85-SX K6 Schaufenster 20W 60° 3200lm Track 3000K',
        'watt': 20, 'flux_lm': 3200, 'beam_deg': 60,
        'cutout_mm': 85, 'embed_mm': 146, 'outer_mm': 85,
        'mounting': 'track_3phase', 'ip': 'IP20', 'cri': 90, 'cct_k': 3000,
        'k_code': 'K6', 'use': 'Window display / Schaufenster track spotlight',
    },
}

# ── Luminaire luminous flux [lm] per type ─────────────────────────────────────

LUMINAIRE_FLUX: dict[str, int] = {
    # Letter type codes
    'A': 2400,   # MIKA80-E K1 15W 40° — shelf inner aisle
    'C': 2400,   # MIKA80-E K3 15W 40° — shelf edge/perimeter (same spec as A)
    'B': 3200,   # MIKA80-E K4 20W 60° — supplemental wide-angle
    'D': 3200,   # MIKA80-E K2 20W 40° — checkout/service strong
    'P': 2100,   # MIKA80-E K5 16W 24° — poster accent power-lens
    'W': 1700,   # MIKA80-E     20W 36° — honeycomb anti-glare cosmetics
    'E': 3200,   # NEO85-SX  K6 20W 60° — window display track spotlight
    # Rossmann K-code aliases
    'K1': 2400,  'K3': 2400,
    'K2': 3200,  'K4': 3200,
    'K5': 2100,  'K6': 3200,
}

# ── Zone lighting strategy ────────────────────────────────────────────────────
#
# Defines which luminaire type to use as primary and supplement for each zone.
# primary   = main grid downlight type
# supplement= wide-angle fill for areas not covered by shelf-guided primary
# edge      = type used for perimeter/edge positions (if different from primary)

ZONE_LIGHTING_STRATEGY: dict[str, dict] = {
    'sales_floor':    {'primary': 'A', 'supplement': 'B', 'edge': 'C'},
    'checkout_zone':  {'primary': 'D', 'supplement': 'B'},
    'storage':        {'primary': 'B'},
    'corridor':       {'primary': 'A'},
    'entrance':       {'primary': 'B'},
    'display_window': {'primary': 'E'},
    'service_area':   {'primary': 'D', 'supplement': 'B'},
    'office':         {'primary': 'D'},
    'unknown':        {'primary': 'A'},
}

# Primary lumi type per zone (drives UF selection in zone_spec)
ZONE_LUMI_TYPE: dict[str, str] = {
    'sales_floor':    'A',
    'checkout_zone':  'D',
    'storage':        'B',
    'corridor':       'A',
    'entrance':       'B',
    'display_window': 'E',
    'service_area':   'D',
    'office':         'D',
    'unknown':        'A',
    # No-lighting zones — placeholder, never processed
    'windfang':       'A',
    'escalator':      'A',
    'elevator':       'A',
    'wc':             'A',
    'technical':      'A',
}

# ── Utilisation-factor table ──────────────────────────────────────────────────
#
# Derived from photometric data for MIKA80-E family (recessed downlight).
# Surface reflectances: ceiling 70 %, walls 50 %, floor 20 % (typical Rossmann).
#
#   k     UF_40° (Type A/D)   UF_60° (Type B/C)
_UF_TABLE: list[tuple[float, float, float]] = [
    (0.60,  0.34,  0.40),
    (0.80,  0.40,  0.47),
    (1.00,  0.45,  0.53),
    (1.25,  0.50,  0.58),
    (1.50,  0.53,  0.61),
    (2.00,  0.58,  0.67),
    (2.50,  0.61,  0.70),
    (3.00,  0.63,  0.72),
    (4.00,  0.66,  0.75),
    (5.00,  0.68,  0.77),
]


# ── Core calculation functions ────────────────────────────────────────────────

def room_index(
    width_mm: float,
    depth_mm: float,
    ceiling_mm: float = DEFAULT_CEILING_MM,
) -> float:
    """
    Room Index  k = (l × b) / (h_m × (l + b))
    Clamped to [0.6, 5.0] per EN 12464-1 UF tables.
    """
    l   = width_mm / 1000
    b   = depth_mm / 1000
    h_m = max((ceiling_mm - WORK_PLANE_MM) / 1000, 0.1)
    if l <= 0 or b <= 0:
        return 1.0
    k = (l * b) / (h_m * (l + b))
    return round(min(max(k, 0.6), 5.0), 3)


def utilisation_factor(k: float, lumi_type: str = 'A') -> float:
    """Interpolate UF from the table for the given Room Index and luminaire type."""
    col = 1 if lumi_type in ('A', 'D', 'E') else 2
    if k <= _UF_TABLE[0][0]:
        return _UF_TABLE[0][col]
    if k >= _UF_TABLE[-1][0]:
        return _UF_TABLE[-1][col]
    for i in range(1, len(_UF_TABLE)):
        if k <= _UF_TABLE[i][0]:
            k0, k1 = _UF_TABLE[i-1][0], _UF_TABLE[i][0]
            v0, v1 = _UF_TABLE[i-1][col], _UF_TABLE[i][col]
            return round(v0 + (k - k0) / (k1 - k0) * (v1 - v0), 4)
    return 0.65


def required_count(
    area_m2: float,
    zone_type: str,
    lumi_type: str = 'A',
    ceiling_mm: float = DEFAULT_CEILING_MM,
    room_width_mm: float = None,
    room_depth_mm: float = None,
) -> int:
    """
    Required luminaire count from the lumen method.
    n = (E_m × A) / (Φ × η × MF)
    """
    E   = TARGET_LUX.get(zone_type, 300)
    Phi = LUMINAIRE_FLUX.get(lumi_type, 2400)

    if room_width_mm is None or room_depth_mm is None:
        # Estimate proportional dimensions from area (assume 2 : 1 aspect)
        room_depth_mm  = math.sqrt(area_m2 * 1e6 / 2)
        room_width_mm  = 2 * room_depth_mm

    k   = room_index(room_width_mm, room_depth_mm, ceiling_mm)
    eta = utilisation_factor(k, lumi_type)
    n   = (E * area_m2) / (Phi * eta * MAINTENANCE_FACTOR)
    return max(1, math.ceil(n))


def optimal_spacing_mm(area_m2: float, n_required: int) -> float:
    """Optimal square-grid spacing in mm for the required count."""
    if n_required <= 0:
        return float(BASE_GRID_PITCH_MM)
    return math.sqrt(area_m2 / n_required) * 1000


def snap_to_ceiling_grid(spacing_mm: float, base: int = BASE_GRID_PITCH_MM) -> int:
    """
    Snap a calculated spacing to the nearest multiple of the Rossmann
    ceiling grid module (1250 mm).  Half-module (625 mm) is also valid.
    Candidates: 625, 1250, 1875, 2500, 3125, 3750, 5000
    """
    candidates = [
        base // 2,           # 625
        base,                # 1250
        base * 3 // 2,       # 1875
        base * 2,            # 2500
        base * 5 // 2,       # 3125
        base * 3,            # 3750
        base * 4,            # 5000
    ]
    return min(candidates, key=lambda p: abs(p - spacing_mm))


# ── All-in-one zone specification ─────────────────────────────────────────────

@dataclass
class ZoneLightingSpec:
    zone_type:            str
    area_m2:              float
    room_width_m:         float
    room_depth_m:         float
    ceiling_height_m:     float
    room_index_k:         float
    utilisation_factor:   float
    maintenance_factor:   float
    target_lux:           int
    luminaire_type:       str
    luminaire_flux_lm:    int
    required_count:       int
    optimal_spacing_mm:   float
    grid_pitch_mm:        int     # snapped to Rossmann ceiling grid
    maintained_lux:       float   # achieved Em with grid_pitch spacing

    def summary(self) -> str:
        return (
            f"{self.zone_type} {self.area_m2:.0f}m²  "
            f"k={self.room_index_k:.2f}  η={self.utilisation_factor:.2f}  "
            f"n_req={self.required_count}  pitch={self.grid_pitch_mm}mm  "
            f"Em={self.maintained_lux:.0f}lux (target {self.target_lux}lux)"
        )


def zone_spec(
    zone_type: str,
    area_m2: float,
    polygon_bounds: tuple,          # (minx, miny, maxx, maxy) in drawing units (mm)
    ceiling_mm: float = DEFAULT_CEILING_MM,
    base_pitch: int = BASE_GRID_PITCH_MM,
) -> ZoneLightingSpec:
    """
    Compute the complete lighting specification for a zone.

    Uses bounding-box dimensions scaled by the polygon fill factor to get
    effective room dimensions (better than raw bounding box for irregular shapes).
    """
    minx, miny, maxx, maxy = polygon_bounds
    bb_w_mm = abs(maxx - minx)
    bb_d_mm = abs(maxy - miny)

    # Scale bounding-box dimensions down to effective room dimensions
    bb_area_m2 = (bb_w_mm / 1000) * (bb_d_mm / 1000)
    fill = min(1.0, area_m2 / bb_area_m2) if bb_area_m2 > 0 else 1.0
    eff_w_mm = bb_w_mm * math.sqrt(fill)
    eff_d_mm = bb_d_mm * math.sqrt(fill)

    lt  = ZONE_LUMI_TYPE.get(zone_type, 'A')
    E   = TARGET_LUX.get(zone_type, 300)
    Phi = LUMINAIRE_FLUX[lt]

    k   = room_index(eff_w_mm, eff_d_mm, ceiling_mm)
    eta = utilisation_factor(k, lt)

    n_req    = max(1, math.ceil((E * area_m2) / (Phi * eta * MAINTENANCE_FACTOR)))
    opt_s    = optimal_spacing_mm(area_m2, n_req)
    snapped  = snap_to_ceiling_grid(opt_s, base_pitch)

    # Validate: actual lux with snapped pitch
    area_per_lumi = (snapped / 1000) ** 2
    n_actual = area_m2 / area_per_lumi if area_per_lumi > 0 else n_req
    Em = (Phi * n_actual * eta * MAINTENANCE_FACTOR) / area_m2

    return ZoneLightingSpec(
        zone_type           = zone_type,
        area_m2             = round(area_m2, 2),
        room_width_m        = round(eff_w_mm / 1000, 2),
        room_depth_m        = round(eff_d_mm / 1000, 2),
        ceiling_height_m    = round(ceiling_mm / 1000, 2),
        room_index_k        = round(k, 2),
        utilisation_factor  = round(eta, 3),
        maintenance_factor  = MAINTENANCE_FACTOR,
        target_lux          = E,
        luminaire_type      = lt,
        luminaire_flux_lm   = Phi,
        required_count      = n_req,
        optimal_spacing_mm  = round(opt_s, 1),
        grid_pitch_mm       = snapped,
        maintained_lux      = round(Em, 1),
    )
