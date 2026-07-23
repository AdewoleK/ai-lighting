#!/usr/bin/env python3
"""
lai_gui.py — LightingAI control panel (native macOS window)

Launched by typing LAI in AutoCAD.
Runs inside Terminal so Terminal's Accessibility permission is inherited.
Step 2 opens a visual symbol configurator — no number entry needed.
"""
import subprocess
import sys
import json
import math
import pathlib
import tkinter.ttk as ttk

try:
    import tkinter as tk
except ImportError:
    print("[LightingAI] tkinter not available. Run: brew install python-tk@3.x",
          file=sys.stderr)
    sys.exit(1)


# ── Send a command to AutoCAD ────────────────────────────────────────────────
def send_cmd(cmd: str) -> None:
    script = (
        'tell application "System Events"\n'
        '  set autocadList to every process whose name is "AutoCAD"\n'
        '  if (count of autocadList) is 0 then\n'
        '    error "AutoCAD is not running"\n'
        '  end if\n'
        '  set frontmost of (item 1 of autocadList) to true\n'
        '  delay 0.3\n'
        '  tell process "AutoCAD"\n'
        f'    keystroke "{cmd}"\n'
        '    key code 36\n'
        '  end tell\n'
        'end tell\n'
    )
    result = subprocess.run(
        ['/usr/bin/osascript', '-e', script],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        try:
            from tkinter import messagebox
            if 'not running' in err.lower():
                messagebox.showwarning(
                    "AutoCAD Not Detected",
                    "AutoCAD does not appear to be open.\n"
                    "Please open AutoCAD and then click the button again."
                )
            else:
                messagebox.showwarning("Command Error",
                                       f"Could not send to AutoCAD:\n\n{err}")
        except Exception:
            print(f"[LightingAI] send_cmd error: {err}", file=sys.stderr)


# ── Shape / colour data ───────────────────────────────────────────────────────
SHAPES = ['Circle', 'Square', 'Diamond', 'Triangle', 'Cross',
          'Hexagon', 'Star',   'Pentagon', 'Octagon',  'Plus']

COLOR_HEX = {
    'Red':     '#ff4040',
    'Yellow':  '#ffdd00',
    'Green':   '#44cc44',
    'Cyan':    '#00cccc',
    'Blue':    '#4488ff',
    'Magenta': '#dd44ff',
    'Orange':  '#ff8822',
}
COLORS = list(COLOR_HEX.keys())

SHAPE_DEFAULTS = ['Circle', 'Square', 'Diamond', 'Triangle', 'Cross', 'Hexagon']
COLOR_DEFAULTS = ['Magenta', 'Red', 'Cyan', 'Yellow', 'Blue', 'Green', 'Magenta']


def draw_shape(canvas, shape_name: str, hex_color: str, size: int = 32) -> None:
    """Draw a filled shape on a tk.Canvas. Size is the canvas pixel dimension."""
    canvas.delete('all')
    p, s = 4, size
    cx, cy = s // 2, s // 2
    r = s // 2 - p
    kw = dict(fill=hex_color, outline='')

    def poly(n, radius, start_deg=90):
        pts = []
        for i in range(n):
            a = math.radians(start_deg + 360 / n * i)
            pts += [cx + math.cos(a) * radius, cy - math.sin(a) * radius]
        return pts

    if shape_name == 'Circle':
        canvas.create_oval(p, p, s - p, s - p, **kw)
    elif shape_name == 'Square':
        canvas.create_rectangle(p, p, s - p, s - p, **kw)
    elif shape_name == 'Diamond':
        canvas.create_polygon(cx, p, s-p, cy, cx, s-p, p, cy, **kw)
    elif shape_name == 'Triangle':
        canvas.create_polygon(cx, p, s-p, s-p, p, s-p, **kw)
    elif shape_name == 'Cross':
        # Circle outline + thick diagonal X
        lw = max(3, s // 7)
        canvas.create_oval(p, p, s-p, s-p, outline=hex_color, width=max(2, lw//2))
        canvas.create_line(p, p, s-p, s-p, fill=hex_color, width=lw, capstyle='round')
        canvas.create_line(p, s-p, s-p, p,  fill=hex_color, width=lw, capstyle='round')
    elif shape_name == 'Hexagon':
        canvas.create_polygon(poly(6, r), **kw)
    elif shape_name == 'Pentagon':
        canvas.create_polygon(poly(5, r), **kw)
    elif shape_name == 'Octagon':
        canvas.create_polygon(poly(8, r, start_deg=22.5), **kw)
    elif shape_name == 'Star':
        pts = []
        for i in range(10):
            a = math.radians(i * 36 - 90)
            rad = r if i % 2 == 0 else r * 0.42
            pts += [cx + math.cos(a) * rad, cy + math.sin(a) * rad]
        canvas.create_polygon(pts, **kw)
    elif shape_name == 'Plus':
        t = max(4, r // 3)
        canvas.create_rectangle(p,    cy-t, s-p,  cy+t, **kw)
        canvas.create_rectangle(cx-t, p,    cx+t, s-p,  **kw)


# ── Step 0: full pipeline analysis dialog ────────────────────────────────────

def open_analysis_dialog():
    import threading
    import tkinter.filedialog as _fd

    dlg = tk.Toplevel(root)
    dlg.title("Analyse Floor Plan")
    dlg.configure(bg='#111419')
    dlg.resizable(True, True)
    dlg.attributes('-topmost', True)

    W, H = 640, 700
    sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
    dlg.geometry(f"{W}x{H}+{max(0, (sw - W) // 2)}+{max(0, (sh - H) // 4)}")
    dlg.minsize(520, 500)

    DBG    = '#111419'
    DCARD  = '#181c24'
    DBRI   = '#e0e6f0'
    DMUT   = '#5c6680'
    DBORD  = '#252c3a'
    DACC   = '#2196f3'

    # ── Header ─────────────────────────────────────────────────────────────
    hdr = tk.Frame(dlg, bg='#181c24')
    hdr.pack(fill='x')
    tk.Label(hdr, text="Analyse Floor Plan",
             font=('Helvetica', 13, 'bold'),
             bg='#181c24', fg=DACC, padx=16, pady=10, anchor='w').pack(side='left')
    tk.Label(hdr, text="AI pipeline",
             font=('Helvetica', 10), bg='#181c24', fg=DMUT, pady=10).pack(side='left')
    tk.Frame(dlg, bg=DBORD, height=1).pack(fill='x')

    # ── File picker ─────────────────────────────────────────────────────────
    fp_frame = tk.Frame(dlg, bg=DBG)
    fp_frame.pack(fill='x', padx=16, pady=(12, 4))
    tk.Label(fp_frame, text="Floor Plan File:",
             font=('Helvetica', 10, 'bold'), bg=DBG, fg=DBRI).pack(anchor='w')

    file_row = tk.Frame(fp_frame, bg=DBG)
    file_row.pack(fill='x', pady=(4, 0))

    file_var = tk.StringVar(value=str(_last_dwg_path[0]) if _last_dwg_path[0] else "")
    file_entry = tk.Entry(file_row, textvariable=file_var,
                          font=('Helvetica', 10),
                          bg='#0d1117', fg=DBRI,
                          insertbackground=DBRI,
                          relief='flat', highlightthickness=1,
                          highlightbackground=DBORD, highlightcolor=DACC)
    file_entry.pack(side='left', fill='x', expand=True, ipady=6, padx=(0, 8))

    def browse_file():
        path = _fd.askopenfilename(
            title="Select Floor Plan",
            filetypes=[("CAD & PDF", "*.dwg *.dxf *.pdf"), ("All files", "*.*")],
        )
        if path:
            file_var.set(path)
            _last_dwg_path[0] = pathlib.Path(path)

    tk.Button(file_row, text="Browse…",
              font=('Helvetica', 10), bg='#1e2330', fg=DBRI,
              activebackground=DBORD, relief='flat', padx=10, pady=4,
              cursor='hand2', command=browse_file).pack(side='left')

    # ── Project / Customer ──────────────────────────────────────────────────
    meta_frame = tk.Frame(dlg, bg=DBG)
    meta_frame.pack(fill='x', padx=16, pady=(8, 4))

    project_var  = tk.StringVar(value="Rossmann EG")
    customer_var = tk.StringVar(value="Dirk Rossmann GmbH")

    for mlabel, mvar in [("Project:", project_var), ("Customer:", customer_var)]:
        mrow = tk.Frame(meta_frame, bg=DBG)
        mrow.pack(fill='x', pady=2)
        tk.Label(mrow, text=mlabel, font=('Helvetica', 9), bg=DBG, fg=DMUT,
                 width=9, anchor='w').pack(side='left')
        me = tk.Entry(mrow, textvariable=mvar, font=('Helvetica', 10),
                      bg='#0d1117', fg=DBRI, insertbackground=DBRI,
                      relief='flat', highlightthickness=1,
                      highlightbackground=DBORD, highlightcolor=DACC)
        me.pack(side='left', fill='x', expand=True, ipady=5)

    # ── Run button ──────────────────────────────────────────────────────────
    tk.Frame(dlg, bg=DBORD, height=1).pack(fill='x', padx=16, pady=(10, 6))

    run_row = tk.Frame(dlg, bg=DBG)
    run_row.pack(fill='x', padx=16, pady=(0, 8))

    run_btn = tk.Button(run_row, text="▶  Analyse Floor Plan",
                        font=('Helvetica', 12, 'bold'),
                        bg=DACC, fg='#111419',
                        activebackground='#1565c0', activeforeground='#111419',
                        relief='flat', padx=24, pady=10, cursor='hand2')
    run_btn.pack(side='left')

    status_lbl = tk.Label(run_row, text="",
                          font=('Helvetica', 9), bg=DBG, fg=DMUT)
    status_lbl.pack(side='left', padx=(12, 0))

    # ── Log area ─────────────────────────────────────────────────────────────
    tk.Label(dlg, text="Pipeline Log:",
             font=('Helvetica', 9, 'bold'), bg=DBG, fg=DMUT).pack(
                 anchor='w', padx=16, pady=(0, 2))

    log_outer = tk.Frame(dlg, bg='#0a0d12')
    log_outer.pack(fill='both', expand=True, padx=16, pady=(0, 6))

    log_sb = tk.Scrollbar(log_outer)
    log_sb.pack(side='right', fill='y')
    log_text = tk.Text(log_outer, font=('Courier', 9),
                       bg='#0a0d12', fg='#7ec8a0',
                       insertbackground='#7ec8a0',
                       relief='flat', wrap='word',
                       yscrollcommand=log_sb.set,
                       state='disabled')
    log_sb.configure(command=log_text.yview)
    log_text.pack(side='left', fill='both', expand=True, padx=2, pady=2)

    def _log(msg: str):
        log_text.configure(state='normal')
        log_text.insert('end', msg + '\n')
        log_text.see('end')
        log_text.configure(state='disabled')

    # ── Results section ───────────────────────────────────────────────────────
    tk.Frame(dlg, bg=DBORD, height=1).pack(fill='x', padx=16)

    results_area = tk.Frame(dlg, bg=DBG)
    results_area.pack(fill='x', padx=16, pady=(6, 2))

    results_title = tk.Label(results_area, text="Results",
                             font=('Helvetica', 10, 'bold'), bg=DBG, fg=DBRI)
    counts_frame  = tk.Frame(results_area, bg=DBG)

    export_row = tk.Frame(dlg, bg=DBG)
    export_row.pack(fill='x', padx=16, pady=(2, 12))

    def _show_results(result: dict):
        results_title.pack(anchor='w', pady=(0, 4))
        for w in counts_frame.winfo_children():
            w.destroy()

        total   = result.get("total_luminaires", 0)
        wattage = result.get("total_wattage", 0)

        type_grid = tk.Frame(counts_frame, bg=DBG)
        type_grid.pack(anchor='w')
        TYPE_INFO = [
            ("A",  "#dd44ff"), ("AW", "#ff8822"),
            ("B",  "#ff4040"), ("C",  "#00cccc"),
            ("D",  "#ffdd00"), ("E",  "#4488ff"),
        ]
        for col, (t, tcolor) in enumerate(TYPE_INFO):
            count = result.get(f"type_{t}", 0)
            cell = tk.Frame(type_grid, bg='#181c24',
                            highlightthickness=1, highlightbackground=DBORD)
            cell.grid(row=0, column=col, padx=(0, 4), pady=2, ipadx=8, ipady=4)
            tk.Label(cell, text=t, font=('Helvetica', 8, 'bold'),
                     bg='#181c24', fg=tcolor).pack()
            tk.Label(cell, text=str(count), font=('Helvetica', 12, 'bold'),
                     bg='#181c24', fg=DBRI).pack()

        tk.Label(counts_frame,
                 text=f"Total: {total} fixtures  ·  {wattage:.0f} W",
                 font=('Helvetica', 10, 'bold'), bg=DBG, fg=DBRI).pack(
                     anchor='w', pady=(6, 0))
        counts_frame.pack(fill='x')

        for w in export_row.winfo_children():
            w.destroy()
        exports = result.get("exports", {})

        def _open_export(path_str: str):
            p = pathlib.Path(path_str)
            if p.exists():
                subprocess.Popen(['open', str(p)])
            else:
                from tkinter import messagebox
                messagebox.showwarning("File Not Found",
                                       f"Export file not found:\n{p}", parent=dlg)

        tk.Label(export_row, text="Open Export:",
                 font=('Helvetica', 9), bg=DBG, fg=DMUT).pack(side='left', padx=(0, 8))
        for elabel, ekey, ecolor in [
            ("DXF",   "dxf",  '#4a8fff'),
            ("Excel", "xlsx", '#4caf50'),
            ("PDF",   "pdf",  '#ff6b6b'),
        ]:
            epath = exports.get(ekey, "")
            tk.Button(export_row, text=elabel,
                      font=('Helvetica', 10, 'bold'),
                      bg='#1e2330', fg=ecolor,
                      activebackground=DBORD, activeforeground=ecolor,
                      relief='flat', padx=10, pady=6, cursor='hand2',
                      command=lambda p=epath: _open_export(p)).pack(side='left', padx=(0, 4))

        _last_job_result[0] = result

    # ── Run logic ─────────────────────────────────────────────────────────────
    _running  = [False]
    _log_q:  list = []

    def _flush_logs():
        while _log_q:
            _log(_log_q.pop(0))

    def _drain():
        _flush_logs()
        if _running[0]:
            dlg.after(200, _drain)
        else:
            _flush_logs()   # one final drain after worker exits

    def _worker():
        try:
            import requests as _req
            api = _API_BASE

            _log_q.append("Checking API server…")
            try:
                hr = _req.get(f"{api}/health", timeout=5)
                if not hr.ok or hr.json().get("status") != "ok":
                    raise ConnectionError("unexpected response")
                _log_q.append(f"[OK] API server is running at {api}")
            except Exception:
                _log_q.append(f"[ERROR] Cannot reach API at {api}")
                _log_q.append("       Tip: open a terminal and run:")
                _log_q.append("         cd ~/ai-lighting && python main.py api")
                dlg.after(0, lambda: run_btn.configure(
                    state='normal', text="▶  Analyse Floor Plan", bg=DACC))
                dlg.after(0, lambda: status_lbl.configure(
                    text="API not reachable", fg='#ff5c5c'))
                return

            dwg = file_var.get().strip()
            _log_q.append(f"Uploading: {pathlib.Path(dwg).name}")
            with open(dwg, "rb") as fh:
                pr = _req.post(
                    f"{api}/process",
                    files={"file": (pathlib.Path(dwg).name, fh, "application/octet-stream")},
                    data={"project_name": project_var.get().strip() or "Rossmann EG",
                          "customer":     customer_var.get().strip() or "Dirk Rossmann GmbH",
                          "concept_id":   "rossmann_standard"},
                    timeout=30,
                )
            pr.raise_for_status()
            job_id = pr.json()["job_id"]
            _log_q.append(f"Job ID: {job_id}")

            import time
            last_msg = ""
            while True:
                jr = _req.get(f"{api}/jobs/{job_id}", timeout=10)
                jr.raise_for_status()
                jdata  = jr.json()
                jstat  = jdata["status"]
                jmsg   = jdata.get("message", "")
                if jmsg and jmsg != last_msg:
                    _log_q.append(f"[{jstat.upper():<10}] {jmsg}")
                    last_msg = jmsg
                if jstat == "done":
                    result = jdata.get("result", {})
                    _log_q.append("=" * 50)
                    n = result.get("total_luminaires", "?")
                    w = result.get("total_wattage", 0)
                    _log_q.append(f"DONE: {n} luminaires  {w:.0f} W")
                    for _t in ("A", "AW", "B", "C", "D", "E"):
                        _n = result.get(f"type_{_t}", 0)
                        if _n:
                            _log_q.append(f"  Type {_t}: {_n}")
                    _log_q.append("=" * 50)
                    dlg.after(0, lambda r=result: _show_results(r))
                    dlg.after(0, lambda _n=n: status_lbl.configure(
                        text=f"{_n} lights placed ✓", fg='#4caf50'))
                    break
                elif jstat == "error":
                    _log_q.append(f"[ERROR] {jmsg}")
                    dlg.after(0, lambda: status_lbl.configure(
                        text="Pipeline failed — see log", fg='#ff5c5c'))
                    break
                time.sleep(1.5)

        except Exception as exc:
            _log_q.append(f"[FATAL] {exc}")
            dlg.after(0, lambda _e=exc: status_lbl.configure(
                text=f"Error: {_e}", fg='#ff5c5c'))
        finally:
            _running[0] = False
            dlg.after(0, lambda: run_btn.configure(
                state='normal', text="▶  Analyse Floor Plan", bg=DACC))

    def on_run():
        if _running[0]:
            return
        dwg = file_var.get().strip()
        if not dwg or not pathlib.Path(dwg).exists():
            from tkinter import messagebox
            messagebox.showwarning("No File",
                                   "Please select a floor plan file first.", parent=dlg)
            return
        _last_dwg_path[0] = pathlib.Path(dwg)
        _running[0] = True
        run_btn.configure(state='disabled', text="Running…", bg='#1a3a5c')
        status_lbl.configure(text="Starting pipeline…", fg=DMUT)
        results_title.pack_forget()
        counts_frame.pack_forget()
        for _w in export_row.winfo_children():
            _w.destroy()
        threading.Thread(target=_worker, daemon=True).start()
        dlg.after(200, _drain)

    run_btn.configure(command=on_run)


# ── Step 1: grid line-thickness + color picker ───────────────────────────────
_GRID_CFG_FILE  = pathlib.Path.home() / "ai-lighting" / "lightingai_grid_config.json"
_SUMMARY_FILE        = pathlib.Path.home() / "ai-lighting" / "lightingai_summary.json"
_CUSTOM_DESCS_FILE   = pathlib.Path.home() / "ai-lighting" / "lightingai_custom_descs.json"
_MAX_CUSTOM_DESCS    = 15
_API_BASE            = "http://localhost:8000"
_last_dwg_path:   list = [None]   # mutable cell — pathlib.Path or None
_last_job_result: list = [None]   # mutable cell — result dict or None


def _load_custom_descs() -> list:
    """Return user-saved custom descriptions, most-recent first."""
    try:
        if _CUSTOM_DESCS_FILE.exists():
            data = json.loads(_CUSTOM_DESCS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return [str(d).strip() for d in data if str(d).strip()]
    except Exception:
        pass
    return []


def _save_custom_desc(desc: str) -> None:
    """Persist a custom description; deduplicates and keeps most-recent at top."""
    desc = desc.strip()
    if not desc:
        return
    existing = _load_custom_descs()
    existing = [d for d in existing if d != desc]
    existing.insert(0, desc)
    existing = existing[:_MAX_CUSTOM_DESCS]
    try:
        _CUSTOM_DESCS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CUSTOM_DESCS_FILE.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8'
        )
    except Exception:
        pass


def _delete_custom_desc(desc: str) -> None:
    """Remove a custom description from the persistent list."""
    existing = _load_custom_descs()
    existing = [d for d in existing if d != desc]
    try:
        _CUSTOM_DESCS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CUSTOM_DESCS_FILE.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8'
        )
    except Exception:
        pass


def _edit_custom_desc(old: str, new: str) -> None:
    """Replace an existing custom description in-place, preserving its list position."""
    new = new.strip()
    if not new:
        return
    existing = _load_custom_descs()
    existing = [new if d == old else d for d in existing]
    try:
        _CUSTOM_DESCS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CUSTOM_DESCS_FILE.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8'
        )
    except Exception:
        pass


# ── Presets persistence ──────────────────────────────────────────────────────
_PRESETS_FILE = pathlib.Path.home() / "ai-lighting" / "lightingai_presets.json"
_MAX_PRESETS  = 3


def _load_presets() -> list:
    """Return saved presets (up to _MAX_PRESETS)."""
    try:
        if _PRESETS_FILE.exists():
            data = json.loads(_PRESETS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return data[:_MAX_PRESETS]
    except Exception:
        pass
    return []


def _write_presets(presets: list) -> None:
    """Persist the presets list to disk."""
    try:
        _PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PRESETS_FILE.write_text(
            json.dumps(presets[:_MAX_PRESETS], ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
    except Exception:
        pass


# AutoCAD lineweight enum values (hundredths of mm)
# Each tuple: (acad_lw_int, label, mm_string, preview_px)
_PRESET_LINEWEIGHTS = [
    (13, "Extra Light", "0.13 mm",  1),
    (18, "Light",       "0.18 mm",  2),
    (25, "Standard ★",  "0.25 mm",  3),
    (35, "Medium",      "0.35 mm",  4),
    (50, "Bold",        "0.50 mm",  6),
]
_DEFAULT_LW = 25   # 0.25 mm — Rossmann standard

# AutoCAD ACI color presets for the grid lines
# Each tuple: (aci_int, label, hex_for_swatch)
_PRESET_COLORS = [
    (253, "Light Grey ★", "#C8C8C8"),
    (8,   "Dark Grey",    "#808080"),
    (5,   "Blue",         "#4488FF"),
    (4,   "Cyan",         "#00CCCC"),
    (7,   "White",        "#E8E8E8"),
    (2,   "Yellow",       "#FFDD00"),
]
_DEFAULT_COLOR = 253


def open_grid_dialog():
    dlg = tk.Toplevel(root)
    dlg.title("Grid Line Thickness")
    dlg.configure(bg='#111419')
    dlg.resizable(False, False)
    dlg.attributes('-topmost', True)
    dlg.grab_set()

    DLG_BG   = '#111419'
    CARD_BG  = '#1a1f28'
    SEL_BG   = '#1e3a5f'
    SEL_BD   = '#4a90d9'
    NORM_BD  = '#2a2f3a'
    BRIGHT   = '#e8eaf0'
    MUTED    = '#8892a0'
    BTN_OK   = '#4caf50'
    BTN_CANC = '#333840'
    LINE_COL = '#7b9fc8'   # grid line preview colour

    selected_lw = tk.IntVar(value=_DEFAULT_LW)

    # ── Header ──────────────────────────────────────────────────────────────
    hdr = tk.Frame(dlg, bg=DLG_BG)
    hdr.pack(fill='x', padx=18, pady=(16, 4))
    tk.Label(hdr, text="Grid Line Thickness",
             font=('Helvetica', 15, 'bold'), bg=DLG_BG, fg=BRIGHT).pack(anchor='w')
    tk.Label(hdr, text="Choose how thick the ceiling grid lines will be printed.",
             font=('Helvetica', 10), bg=DLG_BG, fg=MUTED).pack(anchor='w')

    tk.Frame(dlg, bg='#2a2f3a', height=1).pack(fill='x', padx=18, pady=(10, 12))

    # ── Preset cards (each shows a live line-weight preview) ────────────────
    preset_frames: dict = {}

    def refresh_cards(active_lw: int):
        for lw_val, frame in preset_frames.items():
            is_sel = (lw_val == active_lw)
            bg = SEL_BG if is_sel else CARD_BG
            bd = SEL_BD if is_sel else NORM_BD
            frame.configure(bg=bg, highlightbackground=bd)
            for child in frame.winfo_children():
                try:
                    child.configure(bg=bg)
                except tk.TclError:
                    pass  # Canvas ignores bg kwarg

    def select_preset(lw_val: int):
        selected_lw.set(lw_val)
        custom_var.set("")
        refresh_cards(lw_val)

    cards_frame = tk.Frame(dlg, bg=DLG_BG)
    cards_frame.pack(fill='x', padx=18, pady=(0, 6))

    for lw_val, label, mm_str, px in _PRESET_LINEWEIGHTS:
        is_default = (lw_val == _DEFAULT_LW)
        f = tk.Frame(cards_frame,
                     bg=SEL_BG if is_default else CARD_BG,
                     highlightthickness=2,
                     highlightbackground=SEL_BD if is_default else NORM_BD,
                     cursor='hand2')
        f.pack(side='left', padx=(0, 7), pady=2, ipadx=6, ipady=4)
        preset_frames[lw_val] = f

        # Line preview via Canvas
        cv = tk.Canvas(f, width=70, height=28,
                       bg=SEL_BG if is_default else CARD_BG,
                       highlightthickness=0)
        cv.pack(pady=(6, 2), padx=8)
        cv.create_line(6, 14, 64, 14, fill=LINE_COL, width=px)

        tk.Label(f, text=label, font=('Helvetica', 9, 'bold'),
                 bg=f['bg'], fg=BRIGHT).pack(padx=8)
        tk.Label(f, text=mm_str, font=('Helvetica', 8),
                 bg=f['bg'], fg=MUTED).pack(padx=8, pady=(0, 4))

        for widget in [f, cv] + list(f.winfo_children()):
            widget.bind('<Button-1>', lambda e, v=lw_val: select_preset(v))

    # ── Custom entry ─────────────────────────────────────────────────────────
    tk.Frame(dlg, bg='#2a2f3a', height=1).pack(fill='x', padx=18, pady=(8, 10))

    custom_row = tk.Frame(dlg, bg=DLG_BG)
    custom_row.pack(fill='x', padx=18, pady=(0, 2))
    tk.Label(custom_row, text="Or enter a custom thickness:",
             font=('Helvetica', 10), bg=DLG_BG, fg=MUTED).pack(side='left')

    entry_row = tk.Frame(dlg, bg=DLG_BG)
    entry_row.pack(fill='x', padx=18, pady=(2, 0))

    custom_var = tk.StringVar()

    def on_custom_change(*_):
        txt = custom_var.get().strip()
        if txt:
            for f in preset_frames.values():
                f.configure(bg=CARD_BG, highlightbackground=NORM_BD)
                for child in f.winfo_children():
                    try: child.configure(bg=CARD_BG)
                    except tk.TclError: pass

    custom_var.trace_add('write', on_custom_change)

    custom_entry = tk.Entry(entry_row, textvariable=custom_var, width=8,
                            font=('Helvetica', 12),
                            bg='#1a1f28', fg=BRIGHT, insertbackground=BRIGHT,
                            relief='flat', highlightthickness=2,
                            highlightbackground='#2a2f3a',
                            highlightcolor=SEL_BD)
    custom_entry.pack(side='left')
    tk.Label(entry_row, text=" mm  (0.05 – 2.00)",
             font=('Helvetica', 9), bg=DLG_BG, fg='#555e6a').pack(side='left', padx=(4, 0))

    tk.Frame(dlg, bg='#2a2f3a', height=1).pack(fill='x', padx=18, pady=(10, 10))

    # ── Color picker ─────────────────────────────────────────────────────────
    tk.Label(dlg, text="Grid Line Color:", font=('Helvetica', 10),
             bg=DLG_BG, fg=MUTED).pack(anchor='w', padx=18)

    selected_color = tk.IntVar(value=_DEFAULT_COLOR)
    color_frames: dict = {}

    def select_color(aci: int):
        selected_color.set(aci)
        for a, cf in color_frames.items():
            is_sel = (a == aci)
            cf.configure(highlightbackground=SEL_BD if is_sel else NORM_BD)

    color_row = tk.Frame(dlg, bg=DLG_BG)
    color_row.pack(fill='x', padx=18, pady=(4, 0))

    for aci, clabel, chex in _PRESET_COLORS:
        is_default = (aci == _DEFAULT_COLOR)
        cf = tk.Frame(color_row, bg=CARD_BG, highlightthickness=2,
                      highlightbackground=SEL_BD if is_default else NORM_BD,
                      cursor='hand2')
        cf.pack(side='left', padx=(0, 6), pady=2, ipadx=5, ipady=4)
        color_frames[aci] = cf
        # Colour swatch
        sw = tk.Canvas(cf, width=30, height=14, bg=CARD_BG, highlightthickness=0)
        sw.pack(pady=(4, 1), padx=6)
        sw.create_rectangle(2, 2, 28, 12, fill=chex, outline='')
        tk.Label(cf, text=clabel, font=('Helvetica', 8),
                 bg=CARD_BG, fg=MUTED).pack(padx=6, pady=(0, 3))
        for w in [cf, sw] + list(cf.winfo_children()):
            w.bind('<Button-1>', lambda e, a=aci: select_color(a))

    tk.Frame(dlg, bg='#2a2f3a', height=1).pack(fill='x', padx=18, pady=(10, 8))

    # ── Action buttons ───────────────────────────────────────────────────────
    btn_row = tk.Frame(dlg, bg=DLG_BG)
    btn_row.pack(fill='x', padx=18, pady=(0, 16))

    def on_draw():
        raw = custom_var.get().strip()
        if raw:
            try:
                mm_val = float(raw)
            except ValueError:
                from tkinter import messagebox
                messagebox.showwarning("Invalid Input",
                    "Please enter a number, e.g. 0.25", parent=dlg)
                return
            if not (0.05 <= mm_val <= 2.00):
                from tkinter import messagebox
                messagebox.showwarning("Out of Range",
                    f"{mm_val} mm is outside the valid range (0.05 – 2.00 mm).", parent=dlg)
                return
            lw = int(round(mm_val * 100))
        else:
            lw = selected_lw.get()

        cfg = {"lineweight": lw, "color": selected_color.get()}
        _GRID_CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _GRID_CFG_FILE.write_text(json.dumps(cfg))
        dlg.destroy()
        send_cmd('LIGHTINGAI_GRID')

    def on_cancel():
        dlg.destroy()

    tk.Button(btn_row, text="Draw Grid", font=('Helvetica', 11, 'bold'),
              bg=BTN_OK, fg='white', activebackground='#388e3c',
              activeforeground='white', relief='flat', padx=20, pady=8,
              cursor='hand2', command=on_draw).pack(side='left')
    tk.Button(btn_row, text="Cancel", font=('Helvetica', 11),
              bg=BTN_CANC, fg=MUTED, activebackground='#444',
              activeforeground=BRIGHT, relief='flat', padx=16, pady=8,
              cursor='hand2', command=on_cancel).pack(side='left', padx=(10, 0))

    dlg.bind('<Return>', lambda e: on_draw())
    dlg.bind('<Escape>', lambda e: on_cancel())

    dlg.update_idletasks()
    rx = root.winfo_rootx() + (root.winfo_width()  - dlg.winfo_width())  // 2
    ry = root.winfo_rooty() + (root.winfo_height() - dlg.winfo_height()) // 2
    dlg.geometry(f"+{rx}+{ry}")


# ── Step 2: visual symbol configurator ──────────────────────────────────────
def open_config_dialog():
    dlg = tk.Toplevel(root)
    dlg.title("Configure Light Symbols")
    dlg.configure(bg='#111419')
    dlg.resizable(False, False)
    dlg.attributes('-topmost', True)
    dlg.lift()

    W = 460
    sw = dlg.winfo_screenwidth()
    sh = dlg.winfo_screenheight()
    H = min(900, int(sh * 0.88))   # tall enough to show everything; caps at 88% of screen
    dlg.geometry(f"{W}x{H}+{max(0, (sw - W) // 2 - 160)}+{max(0, (sh - H) // 2)}")

    # ── Load existing config so previous settings are preserved ─────────────
    import pathlib
    _cfg_path = pathlib.Path.home() / "ai-lighting" / "lightingai_typeconfig.json"
    _existing: dict = {}
    if _cfg_path.exists():
        try:
            for _e in json.loads(_cfg_path.read_text()):
                _existing[_e["type"]] = _e
        except Exception:
            pass

    # ── Per-type state ──────────────────────────────────────────────────────
    selections = [
        {
            'shape':       _existing.get(chr(65+i), {}).get('shape',       SHAPE_DEFAULTS[i]),
            'color':       _existing.get(chr(65+i), {}).get('color',       COLOR_DEFAULTS[i]),
            'description': _existing.get(chr(65+i), {}).get('description', ''),
        }
        for i in range(6)
    ]
    active_idx  = tk.IntVar(value=0)
    num_types   = tk.IntVar(value=5)

    # ── Header ──────────────────────────────────────────────────────────────
    hdr = tk.Frame(dlg, bg='#181c24')
    hdr.pack(fill='x')
    tk.Label(hdr, text="Configure Light Symbols",
             font=('Helvetica', 13, 'bold'),
             bg='#181c24', fg='#e0e6f0',
             padx=16, pady=12, anchor='w').pack(side='left')
    tk.Frame(dlg, bg='#252c3a', height=1).pack(fill='x')

    # ── Presets strip ────────────────────────────────────────────────────────
    ps_outer = tk.Frame(dlg, bg='#0d1117')
    ps_outer.pack(fill='x')

    ps_hdr = tk.Frame(ps_outer, bg='#0d1117')
    ps_hdr.pack(fill='x', padx=16, pady=(10, 4))
    tk.Label(ps_hdr, text="MY PRESETS",
             font=('Helvetica', 10, 'bold'), bg='#0d1117', fg='#8892a4').pack(side='left')
    tk.Label(ps_hdr, text="Save your full setup and reload it in one click",
             font=('Helvetica', 8), bg='#0d1117', fg='#3a4254').pack(side='left', padx=(8, 0))

    ps_cards_row = tk.Frame(ps_outer, bg='#0d1117')
    ps_cards_row.pack(fill='x', padx=16, pady=(0, 10))

    # — Preset helpers (forward-reference rebuild_tabs via closure — called at click time) ——

    def _prompt_save_preset():
        existing = _load_presets()
        if len(existing) >= _MAX_PRESETS:
            from tkinter import messagebox
            messagebox.showwarning(
                "Presets Full",
                f"You already have {_MAX_PRESETS} presets saved.\n"
                "Delete one to make room for a new one.",
                parent=dlg
            )
            return

        prompt = tk.Toplevel(dlg)
        prompt.title("Save Preset")
        prompt.configure(bg='#111419')
        prompt.resizable(False, False)
        prompt.grab_set()
        prompt.attributes('-topmost', True)

        tk.Label(prompt, text="Give this preset a name:",
                 font=('Helvetica', 12), bg='#111419', fg='#c0cce0').pack(
                     padx=24, pady=(18, 6), anchor='w')

        name_var = tk.StringVar()
        name_ent = tk.Entry(prompt, textvariable=name_var,
                            font=('Helvetica', 12),
                            bg='#0d1117', fg='#e8eeff',
                            insertbackground='#e040fb',
                            relief='flat', bd=0,
                            highlightthickness=2,
                            highlightbackground='#2a3248',
                            highlightcolor='#e040fb')
        name_ent.pack(fill='x', padx=24, ipady=8)
        name_ent.focus_set()

        def _do_save():
            name = name_var.get().strip()
            if not name:
                return
            n = num_types.get()
            config = [
                {"type":        chr(65 + i),
                 "shape":       selections[i]['shape'],
                 "color":       selections[i]['color'],
                 "description": selections[i].get('description', '').strip()}
                for i in range(n)
            ]
            saved = _load_presets()
            saved.append({"name": name, "num_types": n, "config": config})
            _write_presets(saved)
            _rebuild_preset_strip()
            prompt.destroy()

        br = tk.Frame(prompt, bg='#111419')
        br.pack(fill='x', padx=24, pady=14)
        tk.Button(br, text="Save",
                  font=('Helvetica', 11, 'bold'),
                  bg='#e040fb', fg='#111419',
                  activebackground='#c020d0', activeforeground='#111419',
                  relief='flat', padx=16, pady=6, cursor='hand2',
                  command=_do_save).pack(side='right', padx=(6, 0))
        tk.Button(br, text="Cancel",
                  font=('Helvetica', 11),
                  bg='#1e2330', fg='#8892a4',
                  relief='flat', padx=16, pady=6, cursor='hand2',
                  command=prompt.destroy).pack(side='right')

        name_ent.bind('<Return>', lambda e: _do_save())
        name_ent.bind('<Escape>', lambda e: prompt.destroy())

        prompt.update_idletasks()
        px = dlg.winfo_rootx() + (dlg.winfo_width()  - prompt.winfo_reqwidth())  // 2
        py = dlg.winfo_rooty() + (dlg.winfo_height() - prompt.winfo_reqheight()) // 2
        prompt.geometry(f"+{max(0, px)}+{max(0, py)}")

    def _apply_preset(preset):
        n = min(max(1, preset.get('num_types', 5)), 6)
        num_types.set(n)
        for entry in preset.get('config', []):
            idx = ord(entry.get('type', 'A')) - 65
            if 0 <= idx < 6:
                selections[idx]['shape']       = entry.get('shape',       SHAPE_DEFAULTS[idx])
                selections[idx]['color']       = entry.get('color',       COLOR_DEFAULTS[idx])
                selections[idx]['description'] = entry.get('description', '')
        rebuild_tabs()   # resolved at call time — defined later in the same closure

    def _delete_preset_at(slot_idx):
        presets = _load_presets()
        if 0 <= slot_idx < len(presets):
            presets.pop(slot_idx)
            _write_presets(presets)
        _rebuild_preset_strip()

    def _rebuild_preset_strip():
        for w in ps_cards_row.winfo_children():
            w.destroy()
        presets = _load_presets()

        for slot in range(_MAX_PRESETS):
            is_filled = slot < len(presets)
            card = tk.Frame(ps_cards_row,
                            bg='#141820' if is_filled else '#0b0d14',
                            highlightthickness=1,
                            highlightbackground='#252c3a' if is_filled else '#181e28')
            card.pack(side='left', fill='both', expand=True,
                      padx=(0, 4 if slot < _MAX_PRESETS - 1 else 0))

            if is_filled:
                preset = presets[slot]
                name   = preset.get('name', f'Preset {slot + 1}')
                cfg    = preset.get('config', [])
                n_t    = preset.get('num_types', len(cfg))

                # Name
                tk.Label(card,
                         text=(name[:15] + '…') if len(name) > 15 else name,
                         font=('Helvetica', 10, 'bold'), bg='#141820', fg='#e0e6f0',
                         anchor='w').pack(fill='x', padx=8, pady=(8, 2))

                # Mini shape/colour preview
                dots = tk.Frame(card, bg='#141820')
                dots.pack(fill='x', padx=8, pady=(0, 2))
                for pe in cfg[:5]:
                    chex = COLOR_HEX.get(pe.get('color', 'Blue'), '#4488ff')
                    dc   = tk.Canvas(dots, width=13, height=13,
                                     bg='#141820', highlightthickness=0)
                    dc.pack(side='left', padx=1)
                    draw_shape(dc, pe.get('shape', 'Circle'), chex, size=13)

                tk.Label(card,
                         text=f"{n_t} type{'s' if n_t != 1 else ''}",
                         font=('Helvetica', 8), bg='#141820', fg='#5c6680').pack(
                             anchor='w', padx=8, pady=(0, 4))

                # Apply / Delete row
                br = tk.Frame(card, bg='#141820')
                br.pack(fill='x', padx=6, pady=(0, 8))

                def _make_apply_cmd(p=preset):
                    return lambda: _apply_preset(p)

                def _make_delete_cmd(s=slot):
                    return lambda: _delete_preset_at(s)

                tk.Button(br, text="Apply",
                          font=('Helvetica', 9, 'bold'),
                          bg='#e040fb', fg='#111419',
                          activebackground='#c020d0', activeforeground='#111419',
                          relief='flat', padx=4, pady=3, cursor='hand2',
                          command=_make_apply_cmd()).pack(
                              side='left', fill='x', expand=True, padx=(0, 2))
                tk.Button(br, text="✕",
                          font=('Helvetica', 9, 'bold'),
                          bg='#1a1520', fg='#ff5c7a',
                          activebackground='#2a1020', activeforeground='#ff8090',
                          relief='flat', padx=8, pady=3, cursor='hand2',
                          command=_make_delete_cmd()).pack(side='left')

            else:
                # Empty slot
                tk.Label(card, text="—\nopen slot",
                         font=('Helvetica', 9), bg='#0b0d14', fg='#1e2330',
                         justify='center').pack(expand=True, pady=20)

    # Save button (packed after helpers are defined so command is valid)
    tk.Button(ps_hdr, text="＋  Save current as preset",
              font=('Helvetica', 9, 'bold'), bg='#1a2236', fg='#e040fb',
              activebackground='#252c3a', activeforeground='#ff70ff',
              relief='flat', padx=8, pady=3, cursor='hand2',
              command=_prompt_save_preset).pack(side='right')

    _rebuild_preset_strip()

    tk.Frame(dlg, bg='#252c3a', height=1).pack(fill='x')

    # ── Number of types ──────────────────────────────────────────────────────
    n_row = tk.Frame(dlg, bg='#111419')
    n_row.pack(fill='x', padx=16, pady=(12, 4))
    tk.Label(n_row, text="How many light types?",
             font=('Helvetica', 15), bg='#111419', fg='#FFFFFF').pack(side='left')

    def change_n(delta):
        v = num_types.get() + delta
        if 1 <= v <= 6:
            num_types.set(v)
            rebuild_tabs()

    tk.Button(n_row, text="−", font=('Helvetica', 15, 'bold'),
              bg='#1e2330', fg='#111419', relief='flat',
              padx=10, pady=2, cursor='hand2',
              command=lambda: change_n(-1)).pack(side='left', padx=(14, 4))
    tk.Label(n_row, textvariable=num_types, font=('Helvetica', 14, 'bold'),
             bg='#111419', fg='#e040fb', width=2).pack(side='left')
    tk.Button(n_row, text="+", font=('Helvetica', 15, 'bold'),
              bg='#1e2330', fg="#111419", relief='flat',
              padx=10, pady=2, cursor='hand2',
              command=lambda: change_n(1)).pack(side='left', padx=(4, 0))

    # ── Type selector tabs ───────────────────────────────────────────────────
    tabs_frame = tk.Frame(dlg, bg='#111419')
    tabs_frame.pack(fill='x', padx=16, pady=(8, 0))

    type_btns = []

    def select_type(idx):
        active_idx.set(idx)
        refresh()
        for j, b in enumerate(type_btns):
            is_active = (j == idx)
            b.configure(
                bg='#e040fb' if is_active else '#1e2330',
                fg='#111419' if is_active else '#8892a4'
            )

    def rebuild_tabs():
        for b in type_btns:
            b.pack_forget()
        n = num_types.get()
        for j in range(n):
            type_btns[j].pack(side='left', padx=2)
        cur = active_idx.get()
        select_type(cur if cur < n else n - 1)

    for i in range(6):
        b = tk.Button(tabs_frame,
                      text=chr(65 + i),
                      font=('Helvetica', 12, 'bold'),
                      bg='#1e2330', fg='#8892a4',
                      relief='flat', padx=14, pady=6,
                      cursor='hand2',
                      command=lambda x=i: select_type(x))
        type_btns.append(b)

    # ── Scrollable content card ───────────────────────────────────────────────
    tk.Frame(dlg, bg='#252c3a', height=1).pack(fill='x', padx=16, pady=(8, 0))

    card_outer = tk.Frame(dlg, bg='#181c24')
    card_outer.pack(fill='both', expand=True, padx=16, pady=0)

    card_canvas = tk.Canvas(card_outer, bg='#181c24', highlightthickness=0)
    card_canvas.pack(side='left', fill='both', expand=True)

    card_sb = tk.Scrollbar(card_outer, orient='vertical', command=card_canvas.yview)
    card_sb.pack(side='right', fill='y')
    card_canvas.configure(yscrollcommand=card_sb.set)

    card = tk.Frame(card_canvas, bg='#181c24')
    _card_win = card_canvas.create_window((0, 0), window=card, anchor='nw')

    def _on_card_canvas_resize(e):
        card_canvas.itemconfig(_card_win, width=e.width)
    card_canvas.bind('<Configure>', _on_card_canvas_resize)

    def _on_card_content_change(e):
        card_canvas.configure(scrollregion=card_canvas.bbox('all'))
    card.bind('<Configure>', _on_card_content_change)

    def _card_mousewheel(e):
        card_canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
    card_canvas.bind('<MouseWheel>', _card_mousewheel)
    card.bind('<MouseWheel>', _card_mousewheel)

    # Shape rows — 5 per row, 2 rows
    tk.Label(card, text="SHAPE",
             font=('Helvetica', 9, 'bold'), bg='#181c24', fg='#3a4254',
             anchor='w').pack(fill='x', padx=14, pady=(14, 4))

    shape_widgets = []
    for row_idx in range(2):
        row_frame = tk.Frame(card, bg='#181c24')
        row_frame.pack(fill='x', padx=14, pady=(0, 4))
        for sh in SHAPES[row_idx * 5 : row_idx * 5 + 5]:
            col = tk.Frame(row_frame, bg='#181c24', cursor='hand2')
            col.pack(side='left', padx=3)
            c = tk.Canvas(col, width=40, height=40,
                          bg='#0d1117', highlightthickness=2,
                          highlightbackground='#1e2330')
            c.pack()
            lbl = tk.Label(col, text=sh[:4],
                           font=('Helvetica', 7), bg='#181c24', fg='#3a4254')
            lbl.pack()
            shape_widgets.append((c, lbl, sh, col))

    # Color row
    tk.Label(card, text="COLOUR",
             font=('Helvetica', 9, 'bold'), bg='#181c24', fg='#3a4254',
             anchor='w').pack(fill='x', padx=14, pady=(14, 6))

    color_row = tk.Frame(card, bg='#181c24')
    color_row.pack(fill='x', padx=14)

    color_widgets = []   # (canvas, label_widget, color_name)
    for cl in COLORS:
        col = tk.Frame(color_row, bg='#181c24', cursor='hand2')
        col.pack(side='left', padx=3)
        c = tk.Canvas(col, width=36, height=36,
                      bg=COLOR_HEX[cl],
                      highlightthickness=2,
                      highlightbackground='#1e2330')
        c.pack()
        lbl = tk.Label(col, text=cl[:3],
                       font=('Helvetica', 8), bg='#181c24', fg='#3a4254')
        lbl.pack()
        color_widgets.append((c, lbl, cl, col))

    # ── Description Manager ───────────────────────────────────────────────
    _CANONICAL_DESCS = [
        "MIKA80-E K1 Regalbeleuchtung 15W 40° 2400lm 3000K",
        "MIKA80-E K4 Ergänzungsbeleuchtung 20W 60° 3200lm 3000K",
        "MIKA80-E K3 Regalbeleuchtung Rand 15W 40° 2400lm 3000K",
        "MIKA80-E K2 Checkout/Service 20W 40° 3200lm 3000K",
        "NEO85-SX K6 Schaufenster-Strahler 20W 60° 3200lm Track",
    ]
    _DESC_PLACEHOLDER = "Type your light description here..."

    # — Section header ─────────────────────────────────────────────────────
    tk.Frame(card, bg='#252c3a', height=1).pack(fill='x', padx=14, pady=(16, 0))
    desc_hdr_row = tk.Frame(card, bg='#181c24')
    desc_hdr_row.pack(fill='x', padx=14, pady=(10, 2))
    tk.Label(desc_hdr_row, text="LIGHT DESCRIPTION",
             font=('Helvetica', 12, 'bold'), bg='#181c24', fg='#c0cce0',
             anchor='w').pack(side='left')
    tk.Label(desc_hdr_row, text="or pick from the list below",
             font=('Helvetica', 10), bg='#181c24', fg='#5c6680',
             anchor='e').pack(side='right')

    # — Entry with placeholder ─────────────────────────────────────────────
    desc_var = tk.StringVar()
    desc_entry = tk.Entry(card, textvariable=desc_var,
                          font=('Helvetica', 12),
                          bg='#0d1117', fg='#5c6680',
                          insertbackground='#e040fb',
                          relief='flat', bd=0,
                          highlightthickness=2,
                          highlightbackground='#2a3248',
                          highlightcolor='#e040fb')
    desc_entry.pack(fill='x', padx=14, ipady=9, pady=(0, 0))

    def _is_placeholder():
        return desc_var.get() == _DESC_PLACEHOLDER

    def _show_placeholder():
        desc_var.set(_DESC_PLACEHOLDER)
        desc_entry.configure(fg='#5c6680')

    def _clear_placeholder(e=None):
        if _is_placeholder():
            desc_var.set('')
            desc_entry.configure(fg='#e8eeff')

    def _restore_if_empty(e=None):
        if not desc_var.get().strip():
            _show_placeholder()

    desc_entry.bind('<FocusIn>',  _clear_placeholder)
    desc_entry.bind('<FocusOut>', _restore_if_empty)

    def _on_desc_change(*_):
        if _is_placeholder():
            return
        selections[active_idx.get()]['description'] = desc_var.get()

    desc_var.trace_add('write', _on_desc_change)
    _show_placeholder()

    # — Unified list: My Saved + Standard Catalog ─────────────────────────
    list_hdr = tk.Frame(card, bg='#181c24')
    list_hdr.pack(fill='x', padx=14, pady=(14, 2))
    tk.Label(list_hdr, text="ALL DESCRIPTIONS",
             font=('Helvetica', 11, 'bold'), bg='#181c24', fg='#8892a4').pack(side='left')

    def _do_add_to_list():
        val = desc_var.get().strip()
        if not val or _is_placeholder() or val in _CANONICAL_DESCS:
            return
        _save_custom_desc(val)
        _rebuild_list()

    tk.Button(list_hdr, text="  + Add to My List  ",
              font=('Helvetica', 10, 'bold'), bg='#2a1a3e', fg='#e040fb',
              activebackground='#3a2050', activeforeground='#ff70ff',
              relief='flat', padx=4, pady=4, cursor='hand2',
              command=_do_add_to_list).pack(side='right')

    list_outer = tk.Frame(card, bg='#0d1117',
                          highlightthickness=1, highlightbackground='#2a3248')
    list_outer.pack(fill='x', padx=14, pady=(0, 8))

    list_canvas = tk.Canvas(list_outer, bg='#0d1117', highlightthickness=0)
    list_canvas.pack(side='left', fill='x', expand=True)

    list_sb = tk.Scrollbar(list_outer, orient='vertical', command=list_canvas.yview)
    list_canvas.configure(yscrollcommand=list_sb.set)

    list_inner = tk.Frame(list_canvas, bg='#0d1117')
    _list_win_id = list_canvas.create_window((0, 0), window=list_inner, anchor='nw')

    def _on_list_resize(e):
        list_canvas.itemconfig(_list_win_id, width=e.width)
    list_canvas.bind('<Configure>', _on_list_resize)

    def _use_desc(d):
        desc_entry.configure(fg='#e8eeff')
        desc_var.set(d)

    def _do_inline_edit(old_desc, row_frame):
        for w in row_frame.winfo_children():
            w.destroy()
        edit_var = tk.StringVar(value=old_desc)
        edit_ent = tk.Entry(row_frame, textvariable=edit_var,
                            font=('Helvetica', 11), bg='#1a2b42', fg='#e8eeff',
                            insertbackground='#e040fb', relief='flat', bd=0,
                            highlightthickness=2, highlightcolor='#5c9fff',
                            highlightbackground='#5c9fff')
        edit_ent.pack(side='left', fill='x', expand=True, padx=8, pady=5)
        edit_ent.select_range(0, 'end')
        edit_ent.focus_set()

        def _confirm():
            new = edit_var.get().strip()
            if new and new != old_desc:
                _edit_custom_desc(old_desc, new)
                if desc_var.get() == old_desc:
                    _use_desc(new)
            _rebuild_list()

        edit_ent.bind('<Return>', lambda e: _confirm())
        edit_ent.bind('<Escape>', lambda e: _rebuild_list())

        tk.Button(row_frame, text=" ✓ ",
                  font=('Helvetica', 11, 'bold'), bg='#16111e', fg='#40e090',
                  activebackground='#1a2e1a', relief='flat', padx=4, cursor='hand2',
                  command=_confirm).pack(side='right', padx=(0, 2))
        tk.Button(row_frame, text=" ✕ ",
                  font=('Helvetica', 11, 'bold'), bg='#16111e', fg='#ff5c7a',
                  activebackground='#2a1520', relief='flat', padx=4, cursor='hand2',
                  command=_rebuild_list).pack(side='right')

    def _rebuild_list():
        for w in list_inner.winfo_children():
            w.destroy()
        custom = _load_custom_descs()

        # ── MY SAVED ─────────────────────────────────────────────────────
        my_lbl_row = tk.Frame(list_inner, bg='#0d1117')
        my_lbl_row.pack(fill='x', padx=10, pady=(8, 3))
        tk.Label(my_lbl_row, text="MY SAVED",
                 font=('Helvetica', 10, 'bold'), bg='#0d1117', fg='#e040fb').pack(side='left')

        if not custom:
            hint_bg = '#0b0e18'
            hint = tk.Frame(list_inner, bg=hint_bg)
            hint.pack(fill='x', padx=6, pady=(0, 6))
            tk.Label(hint,
                     text="Type your description above, then click\n"
                          "\"+  Add to My List\" to save it here.",
                     font=('Helvetica', 10), bg=hint_bg, fg='#6a7890',
                     justify='left').pack(padx=14, pady=10, anchor='w')
        else:
            for desc in custom:
                row = tk.Frame(list_inner, bg='#16111e', cursor='hand2')
                row.pack(fill='x', pady=1)

                dot = tk.Label(row, text="  ●",
                               font=('Helvetica', 10), bg='#16111e', fg='#e040fb')
                dot.pack(side='left', pady=6)
                dot.bind('<Button-1>', lambda e, d=desc: _use_desc(d))

                lbl = tk.Label(row, text=desc,
                               font=('Helvetica', 10), bg='#16111e', fg='#e8c0ff',
                               anchor='w', cursor='hand2')
                lbl.pack(side='left', fill='x', expand=True, padx=4, pady=6)
                lbl.bind('<Button-1>', lambda e, d=desc: _use_desc(d))
                row.bind('<Button-1>', lambda e, d=desc: _use_desc(d))

                def _start_edit(d=desc, r=row):
                    _do_inline_edit(d, r)

                def _do_delete(d=desc):
                    _delete_custom_desc(d)
                    _rebuild_list()

                tk.Button(row, text=" ✎ ",
                          font=('Helvetica', 11), bg='#16111e', fg='#5c9fff',
                          activebackground='#1e2a3a', activeforeground='#90c8ff',
                          relief='flat', cursor='hand2',
                          command=_start_edit).pack(side='right', padx=(0, 2), pady=4)
                tk.Button(row, text=" ✕ ",
                          font=('Helvetica', 11), bg='#16111e', fg='#ff5c7a',
                          activebackground='#2a1520', activeforeground='#ff90a0',
                          relief='flat', cursor='hand2',
                          command=_do_delete).pack(side='right', pady=4)

        # ── STANDARD CATALOG divider ──────────────────────────────────────
        div = tk.Frame(list_inner, bg='#0d1117')
        div.pack(fill='x', padx=10, pady=(10, 4))
        tk.Frame(div, bg='#2a3248', height=1).pack(
            fill='x', side='left', expand=True, pady=7)
        tk.Label(div, text="  STANDARD CATALOG  ",
                 font=('Helvetica', 9, 'bold'), bg='#0d1117', fg='#5c6680').pack(side='left')
        tk.Frame(div, bg='#2a3248', height=1).pack(
            fill='x', side='left', expand=True, pady=7)

        # ── Catalog rows ──────────────────────────────────────────────────
        for cat_desc in _CANONICAL_DESCS:
            cat_row = tk.Frame(list_inner, bg='#0d1117', cursor='hand2')
            cat_row.pack(fill='x')
            tk.Frame(cat_row, bg='#131820', height=1).pack(fill='x')
            cat_lbl = tk.Label(cat_row, text=cat_desc,
                               font=('Helvetica', 10), bg='#0d1117', fg='#9aaabe',
                               anchor='w', cursor='hand2')
            cat_lbl.pack(fill='x', padx=12, pady=6)
            cat_lbl.bind('<Button-1>', lambda e, d=cat_desc: _use_desc(d))
            cat_row.bind('<Button-1>', lambda e, d=cat_desc: _use_desc(d))

            def _make_hover(r, lbl):
                def _on(e):
                    r.configure(bg='#141c2e')
                    lbl.configure(bg='#141c2e', fg='#e8eeff')
                def _off(e):
                    r.configure(bg='#0d1117')
                    lbl.configure(bg='#0d1117', fg='#9aaabe')
                r.bind('<Enter>', _on);   r.bind('<Leave>', _off)
                lbl.bind('<Enter>', _on); lbl.bind('<Leave>', _off)
            _make_hover(cat_row, cat_lbl)

        # ── Resize canvas ─────────────────────────────────────────────────
        n_custom = len(custom) if custom else 1
        n_total  = n_custom + len(_CANONICAL_DESCS) + 3
        new_h    = min(n_total * 34 + 20, 260)
        list_canvas.configure(height=new_h)
        if n_total * 34 + 20 > 260:
            list_sb.pack(side='right', fill='y')
        else:
            list_sb.pack_forget()

        list_inner.update_idletasks()
        list_canvas.configure(scrollregion=list_canvas.bbox('all'))

    _rebuild_list()

    # Preview row
    tk.Frame(card, bg='#252c3a', height=1).pack(fill='x', padx=14, pady=(10, 0))
    prev_row = tk.Frame(card, bg='#181c24')
    prev_row.pack(fill='x', padx=14, pady=(10, 12))
    tk.Label(prev_row, text="Preview:",
             font=('Helvetica', 10), bg='#181c24', fg='#5c6680').pack(side='left')
    preview_c = tk.Canvas(prev_row, width=48, height=48,
                          bg='#0d1117', highlightthickness=0)
    preview_c.pack(side='left', padx=10)
    preview_lbl = tk.Label(prev_row, text="",
                           font=('Helvetica', 11, 'bold'),
                           bg='#181c24', fg='#e0e6f0')
    preview_lbl.pack(side='left')

    # ── Refresh: redraw everything for the active type ────────────────────
    def refresh():
        idx  = active_idx.get()
        sel  = selections[idx]
        shp  = sel['shape']
        clr  = sel['color']
        chex = COLOR_HEX[clr]

        for c, lbl, name, frame in shape_widgets:
            active = (name == shp)
            draw_shape(c, name, chex if active else '#2a3040', size=40)
            c.configure(highlightbackground='#e040fb' if active else '#1e2330')
            lbl.configure(fg='#e0e6f0' if active else '#3a4254')

        for c, lbl, name, frame in color_widgets:
            active = (name == clr)
            c.configure(highlightbackground='#ffffff' if active else '#1e2330',
                        highlightthickness=3 if active else 2)
            lbl.configure(fg='#e0e6f0' if active else '#3a4254')

        _show_placeholder()
        draw_shape(preview_c, shp, chex, size=48)
        preview_lbl.configure(text=f"{shp}  ·  {clr}")

    def set_shape(shape):
        selections[active_idx.get()]['shape'] = shape
        refresh()

    def set_color(color):
        selections[active_idx.get()]['color'] = color
        refresh()

    for c, lbl, name, frame in shape_widgets:
        for w in (c, lbl, frame):
            w.bind('<Button-1>', lambda e, s=name: set_shape(s))

    for c, lbl, name, frame in color_widgets:
        for w in (c, lbl, frame):
            w.bind('<Button-1>', lambda e, col=name: set_color(col))

    # ── Save / Cancel ─────────────────────────────────────────────────────
    tk.Frame(dlg, bg='#252c3a', height=1).pack(fill='x', padx=16)
    btn_row = tk.Frame(dlg, bg='#111419')
    btn_row.pack(fill='x', padx=16, pady=12)

    def save_config():
        import pathlib, threading
        n = num_types.get()
        # Types the GUI manages (A through the nth letter, e.g. A-E for n=5)
        gui_types = {chr(65 + i) for i in range(n)}
        config = [
            {"type":        chr(65 + i),
             "shape":       selections[i]['shape'],
             "color":       selections[i]['color'],
             "description": selections[i]['description'].strip()}
            for i in range(n)
        ]
        # Preserve entries for types not managed by this GUI (AW, W, P, etc.)
        # so that the canonical Rossmann type table is never silently truncated.
        dest = pathlib.Path.home() / "ai-lighting" / "lightingai_typeconfig.json"
        if dest.exists():
            try:
                for _e in json.loads(dest.read_text()):
                    if _e.get("type") not in gui_types:
                        config.append(_e)
            except Exception:
                pass
        payload = json.dumps(config, ensure_ascii=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(payload)
        # Also write to /tmp/ for backward compat with older loaded LISP sessions
        try:
            pathlib.Path("/tmp/lightingai_typeconfig.json").write_text(payload)
        except Exception:
            pass

        # Regenerate commands.lsp with the new symbol config.
        # --regenerate is now floor-plan-aware: it reads lightingai_origin.json
        # (written by LIGHTINGAI_GRID) and picks only jobs for that floor plan,
        # so it cannot accidentally use data from a different drawing.
        bridge = pathlib.Path(__file__).parent / "lightingai_bridge.py"
        def _regen():
            try:
                subprocess.run(
                    [sys.executable, str(bridge), "--regenerate"],
                    timeout=60, capture_output=True
                )
            except Exception:
                pass
        threading.Thread(target=_regen, daemon=True).start()

        dlg.destroy()

    tk.Button(btn_row, text="Save & Apply",
              font=('Helvetica', 12, 'bold'),
              bg='#e040fb', fg='#111419',
              activebackground='#c020d0', activeforeground='#111419',
              relief='flat', padx=20, pady=8, cursor='hand2',
              command=save_config).pack(side='right', padx=(8, 0))
    tk.Button(btn_row, text="Cancel",
              font=('Helvetica', 12),
              bg='#1e2330', fg='#8892a4',
              relief='flat', padx=16, pady=8, cursor='hand2',
              command=dlg.destroy).pack(side='right')

    # ── Initialise ────────────────────────────────────────────────────────
    rebuild_tabs()


# ── Window setup ──────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("LightingAI")
root.configure(bg='#111419')
root.resizable(False, False)

root.update_idletasks()
sw = root.winfo_screenwidth()
root.geometry(f"296x600+{sw - 316}+44")

root.attributes('-topmost', True)
root.lift()
root.focus_force()

BG     = '#111419'
CARD   = '#181c24'
HOVER  = '#1e2330'
BRIGHT = '#e0e6f0'
MUTED  = '#5c6680'
BORDER = '#252c3a'


# ── Header ────────────────────────────────────────────────────────────────────
hdr = tk.Frame(root, bg='#181c24')
hdr.pack(fill='x')
tk.Label(hdr, text="LightingAI",
         font=('Helvetica', 13, 'bold'),
         bg='#181c24', fg='#e040fb',
         padx=16, pady=10, anchor='w').pack(side='left')
tk.Label(hdr, text="MIKA80-E · Rossmann",
         font=('Helvetica', 10),
         bg='#181c24', fg=MUTED, pady=10).pack(side='left')
tk.Frame(root, bg=BORDER, height=1).pack(fill='x')


# ── Card factory ──────────────────────────────────────────────────────────────
def make_card(step_num: str, title: str, desc: str,
              action, accent: str) -> None:
    """action is either a command string or a callable (for the config dialog)."""

    outer = tk.Frame(root, bg=BG)
    outer.pack(fill='x', padx=10, pady=(7, 0))

    tk.Frame(outer, bg=accent, width=5).pack(side='left', fill='y')

    card = tk.Frame(outer, bg=CARD, cursor='hand2')
    card.pack(side='left', fill='both', expand=True)

    n_lbl = tk.Label(card, text=step_num,
                     font=('Helvetica', 11, 'bold'),
                     bg=CARD, fg=accent, padx=12, pady=10, anchor='w')
    n_lbl.pack(fill='x')

    t_lbl = tk.Label(card, text=title,
                     font=('Helvetica', 13, 'bold'),
                     bg=CARD, fg=BRIGHT, padx=12, anchor='w')
    t_lbl.pack(fill='x')

    d_lbl = tk.Label(card, text=desc,
                     font=('Helvetica', 10),
                     bg=CARD, fg=MUTED, padx=20, pady=6,
                     anchor='w', justify='left', wraplength=240)
    d_lbl.pack(fill='x')

    all_w = [card, n_lbl, t_lbl, d_lbl]

    def set_bg(bg):
        for w in all_w:
            try: w.configure(bg=bg)
            except tk.TclError: pass

    def on_enter(e): set_bg(HOVER)

    def on_leave(e):
        rx, ry = card.winfo_rootx(), card.winfo_rooty()
        if not (rx <= card.winfo_pointerx() <= rx + card.winfo_width() and
                ry <= card.winfo_pointery() <= ry + card.winfo_height()):
            set_bg(CARD)

    def on_click(e=None):
        if callable(action):
            action()
        else:
            send_cmd(action)

    for w in all_w:
        w.bind('<Enter>',    on_enter)
        w.bind('<Leave>',    on_leave)
        w.bind('<Button-1>', on_click)


# ── Four workflow steps ───────────────────────────────────────────────────────
make_card(
    '1', 'Analyse Floor Plan',
    'Pick a DWG / PDF, run the AI pipeline,\nand view results + export links here.',
    open_analysis_dialog, '#2196f3'
)

make_card(
    '2', 'Draw Grid',
    'Auto-detect the store outline and draw\nthe ceiling grid at your chosen pitch.',
    open_grid_dialog, '#7b8ba8'
)

make_card(
    '3', 'Configure Symbols  ★',
    'Click shapes and colours for each light type.\nOpens a visual picker — no typing needed.',
    open_config_dialog, '#e040fb'
)

make_card(
    '4', 'Place Lights',
    'Insert all luminaire symbols,\nlegend, and title block into the drawing.',
    'LIGHTINGAI_PLACE', '#4caf50'
)

# ── Utility row ───────────────────────────────────────────────────────────────
tk.Frame(root, bg=BORDER, height=1).pack(fill='x', padx=10, pady=(10, 0))
util_row = tk.Frame(root, bg=BG)
util_row.pack(fill='x', padx=10, pady=6)

for label, cmd in [('Clear All', 'LIGHTINGAI_CLEAR'), ('Status', 'LIGHTINGAI_STATUS')]:
    b = tk.Button(util_row, text=label,
                  font=('Helvetica', 10),
                  bg='#1e2330', fg='#8892a4',
                  activebackground='#252c3a', activeforeground=BRIGHT,
                  relief='flat', bd=0, padx=10, pady=6,
                  cursor='hand2',
                  command=lambda c=cmd: send_cmd(c))
    b.pack(side='left', fill='x', expand=True, padx=(0, 4))

# ── Luminaire Schedule panel ──────────────────────────────────────────────────
tk.Frame(root, bg=BORDER, height=1).pack(fill='x', padx=10, pady=(6, 0))

sched_hdr = tk.Frame(root, bg=BG)
sched_hdr.pack(fill='x', padx=10, pady=(6, 0))
tk.Label(sched_hdr, text='Luminaire Schedule',
         font=('Helvetica', 11, 'bold'), bg=BG, fg=BRIGHT).pack(side='left')

_sched_refresh_btn = tk.Button(sched_hdr, text='↻',
                               font=('Helvetica', 11), bg=BG, fg=MUTED,
                               relief='flat', bd=0, padx=6,
                               activebackground=BG, activeforeground=BRIGHT,
                               cursor='hand2')
_sched_refresh_btn.pack(side='right')

sched_body = tk.Frame(root, bg=BG)
sched_body.pack(fill='x', padx=10, pady=(2, 0))

# Placeholder label — replaced by rows once summary.json exists
_sched_placeholder = tk.Label(sched_body,
    text='No data yet — place lights to see the schedule.',
    font=('Helvetica', 9), bg=BG, fg='#2e364a', anchor='w')
_sched_placeholder.pack(fill='x')

_sched_rows: list = []
_sched_last_mtime: list = [0.0]   # mutable cell for closure


def _build_sched_rows(summary: dict) -> None:
    for w in _sched_rows:
        try: w.destroy()
        except Exception: pass
    _sched_rows.clear()

    by_type = summary.get("by_type", [])
    for row in by_type:
        t     = row.get("type", "?")
        desc  = row.get("description", "")
        count = row.get("count", 0)
        watt  = row.get("watt_total", 0)
        # Truncate description to fit panel width
        if len(desc) > 28:
            desc = desc[:26] + "…"
        line = f"{t}  {desc:<28}  {count:>3}×  {watt:>6.0f} W"
        lbl = tk.Label(sched_body, text=line,
                       font=('Courier', 9), bg=BG, fg=MUTED, anchor='w')
        lbl.pack(fill='x')
        _sched_rows.append(lbl)

    # Totals line
    total_c = summary.get("total_count", 0)
    total_w = summary.get("total_watt", 0)
    area    = summary.get("floor_area_m2")
    wpm2    = summary.get("watt_per_m2")
    tot1 = f"Total: {total_c} fixtures    {total_w:.0f} W"
    lbl1 = tk.Label(sched_body, text=tot1,
                    font=('Helvetica', 9, 'bold'), bg=BG, fg=BRIGHT, anchor='w')
    lbl1.pack(fill='x', pady=(2, 0))
    _sched_rows.append(lbl1)

    if area and wpm2 is not None:
        tot2 = f"Floor: {area:.0f} m²    Lighting load: {wpm2:.2f} W/m²"
        lbl2 = tk.Label(sched_body, text=tot2,
                        font=('Helvetica', 9), bg=BG, fg=MUTED, anchor='w')
        lbl2.pack(fill='x')
        _sched_rows.append(lbl2)


def refresh_schedule() -> None:
    if not _SUMMARY_FILE.exists():
        return
    try:
        mtime = _SUMMARY_FILE.stat().st_mtime
        if mtime == _sched_last_mtime[0]:
            return
        _sched_last_mtime[0] = mtime
        summary = json.loads(_SUMMARY_FILE.read_text(encoding='utf-8'))
        _sched_placeholder.pack_forget()
        _build_sched_rows(summary)
    except Exception:
        pass


def _poll_schedule():
    refresh_schedule()
    root.after(5000, _poll_schedule)


_sched_refresh_btn.configure(command=refresh_schedule)
_poll_schedule()

# ── Footer ────────────────────────────────────────────────────────────────────
tk.Frame(root, bg=BORDER, height=1).pack(fill='x', padx=10, pady=(6, 0))
tk.Label(root, text='Click a step · type LAI to reopen if closed',
         font=('Helvetica', 9), bg=BG, fg='#2e364a', pady=8).pack()

root.mainloop()
