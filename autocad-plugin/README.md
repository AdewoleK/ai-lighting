# LightingAI AutoCAD Plugin

AutoCAD .NET plugin that places MIKA80-E luminaires directly into an open DWG
by calling the **lighting-ai Python backend** — no round-trip, no reconstruction,
the original floor plan geometry is never touched.

---

## How it works

```
Designer in AutoCAD
  1. Run  LIGHTINGAI_SETUP  → pick Startmaß point in the drawing
  2. Run  LIGHTINGAI_PLACE  → plugin uploads DWG, calls pipeline, places lights
  3. Done — luminaires live on layer AI-LUMINAIRES inside the same DWG
```

The Python API returns a list of `{x, y, type, product_code}` objects.
The plugin creates `MIKA80E-A` … `MIKA80E-E` block definitions and inserts them
as standard AutoCAD `INSERT` entities with `ATTRIB` values attached.

---

## Prerequisites

| Requirement | Version |
|-------------|---------|
| AutoCAD | 2020 – 2025 (R23–R25), 64-bit |
| .NET SDK | 8.0+ (for build); .NET 4.8 runtime ships with AutoCAD |
| lighting-ai backend | running on `http://localhost:8000` (or any URL) |

---

## Build

### 1. Point at your AutoCAD installation

The project needs the four AutoCAD DLLs (`acmgd.dll`, `acdbmgd.dll`,
`AcCoreMgd.dll`, `AdWindows.dll`).  Set `AcadDir` to match your install:

```powershell
# Example: AutoCAD 2024
dotnet build -p:AcadDir="C:\Program Files\Autodesk\AutoCAD 2024"
```

Alternatively edit `LightingAI.Plugin.csproj` and set `<AcadDir>` directly.

### 2. Build in Release mode

```powershell
cd autocad-plugin
dotnet build -c Release
```

The post-build step automatically copies the DLL into `bundle\Contents\`.

---

## Install

### Option A — ApplicationPlugins folder (recommended)

Copy the entire `LightingAI.bundle` folder into:

```
%APPDATA%\Autodesk\ApplicationPlugins\
```

Restart AutoCAD. The plugin loads automatically on startup.

### Option B — NETLOAD (per-session, for testing)

```
Command: NETLOAD
→ browse to  autocad-plugin\bundle\Contents\LightingAI.Plugin.dll
```

---

## Commands

| Command | Description |
|---------|-------------|
| `LIGHTINGAI_SETUP` | Pick the **Startmaß Rasterdecke** grid origin point. Writes a POINT entity to layer `AI-GRID-ORIGIN` and stores the coordinate in the drawing's Named Objects Dictionary. Run this once per drawing. |
| `LIGHTINGAI_PLACE` | Runs the full pipeline. Prompts for project name / customer / concept, saves the DWG to a temp file, uploads it to the backend, polls until done, then places luminaire blocks and draws the legend + title block. Safe to re-run — removes previous AI entities first. |
| `LIGHTINGAI_CLEAR` | Removes every entity on an `AI-*` layer. Original floor plan untouched. |
| `LIGHTINGAI_SETTINGS` | Change the API base URL for this session (default `http://localhost:8000`). |

A **"Lighting AI"** ribbon tab with large buttons for all four commands is added
automatically on load.

---

## Layer structure written into the DWG

| Layer | ACI colour | Content |
|-------|------------|---------|
| `AI-LUMINAIRES` | 6 (magenta) | Block INSERTs for all luminaires |
| `AI-GRID-ORIGIN` | 2 (yellow) | Startmaß POINT + annotation text |
| `AI-LEGEND` | 7 | Leuchtenlegende panel |
| `AI-TITLEBLOCK` | 7 | Schriftfeld (title block) |
| `AI-DIMENSIONS` | 7 | (reserved for future dimension chains) |
| `AI-ANNOTATIONS` | 9 | (reserved) |
| `AI-ZONES` | 3 (green) | (reserved) |

---

## Block definitions created

| Block name | Geometry | ATTDEFs |
|------------|----------|---------|
| `MIKA80E-A` | 128 mm circle + dot + cross, ACI 6 | `TYPE`, `PRODUCT` |
| `MIKA80E-B` | same, ACI 1 | `TYPE`, `PRODUCT` |
| `MIKA80E-C` | same, ACI 4 | `TYPE`, `PRODUCT` |
| `MIKA80E-D` | same, ACI 2 | `TYPE`, `PRODUCT` |
| `MIKA80E-E` | same, ACI 5 | `TYPE`, `PRODUCT` |

The `ATTRIB` values (`TYPE` = "A"…"E", `PRODUCT` = full product code) are attached
to every INSERT, making the drawing BOM-extractable directly from AutoCAD.

---

## AutoCAD 2025 (.NET 8) note

AutoCAD 2025 moved from .NET Framework 4.8 to .NET 8.  To rebuild for 2025:

1. Edit `LightingAI.Plugin.csproj` → change `<TargetFramework>net48</TargetFramework>`
   to `<TargetFramework>net8.0-windows</TargetFramework>`
2. Update `<AcadDir>` to your AutoCAD 2025 path
3. Rebuild

The C# source code itself requires no changes.

---

## Starting the Python backend

```bash
# From the repo root
pip install fastapi uvicorn ezdxf pymupdf shapely scikit-learn openpyxl jinja2
uvicorn services.api.main:app --host 0.0.0.0 --port 8000 --reload
```

The plugin expects the API at `http://localhost:8000` by default.
Change it per-session with `LIGHTINGAI_SETTINGS`.
