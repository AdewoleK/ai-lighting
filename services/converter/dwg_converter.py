"""
lighting-ai/services/converter/dwg_converter.py

Binary DWG → DXF conversion.

Priority order:
  1. LibreDWG dwg2dxf  (open-source, Mac/Linux, supports all DWG versions incl. AC1032)
       Mac:   brew install libredwg
       Linux: apt install libredwg-tools  OR  brew install libredwg
  2. ODA File Converter CLI  (free download, high-fidelity, all platforms)
       https://www.opendesign.com/guestfiles/oda_file_converter
  3. ezdxf recover mode  (partial — works on DXF-like DWG files only)
  4. Raise a clear error with installation instructions
"""
from __future__ import annotations
import os, shutil, subprocess, tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import ODA_CONVERTER_PATH, DWG_DIR


# ── LibreDWG search paths ────────────────────────────────────────────────────

_LIBREDWG_CANDIDATES = [
    # Homebrew on Apple Silicon Mac
    "/opt/homebrew/bin/dwg2dxf",
    # Homebrew on Intel Mac / Linux via brew
    "/usr/local/bin/dwg2dxf",
    # Linux system package
    "/usr/bin/dwg2dxf",
    # Common manual installs
    os.path.expanduser("~/bin/dwg2dxf"),
]


def _find_libredwg() -> str | None:
    for path in _LIBREDWG_CANDIDATES:
        if path and Path(path).exists():
            return path
    return shutil.which("dwg2dxf")   # also check $PATH


# ── ODA File Converter search paths ─────────────────────────────────────────

_ODA_CANDIDATES = [
    ODA_CONVERTER_PATH,
    "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
    "/usr/bin/ODAFileConverter",
    "/usr/local/bin/ODAFileConverter",
    os.path.expanduser("~/ODAFileConverter/ODAFileConverter"),
    os.path.expanduser("~/bin/ODAFileConverter"),
    r"C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe",
    r"C:\Program Files (x86)\ODA\ODAFileConverter\ODAFileConverter.exe",
]


def _find_oda() -> str | None:
    for path in _ODA_CANDIDATES:
        if path and Path(path).exists():
            return str(path)
    return shutil.which("ODAFileConverter")


# ── Public API ────────────────────────────────────────────────────────────────

def convert_dwg_to_dxf(dwg_path: str | Path, output_dir: str | Path | None = None) -> Path:
    """
    Convert a binary DWG file to DXF and return the path of the DXF file.
    Tries LibreDWG → ODA → ezdxf in order, raises ValueError if all fail.
    """
    dwg_path = Path(dwg_path)
    if not dwg_path.exists():
        raise FileNotFoundError(f"DWG file not found: {dwg_path}")

    # Already a DXF / ASCII file? Return as-is.
    with open(dwg_path, "rb") as f:
        header = f.read(20)
    if (header.startswith(b"  0\r\n") or header.startswith(b"  0\n")
            or b"SECTION" in header):
        return dwg_path

    out_dir = Path(output_dir) if output_dir else dwg_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf_path = out_dir / (dwg_path.stem + "_converted.dxf")

    if dxf_path.exists():
        return dxf_path

    # ── Strategy 1: LibreDWG (dwg2dxf) ───────────────────────────────────────
    libredwg = _find_libredwg()
    if libredwg:
        try:
            result = _convert_via_libredwg(libredwg, dwg_path, dxf_path)
            print(f"[DWG converter] LibreDWG: {dwg_path.name} → {result.name}")
            return result
        except Exception as e:
            print(f"[DWG converter] LibreDWG failed: {e}")

    # ── Strategy 2: ODA File Converter ───────────────────────────────────────
    oda = _find_oda()
    if oda:
        try:
            result = _convert_via_oda(oda, dwg_path, out_dir)
            print(f"[DWG converter] ODA: {dwg_path.name} → {result.name}")
            return result
        except Exception as e:
            print(f"[DWG converter] ODA failed: {e}")

    # ── Strategy 3: ezdxf recover ─────────────────────────────────────────────
    try:
        result = _convert_via_ezdxf(dwg_path, dxf_path)
        print(f"[DWG converter] ezdxf recovery: {dwg_path.name} → {result.name}")
        return result
    except Exception as e:
        print(f"[DWG converter] ezdxf recovery failed: {e}")

    # ── Nothing worked ────────────────────────────────────────────────────────
    raise ValueError(
        f"Binary DWG '{dwg_path.name}' cannot be read directly "
        f"(format: {header[:6]!r}).\n\n"
        "Options:\n"
        "  1. Install LibreDWG (recommended — free, one command):\n"
        "       Mac:   brew install libredwg\n"
        "       Linux: sudo apt install libredwg-tools\n"
        "     Then retry — no other steps needed.\n\n"
        "  2. Install ODA File Converter (free download):\n"
        "       https://www.opendesign.com/guestfiles/oda_file_converter\n\n"
        "  3. Upload the companion PDF alongside the DWG file.\n"
        "  4. Export a DXF from AutoCAD:  File → Save As → AutoCAD DXF\n"
    )


