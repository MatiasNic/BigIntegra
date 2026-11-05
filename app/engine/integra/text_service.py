"""Lectura del texto de cada página: capa de texto del PDF u OCR local.

Primero se usa la capa de texto que trae el PDF (documentos digitales). Si la página
no tiene texto suficiente (escaneo, foto de celular convertida a PDF), se hace OCR con
el Tesseract que viene integrado en PyMuPDF/MuPDF. No hay que instalar nada: solo se
necesita el archivo de idioma ``spa.traineddata`` en ``tools/tessdata/``, que viaja
dentro del .exe igual que Ghostscript.

Sin dependencia de PySide6 (regla de ``app/engine``).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.engine.cancellation import raise_if_cancelled

logger = logging.getLogger("bigpdf")

# Menos de esta cantidad de caracteres "útiles" en la capa de texto = página escaneada.
MIN_TEXT_CHARS = 40
OCR_LANGUAGE = "spa"
OCR_DPI = 300


@dataclass
class PageText:
    """Texto de una página y de dónde salió."""

    text: str
    source: str  # "texto" (capa del PDF) | "ocr" | "vacia" | "error"
    seconds: float = 0.0
    error: str = ""

    @property
    def normalized(self) -> str:
        return normalize(self.text)


def _resource_roots() -> list[Path]:
    """Mismo criterio que ghostscript_service: _MEIPASS y carpeta del .exe, o raíz del repo."""
    if getattr(sys, "frozen", False):
        roots: list[Path] = []
        if hasattr(sys, "_MEIPASS"):
            roots.append(Path(sys._MEIPASS))
        roots.append(Path(sys.executable).parent)
        return roots
    return [Path(__file__).resolve().parents[3]]


def find_tessdata_dir() -> Path | None:
    """Carpeta con ``spa.traineddata`` o None si no está (el OCR queda deshabilitado)."""
    env = os.environ.get("BIGINTEGRA_TESSDATA")
    candidates = [Path(env)] if env else []
    candidates += [root / "tools" / "tessdata" for root in _resource_roots()]
    for cand in candidates:
        if (cand / f"{OCR_LANGUAGE}.traineddata").is_file():
            return cand
    return None


def normalize(text: str) -> str:
    """Minúsculas, sin tildes, ligaduras separadas (ﬁ → fi) y espacios colapsados.

    Todo el catálogo y el clasificador comparan contra texto normalizado, así una
    tilde o una ligadura perdida por el OCR no cambia el resultado.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[_‐-―]+", " ", text)  # renglones "_____" de los formularios
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _useful_chars(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def extract_page_text(
    page: pymupdf.Page,
    tessdata: Path | None,
    *,
    force_ocr: bool = False,
) -> PageText:
    """Texto de una página. Si la capa de texto es pobre y hay tessdata, hace OCR."""
    started = time.monotonic()
    try:
        text = page.get_text()
    except Exception as exc:  # PDF dañado: seguimos con OCR si se puede
        logger.warning("No se pudo leer la capa de texto: %s", exc)
        text = ""
    if not force_ocr and _useful_chars(text) >= MIN_TEXT_CHARS:
        return PageText(text, "texto", time.monotonic() - started)
    if tessdata is None:
        source = "texto" if text.strip() else "vacia"
        return PageText(text, source, time.monotonic() - started, error="OCR no disponible")
    try:
        textpage = page.get_textpage_ocr(
            language=OCR_LANGUAGE, dpi=OCR_DPI, full=True, tessdata=str(tessdata)
        )
        ocr_text = textpage.extractText()
    except Exception as exc:
        logger.warning("Falló el OCR de la página %s: %s", page.number, exc)
        return PageText(text, "error", time.monotonic() - started, error=str(exc))
    source = "ocr" if _useful_chars(ocr_text) else "vacia"
    return PageText(ocr_text, source, time.monotonic() - started)


def extract_document_texts(
    pdf_path: Path,
    *,
    page_numbers: list[int] | None = None,
    tessdata: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[int, PageText]:
    """Texto de varias páginas de un PDF (todas si ``page_numbers`` es None)."""
    if tessdata is None:
        tessdata = find_tessdata_dir()
    result: dict[int, PageText] = {}
    with pymupdf.open(pdf_path) as doc:
        numbers = list(range(doc.page_count)) if page_numbers is None else page_numbers
        for idx, num in enumerate(numbers):
            raise_if_cancelled(cancel_check)
            result[num] = extract_page_text(doc[num], tessdata)
            if progress:
                progress(idx + 1, len(numbers))
    return result


def open_as_pdf(path: Path) -> pymupdf.Document:
    """Abre PDF o imagen (JPG/PNG/TIFF…) como documento PDF en memoria."""
    doc = pymupdf.open(path)
    if doc.is_pdf:
        return doc
    try:
        data = doc.convert_to_pdf()
    finally:
        doc.close()
    return pymupdf.open("pdf", data)
