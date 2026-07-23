"""
services/log.py — structured terminal logging for the lighting-ai pipeline.

Every service imports `log = get_logger(__name__)` and calls log.info/debug.
The root handler is configured once in main.py (or the API startup) with
`configure_logging()`.  Default level is INFO; pass verbose=True for DEBUG.
"""
from __future__ import annotations
import logging
import sys


# ── ANSI colour codes (stripped automatically on non-TTY output) ──────────────

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"

_COLOURS = {
    "DEBUG":    "\033[36m",    # cyan
    "INFO":     "\033[32m",    # green
    "WARNING":  "\033[33m",    # yellow
    "ERROR":    "\033[31m",    # red
    "CRITICAL": "\033[35m",    # magenta
}

# Compact tag shown in square brackets per logger name
_TAG_MAP = {
    "services.parser.dwg_parser":           "PARSE·DWG ",
    "services.parser.pdf_parser":           "PARSE·PDF ",
    "services.classifier.room_classifier_real": "ZONE      ",
    "services.grid.ceiling_grid":           "GRID      ",
    "services.placer.real_placer":          "PLACE     ",
    "services.exporter.exporter":           "EXPORT    ",
    "main":                                 "PIPELINE  ",
    "__main__":                             "PIPELINE  ",
}


class _PipelineFormatter(logging.Formatter):
    """Coloured, fixed-width tag formatter for terminal output."""

    def __init__(self, use_colour: bool = True):
        super().__init__()
        self._use_colour = use_colour and sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        tag  = _TAG_MAP.get(record.name, record.name[-10:].upper().ljust(10))
        lvl  = record.levelname
        msg  = record.getMessage()

        if self._use_colour:
            col  = _COLOURS.get(lvl, "")
            line = f"{_BOLD}{col}[{tag}]{_RESET} {msg}"
        else:
            line = f"[{tag}] {msg}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(verbose: bool = False) -> None:
    """
    Call once at startup (main.py or API init) to install the pipeline handler.
    verbose=True → DEBUG level (shows every candidate, every shelf row);
    verbose=False → INFO level (shows counts and key decisions).
    """
    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers (avoids duplicates on hot-reload)
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_PipelineFormatter(use_colour=True))
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    for noisy in ("ezdxf", "PIL", "matplotlib", "shapely", "urllib3",
                  "uvicorn.access", "fastapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger.  Usage: log = get_logger(__name__)"""
    return logging.getLogger(name)
