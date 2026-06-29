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
    Re-generate lightingai_commands.lsp from the most recent job stored in jobs.db.
    Called automatically by the GUI after the user saves their type/description config
    so that LIGHTINGAI_PLACE immediately reflects the new descriptions without a
    full re-upload of the DWG.
    Returns True on success.
    """
    import sqlite3
    db_path = _AI_DIR / "data" / "jobs.db"
    # Also check next to the script for dev layouts
    if not db_path.exists():
        db_path = Path(__file__).parent.parent.parent / "data" / "jobs.db"
    if not db_path.exists():
        print("[LightingAI] Regenerate: jobs.db not found — run bridge with DWG first.")
        return False
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
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
    print(f"[LightingAI] Regenerating commands for {len(placed)} luminaires "
          f"(from stored job, no upload needed)…")
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


def generate_legend(placed: list[dict], cfg_map: dict = None) -> list[str]:
    """Draw the Leuchtenlegende panel to the right of the drawing."""
    if cfg_map is None:
        cfg_map = {}
    if not placed:
        return []

    max_x = max(p["x"] for p in placed)
    max_y = max(p["y"] for p in placed)

    # Collect one representative per type, preserve order A→E
    seen: dict[str, dict] = {}
    type_count: dict[str, int] = {}
    for lp in placed:
        t = lp["lumi_type"]
        seen.setdefault(t, lp)
        type_count[t] = type_count.get(t, 0) + 1
    types = [seen[t] for t in sorted(seen.keys())]

    lx   = max_x + 12_000
    ly   = max_y
    W    = 110_000
    rowH = 16_000   # 2× original — icons need room
    cr   = 7_000    # icon radius: 2× original 3520
    pad  = 2_000
    th   = 2_500

    lines = []
    lines.append(";; ── Legend ───────────────────────────────────────────────────")
    lines.append("(defun lai:draw-legend ()")
    lines.append(f'  (setvar "CLAYER" "AI-LEGEND")')

    def rect(x, y, w, h):
        pts = [(x, y), (x+w, y), (x+w, y+h), (x, y+h), (x, y)]
        coords = " ".join(f"(list {px:.1f} {py:.1f} 0)" for px,py in pts)
        return (
            f'  (command "_.PLINE" {coords} "")'
        )

    def txt(x, y, h, aci, text):
        return (
            f'  (entmake (list (cons 0 "TEXT") (cons 8 "AI-LEGEND") '
            f'(cons 62 {aci}) (cons 10 (list {x:.1f} {y:.1f} 0)) '
            f'(cons 40 {h:.1f}) (cons 1 "{_esc(text)}")))'
        )

    # Legend symbol lineweight: 50 = 0.50 mm (DXF group 370).
    # Thick lines make outline symbols look as substantial as the filled text beside them.
    SYM_LW = 50

    def circle(cx, cy, r, aci):
        return (
            f'  (entmake (list (cons 0 "CIRCLE") (cons 8 "AI-LEGEND") '
            f'(cons 62 {aci}) (cons 370 {SYM_LW}) '
            f'(cons 10 (list {cx:.1f} {cy:.1f} 0)) (cons 40 {r:.1f})))'
        )

    def hline(x1, y, x2, aci=8):
        return (
            f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-LEGEND") '
            f'(cons 62 {aci}) (cons 10 (list {x1:.1f} {y:.1f} 0)) '
            f'(cons 11 (list {x2:.1f} {y:.1f} 0))))'
        )

    def seg(x1, y1, x2, y2, aci):
        return (
            f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-LEGEND") '
            f'(cons 62 {aci}) (cons 370 {SYM_LW}) '
            f'(cons 10 (list {x1:.1f} {y1:.1f} 0)) '
            f'(cons 11 (list {x2:.1f} {y2:.1f} 0))))'
        )

    import math as _m

    # Column layout
    W       = 140_000
    n_rows  = len(types)
    totH    = rowH * n_rows
    bot_y   = ly - totH

    # Column divider X positions
    c1x     = lx + 13_000   # end of col 1 (type letter)
    c2x     = lx + 31_000   # end of col 2 (symbol)
    c3x     = lx + 120_000  # end of col 3 (description)
    sym_cx  = (c1x + c2x) / 2
    desc_x  = c2x + 2_000
    count_x = c3x + 2_000

    def vline(x, y1, y2):
        return (f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-LEGEND") '
                f'(cons 62 8) (cons 10 (list {x:.1f} {y1:.1f} 0)) '
                f'(cons 11 (list {x:.1f} {y2:.1f} 0))))')

    # Outer border
    lines.append(rect(lx, bot_y, W, totH))
    # Vertical column dividers (full height)
    lines.append(vline(c1x, bot_y, ly))
    lines.append(vline(c2x, bot_y, ly))
    lines.append(vline(c3x, bot_y, ly))

    # Data rows — one per light type, stacked downward from ly
    for i, lp in enumerate(types):
        t     = lp["lumi_type"]
        cfg   = cfg_map.get(t, {})
        aci   = cfg.get("aci",   TYPE_ACI.get(t, 6))
        shape = cfg.get("shape", DEFAULT_SHAPES.get(t, "Circle"))
        qty        = type_count.get(t, 0)
        user_desc  = cfg.get("description", "").strip()
        desc       = user_desc if user_desc else lp.get("description", f"Type {t}")

        row_top = ly - rowH * i
        cy      = row_top - rowH * 0.5   # vertical center of this row

        # Horizontal row separator (below this row)
        if i > 0:
            lines.append(hline(lx, row_top, lx + W))

        # Col 1: type letter, centered in col 1
        lines.append(txt(lx + 3_000, cy - th * 0.4, th, aci, t))

        # Col 2: symbol — cx/cy already set above
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

        # Col 4: count
        lines.append(txt(count_x, cy - th * 0.4, th, aci, str(qty)))

    lines.append(')')
    lines.append('(lai:draw-legend)')
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

    # Read grid pitch from origin file so symbol scale matches the grid cell size
    pitch = 1250.0
    if ORIGIN_FILE.exists():
        try:
            pitch = float(json.loads(ORIGIN_FILE.read_text()).get("pitch", 1250))
        except Exception:
            pass

    # Collect unique types for block definition generation
    seen_types: dict[str, dict] = {}
    for lp in placed:
        seen_types.setdefault(lp["lumi_type"], lp)
    unique_types = list(seen_types.values())

    # Save discovered descriptions as suggestions for the GUI combobox
    suggestions = {lp["lumi_type"]: lp.get("description", "") for lp in unique_types}
    suggestions_file = _AI_DIR / "lightingai_suggestions.json"
    try:
        suggestions_file.write_text(json.dumps(suggestions), encoding="utf-8")
    except Exception:
        pass

    sections: list[str] = []
    sections.append(f";; Generated by LightingAI bridge — {len(placed)} luminaires")
    sections.append(f";; Project: {project}  Customer: {customer}")
    sections.append(f";; Total wattage: {sum(p['wattage'] for p in placed):.0f} W")
    sections.append("")

    sections += generate_block_defs(unique_types, cfg_map)
    sections += generate_inserts(placed, cfg_map, pitch=pitch)
    sections += generate_legend(placed, cfg_map)

    content = "\n".join(sections)
    out_path.write_text(content, encoding="utf-8")
    print(f"  Commands file written: {out_path}  ({len(placed)} luminaires)")

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
