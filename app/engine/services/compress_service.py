"""Motor de compresión inteligente de BIGPDF.

La interfaz mantiene una sola opción de compresión. El motor analiza el PDF y
elige automáticamente la estrategia más conveniente:

- limpieza estructural sin pérdida;
- recompresión de imágenes conservando sus dimensiones (casos JPG/escaneos);
- Ghostscript, cuando está disponible, para PDFs mixtos/vectoriales;

La recompresión de expedientes basados en fotografías usa JPEG calidad 65 sin
reducir la resolución de las imágenes. Es el mismo criterio observado en el
archivo de referencia procesado por SmallPDF (42 MB -> ~9 MB).
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import fitz

from app.config import EXPORTS_DIR, ensure_storage_dirs
from app.engine.cancellation import OperationCancelled, raise_if_cancelled
from app.engine.services.ghostscript_service import (
    compress_with_ghostscript,
    is_ghostscript_available,
)

logger = logging.getLogger("bigpdf")

VALID_LEVELS = {"low", "medium", "high", "none", "auto", "compress", "compressed"}

# Perfiles internos. No se muestran al usuario: la selección es automática.
_SCAN_JPEG_QUALITY = 65       # páginas-foto / expedientes escaneados
_IMAGE_HEAVY_QUALITY = 70    # PDFs con mayoría de imágenes
_MIXED_JPEG_QUALITY = 75     # PDF mixto con texto/vector + imágenes
_MIN_LOSSY_REDUCTION = 0.08  # no degradar imágenes por menos de 8 % de ahorro
_MIN_ANY_REDUCTION = 0.01    # limpieza sin pérdida: aceptar desde 1 %

# Verificación visual de cada resultado contra el original (escala de grises, 0-255).
# Una recompresión JPEG normal da diferencias medias de ~0,2-1; una imagen dañada
# (desplazada, recortada, con colores cambiados) da 10 o más. Se mide la página
# entera y también por zonas, para detectar un daño localizado (un sello, una firma).
_VISUAL_LONG_SIDE_PX = 240   # render de comparación (lado mayor): ~30 ms por página cambiada
_VISUAL_GRID = 8             # zonas por lado para el control localizado
_VISUAL_MAX_MEAN_DIFF = 6.0
_VISUAL_MAX_CELL_DIFF = 40.0
# Control de COLOR (el de grises no ve un cambio de tono con igual luminosidad,
# p. ej. una tinta plana/Pantone que Ghostscript convierte mal). Se compara el
# color medio de cada zona sin el brillo: el ruido JPEG se promedia y da ~1-7;
# una tinta plana cambiada da 12 o más.
_COLOR_LONG_SIDE_PX = 96
_VISUAL_MAX_CELL_CHROMA = 10.0

_ICC_REF_RE = re.compile(r"/ICCBased\s+(\d+)\s+0\s+R")


@dataclass(frozen=True)
class PdfProfile:
    page_count: int
    sampled_pages: int
    image_page_ratio: float
    full_page_image_ratio: float
    dominant_image_coverage: float
    text_page_ratio: float
    image_bytes_ratio: float
    image_bytes_known: bool
    scan_like: bool
    image_heavy: bool
    has_images: bool


@dataclass
class _Candidate:
    path: Path
    size: int
    method: str
    lossy: bool
    engine: str

    def reduction(self, before: int) -> float:
        return (before - self.size) / before if before else 0.0


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _notify_progress(
    progress_fn: Callable[[dict], None] | None,
    pct: int,
    stage: str,
    message: str,
    *,
    stage_index: int,
    stage_total: int = 6,
) -> None:
    if not progress_fn:
        return
    try:
        progress_fn({
            "pct": max(0, min(100, int(pct))),
            "stage": stage,
            "message": message,
            "stage_index": stage_index,
            "stage_total": stage_total,
        })
    except Exception:
        pass


def _sample_indexes(page_count: int, limit: int = 20) -> list[int]:
    if page_count <= 0:
        return []
    if page_count <= limit:
        return list(range(page_count))
    # Muestra uniforme: incluye primera y última página.
    return sorted({round(i * (page_count - 1) / (limit - 1)) for i in range(limit)})


def _xref_stream_length(doc: fitz.Document, xref: int) -> int | None:
    """Obtiene /Length sin extraer la imagen completa a memoria.

    Devuelve None si no se pudo determinar (para no confundirlo con "0 bytes").
    """
    try:
        kind, value = doc.xref_get_key(xref, "Length")
        if kind == "xref":
            # /Length indirecto ("6 0 R"): común en escaneos con OCR.
            kind, value = "int", doc.xref_object(int(value.split()[0]), compressed=True)
        if kind in {"int", "real"}:
            return max(0, int(float(value)))
    except Exception:
        pass
    return None


def _analyze_pdf(source_path: Path, cancel_check: Callable[[], bool] | None = None) -> PdfProfile:
    """Clasifica el contenido con una muestra visual y el peso de streams de imagen."""
    before = source_path.stat().st_size
    doc = fitz.open(str(source_path))
    try:
        page_count = doc.page_count
        image_pages = 0
        unique_xrefs: set[int] = set()

        # Recuento liviano de imágenes en todas las páginas.
        for page_index, page in enumerate(doc):
            if page_index % 20 == 0:
                raise_if_cancelled(cancel_check)
            images = page.get_images(full=True)
            if images:
                image_pages += 1
                unique_xrefs.update(img[0] for img in images if img and img[0] > 0)

        lengths = [_xref_stream_length(doc, xref) for xref in unique_xrefs]
        image_bytes = sum(length for length in lengths if length is not None)
        image_bytes_known = all(length is not None for length in lengths)
        image_bytes_ratio = min(1.0, image_bytes / before) if before else 0.0
        image_page_ratio = image_pages / page_count if page_count else 0.0

        indexes = _sample_indexes(page_count)
        full_page_images = 0
        dominant_coverage_sum = 0.0
        text_pages = 0

        for idx in indexes:
            raise_if_cancelled(cancel_check)
            page = doc[idx]
            try:
                if (page.get_text("text") or "").strip():
                    text_pages += 1
            except Exception:
                pass

            page_area = abs(page.rect.width * page.rect.height) or 1.0
            max_coverage = 0.0
            seen: set[int] = set()
            for image in page.get_images(full=True):
                xref = image[0]
                if xref <= 0 or xref in seen:
                    continue
                seen.add(xref)
                try:
                    for rect in page.get_image_rects(xref):
                        coverage = abs(rect.width * rect.height) / page_area
                        max_coverage = max(max_coverage, coverage)
                except Exception:
                    continue
            dominant_coverage_sum += min(1.0, max_coverage)
            if max_coverage >= 0.72:
                full_page_images += 1

        sampled = len(indexes)
        full_ratio = full_page_images / sampled if sampled else 0.0
        dominant_coverage = dominant_coverage_sum / sampled if sampled else 0.0
        text_ratio = text_pages / sampled if sampled else 0.0

        # También funciona con escaneos que traen una capa OCR oculta.
        scan_like = full_ratio >= 0.65 and (
            text_ratio <= 0.55 or image_bytes_ratio >= 0.55
        )
        image_heavy = scan_like or dominant_coverage >= 0.55 or (
            image_bytes_ratio >= 0.45 and dominant_coverage >= 0.35
        )

        profile = PdfProfile(
            page_count=page_count,
            sampled_pages=sampled,
            image_page_ratio=round(image_page_ratio, 3),
            full_page_image_ratio=round(full_ratio, 3),
            dominant_image_coverage=round(dominant_coverage, 3),
            text_page_ratio=round(text_ratio, 3),
            image_bytes_ratio=round(image_bytes_ratio, 3),
            image_bytes_known=image_bytes_known,
            scan_like=scan_like,
            image_heavy=image_heavy,
            has_images=bool(unique_xrefs),
        )
        logger.info(
            "compression analysis: pages=%s image_pages=%.1f%% full_page_images=%.1f%% "
            "dominant_coverage=%.1f%% text_pages=%.1f%% image_bytes=%.1f%% "
            "scan_like=%s image_heavy=%s",
            profile.page_count,
            profile.image_page_ratio * 100,
            profile.full_page_image_ratio * 100,
            profile.dominant_image_coverage * 100,
            profile.text_page_ratio * 100,
            profile.image_bytes_ratio * 100,
            profile.scan_like,
            profile.image_heavy,
        )
        return profile
    finally:
        doc.close()


def _validate_candidate(path: Path, expected_pages: int) -> int | None:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return None
        doc = fitz.open(str(path))
        try:
            if doc.needs_pass or doc.page_count != expected_pages:
                return None
        finally:
            doc.close()
        return path.stat().st_size
    except Exception:
        return None


def _page_image_signature(doc: fitz.Document, page: fitz.Page) -> list[tuple]:
    """Huella de las imágenes de una página (tamaño, filtro, bytes). Si coincide
    entre original y resultado, esas imágenes no se tocaron."""
    signature = []
    for image in page.get_images(full=True):
        xref, _smask, width, height, bpc, colorspace = image[:6]
        signature.append((width, height, bpc, colorspace, image[8], _xref_stream_length(doc, xref)))
    return sorted(signature, key=repr)


def _render_gray(page: fitz.Page) -> tuple[int, int, bytes]:
    rect = page.rect
    zoom = _VISUAL_LONG_SIDE_PX / max(rect.width, rect.height, 1.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    return pix.width, pix.height, pix.samples


def _cell_colors(page: fitz.Page) -> dict[tuple[int, int], tuple[float, float, float]]:
    """Color medio (R, G, B) de cada zona de la página, en un render chico."""
    rect = page.rect
    zoom = _COLOR_LONG_SIDE_PX / max(rect.width, rect.height, 1.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
    width, height, samples = pix.width, pix.height, pix.samples
    grid = _VISUAL_GRID
    cell_w = max(1, -(-width // grid))
    cell_h = max(1, -(-height // grid))
    acc: dict[tuple[int, int], list[int]] = {}
    for y in range(height):
        cy = y // cell_h
        base = y * width * 3
        for x in range(width):
            i = base + x * 3
            cell = acc.setdefault((cy, x // cell_w), [0, 0, 0, 0])
            cell[0] += samples[i]
            cell[1] += samples[i + 1]
            cell[2] += samples[i + 2]
            cell[3] += 1
    return {k: (v[0] / v[3], v[1] / v[3], v[2] / v[3]) for k, v in acc.items() if v[3]}


def _color_difference(a: dict, b: dict) -> float:
    """Peor desvío de TONO entre zonas (sin contar el cambio de brillo), 0-255."""
    worst = 0.0
    for key, (ra, ga, ba) in a.items():
        if key not in b:
            continue
        rb, gb, bb = b[key]
        la, lb = (ra + ga + ba) / 3, (rb + gb + bb) / 3
        worst = max(
            worst,
            abs((ra - la) - (rb - lb)),
            abs((ga - la) - (gb - lb)),
            abs((ba - la) - (bb - lb)),
        )
    return worst


def _structure_signature(doc: fitz.Document) -> tuple:
    """Lo que no se ve en el render pero no se puede perder al comprimir:
    campos de formulario, anotaciones, enlaces y marcadores."""
    pages = []
    for page in doc:
        widgets = sum(1 for _ in page.widgets())
        annots = sum(1 for _ in page.annots())
        pages.append((widgets, annots, len(page.get_links())))
    return len(doc.get_toc(simple=True)), tuple(pages)


def _has_form_fields(source_path: Path) -> bool:
    try:
        with fitz.open(str(source_path)) as doc:
            return bool(doc.is_form_pdf)
    except Exception:
        return False


def _visual_difference(a: tuple[int, int, bytes], b: tuple[int, int, bytes]) -> tuple[float, float]:
    """Diferencia media de la página y de la peor zona (0-255)."""
    width, height, pa = a
    _, _, pb = b
    grid = _VISUAL_GRID
    cell_w = max(1, -(-width // grid))
    cell_h = max(1, -(-height // grid))
    cells: dict[tuple[int, int], list[int]] = {}
    total = 0
    for y in range(height):
        row = y * width
        cy = y // cell_h
        for x in range(width):
            d = abs(pa[row + x] - pb[row + x])
            total += d
            cell = cells.setdefault((cy, x // cell_w), [0, 0])
            cell[0] += d
            cell[1] += 1
    mean = total / max(1, width * height)
    worst = max((s / n for s, n in cells.values() if n), default=0.0)
    return mean, worst


def _check_visual_integrity(
    source_path: Path,
    candidate_path: Path,
    cancel_check: Callable[[], bool] | None = None,
) -> str | None:
    """Compara cómo se VE cada página del resultado contra el original.

    Devuelve None si todo coincide, o una descripción del problema. Antes solo se
    verificaba que el PDF abriera y tuviera las mismas páginas: un resultado con
    imágenes dañadas pasaba como válido y el usuario no se enteraba.
    Las páginas cuyas imágenes y contenido no cambiaron se saltean.
    """
    with fitz.open(str(source_path)) as original, fitz.open(str(candidate_path)) as result:
        if original.page_count != result.page_count:
            return "cantidad de páginas distinta"
        # Ghostscript borraba los campos de formulario (y su contenido) sin que el
        # render lo mostrara si el campo estaba vacío o sin apariencia propia.
        sig_original, sig_result = _structure_signature(original), _structure_signature(result)
        if sig_original != sig_result:
            if sig_original[0] != sig_result[0]:
                return "se perdieron marcadores del documento"
            for index, (before, after) in enumerate(zip(sig_original[1], sig_result[1])):
                if before != after:
                    return (
                        f"página {index + 1}: cambiaron formularios/anotaciones/enlaces "
                        f"({before} → {after})"
                    )
        for index in range(original.page_count):
            raise_if_cancelled(cancel_check)
            page_a, page_b = original[index], result[index]
            if page_a.rect != page_b.rect:
                return f"página {index + 1}: cambió el tamaño de la página"
            # Solo se saltea si ni las imágenes ni el contenido dibujado cambiaron.
            sig_a = _page_image_signature(original, page_a)
            if (
                sig_a
                and sig_a == _page_image_signature(result, page_b)
                and page_a.read_contents() == page_b.read_contents()
            ):
                continue
            render_a, render_b = _render_gray(page_a), _render_gray(page_b)
            if render_a[:2] != render_b[:2]:
                return f"página {index + 1}: cambió el tamaño renderizado"
            mean, worst = _visual_difference(render_a, render_b)
            if mean > _VISUAL_MAX_MEAN_DIFF or worst > _VISUAL_MAX_CELL_DIFF:
                return (
                    f"página {index + 1}: se ve distinta al original "
                    f"(diferencia media {mean:.1f}, peor zona {worst:.1f})"
                )
            chroma = _color_difference(_cell_colors(page_a), _cell_colors(page_b))
            if chroma > _VISUAL_MAX_CELL_CHROMA:
                return f"página {index + 1}: cambiaron los colores (desvío {chroma:.1f})"
    return None


def _add_candidate(
    candidates: list[_Candidate],
    path: Path,
    method: str,
    engine: str,
    lossy: bool,
    before: int,
    expected_pages: int,
    *,
    source_path: Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    size = _validate_candidate(path, expected_pages)
    if size is None or size >= before:
        _safe_unlink(path)
        return False

    reduction = (before - size) / before if before else 0.0
    minimum = _MIN_LOSSY_REDUCTION if lossy else _MIN_ANY_REDUCTION
    if reduction < minimum:
        logger.info(
            "compression candidate rejected: method=%s reduction=%.1f%% (minimum %.1f%%)",
            method,
            reduction * 100,
            minimum * 100,
        )
        _safe_unlink(path)
        return False

    if source_path is not None:
        try:
            problem = _check_visual_integrity(source_path, path, cancel_check)
        except OperationCancelled:
            _safe_unlink(path)
            raise
        except Exception as exc:  # si no se puede verificar, no se arriesga
            problem = f"no se pudo verificar ({exc})"
        if problem:
            logger.warning("compression candidate DISCARDED: method=%s %s", method, problem)
            _safe_unlink(path)
            return False

    candidates.append(_Candidate(path, size, method, lossy, engine))
    logger.info(
        "compression candidate accepted: method=%s size=%.2fMB reduction=%.1f%%",
        method,
        size / 1024 / 1024,
        reduction * 100,
    )
    return True


def _build_structural_candidate(
    source_path: Path,
    output_path: Path,
    *,
    compression_effort: int = 100,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    raise_if_cancelled(cancel_check)
    doc = fitz.open(str(source_path))
    try:
        doc.save(
            str(output_path),
            garbage=4,
            clean=True,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            use_objstms=1,
            compression_effort=compression_effort,
        )
    finally:
        doc.close()
    raise_if_cancelled(cancel_check)


@contextmanager
def _heartbeat(
    progress_fn: Callable[[dict], None] | None,
    pct: int,
    stage: str,
    message: str,
    *,
    stage_index: int,
    interval: float = 2.0,
):
    """Emite "… N s" mientras corre una llamada larga en C sin puntos de progreso
    (rewrite_images/save), para que la barra no parezca colgada."""
    if not progress_fn:
        yield
        return
    stop = threading.Event()
    started = time.monotonic()

    def _beat() -> None:
        while not stop.wait(interval):
            elapsed = int(time.monotonic() - started)
            _notify_progress(progress_fn, pct, stage, f"{message} {elapsed} s", stage_index=stage_index)

    thread = threading.Thread(target=_beat, name="bigpdf-compress-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


def _normalize_gray_icc_images(doc: fitz.Document) -> int:
    """Marca como /DeviceGray las imágenes con perfil ICC de escala de grises (N=1).

    rewrite_images() de PyMuPDF recodifica esas imágenes como JPEG a color (3
    canales) pero deja declarado el perfil de 1 canal: el visor lee los datos con
    el formato equivocado y la página sale desarmada (imagen ampliada, corrida y
    repetida). Pasa con escaneos en grises muy comunes. Con /DeviceGray la
    recompresión sale bien; visualmente es equivalente (diferencia < 1/255).
    """
    fixed = 0
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "Subtype")[1] != "/Image":
                continue
            kind, value = doc.xref_get_key(xref, "ColorSpace")
            if kind == "xref":
                value = doc.xref_object(int(value.split()[0]), compressed=True)
            elif kind != "array":
                continue
            match = _ICC_REF_RE.search(value)
            if not match:
                continue
            components = doc.xref_get_key(int(match.group(1)), "N")[1]
            # Lo mismo pasa con los perfiles CMYK (N=4, imprentas y algunos escáneres):
            # se recodificaban mal y la verificación descartaba la compresión entera.
            device = {"1": "/DeviceGray", "4": "/DeviceCMYK"}.get(components)
            if device:
                doc.xref_set_key(xref, "ColorSpace", device)
                fixed += 1
        except Exception:
            continue
    return fixed


def _image_icc_profiles(doc: fitz.Document) -> dict[int, int]:
    """Imágenes con perfil ICC: {xref de la imagen: xref del perfil}."""
    found: dict[int, int] = {}
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "Subtype")[1] != "/Image":
                continue
            kind, value = doc.xref_get_key(xref, "ColorSpace")
            if kind == "xref":
                value = doc.xref_object(int(value.split()[0]), compressed=True)
            elif kind != "array":
                continue
            match = _ICC_REF_RE.search(value)
            if match:
                found[xref] = int(match.group(1))
        except Exception:
            continue
    return found


def _untag_added_icc_profiles(doc: fitz.Document, original_profiles: set[int]) -> int:
    """rewrite_images() le agrega a cada imagen recodificada un perfil ICC genérico.
    En MuPDF no cambia nada, pero otros visores (Adobe, Chrome, poppler) muestran
    los CMYK con otros colores. Se vuelve al espacio de color "Device" equivalente,
    que es lo que tenían antes de recomprimir."""
    fixed = 0
    for image_xref, profile_xref in _image_icc_profiles(doc).items():
        if profile_xref in original_profiles:
            continue
        try:
            components = doc.xref_get_key(profile_xref, "N")[1]
            device = {"1": "/DeviceGray", "3": "/DeviceRGB", "4": "/DeviceCMYK"}.get(components)
            if device:
                doc.xref_set_key(image_xref, "ColorSpace", device)
                fixed += 1
        except Exception:
            continue
    return fixed


def _build_image_rewrite_candidate(
    source_path: Path,
    output_path: Path,
    quality: int,
    *,
    lossless: bool = True,
    compression_effort: int = 100,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    """Recomprime imágenes internas sin rasterizar páginas ni reducir dimensiones.

    ``lossless=False`` deja intactas las imágenes que hoy están sin pérdida
    (capturas de pantalla, planos, diagramas) para no agregarles artefactos JPEG.
    """
    raise_if_cancelled(cancel_check)
    doc = fitz.open(str(source_path))
    try:
        rewrite = getattr(doc, "rewrite_images", None)
        if not callable(rewrite):
            raise RuntimeError("La versión instalada de PyMuPDF no incluye rewrite_images().")

        gray_fixed = _normalize_gray_icc_images(doc)
        if gray_fixed:
            logger.info("image rewrite: %s imagen(es) ICC gris/CMYK pasadas a Device*", gray_fixed)
        original_profiles = set(_image_icc_profiles(doc).values())
        rewrite(
            dpi_threshold=None,
            dpi_target=0,       # conserva ancho y alto original de cada imagen
            quality=quality,
            lossy=True,
            lossless=lossless,
            bitonal=False,      # preserva imágenes 1-bit / códigos puros
            color=True,
            gray=True,
            set_to_gray=False,
        )
        _untag_added_icc_profiles(doc, original_profiles)
        raise_if_cancelled(cancel_check)
        doc.save(
            str(output_path),
            garbage=4,
            clean=True,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            use_objstms=1,
            compression_effort=compression_effort,
        )
    finally:
        doc.close()
    raise_if_cancelled(cancel_check)


def _choose_candidate(candidates: list[_Candidate], before: int) -> _Candidate | None:
    if not candidates:
        return None

    best = min(candidates, key=lambda c: c.size)

    # Si una alternativa sin pérdida queda a menos de 8 % del mejor resultado,
    # preferirla para evitar una recompresión innecesaria.
    lossless = [c for c in candidates if not c.lossy]
    if best.lossy and lossless:
        best_lossless = min(lossless, key=lambda c: c.size)
        if best_lossless.size <= best.size * 1.08:
            best = best_lossless

    logger.info(
        "compression selected: method=%s engine=%s %.2fMB -> %.2fMB (%.1f%%)",
        best.method,
        best.engine,
        before / 1024 / 1024,
        best.size / 1024 / 1024,
        best.reduction(before) * 100,
    )
    return best


def compress_pdf_file(
    source_path: Path,
    dest_path: Path,
    level: str = "auto",
    *,
    large_batch: bool = False,
    compression_effort: int | None = None,
    progress_fn: Callable[[dict], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """Comprime automáticamente y conserva el original sin cambiar estrategias.

    En lote grande se mantienen el análisis y los candidatos actuales. Únicamente
    se reduce ``compression_effort`` de PyMuPDF para evitar que el proceso consuma
    CPU durante demasiado tiempo. No se alteran resolución, calidad JPEG ni lógica
    de elección del candidato.
    """
    ensure_storage_dirs()
    source_path = Path(source_path)
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    before = source_path.stat().st_size
    effort = int(compression_effort if compression_effort is not None else (65 if large_batch else 100))
    effort = max(1, min(100, effort))

    raise_if_cancelled(cancel_check)
    if level == "none":
        shutil.copy2(str(source_path), str(dest_path))
        return {
            "engine": "none", "level": "none", "method": "original",
            "before_bytes": before, "after_bytes": before, "saved_bytes": 0,
            "saved_percent": 0.0, "used_fallback": False,
            "balanced_levels_available": is_ghostscript_available(),
            "large_batch": large_batch, "compression_effort": effort,
        }

    _notify_progress(
        progress_fn, 3, "Analizando contenido", "Analizando el contenido del PDF…", stage_index=1,
    )
    profile = _analyze_pdf(source_path, cancel_check=cancel_check)
    raise_if_cancelled(cancel_check)
    candidates: list[_Candidate] = []
    temp_paths: list[Path] = []
    try:
        structural = dest_path.parent / f"{dest_path.stem}.structural.tmp.pdf"
        temp_paths.append(structural)
        _notify_progress(
            progress_fn, 18, "Optimización estructural",
            f"Optimizando la estructura del PDF (esfuerzo {effort}/100)…",
            stage_index=2,
        )
        try:
            _build_structural_candidate(
                source_path, structural,
                compression_effort=effort,
                cancel_check=cancel_check,
            )
            _add_candidate(
                candidates, structural, "pymupdf_structural", "pymupdf", False,
                before, profile.page_count,
                source_path=source_path, cancel_check=cancel_check,
            )
        except Exception as exc:
            if isinstance(exc, OperationCancelled):
                raise
            logger.warning("structural compression failed: %s", exc)
            _safe_unlink(structural)

        image_candidate_ok = False
        # Si las imágenes pesan menos que el ahorro mínimo exigido a una recompresión
        # con pérdida, recomprimirlas nunca puede alcanzarlo: se saltea (en PDFs ya
        # comprimidos tardaba ~10 s para terminar descartado).
        images_worth_rewriting = profile.has_images and (
            not profile.image_bytes_known or profile.image_bytes_ratio >= _MIN_LOSSY_REDUCTION
        )
        if profile.has_images and not images_worth_rewriting:
            logger.info(
                "image rewrite skipped: image_bytes=%.1f%% < %.1f%%",
                profile.image_bytes_ratio * 100, _MIN_LOSSY_REDUCTION * 100,
            )
        if images_worth_rewriting:
            if profile.scan_like:
                quality = _SCAN_JPEG_QUALITY
                profile_name = "scan"
            elif profile.image_heavy:
                quality = _IMAGE_HEAVY_QUALITY
                profile_name = "image_heavy"
            else:
                quality = _MIXED_JPEG_QUALITY
                profile_name = "mixed"

            rewritten = dest_path.parent / f"{dest_path.stem}.images_q{quality}.tmp.pdf"
            temp_paths.append(rewritten)
            _notify_progress(
                progress_fn, 43, "Comprimiendo imágenes",
                "Comprimiendo imágenes…",
                stage_index=3,
            )
            try:
                with _heartbeat(
                    progress_fn, 50, "Comprimiendo imágenes", "Comprimiendo imágenes…",
                    stage_index=3,
                ):
                    _build_image_rewrite_candidate(
                        source_path, rewritten, quality,
                        # En PDFs mixtos no pasar a JPEG capturas/diagramas sin pérdida.
                        lossless=profile_name != "mixed",
                        compression_effort=effort,
                        cancel_check=cancel_check,
                    )
                image_candidate_ok = _add_candidate(
                    candidates, rewritten,
                    f"pymupdf_rewrite_{profile_name}_q{quality}",
                    "pymupdf_image_rewrite", True, before, profile.page_count,
                    source_path=source_path, cancel_check=cancel_check,
                )
            except Exception as exc:
                if isinstance(exc, OperationCancelled):
                    raise
                logger.warning("image rewrite compression failed: %s", exc)
                _safe_unlink(rewritten)

        raise_if_cancelled(cancel_check)
        # Con formularios no se usa Ghostscript: los aplana o los borra (con lo que
        # el usuario completó). La verificación igual lo descartaría, pero así no
        # se gasta el tiempo.
        if (
            is_ghostscript_available()
            and (not profile.scan_like or not image_candidate_ok)
            and not _has_form_fields(source_path)
        ):
            gs_path = dest_path.parent / f"{dest_path.stem}.ghostscript.tmp.pdf"
            temp_paths.append(gs_path)
            _notify_progress(
                progress_fn, 68, "Procesamiento Ghostscript",
                "Procesando una alternativa con Ghostscript…",
                stage_index=4,
            )
            try:
                result = compress_with_ghostscript(
                    source_path, gs_path, "high",
                    cancel_check=cancel_check,
                    timeout_seconds=900 if large_batch else 180,
                    progress_fn=lambda message: _notify_progress(
                        progress_fn, 72, "Procesamiento Ghostscript", message,
                        stage_index=4,
                    ),
                )
                if result.get("success"):
                    _add_candidate(
                        candidates, gs_path,
                        f"gs_{result.get('method', 'automatic')}",
                        "ghostscript", True, before, profile.page_count,
                        source_path=source_path, cancel_check=cancel_check,
                    )
                else:
                    _safe_unlink(gs_path)
            except Exception as exc:
                if isinstance(exc, OperationCancelled):
                    raise
                logger.warning("ghostscript compression failed: %s", exc)
                _safe_unlink(gs_path)

        raise_if_cancelled(cancel_check)
        _notify_progress(
            progress_fn, 92, "Validando resultado",
            "Comparando y validando las versiones generadas…",
            stage_index=6,
        )
        selected = _choose_candidate(candidates, before)
        if selected is None:
            shutil.copy2(str(source_path), str(dest_path))
            after = before
            method = "original_no_useful_reduction"
            engine = "none"
            lossy = False
        else:
            try:
                selected.path.replace(dest_path)
            except OSError:
                shutil.copy2(str(selected.path), str(dest_path))
                _safe_unlink(selected.path)
            after = dest_path.stat().st_size
            method = selected.method
            engine = selected.engine
            lossy = selected.lossy
    finally:
        # Siempre limpiar temporales, también al cancelar o ante un error.
        for candidate in candidates:
            if candidate.path != dest_path:
                _safe_unlink(candidate.path)
        for path in temp_paths:
            if path != dest_path:
                _safe_unlink(path)

    raise_if_cancelled(cancel_check)
    _notify_progress(progress_fn, 100, "Compresión finalizada", "Compresión finalizada.", stage_index=6)
    saved = max(0, before - after)
    pct = round(saved / before * 100, 1) if before else 0.0
    return {
        "engine": engine, "level": "auto", "method": method,
        "before_bytes": before, "after_bytes": after,
        "saved_bytes": saved, "saved_percent": pct,
        "used_fallback": engine not in {"ghostscript", "pymupdf_image_rewrite"},
        "lossy": lossy, "scan_like": profile.scan_like,
        "image_heavy": profile.image_heavy,
        "balanced_levels_available": is_ghostscript_available(),
        "large_batch": large_batch, "compression_effort": effort,
    }


def compress_pdf(
    source_path: Path,
    level: str = "auto",
    *,
    progress_fn: Callable[[dict], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[str, Path, int, int]:
    """Comprime un documento individual y lo registra en la carpeta de exportación."""
    if level not in VALID_LEVELS:
        logger.warning("compress_pdf: nivel inválido %r, usando automático", level)
        level = "auto"
    export_id = str(uuid.uuid4())
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Subcarpeta propia: la limpieza al cancelar una exportación borra los
    # temporales de EXPORTS_DIR y no debe tocar los de una compresión en curso.
    out_path = EXPORTS_DIR / "single" / f"bigpdf_compressed_{ts}_{export_id[:8]}.pdf"
    try:
        metrics = compress_pdf_file(
            source_path, out_path, level,
            progress_fn=progress_fn, cancel_check=cancel_check,
        )
    except BaseException:
        _safe_unlink(out_path)
        raise
    return export_id, out_path, metrics["before_bytes"], metrics["after_bytes"]

