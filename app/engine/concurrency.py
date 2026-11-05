import threading
from collections.abc import Callable
from contextlib import contextmanager

from app.config import MAX_CONCURRENT_PDF_OPS, PDF_SLOT_WAIT_SECONDS

_pdf_semaphore = threading.Semaphore(MAX_CONCURRENT_PDF_OPS)


class ServerBusyError(Exception):
    """Otra operación pesada sigue en curso y se agotó la espera."""


@contextmanager
def pdf_operation_slot(
    on_wait: Callable[[], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
):
    """Serializa las operaciones pesadas de PDF (importar, comprimir).

    Si hay otra en curso, la nueva queda en cola hasta PDF_SLOT_WAIT_SECONDS en
    vez de fallar al instante. ``on_wait`` se llama una vez si hay que esperar
    (para avisar en la interfaz). Bloquea el hilo que la llama: usarla solo
    desde workers, nunca en el hilo de Qt.
    """
    acquired = _pdf_semaphore.acquire(blocking=False)
    if not acquired:
        if on_wait is not None:
            try:
                on_wait()
            except Exception:
                pass
        waited = 0.0
        while not acquired and waited < PDF_SLOT_WAIT_SECONDS:
            if cancel_check is not None and cancel_check():
                from app.engine.cancellation import OperationCancelled
                raise OperationCancelled("Procesamiento cancelado por el usuario.")
            acquired = _pdf_semaphore.acquire(timeout=0.5)
            waited += 0.5
    if not acquired:
        raise ServerBusyError(
            "Big PDF sigue terminando otra tarea. Esperá a que finalice e intentá de nuevo."
        )
    try:
        yield
    finally:
        _pdf_semaphore.release()
