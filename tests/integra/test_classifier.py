import pymupdf
import pytest

from app.engine.integra.classifier import classify_pages, classify_text, load_catalog
from app.engine.integra.text_service import extract_page_text, find_tessdata_dir, normalize

from .conftest import rasterize

# Tipo esperado por hoja de cada instructivo oficial (formularios 2024).
ESPERADO = {
    "Tratamientos_Reabilitacion.pdf": [
        "instructivo",
        "instructivo",
        "prescripcion_rehabilitacion",
        "presupuesto_profesional",
        "presupuesto_institucion_rehabilitacion",
        "trabajo_coordinado",
        "acreditacion_bancaria",
    ],
    "Transporte_Especial.pdf": [
        "instructivo",
        "instructivo",
        "conformidad_transporte",
        "prescripcion_transporte",
        "presupuesto_transporte",
        "acreditacion_bancaria",
    ],
    "Terapeuticas_Educativas_-_CET_-_CENTRO_DE_DIA.pdf": [
        "instructivo",
        "instructivo",
        "prescripcion_terapeutico_educativa",
        "presupuesto_terapeutico_educativo",
        "acreditacion_bancaria",
    ],
    "Estimulacion_Temprana.pdf": [
        "instructivo",
        "instructivo",
        "prescripcion_rehabilitacion",
        "presupuesto_institucion_rehabilitacion",
        "acreditacion_bancaria",
    ],
    "Requisitos_Generales.pdf": [
        "instructivo",
        "datos_personales",
        "conformidad_prestacional",
        "conformidad_transporte",
        "fim",
        "fim",
        "fim",
        "fim",
        "fim",
        "fim",
        "resumen_hc",
        "nota_cambio_prestador",
    ],
}


def test_catalog_loads_and_ids_are_unique():
    cat = load_catalog()
    ids = [t.id for t in cat.tipos]
    assert len(ids) == len(set(ids))
    assert "prescripcion_rehabilitacion" in ids


def test_normalize_removes_accents_and_ligatures():
    assert normalize("PRESCRIPCIÓN  ﬁrmada\n___ Médica") == "prescripcion firmada medica"


@pytest.mark.parametrize("filename", sorted(ESPERADO))
def test_official_forms_digital(formularios, filename):
    with pymupdf.open(formularios / filename) as doc:
        pages = [{"text": p.get_text(), "source_document_id": filename} for p in doc]
    got = [c.type_id for c in classify_pages(pages)]
    assert got == ESPERADO[filename]


@pytest.mark.skipif(find_tessdata_dir() is None, reason="Falta tools/tessdata/spa.traineddata")
def test_official_forms_scanned_with_ocr(formularios, tmp_path):
    """Las hojas de formularios escaneadas (sin capa de texto) se identifican por OCR."""
    src = formularios / "Tratamientos_Reabilitacion.pdf"
    scanned = rasterize(src, tmp_path / "scan.pdf")
    tessdata = find_tessdata_dir()
    with pymupdf.open(scanned) as doc:
        assert not doc[2].get_text().strip()  # de verdad no tiene texto
        texts = [extract_page_text(doc[i], tessdata) for i in (2, 3)]
    assert all(t.source == "ocr" for t in texts)
    assert classify_text(texts[0].text).type_id == "prescripcion_rehabilitacion"
    assert classify_text(texts[1].text).type_id == "presupuesto_profesional"


def test_unknown_text_and_continuation():
    pages = [
        {
            "text": "PRESCRIPCIÓN MÉDICA PRESTACIONES DE REHABILITACIÓN\nCantidad de sesiones semanales: 3",
            "source_document_id": "a",
        },
        {"text": "texto sin relación", "source_document_id": "a"},
        {"text": "texto sin relación", "source_document_id": "b"},
    ]
    got = classify_pages(pages)
    assert got[0].type_id == "prescripcion_rehabilitacion"
    assert (got[1].type_id, got[1].confianza) == ("prescripcion_rehabilitacion", "continuacion")
    assert got[2].type_id == "desconocido"


def test_external_documents_by_keywords():
    cud = (
        "REPÚBLICA ARGENTINA - AGENCIA NACIONAL DE DISCAPACIDAD\nCERTIFICADO ÚNICO DE DISCAPACIDAD\n"
        "Ley 22.431\nOrientación prestacional: rehabilitación\nAcompañante: SI\n"
        "Fecha de vencimiento: 10/05/2027"
    )
    arca = (
        "ARCA - Agencia de Recaudación y Control Aduanero\nCONSTANCIA DE INSCRIPCIÓN\n"
        "CUIT: 20-12345678-6\nActividad principal\nMonotributo"
    )
    assert classify_text(cud).type_id == "cud"
    assert classify_text(arca).type_id == "constancia_arca"


def test_cud_recognized_by_number_alone():
    """Un CUD mal escaneado se reconoce solo por la estructura de su número."""
    text = "ARG-01-20123456786-20220315-20270315-BUE-123\nApellido y nombre"
    assert classify_text(text).type_id == "cud"
