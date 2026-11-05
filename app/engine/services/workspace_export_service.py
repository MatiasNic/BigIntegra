"""Servicio de exportación de workspace: construye PDFs y ZIPs.

La usan el bridge de escritorio (QWebChannel) y el proceso independiente de
exportación.
"""
from __future__ import annotations

import logging
import re
import shutil
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import fitz

from app.config import EXPORTS_DIR, UPLOADS_DIR
from app.engine.cancellation import raise_if_cancelled
from app.engine.services.compress_service import compress_pdf_file
from app.engine.services.export_registry import set_export

logger = logging.getLogger("bigpdf")

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _clean_stem(stem: str, fallback: str) -> str:
    # "/", "\\" y ":" se cambian por un guion ("Informe 2024/05" → "Informe 2024-05"):
    # borrarlos pegaba las palabras, y dejarlos cortaba el nombre o fallaba en Windows.
    stem = re.sub(r'\s*[\\/:]\s*', lambda m: ' - ' if m.group(0).strip() != m.group(0) else '-', stem)
    stem = re.sub(r'[*?"<>|\x00-\x1f]', '', stem)
    stem = re.sub(r'\s+', ' ', stem).strip().strip('.').strip()
    stem = stem[:80].rstrip(' .')
    if not stem:
        stem = fallback
    if stem.split('.')[0].strip().upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return stem


def sanitize_filename(name: str, fallback: str = "parte") -> str:
    """Nombre de archivo PDF válido en Windows (siempre termina en ".pdf")."""
    name = (name or "").strip()
    stem = name[:-4] if name.lower().endswith('.pdf') else name
    return _clean_stem(stem, fallback) + '.pdf'


def sanitize_download_filename(filename: str, ext: str, fallback: str = "documento") -> str:
    """Nombre válido en Windows con la extensión ``ext`` (".pdf" o ".zip").

    Lo usa el guardado: el nombre llega tal cual lo escribió el usuario
    ("Expte 12/2024: final") y antes un "/" lo recortaba a lo que venía después.
    """
    name = (filename or "").strip()
    ext = ext.lower()
    stem = name[:-len(ext)] if ext and name.lower().endswith(ext) else name
    return _clean_stem(stem, fallback) + ext


def _emit_progress(progress_fn: Callable[[dict], None] | None, **payload) -> None:
    if not progress_fn:
        return
    try:
        progress_fn(payload)
    except Exception:
        pass


# ─── Conservar la estructura del documento ────────────────────────────────────
# insert_pdf copia cada página por separado: sin esto se perdían los marcadores
# (índice), los enlaces internos, la numeración de páginas (i, ii…), los datos del
# documento (título, autor), los adjuntos, y las capas ocultas pasaban a verse.
# Cada paso es independiente y "best effort": si uno falla, se registra y el PDF
# se exporta igual (sin esa parte), nunca se rompe la exportación.

_INTERNAL_LINK_KINDS = {fitz.LINK_GOTO, fitz.LINK_NAMED}


def _first_positions(placements: list[tuple[str, int]]) -> dict[tuple[str, int], int]:
    """(documento, página de origen) → primera posición en el PDF exportado."""
    first: dict[tuple[str, int], int] = {}
    for out_index, key in enumerate(placements):
        first.setdefault(key, out_index)
    return first


def _restore_internal_links(dst, placements, sources, first) -> None:
    for out_index, (doc_id, pn) in enumerate(placements):
        page = dst[out_index]
        src_page = sources[doc_id][pn]
        for link in src_page.get_links():
            if link.get("kind") not in _INTERNAL_LINK_KINDS:
                continue
            target = link.get("page", -1)
            if target is None or target < 0 or target == pn:
                continue  # sin destino, o a la misma hoja (esos ya los copia insert_pdf)
            new_target = first.get((doc_id, target))
            if new_target is None:
                continue  # apunta a una hoja que no se exporta
            # get_links da la zona en coordenadas de la página tal como se ve (con su
            # giro) e insert_link la espera sin girar: sin esto el enlace se corría.
            area = fitz.Rect(link["from"]) * src_page.derotation_matrix
            new_link = {
                "kind": fitz.LINK_GOTO,
                "from": area,
                "page": new_target,
                "to": link.get("to") or fitz.Point(0, 0),
                "zoom": link.get("zoom", 0),
            }
            page.insert_link(new_link)


