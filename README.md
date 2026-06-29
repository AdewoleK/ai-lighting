# AI Lighting — Automated Lighting Design for Rossmann Retail Stores

Enterprise lighting placement pipeline for MAX FRANKE.led professional.
Upload a Rossmann floor plan DWG, get AutoCAD-ready luminaire placements with a 4-column legend, scaled symbols, and a 1250 mm ceiling grid — in under 2 minutes.

**Validated accuracy (Puderbach 4073):**
| Type | Product | Pipeline | Real plan |
|------|---------|----------|-----------|
| A | MIKA80-E K1 Regalbeleuchtung 15W 40° | 159 | 159 ✓ |
| B | MIKA80-E K4 Ergänzungsbeleuchtung 20W 60° | 72 | 72 ✓ |
| C | MIKA80-E K3 Regalbeleuchtung Rand 15W 40° | 4 | 4 ✓ |
| D | MIKA80-E K2 Checkout/Service 20W 40° | 28 | 28 ✓ |
| E | NEO85-SX K6 Schaufenster-Strahler 20W 60° | 33 | 33 ✓ |
| **Total** | | **296** | **296 — 100%** |

---

## How the system works

```
┌─────────────────────────────────────────────────────────────┐
│  Terminal                                                   │
│  uvicorn services.api.main:app --port 8000   (keep running) │
└─────────────────────┬───────────────────────────────────────┘
                      │ API at http://localhost:8000
                      │
┌─────────────────────▼───────────────────────────────────────┐
│  AutoCAD  →  type LAI  →  Control Panel opens              │
│                                                             │
│  [ Step 1: Configure Lights ]  ←── choose shape / color /  │
│                                     description per type    │
│                                                             │
│  [ Step 2: Select DWG + Run ]  ←── file picker opens,      │
│                                     bridge runs in Terminal │
│                                     uploads DWG to API,     │
│                                     writes commands.lsp     │
│                                                             │
│  [ Step 3: Place Lights     ]  ←── LIGHTINGAI_CLEAR        │
│                                     LIGHTINGAI_PLACE        │
└─────────────────────────────────────────────────────────────┘
                      │
                      ▼
         AutoCAD drawing with:
           • Luminaires placed on 1250 mm grid
           • Symbols scaled to fill each grid cell
           • 4-column legend: Type | Symbol | Description | Count
```

---

## One-time setup

### 1. Start the backend API

```bash
cd ~/ai-lighting
pip install -r requirements.txt        # first time only
uvicorn services.api.main:app --port 8000
```

Keep this Terminal window open — the API must be running whenever you use the plugin.

### 2. Build the AutoCAD panel app

```bash
python3 ~/ai-lighting/autocad-plugin/lisp/lai_setup.py
```

This creates `~/LightingAI.app` — the macOS app bundle that AutoCAD launches when you type `LAI`.

### 3. Load the plugin into AutoCAD

In AutoCAD:
1. Type `APPLOAD`
2. Browse to `~/ai-lighting/autocad-plugin/lisp/LightingAI.lsp` and load it
3. Type `LAI` — the control panel should appear

> This only needs to be done once. AutoCAD remembers the loaded file across sessions.

---

## Everyday workflow

### Step 1 — Open the control panel

In AutoCAD, type:
```
LAI
```

The LightingAI control panel opens in a separate window.

---

### Step 2 — Configure light types (optional, first run or when changing)

Click **Configure Lights** in the panel.

A dialog opens where you set for each type (A–E):
- **Shape** — Circle, Square, Diamond, Triangle, Cross, or 5 more
- **Color** — Magenta, Red, Cyan, Yellow, Blue, etc.
- **Description** — choose from the dropdown or type your own (e.g. `MIKA80-E K1 Regalbeleuchtung 15W 40° 2400lm 3000K`)

Click **Save & Apply**. The commands file updates automatically — no need to re-run the bridge just for a description change.

---

### Step 3 — Run the AI pipeline

Click **Select DWG + Run** in the panel.

A file browser opens — select your floor plan DWG. A Terminal window opens automatically and shows:

