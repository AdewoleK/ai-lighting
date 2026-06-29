"""
lighting-ai/services/api/main.py

FastAPI gateway — full pipeline as REST API.

Endpoints:
  GET  /health
  GET  /concepts                     list available concepts
  POST /concepts                     upload a new concept YAML
  DELETE /concepts/{id}              remove a concept
  POST /process                      upload plan → run pipeline → job id
  GET  /jobs/{id}                    poll job status
  GET  /history                      list all past jobs (persistent)
  GET  /exports/{id}/{fmt}           download dxf | xlsx | pdf | html
  POST /corrections                  submit designer corrections
"""
from __future__ import annotations
import json, traceback, uuid
from pathlib import Path
from typing import Literal, Optional
import sys

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import DWG_DIR, EXPORTS_DIR, CONCEPTS_DIR, MODELS_DIR
import services.storage.db as db

app = FastAPI(title="lighting-ai", version="1.0.0",
              description="Automated Rossmann lighting design pipeline")

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

ROOT       = Path(__file__).parent.parent.parent
STATIC_DIR = ROOT / "ui" / "dist"
if STATIC_DIR.exists():
    app.mount("/assets",
              StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")


# ── Pydantic models ───────────────────────────────────────────────────────────

class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued","processing","done","error"]
    message: str = ""
    result: Optional[dict] = None

class CorrectionPayload(BaseModel):
    job_id: str
    corrections: list[dict]

class HistoryItem(BaseModel):
    job_id: str
    status: str
    filename: str
    project_name: str
    customer: str
    concept_id: str
    created_at: float
    message: str


# ── Startup ───────────────────────────────────────────────────────────────────

def _bootstrap():
    CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
    default = CONCEPTS_DIR / "rossmann_standard.yaml"
    if not default.exists():
        src = ROOT / "data/concepts/rossmann_standard.yaml"
        if src.exists():
            import shutil; shutil.copy(src, default)

_bootstrap()


# ── Pipeline worker ───────────────────────────────────────────────────────────

def _run_pipeline(job_id: str, plan_path: Path,
                  concept_id: str, project_name: str, customer: str):
    def _set(status, message):
        db.update_job(job_id, status, message)

    try:
        _set("processing", "Parsing plan…")

        from services.parser.pdf_parser import RealPlanParser
        from services.classifier.room_classifier_real import RealRoomClassifier
        from services.placer.real_placer import RealLuminairePlacer
        from services.exporter.exporter import export_dwg, export_excel, export_pdf

        plan       = RealPlanParser().parse(plan_path)
        _set("processing", "Classifying zones…")
        classified = RealRoomClassifier().classify(plan)
        _set("processing", "Placing luminaires…")
        result     = RealLuminairePlacer().place_all(plan, classified)
        _set("processing", "Exporting…")

        stem = f"{job_id}_{plan_path.stem}"
        pfx  = str(EXPORTS_DIR / stem)

        source_dxf = str(plan_path) if plan_path.suffix in ('.dxf','.dwg') else None
        dwg_out  = export_dwg(result, classified,
                              source_dxf_path=source_dxf,
                              output_path=pfx+"_luminaires.dxf",
                              project_name=project_name, customer=customer,
                              concept_id=concept_id)
        xlsx_out = export_excel(result, classified,
                                project_name=project_name, customer=customer,
                                concept_id=concept_id,
                                output_path=pfx+"_schedule.xlsx")
        pdf_out  = export_pdf(result, classified,
                              concept_id=concept_id, customer=customer,
                              project_name=project_name,
                              output_path=pfx+"_documentation")

        placed_data = [
            {"id": i, "x": round(lp.x), "y": round(lp.y),
             "rotation": lp.rotation,
             "zone_type": lp.zone_type, "lumi_type": lp.lumi_type,
             "product_code": lp.product_code, "description": lp.description,
             "wattage": lp.wattage, "lux_output": lp.lux_output,
             "mounting_type": lp.mounting_type,
             "beam_angle_deg": lp.beam_angle_deg,
             "grid_snapped": lp.grid_snapped,
             "shelf_aligned": lp.shelf_aligned}
            for i, lp in enumerate(result.placed)
        ]
        zones_data = [
            {"index": z.polygon_index, "zone_type": z.zone_type,
             "confidence": round(z.confidence, 3), "method": z.method,
             "area_m2": round(z.area_m2, 2), "bounds": list(z.polygon.bounds)}
            for z in classified.zones
        ]
        zone_reports_data = [
            {
                "zone_type":          r.zone_type,
                "area_m2":            r.area_m2,
                "room_width_m":       r.room_width_m,
                "room_depth_m":       r.room_depth_m,
                "ceiling_height_m":   r.ceiling_height_m,
                "room_index_k":       r.room_index_k,
                "utilisation_factor": r.utilisation_factor,
                "target_lux":         r.target_lux,
                "required_count":     r.required_count,
                "placed_count":       r.placed_count,
                "grid_pitch_mm":      r.grid_pitch_mm,
                "maintained_lux":     round(r.maintained_lux_actual(), 1),
                "luminaire_type":     r.luminaire_type,
                "luminaire_flux_lm":  r.luminaire_flux_lm,
                "target_met":         r.maintained_lux_actual() >= r.target_lux * 0.80,
            }
            for r in result.zone_reports
        ]

        db.update_job(job_id, "done", "Pipeline complete", result={
            "summary":          result.summary(),
            "total_luminaires": len(result.placed),
            "total_wattage":    round(result.total_wattage()),
            "type_A":           len(result.by_type("A")),
            "type_B":           len(result.by_type("B")),
            "type_C":           len(result.by_type("C")),
            "type_D":           len(result.by_type("D")),
            "type_E":           len(result.by_type("E")),
            "zones":            zones_data,
            "zone_reports":     zone_reports_data,
            "placed":           placed_data,
            "exports": {
                "dxf":  str(dwg_out),
                "xlsx": str(xlsx_out),
                "pdf":  str(pdf_out),
            },
        })

    except ValueError as e:
        db.update_job(job_id, "error", str(e))
        print(f"[Job {job_id}] ValueError: {e}")

    except ImportError as e:
        msg = ("Missing dependency — run: pip install pymupdf ezdxf shapely "
               "scikit-learn openpyxl jinja2")
        db.update_job(job_id, "error", msg)
        print(f"[Job {job_id}] ImportError: {e}")

    except Exception as e:
        tb  = traceback.format_exc()
        db.update_job(job_id, "error", f"{type(e).__name__}: {e}", traceback=tb)
        print(f"[Job {job_id}] Unhandled:\n{tb}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _job_or_404(job_id: str) -> dict:
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Job not found")
    return row


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


# ── Concept management ────────────────────────────────────────────────────────

@app.get("/concepts")
def list_concepts():
    yamls = sorted(CONCEPTS_DIR.glob("*.yaml"))
    return {"concepts": [p.stem for p in yamls] or ["rossmann_standard"]}


@app.post("/concepts", status_code=201)
async def create_concept(
    file: UploadFile = File(...),
    concept_id: str  = Form(...),
):
    if not concept_id.replace("_","").replace("-","").isalnum():
        raise HTTPException(400, "concept_id must be alphanumeric (dashes/underscores ok)")
    dest = CONCEPTS_DIR / f"{concept_id}.yaml"
    if dest.exists():
        raise HTTPException(409, f"Concept '{concept_id}' already exists. DELETE it first.")
    content = await file.read()
    dest.write_bytes(content)
    return {"status": "created", "concept_id": concept_id}


@app.delete("/concepts/{concept_id}", status_code=200)
def delete_concept(concept_id: str):
    if concept_id == "rossmann_standard":
        raise HTTPException(403, "Cannot delete the default concept.")
    path = CONCEPTS_DIR / f"{concept_id}.yaml"
    if not path.exists():
        raise HTTPException(404, f"Concept '{concept_id}' not found.")
    path.unlink()
    return {"status": "deleted", "concept_id": concept_id}


# ── Job processing ────────────────────────────────────────────────────────────

@app.post("/process", response_model=JobStatus)
async def process(
    background_tasks: BackgroundTasks,
    file:         UploadFile = File(...),
    concept_id:   str        = Form("rossmann_standard"),
    project_name: str        = Form("Lighting Project"),
    customer:     str        = Form("Dirk Rossmann GmbH"),
):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".dxf", ".dwg", ".pdf"):
        raise HTTPException(400, "Only .pdf, .dxf and .dwg files accepted.")

    concept_path = CONCEPTS_DIR / f"{concept_id}.yaml"
    if not concept_path.exists():
        raise HTTPException(400, f"Concept '{concept_id}' not found.")

    job_id   = str(uuid.uuid4())[:8]
    savepath = DWG_DIR / f"{job_id}_{file.filename}"
    savepath.write_bytes(await file.read())

    db.create_job(job_id,
                  filename=file.filename,
                  concept_id=concept_id,
                  project_name=project_name,
                  customer=customer)

    background_tasks.add_task(
        _run_pipeline, job_id, savepath, concept_id, project_name, customer)

    return JobStatus(job_id=job_id, status="queued",
                     message=f"Queued — poll /jobs/{job_id}")


@app.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    row = _job_or_404(job_id)
    return JobStatus(job_id=job_id, status=row["status"],
                     message=row["message"], result=row.get("result"))


# ── Planning history ──────────────────────────────────────────────────────────

@app.get("/history")
def get_history(limit: int = 50, offset: int = 0):
    rows = db.list_jobs(limit=limit, offset=offset)
    return {
        "total":  len(rows),
        "limit":  limit,
        "offset": offset,
        "jobs": [
            {
                "job_id":       r["job_id"],
                "status":       r["status"],
                "filename":     r.get("filename", ""),
                "project_name": r.get("project_name", ""),
                "customer":     r.get("customer", ""),
                "concept_id":   r.get("concept_id", ""),
                "created_at":   r.get("created_at", 0),
                "message":      r.get("message", ""),
                "total_luminaires": (r.get("result") or {}).get("total_luminaires"),
                "total_wattage":    (r.get("result") or {}).get("total_wattage"),
            }
            for r in rows
        ],
    }


# ── Exports ───────────────────────────────────────────────────────────────────

@app.get("/exports/{job_id}/{fmt}")
def download_export(job_id: str,
                    fmt: Literal["dxf", "xlsx", "pdf", "html"]):
    row = _job_or_404(job_id)
    if row["status"] != "done":
        raise HTTPException(400, f"Job status is '{row['status']}', not done.")

    exports  = (row.get("result") or {}).get("exports", {})
    path_map = {
        "dxf":  exports.get("dxf", ""),
        "xlsx": exports.get("xlsx", ""),
        "pdf":  exports.get("pdf", ""),
        "html": exports.get("pdf", ""),   # html fallback lives at the same path
    }
    path = Path(path_map.get(fmt, ""))
    if not path.exists():
        # Try .html extension if pdf was written as HTML fallback
        alt = path.with_suffix(".html")
        if alt.exists():
            path = alt
        else:
            raise HTTPException(404, f"Export file not found: {path.name}")
    return FileResponse(str(path), filename=path.name)


# ── Corrections / RL signal ───────────────────────────────────────────────────

@app.post("/corrections")
def submit_corrections(payload: CorrectionPayload):
    _job_or_404(payload.job_id)
    total = db.add_corrections(payload.job_id, payload.corrections)
    # Also persist to disk for offline RL training
    out      = EXPORTS_DIR / f"{payload.job_id}_corrections.json"
    existing = json.loads(out.read_text()) if out.exists() else []
    existing.extend(payload.corrections)
    out.write_text(json.dumps(existing, indent=2))
    return {"status": "recorded", "count": len(payload.corrections),
            "total_corrections": total,
            "message": "Saved for RL training"}


# ── Serve React frontend (catch-all) ─────────────────────────────────────────

_API_PREFIXES = {"health","concepts","process","jobs","history",
                 "exports","corrections","docs","openapi.json"}

@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    if full_path.split("/")[0] in _API_PREFIXES:
        raise HTTPException(404, "Not found")
    index = STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(content=index.read_text(), status_code=200)
    return HTMLResponse(
        content=("<h1>lighting-ai API</h1>"
                 "<p>Frontend not built yet. "
                 "API docs: <a href='/docs'>/docs</a></p>"),
        status_code=200,
    )


# ── Dev runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from config import API_HOST, API_PORT
    uvicorn.run("services.api.main:app", host=API_HOST,
                port=API_PORT, reload=True)