def _restore_toc(dst, placements, sources, first) -> None:
    toc = []
    for doc_id in dict.fromkeys(doc_id for doc_id, _ in placements):
        for entry in sources[doc_id].get_toc(simple=False):
            level, title, page = entry[0], entry[1], entry[2]
            new_page = first.get((doc_id, page - 1)) if page and page > 0 else None
            if new_page is None:
                continue
            dest = dict(entry[3]) if len(entry) > 3 and isinstance(entry[3], dict) else {}
            dest["kind"] = fitz.LINK_GOTO
            dest["page"] = new_page
            dest.pop("xref", None)
            dest.pop("nameddest", None)
            # Si se perdió el marcador "padre", el hijo sube de nivel (no puede saltear).
            level = max(1, min(level, (toc[-1][0] + 1) if toc else 1))
            toc.append([level, title, new_page + 1, dest])
    if toc:
        dst.set_toc(toc)


def _restore_page_labels(dst, placements, sources) -> None:
    doc_ids = dict.fromkeys(doc_id for doc_id, _ in placements)
    rules_by_doc = {doc_id: sources[doc_id].get_page_labels() for doc_id in doc_ids}
    if not any(rules_by_doc.values()):
        return
    new_rules = []
    previous = None  # (doc_id, índice de regla, número) de la página anterior
    for out_index, (doc_id, pn) in enumerate(placements):
        rules = sorted(rules_by_doc[doc_id], key=lambda r: r["startpage"])
        rule_index = None
        for idx, rule in enumerate(rules):
            if rule["startpage"] <= pn:
                rule_index = idx
        if rule_index is None:
            # Hoja sin numeración propia: la que mostraría cualquier visor (1, 2, 3…).
            current = (doc_id, None, out_index + 1)
            rule = {"prefix": "", "style": "D"}
            number = out_index + 1
        else:
            rule = rules[rule_index]
            number = int(rule.get("firstpagenum") or 1) + pn - rule["startpage"]
            current = (doc_id, rule_index, number)
        continues = (
            previous is not None
            and previous[0] == current[0]
            and previous[1] == current[1]
            and previous[2] + 1 == current[2]
        )
        if not continues:
            new_rules.append({
                "startpage": out_index,
                "prefix": rule.get("prefix", ""),
                "style": rule.get("style", ""),
                "firstpagenum": number,
            })
        previous = current
    dst.set_page_labels(new_rules)


def _restore_metadata(dst, placements, sources) -> None:
    doc_ids = list(dict.fromkeys(doc_id for doc_id, _ in placements))
    # Solo si todo sale de UN documento: con varios, el título de uno no describe el resultado.
    if len(doc_ids) != 1:
        return
    meta = sources[doc_ids[0]].metadata or {}
    keep = {k: meta.get(k) or "" for k in ("title", "author", "subject", "keywords", "creator")}
    if any(keep.values()):
        dst.set_metadata({**(dst.metadata or {}), **keep})


def _restore_attachments(dst, placements, sources) -> None:
    taken = set(dst.embfile_names())
    for doc_id in dict.fromkeys(doc_id for doc_id, _ in placements):
        src = sources[doc_id]
        for name in src.embfile_names():
            info = src.embfile_info(name)
            new_name = name
            counter = 2
            while new_name in taken:
                new_name = f"{name} ({counter})"
                counter += 1
            dst.embfile_add(
                new_name, src.embfile_get(name),
                filename=info.get("filename") or name,
                ufilename=info.get("ufilename") or info.get("filename") or name,
                desc=info.get("desc") or "",
            )
            taken.add(new_name)


def _restore_layers(dst, placements, sources) -> None:
    """Las capas (OCG) copiadas con las páginas quedaban sin /OCProperties en el
    catálogo y los visores mostraban TODO, también lo que en el original estaba
    oculto (borradores, notas internas). Se vuelve a declarar cada capa con el
    estado (visible/oculta) que tenía en su documento de origen."""
    states: dict[str, bool] = {}
    for doc_id in dict.fromkeys(doc_id for doc_id, _ in placements):
        for info in (sources[doc_id].get_ocgs() or {}).values():
            name = info.get("name") or ""
            # Si dos documentos tienen una capa con el mismo nombre y distinto
            # estado, gana "visible" (nunca esconder algo que en otro se veía).
            states[name] = states.get(name, False) or bool(info.get("on"))
    if not states:
        return
    ocg_xrefs: list[int] = []
    off: list[int] = []
    for xref in range(1, dst.xref_length()):
        try:
            if dst.xref_get_key(xref, "Type")[1] != "/OCG":
                continue
            kind, raw_name = dst.xref_get_key(xref, "Name")
        except Exception:
            continue
        name = raw_name[1:-1] if kind == "string" and raw_name.startswith("(") else raw_name
        ocg_xrefs.append(xref)
        if name in states and not states[name]:
            off.append(xref)
    if not ocg_xrefs:
        return
    refs = " ".join(f"{x} 0 R" for x in ocg_xrefs)
    off_refs = " ".join(f"{x} 0 R" for x in off)
    dst.xref_set_key(
        dst.pdf_catalog(), "OCProperties",
        f"<</OCGs[{refs}]/D<</Order[{refs}]/ON[]/OFF[{off_refs}]>>>>",
    )


