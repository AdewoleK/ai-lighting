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

ORIGIN_FILE   = Path("/tmp/lightingai_origin.json")
COMMANDS_FILE = Path("/tmp/lightingai_commands.lsp")

TYPE_ACI = {          # AutoCAD Color Index per luminaire type
    "A": 6,           # magenta
    "B": 1,           # red
    "C": 4,           # cyan
    "D": 2,           # yellow
    "E": 5,           # blue
}
CUTOUT_R  = 64.0      # half of 128 mm cutout diameter
OUTER_R   = 70.0      # outer circle for legend symbols


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="LightingAI Mac bridge")
    p.add_argument("dwg",          help="Path to the floor plan DWG file")
    p.add_argument("--api",        default="http://localhost:8000")
    p.add_argument("--project",    default="Rossmann EG")
    p.add_argument("--customer",   default="Dirk Rossmann GmbH")
    p.add_argument("--concept",    default="rossmann_standard")
    p.add_argument("--out",        default=str(COMMANDS_FILE))
    return p.parse_args()


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


def generate_block_defs(types_seen: list[dict]) -> list[str]:
    """
    Return LISP code that creates the MIKA80E-* block definitions using ENTMAKE.
    One block per unique luminaire type (A–E).
    """
    lines = []
    lines.append(";; ── Block definitions ───────────────────────────────────────")
    lines.append("(defun lai:make-blocks ()")

    for lp in types_seen:
        t   = lp["lumi_type"]
        aci = TYPE_ACI.get(t, 6)
        r   = CUTOUT_R
        bn  = f"MIKA80E-{t}"

        lines.append(f'  ;; Type {t}: {lp["description"]}')
        lines.append(f'  (if (not (tblsearch "BLOCK" "{bn}"))')
        lines.append(f'    (progn')
        # Begin block definition
        lines.append(f'      (entmake (list (cons 0 "BLOCK") (cons 2 "{bn}") (cons 10 (list 0 0 0)) (cons 70 0)))')
        # Outer circle
        lines.append(f'      (entmake (list (cons 0 "CIRCLE") (cons 8 "0") (cons 62 {aci}) (cons 10 (list 0 0 0)) (cons 40 {r:.4f})))')
        # Inner dot
        lines.append(f'      (entmake (list (cons 0 "CIRCLE") (cons 8 "0") (cons 62 {aci}) (cons 10 (list 0 0 0)) (cons 40 {r*0.35:.4f})))')
        # Horizontal cross-hair line
        lines.append(f'      (entmake (list (cons 0 "LINE") (cons 8 "0") (cons 62 {aci}) (cons 10 (list {-r*0.5:.4f} 0 0)) (cons 11 (list {r*0.5:.4f} 0 0))))')
        # Vertical cross-hair line
        lines.append(f'      (entmake (list (cons 0 "LINE") (cons 8 "0") (cons 62 {aci}) (cons 10 (list 0 {-r*0.5:.4f} 0)) (cons 11 (list 0 {r*0.5:.4f} 0))))')
        # End block definition
        lines.append(f'      (entmake (list (cons 0 "ENDBLK") (cons 8 "0")))')
        lines.append(f'    ) ;; end progn')
        lines.append(f'  ) ;; end if')

    lines.append(')')
    lines.append('(lai:make-blocks)')
    lines.append('')
    return lines


def generate_inserts(placed: list[dict]) -> list[str]:
    """
    Return LISP code that INSERTs every luminaire block.
    Uses ENTMAKE directly (faster than calling INSERT command for 100+ lights).
    """
    lines = []
    lines.append(";; ── Luminaire inserts ────────────────────────────────────────")
    lines.append("(defun lai:place-luminaires ()")
    lines.append('  (setvar "CLAYER" "AI-LUMINAIRES")')

    for lp in placed:
        t   = lp["lumi_type"]
        aci = TYPE_ACI.get(t, 6)
        bn  = f"MIKA80E-{t}"
        rot = lp.get("rotation", 0.0)
        rot_rad = rot * 3.14159265 / 180.0

        lines.append(
            f'  (entmake (list (cons 0 "INSERT") (cons 2 "{bn}") '
            f'(cons 10 (list {lp["x"]:.4f} {lp["y"]:.4f} 0)) '
            f'(cons 41 1.0) (cons 42 1.0) (cons 50 {rot_rad:.6f}) '
            f'(cons 8 "AI-LUMINAIRES") (cons 62 {aci})))'
        )

    lines.append(')')
    lines.append('(lai:place-luminaires)')
    lines.append('')
    return lines


