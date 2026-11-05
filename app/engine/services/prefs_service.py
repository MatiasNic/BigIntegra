"""Preferencias del usuario persistidas en APP_DATA_DIR/prefs.json.

Sin dependencia de Qt (vive en app/engine). Guarda solo ajustes chicos: si
descargar pide ubicación y la última carpeta usada en los diálogos.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from app.config import APP_DATA_DIR

logger = logging.getLogger("bigpdf")

_PREFS_PATH = APP_DATA_DIR / "prefs.json"
_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            data = json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
            _cache = data if isinstance(data, dict) else {}
        except Exception:
            _cache = {}
    return _cache


def _save() -> None:
    try:
        _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PREFS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache or {}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PREFS_PATH)
    except Exception as exc:
        logger.debug("prefs: no se pudo guardar %s: %s", _PREFS_PATH, exc)


def get_pref(key: str, default: Any = None) -> Any:
    with _lock:
        return _load().get(key, default)


def set_pref(key: str, value: Any) -> None:
    with _lock:
        _load()[key] = value
        _save()


def get_choose_location() -> bool:
    return bool(get_pref("choose_location", False))


def set_choose_location(value: bool) -> None:
    set_pref("choose_location", bool(value))


def get_last_dir(default: Path) -> Path:
    """Última carpeta usada en un diálogo de archivos, si todavía existe."""
    raw = get_pref("last_dir")
    if raw:
        candidate = Path(raw)
        if candidate.is_dir():
            return candidate
    return default


def set_last_dir(path: str | Path) -> None:
    p = Path(path)
    if p.is_file():
        p = p.parent
    if str(p):
        set_pref("last_dir", str(p))
