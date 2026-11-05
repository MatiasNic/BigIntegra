"""Supervisor del proceso independiente de exportación."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.config import STORAGE_DIR, WORKING_DIR, ensure_storage_dirs


class _ExportProcessSignals(QObject):
    progress = Signal(str)
    finished = Signal(str)


@dataclass
class _ProcessState:
    job_id: str
    process: subprocess.Popen | None
    signals: _ExportProcessSignals
    request_path: Path
    cancel_path: Path
    event_path: Path
    log_path: Path
    started_at: float
    cancel_requested: bool = False


class ExportProcessManager:
    """Lanza un único exportador externo y permite cancelarlo por job_id."""

    def __init__(self) -> None:
        self._active: dict[str, _ProcessState] = {}
        self._lock = threading.RLock()

    def _build_command(self, request_path: Path) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--bigpdf-export-worker", str(request_path)]
        main_path = Path(__file__).resolve().parents[2] / "main.py"
        return [sys.executable, str(main_path), "--bigpdf-export-worker", str(request_path)]

    def create_job(self, request: dict[str, Any]) -> tuple[str, _ExportProcessSignals]:
        """Prepara archivos y señales sin iniciar todavía el child.

        El bridge conecta progress/finished antes de llamar start_job(), evitando
        perder la señal de un PDF pequeño que termine en milisegundos.
        """
        ensure_storage_dirs()
        with self._lock:
            if any(state.process is None or state.process.poll() is None for state in self._active.values()):
                raise RuntimeError("Ya hay una exportación en curso. Esperá a que termine o cancelala.")

            job_id = uuid.uuid4().hex[:12]
            request_path = WORKING_DIR / f"export_request_{job_id}.json"
            cancel_path = WORKING_DIR / f"export_cancel_{job_id}.flag"
            event_path = WORKING_DIR / f"export_events_{job_id}.jsonl"
            log_path = WORKING_DIR / f"export_worker_{job_id}.log"
            request_path.write_text(
                json.dumps({
                    "job_id": job_id,
                    "request": request,
                    "cancel_file": str(cancel_path),
                    "event_file": str(event_path),
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            signals = _ExportProcessSignals()
            state = _ProcessState(
                job_id=job_id,
                process=None,
                signals=signals,
                request_path=request_path,
                cancel_path=cancel_path,
                event_path=event_path,
                log_path=log_path,
                started_at=0.0,
            )
            self._active[job_id] = state
            return job_id, signals

    def start_job(self, job_id: str) -> None:
        with self._lock:
            state = self._active.get(job_id)
            if not state:
                raise RuntimeError(f"Job de exportación {job_id!r} no encontrado.")
            if state.process is not None:
                raise RuntimeError(f"Job de exportación {job_id!r} ya iniciado.")

            env = os.environ.copy()
            env["BIGPDF_SESSION_DIR"] = str(STORAGE_DIR)
            env["BIGPDF_EXPORT_WORKER"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            # El hijo vigila al padre: si la app crashea o se mata desde el
            # Administrador de tareas, la exportación (y su Ghostscript) se corta
            # sola en vez de seguir consumiendo CPU huérfana.
            env["BIGPDF_PARENT_PID"] = str(os.getpid())
            # El hijo es el mismo .exe: que no vuelva a mostrar la imagen de inicio.
            env["PYINSTALLER_SUPPRESS_SPLASH_SCREEN"] = "1"
            log_stream = state.log_path.open("w", encoding="utf-8")
            kwargs: dict[str, Any] = {
                "cwd": str(Path(__file__).resolve().parents[2]),
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": log_stream,
            }
            if os.name == "nt":
                kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                )
            else:
                kwargs["start_new_session"] = True

            try:
                process = subprocess.Popen(self._build_command(state.request_path), **kwargs)
            except Exception:
                self._active.pop(job_id, None)
                for path in (state.request_path, state.cancel_path, state.event_path, state.log_path):
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                raise
            finally:
                log_stream.close()

            state.process = process
            state.started_at = time.monotonic()
            threading.Thread(target=self._monitor, args=(state,), daemon=True).start()

    def _monitor(self, state: _ProcessState) -> None:
        if state.process is None:
            return
        terminal: dict[str, Any] | None = None
        offset = 0  # bytes ya procesados del archivo de eventos

        def consume_events() -> None:
            nonlocal offset, terminal
            try:
                with state.event_path.open("rb") as stream:
                    stream.seek(offset)
                    chunk = stream.read()
            except (FileNotFoundError, OSError):
                return
            # Solo procesar líneas completas: si el hijo está escribiendo la línea
            # "result" justo ahora, se relee en la próxima pasada en vez de perderla
            # (antes una exportación exitosa podía reportarse como fallida).
            end = chunk.rfind(b"\n")
            if end < 0:
                return
            offset += end + 1
            for raw_line in chunk[:end].split(b"\n"):
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except Exception:
                    continue
                kind = event.get("kind")
                if kind == "progress":
                    payload = {"job_id": state.job_id, **{k: v for k, v in event.items() if k != "kind"}}
                    self._emit(state.signals.progress, payload)
                elif kind in {"result", "cancelled", "error"}:
                    terminal = event

        while state.process.poll() is None:
            consume_events()
            time.sleep(0.1)
        consume_events()
        return_code = state.process.wait()

        if state.cancel_requested and (terminal is None or terminal.get("kind") != "result"):
            terminal = {"kind": "cancelled", "error": "Procesamiento cancelado por el usuario."}

        if terminal and terminal.get("kind") == "result" and return_code == 0:
            payload = {"job_id": state.job_id, "ok": True, "result": terminal.get("result") or {}}
        elif terminal and terminal.get("kind") == "cancelled":
            self._cleanup_cancelled_temps()
            payload = {
                "job_id": state.job_id, "ok": False, "cancelled": True,
                "error": terminal.get("error") or "Procesamiento cancelado por el usuario.",
            }
        else:
            error = "El proceso independiente de exportación finalizó inesperadamente."
            traceback_tail = ""
            if terminal:
                error = terminal.get("error") or error
                traceback_tail = terminal.get("traceback") or ""
            else:
                try:
                    traceback_tail = state.log_path.read_text(encoding="utf-8", errors="replace")[-1800:]
                except Exception:
                    pass
            payload = {
                "job_id": state.job_id, "ok": False, "error": error,
                "traceback": traceback_tail[-1800:], "return_code": return_code,
            }
        self._emit(state.signals.finished, payload)

    @staticmethod
    def _emit(signal, payload: dict) -> None:
        # Al cerrar la app el QObject de señales puede estar destruido.
        try:
            signal.emit(json.dumps(payload, ensure_ascii=False))
        except RuntimeError:
            pass

    def has_running_jobs(self) -> bool:
        with self._lock:
            return any(
                state.process is not None and state.process.poll() is None
                for state in self._active.values()
            )

    @staticmethod
    def _cleanup_cancelled_temps() -> None:
        exports_dir = STORAGE_DIR / "exports"
        for pattern in ("tmp_*.pdf", "*.structural.tmp.pdf", "*.images_q*.tmp.pdf", "*.ghostscript.tmp.pdf"):
            for path in exports_dir.glob(pattern):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            state = self._active.get(job_id)
            if not state or state.process is None or state.process.poll() is not None:
                return False
            state.cancel_requested = True
            try:
                state.cancel_path.write_text("cancel", encoding="utf-8")
            except Exception:
                pass
            threading.Thread(target=self._force_stop_after_grace, args=(state,), daemon=True).start()
            return True

    def _force_stop_after_grace(self, state: _ProcessState) -> None:
        deadline = time.monotonic() + 1.8
        while time.monotonic() < deadline:
            if state.process.poll() is not None:
                return
            time.sleep(0.1)
        self._terminate_tree(state.process)

    @staticmethod
    def _terminate_tree(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass

    def cleanup_job(self, job_id: str) -> None:
        with self._lock:
            state = self._active.pop(job_id, None)
        if not state:
            return
        try:
            state.request_path.unlink(missing_ok=True)
        except Exception:
            pass
        for path in (state.cancel_path, state.event_path, state.log_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    def shutdown(self) -> None:
        with self._lock:
            states = list(self._active.values())
        for state in states:
            if state.process is not None and state.process.poll() is None:
                state.cancel_requested = True
                try:
                    state.cancel_path.write_text("cancel", encoding="utf-8")
                except Exception:
                    pass
                self._terminate_tree(state.process)
