import sqlite3
import threading
import time
from pathlib import Path

from app.config import STORAGE_DIR

_DB_PATH = STORAGE_DIR / "export_registry.db"
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS exports ("
        "export_id TEXT PRIMARY KEY, "
        "path TEXT NOT NULL, "
        "filename TEXT NOT NULL, "
        "media_type TEXT NOT NULL, "
        "created_at REAL NOT NULL"
        ")"
    )
    return conn


def set_export(export_id: str, path: Path, filename: str, media_type: str) -> None:
    with _lock:
        conn = _conn()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO exports (export_id, path, filename, media_type, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (export_id, str(path), filename, media_type, time.time()),
                )
        finally:
            conn.close()


def get_export(export_id: str) -> dict | None:
    with _lock:
        conn = _conn()
        try:
            with conn:
                row = conn.execute(
                    "SELECT path, filename, media_type FROM exports WHERE export_id = ?",
                    (export_id,),
                ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return {"path": Path(row[0]), "filename": row[1], "media_type": row[2]}


def prune_older_than(cutoff_timestamp: float) -> int:
    """Borra filas viejas de la tabla (no archivos en disco — eso lo hace cleanup_old_files).
    Mantiene la tabla acotada en el tiempo, igual que el storage en disco."""
    with _lock:
        conn = _conn()
        try:
            with conn:
                cur = conn.execute("DELETE FROM exports WHERE created_at < ?", (cutoff_timestamp,))
                return cur.rowcount
        finally:
            conn.close()
