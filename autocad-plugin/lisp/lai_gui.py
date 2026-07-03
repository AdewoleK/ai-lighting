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


# ── Step 1: grid line-thickness + color picker ───────────────────────────────
_GRID_CFG_FILE  = pathlib.Path.home() / "ai-lighting" / "lightingai_grid_config.json"
_SUMMARY_FILE   = pathlib.Path.home() / "ai-lighting" / "lightingai_summary.json"

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

    W, H = 460, 630
    sw = dlg.winfo_screenwidth()
    sh = dlg.winfo_screenheight()
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

    # ── Number of types ──────────────────────────────────────────────────────
    n_row = tk.Frame(dlg, bg='#111419')
    n_row.pack(fill='x', padx=16, pady=(12, 4))
    tk.Label(n_row, text="How many light types?",
             font=('Helvetica', 11), bg='#111419', fg='#8892a4').pack(side='left')

    def change_n(delta):
        v = num_types.get() + delta
        if 1 <= v <= 6:
            num_types.set(v)
            rebuild_tabs()

    tk.Button(n_row, text="−", font=('Helvetica', 13, 'bold'),
              bg='#1e2330', fg='#e0e6f0', relief='flat',
              padx=10, pady=2, cursor='hand2',
              command=lambda: change_n(-1)).pack(side='left', padx=(14, 4))
    tk.Label(n_row, textvariable=num_types, font=('Helvetica', 14, 'bold'),
             bg='#111419', fg='#e040fb', width=2).pack(side='left')
    tk.Button(n_row, text="+", font=('Helvetica', 13, 'bold'),
              bg='#1e2330', fg='#e0e6f0', relief='flat',
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

    # ── Content card ─────────────────────────────────────────────────────────
    tk.Frame(dlg, bg='#252c3a', height=1).pack(fill='x', padx=16, pady=(8, 0))
    card = tk.Frame(dlg, bg='#181c24')
    card.pack(fill='both', expand=True, padx=16, pady=0)

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

    # Description combobox — suggestions loaded from last bridge run
    tk.Label(card, text="DESCRIPTION  (choose from list or type your own)",
             font=('Helvetica', 9, 'bold'), bg='#181c24', fg='#3a4254',
             anchor='w').pack(fill='x', padx=14, pady=(14, 4))

    # Canonical Rossmann product catalog — always shown in dropdown, always distinct
    _CANONICAL_DESCS = [
        "MIKA80-E K1 Regalbeleuchtung 15W 40° 2400lm 3000K",
        "MIKA80-E K4 Ergänzungsbeleuchtung 20W 60° 3200lm 3000K",
        "MIKA80-E K3 Regalbeleuchtung Rand 15W 40° 2400lm 3000K",
        "MIKA80-E K2 Checkout/Service 20W 40° 3200lm 3000K",
        "NEO85-SX K6 Schaufenster-Strahler 20W 60° 3200lm Track",
    ]
    _all_suggestions: list = _CANONICAL_DESCS

    # ttk style so the combobox matches the dark theme
    _style = ttk.Style()
    _style.configure('Dark.TCombobox',
                     fieldbackground='#0d1117', background='#1e2330',
                     foreground='#e0e6f0', selectbackground='#e040fb',
                     selectforeground='#111419', arrowcolor='#e0e6f0')

    desc_var = tk.StringVar()
    desc_combo = ttk.Combobox(card, textvariable=desc_var,
                              values=_all_suggestions,
                              font=('Helvetica', 10),
                              style='Dark.TCombobox',
                              state='normal')
    desc_combo.pack(fill='x', padx=14, ipady=4)

    def _update_combo_suggestions():
        """Refresh dropdown list — always show all 5 canonical descriptions, deduplicated."""
        current = desc_var.get().strip()
        seen_vals: set = set()
        combined: list = []
        for v in ([current] if current else []) + _CANONICAL_DESCS:
            if v and v not in seen_vals:
                combined.append(v)
                seen_vals.add(v)
        desc_combo['values'] = combined

    def _on_desc_change(*_):
        selections[active_idx.get()]['description'] = desc_var.get()

    desc_var.trace_add('write', _on_desc_change)

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

        desc_var.set(sel.get('description', ''))
        _update_combo_suggestions()
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
        config = [
            {"type":        chr(65 + i),
             "shape":       selections[i]['shape'],
             "color":       selections[i]['color'],
             "description": selections[i]['description'].strip()}
            for i in range(n)
        ]
        payload = json.dumps(config)
        dest = pathlib.Path.home() / "ai-lighting" / "lightingai_typeconfig.json"
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
root.geometry(f"296x508+{sw - 316}+44")

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
    '1', 'Draw Grid',
    'Auto-detect the store outline and draw\nthe ceiling grid at your chosen pitch.',
    open_grid_dialog, '#7b8ba8'
)

make_card(
    '2', 'Configure Symbols  ★',
    'Click shapes and colours for each light type.\nOpens a visual picker — no typing needed.',
    open_config_dialog, '#e040fb'
)

make_card(
    '3', 'Place Lights',
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
