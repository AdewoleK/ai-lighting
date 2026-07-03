#!/usr/bin/env python3
"""
lightingai_bridge.py — Mac AutoCAD bridge

Connects the AutoLISP plugin (LightingAI.lsp) to the Python FastAPI backend.

Usage (run from Terminal, AFTER running LIGHTINGAI_SETUP in AutoCAD):

    python3 lightingai_bridge.py /path/to/floorplan.dwg

    Optional flags:
      --api       http://localhost:8000   API base URL
      --project   "Rossmann Hamburg EG"
      --customer  "Dirk Rossmann GmbH"
      --concept   rossmann_standard
      --out       /tmp/lightingai_commands.lsp   output LISP file

What it does:
  1. Reads grid origin from  /tmp/lightingai_origin.json  (written by LIGHTINGAI_SETUP)
  2. Uploads the DWG to the FastAPI backend  (POST /process)
  3. Polls  GET /jobs/{id}  until done
  4. Writes  /tmp/lightingai_commands.lsp  — a LISP file AutoCAD will execute
     to INSERT every luminaire block, draw the legend, and draw the title block

After this script finishes, go back to AutoCAD and run:  LIGHTINGAI_PLACE
"""

from __future__ import annotations
import argparse, json, sys, time, textwrap
from pathlib import Path

import requests   # pip install requests


# ── Config ────────────────────────────────────────────────────────────────────

# ~/ai-lighting/ is persistent — macOS cleans /tmp files periodically
_AI_DIR       = Path.home() / "ai-lighting"
ORIGIN_FILE   = _AI_DIR / "lightingai_origin.json"
COMMANDS_FILE = _AI_DIR / "lightingai_commands.lsp"

TYPE_ACI = {          # AutoCAD Color Index per luminaire type (defaults)
    "A": 6,           # magenta
    "B": 1,           # red
    "C": 4,           # cyan
    "D": 2,           # yellow
    "E": 5,           # blue
}
CUTOUT_R  = 64.0      # half of 128 mm cutout diameter
OUTER_R   = 70.0      # outer circle for legend symbols

TYPE_CONFIG_FILE = _AI_DIR / "lightingai_typeconfig.json"

DEFAULT_SHAPES = {    # default symbol shape per type letter
    "A": "Circle",
    "B": "Square",
    "C": "Diamond",
    "D": "Triangle",
    "E": "Cross",
    "F": "Hexagon",
}

COLOR_NAME_ACI = {    # human-readable color name → ACI value
    "Red":     1,
    "Yellow":  2,
    "Green":   3,
    "Cyan":    4,
    "Blue":    5,
    "Magenta": 6,
    "White":   7,
    "Orange":  30,
}


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="LightingAI Mac bridge")
    p.add_argument("dwg",          nargs="?", help="Path to the floor plan DWG file")
    p.add_argument("--api",        default="http://localhost:8000")
    p.add_argument("--project",    default="Rossmann EG")
    p.add_argument("--customer",   default="Dirk Rossmann GmbH")
    p.add_argument("--concept",    default="rossmann_standard")
    p.add_argument("--out",        default=str(COMMANDS_FILE))
    p.add_argument("--regenerate", action="store_true",
                   help="Re-generate commands.lsp from the last stored job result "
                        "(no upload needed — just applies updated type config)")
    return p.parse_args()


