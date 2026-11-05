"""Reglas exactas sobre los datos extraídos (fechas, vigencias, coincidencias).

Cada regla devuelve hallazgos ``{"nivel": "revisar"|"falta"|"info", "texto", "paginas"}``.
Nunca se marca "falta" por una duda de lectura: si un dato no se pudo leer, la regla
no opina (el analista lo verifica en el documento original).

Fuentes: instructivos 2024 (fecha de emisión anterior al período, prórroga del CUD,
sesiones del presupuesto ≤ orden, dependencia en transporte solo para mayores) y
reglas del flujo anterior (ARCA / bancaria con 2 años o más, zona desfavorable,
CUIT/CBU con verificador inválido).
"""

from __future__ import annotations

from datetime import date
from typing import Any

# Res. 1907/2023: prórroga de CUD vencidos en 2022 (+3 años), 2023 (+2) y 2024 (+1).
CUD_PRORROGA = {2022: 3, 2023: 2, 2024: 1}
ANTIGUEDAD_MAX_DIAS = 730  # 2 años: ARCA y documentación bancaria


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _pages(doc: dict) -> list[int]:
    return doc["paginas"]


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # 29/02
        return value.replace(year=value.year + years, day=28)


def cud_vencimiento_efectivo(vto: date) -> date:
    return _add_years(vto, CUD_PRORROGA.get(vto.year, 0))


def regla_cud(docs: list[dict], today: date) -> list[dict]:
    out = []
    for doc in docs:
        vto = _d(doc["datos"].get("vencimiento"))
        if not vto:
            out.append(
                {
                    "nivel": "info",
                    "texto": "CUD presente: no se pudo leer el vencimiento. Verificar vigencia.",
                    "paginas": _pages(doc),
                }
            )
            continue
        efectivo = cud_vencimiento_efectivo(vto)
        if efectivo < today:
            out.append(
                {
                    "nivel": "revisar",
                    "texto": f"CUD vencido (vto. {vto:%d/%m/%Y}"
                    + (f", prorrogado al {efectivo:%d/%m/%Y}" if efectivo != vto else "")
                    + "). Verificar vigencia.",
                    "paginas": _pages(doc),
                }
            )
        else:
            extra = f" (prórroga Res. 1907/2023 hasta {efectivo:%d/%m/%Y})" if efectivo != vto else ""
            numero = (doc["datos"].get("cud") or {}).get("numero")
            texto = f"CUD vigente, vence {vto:%d/%m/%Y}{extra}" + (f". N° {numero}" if numero else "") + "."
            out.append({"nivel": "ok", "texto": texto, "paginas": _pages(doc)})
    return out


def regla_identificadores(docs: list[dict]) -> list[dict]:
    """CUIT/CUIL o CBU con dígito verificador inválido: casi siempre error de lectura o tipeo."""
    out = []
    for doc in docs:
        for cuit in doc["datos"].get("cuit", []):
            if not cuit["valido"]:
                out.append(
                    {
                        "nivel": "revisar",
                        "texto": f"CUIT/CUIL {cuit['valor']} con dígito verificador inválido "
                        f"en {doc['nombre']}. Verificar en documento original.",
                        "paginas": _pages(doc),
                    }
                )
        for cbu in doc["datos"].get("cbu", []):
            if not cbu["valido"]:
                out.append(
                    {
                        "nivel": "revisar",
                        "texto": f"CBU {cbu['valor']} con dígitos verificadores inválidos "
                        f"en {doc['nombre']}. Verificar en documento original.",
                        "paginas": _pages(doc),
                    }
                )
    return out


def regla_antiguedad(docs: list[dict], today: date) -> list[dict]:
    """ARCA, comprobante de CBU o formulario bancario con 2 años o más (fecha más reciente leída)."""
    out = []
    for doc in docs:
        if doc["type_id"] not in ("constancia_arca", "comprobante_cbu", "acreditacion_bancaria"):
            continue
        fechas = [date.fromisoformat(f) for f in doc["datos"].get("fechas", []) if f]
        fechas = [f for f in fechas if f <= today]
        if not fechas:
            continue
        ultima = max(fechas)
        if (today - ultima).days >= ANTIGUEDAD_MAX_DIAS:
            que = "Constancia ARCA" if doc["type_id"] == "constancia_arca" else "Documentación bancaria"
            out.append(
                {
                    "nivel": "revisar",
                    "texto": f"{que} con antigüedad mayor a 2 años ({ultima:%d/%m/%Y}). "
                    "Verificar si corresponde presentarla actualizada.",
                    "paginas": _pages(doc),
                }
            )
    return out