def _convert_via_libredwg(dwg2dxf_bin: str, dwg_path: Path, dxf_out: Path) -> Path:
    """
    Use LibreDWG's dwg2dxf to convert binary DWG → DXF.

    dwg2dxf writes its output next to the input file by default, so we
    copy the DWG to a temp directory, run the conversion there, then move
    the result to the requested output path.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dwg = Path(tmp) / dwg_path.name
        shutil.copy2(dwg_path, tmp_dwg)

        result = subprocess.run(
            [dwg2dxf_bin, "-o", str(dxf_out), str(tmp_dwg)],
            capture_output=True,
            text=True,
            timeout=120,
        )

        # dwg2dxf exits 0 on success; non-zero on hard failure
        # Some versions print warnings but still produce valid output
        if dxf_out.exists() and dxf_out.stat().st_size > 0:
            return dxf_out

        # Fallback: dwg2dxf may ignore -o and write next to the input
        fallback = Path(tmp) / (dwg_path.stem + ".dxf")
        if fallback.exists() and fallback.stat().st_size > 0:
            shutil.move(str(fallback), str(dxf_out))
            return dxf_out

        raise RuntimeError(
            f"dwg2dxf exited {result.returncode}. "
            f"stderr: {result.stderr[:300]}"
        )


def _convert_via_oda(oda_bin: str, dwg_path: Path, out_dir: Path) -> Path:
    """
    Run ODA File Converter CLI.

    CLI syntax:
      ODAFileConverter <input_dir> <output_dir> <version> <type> <recurse> <audit> [filter]

    We copy the single DWG into a temp input dir so the converter only
    processes one file.
    """
    with tempfile.TemporaryDirectory() as tmp_in:
        tmp_in_path = Path(tmp_in)
        shutil.copy2(dwg_path, tmp_in_path / dwg_path.name)

        cmd = [
            oda_bin,
            str(tmp_in_path),   # input folder
            str(out_dir),        # output folder
            "ACAD2018",          # output DWG/DXF version
            "DXF",               # output type
            "0",                 # recurse subdirectories: no
            "1",                 # audit: yes
            f"*.{dwg_path.suffix.lstrip('.')}",  # filter
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            raise RuntimeError(f"ODA returned {result.returncode}: {result.stderr}")

    # Find the produced DXF
    candidates = list(out_dir.glob(f"{dwg_path.stem}*.dxf"))
    if not candidates:
        # ODA sometimes uses original stem exactly
        dxf = out_dir / (dwg_path.stem + ".dxf")
        if not dxf.exists():
            raise RuntimeError("ODA ran successfully but no DXF output found")
        return dxf

    return max(candidates, key=lambda p: p.stat().st_mtime)


def _convert_via_ezdxf(dwg_path: Path, dxf_out: Path) -> Path:
    """
    Attempt to read the DWG using ezdxf's recover mode and re-save as DXF.
    Works for many DWG files that are internally structured like DXF.
    """
    import ezdxf
    doc, _ = ezdxf.recover.readfile(str(dwg_path))
    doc.saveas(str(dxf_out))
    return dxf_out


# ── CLI helper ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python dwg_converter.py <file.dwg>")
        sys.exit(1)
    out = convert_dwg_to_dxf(sys.argv[1])
    print(f"Output: {out}")
