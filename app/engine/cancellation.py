"""Cancelación cooperativa para operaciones PDF pesadas."""
from __future__ import annotations

from collections.abc import Callable


class OperationCancelled(RuntimeError):
    """La operación fue cancelada deliberadamente por el usuario."""


def raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise OperationCancelled("Procesamiento cancelado por el usuario.")
