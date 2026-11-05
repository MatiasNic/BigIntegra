"""Punto de entrada del análisis para el bridge: lee, clasifica y analiza el workspace.

El texto de cada hoja se guarda en memoria durante la sesión (clave: documento +
número de hoja). Así, cuando el usuario corrige una etiqueta o cambia las
prestaciones, ``analyze_workspace`` vuelve a correr al instante sin repetir el OCR.
Nada se guarda en disco fuera de la carpeta temporal de la sesión.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf

from app.engine.cancellation import raise_if_cancelled
from app.engine.integra.analyzer import analyze
from app.engine.integra.classifier import classify_pages, manual_classification
from app.engine.integra.text_service import PageText, extract_page_text, find_tessdata_dir

_cache: dict[tuple[str, int], PageText] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def read_pages(
    pages: list[dict[str, Any]],
    uploads_dir: Path,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[tuple[str, int], PageText]:
    """Lee (o toma de la caché) el texto de las hojas pedidas."""
    tessdata = find_tessdata_dir()
    pending: dict[str, list[int]] = {}
    with _lock:
        for page in pages:
            key = (page["source_document_id"], int(page["page_num"]))
            if key not in _cache:
                pending.setdefault(key[0], []).append(key[1])
    total = sum(len(v) for v in pending.values())
    done = 0
    for doc_id, numbers in pending.items():
        path = uploads_dir / f"{doc_id}.pdf"
        with pymupdf.open(path) as doc:
            for num in numbers:
                raise_if_cancelled(cancel_check)
                if progress:
                    progress(done, total, f"Leyendo hoja {done + 1} de {total}…")
                text = (
                    extract_page_text(doc[num], tessdata)
                    if 0 <= num < doc.page_count
                    else PageText("", "vacia")
                )
                with _lock:
                    _cache[(doc_id, num)] = text
                done += 1
    with _lock:
        return {
            (p["source_document_id"], int(p["page_num"])): _cache[
                (p["source_document_id"], int(p["page_num"]))
            ]
            for p in pages
        }


def analyze_workspace(
    payload: dict[str, Any],
    uploads_dir: Path,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """payload: ``{pages:[{page_id, source_document_id, page_num, filename?, override_type?}],
    prestaciones:[...], condiciones:{...}, modo}``. Las hojas eliminadas no se envían."""
    pages = [p for p in payload.get("pages", []) if not p.get("deleted")]
    texts = read_pages(pages, uploads_dir, progress=progress, cancel_check=cancel_check)
    enriched = []
    for page in pages:
        text = texts[(page["source_document_id"], int(page["page_num"]))]
        enriched.append({**page, "text": text.text, "fuente_texto": text.source})
    auto = classify_pages(enriched)
    for page, cls in zip(enriched, auto, strict=True):
        override = page.get("override_type")
        page["clasificacion"] = manual_classification(override) if override else cls
    result = analyze(
        enriched,
        list(payload.get("prestaciones") or []),
        dict(payload.get("condiciones") or {}),
        modo=payload.get("modo") or "integracion",
    )
    result["hojas"] = [
        {
            "page_id": p.get("page_id"),
            "fuente_texto": p["fuente_texto"],
            **p["clasificacion"].to_dict(),
        }
        for p in enriched
    ]
    return result