```
[LightingAI] Uploading plan.dxf …
[LightingAI] Job abc123 queued — polling…
  [processing]  Parsing plan…
  [processing]  Exporting…
  [done]  Pipeline complete

[LightingAI] Pipeline complete:
  Total luminaires : 296
  Total wattage    : 5105 W
  Type A/B/C/D/E   : 159/72/4/28/33

[LightingAI] ✓  All done!
   Now go to AutoCAD and type:  LIGHTINGAI_PLACE
```

---

### Step 4 — Place the lights in AutoCAD

Back in AutoCAD, type:
```
LIGHTINGAI_CLEAR
```
then:
```
LIGHTINGAI_PLACE
```

`LIGHTINGAI_CLEAR` removes any previous AI placement. `LIGHTINGAI_PLACE` draws all luminaires, the ceiling grid, and the legend.

---

## Updating descriptions without re-running the pipeline

If you only want to change the description text in the legend (no new DWG):

1. Click **Configure Lights** → change descriptions → **Save & Apply**
2. In AutoCAD: `LIGHTINGAI_CLEAR` → `LIGHTINGAI_PLACE`

The commands file regenerates from the stored result instantly — no upload needed.

---

## Project structure

```
ai-lighting/
│
├── autocad-plugin/
│   └── lisp/
│       ├── LightingAI.lsp           # AutoCAD plugin (LAI, LIGHTINGAI_PLACE, LIGHTINGAI_CLEAR)
│       ├── lightingai_bridge.py     # Bridge: uploads DWG → API → writes commands.lsp
│       ├── lai_gui.py               # Control panel + config dialog (tkinter)
│       └── lai_setup.py             # One-time setup: builds ~/LightingAI.app
│
├── services/
│   ├── api/main.py                  # FastAPI REST gateway (port 8000)
│   ├── parser/dwg_parser.py         # DWG/DXF floor plan parser
│   ├── parser/pdf_parser.py         # PDF floor plan parser
│   ├── classifier/room_classifier_real.py   # Zone classifier
│   ├── placer/real_placer.py        # Luminaire placement engine
│   ├── exporter/exporter.py         # DXF + Excel BOM + HTML docs
│   ├── lighting/calculator.py       # Lux / wattage calculations
│   ├── converter/dwg_converter.py   # DWG → DXF conversion
│   └── storage/db.py                # SQLite job store
│
├── data/
│   ├── concepts/rossmann_standard.yaml   # Product specs + placement rules
│   ├── annotations/                      # Training labels
│   └── exports/                          # Generated outputs (not versioned)
│
├── ml/
│   └── training/train_classifier.py      # Classifier training
│
├── main.py                          # CLI: pipeline / api / train / validate
├── config.py                        # Central configuration
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## Runtime files (auto-generated, not versioned)

These files live in `~/ai-lighting/` and are created at runtime:

| File | Created by | Contains |
|------|-----------|---------|
| `lightingai_commands.lsp` | Bridge | AutoCAD commands — luminaires + grid + legend |
| `lightingai_typeconfig.json` | Config dialog | Shape / color / description per type A–E |
| `lightingai_origin.json` | Bridge | Grid origin X/Y + pitch |
| `lightingai_suggestions.json` | Bridge | Description suggestions for the dropdown |

---

## Docker deployment (production)

```bash
docker-compose up -d
# API available at http://localhost:8000
```

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Health check |
| `GET`  | `/concepts` | List concept models |
| `POST` | `/process` | Upload DWG → run pipeline |
| `GET`  | `/jobs/{id}` | Poll job status |
| `GET`  | `/exports/{id}/{fmt}` | Download `dxf` \| `xlsx` \| `html` |
| `POST` | `/corrections` | Submit designer corrections |

---

## Dependencies

| Library | Purpose |
|---------|---------|
| `ezdxf` | DWG/DXF read/write |
| `shapely` | Polygon geometry |
| `scikit-learn` | Zone classifier |
| `fastapi` | REST API |
| `openpyxl` | Excel BOM |
| `jinja2` | HTML documentation |
| `numpy` | Numerical operations |
| `requests` | Bridge → API communication |