def generate_legend(placed: list[dict]) -> list[str]:
    """Draw the Leuchtenlegende panel to the right of the drawing."""
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
    rowH = 8_000
    pad  = 2_000
    th   = 2_500
    totH = rowH * (len(types) + 2) + 4_000

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

    def circle(cx, cy, r, aci):
        return (
            f'  (entmake (list (cons 0 "CIRCLE") (cons 8 "AI-LEGEND") '
            f'(cons 62 {aci}) (cons 10 (list {cx:.1f} {cy:.1f} 0)) (cons 40 {r:.1f})))'
        )

    def hline(x1, y, x2, aci=8):
        return (
            f'  (entmake (list (cons 0 "LINE") (cons 8 "AI-LEGEND") '
            f'(cons 62 {aci}) (cons 10 (list {x1:.1f} {y:.1f} 0)) '
            f'(cons 11 (list {x2:.1f} {y:.1f} 0))))'
        )

    # Outer border
    lines.append(rect(lx, ly - totH, W, totH))
    lines.append(hline(lx, ly - rowH,    lx + W))
    lines.append(hline(lx, ly - rowH*2,  lx + W))

    # Header
    lines.append(txt(lx + pad, ly - rowH*0.4, th, 7, "LEUCHTENLEGENDE / LEGEND"))
    lines.append(txt(lx + pad, ly - rowH*1.5, th*0.8, 8,
                     "Deckenausschnitt  AD:140 mm  EBT:110 mm  DA:128 mm"))

    # One row per luminaire type
    cr = 64.0
    for i, lp in enumerate(types):
        t   = lp["lumi_type"]
        aci = TYPE_ACI.get(t, 6)
        qty = type_count.get(t, 0)
        ry  = ly - rowH * (i + 3)

        lines.append(circle(lx + cr + pad, ry + rowH/2, cr,       aci))
        lines.append(circle(lx + cr + pad, ry + rowH/2, cr*0.35,  aci))
        lines.append(txt(lx + cr*2 + pad*3, ry + rowH*0.6, th*0.9, aci, f"Typ {t}"))
        lines.append(txt(lx + 16_000, ry + rowH*0.7, th*0.75, 7, _esc(lp["product_code"])))
        lines.append(txt(lx + 16_000, ry + rowH*0.28, th*0.7, 8,
                         f'{lp["wattage"]}W  {int(lp["beam_angle_deg"])}deg  {_esc(lp["description"])}'))
        lines.append(txt(lx + W - 18_000, ry + rowH*0.5, th, aci, f"x {qty}"))
        lines.append(hline(lx, ry, lx + W))

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

    # Collect unique types for block definition generation
    seen_types: dict[str, dict] = {}
    for lp in placed:
        seen_types.setdefault(lp["lumi_type"], lp)
    unique_types = list(seen_types.values())

    sections: list[str] = []
    sections.append(f";; Generated by LightingAI bridge — {len(placed)} luminaires")
    sections.append(f";; Project: {project}  Customer: {customer}")
    sections.append(f";; Total wattage: {sum(p['wattage'] for p in placed):.0f} W")
    sections.append("")

    sections += generate_block_defs(unique_types)
    sections += generate_inserts(placed)
    sections += generate_legend(placed)
    sections += generate_title_block(placed, project, customer, concept)

    out_path.write_text("\n".join(sections), encoding="utf-8")
    print(f"  Commands file written: {out_path}  ({len(placed)} luminaires)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    dwg  = Path(args.dwg).expanduser().resolve()
    out  = Path(args.out)

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
