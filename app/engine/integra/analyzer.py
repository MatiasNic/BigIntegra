"""Análisis del legajo: arma documentos, evalúa el checklist de cada prestación y propone orden.

Entrada: las páginas en el orden actual de la grilla, cada una con su texto (ya leído)
y su clasificación (automática o corregida a mano), más las prestaciones y condiciones
que eligió el usuario.

Salida (dict JSON): por prestación el mismo esquema que usaban los subagentes del flujo
anterior (``correcto`` / ``aModificar`` / ``faltante`` / ``observaciones``) más el
detalle por requisito con las hojas donde se encontró, el orden propuesto y el informe.
"""

from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.engine.integra import rules
from app.engine.integra.classifier import CATALOG_DIR, Classification, load_catalog
from app.engine.integra.extractors import extract_fields, field_value, find_dnis, first_int
from app.engine.integra.ordering import propose_order
from app.engine.integra.report import build_report_html, build_socio_text

ESTADO_OK, ESTADO_REVISAR, ESTADO_FALTA = "ok", "revisar", "falta"


@lru_cache(maxsize=2)
def load_prestaciones(path: str | None = None) -> dict[str, Any]:
    file = Path(path) if path else CATALOG_DIR / "prestaciones.json"
    return json.loads(file.read_text(encoding="utf-8"))


def catalog_summary() -> dict[str, Any]:
    """Lo que la interfaz necesita para armar el panel: prestaciones, condiciones y tipos."""
    pres = load_prestaciones()
    cat = load_catalog()
    return {
        "version": pres.get("version", ""),
        "prestaciones": [
            {"id": p["id"], "nombre": p["nombre"], "provisoria": bool(p.get("provisoria"))}
            for p in pres["prestaciones"]
        ],
        "condiciones": pres["condiciones"],
        "tipos": [{"id": t.id, "nombre": t.nombre, "grupo": t.grupo} for t in cat.tipos],
    }


