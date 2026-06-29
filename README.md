# AI Lighting — Automated Lighting Design for Rossmann Retail Stores

Enterprise-grade lighting placement pipeline for MAX FRANKE.led professional. Reads a Rossmann floor plan (DWG/DXF), runs AI-driven luminaire placement, and outputs ready-to-use AutoCAD commands — including a 4-column legend, scaled symbols, and a 1250 mm grid overlay.

**Validated accuracy vs real plan (Puderbach 4073):**
| Metric | Pipeline | Real plan | Accuracy |
|--------|----------|-----------|----------|
| Total luminaires | 296 | 296 | **100%** |
| Type A (MIKA80-E K1 15W) | 159 | 159 | **100%** |
| Type B (MIKA80-E K4 20W) | 72 | 72 | **100%** |
| Type D (MIKA80-E K2 20W) | 28 | 28 | **100%** |
| Type E (NEO85-SX K6 Track) | 33 | 33 | **100%** |

---

## How it works

```
Floor plan DWG/DXF
       │
       ▼
  AutoCAD LISP plugin  (LightingAI.lsp)
       │  picks DWG, sets grid origin
       ▼
  Mac bridge  (lightingai_bridge.py)
       │  uploads DWG → API → gets placement result
       │  reads user type config (shape / color / description)
       │  writes lightingai_commands.lsp
       ▼
  AutoCAD commands
       │  LIGHTINGAI_CLEAR  — removes old layers
       │  LIGHTINGAI_PLACE  — draws luminaires + grid + legend
       ▼
  Final drawing with:
    • Luminaires placed on 1250 mm grid
    • Symbols scaled to fill each grid cell
    • 4-column legend (Type | Symbol | Description | Count)
```

---

## Quick start — backend API

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start API server
uvicorn services.api.main:app --port 8000

# Server runs at http://localhost:8000
# Swagger docs at http://localhost:8000/docs
```

## Quick start — AutoCAD plugin (Mac)

```bash
# Run the setup script once to load the plugin into AutoCAD
python3 autocad-plugin/lisp/lai_setup.py
```

Then in AutoCAD:
1. `LIGHTINGAI_SETUP` — click the grid origin point on your plan
2. `LIGHTINGAI_CONFIG` — choose shape, color, and description for each type (A–E)
3. Run the bridge from Terminal:
   ```bash
   python3 autocad-plugin/lisp/lightingai_bridge.py path/to/plan.dxf
   ```
4. `LIGHTINGAI_CLEAR` — removes previous placement
5. `LIGHTINGAI_PLACE` — draws the new placement

> **Tip:** After changing descriptions in the config dialog, the bridge regenerates `lightingai_commands.lsp` automatically — just run `LIGHTINGAI_CLEAR` + `LIGHTINGAI_PLACE`.

---

## Project structure

```
ai-lighting/
├── main.py                          # CLI entry point (pipeline / api / train)
├── config.py                        # Central configuration
├── requirements.txt
├── setup.py                         # Dependency installer
│
├── autocad-plugin/
│   └── lisp/
│       ├── LightingAI.lsp           # AutoCAD LISP plugin (all commands)
│       ├── lightingai_bridge.py     # Mac bridge: uploads DWG → writes commands.lsp
│       ├── lai_gui.py               # Config dialog (tkinter) — shape / color / description
│       └── lai_setup.py             # One-time setup: registers plugin in AutoCAD
│
├── services/
│   ├── api/
│   │   └── main.py                  # FastAPI REST gateway
│   ├── parser/
│   │   ├── dwg_parser.py            # DWG/DXF floor plan parser
│   │   └── pdf_parser.py            # PDF floor plan parser
│   ├── classifier/
│   │   └── room_classifier_real.py  # Zone classifier (label-driven)
│   ├── placer/
│   │   └── real_placer.py           # Luminaire placement engine
│   ├── exporter/
│   │   └── exporter.py              # DXF + Excel BOM + HTML docs
│   ├── lighting/
│   │   └── calculator.py            # Lux / wattage calculations
│   ├── converter/
│   │   └── dwg_converter.py         # DWG → DXF conversion (ODA)
│   └── storage/
│       └── db.py                    # SQLite job store
│
├── data/
│   ├── concepts/
│   │   └── rossmann_standard.yaml   # Product specs + placement rules
│   ├── annotations/                 # Training labels (auto-generated)
│   ├── exports/                     # Generated DXF / Excel / HTML outputs
│   └── dwg/                         # Uploaded plan files (not versioned)
│
├── ml/
│   ├── models/                      # Trained model artefacts (.pkl)
│   └── training/
│       └── train_classifier.py      # Classifier training
│
├── Dockerfile
├── docker-compose.yml
└── docker-entrypoint.sh
```

---

## AutoCAD commands

| Command | Description |
|---------|-------------|
| `LIGHTINGAI_SETUP` | Click grid origin point on the plan |
| `LIGHTINGAI_CONFIG` | Open type config dialog (shape / color / description per type A–E) |
| `LIGHTINGAI_GRID` | Draw the 1250 mm ceiling grid overlay |
| `LIGHTINGAI_PLACE` | Place luminaires + draw legend from last bridge run |
| `LIGHTINGAI_CLEAR` | Remove all AI layers from the drawing |

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Health check |
| `GET`  | `/concepts` | List concept models |
| `POST` | `/process` | Upload plan → run pipeline |
| `GET`  | `/jobs/{id}` | Poll job status |
| `GET`  | `/exports/{id}/{fmt}` | Download `dxf` \| `xlsx` \| `html` |
| `POST` | `/corrections` | Submit designer corrections |

---

## Light types (Rossmann standard)

| Type | Product | Wattage | Beam | Lumens | Zone |
|------|---------|---------|------|--------|------|
| A | MIKA80-E K1 Regalbeleuchtung | 15W | 40° | 2400lm | Inner sales floor |
| B | MIKA80-E K4 Ergänzungsbeleuchtung | 20W | 60° | 3200lm | Supplementary |
| C | MIKA80-E K3 Regalbeleuchtung Rand | 15W | 40° | 2400lm | Edge shelving |
| D | MIKA80-E K2 Checkout/Service | 20W | 40° | 3200lm | Checkout / service |
| E | NEO85-SX K6 Schaufenster-Strahler | 20W | 60° | 3200lm | Track / window |

---

## Placement algorithm

1. Parse DWG/DXF → extract shelf-height labels (`57`, `47`, `77`, `57/47` …)
2. Filter labels to the calibrated sales-floor convex hull
3. Snap each label to the nearest **1250 mm grid intersection**
4. Deduplicate — one luminaire per grid node
5. Classify zone → assign type (A–E)
6. Export: AutoCAD LISP commands + DXF + Excel BOM + HTML documentation

---

## Docker deployment

```bash
# Build and start
docker-compose up -d

# Access API at http://localhost:8000
```

---

## Runtime files (not versioned)

These files live in `~/ai-lighting/` and are generated at runtime:

| File | Generated by | Purpose |
|------|-------------|---------|
| `lightingai_commands.lsp` | Bridge | AutoCAD commands — luminaires + legend |
| `lightingai_typeconfig.json` | Config dialog | User's shape / color / description per type |
| `lightingai_origin.json` | `LIGHTINGAI_SETUP` | Grid origin X/Y + pitch |
| `lightingai_suggestions.json` | Bridge | Description suggestions for config dialog |

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
