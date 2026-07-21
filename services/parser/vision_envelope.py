"""
vision_envelope.py — Vision-based building envelope detection fallback.

When the DXF parser cannot identify the building envelope from polyline geometry
(e.g. walls are drawn as individual LINE segments with no closed polylines),
this module renders the DXF to a PNG and uses OpenCV contour detection to find
the largest closed region, which is the building footprint.

Usage:
    from services.parser.vision_envelope import detect_envelope_from_dxf
    envelope_polygon = detect_envelope_from_dxf(dxf_path, plan)
    # Returns a Shapely Polygon in mm coordinates, or None if detection fails.
"""
from __future__ import annotations
import math
from pathlib import Path
from typing import Optional

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

try:
    import ezdxf
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    _EZDXF_DRAW_AVAILABLE = True
except ImportError:
    _EZDXF_DRAW_AVAILABLE = False

from shapely.geometry import Polygon


# Resolution for the rendered image (pixels per metre of floor plan)
_PIXELS_PER_METRE = 4   # 4 px/m → a 700m² store ≈ 1680×1200 px


def detect_envelope_from_dxf(dxf_path: str, plan) -> Optional[Polygon]:
    """
    Render the DXF file to a greyscale image, find the largest closed contour,
    and convert it back to a Shapely Polygon in millimetre coordinates.

    Returns None if cv2 / ezdxf.draw are unavailable, or if no suitable
    contour is found.
    """
    if not _CV2_AVAILABLE or not _EZDXF_DRAW_AVAILABLE:
        return None

    try:
        return _detect(dxf_path, plan)
    except Exception as exc:
        print(f"[vision_envelope] detection failed: {exc}")
        return None


def _detect(dxf_path: str, plan) -> Optional[Polygon]:
    import tempfile, os
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    # ── Render DXF to PNG ────────────────────────────────────────────────────
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    fig = plt.figure(figsize=(20, 20), dpi=72)
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor('black')
    fig.patch.set_facecolor('black')

    ctx      = RenderContext(doc)
    backend  = MatplotlibBackend(ax)
    frontend = Frontend(ctx, backend)
    frontend.draw_layout(msp, finalize=True)

    # Get the axis limits (in DXF model-space units = mm)
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
        tmp_path = tmp.name
    fig.savefig(tmp_path, dpi=72, bbox_inches='tight', facecolor='black')
    plt.close(fig)

    # ── OpenCV contour detection ─────────────────────────────────────────────
    img = cv2.imread(tmp_path, cv2.IMREAD_GRAYSCALE)
    os.unlink(tmp_path)

    if img is None:
        return None

    # Threshold: anything brighter than background is a wall/line
    _, binary = cv2.threshold(img, 30, 255, cv2.THRESH_BINARY)

    # Close small gaps in wall lines so they form closed regions
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=3)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        return None

    h, w = img.shape
    # Convert pixel coordinates back to mm using the axis limits
    def px_to_mm(px, py):
        x_mm = xlim[0] + (px / w) * (xlim[1] - xlim[0])
        y_mm = ylim[0] + ((h - py) / h) * (ylim[1] - ylim[0])
        return x_mm, y_mm

    # Find the largest contour whose area corresponds to a plausible store size
    best_poly = None
    best_area = 0.0

    for cnt in contours:
        if len(cnt) < 4:
            continue
        # Approximate the contour to reduce noise
        eps  = 0.01 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, eps, True)
        if len(approx) < 3:
            continue

        pts_mm = [px_to_mm(int(p[0][0]), int(p[0][1])) for p in approx]
        try:
            poly = Polygon(pts_mm)
            if not poly.is_valid:
                poly = poly.buffer(0)
            area_m2 = poly.area / 1e6
            # Plausible Rossmann / retail store: 80 m² – 3000 m²
            if 80 <= area_m2 <= 3000 and area_m2 > best_area:
                best_area = area_m2
                best_poly = poly
        except Exception:
            continue

    return best_poly


def is_available() -> bool:
    """Return True if both cv2 and ezdxf drawing are importable."""
    return _CV2_AVAILABLE and _EZDXF_DRAW_AVAILABLE
