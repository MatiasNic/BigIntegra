from datetime import date

from app.engine.integra import rules
from app.engine.integra.extractors import (
    cbu_is_valid,
    cuit_is_valid,
    extract_fields,
    field_value,
    find_cbus,
    find_dates,
    find_periodo,
)


def test_cuit_check_digit():
    assert cuit_is_valid("20-12345678-6")
    assert not cuit_is_valid("20-12345678-5")
    assert not cuit_is_valid("2012345678")


def test_cbu_check_digits():
    assert cbu_is_valid("2850590940090418135201")
    assert not cbu_is_valid("2850590940090418135202")


def test_cbu_not_built_from_numbered_table():
    """Los números de ítem de la FIM (2 3 4 … 16) no deben leerse como CBU."""
    assert find_cbus("2 3 4 5 6 7 8 9 10 11 12 13 14 15 16") == []
    assert find_cbus("CBU: 28505909 40090418135201")[0]["valido"]


def test_dates_numeric_and_text():
    assert find_dates("Fecha: 05/03/2025 y 1 de abril de 2025") == [date(2025, 3, 5), date(2025, 4, 1)]
    assert find_dates("31/02/2025") == []


def test_field_value_reads_label_and_ignores_empty_fields():
    text = "Cantidad de sesiones semanales: 5.\nNombre y apellido:\nDNI: Edad:"
    assert field_value(text, "Cantidad de sesiones semanales") == "5"
    assert field_value(text, "Nombre y apellido") == ""


def test_periodo_desde_hasta():
    p = find_periodo("Periodo: desde: 01/03/2025 hasta: 31/12/2025")
    assert (p.desde, p.hasta) == (date(2025, 3, 1), date(2025, 12, 31))


def test_extract_fields_prescripcion():
    text = (
        "PRESCRIPCIÓN MÉDICA PRESTACIONES DE REHABILITACIÓN\nFecha: 10/02/2025\n"
        "Prestación: Kinesiología\nCantidad de sesiones semanales: 3\n"
        "Periodo: desde: 01/03/2025 hasta: 31/12/2025"
    )
    data = extract_fields(text, "prescripcion_rehabilitacion")
    assert data["sesiones_semanales"] == 3
    assert data["prestacion"] == "kinesiologia"
    assert data["periodo"] == {"desde": "2025-03-01", "hasta": "2025-12-31"}


def _doc(type_id, datos, paginas=(1,), grupo=""):
    return {"type_id": type_id, "nombre": type_id, "grupo": grupo, "paginas": list(paginas), "datos": datos}


def test_cud_prorroga_res_1907():
    # Vencido en 2023 → prorrogado 2 años (hasta 2025).
    ok = rules.regla_cud([_doc("cud", {"vencimiento": "2023-06-30"})], date(2025, 1, 10))
    assert ok[0]["nivel"] == "ok" and "prórroga" in ok[0]["texto"]
    vencido = rules.regla_cud([_doc("cud", {"vencimiento": "2023-06-30"})], date(2025, 7, 1))
    assert vencido[0]["nivel"] == "revisar"


def test_arca_older_than_two_years():
    old = rules.regla_antiguedad([_doc("constancia_arca", {"fechas": ["2022-01-10"]})], date(2025, 3, 1))
    new = rules.regla_antiguedad([_doc("constancia_arca", {"fechas": ["2024-09-10"]})], date(2025, 3, 1))
    assert len(old) == 1 and not new


def test_orden_vs_presupuesto_sesiones_y_periodo():
    orden = _doc(
        "prescripcion_rehabilitacion",
        {
            "fechas": ["2025-02-10"],
            "sesiones_semanales": 2,
            "periodo": {"desde": "2025-03-01", "hasta": "2025-12-31"},
        },
        grupo="orden_medica",
    )
    pres = _doc(
        "presupuesto_profesional",
        {
            "fechas": ["2025-02-12"],
            "sesiones_semanales": 3,
            "periodo": {"desde": "2025-04-01", "hasta": "2025-12-31"},
        },
        (2,),
        grupo="presupuesto",
    )
    textos = [h["texto"] for h in rules.regla_fechas_y_periodos([orden], [pres])]
    assert any("no puede exceder" in t for t in textos)
    assert any("diferencias en el período" in t for t in textos)


def test_emision_posterior_al_inicio():
    orden = _doc(
        "prescripcion_rehabilitacion",
        {"fechas": ["2025-03-15"], "periodo": {"desde": "2025-03-01", "hasta": "2025-12-31"}},
        grupo="orden_medica",
    )
    textos = [h["texto"] for h in rules.regla_fechas_y_periodos([orden], [])]
    assert any("posterior al inicio" in t for t in textos)


def test_cud_code_has_dates():
    # Número de CUD ficticio: emitido 15/03/2022, vence 15/03/2027.
    from app.engine.integra.extractors import find_cud_code

    code = find_cud_code("Certificado N° ARG-01-20123456786-20220315-20270315-BUE-123 otra cosa")
    assert code["vencimiento"] == "2027-03-15" and code["emision"] == "2022-03-15"
    # Variantes de la cola vistas en legajos reales (provincia de 2 letras, 5 dígitos, sin cola).
    for tail in ("-BA-123", "-BUE-12345", "", "-01-123"):
        assert find_cud_code(f"ARG-01-20123456786-20220315-20270315{tail}")["vencimiento"] == "2027-03-15"
    # Tolera espacios del OCR y prefijo mal leído.
    assert (
        find_cud_code("IRG - 01 - 20123456786 - 20220315 - 20270315 - BUE - 123")["vencimiento"]
        == "2027-03-15"
    )


def test_cud_vencimiento_prefers_code_over_label():
    text = (
        "CERTIFICADO ÚNICO DE DISCAPACIDAD\nVencimiento: 01/01/2020\n"
        "ARG-01-20123456786-20220315-20270315-BUE-123"
    )
    assert extract_fields(text, "cud")["vencimiento"] == "2027-03-15"


def test_cue():
    from app.engine.integra.extractors import find_cue

    assert find_cue("Escuela N° 5 - CUE: 060123400") == "060123400"
    assert find_cue("C.U.E. N° 0601234-00") == "060123400"
    assert find_cue("sin clave") == ""