def _preserve_structure(dst, placements, sources) -> None:
    first = _first_positions(placements)
    steps = (
        ("enlaces internos", lambda: _restore_internal_links(dst, placements, sources, first)),
        ("marcadores", lambda: _restore_toc(dst, placements, sources, first)),
        ("numeración de páginas", lambda: _restore_page_labels(dst, placements, sources)),
        ("datos del documento", lambda: _restore_metadata(dst, placements, sources)),
        ("adjuntos", lambda: _restore_attachments(dst, placements, sources)),
        ("capas", lambda: _restore_layers(dst, placements, sources)),
    )
    for label, step in steps:
        try:
            step()
        except Exception as exc:
            logger.warning("build_pdf: no se pudo conservar %s: %s", label, exc)


def build_pdf(
    pages_config,
    compression: str = "none",
    name: str = "",
    progress_fn: Callable[[dict], None] | None = None,
    *,
    cancel_check: Callable[[], bool] | None = None,
    large_batch: bool = False,
    compression_effort: int | None = None,
) -> tuple[str, Path, int, int]:
    """Construye un PDF manteniendo la lógica de páginas de BIGPDF 1.4.

    No cambia la apertura de fuentes, la inserción página por página ni el análisis
    de compresión. Agrega etapas reales, cancelación y el perfil de esfuerzo para
    lotes grandes.
    """
    total_pages = len(pages_config)
    logger.info(
        "build_pdf START: name=%r pages=%s compression=%r large_batch=%s effort=%s",
        name, total_pages, compression, large_batch, compression_effort,
    )
    open_docs: dict[str, fitz.Document] = {}
    missing_docs: list[str] = []
    skipped_pages = 0
    placements: list[tuple[str, int]] = []  # (documento, página de origen) por página exportada
    dst = fitz.open()

    _emit_progress(
        progress_fn, pct=3, stage="Preparando documento",
        message=f"Preparando {total_pages} páginas…", stage_index=1, stage_total=9,
    )
    try:
        for i, pc in enumerate(pages_config):
            if i % 10 == 0:
                raise_if_cancelled(cancel_check)
            doc_id = pc.source_document_id
            if doc_id not in open_docs:
                p = UPLOADS_DIR / f"{doc_id}.pdf"
                if not p.exists():
                    logger.warning("build_pdf: source NOT FOUND -> %s", p)
                    if doc_id not in missing_docs:
                        missing_docs.append(doc_id)
                    skipped_pages += 1
                    continue
                open_docs[doc_id] = fitz.open(str(p))
            src = open_docs[doc_id]
            pn = pc.page_num
            if 0 <= pn < src.page_count:
                # final=False: conserva el mapa de objetos ya copiados entre llamadas,
                # así fuentes/imágenes compartidas se copian una sola vez y no una
                # vez por página (con final=True el PDF podía crecer 25-60x).
                dst.insert_pdf(src, from_page=pn, to_page=pn, final=False)
                placements.append((doc_id, pn))
                # La rotación de la UI es un delta sobre la orientación propia de la
                # página (/Rotate), igual que la muestra pdf.js en las miniaturas.
                if pc.rotation:
                    dst[-1].set_rotation((src[pn].rotation + pc.rotation) % 360)
            else:
                logger.warning("build_pdf: page out of range doc=%s page=%s", doc_id, pn)
                skipped_pages += 1

            if (i + 1) % 20 == 0 or i + 1 == total_pages:
                pct = 5 + int((i + 1) / max(1, total_pages) * 50)
                _emit_progress(
                    progress_fn, pct=pct, stage="Armando documento",
                    message=f"Armando documento: {i + 1}/{total_pages} páginas…",
                    stage_index=2, stage_total=9,
                )
        if not missing_docs and not skipped_pages and placements:
            raise_if_cancelled(cancel_check)
            _preserve_structure(dst, placements, open_docs)
    except BaseException:
        dst.close()
        raise
    finally:
        for document in open_docs.values():
            document.close()

    raise_if_cancelled(cancel_check)
    if missing_docs or skipped_pages or dst.page_count == 0:
        # No exportar un PDF incompleto en silencio: el usuario creería que salió bien.
        dst.close()
        if missing_docs:
            raise ValueError(
                f"Faltan {len(missing_docs)} archivo(s) fuente de la sesión "
                f"({skipped_pages} página(s) afectadas). "
                "Volvé a importar los archivos y exportá de nuevo."
            )
        if skipped_pages:
            raise ValueError(
                f"{skipped_pages} página(s) no existen en su archivo de origen. "
                "Volvé a importar los archivos y exportá de nuevo."
            )
        raise ValueError("Sin páginas para exportar.")

    export_id = str(uuid.uuid4())
    if name and name.strip():
        filename = sanitize_filename(name.strip())
        out = EXPORTS_DIR / filename
        if out.exists():
            stem = filename[:-4] if filename.lower().endswith('.pdf') else filename
            out = EXPORTS_DIR / f"{stem}_{export_id[:8]}.pdf"
    else:
        out = EXPORTS_DIR / f"parte_{export_id[:8]}.pdf"

    logger.info(
        "build_pdf SAVING: filename=%r pages=%s compression=%r",
        out.name, dst.page_count, compression,
    )
    _emit_progress(
        progress_fn, pct=58, stage="Guardando PDF temporal",
        message="Guardando el PDF temporal…", stage_index=3, stage_total=9,
    )
    raise_if_cancelled(cancel_check)

    tmp = EXPORTS_DIR / f"tmp_{export_id[:8]}.pdf"
    try:
        dst.save(str(tmp), garbage=1)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        dst.close()
    try:
        raise_if_cancelled(cancel_check)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    size_before_bytes = tmp.stat().st_size
    _emit_progress(
        progress_fn, pct=65, stage="PDF temporal preparado",
        message=f"PDF temporal preparado ({size_before_bytes / 1024 / 1024:.1f} MB).",
        stage_index=3, stage_total=9,
    )

    if compression != "none":
        def _compression_progress(event: dict) -> None:
            internal_pct = max(0, min(100, int(event.get("pct", 0))))
            overall_pct = 66 + int(internal_pct * 0.30)
            _emit_progress(
                progress_fn,
                pct=min(96, overall_pct),
                stage=event.get("stage", "Comprimiendo PDF"),
                message=event.get("message", "Comprimiendo PDF…"),
                stage_index=min(8, 3 + int(event.get("stage_index", 1))),
                stage_total=9,
            )

        try:
            compress_pdf_file(
                tmp, out, compression,
                large_batch=large_batch,
                compression_effort=compression_effort,
                progress_fn=_compression_progress,
                cancel_check=cancel_check,
            )
        except BaseException:
            # Si se canceló justo al final, compress_pdf_file ya pudo haber dejado
            # el resultado en `out`: no dejarlo huérfano en la sesión.
            out.unlink(missing_ok=True)
            raise
        finally:
            tmp.unlink(missing_ok=True)
    else:
        _emit_progress(
            progress_fn, pct=90, stage="Preparando archivo final",
            message="Preparando el archivo final…", stage_index=8, stage_total=9,
        )
        try:
            tmp.replace(out)
        except OSError:
            shutil.copy2(str(tmp), str(out))
            tmp.unlink(missing_ok=True)

    try:
        raise_if_cancelled(cancel_check)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    size = out.stat().st_size
    _emit_progress(
        progress_fn, pct=98, stage="Validando archivo final",
        message="Validando el archivo final…", stage_index=9, stage_total=9,
    )
    logger.info("build_pdf DONE: out=%r size_after=%s KB", out.name, size // 1024)
    return export_id, out, size, size_before_bytes


def build_zip(paths: list[Path], names: list[str]) -> str:
    """Construye un ZIP con los PDFs exportados y registra el export.

    Returns export_id del ZIP.
    """
    export_id = str(uuid.uuid4())
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = EXPORTS_DIR / f"bigpdf_zip_{ts}_{export_id[:8]}.zip"

    safe_names: list[str] = []
    # Windows no distingue mayúsculas: "Informe.pdf" e "informe.pdf" chocan al extraer.
    _seen: set[str] = set()
    for i, raw_name in enumerate(names):
        n = (raw_name or '').strip()
        if not n or n.lower().startswith('bigpdf_export') or n.lower().startswith('bigpdf_zip'):
            n = f"Parte {i + 1}.pdf"
        if not n.lower().endswith('.pdf'):
            n += '.pdf'
        stem = n[:-4]
        suffix = 2
        while n.lower() in _seen:
            n = f"{stem}_{suffix}.pdf"
            suffix += 1
        _seen.add(n.lower())
        safe_names.append(n)

    # ZIP_STORED: los PDFs ya vienen comprimidos; deflate tardaba ~15x más sin
    # reducir el tamaño de forma apreciable.
    missing = [name for path, name in zip(paths, safe_names) if not Path(path).exists()]
    if missing:
        # Antes se salteaban en silencio y el ZIP salía incompleto.
        raise RuntimeError(f"No se pudo armar el ZIP: falta {', '.join(missing)}.")
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_STORED) as zf:
        for path, name in zip(paths, safe_names):
            logger.debug(f"zip arcname={name!r} physical={path.name!r}")
            zf.write(str(path), name)

    set_export(export_id, zip_path, f"bigpdf_partes_{ts}.zip", "application/zip")
    return export_id
