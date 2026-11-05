"""Tests del bridge (lo que llama la interfaz): importar, comprimir, cancelar,
exportar en proceso aparte y guardar. Corren sin ventana (Qt offscreen)."""

import json

import fitz
import pytest

from app.config import UPLOADS_DIR


def _call(bridge, method, payload=None):
    return json.loads(getattr(bridge, method)(json.dumps(payload or {})))


@pytest.fixture
def pdf_path(tmp_path):
    """PDF de 6 páginas; la primera ya trae /Rotate 90 (como muchos escaneos)."""
    path = tmp_path / "documento con espacios.pdf"
    doc = fitz.open()
    for i in range(6):
        page = doc.new_page()
        page.insert_text((72, 100), f"Página {i + 1}", fontsize=30)
    doc[0].set_rotation(90)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def imported(bridge, pdf_path, wait_job):
    bridge.set_last_selected_paths([str(pdf_path)])
    started = _call(bridge, "importSelectedFiles", {"names": [pdf_path.name]})
    result = wait_job(started["job_id"])
    assert result["ok"], result
    (doc,) = result["result"]["documents"]
    assert doc["status"] == "ready", doc
    return doc


def test_import_by_path(imported):
    assert imported["pages"] == 6
    assert (UPLOADS_DIR / f"{imported['document_id']}.pdf").exists()


def test_import_rejects_invalid_file_and_cleans_up(bridge, wait_job, tmp_path):
    fake = tmp_path / "no_es_pdf.pdf"
    fake.write_bytes(b"hola, no soy un PDF")
    before = set(UPLOADS_DIR.iterdir())
    bridge.set_last_selected_paths([str(fake)])
    started = _call(bridge, "importSelectedFiles", {"names": [fake.name]})
    result = wait_job(started["job_id"])
    (doc,) = result["result"]["documents"]
    assert doc["status"] == "error"
    assert set(UPLOADS_DIR.iterdir()) == before


def test_invalid_ids_are_rejected(bridge):
    assert not _call(bridge, "getFileUrl", {"document_id": "../../etc/passwd"})["ok"]
    assert not _call(bridge, "compress", {"document_id": "x"})["ok"]
    assert _call(bridge, "cancelJob", {"job_id": "inexistente"})["cancel_requested"] is False


def test_compress_and_save_twice_without_overwriting(bridge, wait_job, imported):
    started = _call(bridge, "compress", {"document_id": imported["document_id"], "level": "high"})
    result = wait_job(started["job_id"], timeout=120)
    assert result["ok"], result
    export_id = result["result"]["export_id"]

    first = _call(bridge, "saveExport", {"export_id": export_id, "filename": "Salida.pdf"})
    second = _call(bridge, "saveExport", {"export_id": export_id, "filename": "Salida.pdf"})
    wait_job(first["job_id"])
    wait_job(second["job_id"])

    names = sorted(p.name for p in bridge.downloads.iterdir())
    assert names == ["Salida (2).pdf", "Salida.pdf"]
    for path in bridge.downloads.iterdir():
        with fitz.open(str(path)) as doc:
            assert doc.page_count == 6


def test_compress_can_be_cancelled(bridge, wait_job, imported):
    started = _call(bridge, "compress", {"document_id": imported["document_id"], "level": "high"})
    assert _call(bridge, "cancelJob", {"job_id": started["job_id"]})["cancel_requested"]
    result = wait_job(started["job_id"], timeout=60)
    assert not result["ok"]
    assert result.get("cancelled")
    assert not bridge.has_active_jobs()


def test_export_workspace_in_separate_process(bridge, wait_job, imported):
    doc_id = imported["document_id"]
    pages = [
        {"page_id": f"p{i}", "source_document_id": doc_id, "page_num": i, "rotation": 90 if i == 0 else 0}
        for i in (0, 2, 4)
    ]
    request = {
        "pages": pages,
        "outputs": [{"name": "Parte A", "page_ids": [p["page_id"] for p in pages], "compression": "none"}],
        "make_zip": True,
    }
    started = _call(bridge, "exportWorkspace", request)
    assert started["ok"], started
    result = wait_job(started["job_id"], timeout=120)
    assert result["ok"], result
    output = result["result"]["outputs"][0]
    assert result["result"]["zip_export_id"]

    from app.engine.services.export_registry import get_export

    with fitz.open(get_export(output["export_id"])["path"]) as doc:
        assert doc.page_count == 3
        # Rotación = delta: /Rotate 90 original + 90 de la interfaz = 180.
        assert doc[0].rotation == 180
    assert any(e.get("job_id") == started["job_id"] for e in bridge.progress_events)


def test_export_fails_clearly_when_source_is_missing(bridge):
    request = {
        "pages": [
            {
                "page_id": "p0",
                "source_document_id": "00000000-0000-4000-8000-000000000000",
                "page_num": 0,
            }
        ],
        "outputs": [{"name": "X", "page_ids": ["p0"]}],
    }
    result = _call(bridge, "exportWorkspace", request)
    assert not result["ok"]
    assert "no encontrados" in result["detail"]


