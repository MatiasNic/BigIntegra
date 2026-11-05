"""Background job runner para BIGPDF Desktop.

Evita congelar el event loop de Qt cuando se procesan PDFs pesados.
QThreadPool ejecuta los workers; señales Qt propagan resultados al hilo
principal → QWebChannel → JavaScript automáticamente.
"""
from __future__ import annotations

import json
import threading
import traceback
import uuid
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _JobSignals(QObject):
    """Carrier de señales por job. Debe ser QObject para emitir señales thread-safe."""

    progress = Signal(str)  # JSON: {job_id, pct, message, title}
    finished = Signal(str)  # JSON: {job_id, ok, result?, error?}


class _JobWorker(QRunnable):
    def __init__(
        self,
        job_id: str,
        fn: Callable,
        args: tuple,
        kwargs: dict,
        signals: _JobSignals,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.job_id = job_id
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = signals

    def _emit_finished(self, payload: dict) -> None:
        # Al cerrar la app, Qt puede destruir el QObject de señales mientras el
        # worker termina: emitir sobre un objeto borrado lanza RuntimeError.
        try:
            self.signals.finished.emit(json.dumps(payload, ensure_ascii=False))
        except RuntimeError:
            pass

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
            self._emit_finished({"job_id": self.job_id, "ok": True, "result": result})
        except Exception as exc:
            from app.engine.cancellation import OperationCancelled
            payload = {
                "job_id": self.job_id,
                "ok": False,
                "error": str(exc) or exc.__class__.__name__,
            }
            if isinstance(exc, OperationCancelled):
                payload["cancelled"] = True
            else:
                payload["traceback"] = traceback.format_exc()[-800:]
            self._emit_finished(payload)
        except BaseException:
            # MemoryError y similares no son Exception — siempre notificar JS
            # para evitar que el job quede colgado indefinidamente.
            self._emit_finished(
                {"job_id": self.job_id, "ok": False, "error": "Error crítico al procesar el archivo."}
            )


class JobManager:
    """Envía callables pesados a QThreadPool.

    Uso:
        job_id, signals = mgr.submit(fn, args=(a, b))
        signals.finished.connect(mi_callback)

    Las señales se emiten desde el hilo worker → Qt las encola al hilo
    principal → QWebChannel las reenvía a JS automáticamente.
    _active mantiene las señales vivas hasta que el job termina.
    """

    _MAX_THREADS = 2  # GS + fitz son CPU/IO pesados; más threads = más contención

    def __init__(self) -> None:
        self._pool = QThreadPool.globalInstance()
        self._pool.setMaxThreadCount(self._MAX_THREADS)
        self._active: dict[str, _JobSignals] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def create_job(self) -> tuple[str, _JobSignals]:
        """Crea job_id + signals sin arrancar el worker todavía.

        Permite al caller definir _work() capturando signals/job_id ANTES de start_job(),
        lo que habilita emisiones de progress desde dentro del worker.
        Usar siempre junto con start_job() — no hacer submit() sobre el mismo job.
        """
        job_id = uuid.uuid4().hex[:12]
        signals = _JobSignals()
        with self._lock:
            self._active[job_id] = signals
            self._cancel_events[job_id] = threading.Event()
        return job_id, signals

    def cancel_check_for(self, job_id: str) -> Callable[[], bool]:
        """Función para pasar como ``cancel_check`` al motor de PDF."""
        with self._lock:
            event = self._cancel_events.get(job_id) or threading.Event()
        return event.is_set

    def start_job(
        self,
        job_id: str,
        fn: Callable,
        args: tuple = (),
        kwargs: dict | None = None,
    ) -> None:
        """Arranca worker para un job ya creado con create_job()."""
        signals = self._active.get(job_id)
        if not signals:
            raise RuntimeError(f"Job {job_id!r} no encontrado en _active.")

        def _tracked(*a, **kw):
            try:
                return fn(*a, **kw)
            finally:
                with self._lock:
                    self._running.discard(job_id)

        with self._lock:
            self._running.add(job_id)
        worker = _JobWorker(job_id, _tracked, args, kwargs or {}, signals)
        self._pool.start(worker)

    def submit(
        self,
        fn: Callable,
        args: tuple = (),
        kwargs: dict | None = None,
        *,
        on_finished: Callable[[str], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[str, _JobSignals]:
        """Ejecuta fn(*args, **kwargs) en hilo background. Devuelve (job_id, signals).

        Conecta ``on_finished``/``on_progress`` ANTES de arrancar el worker: si se
        conectaran después, un job muy rápido podía emitir ``finished`` sin
        receptor y la interfaz quedaba esperando hasta el timeout.

        _active mantiene signals vivo hasta que el caller lo limpia con cleanup_job().
        No conectamos cleanup aquí: si el cleanup lambda corriera en slot-1 y el relay
        en slot-2, CPython puede destruir signals entre ambos (refcount 0 después de pop),
        causando que slot-2 nunca se llame y el job quede colgado.
        El caller debe llamar cleanup_job() DESPUÉS de relay (bridge._relay_job_finished).
        """
        job_id, signals = self.create_job()
        if on_finished is not None:
            signals.finished.connect(on_finished)
        if on_progress is not None:
            signals.progress.connect(on_progress)
        self.start_job(job_id, fn, args, kwargs)
        return job_id, signals

    def cancel(self, job_id: str) -> bool:
        """Pide cancelar un job en curso. Devuelve False si ya terminó."""
        with self._lock:
            event = self._cancel_events.get(job_id)
            running = job_id in self._running
        if event is None or not running:
            return False
        event.set()
        return True

    def has_running_jobs(self) -> bool:
        with self._lock:
            return bool(self._running)

    def shutdown(self, wait_ms: int = 3000) -> None:
        """Cancela todos los jobs y espera un poco a que suelten los archivos."""
        with self._lock:
            events = list(self._cancel_events.values())
        for event in events:
            event.set()
        self._pool.waitForDone(wait_ms)

    def cleanup_job(self, job_id: str) -> None:
        """Libera la referencia a signals para este job. Llamar DESPUÉS de relay."""
        with self._lock:
            self._active.pop(job_id, None)
            self._cancel_events.pop(job_id, None)
