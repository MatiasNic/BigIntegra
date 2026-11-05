"""Proceso independiente de exportación de BIGPDF.

Protocolo stdout: una línea JSON por evento. La interfaz principal solo supervisa
este proceso; PyMuPDF y Ghostscript no comparten el proceso de QtWebEngine.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import EXPORTS_DIR, UPLOADS_DIR, ensure_storage_dirs
from app.engine.cancellation import OperationCancelled, raise_if_cancelled
from app.engine.models.document_model import WorkspaceExportRequest
from app.engine.services.export_registry import get_export, set_export
from app.engine.services.workspace_export_service import (
    build_pdf,
    build_zip,
    sanitize_filename,
)

LARGE_BATCH_DOCUMENTS = 100
LARGE_BATCH_PAGES = 100
LARGE_BATCH_BYTES = 500 * 1024 * 1024
LARGE_BATCH_COMPRESSION_EFFORT = 60
NORMAL_COMPRESSION_EFFORT = 100
_EVENT_FILE: Path | None = None


@dataclass(frozen=True)
class BatchProfile:
    large_batch: bool
    document_count: int
    page_count: int
    source_bytes: int
    compression_effort: int
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "large_batch": self.large_batch,
            "document_count": self.document_count,
            "page_count": self.page_count,
            "source_bytes": self.source_bytes,
            "compression_effort": self.compression_effort,
            "reasons": list(self.reasons),
        }


_EVENT_LOCK = threading.Lock()


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _parent_watch(interval: float = 2.0):
    """Devuelve una función barata que dice si el proceso padre sigue vivo
    (consulta el SO como mucho cada ``interval`` segundos)."""
    try:
        parent_pid = int(os.environ.get("BIGPDF_PARENT_PID", "0") or 0)
    except ValueError:
        parent_pid = 0
    state = {"checked": 0.0, "alive": True}

    def alive() -> bool:
        if not parent_pid or not state["alive"]:
            return state["alive"]
        now = time.monotonic()
        if now - state["checked"] >= interval:
            state["checked"] = now
            state["alive"] = _process_alive(parent_pid)
        return state["alive"]

    return alive


def _write_event(kind: str, **payload: Any) -> None:
    line = json.dumps({"kind": kind, **payload}, ensure_ascii=False) + "\n"
    if _EVENT_FILE is not None:
        try:
            # Lock: el latido de compresión emite progreso desde otro hilo y dos
            # escrituras simultáneas podían mezclar líneas del archivo de eventos.
            with _EVENT_LOCK, _EVENT_FILE.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
        except Exception:
            pass
    # Útil en desarrollo. En PyInstaller windowed sys.stdout puede ser None;
    # el archivo de eventos es el canal de IPC autoritativo.
    try:
        if sys.stdout is not None:
            sys.stdout.write(line)
            sys.stdout.flush()
    except Exception:
        pass


def _profile_request(req: WorkspaceExportRequest) -> BatchProfile:
    active_pages = [page for page in req.pages if not page.deleted]
    doc_ids = {page.source_document_id for page in active_pages}
    source_bytes = 0
    for doc_id in doc_ids:
        path = UPLOADS_DIR / f"{doc_id}.pdf"
        try:
            source_bytes += path.stat().st_size
        except OSError:
            pass

    reasons: list[str] = []
    if len(doc_ids) >= LARGE_BATCH_DOCUMENTS:
        reasons.append(f"{len(doc_ids)} archivos")
    if len(active_pages) >= LARGE_BATCH_PAGES:
        reasons.append(f"{len(active_pages)} páginas")
    if source_bytes >= LARGE_BATCH_BYTES:
        reasons.append(f"{source_bytes / 1024 / 1024:.0f} MB de origen")

    large = bool(reasons)
    return BatchProfile(
        large_batch=large,
        document_count=len(doc_ids),
        page_count=len(active_pages),
        source_bytes=source_bytes,
        compression_effort=LARGE_BATCH_COMPRESSION_EFFORT if large else NORMAL_COMPRESSION_EFFORT,
        reasons=tuple(reasons),
    )


def export_worker_entry(request_path: str | Path) -> int:
    ensure_storage_dirs()
    try:
        from app.engine.logging_config import setup_logging
        setup_logging("bigpdf-export.log")
    except Exception:
        pass
    request_path = Path(request_path)
    started = time.monotonic()
    created_paths: list[Path] = []

    try:
        wrapper = json.loads(request_path.read_text(encoding="utf-8"))
        global _EVENT_FILE
        _EVENT_FILE = Path(wrapper["event_file"])
        _EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _EVENT_FILE.write_text("", encoding="utf-8")
        raw_request = wrapper.get("request") or {}
        cancel_file = Path(wrapper["cancel_file"])
        req = WorkspaceExportRequest.model_validate(raw_request)
        profile = _profile_request(req)

        parent_alive = _parent_watch()

        def cancelled() -> bool:
            return cancel_file.exists() or not parent_alive()

        def emit_progress(event: dict) -> None:
            payload = dict(event)
            payload["elapsed_seconds"] = round(time.monotonic() - started, 1)
            payload.update(profile.as_dict())
            _write_event("progress", **payload)

        raise_if_cancelled(cancelled)
        if profile.large_batch:
            emit_progress({
                "pct": 1,
                "stage": "Modo lote grande",
                "message": (
                    "Modo lote grande activado automáticamente: "
                    + ", ".join(profile.reasons)
                    + f". Esfuerzo de compresión {profile.compression_effort}/100."
                ),
                "stage_index": 1,
                "stage_total": 9,
                "title": "Procesando lote grande",
            })
        else:
            emit_progress({
                "pct": 1,
                "stage": "Preparando exportación",
                "message": f"Preparando {profile.document_count} archivos y {profile.page_count} páginas…",
                "stage_index": 1,
                "stage_total": 9,
                "title": "Exportando PDF",
            })

        output_results: list[dict] = []
        total_outputs = len(req.outputs)
        for idx, output in enumerate(req.outputs):
            raise_if_cancelled(cancelled)
            effective_name = (output.name or "").strip() or f"Parte {idx + 1}"
            page_id_set = set(output.page_ids)
            order_map = {pid: i for i, pid in enumerate(output.page_ids)}
            group_pages = [
                page for page in req.pages
                if page.page_id in page_id_set and not page.deleted
            ]
            group_pages.sort(key=lambda page: order_map.get(page.page_id, 9999))
            # Antes las páginas desconocidas (o la parte entera vacía) se salteaban en
            # silencio: el PDF salía con menos páginas que las pedidas y "ok".
            known_ids = {page.page_id for page in group_pages}
            missing = [pid for pid in dict.fromkeys(output.page_ids) if pid not in known_ids]
            if missing or not group_pages:
                raise ValueError(
                    f'No se pudo armar "{effective_name}": faltan {len(missing) or "todas las"} '
                    "página(s). Volvé a intentar; si se repite, cerrá y abrí Big Integra."
                )

            output_base = idx / max(1, total_outputs)
            output_span = 1 / max(1, total_outputs)

            def output_progress(event: dict, *, _base=output_base, _span=output_span, _idx=idx) -> None:
                local_pct = max(0, min(100, int(event.get("pct", 0))))
                overall = int((_base + (local_pct / 100) * _span) * 95)
                enriched = dict(event)
                enriched["pct"] = max(1, min(95, overall))
                if total_outputs > 1:
                    enriched["message"] = (
                        f"Parte {_idx + 1} de {total_outputs} · "
                        + str(event.get("message") or "Procesando…")
                    )
                emit_progress(enriched)

            export_id, out_path, size, size_before = build_pdf(
                group_pages,
                output.compression,
                effective_name,
                progress_fn=output_progress,
                cancel_check=cancelled,
                large_batch=profile.large_batch,
                compression_effort=profile.compression_effort,
            )
            created_paths.append(out_path)
            raise_if_cancelled(cancelled)

            download_filename = sanitize_filename(effective_name)
            saved = size_before - size
            saved_pct = round(saved / size_before * 100, 1) if size_before else 0
            set_export(export_id, out_path, download_filename, "application/pdf")
            output_results.append({
                "name": output.name,
                "export_id": export_id,
                "filename": download_filename,
                "size_bytes": size,
                "size_before": size_before,
                "saved_bytes": max(0, saved),
                "saved_pct": max(0.0, saved_pct),
                "compression": output.compression,
            })

        raise_if_cancelled(cancelled)
        zip_export_id = None
        zip_filename = None
        if req.make_zip and output_results:
            emit_progress({
                "pct": 97,
                "stage": "Creando archivo ZIP",
                "message": "Creando el archivo ZIP final…",
                "stage_index": 9,
                "stage_total": 9,
            })
            zip_export_id = build_zip(
                [get_export(result["export_id"])["path"] for result in output_results],
                [result["filename"] for result in output_results],
            )
            entry = get_export(zip_export_id)
            if entry:
                created_paths.append(Path(entry["path"]))
            if len(output_results) == 1:
                zip_filename = f"{Path(output_results[0]['filename']).stem}.zip"
            else:
                zip_filename = entry.get("filename") if entry else "bigpdf_partes.zip"

        raise_if_cancelled(cancelled)
        emit_progress({
            "pct": 100,
            "stage": "Exportación finalizada",
            "message": "Exportación finalizada correctamente.",
            "stage_index": 9,
            "stage_total": 9,
        })
        _write_event(
            "result",
            result={
                "outputs": output_results,
                "zip_export_id": zip_export_id,
                "zip_filename": zip_filename,
                "processing_profile": profile.as_dict(),
            },
            elapsed_seconds=round(time.monotonic() - started, 1),
        )
        return 0
    except OperationCancelled:
        for path in created_paths:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        for pattern in ("tmp_*.pdf", "*.structural.tmp.pdf", "*.images_q*.tmp.pdf", "*.ghostscript.tmp.pdf"):
            for path in EXPORTS_DIR.glob(pattern):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
        _write_event(
            "cancelled",
            error="Procesamiento cancelado por el usuario.",
            elapsed_seconds=round(time.monotonic() - started, 1),
        )
        return 2
    except Exception as exc:
        _write_event(
            "error",
            error=str(exc) or exc.__class__.__name__,
            traceback=traceback.format_exc()[-1800:],
            elapsed_seconds=round(time.monotonic() - started, 1),
        )
        return 1
    finally:
        try:
            request_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Uso: python -m app.desktop.export_process REQUEST.json")
    raise SystemExit(export_worker_entry(sys.argv[1]))
