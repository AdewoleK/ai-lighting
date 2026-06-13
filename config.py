"""lighting-ai/config.py — central configuration, all services import from here."""
import os
from pathlib import Path

ROOT = Path(__file__).parent

DATA_DIR        = ROOT / "data"
DWG_DIR         = DATA_DIR / "dwg"
ANNOTATIONS_DIR = DATA_DIR / "annotations"
EXPORTS_DIR     = DATA_DIR / "exports"
MODELS_DIR      = ROOT / "ml" / "models"
CONCEPTS_DIR    = ROOT / "data" / "concepts"
DB_PATH         = DATA_DIR / "jobs.db"

for _d in [DWG_DIR, ANNOTATIONS_DIR, EXPORTS_DIR, MODELS_DIR, CONCEPTS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ODA File Converter — free tool to convert binary .dwg → .dxf
# Download: https://www.opendesign.com/guestfiles/oda_file_converter
# Set ODA_CONVERTER_PATH env var or install at one of the default locations below.
ODA_CONVERTER_PATH = os.getenv("ODA_CONVERTER_PATH", "")

DEFAULT_LAYER_MAP = {
    "walls":      ["WALLS","A-WALL","WAND","0"],
    "ceiling":    ["CEILING","A-CLNG","DECKE","RASTERDECKE"],
    "grid":       ["GRID","CEILING-GRID","RASTER","A-CLNG-GRID","DECKENRASTER"],
    "doors":      ["DOORS","A-DOOR","TUR","TUER"],
    "windows":    ["WINDOWS","A-GLAZ","FENSTER"],
    "furniture":  ["FURNITURE","A-FURN","EINRICHTUNG"],
    "shelving":   ["SHELVING","REGAL","GONDOLA","BEELINE","SLATWALL"],
    "checkout":   ["CHECKOUT","KASSE","SB","KASSENSTUHL"],
    "luminaires": ["LUMINAIRES","LEUCHTE","BELEUCHTUNG","E-LITE"],
    "annotations":["TEXT","ANNO","A-ANNO"],
}

ZONE_TYPES = ["sales_floor","checkout_zone","entrance","storage",
              "office","corridor","service_area","unknown"]

GRID_PITCH_MM     = 1250
MIN_WALL_CLEARANCE= 400
MIN_LUMI_SPACING  = 875
DEFAULT_CEILING_H = 3000

API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", 8000))