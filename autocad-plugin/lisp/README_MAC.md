# LightingAI — AutoCAD for Mac (AutoLISP)

Since AutoCAD for Mac does not support .NET plugins, this folder provides an
**AutoLISP + Python bridge** that achieves the same result:
luminaires are placed directly into your open DWG — same drawing, same layers,
same block definitions, same title block — without any round-trip file export.

---

## Files

| File | Purpose |
|------|---------|
| `LightingAI.lsp` | AutoLISP plugin — defines four AutoCAD commands |
| `lightingai_bridge.py` | Python bridge — calls the FastAPI backend, writes the LISP placement file |

---

## One-time setup (do this once)

### 1. Install the Python dependency

Open Terminal:

```bash
pip3 install requests
```

### 2. Load the LISP file into AutoCAD

You have two options:

**Option A — Drag and drop (easiest)**
Drag `LightingAI.lsp` from Finder onto the AutoCAD drawing window.
You will see the plugin banner appear in the command line area.

**Option B — Type the load command**
In the AutoCAD command line at the bottom of the screen, click it and type:

```
(load "/Users/dextercyberlabs/ai-lighting/autocad-plugin/lisp/LightingAI.lsp")
```

Press Enter. You should see:

```
[LightingAI] ╔══════════════════════════════════════════════╗
[LightingAI] ║   LIGHTING AI — MIKA80-E Rossmann (Mac)      ║
...
```

**Option C — Auto-load on every AutoCAD startup**
In AutoCAD, go to: **Tools → Application Manager → Load Application**
Add `LightingAI.lsp` to the **Startup Suite**.
It will load automatically every time AutoCAD opens.

---

## Every-time workflow (4 steps)

```
┌─────────────────────────────────────────────────────────────┐
│  AutoCAD (Mac)            Terminal                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Step 1: LIGHTINGAI_SETUP                                   │
│  Pick the Startmaß point                                    │
│  on screen (snap to grid)                                   │
│                                                             │
│                           Step 2: run bridge.py             │
│                           python3 lightingai_bridge.py \    │
│                             /path/to/myfloor.dwg            │
│                                                             │
│  Step 3: LIGHTINGAI_PLACE                                   │
│  Luminaires appear in      ← bridge wrote the LISP file    │
│  the drawing               ← AutoCAD loads + runs it       │
│                                                             │
│  Step 4 (optional):                                         │
│  LIGHTINGAI_CLEAR          ← redo from scratch             │
└─────────────────────────────────────────────────────────────┘
```

---

## Detailed step-by-step

### Step 1 — Mark the grid origin in AutoCAD

In the AutoCAD command line, type:

```
LIGHTINGAI_SETUP
```

AutoCAD will ask you to **pick a point**. Click the intersection where the first
ceiling grid lines cross (the "Startmaß Rasterdecke"). Use the OSNAP
**Intersection** snap for precision.

Then it asks for the **grid pitch** — press Enter to accept 1250 mm (Rossmann standard).

A yellow dot and small text annotation will appear at your chosen point.

---

### Step 2 — Run the Python bridge in Terminal

Open Terminal (`Cmd + Space` → "Terminal").

```bash
python3 /Users/dextercyberlabs/ai-lighting/autocad-plugin/lisp/lightingai_bridge.py \
  "/path/to/your/floorplan.dwg"
```

With optional project details:

```bash
python3 /Users/dextercyberlabs/ai-lighting/autocad-plugin/lisp/lightingai_bridge.py \
  "/path/to/your/floorplan.dwg" \
  --project "Rossmann Hamburg EG" \
  --customer "Dirk Rossmann GmbH" \
  --concept rossmann_standard
```

You will see the pipeline running:

```
[LightingAI] Backend online.
[LightingAI] Grid origin: X=14160  Y=16400  pitch=1250 mm
[LightingAI] Uploading floorplan.dwg (842 KB)…
[LightingAI] Job a1b2c3d4 queued — polling…
  [processing]  Parsing plan…
  [processing]  Classifying zones…
  [processing]  Placing luminaires…
  [processing]  Exporting…
  [done]

[LightingAI] Pipeline complete:
  Total luminaires : 167
  Total wattage    : 2722 W
  Type A/B/C/D/E   : 106/61/0/0/0

[LightingAI] ✓  All done!
[LightingAI]    Now go to AutoCAD and type:  LIGHTINGAI_PLACE
```

The bridge wrote `/tmp/lightingai_commands.lsp` — do not close the Terminal yet.

---

### Step 3 — Place the luminaires in AutoCAD

Click back in AutoCAD. In the command line, type:

```
LIGHTINGAI_PLACE
```

AutoCAD will load the LISP commands file and execute it. You will see the
luminaire circles appear on the drawing, along with the legend panel and title block.

This usually takes 5–20 seconds depending on the number of luminaires.

---

### Step 4 — Optional: clear and redo

If you want to change settings and re-run, first clear the previous result:

```
LIGHTINGAI_CLEAR
```

Then repeat from Step 1.

---

## Commands reference

| Command | What it does |
|---------|-------------|
| `LIGHTINGAI_SETUP` | Pick the Startmaß grid origin point. Saves to `/tmp/lightingai_origin.json`. |
| `LIGHTINGAI_PLACE` | Loads `/tmp/lightingai_commands.lsp` and executes it — places all luminaires, legend, and title block. |
| `LIGHTINGAI_CLEAR` | Erases every entity on an `AI-*` layer. Original floor plan untouched. |
| `LIGHTINGAI_STATUS` | Shows whether the origin and commands files are ready. |

---

## Layers written into the drawing

| Layer | Colour | Content |
|-------|--------|---------|
| `AI-LUMINAIRES` | 6 magenta | MIKA80E-* block inserts |
| `AI-GRID-ORIGIN` | 2 yellow | Startmaß point + text |
| `AI-LEGEND` | 7 white | Leuchtenlegende panel |
| `AI-TITLEBLOCK` | 7 white | Schriftfeld |
| `AI-DIMENSIONS` | 7 white | (reserved) |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `(load ...)` gives "file not found" | Use the full absolute path; check for typos |
| `LIGHTINGAI_SETUP` — command not found | The LISP file is not loaded; repeat the load step |
| Bridge says "Cannot reach backend" | Start the Python API first: `uvicorn services.api.main:app --port 8000` |
| `LIGHTINGAI_PLACE` — commands file not found | Run the Python bridge in Terminal first (Step 2) |
| Luminaires appear at wrong location | Re-run LIGHTINGAI_SETUP and snap more precisely to the grid intersection |
| Blocks look like "?" / missing | AutoCAD did not receive the ENDBLK; close and reload the DWG |

---

## Making the load command permanent

To avoid typing `(load ...)` every time you open AutoCAD:

1. In AutoCAD, go to **Tools → Load Application**
2. Click the **Startup Suite** button (bottom-left of the dialog)
3. Click **Add** and browse to `LightingAI.lsp`
4. Click **Close**

From now on, all four `LIGHTINGAI_*` commands are available the moment AutoCAD starts.
