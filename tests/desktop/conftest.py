"""Fixtures para testear app/desktop (bridge, workers, exportación) sin ventana.

Usa Qt en modo "offscreen". Si PySide6 no está instalado (por ejemplo en el CI,
que no instala Qt para ahorrar minutos), estos tests se saltean solos.
"""
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 no instalado")


@pytest.fixture(scope="session")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def bridge(qapp, tmp_path):
    from app.desktop.bridge import BigPdfBridge

    downloads = tmp_path / "Descargas"
    downloads.mkdir()
    b = BigPdfBridge(None, get_downloads_dir=lambda: downloads)
    b.finished_jobs = {}
    b.progress_events = []
    b.jobFinished.connect(lambda raw: b.finished_jobs.__setitem__(json.loads(raw)["job_id"], json.loads(raw)))
    b.jobProgress.connect(lambda raw: b.progress_events.append(json.loads(raw)))
    b.downloads = downloads
    yield b
    b.shutdown_background_jobs()


@pytest.fixture
def wait_job(qapp, bridge):
    """wait_job(job_id): procesa eventos de Qt hasta que llegue jobFinished."""
    def _wait(job_id, timeout=60):
        deadline = time.monotonic() + timeout
        while job_id not in bridge.finished_jobs:
            if time.monotonic() > deadline:
                raise TimeoutError(f"El job {job_id} no terminó en {timeout} s")
            qapp.processEvents()
            time.sleep(0.02)
        return bridge.finished_jobs[job_id]
    return _wait
