"""
lighting-ai/main.py — master entry point.

Modes:
  pipeline      Run the full ML+geometric pipeline on a plan file
  api           Start the FastAPI server
  train         Train / retrain the ZONE classifier (room_classifier.pkl)
  train-placer  Train / retrain the PLACEMENT classifier (placer_model.pkl)
  validate      Validate placement accuracy against a known output plan

Usage:
  python main.py pipeline --file plan.dwg
  python main.py pipeline --demo
  python main.py api
  python main.py train --synthetic
  python main.py train-placer --synthetic
  python main.py train-placer --annotations data/annotations/placements.jsonl
  python main.py validate

Environment:
  COLLECT_TRAINING=1   (default) — save placement training data during pipeline runs
  COLLECT_TRAINING=0   — disable training data collection (e.g. batch benchmarks)
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import API_HOST, API_PORT, EXPORTS_DIR, CONCEPTS_DIR
from services.log import configure_logging, get_logger

log = get_logger(__name__)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(file_path: str, pdf_fallback: str = None,
                 concept_id: str = "rossmann_standard",
                 project_name: str = "Lighting Project",
                 customer: str = "Dirk Rossmann GmbH",
                 demo: bool = False,
                 verbose: bool = False) -> dict:

    configure_logging(verbose=verbose)

    from services.parser.pdf_parser import RealPlanParser
    from services.classifier.room_classifier_real import RealRoomClassifier
    from services.placer.real_placer import RealLuminairePlacer
    from services.exporter.exporter import export_dwg, export_excel, export_pdf

    _ensure_concept()

    print("=" * 58)

    # ── Parse ──────────────────────────────────────────────────────────────
    print("[1/5] Parsing floor plan…")
    parser = RealPlanParser()

    if demo or not file_path or not Path(file_path).exists():
        up = Path("/mnt/user-data/uploads")
        candidates = [
            up / "3600_HH_Jungfernstieg_EG_SB_Kassen_20240506.pdf",
            up / "Ro_Hamburg_Jungfernstieg_3600_20260113-EG.dwg",
        ]
        plan_file = next((p for p in candidates if p.exists()), None)
        if plan_file is None:
            print("  Demo mode: no real file found, using synthetic plan.")
            plan = _make_synthetic_plan()
        else:
            print(f"  Demo mode: using {plan_file.name}")
            plan = parser.parse(plan_file)
    else:
        kw = {"pdf_fallback": pdf_fallback} if pdf_fallback else {}
        plan = parser.parse(file_path, **kw)

    print(f"  {plan.summary()}")

    # ── Classify ───────────────────────────────────────────────────────────
    print("[2/5] Classifying zones…")
    classified = RealRoomClassifier().classify(plan)
    print(f"  {classified.summary()}")
    for z in classified.zones:
        print(f"    Zone {z.polygon_index:2d}: {z.zone_type:15s} "
              f"{z.area_m2:7.1f}m²  conf={z.confidence:.2f}  [{z.method}]")

    # ── Place ──────────────────────────────────────────────────────────────
    print("[3/5] Placing luminaires…")
    result = RealLuminairePlacer().place_all(plan, classified)
    print(f"  {result.summary()}")
    A = result.by_type("A"); B = result.by_type("B")
    print(f"  Type A (15W 40°): {len(A)}")
    print(f"  Type B (20W 60°): {len(B)}")

    # ── Export ─────────────────────────────────────────────────────────────
    print("[4/5] Exporting…")
    stem    = Path(plan.source_file).stem if Path(plan.source_file).exists() else "output"
    prefix  = str(EXPORTS_DIR / stem)
    src_dxf = str(plan.source_file) if Path(plan.source_file).suffix == '.dxf' else None

    dwg  = export_dwg(result, classified, source_dxf_path=src_dxf, output_path=prefix+"_luminaires.dxf")
    xlsx = export_excel(result, classified, project_name=project_name,
                        customer=customer, concept_id=concept_id,
                        output_path=prefix+"_schedule.xlsx")
    pdf  = export_pdf(result, classified, concept_id=concept_id,
                      customer=customer, project_name=project_name,
                      output_path=prefix+"_documentation")

    print("[5/5] Done.")
    print("=" * 58)
    print(f"  Luminaires placed : {len(result.placed)}")
    print(f"  Total wattage     : {result.total_wattage():.0f} W")
    print(f"  DXF               : {dwg}")
    print(f"  Excel             : {xlsx}")
    print(f"  PDF/HTML          : {pdf}")
    print("=" * 58)

    return {"placed": len(result.placed), "wattage": result.total_wattage(),
            "exports": {"dxf": str(dwg), "xlsx": str(xlsx), "pdf": str(pdf)}}


def _ensure_concept():
    CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
    dst = CONCEPTS_DIR / "rossmann_standard.yaml"
    if not dst.exists():
        src = Path(__file__).parent / "data/concepts/rossmann_standard.yaml"
        if src.exists():
            import shutil; shutil.copy(src, dst)


def _make_synthetic_plan():
    """Synthetic ParsedPlan for demo/testing without a real file."""
    from shapely.geometry import MultiPoint, Point
    from services.parser.pdf_parser import ParsedPlan, FurnitureInsert

    plan = ParsedPlan(source_file="synthetic_demo.pdf", scale="1:50",
                      ceiling_height_mm=3000.0, grid_pitch_mm=1250.0)

    # Shelf labels spread across a typical sales floor area
    import random, math
    random.seed(42)
    for x in range(15000, 58000, 1250):
        for y in range(17000, 36000, 1250):
            if random.random() > 0.45:
                plan.furniture.append(FurnitureInsert(
                    "SHELF_57", (x + random.randint(-200, 200),
                                 y + random.randint(-200, 200)),
                    0.0, "SHELVING", "shelving"))

    plan.zone_labels = [{
        "text": "Verkaufsraum-EG\n643,60 m²",
        "zone_type": "sales_floor",
        "area_m2": 643.60,
        "x_mm": 36500.0,
        "y_mm": 26500.0,
    }]
    xs = [f.position[0] for f in plan.furniture]
    ys = [f.position[1] for f in plan.furniture]
    plan.bounds = (min(xs), min(ys), max(xs), max(ys))
    return plan


# ── Validate ──────────────────────────────────────────────────────────────────

def run_validate():
    """Compare pipeline output against the real Rossmann EG output plan."""
    from services.parser.pdf_parser import RealPlanParser, extract_luminaires_from_lighting_plan
    from services.classifier.room_classifier_real import RealRoomClassifier
    from services.placer.real_placer import RealLuminairePlacer

    UP = Path("/mnt/user-data/uploads")
    input_pdf  = UP / "3600_HH_Jungfernstieg_EG_SB_Kassen_20240506.pdf"
    output_pdf = UP / "Ro_Hamburg_Jungfernstieg_3600_20260113-EG-DRP.pdf"

    if not input_pdf.exists() or not output_pdf.exists():
        print("Reference files not found in /mnt/user-data/uploads/")
        return

    print("Validating pipeline against real plan…")

    # Ground truth from real output plan
    gt = extract_luminaires_from_lighting_plan(output_pdf)
    gt_A = sum(1 for l in gt if l.lumi_type == 'A')
    gt_B = sum(1 for l in gt if l.lumi_type == 'B')
    print(f"\nGround truth (from output PDF): {len(gt)} luminaires  A={gt_A}  B={gt_B}")

    # Pipeline prediction
    plan  = RealPlanParser().parse(input_pdf)
    clf   = RealRoomClassifier().classify(plan)
    result= RealLuminairePlacer().place_all(plan, clf)
    pred_A = len(result.by_type('A'))
    pred_B = len(result.by_type('B'))

    print(f"Pipeline prediction:            {len(result.placed)} luminaires  A={pred_A}  B={pred_B}")
    print()
    print(f"  Total accuracy:  {(1 - abs(len(result.placed)-len(gt))/max(len(gt),1))*100:.1f}%")
    print(f"  Type A accuracy: {(1 - abs(pred_A-gt_A)/max(gt_A,1))*100:.1f}%")
    print(f"  Type B accuracy: {(1 - abs(pred_B-gt_B)/max(gt_B,1))*100:.1f}%")


# ── API ───────────────────────────────────────────────────────────────────────

def run_api():
    import uvicorn
    configure_logging(verbose=False)
    _ensure_concept()
    log.info(f"Starting lighting-ai API on http://{API_HOST}:{API_PORT}")
    log.info(f"  Swagger docs: http://{API_HOST}:{API_PORT}/docs")
    uvicorn.run("services.api.main:app", host=API_HOST,
                port=API_PORT, reload=True)


# ── Train ─────────────────────────────────────────────────────────────────────

def run_train(synthetic=True, from_reference=False,
              annotations=None, corrections=None, n_per_class=300):
    from ml.training.train_classifier import (
        generate_synthetic, extract_from_reference,
        load_annotations, load_corrections, train, feat_importance,
    )
    import numpy as np
    from config import ANNOTATIONS_DIR

    X_all = np.empty((0, 14), dtype=np.float32); y_all = []

    if from_reference:
        Xr, yr = extract_from_reference(str(ANNOTATIONS_DIR / "labels.jsonl"))
        if len(Xr): X_all = np.vstack([X_all, Xr]) if len(X_all) else Xr; y_all.extend(yr)

    if annotations and Path(annotations).exists():
        Xa, ya = load_annotations(annotations)
        if len(Xa): X_all = np.vstack([X_all, Xa]) if len(X_all) else Xa; y_all.extend(ya)

    if corrections:
        Xc, yc = load_corrections(corrections)
        if len(Xc): X_all = np.vstack([X_all, Xc]) if len(X_all) else Xc; y_all.extend(yc)

    if synthetic or len(X_all) < 50:
        print(f"Generating synthetic data ({n_per_class}/class)…")
        Xs, ys = generate_synthetic(n_per_class)
        X_all = np.vstack([X_all, Xs]) if len(X_all) else Xs; y_all.extend(ys)

    if len(X_all) == 0:
        print("No training data. Use --synthetic."); return

    clf = train(X_all, y_all)
    feat_importance(clf)
    print(f"\nModel trained on {len(X_all)} samples.")


# ── Train placement model ─────────────────────────────────────────────────────

def run_train_placer(synthetic=True, annotations=None, n_synthetic=400):
    """Train the ML luminaire placement classifier (placer_model.pkl)."""
    from ml.training.train_placer import (
        _jsonl_to_arrays, generate_synthetic, train, feature_importance,
        PLACEMENTS_JSONL,
    )
    import numpy as np
    from services.placer.placement_features import FEATURE_DIM

    X_all = np.empty((0, FEATURE_DIM), dtype=np.float32)
    y_all: list = []

    # Load accumulated placement samples from pipeline runs
    ann_path = Path(annotations) if annotations else PLACEMENTS_JSONL
    Xa, ya = _jsonl_to_arrays([ann_path])
    if len(Xa):
        X_all = np.vstack([X_all, Xa]) if len(X_all) else Xa
        y_all.extend(ya)
        print(f"Loaded {len(Xa)} real placement samples from {ann_path}")

    if synthetic or len(X_all) < 200:
        print(f"Adding synthetic training samples ({n_synthetic}/class) …")
        Xs, ys = generate_synthetic(n_synthetic)
        X_all = np.vstack([X_all, Xs]) if len(X_all) else Xs
        y_all.extend(ys)

    if len(X_all) == 0:
        print("No training data. Run the pipeline first to generate placements.jsonl.")
        return

    clf = train(X_all, y_all)
    feature_importance(clf)
    print(f"\nPlacement model trained on {len(X_all)} samples.")


# ── Auto-ensure models exist ──────────────────────────────────────────────────

def _ensure_models():
    """Bootstrap ML models on first run if they don't exist yet."""
    from config import MODELS_DIR
    zone_model = MODELS_DIR / "room_classifier.pkl"
    placer_model = MODELS_DIR / "placer_model.pkl"

    if not zone_model.exists():
        print("[ML] Zone classifier not found — training with synthetic data …")
        run_train(synthetic=True)

    if not placer_model.exists():
        print("[ML] Placement classifier not found — training with synthetic data …")
        run_train_placer(synthetic=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="lighting-ai — automated ML lighting design pipeline",
        formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    # pipeline
    pp = sub.add_parser("pipeline", help="Run full pipeline on a plan file")
    pp.add_argument("--file",         default="",   help="Path to PDF / DXF / DWG")
    pp.add_argument("--pdf-fallback", default=None, help="Companion PDF for binary DWG")
    pp.add_argument("--concept",      default="rossmann_standard")
    pp.add_argument("--project-name", default="Lighting Project")
    pp.add_argument("--customer",     default="Dirk Rossmann GmbH")
    pp.add_argument("--demo",         action="store_true",
                    help="Run on real uploaded plan (or synthetic if not found)")
    pp.add_argument("--verbose", "-v", action="store_true",
                    help="Show detailed debug output (every row, every candidate)")

    # api
    sub.add_parser("api", help="Start FastAPI server")

    # train
    tp = sub.add_parser("train", help="Train zone classifier")
    tp.add_argument("--synthetic",      action="store_true")
    tp.add_argument("--from-reference", action="store_true")
    tp.add_argument("--annotations",    default=None)
    tp.add_argument("--corrections",    default=None)
    tp.add_argument("--n-per-class",    type=int, default=300)

    # train-placer
    plp = sub.add_parser("train-placer",
                          help="Train ML placement classifier (placer_model.pkl)")
    plp.add_argument("--synthetic",    action="store_true",
                     help="Add synthetic samples when real data is scarce")
    plp.add_argument("--annotations",  default=None,
                     help="Path to placements.jsonl (default: data/annotations/placements.jsonl)")
    plp.add_argument("--n-synthetic",  type=int, default=400,
                     help="Synthetic samples per class")

    # validate
    sub.add_parser("validate", help="Validate vs real Rossmann EG plan")

    args = p.parse_args()

    if args.mode == "pipeline":
        _ensure_models()
        run_pipeline(file_path=args.file, pdf_fallback=args.pdf_fallback,
                     concept_id=args.concept, project_name=args.project_name,
                     customer=args.customer, demo=args.demo,
                     verbose=getattr(args, 'verbose', False))
    elif args.mode == "api":
        _ensure_models()
        run_api()
    elif args.mode == "train":
        run_train(synthetic=args.synthetic, from_reference=args.from_reference,
                  annotations=args.annotations, corrections=args.corrections,
                  n_per_class=args.n_per_class)
    elif args.mode == "train-placer":
        run_train_placer(
            synthetic   = args.synthetic,
            annotations = args.annotations,
            n_synthetic = args.n_synthetic,
        )
    elif args.mode == "validate":
        run_validate()


if __name__ == "__main__":
    main()