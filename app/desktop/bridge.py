"""Bridge desktop real para Big Integra.

Expone funciones Python a la interfaz HTML/JS local mediante QWebChannel.
No usa FastAPI, Uvicorn, puertos, HTTP local ni 127.0.0.1.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QApplication, QFileDialog

from app.config import (
    MAX_FILE_SIZE,
    MAX_FILES_PER_OPERATION,
    MAX_PAGES_PER_PDF,
    UPLOADS_DIR,
    ensure_storage_dirs,
)
from app.desktop.export_process_manager import ExportProcessManager
from app.desktop.workers import JobManager
from app.engine.cancellation import OperationCancelled
from app.engine.concurrency import ServerBusyError, pdf_operation_slot
from app.engine.integra.analyzer import catalog_summary
from app.engine.integra.service import analyze_workspace
from app.engine.models.document_model import CompressRequest, WorkspaceExportRequest
from app.engine.services.cleanup_service import cleanup_session_temp_dir
from app.engine.services.compress_service import compress_pdf
from app.engine.services.conversion_service import ensure_pdf_from_temp
from app.engine.services.export_registry import get_export, set_export
from app.engine.services.file_service import check_pdf_limits
from app.engine.services.prefs_service import (
    get_choose_location,
    get_last_dir,
    set_choose_location,
    set_last_dir,
)
from app.engine.services.workspace_export_service import sanitize_download_filename

logger = logging.getLogger("bigpdf")

# Rutas internas (carpeta de sesión con nombres UUID) que no deben verse en un mensaje.
_INTERNAL_PATH_RE = re.compile(
    r"['\"]?[^'\"\s]*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}[^'\"\s]*['\"]?",
    re.IGNORECASE,
)

# Valida formato UUID (8-4-4-4-12 hex). Todos los IDs internos se generan con
# uuid.uuid4(), así que el formato es suficiente para el caso desktop (usuario local).
# Si en el futuro se aceptan IDs de origen externo, endurecer para validar versión/variante.
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)
_MB = 1024 * 1024


def _error_entry(filename: str, message: str) -> dict[str, Any]:
    return {"filename": filename, "status": "error", "error": message}


class BigPdfBridge(QObject):
    """API local de Big Integra para QWebChannel.

    Todos los métodos públicos reciben/devuelven JSON string para evitar problemas
    de conversión entre JS y Python.

    Importación y compresión individual usan JobManager. La exportación del
    workspace usa un proceso independiente cancelable. Todos devuelven job_id
    inmediatamente y emiten jobFinished al terminar.
    """

    # Señales publicadas a JS por QWebChannel — el frontend conecta a ellas.
    jobProgress = Signal(str)   # JSON: {job_id, pct, message, title}
    jobFinished = Signal(str)   # JSON: {job_id, ok, result?, error?}
    filesDropped = Signal()     # emitida cuando Qt intercepta OS drop de archivos locales
    nativeDragStateChanged = Signal(bool)  # True = drag activo sobre ventana, False = terminó

    def __init__(self, parent=None, *, get_downloads_dir=None, get_unique_download_path=None) -> None:
        super().__init__(parent)
        self._parent = parent
        self._last_selected_paths: list[str] = []
        self._skipped_in_folders = 0  # archivos no admitidos en carpetas soltadas
        self._paths_lock = threading.Lock()
        self._get_downloads_dir = get_downloads_dir
        self._get_unique_download_path = get_unique_download_path
        self._job_mgr = JobManager()
        self._export_process_mgr = ExportProcessManager()

    # ─── helpers ──────────────────────────────────────────────────────────────

    def set_last_selected_paths(self, paths: list[str], skipped_in_folders: int = 0) -> None:
        # Guarda rutas absolutas recibidas desde QFileDialog o drag nativo de Windows.
        # No se guardan basenames: el importador siempre necesita ruta completa.
        cleaned: list[str] = []
        for p in paths or []:
            if not p:
                continue
            try:
                cleaned.append(str(Path(p).expanduser().resolve()))
            except Exception:
                cleaned.append(str(p))
        with self._paths_lock:
            self._last_selected_paths = cleaned
            self._skipped_in_folders = max(0, int(skipped_in_folders or 0))

    def _json_ok(self, data: dict[str, Any] | None = None) -> str:
        payload = {"ok": True}
        if data:
            payload.update(data)
        return json.dumps(payload, ensure_ascii=False)

    def _json_error(self, message: str, **extra: Any) -> str:
        payload = {"ok": False, "detail": str(message or "Error desconocido.")}
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    def _parse(self, payload: str | None) -> dict[str, Any]:
        if not payload:
            return {}
        try:
            data = json.loads(payload)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _download_dir(self) -> Path:
        if self._get_downloads_dir:
            return Path(self._get_downloads_dir())
        fallback = Path.home() / "Downloads"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    def _unique_download_path(self, filename: str) -> Path:
        if self._get_unique_download_path:
            return Path(self._get_unique_download_path(filename))
        downloads = self._download_dir()
        safe = Path(filename or "bigpdf_export.pdf").name
        stem = Path(safe).stem or "bigpdf_export"
        ext = Path(safe).suffix or ".pdf"
        candidate = downloads / f"{stem}{ext}"
        counter = 2
        while candidate.exists():
            candidate = downloads / f"{stem} ({counter}){ext}"
            counter += 1
        return candidate

    def _valid_doc_id(self, doc_id: str) -> bool:
        return bool(doc_id and _UUID_RE.match(doc_id))

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Mensaje para el usuario: en español y sin rutas internas ni códigos."""
        detail = getattr(exc, "detail", None)
        if detail:
            return str(detail)
        if isinstance(exc, OSError):
            errno_ = getattr(exc, "errno", None)
            winerror = getattr(exc, "winerror", None)
            if errno_ == 28 or winerror == 112:
                return "No hay espacio suficiente en el disco."
            if isinstance(exc, PermissionError) or errno_ == 13 or winerror in (5, 32, 33):
                return (
                    "No se pudo guardar: el archivo está abierto en otro programa "
                    "o no hay permiso para escribir en esa carpeta."
                )
            if winerror in (123, 87) or errno_ == 22:
                return "El nombre o la ruta del archivo no son válidos."
        text = str(exc) or exc.__class__.__name__
        if text.startswith(("Failed to open", "cannot open", "code=")) or "format error" in text:
            return "No se pudo abrir el archivo: está dañado o no es un PDF válido."
        # Nunca mostrar rutas de la carpeta temporal de la sesión.
        return _INTERNAL_PATH_RE.sub("(archivo)", text)

    @Slot(str)
    def _relay_job_finished(self, raw: str) -> None:
        """Reenvía finished a JS y libera signals de _active.

        Emite primero (JS recibe resultado), limpia después.
        Orden importa: cleanup_job() baja refcount de signals; si limpiáramos
        antes de emitir, signals podría quedar sin refs mientras Qt aún despacha.
        """
        parsed = {}
        job_id = ""
        try:
            parsed = json.loads(raw)
            job_id = str(parsed.get("job_id") or "")
            ok = parsed.get("ok", False)
            logger.info(f"jobFinished relay: job={job_id or '?'} ok={ok}")
            if not ok:
                logger.warning(f"jobFinished FAILED: job={job_id or '?'} error={parsed.get('error','')!r}")
        except Exception:
            pass
        self.jobFinished.emit(raw)
        try:
            self._job_mgr.cleanup_job(job_id)
        except Exception:
            pass

    @Slot(str)
    def _relay_job_progress(self, raw: str) -> None:
        """Reenvía progreso de un worker a jobProgress (que llega a JS)."""
        self.jobProgress.emit(raw)

    @Slot(str)
    def _relay_export_finished(self, raw: str) -> None:
        """Reenvía el resultado del proceso externo y libera su supervisor."""
        job_id = ""
        try:
            job_id = str((json.loads(raw) or {}).get("job_id") or "")
        except Exception:
            pass
        self.jobFinished.emit(raw)
        try:
            self._export_process_mgr.cleanup_job(job_id)
        except Exception:
            pass

    def has_active_jobs(self) -> bool:
        """True si hay una importación, compresión, guardado o exportación en curso."""
        try:
            return self._job_mgr.has_running_jobs() or self._export_process_mgr.has_running_jobs()
        except Exception:
            return False

    def shutdown_background_jobs(self) -> None:
        """Cancela procesos y workers antes de limpiar la carpeta de sesión.

        Antes solo se cancelaba la exportación: una compresión o conversión de
        Office seguía corriendo sobre archivos que se estaban borrando.
        """
        try:
            self._export_process_mgr.shutdown()
        except Exception as exc:
            logger.debug("shutdown export: %s", exc)
        try:
            self._job_mgr.shutdown(wait_ms=3000)
        except Exception as exc:
            logger.debug("shutdown jobs: %s", exc)

    def _wait_notice(self, signals, job_id: str):
        """Callback para pdf_operation_slot: avisa a la UI que la tarea quedó en cola."""
        def _notice() -> None:
            signals.progress.emit(json.dumps({
                "job_id": job_id,
                "pct": 1,
                "message": "Esperando que termine otra tarea de Big Integra…",
            }, ensure_ascii=False))
        return _notice

    # ─── app ──────────────────────────────────────────────────────────────────

    @Slot(str, result=str)
    def closeApp(self, payload: str = "") -> str:  # noqa: N802
        app = QApplication.instance()
        if app:
            app.quit()
        return self._json_ok()

    # ─── selección/importación ────────────────────────────────────────────────

    @Slot(str, result=str)
    def importSelectedFiles(self, payload: str) -> str:  # noqa: N802
        """Importa archivos elegidos con QFileDialog interceptado por chooseFiles().

        Devuelve {job_id, async:true} inmediatamente; emite jobFinished cuando termina.
        Evita subir contenido por HTTP o codificar PDFs grandes en base64.
        """
        ensure_storage_dirs()
        data = self._parse(payload)
        names = [str(n) for n in data.get("names", [])]

        # BUG-2: lectura + limpieza atómica bajo lock — evita race con set_last_selected_paths.
        with self._paths_lock:
            raw_paths = list(self._last_selected_paths)
            self._last_selected_paths = []
            skipped_in_folders = self._skipped_in_folders
            self._skipped_in_folders = 0
        paths = [Path(p) for p in raw_paths if p]

        # Diálogo cerrado o sin rutas válidas.
        if not paths:
            return self._json_ok({"used_selected_paths": False, "documents": []})

        # Si Chromium dispara un change viejo o no coincide con los archivos del input,
        # evitamos importar algo incorrecto. Se permite comparar solo por basename.
        if names:
            path_names = [p.name for p in paths]
            if sorted(path_names) != sorted(names):
                return self._json_ok({"used_selected_paths": False, "documents": []})

        # create_job() primero para que _work capture job_id + signals y pueda emitir progress.
        job_id, signals = self._job_mgr.create_job()

        def _progress(idx: int, total: int) -> None:
            pct = max(5, int((idx + 1) / total * 100)) if total > 0 else 50
            signals.progress.emit(json.dumps({
                "job_id": job_id,
                "pct": pct,
                "message": f"Procesando archivo {idx + 1} de {total}…",
            }, ensure_ascii=False))

        def _work():
            result = self._import_paths(
                paths, progress_fn=_progress,
                on_wait=self._wait_notice(signals, job_id),
                cancel_check=self._job_mgr.cancel_check_for(job_id),
            )
            result["used_selected_paths"] = True
            if skipped_in_folders:
                result["notice"] = (
                    f"{skipped_in_folders} archivo(s) de la carpeta no se importaron "
                    "por tener un formato no admitido."
                )
            return result

        signals.finished.connect(self._relay_job_finished)
        signals.progress.connect(self._relay_job_progress)
        self._job_mgr.start_job(job_id, _work)
        return self._json_ok({"job_id": job_id, "async": True})

    @Slot(str, result=str)
    def importFileData(self, payload: str) -> str:  # noqa: N802
        """Fallback para drag & drop: recibe archivos codificados en base64 desde JS.

        Se usa solo cuando no hay rutas locales disponibles (drag & drop sin QFileDialog).
        Para selección normal, importSelectedFiles() usa rutas directas de Windows.

        json.loads() del payload es inevitable en hilo Qt (mecanismo de @Slot).
        El JS limita el payload a 70 MB binarios totales para que json.loads tarde < 300ms.
        base64.b64decode + write_bytes se ejecutan en worker (fondo) para no congelar.
        """
        # Guardia de tamaño antes de json.loads para evitar bloqueo catastrófico.
        # El límite en JS (70 MB binarios ≈ 93 MB JSON) debería prevenirlo,
        # pero si el JS está en caché vieja o hay múltiples archivos, este freno extra protege.
        if payload and len(payload) > 120 * 1024 * 1024:
            return self._json_error(
                "Archivos demasiado grandes para arrastrar. Usá el botón Seleccionar archivos."
            )

        ensure_storage_dirs()
        data = self._parse(payload)
        files = data.get("files") or []
        if not isinstance(files, list):
            return self._json_error("No se recibieron archivos.")
        if len(files) > MAX_FILES_PER_OPERATION:
            return self._json_error(f"Máximo {MAX_FILES_PER_OPERATION} archivos por operación.")
        if not files:
            return self._json_ok({"used_selected_paths": False, "documents": []})

        # base64.b64decode + write_bytes van al worker: evitan congelar el hilo Qt
        # con archivos de 25 MB (decode + IO ≈ 200 ms por archivo si corrieran aquí).
        # 'files' es una lista Python normal; pasar por closure es thread-safe porque
        # el hilo principal no la toca después de hacer submit().
        files_snapshot = list(files)

        job_id, signals = self._job_mgr.create_job()

        def _progress(idx: int, total: int) -> None:
            pct = max(5, int((idx + 1) / total * 100)) if total > 0 else 50
            signals.progress.emit(json.dumps({
                "job_id": job_id,
                "pct": pct,
                "message": f"Preparando archivo {idx + 1} de {total}…",
            }, ensure_ascii=False))

        def _work():
            ensure_storage_dirs()
            staged: list[tuple[Path, str, int]] = []
            pre_errors: list[dict[str, Any]] = []
            for f in files_snapshot:
                if not isinstance(f, dict):
                    continue
                filename = str(f.get("filename") or "archivo")
                b64 = str(f.get("data_base64") or "")
                ext = Path(filename).suffix.lower() or ".bin"
                try:
                    raw = base64.b64decode(b64.encode("ascii"), validate=False)
                    if len(raw) > MAX_FILE_SIZE:
                        raise ValueError(f"{filename} supera el límite de {MAX_FILE_SIZE // _MB} MB.")
                    ensure_storage_dirs()
                    tmp_path = UPLOADS_DIR / f"_desktop_{os.urandom(8).hex()}{ext}"
                    tmp_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path.write_bytes(raw)
                    staged.append((tmp_path, filename, len(raw)))
                except Exception as exc:
                    pre_errors.append(_error_entry(filename, self._safe_error(exc)))
            result = self._process_staged(
                staged, pre_errors, progress_fn=_progress,
                on_wait=self._wait_notice(signals, job_id),
                cancel_check=self._job_mgr.cancel_check_for(job_id),
            )
            result["used_selected_paths"] = False
            return result

        signals.finished.connect(self._relay_job_finished)
        signals.progress.connect(self._relay_job_progress)
        self._job_mgr.start_job(job_id, _work)
        return self._json_ok({"job_id": job_id, "async": True})

    def _import_paths(
        self, paths: list[Path], progress_fn=None, on_wait=None, cancel_check=None,
    ) -> dict[str, Any]:
        ensure_storage_dirs()
        if len(paths) > MAX_FILES_PER_OPERATION:
            message = f"Máximo {MAX_FILES_PER_OPERATION} archivos por operación."
            return {"documents": [_error_entry("", message)]}

        staged: list[tuple[Path, str, int]] = []
        results: list[dict[str, Any]] = []
        for src in paths:
            try:
                src = Path(src).expanduser()
                display_name = src.name or str(src)
                if not src.exists() or not src.is_file():
                    raise ValueError("Archivo no encontrado o ruta inaccesible.")
                size = src.stat().st_size
                if size > MAX_FILE_SIZE:
                    raise ValueError(f"{display_name} supera el límite de {MAX_FILE_SIZE // _MB} MB.")
                if size == 0:
                    raise ValueError("El archivo está vacío.")
                ensure_storage_dirs()
                ext = src.suffix.lower() or ".bin"
                tmp_path = UPLOADS_DIR / f"_desktop_{os.urandom(8).hex()}{ext}"
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(tmp_path))
                staged.append((tmp_path, display_name, size))
            except Exception as exc:
                results.append(_error_entry(src.name or str(src), self._safe_error(exc)))
        return self._process_staged(
            staged, results, progress_fn=progress_fn, on_wait=on_wait, cancel_check=cancel_check,
        )

    def _process_staged(
        self,
        staged: list[tuple[Path, str, int]],
        results: list[dict[str, Any]],
        progress_fn=None,
        on_wait=None,
        cancel_check=None,
    ) -> dict[str, Any]:
        """Procesa archivos temporales ya listos (PDF/imagen/office).

        progress_fn(idx_0based, total): llamado antes de procesar cada archivo.
        Se usa para emitir signals.progress desde el worker sin acoplar
        _process_staged a señales Qt específicas.
        """
        ensure_storage_dirs()
        total = len(staged)
        try:
            with pdf_operation_slot(on_wait=on_wait, cancel_check=cancel_check):
                for i, (tmp_path, filename, size) in enumerate(staged):
                    if cancel_check and cancel_check():
                        raise OperationCancelled("Importación cancelada.")
                    if progress_fn and total > 0:
                        try:
                            progress_fn(i, total)
                        except Exception:
                            pass
                    pdf_path = None
                    try:
                        info: dict[str, Any] = {}
                        doc_id, pdf_path, original_type = ensure_pdf_from_temp(
                            tmp_path, filename, size, info=info,
                        )
                        pages = check_pdf_limits(pdf_path, MAX_PAGES_PER_PDF)
                        if info.get("repaired"):
                            info["warning"] = (
                                "el archivo estaba dañado y se recuperó lo posible: "
                                "revisá que estén todas las páginas."
                            )
                        results.append({
                            "document_id": doc_id,
                            "filename": filename,
                            "original_filename": filename,
                            "original_type": original_type,
                            "converted_pdf_id": doc_id,
                            "pages": pages,
                            "size_bytes": size,
                            "status": "ready",
                            "file_url": pdf_path.resolve().as_uri(),
                            **({"warning": info["warning"]} if info.get("warning") else {}),
                        })
                    except Exception as exc:
                        try:
                            tmp_path.unlink(missing_ok=True)
                            # Si el PDF ya se movió a uploads y después se rechazó
                            # (más de MAX_PAGES, contraseña), no dejarlo ocupando disco.
                            if pdf_path is not None:
                                Path(pdf_path).unlink(missing_ok=True)
                        except Exception:
                            pass
                        results.append(_error_entry(filename, self._safe_error(exc)))
        except (ServerBusyError, OperationCancelled) as exc:
            for tmp_path, _, _ in staged:
                tmp_path.unlink(missing_ok=True)
            # Lo ya importado no llega a la pantalla: no dejarlo ocupando disco.
            for entry in results:
                doc_id = entry.get("document_id")
                if doc_id:
                    (UPLOADS_DIR / f"{doc_id}.pdf").unlink(missing_ok=True)
            return {"documents": [{"filename": "", "status": "error", "error": self._safe_error(exc)}]}
        return {"documents": results}

    # ─── documentos/PDF.js ─────────────────────────────────────────────────────

    @Slot(str, result=str)
    def getFileUrl(self, payload: str) -> str:  # noqa: N802
        ensure_storage_dirs()
        data = self._parse(payload)
        doc_id = str(data.get("document_id") or data.get("id") or "")
        if not self._valid_doc_id(doc_id):
            return self._json_error("ID de documento inválido.")
        try:
            path = (UPLOADS_DIR / f"{doc_id}.pdf").resolve()
            if not path.exists():
                return self._json_error("Documento no encontrado.")
            return self._json_ok({"url": path.as_uri()})
        except Exception as exc:
            return self._json_error(self._safe_error(exc))

    @Slot(str, result=str)
    def getDocument(self, payload: str) -> str:  # noqa: N802
        """Devuelve metadatos de un documento. check_pdf_limits (fitz.open) corre en worker.

        Para PDFs de 1000+ páginas, fitz.open puede tardar 300-500ms en hilo Qt.
        Esta versión devuelve {job_id, async:true} para no congelar la UI.
        """
        ensure_storage_dirs()
        data = self._parse(payload)
        doc_id = str(data.get("document_id") or data.get("id") or "")
        if not self._valid_doc_id(doc_id):
            return self._json_error("ID de documento inválido.")
        path = (UPLOADS_DIR / f"{doc_id}.pdf").resolve()
        if not path.exists():
            return self._json_error("Documento no encontrado.")

        def _work():
            pages = check_pdf_limits(path, MAX_PAGES_PER_PDF)
            return {"document_id": doc_id, "pages": pages, "filename": path.name, "file_url": path.as_uri()}

        try:
            job_id, _signals = self._job_mgr.submit(_work, on_finished=self._relay_job_finished)
            return self._json_ok({"job_id": job_id, "async": True})
        except Exception as exc:
            return self._json_error(self._safe_error(exc))

    # ─── preferencias/descargas ────────────────────────────────────────────────

    @Slot(str, result=str)
    def getDownloadLocationPref(self, payload: str = "") -> str:  # noqa: N802
        return self._json_ok({"choose_location": get_choose_location()})

    @Slot(str, result=str)
    def setDownloadLocationPref(self, payload: str) -> str:  # noqa: N802
        data = self._parse(payload)
        set_choose_location(bool(data.get("choose_location", False)))
        return self._json_ok()

    @Slot(str, result=str)
    def saveExport(self, payload: str) -> str:  # noqa: N802
        """Guarda archivo exportado en destino elegido por el usuario.

        QFileDialog se abre aquí (hilo Qt). shutil.copy2 corre en background
        para no congelar la UI durante copias de archivos grandes (100 MB+).
        Devuelve {cancelled:true} en sync si el usuario cancela el diálogo,
        o {job_id, async:true} para esperar el fin de la copia.
        """
        ensure_storage_dirs()
        data = self._parse(payload)
        export_id = str(data.get("export_id") or "")
        raw_filename = str(data.get("filename") or "bigpdf_export.pdf")
        try:
            entry = get_export(export_id)
            if not entry or not entry.get("path") or not Path(entry["path"]).exists():
                return self._json_error("Archivo exportado no encontrado.")
            src = Path(entry["path"])
            # El nombre llega como lo escribió el usuario: se limpia de caracteres
            # que Windows no acepta ("/" antes recortaba el nombre) y se asegura la
            # extensión real del archivo (".pdf" o ".zip").
            filename = sanitize_download_filename(raw_filename, src.suffix or ".pdf")

            # QFileDialog DEBE ejecutarse en hilo Qt (nunca dentro del worker).
            manual = get_choose_location()
            reserved = False
            if manual:
                initial = str(get_last_dir(self._download_dir()) / filename)
                suffix = src.suffix.lower()
                if suffix == ".pdf":
                    file_filter = "Documento PDF (*.pdf)"
                elif suffix == ".zip":
                    file_filter = "Archivo ZIP (*.zip)"
                else:
                    file_filter = "Todos los archivos (*.*)"
                target, _ = QFileDialog.getSaveFileName(
                    self._parent, "Guardar archivo", initial, file_filter
                )
                if not target:
                    return self._json_ok({"cancelled": True})
                dst = Path(target)
                # "Informe v1.2" no tiene extensión ".2": se agrega la real.
                if src.suffix and dst.suffix.lower() != src.suffix.lower():
                    dst = dst.with_name(dst.name + src.suffix)
                set_last_dir(dst.parent)
            else:
                dst = self._reserve_download_path(filename)
                reserved = True

            dst.parent.mkdir(parents=True, exist_ok=True)
            if manual:
                message = f"Archivo guardado correctamente: {dst.name}"
            else:
                message = f"Archivo guardado en Descargas: {dst.name}"
            final_name = dst.name
            src_str, dst_str = str(src), str(dst)

            def _copy():
                # Copiar a ".part" y renombrar: el archivo nunca aparece a medio
                # copiar en Descargas, y si falla no queda un PDF roto.
                part = Path(dst_str + ".part")
                try:
                    shutil.copy2(src_str, str(part))
                    os.replace(str(part), dst_str)
                except BaseException:
                    part.unlink(missing_ok=True)
                    if reserved:
                        Path(dst_str).unlink(missing_ok=True)
                    raise
                return {"filename": final_name, "path": dst_str, "message": message, "cancelled": False}

            job_id, _signals = self._job_mgr.submit(_copy, on_finished=self._relay_job_finished)
            return self._json_ok({"job_id": job_id, "async": True})
        except Exception as exc:
            return self._json_error(self._safe_error(exc))

    def _reserve_download_path(self, filename: str) -> Path:
        """Elige un nombre libre en Descargas y lo reserva creándolo vacío.

        Sin la reserva, dos guardados rápidos con el mismo nombre podían elegir la
        misma ruta (la copia corre en segundo plano) y el segundo pisaba al primero.
        """
        candidate = self._unique_download_path(filename)
        stem, ext = candidate.stem, candidate.suffix
        base_stem = re.sub(r" \(\d+\)$", "", stem)
        counter = 2
        for _ in range(1000):
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                with open(candidate, "xb"):
                    pass
                return candidate
            except FileExistsError:
                candidate = candidate.with_name(f"{base_stem} ({counter}){ext}")
                counter += 1
        return candidate

    # ─── exportación/compresión ────────────────────────────────────────────────

    @Slot(str, result=str)
    def exportWorkspace(self, payload: str) -> str:  # noqa: N802
        """Exporta el workspace en un proceso independiente cancelable.

        PyMuPDF y Ghostscript dejan de compartir memoria/CPU con QtWebEngine. El
        slot devuelve inmediatamente un job_id y el child informa etapas reales.
        """
        ensure_storage_dirs()
        data = self._parse(payload)
        try:
            req = WorkspaceExportRequest.model_validate(data)
            active_pages = [page for page in req.pages if not page.deleted]
            doc_ids = {page.source_document_id for page in active_pages}
            missing_ids = [
                doc_id for doc_id in doc_ids
                if not (UPLOADS_DIR / f"{doc_id}.pdf").exists()
            ]
            if missing_ids:
                return self._json_error(
                    f"Archivos fuente no encontrados: {missing_ids[:3]}. Recargá los archivos."
                )

            job_id, signals = self._export_process_mgr.create_job(data)
            signals.finished.connect(self._relay_export_finished)
            signals.progress.connect(self._relay_job_progress)
            self._export_process_mgr.start_job(job_id)
            logger.info(
                "exportWorkspace external process: job=%s outputs=%s active_pages=%s",
                job_id, len(req.outputs), len(active_pages),
            )
            return self._json_ok({"job_id": job_id, "async": True, "cancellable": True})
        except Exception as exc:
            logger.exception("exportWorkspace process launch error: %s", exc)
            return self._json_error(self._safe_error(exc))

    @Slot(str, result=str)
    def cancelJob(self, payload: str) -> str:  # noqa: N802
        """Cancela una exportación (proceso y su árbol de hijos) o una compresión/importación."""
        data = self._parse(payload)
        job_id = str(data.get("job_id") or "")
        if not job_id:
            return self._json_error("Job inválido.")
        cancelled = self._export_process_mgr.cancel(job_id) or self._job_mgr.cancel(job_id)
        if not cancelled:
            return self._json_ok({"cancel_requested": False, "message": "El proceso ya terminó."})
        return self._json_ok({"cancel_requested": True, "message": "Cancelando procesamiento…"})

    @Slot(str, result=str)
    def compress(self, payload: str) -> str:  # noqa: N802
        """Comprime un PDF en background (GS puede tardar hasta 2 minutos en archivos grandes).

        Devuelve {job_id, async:true} inmediatamente; emite jobFinished al terminar.
        """
        ensure_storage_dirs()
        data = self._parse(payload)
        try:
            req = CompressRequest.model_validate(data)
            if not self._valid_doc_id(req.document_id):
                return self._json_error("ID de documento inválido.")
            src = UPLOADS_DIR / f"{req.document_id}.pdf"
            if not src.exists():
                return self._json_error("Documento no encontrado.")
            level = req.level

            job_id, signals = self._job_mgr.create_job()
            cancel_check = self._job_mgr.cancel_check_for(job_id)

            def _progress(event: dict) -> None:
                signals.progress.emit(json.dumps({
                    "job_id": job_id,
                    "pct": event.get("pct", 0),
                    "message": event.get("message") or "Comprimiendo…",
                    "stage": event.get("stage") or "",
                    "title": "Comprimiendo PDF",
                }, ensure_ascii=False))

            def _work():
                notice = self._wait_notice(signals, job_id)
                with pdf_operation_slot(on_wait=notice, cancel_check=cancel_check):
                    export_id, out_path, orig, compressed = compress_pdf(
                        src, level, progress_fn=_progress, cancel_check=cancel_check,
                    )
                    set_export(export_id, out_path, out_path.name, "application/pdf")
                    return {
                        "export_id": export_id,
                        "filename": out_path.name,
                        "original_size_bytes": orig,
                        "compressed_size_bytes": compressed,
                        "reduction_pct": round((1 - compressed / orig) * 100, 1) if orig else 0,
                    }

            signals.finished.connect(self._relay_job_finished)
            signals.progress.connect(self._relay_job_progress)
            self._job_mgr.start_job(job_id, _work)
            return self._json_ok({"job_id": job_id, "async": True, "cancellable": True})
        except Exception as exc:
            return self._json_error(self._safe_error(exc))

    # ─── Integra: identificar, ordenar y analizar el legajo ──────────────────

    @Slot(str, result=str)
    def integraCatalog(self, payload: str = "") -> str:  # noqa: N802
        """Prestaciones, condiciones y tipos de documento para armar el panel Integra."""
        try:
            return self._json_ok(catalog_summary())
        except Exception as exc:
            return self._json_error(self._safe_error(exc))

    @Slot(str, result=str)
    def integraAnalyze(self, payload: str) -> str:  # noqa: N802
        """Lee (texto u OCR), clasifica y analiza las hojas del workspace en background.

        La primera vez puede tardar (OCR ~2 s por hoja escaneada); después el texto queda
        en memoria y volver a analizar (otra etiqueta, otras prestaciones) es inmediato.
        """
        ensure_storage_dirs()
        data = self._parse(payload)
        try:
            pages = data.get("pages") or []
            if not pages:
                return self._json_error("No hay hojas para analizar.")
            for page in pages:
                if not self._valid_doc_id(str(page.get("source_document_id", ""))):
                    return self._json_error("ID de documento inválido.")
                if not (UPLOADS_DIR / f"{page['source_document_id']}.pdf").exists():
                    return self._json_error("Archivos fuente no encontrados. Volvé a importarlos.")

            job_id, signals = self._job_mgr.create_job()
            cancel_check = self._job_mgr.cancel_check_for(job_id)

            def _progress(done: int, total: int, message: str) -> None:
                signals.progress.emit(json.dumps({
                    "job_id": job_id,
                    "pct": round(done * 100 / total) if total else 100,
                    "message": message,
                    "title": "Analizando documentación",
                }, ensure_ascii=False))

            def _work():
                notice = self._wait_notice(signals, job_id)
                with pdf_operation_slot(on_wait=notice, cancel_check=cancel_check):
                    return analyze_workspace(
                        data, UPLOADS_DIR, progress=_progress, cancel_check=cancel_check,
                    )

            signals.finished.connect(self._relay_job_finished)
            signals.progress.connect(self._relay_job_progress)
            self._job_mgr.start_job(job_id, _work)
            return self._json_ok({"job_id": job_id, "async": True, "cancellable": True})
        except Exception as exc:
            return self._json_error(self._safe_error(exc))

    # ─── limpieza ──────────────────────────────────────────────────────────────

    @Slot(str, result=str)
    def cleanup(self, payload: str = "") -> str:  # noqa: N802
        """No-op en desktop mientras la app está abierta.

        Antes este método podía borrar PDFs fuente de uploads cuando el frontend
        disparaba pagehide. En QWebEngine/PyInstaller ONEFILE pagehide puede ocurrir
        sin que el usuario cierre BIG PDF, por lo que la UI quedaba con páginas en
        memoria pero exportWorkspace fallaba con "Archivos fuente no encontrados".

        La limpieza segura de la sesión completa se hace únicamente al cerrar la
        ventana: MainWindow.closeEvent -> cleanup_session_temp_dir().
        """
        return self._json_ok({
            "removed": 0,
            "message": "Limpieza parcial deshabilitada en desktop; la sesión se limpia al cerrar.",
        })

    @Slot(str, result=str)
    def cleanupSessionTempDir(self, payload: str = "") -> str:  # noqa: N802
        try:
            return self._json_ok(cleanup_session_temp_dir())
        except Exception as exc:
            return self._json_error(self._safe_error(exc))
