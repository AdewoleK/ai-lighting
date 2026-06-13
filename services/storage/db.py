"""
lighting-ai/services/storage/db.py

SQLite-backed persistent job store.
Replaces the in-memory JOBS dict so jobs survive server restarts
and planning history is available.
"""
from __future__ import annotations
import json, sqlite3, time
from pathlib import Path
from typing import Optional
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import DB_PATH


# ── Schema ───────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'queued',
    message      TEXT NOT NULL DEFAULT '',
    result       TEXT,          -- JSON blob
    traceback    TEXT,
    corrections  TEXT,          -- JSON array
    filename     TEXT,
    concept_id   TEXT,
    project_name TEXT,
    customer     TEXT,
    created_at   REAL NOT NULL  -- Unix timestamp
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.execute(_DDL)


# Call once on import so the table always exists before anything uses it
init_db()


# ── Write helpers ─────────────────────────────────────────────────────────────

def create_job(job_id: str, filename: str, concept_id: str,
               project_name: str, customer: str) -> None:
    with _conn() as c:
        c.execute(
            """INSERT OR IGNORE INTO jobs
               (job_id, status, message, filename, concept_id,
                project_name, customer, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (job_id, "queued", "Job queued", filename, concept_id,
             project_name, customer, time.time()),
        )


def update_job(job_id: str, status: str, message: str,
               result: Optional[dict] = None,
               traceback: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE jobs SET status=?, message=?,
               result=?, traceback=? WHERE job_id=?""",
            (status, message,
             json.dumps(result) if result is not None else None,
             traceback,
             job_id),
        )


def add_corrections(job_id: str, new_corrections: list[dict]) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT corrections FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            return 0
        existing = json.loads(row["corrections"] or "[]")
        existing.extend(new_corrections)
        c.execute(
            "UPDATE jobs SET corrections=? WHERE job_id=?",
            (json.dumps(existing), job_id),
        )
        return len(existing)


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_job(job_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_jobs(limit: int = 100, offset: int = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def job_exists(job_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    return row is not None


# ── Internal ──────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["result"]      = json.loads(d["result"])      if d["result"]      else None
    d["corrections"] = json.loads(d["corrections"]) if d["corrections"] else []
    return d