def build_documents(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agrupa hojas consecutivas del mismo tipo y mismo archivo en un documento."""
    docs: list[dict[str, Any]] = []
    for pos, page in enumerate(pages):
        cls: Classification = page["clasificacion"]
        same = (
            docs
            and docs[-1]["type_id"] == cls.type_id
            and docs[-1]["source_document_id"] == page.get("source_document_id")
            and cls.type_id not in ("desconocido",)
            and not _starts_new_doc(cls)
        )
        if same:
            docs[-1]["paginas"].append(pos + 1)
            docs[-1]["page_ids"].append(page.get("page_id"))
            docs[-1]["texto"] += "\n" + page.get("text", "")
            continue
        docs.append(
            {
                "type_id": cls.type_id,
                "nombre": cls.nombre,
                "grupo": cls.grupo,
                "confianza": cls.confianza,
                "source_document_id": page.get("source_document_id"),
                "archivo": page.get("filename", ""),
                "paginas": [pos + 1],
                "page_ids": [page.get("page_id")],
                "texto": page.get("text", ""),
            }
        )
    for doc in docs:
        doc["datos"] = extract_fields(doc["texto"], doc["type_id"])
    return docs


def _starts_new_doc(cls: Classification) -> bool:
    """Una hoja con título propio inicia otro documento aunque sea del mismo tipo
    (dos órdenes médicas seguidas en el mismo PDF), salvo formularios de varias hojas."""
    if cls.type_id == "fim":
        return False
    return any(h.startswith("título:") for h in cls.coincidencias)


def _aplica(req: dict, condiciones: dict[str, str], prestaciones: list[str]) -> bool:
    for key, value in (req.get("cuando") or {}).items():
        if condiciones.get(key) != value:
            return False
    excepto = req.get("excepto_solo")
    return not (excepto and set(prestaciones) <= set(excepto))


def _hojas(paginas: list[int]) -> str:
    if not paginas:
        return ""
    return ("hoja " if len(paginas) == 1 else "hojas ") + ", ".join(str(p) for p in paginas)


def _evaluar_requisito(req: dict, docs: list[dict]) -> dict[str, Any]:
    principales = [d for d in docs if d["type_id"] in req.get("tipos", [])]
    alternativos = [d for d in docs if d["type_id"] in req.get("alternativos", [])]
    item = {"id": req["id"], "nombre": req["nombre"], "paginas": [], "detalle": ""}
    if principales:
        paginas = [p for d in principales for p in d["paginas"]]
        dudoso = all(d["confianza"] in ("baja", "continuacion") for d in principales)
        nombres = " + ".join(dict.fromkeys(d["nombre"] for d in principales))
        item.update(
            estado=ESTADO_REVISAR if dudoso else ESTADO_OK,
            paginas=paginas,
            detalle=f"{nombres} ({_hojas(paginas)})"
            + (". Identificación dudosa: verificar." if dudoso else ""),
        )
    elif alternativos:
        paginas = [p for d in alternativos for p in d["paginas"]]
        item.update(
            estado=ESTADO_REVISAR,
            paginas=paginas,
            detalle=f"{req.get('obs_alternativo', 'Documento en otro formato.')} ({_hojas(paginas)})",
        )
    elif req.get("opcional"):
        item.update(estado=ESTADO_REVISAR, detalle=req["falta"])
    else:
        item.update(estado=ESTADO_FALTA, detalle=req["falta"])
    return item


def _resumen(items: list[dict], hallazgos: list[dict]) -> dict[str, Any]:
    correcto = [i["detalle"] for i in items if i["estado"] == ESTADO_OK]
    a_modificar = [i["detalle"] for i in items if i["estado"] == ESTADO_REVISAR]
    faltante = [i["detalle"] for i in items if i["estado"] == ESTADO_FALTA]
    observaciones = []
    for h in hallazgos:
        texto = h["texto"] + (f" ({_hojas(h['paginas'])})" if h.get("paginas") else "")
        if h["nivel"] == "ok":
            correcto.append(texto)
        elif h["nivel"] == "revisar":
            observaciones.append(texto)
        elif h["nivel"] == "info":
            observaciones.append(texto)
    if faltante:
        estado = "INCOMPLETO"
    elif a_modificar or observaciones:
        estado = "OBSERVADO"
    else:
        estado = "COMPLETO"
    return {
        "estadoGeneral": estado,
        "correcto": correcto,
        "aModificar": a_modificar,
        "faltante": faltante,
        "observaciones": observaciones,
    }


def beneficiario(docs: list[dict]) -> dict[str, Any]:
    """Nombre, DNI y edad del beneficiario según los formularios oficiales."""
    nombre, dni, edad = "", "", None

    def _valid_name(value: str) -> str:
        return value if len(re.findall(r"[a-z]{2,}", value)) >= 2 and ":" not in value else ""

    prioridad = [
        "prescripcion_rehabilitacion",
        "prescripcion_terapeutico_educativa",
        "prescripcion_transporte",
        "resumen_hc",
        "datos_personales",
        "conformidad_prestacional",
    ]
    for type_id in prioridad:
        for doc in (d for d in docs if d["type_id"] == type_id):
            nombre = (
                nombre
                or _valid_name(field_value(doc["texto"], "Nombre y apellido", 60))
                or _valid_name(field_value(doc["texto"], "Apellido y nombre del beneficiario", 60))
                or _valid_name(field_value(doc["texto"], "Apellido y nombre", 60))
            )
            dni = dni or next(iter(find_dnis(doc["texto"])), "")
            edad = edad if edad is not None else first_int(field_value(doc["texto"], "Edad", 10))
    dnis = sorted({x for d in docs for x in d["datos"].get("dni", [])})
    dni = dni or (dnis[0] if len(dnis) == 1 else "")
    return {
        "nombre": nombre.title(),
        "dni": dni,
        "edad": edad,
        "dnis_distintos": dnis if len(dnis) > 1 else [],
    }


def analyze(
    pages: list[dict[str, Any]],
    prestaciones: list[str],
    condiciones: dict[str, str] | None = None,
    *,
    modo: str = "integracion",
    today: date | None = None,
) -> dict[str, Any]:
    """Analiza el legajo. ``pages`` en orden de la grilla, cada una con ``clasificacion``."""
    today = today or date.today()
    catalog = load_prestaciones()
    defaults = {c["id"]: c["defecto"] for c in catalog["condiciones"]}
    condiciones = {**defaults, **(condiciones or {})}
    docs = build_documents(pages)
    benef = beneficiario(docs)
    hallazgos = rules.aplicar_todas(docs, prestaciones, today, benef["edad"])

    generales_items = [
        _evaluar_requisito(req, docs)
        for req in catalog["generales"]["requisitos"]
        if _aplica(req, condiciones, prestaciones)
    ]
    general_hallazgos = [h for h in hallazgos if not _es_de_prestacion(h)]
    if benef["dnis_distintos"]:
        general_hallazgos.append(
            {
                "nivel": "revisar",
                "texto": "Se leen DNI distintos en el legajo ("
                + ", ".join(benef["dnis_distintos"])
                + "). Verificar que toda la documentación sea del mismo beneficiario.",
                "paginas": [],
            }
        )
    resultado_prest = []
    for pres in catalog["prestaciones"]:
        if pres["id"] not in prestaciones:
            continue
        items = [
            _evaluar_requisito(req, docs)
            for req in pres["requisitos"]
            if _aplica(req, condiciones, prestaciones)
        ]
        resultado_prest.append(
            {
                "tipoPrestacion": pres["id"],
                "prestacionAnalizada": pres["nombre"],
                "provisoria": bool(pres.get("provisoria")),
                "fuente": pres.get("fuente", ""),
                "items": items,
                **_resumen(items, []),
            }
        )
    usados = {
        t
        for pres in catalog["prestaciones"]
        if pres["id"] in prestaciones
        for req in pres["requisitos"]
        for t in req.get("tipos", []) + req.get("alternativos", [])
    }
    usados |= {t for req in catalog["generales"]["requisitos"] for t in req.get("tipos", [])}
    no_requeridos = [
        {"nombre": d["nombre"], "paginas": d["paginas"], "type_id": d["type_id"]}
        for d in docs
        if d["type_id"] not in usados
    ]
    generales = {
        "nombre": catalog["generales"]["nombre"],
        "items": generales_items,
        **_resumen(generales_items, general_hallazgos),
    }
    result = {
        "version_catalogo": catalog.get("version", ""),
        "fecha_analisis": today.isoformat(),
        "condiciones": condiciones,
        "beneficiario": benef,
        "generales": generales,
        "prestaciones": resultado_prest,
        "no_requeridos": no_requeridos,
        "documentos": [
            {
                k: d[k]
                for k in (
                    "type_id",
                    "nombre",
                    "grupo",
                    "confianza",
                    "archivo",
                    "paginas",
                    "page_ids",
                    "datos",
                )
            }
            for d in docs
        ],
        "orden_propuesto": propose_order(docs, catalog["orden_armado"], modo=modo),
    }
    result["informe_html"] = build_report_html(result)
    result["respuesta_socio"] = build_socio_text(result)
    return result


def _es_de_prestacion(_hallazgo: dict) -> bool:
    # Hoy todas las reglas se muestran en "generales"; se deja el punto de extensión
    # para asignar hallazgos a una prestación cuando haya un documento por prestación.
    return False