def test_download_location_preference_persists(bridge):
    from app.engine.services import prefs_service

    original = prefs_service.get_choose_location()
    try:
        _call(bridge, "setDownloadLocationPref", {"choose_location": True})
        prefs_service._cache = None  # simula reiniciar la app: relee prefs.json
        assert _call(bridge, "getDownloadLocationPref")["choose_location"] is True
    finally:
        prefs_service.set_choose_location(original)


def _export_one(bridge, wait_job, doc_id, page_ids=(0,), name="Parte"):
    pages = [{"page_id": f"p{i}", "source_document_id": doc_id, "page_num": i} for i in page_ids]
    request = {"pages": pages, "outputs": [{"name": name, "page_ids": [p["page_id"] for p in pages]}]}
    started = _call(bridge, "exportWorkspace", request)
    assert started["ok"], started
    return wait_job(started["job_id"], timeout=120)


def test_save_cleans_invalid_characters_and_reports_real_name(bridge, wait_job, imported):
    result = _export_one(bridge, wait_job, imported["document_id"])
    export_id = result["result"]["outputs"][0]["export_id"]
    saved = []
    for name in ("Expte 12/2024: final.pdf", "Expte 12/2024: final.pdf"):
        started = _call(bridge, "saveExport", {"export_id": export_id, "filename": name})
        saved.append(wait_job(started["job_id"])["result"]["filename"])
    # Antes el "/" recortaba el nombre a "2024: final.pdf" (y ":" falla en Windows).
    assert saved == ["Expte 12-2024 - final.pdf", "Expte 12-2024 - final (2).pdf"]
    assert sorted(p.name for p in bridge.downloads.iterdir()) == sorted(saved)


def test_export_rejects_unknown_pages_instead_of_dropping_them(bridge, wait_job, imported):
    request = {
        "pages": [{"page_id": "p0", "source_document_id": imported["document_id"], "page_num": 0}],
        "outputs": [{"name": "X", "page_ids": ["p0", "fantasma"]}],
    }
    started = _call(bridge, "exportWorkspace", request)
    result = wait_job(started["job_id"], timeout=120)
    assert not result["ok"]
    assert "faltan" in result["error"]


def test_import_reports_empty_and_damaged_files(bridge, wait_job, tmp_path, pdf_path):
    empty = tmp_path / "vacio.docx"
    empty.write_bytes(b"")
    truncated = tmp_path / "cortado.pdf"
    data = pdf_path.read_bytes()
    truncated.write_bytes(data[: len(data) * 2 // 3])
    bridge.set_last_selected_paths([str(empty), str(truncated)])
    started = _call(bridge, "importSelectedFiles", {"names": [empty.name, truncated.name]})
    docs = {d["filename"]: d for d in wait_job(started["job_id"])["result"]["documents"]}
    assert docs["vacio.docx"]["status"] == "error"
    assert "vacío" in docs["vacio.docx"]["error"]
    damaged = docs["cortado.pdf"]
    if damaged["status"] == "ready":
        assert "dañado" in damaged["warning"]
        # Quedó reconstruido: ya abre sin reparar (pdf.js lo puede mostrar).
        with fitz.open(str(UPLOADS_DIR / f"{damaged['document_id']}.pdf")) as doc:
            assert not doc.is_repaired
    else:
        assert damaged["error"]


def test_safe_error_messages_are_friendly(bridge):
    disk_full = bridge._safe_error(OSError(28, "No space left on device"))
    assert disk_full == "No hay espacio suficiente en el disco."
    assert "abierto en otro programa" in bridge._safe_error(PermissionError(13, "denied"))
    msg = bridge._safe_error(
        RuntimeError("Failed to open file '/tmp/x/0b9d5a3e-1c2d-4e5f-8a9b-0123456789ab.pdf' as type pdf.")
    )
    assert "0b9d5a3e" not in msg and "dañado" in msg


def test_integra_catalog_and_analyze(bridge, wait_job, tmp_path):
    """Integra: importar el instructivo oficial de rehabilitación y analizarlo."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "formularios_2024"
        / "Tratamientos_Reabilitacion.pdf"
    )
    catalog = _call(bridge, "integraCatalog")
    assert catalog["ok"] and any(p["id"] == "rehabilitacion" for p in catalog["prestaciones"])

    bridge.set_last_selected_paths([str(src)])
    started = _call(bridge, "importSelectedFiles", {"names": [src.name]})
    (doc,) = wait_job(started["job_id"])["result"]["documents"]
    pages = [
        {"page_id": f"p{i}", "source_document_id": doc["document_id"], "page_num": i}
        for i in range(doc["pages"])
    ]
    started = _call(bridge, "integraAnalyze", {"pages": pages, "prestaciones": ["rehabilitacion"]})
    assert started["ok"], started
    result = wait_job(started["job_id"])
    assert result["ok"], result
    hojas = [h["type_id"] for h in result["result"]["hojas"]]
    assert hojas[2] == "prescripcion_rehabilitacion"
    assert result["result"]["prestaciones"][0]["tipoPrestacion"] == "rehabilitacion"


def test_integra_analyze_rejects_bad_ids(bridge):
    bad = _call(
        bridge, "integraAnalyze", {"pages": [{"page_id": "x", "source_document_id": "../x", "page_num": 0}]}
    )
    assert not bad["ok"]