def regla_fechas_y_periodos(ordenes: list[dict], presupuestos: list[dict]) -> list[dict]:
    """Emisión anterior al período; período y sesiones de orden vs. presupuesto."""
    out = []
    for doc in ordenes + presupuestos:
        periodo = doc["datos"].get("periodo") or {}
        desde = _d(periodo.get("desde"))
        fechas = [date.fromisoformat(f) for f in doc["datos"].get("fechas", [])]
        emision = fechas[0] if fechas else None
        if desde and emision and emision != desde and emision > desde:
            out.append(
                {
                    "nivel": "revisar",
                    "texto": f"{doc['nombre']}: fecha de emisión ({emision:%d/%m/%Y}) posterior al "
                    f"inicio del período ({desde:%d/%m/%Y}). Debe ser anterior.",
                    "paginas": _pages(doc),
                }
            )
    for orden in ordenes:
        po = orden["datos"].get("periodo") or {}
        for pres in presupuestos:
            pp = pres["datos"].get("periodo") or {}
            if (
                po.get("desde")
                and pp.get("desde")
                and (po.get("desde"), po.get("hasta")) != (pp.get("desde"), pp.get("hasta"))
            ):
                # El presupuesto suele indicar solo mes/año: comparar mes y año.
                same = all(
                    (a and b and a[:7] == b[:7])
                    for a, b in ((po.get("desde"), pp.get("desde")), (po.get("hasta"), pp.get("hasta")))
                    if a and b
                )
                if not same:
                    out.append(
                        {
                            "nivel": "revisar",
                            "texto": "Orden médica y presupuesto presentan diferencias en el período "
                            "solicitado. Verificar y unificar documentación.",
                            "paginas": _pages(orden) + _pages(pres),
                        }
                    )
            so, sp = orden["datos"].get("sesiones_semanales"), pres["datos"].get("sesiones_semanales")
            if so and sp and sp > so:
                out.append(
                    {
                        "nivel": "revisar",
                        "texto": f"El presupuesto indica {sp} sesiones semanales y la orden médica {so}: "
                        "no puede exceder lo prescripto.",
                        "paginas": _pages(orden) + _pages(pres),
                    }
                )
            elif so and sp and so == sp:
                out.append(
                    {
                        "nivel": "ok",
                        "texto": f"Orden médica y presupuesto coinciden en {so} sesiones semanales.",
                        "paginas": _pages(orden) + _pages(pres),
                    }
                )
    return out


def regla_zona_desfavorable(docs: list[dict]) -> list[dict]:
    out = []
    for doc in docs:
        zona = doc["datos"].get("zona_desfavorable")
        if zona:
            out.append(
                {
                    "nivel": "revisar",
                    "texto": f"Zona desfavorable detectada ({zona.title()}): verificar si corresponde "
                    "aplicar porcentaje de zona desfavorable en el presupuesto/facturación.",
                    "paginas": _pages(doc),
                }
            )
    return out


def regla_dependencia_transporte(ordenes: list[dict], edad: int | None) -> list[dict]:
    out = []
    for doc in ordenes:
        if doc["type_id"] == "prescripcion_transporte" and doc["datos"].get("dependencia") == "SI":
            if edad is not None and edad < 18:
                out.append(
                    {
                        "nivel": "revisar",
                        "texto": "Se solicita dependencia en transporte para un menor de edad: solo se "
                        "reconoce a mayores con asistencia acreditada por FIM.",
                        "paginas": _pages(doc),
                    }
                )
    return out


def regla_una_orden_por_tratamiento(
    cantidad_ordenes: int, cantidad_prestaciones: int, paginas: list[int]
) -> list[dict]:
    if 0 < cantidad_ordenes < cantidad_prestaciones:
        return [
            {
                "nivel": "revisar",
                "texto": f"Se identifican {cantidad_ordenes} orden(es) médica(s) para "
                f"{cantidad_prestaciones} prestaciones: se requiere una orden por tratamiento.",
                "paginas": paginas,
            }
        ]
    return []


def aplicar_todas(
    docs: list[dict], prestaciones: list[str], today: date, edad: int | None = None
) -> list[dict[str, Any]]:
    by_group = {
        "orden_medica": [d for d in docs if d["grupo"] == "orden_medica"],
        "presupuesto": [d for d in docs if d["grupo"] == "presupuesto"],
    }
    hallazgos: list[dict[str, Any]] = []
    hallazgos += regla_cud([d for d in docs if d["type_id"] == "cud"], today)
    hallazgos += regla_identificadores(docs)
    hallazgos += regla_antiguedad(docs, today)
    hallazgos += regla_fechas_y_periodos(by_group["orden_medica"], by_group["presupuesto"])
    hallazgos += regla_zona_desfavorable(by_group["presupuesto"])
    hallazgos += regla_dependencia_transporte(by_group["orden_medica"], edad)
    hallazgos += regla_una_orden_por_tratamiento(
        len(by_group["orden_medica"]),
        len([p for p in prestaciones if p != "transporte"]) + (1 if "transporte" in prestaciones else 0),
        [p for d in by_group["orden_medica"] for p in d["paginas"]],
    )
    return hallazgos