def regenerate_from_db(out_path: Path, project: str, customer: str, concept: str) -> bool:
    """
    Re-generate lightingai_commands.lsp from the most recent job for the CURRENT
    floor plan.  The current floor plan is read from lightingai_origin.json (written
    by LIGHTINGAI_GRID).  If origin.json is missing or has no dwg_bytes, falls back
    to the globally latest job.  Returns True on success.
    """
    import sqlite3

    # Try to find the current floor plan's filename from origin.json
    current_dwg_base: str | None = None
    origin_file = _AI_DIR / "lightingai_origin.json"
    if origin_file.exists():
        try:
            origin = json.loads(origin_file.read_text())
            dwg_bytes = origin.get("dwg_bytes")
            if dwg_bytes:
                dwg_path = bytes(dwg_bytes).decode("latin-1")
                current_dwg_base = Path(dwg_path).stem  # filename without extension
        except Exception:
            pass

    db_path = _AI_DIR / "data" / "jobs.db"
    if not db_path.exists():
        db_path = Path(__file__).parent.parent.parent / "data" / "jobs.db"
    if not db_path.exists():
        print("[LightingAI] Regenerate: jobs.db not found — run bridge with DWG first.")
        return False
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        if current_dwg_base:
            # Prefer the most recent job whose filename matches the current floor plan
            cur.execute("SELECT result, project_name, customer FROM jobs "
                        "WHERE status='done' AND result IS NOT NULL "
                        "  AND filename LIKE ? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (f"%{current_dwg_base}%",))
            row = cur.fetchone()
            if not row:
                print(f"[LightingAI] No job found for '{current_dwg_base}' — run Step 3 first.")
                con.close()
                return False
        else:
            cur.execute("SELECT result, project_name, customer FROM jobs "
                        "WHERE status='done' AND result IS NOT NULL "
                        "ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
        con.close()
    except Exception as ex:
        print(f"[LightingAI] Regenerate: DB read failed — {ex}")
        return False
    if not row:
        print("[LightingAI] Regenerate: no completed job in DB — run bridge with DWG first.")
        return False
    result_json, db_project, db_customer = row
    result   = json.loads(result_json)
    project  = db_project  or project
    customer = db_customer or customer
    placed   = result.get("placed", [])
    label    = current_dwg_base or "latest"
    print(f"[LightingAI] Regenerating commands for {len(placed)} luminaires "
          f"(floor plan: {label}, no upload needed)…")
    write_commands_lsp(out_path, result, project, customer, concept)
    print("[LightingAI] ✓  commands.lsp updated — run LIGHTINGAI_CLEAR + LIGHTINGAI_PLACE")
    return True


# ── API calls ─────────────────────────────────────────────────────────────────

def check_health(api: str) -> bool:
    try:
        r = requests.get(f"{api}/health", timeout=5)
        return r.ok and r.json().get("status") == "ok"
    except Exception:
        return False


def submit_plan(api: str, dwg_path: Path,
                project: str, customer: str, concept: str) -> str:
    """Upload DWG → returns job_id."""
    with open(dwg_path, "rb") as fh:
        r = requests.post(
            f"{api}/process",
            files={"file": (dwg_path.name, fh, "application/octet-stream")},
            data={"project_name": project, "customer": customer, "concept_id": concept},
            timeout=30,
        )
    r.raise_for_status()
    return r.json()["job_id"]


def poll_job(api: str, job_id: str, interval: float = 2.5) -> dict:
    """Poll /jobs/{id} until done or error."""
    while True:
        r = requests.get(f"{api}/jobs/{job_id}", timeout=10)
        r.raise_for_status()
        data = r.json()
        status = data["status"]
        print(f"  [{status}]  {data.get('message', '')}", flush=True)
        if status in ("done", "error"):
            return data
        time.sleep(interval)


# ── LISP code generation ──────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape a string for use inside a LISP string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _pt(x: float, y: float) -> str:
    """Format a 2-D point as a LISP list."""
    return f"(list {x:.4f} {y:.4f} 0)"


def load_type_config() -> dict:
    """
    Read /tmp/lightingai_typeconfig.json (written by LIGHTINGAI_CONFIG in AutoCAD).
    Returns a dict keyed by type letter:
      {"A": {"shape": "Circle", "aci": 6}, "B": {"shape": "Square", "aci": 1}, ...}
    Returns {} if the file does not exist — callers fall back to hardcoded defaults.
    """
    if not TYPE_CONFIG_FILE.exists():
        return {}
    try:
        entries = json.loads(TYPE_CONFIG_FILE.read_text())
        cfg: dict = {}
        for e in entries:
            t = e.get("type", "")
            cfg[t] = {
                "shape":       e.get("shape", DEFAULT_SHAPES.get(t, "Circle")),
                "aci":         COLOR_NAME_ACI.get(e.get("color", "Magenta"), 6),
                "description": e.get("description", ""),
            }
        return cfg
    except Exception as ex:
        print(f"[LightingAI] Warning: Could not read type config: {ex}")
        return {}


def _block_name(t: str, shape: str, aci: int) -> str:
    """
    Build a block name that encodes the configured shape and colour.
    Changing the config → different name → fresh block is always created.
    e.g. MIKA80E-A-CIR-6, MIKA80E-B-SQR-1
    """
    return f"MIKA80E-{t}-{shape[:3].upper()}-{aci}"


def generate_block_defs(types_seen: list[dict], cfg_map: dict = None) -> list[str]:
    """
    Return LISP code that creates the MIKA80E-* block definitions using ENTMAKE.
    One block per unique luminaire type (A–E). Shapes and colors come from
    cfg_map (populated by LIGHTINGAI_CONFIG) or fall back to defaults.

    Block name encodes shape+color so config changes always create fresh blocks.
    """
    if cfg_map is None:
        cfg_map = {}
    lines = []
    lines.append(";; ── Block definitions ───────────────────────────────────────")
    lines.append("(defun lai:make-blocks ()")

    for lp in types_seen:
        t     = lp["lumi_type"]
        cfg   = cfg_map.get(t, {})
        aci   = cfg.get("aci",   TYPE_ACI.get(t, 6))
        shape = cfg.get("shape", DEFAULT_SHAPES.get(t, "Circle"))
        r     = CUTOUT_R
        bn    = _block_name(t, shape, aci)

        lines.append(f'  ;; Type {t}: {lp["description"]}  [{shape} / ACI {aci}]')
        lines.append(f'  (if (not (tblsearch "BLOCK" "{bn}"))')
        lines.append(f'    (progn')
        # Begin block definition
        lines.append(f'      (entmake (list (cons 0 "BLOCK") (cons 2 "{bn}") (cons 10 (list 0 0 0)) (cons 70 0)))')
        import math as _m

        def _blk_line(x1, y1, x2, y2):
            return (f'      (entmake (list (cons 0 "LINE") (cons 8 "0") (cons 62 {aci}) '
                    f'(cons 10 (list {x1:.4f} {y1:.4f} 0)) (cons 11 (list {x2:.4f} {y2:.4f} 0))))')

        def _blk_circle(radius):
            return (f'      (entmake (list (cons 0 "CIRCLE") (cons 8 "0") (cons 62 {aci}) '
                    f'(cons 10 (list 0 0 0)) (cons 40 {radius:.4f})))')

        def _poly_lines(n, radius, start_deg=90):
            pts = [(_m.cos(_m.radians(start_deg + 360/n*i)) * radius,
                    _m.sin(_m.radians(start_deg + 360/n*i)) * radius) for i in range(n)]
            return [_blk_line(pts[i][0], pts[i][1], pts[(i+1)%n][0], pts[(i+1)%n][1])
                    for i in range(n)]

        # Shape-specific geometry (driven by user config or defaults)
        if shape == "Circle":
            lines += [_blk_circle(r), _blk_circle(r*0.30),
                      _blk_line(-r*0.6, 0, r*0.6, 0), _blk_line(0, -r*0.6, 0, r*0.6)]
        elif shape == "Square":
            lines += [_blk_line(-r,-r, r,-r), _blk_line(r,-r, r,r),
                      _blk_line(r,r, -r,r),   _blk_line(-r,r, -r,-r),
                      _blk_circle(r*0.20)]
        elif shape == "Diamond":
            lines += [_blk_line(0,-r, r,0), _blk_line(r,0, 0,r),
                      _blk_line(0,r, -r,0),  _blk_line(-r,0, 0,-r),
                      _blk_circle(r*0.20)]
        elif shape == "Triangle":
            tx1,ty1 = -r*0.866025, r*0.5
            tx2,ty2 =  r*0.866025, r*0.5
            tx3,ty3 =  0.0,       -r
            lines += [_blk_line(tx1,ty1,tx2,ty2), _blk_line(tx2,ty2,tx3,ty3),
                      _blk_line(tx3,ty3,tx1,ty1), _blk_circle(r*0.20)]
        elif shape == "Cross":
            lines += [_blk_circle(r),
                      _blk_line(-r*0.6,-r*0.6, r*0.6,r*0.6),
                      _blk_line(-r*0.6, r*0.6, r*0.6,-r*0.6)]
        elif shape == "Hexagon":
            lines += _poly_lines(6, r, start_deg=90)
        elif shape == "Pentagon":
            lines += _poly_lines(5, r, start_deg=90)
        elif shape == "Octagon":
            lines += _poly_lines(8, r, start_deg=22.5)
        elif shape == "Star":
            star = []
            for i in range(10):
                ang = _m.radians(i * 36 - 90)
                rad = r if i % 2 == 0 else r * 0.42
                star.append((_m.cos(ang)*rad, _m.sin(ang)*rad))
            lines += [_blk_line(star[i][0],star[i][1],
                                star[(i+1)%10][0],star[(i+1)%10][1]) for i in range(10)]
        elif shape == "Plus":
            t = r * 0.28
            plus = [(-t,-r),(t,-r),(t,-t),(r,-t),(r,t),(t,t),
                    (t,r),(-t,r),(-t,t),(-r,t),(-r,-t),(-t,-t)]
            lines += [_blk_line(plus[i][0],plus[i][1],
                                plus[(i+1)%12][0],plus[(i+1)%12][1]) for i in range(12)]
        else:
            lines += [_blk_circle(r), _blk_circle(r*0.35)]
        # End block definition
        lines.append(f'      (entmake (list (cons 0 "ENDBLK") (cons 8 "0")))')
        lines.append(f'    ) ;; end progn')
        lines.append(f'  ) ;; end if')

    lines.append(')')
    lines.append('(lai:make-blocks)')
    lines.append('')
    return lines


def generate_inserts(placed: list[dict], cfg_map: dict = None,
                     pitch: float = 1250.0) -> list[str]:
    """
    Return LISP code that INSERTs every luminaire block.
    Scale is derived from grid pitch so each symbol fills its full grid cell.
    """
    if cfg_map is None:
        cfg_map = {}
    # Scale block (defined at CUTOUT_R*2 diameter) to fill the pitch-sized grid cell
    scale = pitch / (CUTOUT_R * 2)
    lines = []
    lines.append(";; ── Luminaire inserts ────────────────────────────────────────")
    lines.append("(defun lai:place-luminaires ()")
    lines.append('  (setvar "CLAYER" "AI-LUMINAIRES")')

    for lp in placed:
        t     = lp["lumi_type"]
        cfg   = cfg_map.get(t, {})
        aci   = cfg.get("aci",   TYPE_ACI.get(t, 6))
        shape = cfg.get("shape", DEFAULT_SHAPES.get(t, "Circle"))
        bn    = _block_name(t, shape, aci)   # must match generate_block_defs
        rot   = lp.get("rotation", 0.0)
        rot_rad = rot * 3.14159265 / 180.0

        lines.append(
            f'  (entmake (list (cons 0 "INSERT") (cons 2 "{bn}") '
            f'(cons 10 (list {lp["x"]:.4f} {lp["y"]:.4f} 0)) '
            f'(cons 41 {scale:.6f}) (cons 42 {scale:.6f}) (cons 50 {rot_rad:.6f}) '
            f'(cons 8 "AI-LUMINAIRES") (cons 62 {aci})))'
        )

    lines.append(')')
    lines.append('(lai:place-luminaires)')
    lines.append('')
    return lines


def generate_legend(placed: list[dict], cfg_map: dict = None,
                    fp_right_x: float = None,
                    fp_top_y: float = None,
                    floor_area_m2: float = None) -> list[str]:
    """Draw the Leuchtenlegende panel to the right of the drawing.

    Columns: Type | Symbol | Description | Count | Total W
    Footer:  Gesamt row + Fläche / W/m² row
    """
    if cfg_map is None:
        cfg_map = {}
    if not placed:
        return []

    max_x = max(p["x"] for p in placed)
    max_y = max(p["y"] for p in placed)

    # Collect per-type stats
    seen:       dict[str, dict]  = {}
    type_count: dict[str, int]   = {}
    type_watt:  dict[str, float] = {}
    for lp in placed:
        t = lp["lumi_type"]
        seen.setdefault(t, lp)
        type_count[t] = type_count.get(t, 0) + 1
        type_watt[t]  = type_watt.get(t, 0.0) + lp.get("wattage", 0.0)

    total_count = sum(type_count.values())
    total_watt  = sum(type_watt.values())
    watt_per_m2 = (total_watt / floor_area_m2) if floor_area_m2 else None

    _DEFAULT_DESCS = {
        'A': "MIKA80-E K1 Regalbeleuchtung 15W 40° 2400lm 3000K",
        'B': "MIKA80-E K4 Ergänzungsbeleuchtung 20W 60° 3200lm 3000K",
        'C': "MIKA80-E K3 Regalbeleuchtung Rand 15W 40° 2400lm 3000K",
        'D': "MIKA80-E K2 Checkout/Service 20W 40° 3200lm 3000K",
        'E': "NEO85-SX K6 Schaufenster-Strahler 20W 60° 3200lm Track",
    }
    configured   = sorted(cfg_map.keys()) if cfg_map else []
    extra        = [t for t in sorted(seen.keys()) if t not in cfg_map]
    type_letters = configured + extra if configured else sorted(seen.keys())
    types = []
    for t in type_letters:
        if t in seen:
            types.append(seen[t])
        else:
            types.append({"lumi_type": t, "description": _DEFAULT_DESCS.get(t, f"Type {t}")})

    ref_x = fp_right_x if fp_right_x is not None else max_x
    lx    = ref_x + 30_000
    ly    = fp_top_y if fp_top_y is not None else max_y
    rowH  = 16_000
    cr    = 7_000
    th    = 2_500

    # Footer rows: totals + (optionally) floor-area/W-per-m²
    FOOTER_ROWS = 2 if (floor_area_m2 and watt_per_m2 is not None) else 1
    n_data = len(types)
    # Total height covers data rows + footer rows
    W     = 220_000   # widened: description + count + wattage columns all fit
    totH  = rowH * (n_data + FOOTER_ROWS)
    bot_y = ly - totH

    # Column X positions
    c1x    = lx + 13_000   # end of type-letter col
    c2x    = lx + 31_000   # end of symbol col
    c3x    = lx + 170_000  # end of description col
    c4x    = lx + 195_000  # end of count col  → wattage fills c4x→lx+W
    sym_cx = (c1x + c2x) / 2
    desc_x = c2x + 2_000

    lines = []
    lines.append(";; ── Legend ───────────────────────────────────────────────────")
    lines.append("(defun lai:draw-legend ()")
    lines.append(f'  (setvar "CLAYER" "AI-LEGEND")')

    def rect(x, y, w, h):
        pts = [(x, y), (x+w, y), (x+w, y+h), (x, y+h), (x, y)]
        coords = " ".join(f"(list {px:.1f} {py:.1f} 0)" for px,py in pts)
        return f'  (command "_.PLINE" {coords} "")'

    def txt(x, y, h, aci, text, align='L'):
        if align == 'R':
            return (f'  (entmake (list (cons 0 "TEXT") (cons 8 "AI-LEGEND") '
                    f'(cons 62 {aci}) (cons 10 (list {x:.1f} {y:.1f} 0)) '
                    f'(cons 72 2) (cons 11 (list {x:.1f} {y:.1f} 0)) '
                    f'(cons 40 {h:.1f}) (cons 1 "{_esc(text)}")))')
        return (f'  (entmake (list (cons 0 "TEXT") (cons 8 "AI-LEGEND") '
                f'(cons 62 {aci}) (cons 10 (list {x:.1f} {y:.1f} 0)) '
                f'(cons 40 {h:.1f}) (cons 1 "{_esc(text)}")))')

    SYM_LW = 50

    def circle(cx, cy, r, aci):
        return (f'  (entmake (list (cons 0 "CIRCLE") (cons 8 "AI-LEGEND") '
                f'(cons 62 {aci}) (cons 370 {SYM_LW}) '
                f'(cons 10 (list {cx:.1f} {cy:.1f} 0)) (cons 40 {r:.1f})))')

    def hline(x1, y, x2, aci=8):
        return (f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-LEGEND") '
                f'(cons 62 {aci}) (cons 10 (list {x1:.1f} {y:.1f} 0)) '
                f'(cons 11 (list {x2:.1f} {y:.1f} 0))))')

    def seg(x1, y1, x2, y2, aci):
        return (f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-LEGEND") '
                f'(cons 62 {aci}) (cons 370 {SYM_LW}) '
                f'(cons 10 (list {x1:.1f} {y1:.1f} 0)) '
                f'(cons 11 (list {x2:.1f} {y2:.1f} 0))))')

    def vline(x, y1, y2):
        return (f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-LEGEND") '
                f'(cons 62 8) (cons 10 (list {x:.1f} {y1:.1f} 0)) '
                f'(cons 11 (list {x:.1f} {y2:.1f} 0))))')

    import math as _m

    # Outer border (data + footer rows)
    lines.append(rect(lx, bot_y, W, totH))
    # Vertical column dividers (data rows only — stop at footer divider)
    footer_top = ly - rowH * n_data
    lines.append(vline(c1x, footer_top, ly))
    lines.append(vline(c2x, footer_top, ly))
    lines.append(vline(c3x, footer_top, ly))
    lines.append(vline(c4x, footer_top, ly))

    # Data rows — one per light type, stacked downward from ly
    for i, lp in enumerate(types):
        t     = lp["lumi_type"]
        cfg   = cfg_map.get(t, {})
        aci   = cfg.get("aci",   TYPE_ACI.get(t, 6))
        shape = cfg.get("shape", DEFAULT_SHAPES.get(t, "Circle"))
        qty        = type_count.get(t, 0)
        watt_tot   = type_watt.get(t, 0.0)
        user_desc  = cfg.get("description", "").strip()
        desc       = user_desc if user_desc else lp.get("description", f"Type {t}")

        row_top = ly - rowH * i
        cy      = row_top - rowH * 0.5

        if i > 0:
            lines.append(hline(lx, row_top, lx + W))

        # Col 1: type letter
        lines.append(txt(lx + 3_000, cy - th * 0.4, th, aci, t))

        # Col 2: symbol
        cx = sym_cx

        def _lpoly(n, radius, start_deg=90):
            pts = [(cx + _m.cos(_m.radians(start_deg + 360/n*k)) * radius,
                    cy + _m.sin(_m.radians(start_deg + 360/n*k)) * radius) for k in range(n)]
            return [seg(pts[k][0], pts[k][1], pts[(k+1)%n][0], pts[(k+1)%n][1], aci)
                    for k in range(n)]

        if shape == "Circle":
            lines += [circle(cx,cy,cr,aci), circle(cx,cy,cr*0.30,aci),
                      seg(cx-cr*0.6,cy,cx+cr*0.6,cy,aci),
                      seg(cx,cy-cr*0.6,cx,cy+cr*0.6,aci)]
        elif shape == "Square":
            lines += [seg(cx-cr,cy-cr,cx+cr,cy-cr,aci), seg(cx+cr,cy-cr,cx+cr,cy+cr,aci),
                      seg(cx+cr,cy+cr,cx-cr,cy+cr,aci), seg(cx-cr,cy+cr,cx-cr,cy-cr,aci),
                      circle(cx,cy,cr*0.20,aci)]
        elif shape == "Diamond":
            lines += [seg(cx,cy-cr,cx+cr,cy,aci), seg(cx+cr,cy,cx,cy+cr,aci),
                      seg(cx,cy+cr,cx-cr,cy,aci), seg(cx-cr,cy,cx,cy-cr,aci),
                      circle(cx,cy,cr*0.20,aci)]
        elif shape == "Triangle":
            lines += [seg(cx-cr*0.866025,cy+cr*0.5, cx+cr*0.866025,cy+cr*0.5,aci),
                      seg(cx+cr*0.866025,cy+cr*0.5, cx,cy-cr,aci),
                      seg(cx,cy-cr, cx-cr*0.866025,cy+cr*0.5,aci),
                      circle(cx,cy,cr*0.20,aci)]
        elif shape == "Cross":
            lines += [circle(cx,cy,cr,aci),
                      seg(cx-cr*0.6,cy-cr*0.6,cx+cr*0.6,cy+cr*0.6,aci),
                      seg(cx-cr*0.6,cy+cr*0.6,cx+cr*0.6,cy-cr*0.6,aci)]
        elif shape == "Hexagon":
            lines += _lpoly(6, cr, start_deg=90)
        elif shape == "Pentagon":
            lines += _lpoly(5, cr, start_deg=90)
        elif shape == "Octagon":
            lines += _lpoly(8, cr, start_deg=22.5)
        elif shape == "Star":
            star = [(cx + (_m.cos(_m.radians(k*36-90)) * (cr if k%2==0 else cr*0.42)),
                     cy + (_m.sin(_m.radians(k*36-90)) * (cr if k%2==0 else cr*0.42)))
                    for k in range(10)]
            lines += [seg(star[k][0],star[k][1],star[(k+1)%10][0],star[(k+1)%10][1],aci)
                      for k in range(10)]
        elif shape == "Plus":
            tp = cr * 0.28
            plus = [(cx-tp,cy-cr),(cx+tp,cy-cr),(cx+tp,cy-tp),(cx+cr,cy-tp),
                    (cx+cr,cy+tp),(cx+tp,cy+tp),(cx+tp,cy+cr),(cx-tp,cy+cr),
                    (cx-tp,cy+tp),(cx-cr,cy+tp),(cx-cr,cy-tp),(cx-tp,cy-tp)]
            lines += [seg(plus[k][0],plus[k][1],plus[(k+1)%12][0],plus[(k+1)%12][1],aci)
                      for k in range(12)]
        else:
            lines += [circle(cx,cy,cr,aci), circle(cx,cy,cr*0.35,aci)]

        # Col 3: description
        lines.append(txt(desc_x, cy - th * 0.4, th * 0.85, 7, _esc(desc)))

        # Col 4: count (right-aligned)
        lines.append(txt(c4x - 2_000, cy - th * 0.4, th, aci, str(qty), align='R'))

        # Col 5: total wattage (right-aligned)
        lines.append(txt(lx + W - 2_000, cy - th * 0.4, th, 7,
                         f"{watt_tot:.0f} W", align='R'))

    # ── Footer: totals row ────────────────────────────────────────────────────
    lines.append(hline(lx, footer_top, lx + W))
    f1_cy = footer_top - rowH * 0.5 - th * 0.4
    lines.append(txt(lx + 3_000, f1_cy, th, 7,
                     f"Gesamt / Total:  {total_count} Leuchten"))
    lines.append(txt(lx + W - 2_000, f1_cy, th, 7,
                     f"{total_watt:.0f} W", align='R'))

    # ── Footer: floor area / W per m² ────────────────────────────────────────
    if FOOTER_ROWS == 2 and watt_per_m2 is not None:
        f2_top = footer_top - rowH
        lines.append(hline(lx, f2_top, lx + W))
        f2_cy  = f2_top - rowH * 0.5 - th * 0.4
        lines.append(txt(lx + 3_000, f2_cy, th * 0.82, 8,
                         f"Fläche / Floor area: {floor_area_m2:.0f} m²"
                         f"     Leistungsdichte / Lighting load: {watt_per_m2:.2f} W/m²"))

    lines.append(')')
    lines.append('(lai:draw-legend)')
    lines.append('')
    return lines


def _generate_schedule_REMOVED(placed: list[dict], cfg_map: dict = None,
                      fp_right_x: float = None, legend_bot_y: float = None,
                      floor_area_m2: float = None) -> list[str]:
    """Draw a luminaire statistics table on the drawing, below the legend."""
    if not placed:
        return []
    if cfg_map is None:
        cfg_map = {}

    # ── Compute stats ────────────────────────────────────────────────────────
    type_count: dict[str, int]   = {}
    type_watt:  dict[str, float] = {}
    for lp in placed:
        t = lp["lumi_type"]
        type_count[t] = type_count.get(t, 0) + 1
        type_watt[t]  = type_watt.get(t, 0.0) + lp.get("wattage", 0.0)

    _DEFAULT_DESCS = {
        'A': "MIKA80-E K1 Regalbeleuchtung 15W 40°",
        'B': "MIKA80-E K4 Ergänzungsbeleuchtung 20W 60°",
        'C': "MIKA80-E K3 Regalbeleuchtung Rand 15W 40°",
        'D': "MIKA80-E K2 Checkout/Service 20W 40°",
        'E': "NEO85-SX K6 Schaufenster-Strahler 20W",
    }
    cfg_letters = sorted(cfg_map.keys())
    extra = [t for t in sorted(type_count.keys()) if t not in cfg_map]
    type_letters = cfg_letters + extra if cfg_letters else sorted(type_count.keys())

    total_count = sum(type_count.values())
    total_watt  = sum(type_watt.values())
    watt_per_m2 = (total_watt / floor_area_m2) if floor_area_m2 else None

    # ── Layout ───────────────────────────────────────────────────────────────
    max_x = max(p["x"] for p in placed)
    ref_x = fp_right_x if fp_right_x is not None else max_x
    sx    = ref_x + 30_000        # same left edge as legend
    W     = 180_000
    rowH  = 16_000
    th    = 2_500
    gap   = 20_000                # gap below legend

    sy = (legend_bot_y - gap) if legend_bot_y is not None else 0.0

    n_data = len(type_letters)
    total_rows = n_data + 3       # header + sub-header + data rows + footer row
    bot_y = sy - rowH * total_rows

    # Column X positions
    c1x  = sx + 14_000   # end of "Typ" col
    c2x  = sx + 150_000  # end of "Beschreibung" col
    c3x  = sx + 168_000  # end of "Anz" col
    # Col 4 (Watt) runs to sx+W

    def rect_s(x, y, w, h):
        pts = [(x,y),(x+w,y),(x+w,y+h),(x,y+h),(x,y)]
        coords = " ".join(f"(list {px:.1f} {py:.1f} 0)" for px,py in pts)
        return f'  (command "_.PLINE" {coords} "")'

    def hln(x1, y, x2):
        return (f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-SCHEDULE") '
                f'(cons 62 8) (cons 10 (list {x1:.1f} {y:.1f} 0)) '
                f'(cons 11 (list {x2:.1f} {y:.1f} 0))))')

    def vln(x, y1, y2):
        return (f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-SCHEDULE") '
                f'(cons 62 8) (cons 10 (list {x:.1f} {y1:.1f} 0)) '
                f'(cons 11 (list {x:.1f} {y2:.1f} 0))))')

    def txt_s(x, y, h, aci, text, align='L'):
        if align == 'R':
            return (f'  (entmake (list (cons 0 "TEXT") (cons 8 "AI-SCHEDULE") '
                    f'(cons 62 {aci}) (cons 10 (list {x:.1f} {y:.1f} 0)) '
                    f'(cons 72 2) (cons 11 (list {x:.1f} {y:.1f} 0)) '
                    f'(cons 40 {h:.1f}) (cons 1 "{_esc(text)}")))')
        return (f'  (entmake (list (cons 0 "TEXT") (cons 8 "AI-SCHEDULE") '
                f'(cons 62 {aci}) (cons 10 (list {x:.1f} {y:.1f} 0)) '
                f'(cons 40 {h:.1f}) (cons 1 "{_esc(text)}")))')

    lines = []
    lines.append(";; ── Luminaire Schedule ───────────────────────────────────────")
    lines.append("(defun lai:draw-schedule ()")
    lines.append(f'  (setvar "CLAYER" "AI-SCHEDULE")')

    # Outer border
    lines.append(rect_s(sx, bot_y, W, rowH * total_rows))

    # Vertical column dividers
    lines.append(vln(c1x, bot_y, sy))
    lines.append(vln(c2x, bot_y, sy))
    lines.append(vln(c3x, bot_y, sy))

    # ── Row 0: Title ─────────────────────────────────────────────────────────
    row0_bot = sy - rowH
    lines.append(hln(sx, row0_bot, sx + W))
    title_y = sy - rowH * 0.5 - th * 0.4
    lines.append(txt_s(sx + 3_000, title_y, th * 1.1, 7, "LEUCHTENSTATISTIK  /  LUMINAIRE SCHEDULE"))

    # ── Row 1: Sub-header ────────────────────────────────────────────────────
    row1_bot = sy - rowH * 2
    lines.append(hln(sx, row1_bot, sx + W))
    hdr_y = sy - rowH * 1.5 - th * 0.4
    lines.append(txt_s(sx + 3_000,     hdr_y, th * 0.85, 8, "Typ"))
    lines.append(txt_s(c1x + 3_000,    hdr_y, th * 0.85, 8, "Beschreibung"))
    lines.append(txt_s(c3x - 2_000,    hdr_y, th * 0.85, 8, "Anz", align='R'))
    lines.append(txt_s(sx + W - 2_000, hdr_y, th * 0.85, 8, "W", align='R'))

    # ── Data rows ────────────────────────────────────────────────────────────
    for i, t in enumerate(type_letters):
        cfg      = cfg_map.get(t, {})
        aci      = cfg.get("aci", 7)
        desc     = cfg.get("description", "").strip() or _DEFAULT_DESCS.get(t, f"Type {t}")
        # Truncate long descriptions to fit the column
        if len(desc) > 52:
            desc = desc[:50] + "…"
        count    = type_count.get(t, 0)
        watt_tot = type_watt.get(t, 0.0)

        row_top = sy - rowH * (2 + i)
        row_cy  = row_top - rowH * 0.5 - th * 0.4
        if i > 0:
            lines.append(hln(sx, row_top, sx + W))

        lines.append(txt_s(sx + 3_000,     row_cy, th, aci, t))
        lines.append(txt_s(c1x + 3_000,    row_cy, th * 0.85, 7, desc))
        lines.append(txt_s(c3x - 2_000,    row_cy, th, 7, str(count), align='R'))
        lines.append(txt_s(sx + W - 2_000, row_cy, th, 7, f"{watt_tot:.0f}", align='R'))

    # ── Footer rows ──────────────────────────────────────────────────────────
    footer_top = sy - rowH * (2 + n_data)
    lines.append(hln(sx, footer_top, sx + W))
    f1_cy = footer_top - rowH * 0.5 - th * 0.4
    lines.append(txt_s(sx + 3_000,     f1_cy, th, 7,
                        f"Gesamt / Total:  {total_count} Leuchten"))
    lines.append(txt_s(sx + W - 2_000, f1_cy, th, 7,
                        f"{total_watt:.0f} W", align='R'))

    f2_bot = footer_top - rowH
    lines.append(hln(sx, f2_bot, sx + W))
    f2_cy = f2_bot - rowH * 0.5 - th * 0.4 + rowH   # row below total
    if floor_area_m2 and watt_per_m2 is not None:
        lines.append(txt_s(sx + 3_000, f2_cy - rowH, th * 0.85, 8,
                            f"Fläche / Floor area: {floor_area_m2:.0f} m²"
                            f"     Leistungsdichte / Lighting load: {watt_per_m2:.2f} W/m²"))

    lines.append(')')
    lines.append('(lai:draw-schedule)')
    lines.append('')
    return lines


def generate_title_block(placed: list[dict],
                         project: str, customer: str, concept: str) -> list[str]:
    """Draw the Schriftfeld (title block) below the drawing."""
    if not placed:
        return []

    import datetime
    min_x = min(p["x"] for p in placed)
    min_y = min(p["y"] for p in placed)
    total_lumi    = len(placed)
    total_wattage = sum(p["wattage"] for p in placed)

    tx = min_x
    ty = min_y - 80_000
    W  = 180_000
    H  = 60_000
    pad= 2_500
    lh = 2_000
    th = 3_000
    c1 = tx + W * 0.38
    c2 = tx + W * 0.65
    now = datetime.datetime.now().strftime("%d.%m.%Y")

    lines = []
    lines.append(";; ── Title block ──────────────────────────────────────────────")
    lines.append("(defun lai:draw-titleblock ()")
    lines.append(f'  (setvar "CLAYER" "AI-TITLEBLOCK")')

    def rect(x, y, w, h):
        pts = [(x, y), (x+w, y), (x+w, y+h), (x, y+h), (x, y)]
        coords = " ".join(f"(list {px:.1f} {py:.1f} 0)" for px,py in pts)
        return f'  (command "_.PLINE" {coords} "")'

    def hline(x1, y, x2):
        return (f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-TITLEBLOCK") '
                f'(cons 62 8) (cons 10 (list {x1:.1f} {y:.1f} 0)) '
                f'(cons 11 (list {x2:.1f} {y:.1f} 0))))')

    def vline(x, y1, y2):
        return (f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-TITLEBLOCK") '
                f'(cons 62 8) (cons 10 (list {x:.1f} {y1:.1f} 0)) '
                f'(cons 11 (list {x:.1f} {y2:.1f} 0))))')

    def txt(x, y, h, aci, text):
        return (f'  (entmake (list (cons 0 "TEXT") (cons 8 "AI-TITLEBLOCK") '
                f'(cons 62 {aci}) (cons 10 (list {x:.1f} {y:.1f} 0)) '
                f'(cons 40 {h:.1f}) (cons 1 "{_esc(text)}")))')

    lines.append(rect(tx, ty, W, H))
    lines.append(vline(c1, ty, ty+H))
    lines.append(vline(c2, ty, ty+H))
    lines.append(hline(tx, ty+H*0.60, tx+W))
    lines.append(hline(tx, ty+H*0.35, tx+W))
    lines.append(hline(tx, ty+H*0.15, tx+W))

    # Row 1: company
    lines.append(txt(tx+pad, ty+H*0.69, th*1.4, 6, "MAX FRANKE.led"))
    lines.append(txt(tx+pad, ty+H*0.64, lh,     7, "Osdorfer Landstrasse 174-176  D-22549 Hamburg"))

    # Row 2: project
    lines.append(txt(tx+pad,   ty+H*0.53, lh, 8, "Projekt:"))
    lines.append(txt(tx+pad,   ty+H*0.40, th, 7, _esc(project)))
    lines.append(txt(c1+pad,   ty+H*0.53, lh, 8, "Bauherr:"))
    lines.append(txt(c1+pad,   ty+H*0.40, th, 7, _esc(customer)))
    lines.append(txt(c2+pad,   ty+H*0.53, lh, 8, "Planinhalt:"))
    lines.append(txt(c2+pad,   ty+H*0.40, th, 7, f"Deckenrasterplan - {concept}"))

    # Row 3: scale / date / summary
    lines.append(txt(tx+pad,   ty+H*0.27, lh, 8, "Massatab:"))
    lines.append(txt(tx+pad,   ty+H*0.17, th, 7, "1:75"))
    lines.append(txt(c1+pad,   ty+H*0.27, lh, 8, "Datum:"))
    lines.append(txt(c1+pad,   ty+H*0.17, th, 7, now))
    lines.append(txt(c2+pad,   ty+H*0.27, lh, 8, "Leuchten gesamt:"))
    lines.append(txt(c2+pad,   ty+H*0.17, th, 7, f"{total_lumi} Stk  {total_wattage:.0f} W"))

    # Row 4: warning
    lines.append(txt(tx+pad, ty+pad, lh*0.9, 8,
        "Achtung: Alle Masse am Bau zu pruefen! / All dimensions to be checked locally!"))

    lines.append(')')
    lines.append('(lai:draw-titleblock)')
    lines.append('')
    return lines


def write_commands_lsp(out_path: Path, result: dict,
                       project: str, customer: str, concept: str) -> None:
    placed = result.get("placed", [])

    # Load user-defined type configuration (written by LIGHTINGAI_CONFIG in AutoCAD)
    cfg_map = load_type_config()
    if cfg_map:
        print(f"[LightingAI] Type config loaded: "
              + ", ".join(f"{t}={v['shape']}/{list(COLOR_NAME_ACI.keys())[list(COLOR_NAME_ACI.values()).index(v['aci'])]}"
                          for t, v in sorted(cfg_map.items())))
    else:
        print("[LightingAI] No type config file found — using defaults. "
              "(Run LIGHTINGAI_CONFIG in AutoCAD to customise.)")

    # Read grid pitch and floor-plan bounds from origin file.
    # fp_xmax/ymax are the bounding box of the store LWPOLYLINE (from LIGHTINGAI_GRID).
    pitch = 1250.0
    grid_fp_xmin: float | None = None
    grid_fp_ymin: float | None = None
    grid_fp_xmax: float | None = None
    grid_fp_ymax: float | None = None
    if ORIGIN_FILE.exists():
        try:
            _orig = json.loads(ORIGIN_FILE.read_text())
            pitch = float(_orig.get("pitch", 1250))
            _x1 = _orig.get("fp_xmin", 0); _y1 = _orig.get("fp_ymin", 0)
            _x2 = _orig.get("fp_xmax", 0); _y2 = _orig.get("fp_ymax", 0)
            if _x2: grid_fp_xmax = float(_x2)
            if _y2: grid_fp_ymax = float(_y2)
            if _x1: grid_fp_xmin = float(_x1)
            if _y1: grid_fp_ymin = float(_y1)
        except Exception:
            pass

    # Floor area from the detected store LWPOLYLINE bounding box
    floor_area_m2: float | None = None
    if (grid_fp_xmax and grid_fp_xmin is not None and
            grid_fp_ymax and grid_fp_ymin is not None):
        try:
            w_m = abs(grid_fp_xmax - grid_fp_xmin) / 1000.0
            h_m = abs(grid_fp_ymax - grid_fp_ymin) / 1000.0
            floor_area_m2 = round(w_m * h_m, 1)
        except Exception:
            pass

    # Collect unique types for block definition generation
    seen_types: dict[str, dict] = {}
    for lp in placed:
        seen_types.setdefault(lp["lumi_type"], lp)
    unique_types = list(seen_types.values())

    # Write canonical descriptions to suggestions file — one distinct product per slot
    # so the GUI always has 5 non-duplicate options regardless of floor plan.
    _canonical_descs = {
        'A': "MIKA80-E K1 Regalbeleuchtung 15W 40° 2400lm 3000K",
        'B': "MIKA80-E K4 Ergänzungsbeleuchtung 20W 60° 3200lm 3000K",
        'C': "MIKA80-E K3 Regalbeleuchtung Rand 15W 40° 2400lm 3000K",
        'D': "MIKA80-E K2 Checkout/Service 20W 40° 3200lm 3000K",
        'E': "NEO85-SX K6 Schaufenster-Strahler 20W 60° 3200lm Track",
    }
    suggestions_file = _AI_DIR / "lightingai_suggestions.json"
    try:
        suggestions_file.write_text(json.dumps(_canonical_descs), encoding="utf-8")
    except Exception:
        pass

    sections: list[str] = []
    sections.append(f";; Generated by LightingAI bridge — {len(placed)} luminaires")
    sections.append(f";; Project: {project}  Customer: {customer}")
    sections.append(f";; Total wattage: {sum(p['wattage'] for p in placed):.0f} W")
    sections.append("")

    # Legend X/Y positioning strategy (most-reliable first):
    #   1. grid_fp_xmax / grid_fp_ymax  — bounds of the store LWPOLYLINE detected
    #      by LIGHTINGAI_GRID (written to origin.json).  Always the actual store
    #      footprint regardless of title blocks or other DWG artifacts.
    #   2. Zone bounds  — classified zones from the AI pipeline (fallback when
    #      origin.json was not written by LIGHTINGAI_GRID, e.g. LIGHTINGAI_SETUP).
    #   3. max(placed[x/y])  — absolute last resort; can be corrupted by artifacts.
    zones = result.get("zones", [])
    zone_right_x: float | None = None
    zone_top_y:   float | None = None
    if zones:
        try:
            zone_right_x = max(z["bounds"][2] for z in zones
                               if z.get("bounds") and len(z["bounds"]) >= 3)
            zone_top_y   = max(z["bounds"][3] for z in zones
                               if z.get("bounds") and len(z["bounds"]) >= 4)
        except Exception:
            pass

    fp_right_x = grid_fp_xmax if grid_fp_xmax is not None else zone_right_x
    fp_top_y   = grid_fp_ymax if grid_fp_ymax is not None else zone_top_y

    sections += generate_block_defs(unique_types, cfg_map)
    sections += generate_inserts(placed, cfg_map, pitch=pitch)
    sections += generate_legend(placed, cfg_map,
                                fp_right_x=fp_right_x,
                                fp_top_y=fp_top_y,
                                floor_area_m2=floor_area_m2)

    content = "\n".join(sections)
    out_path.write_text(content, encoding="utf-8")
    print(f"  Commands file written: {out_path}  ({len(placed)} luminaires)")

    # ── Write summary JSON so the GUI panel can display live stats ─────────────
    try:
        type_count: dict[str, int]   = {}
        type_watt:  dict[str, float] = {}
        for lp in placed:
            t = lp["lumi_type"]
            type_count[t] = type_count.get(t, 0) + 1
            type_watt[t]  = type_watt.get(t, 0.0) + lp.get("wattage", 0.0)

        _DEFAULT_DESCS_S = {
            'A': "MIKA80-E K1 Regalbeleuchtung 15W 40° 2400lm 3000K",
            'B': "MIKA80-E K4 Ergänzungsbeleuchtung 20W 60° 3200lm 3000K",
            'C': "MIKA80-E K3 Regalbeleuchtung Rand 15W 40° 2400lm 3000K",
            'D': "MIKA80-E K2 Checkout/Service 20W 40° 3200lm 3000K",
            'E': "NEO85-SX K6 Schaufenster-Strahler 20W 60° 3200lm Track",
        }
        total_count = sum(type_count.values())
        total_watt  = sum(type_watt.values())
        watt_per_m2 = round(total_watt / floor_area_m2, 2) if floor_area_m2 else None
        by_type = []
        for t in (sorted(cfg_map.keys()) if cfg_map else sorted(type_count.keys())):
            cfg_t = (cfg_map or {}).get(t, {})
            by_type.append({
                "type":        t,
                "description": cfg_t.get("description", "").strip() or _DEFAULT_DESCS_S.get(t, f"Type {t}"),
                "count":       type_count.get(t, 0),
                "watt_total":  round(type_watt.get(t, 0.0), 1),
            })

        summary = {
            "total_count":   total_count,
            "total_watt":    round(total_watt, 1),
            "floor_area_m2": floor_area_m2,
            "watt_per_m2":   watt_per_m2,
            "by_type":       by_type,
        }
        summary_file = out_path.parent / "lightingai_summary.json"
        summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"  Summary written: {summary_file}")
    except Exception as _e:
        print(f"  [warn] Could not write summary: {_e}")

    # Also write to /tmp/ so AutoCAD sessions that loaded the plugin before
    # the path change was made can still find the file without a LISP reload.
    _tmp_copy = Path("/tmp/lightingai_commands.lsp")
    try:
        _tmp_copy.write_text(content, encoding="utf-8")
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    out  = Path(args.out)

    if args.regenerate:
        ok = regenerate_from_db(out, args.project, args.customer, args.concept)
        sys.exit(0 if ok else 1)

    if not args.dwg:
        sys.exit("ERROR: dwg path is required (or use --regenerate to rebuild from last job)")
    dwg = Path(args.dwg).expanduser().resolve()

    if not dwg.exists():
        sys.exit(f"ERROR: File not found: {dwg}")

    # ── 1. Health check ───────────────────────────────────────────────────────
    print(f"\n[LightingAI] Connecting to {args.api} …")
    if not check_health(args.api):
        sys.exit(
            f"ERROR: Cannot reach backend at {args.api}\n"
            f"  Make sure the Python API is running:\n"
            f"  cd ~/ai-lighting && uvicorn services.api.main:app --port 8000"
        )
    print("[LightingAI] Backend online.")

    # ── 2. Read grid origin (optional — pipeline auto-detects if not set) ─────
    origin_info = ""
    if ORIGIN_FILE.exists():
        try:
            origin = json.loads(ORIGIN_FILE.read_text())
            origin_info = (f"  Grid origin: X={origin['x']:.0f}  "
                           f"Y={origin['y']:.0f}  pitch={origin['pitch']} mm")
            print(f"[LightingAI] {origin_info}")
        except Exception:
            pass
    else:
        print("[LightingAI] No grid origin file found — pipeline will auto-detect.")
        print("             (For better accuracy: run LIGHTINGAI_SETUP in AutoCAD first)")

    # ── 3. Upload ─────────────────────────────────────────────────────────────
    print(f"[LightingAI] Uploading {dwg.name} ({dwg.stat().st_size // 1024} KB)…")
    job_id = submit_plan(args.api, dwg, args.project, args.customer, args.concept)
    print(f"[LightingAI] Job {job_id} queued — polling…")

    # ── 4. Poll ───────────────────────────────────────────────────────────────
    job = poll_job(args.api, job_id)
    if job["status"] == "error":
        sys.exit(f"\nERROR: Pipeline failed: {job['message']}")

    result = job["result"]
    print(f"\n[LightingAI] Pipeline complete:")
    print(f"  Total luminaires : {result['total_luminaires']}")
    print(f"  Total wattage    : {result['total_wattage']:.0f} W")
    print(f"  Type A/B/C/D/E   : "
          f"{result['type_A']}/{result['type_B']}/"
          f"{result['type_C']}/{result['type_D']}/{result['type_E']}")

    # ── 5. Write LISP commands file ───────────────────────────────────────────
    print(f"\n[LightingAI] Writing AutoCAD commands…")
    write_commands_lsp(out, result, args.project, args.customer, args.concept)

    print(f"\n[LightingAI] ✓  All done!")
    print(f"[LightingAI]    Now go to AutoCAD and type:  LIGHTINGAI_PLACE")
    print(f"[LightingAI]    The luminaires will appear in your drawing.\n")


if __name__ == "__main__":
    main()
