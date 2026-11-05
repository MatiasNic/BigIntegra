"""Reparación automática de PDFs dañados por la compresión de BigPDF <= 1.4.4.

Esas versiones recodificaban los escaneos en escala de grises con perfil ICC de
1 canal como JPEG de 3 canales, pero dejaban declarado el espacio de color de 1
canal: la página se veía ampliada, corrida y con líneas repetidas. La imagen en
sí quedó completa; lo único mal es la declaración de color. Por eso se puede
reparar sin el original, cambiando el espacio de color a /DeviceRGB.

Se aplica sola al importar, sin intervención del usuario. Si el PDF está sano no
se modifica nada (ni siquiera se reescribe el archivo).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import fitz

logger = logging.getLogger("bigpdf")

_ICC_REF_RE = re.compile(r"/ICCBased\s+(\d+)\s+0\s+R")
# Marcadores JPEG "Start Of Frame" (traen la cantidad de componentes de color).
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_components(data: bytes) -> int | None:
    """Cantidad de componentes de color según el encabezado del JPEG."""
    i = 2  # después de SOI (FFD8)
    size = len(data)
    while i + 9 < size:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in _SOF_MARKERS:
            return data[i + 9]
        if marker == 0xFF or marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 1 if marker == 0xFF else 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def _declares_one_channel(doc: fitz.Document, xref: int) -> bool:
    kind, value = doc.xref_get_key(xref, "ColorSpace")
    if kind == "name":
        return value in ("/DeviceGray", "/CalGray")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    elif kind != "array":
        return False
    match = _ICC_REF_RE.search(value)
    return bool(match) and doc.xref_get_key(int(match.group(1)), "N")[1] == "1"


def repair_mismatched_gray_jpegs(pdf_path: Path) -> int:
    """Repara en el lugar las imágenes JPEG de 3 canales declaradas como de 1 canal.

    Devuelve cuántas imágenes se repararon (0 si el PDF estaba sano: en ese caso
    el archivo no se toca). Nunca lanza: ante cualquier problema deja el archivo
    como estaba, porque la importación no debe fallar por esto.
    """
    pdf_path = Path(pdf_path)
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return 0
    try:
        if doc.needs_pass or doc.is_encrypted:
            return 0
        damaged = []
        for xref in range(1, doc.xref_length()):
            try:
                if doc.xref_get_key(xref, "Subtype")[1] != "/Image":
                    continue
                if "DCTDecode" not in doc.xref_get_key(xref, "Filter")[1]:
                    continue
                if not _declares_one_channel(doc, xref):
                    continue
                if _jpeg_components(doc.xref_stream_raw(xref)) == 3:
                    damaged.append(xref)
            except Exception:
                continue
        if not damaged:
            return 0
        for xref in damaged:
            doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB")
        tmp = pdf_path.with_name(pdf_path.stem + ".repair.tmp.pdf")
        try:
            doc.save(str(tmp), garbage=1, deflate=True)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    except Exception as exc:
        logger.warning("repair: no se pudo reparar %s: %s", pdf_path.name, exc)
        return 0
    finally:
        doc.close()
    tmp.replace(pdf_path)
    logger.info("repair: %s imagen(es) reparada(s) en %s", len(damaged), pdf_path.name)
    return len(damaged)


def rebuild_if_damaged(pdf_path: Path) -> bool:
    """Reescribe en el lugar un PDF con la estructura dañada (descarga cortada,
    tabla de referencias rota). Devuelve True si estaba dañado.

    MuPDF (motor de exportación) lo abre "reparándolo" en memoria, pero pdf.js
    (miniaturas) no podía abrirlo: la grilla quedaba con miniaturas en error y la
    exportación salía con páginas en blanco sin aviso. Guardado de nuevo, los dos
    motores ven lo mismo y lo que se ve es lo que se exporta. Nunca lanza.
    """
    pdf_path = Path(pdf_path)
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return False
    tmp = pdf_path.with_name(pdf_path.stem + ".rebuild.tmp.pdf")
    try:
        if doc.needs_pass or not doc.is_repaired or doc.page_count == 0:
            return False
        doc.save(str(tmp), garbage=1, deflate=True)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        logger.warning("repair: no se pudo reconstruir %s: %s", pdf_path.name, exc)
        return False
    finally:
        doc.close()
    tmp.replace(pdf_path)
    logger.info("repair: estructura dañada reconstruida en %s", pdf_path.name)
    return True
