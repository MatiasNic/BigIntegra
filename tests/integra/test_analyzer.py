from datetime import date

from app.engine.integra import service
from app.engine.integra.analyzer import analyze, catalog_summary
from app.engine.integra.classifier import classify_pages

from .conftest import make_pdf

PRESCRIPCION = """PRESCRIPCIÓN MÉDICA
PRESTACIONES DE REHABILITACIÓN
Fecha: 10/02/2025
Nombre y apellido: Juana Pérez
DNI: 45.123.456 Edad: 8
Diagnóstico: TEA
Prestación: Fonoaudiología
Cantidad de sesiones semanales: 2.
Periodo: desde: 01/03/2025 hasta: 31/12/2025
Firma y sello del médico tratante"""

PRESUPUESTO = """PRESUPUESTO PRESTACIONAL
DE PROFESIONALES PARTICULARES
Buenos Aires, 12/02/2025
Prestación (Según Mecanismo Integración): Fonoaudiología
Período solicitado: 01/03/2025 hasta 31/12/2025
Cantidad de sesiones semanales: 2 Cantidad de sesiones mensuales: 8 Valor de la sesión: $ 100
Apellido y nombre del profesional: Ana Gómez
Especialidad: Fonoaudiología
Localidad: Trelew Provincia: Chubut
CRONOGRAMA DE ASISTENCIA"""

CUD = """CERTIFICADO ÚNICO DE DISCAPACIDAD
Agencia Nacional de Discapacidad - Ley 22.431
Orientación prestacional: rehabilitación
Fecha de vencimiento: 30/06/2027"""

INSTRUCTIVO = "Condiciones generales para toda la documentación:\nLa documentación deberá ser firmada"


def _pages(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    doc_id = "00000000-0000-4000-8000-000000000001"
    make_pdf(uploads / f"{doc_id}.pdf", [INSTRUCTIVO, PRESUPUESTO, CUD, PRESCRIPCION])
    pages = [
        {"page_id": f"p{i}", "source_document_id": doc_id, "page_num": i, "filename": "legajo.pdf"}
        for i in range(4)
    ]
    return uploads, pages


def test_catalog_summary_lists_prestaciones():
    summary = catalog_summary()
    ids = [p["id"] for p in summary["prestaciones"]]
    assert {"rehabilitacion", "transporte", "terapeutico_educativo"} <= set(ids)
    assert any(c["id"] == "tipo_solicitud" for c in summary["condiciones"])


def test_analyze_workspace_rehabilitacion(tmp_path):
    service.clear_cache()
    uploads, pages = _pages(tmp_path)
    result = service.analyze_workspace(
        {
            "pages": pages,
            "prestaciones": ["rehabilitacion"],
            "condiciones": {"prestador": "particular"},
            "modo": "entrada",
        },
        uploads,
    )
    tipos = [h["type_id"] for h in result["hojas"]]
    assert tipos == ["instructivo", "presupuesto_profesional", "cud", "prescripcion_rehabilitacion"]

    rehab = result["prestaciones"][0]
    estados = {i["id"]: i["estado"] for i in rehab["items"]}
    assert estados["orden"] == "ok" and estados["presupuesto"] == "ok"
    assert estados["plan"] == "falta" and estados["titulo"] == "falta"
    assert rehab["estadoGeneral"] == "INCOMPLETO"

    generales = {i["id"]: i["estado"] for i in result["generales"]["items"]}
    assert generales["cud"] == "ok" and generales["resumen_hc"] == "falta"
    obs = " ".join(result["generales"]["observaciones"] + result["generales"]["correcto"])
    assert "Zona desfavorable" in obs
    assert "coinciden en 2 sesiones" in obs

    assert result["beneficiario"]["nombre"] == "Juana Perez"
    assert result["beneficiario"]["dni"] == "45123456"

    # Orden propuesto: CUD, prescripción, presupuesto e instructivo al final (no se presenta).
    orden = result["orden_propuesto"]
    assert orden["page_ids"] == ["p2", "p3", "p1", "p0"]
    assert orden["partes"][-1]["nombre"].startswith("NO PRESENTAR")
    assert "respuesta_socio" in result and "Plan de abordaje" in result["respuesta_socio"]


def test_manual_override_is_respected(tmp_path):
    service.clear_cache()
    uploads, pages = _pages(tmp_path)
    pages[2]["override_type"] = "dni"
    result = service.analyze_workspace({"pages": pages, "prestaciones": []}, uploads)
    assert result["hojas"][2]["type_id"] == "dni"
    assert result["hojas"][2]["confianza"] == "manual"


def test_conditions_switch_requirements():
    pages = [{"page_id": "a", "source_document_id": "d", "text": PRESCRIPCION}]
    for page, cls in zip(pages, classify_pages(pages), strict=True):
        page["clasificacion"] = cls
    nueva = analyze(pages, ["rehabilitacion"], {"tipo_solicitud": "nueva"}, today=date(2025, 2, 1))
    cont = analyze(pages, ["rehabilitacion"], {"tipo_solicitud": "continuidad"}, today=date(2025, 2, 1))
    ids_nueva = {i["id"] for i in nueva["prestaciones"][0]["items"]}
    ids_cont = {i["id"] for i in cont["prestaciones"][0]["items"]}
    assert "informe_inicial" in ids_nueva and "informe_evolutivo" not in ids_nueva
    assert "informe_evolutivo" in ids_cont and "informe_inicial" not in ids_cont


def test_acompanante_terapeutico_disponible():
    ids = [p["id"] for p in catalog_summary()["prestaciones"]]
    assert "acompanante_terapeutico" in ids
    pages = [
        {"page_id": "a", "source_document_id": "d", "text": "ORDEN MÉDICA\nSolicito acompañante terapéutico"}
    ]
    for page, cls in zip(pages, classify_pages(pages), strict=True):
        page["clasificacion"] = cls
    result = analyze(pages, ["acompanante_terapeutico"], today=date(2025, 2, 1))
    (at,) = result["prestaciones"]
    assert at["provisoria"] and at["tipoPrestacion"] == "acompanante_terapeutico"
