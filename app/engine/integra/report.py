"""Informe del análisis: HTML para la pantalla/guardar y texto breve para responder al socio.

Mantiene la estructura que usaba el agente unificador: beneficiario, detalle por
prestación con ✔ Correcto / ⚠ Observado / ❌ Faltante y una respuesta breve. Usa la
palabra "legajo" y no menciona OCR en la respuesta al socio.
"""

from __future__ import annotations

from html import escape
from typing import Any

MAX_ITEMS_SOCIO = 6


def _hojas(paginas: list[int]) -> str:
    return ("hoja " if len(paginas) == 1 else "hojas ") + ", ".join(map(str, paginas))


def _ul(items: list[str], empty: str) -> str:
    if not items:
        return f"<ul><li>{escape(empty)}</li></ul>"
    return "<ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>"


def _bloque(nombre: str, data: dict[str, Any], extra: str = "") -> str:
    observado = data["aModificar"] + data["observaciones"]
    estado = data["estadoGeneral"]
    return (
        f"<div class='ir-block'><h3>{escape(nombre)}{extra} "
        f"<span class='ir-estado ir-{estado.lower()}'>{escape(estado)}</span></h3>"
        f"<b>✔ Correcto</b>{_ul(data['correcto'], 'Sin documentos correctos identificados')}"
        f"<b>⚠ Observado</b>{_ul(observado, 'Sin observaciones')}"
        f"<b>❌ Faltante</b>{_ul(data['faltante'], 'Sin faltantes')}</div>"
    )


def build_report_html(result: dict[str, Any]) -> str:
    benef = result["beneficiario"]
    head = ""
    if benef.get("nombre"):
        head = (
            f"<p class='ir-benef'><b>Beneficiario/a:</b> {escape(benef['nombre'])} — "
            f"DNI {escape(benef['dni'] or 'no identificado')}</p>"
        )
    prest = ", ".join(p["prestacionAnalizada"] for p in result["prestaciones"]) or "—"
    parts = [f"<div class='ir-report'>{head}<p><b>Prestaciones:</b> {escape(prest)}</p>"]
    parts.append(_bloque(result["generales"]["nombre"], result["generales"]))
    for p in result["prestaciones"]:
        extra = " <small>(requisitos provisorios)</small>" if p.get("provisoria") else ""
        parts.append(_bloque(p["prestacionAnalizada"], p, extra))
    if result["no_requeridos"]:
        items = [f"{d['nombre']} ({_hojas(d['paginas'])})" for d in result["no_requeridos"]]
        parts.append(
            f"<div class='ir-block'><h3>Hojas no requeridas o sin identificar</h3>{_ul(items, '')}</div>"
        )
    parts.append(
        "<p class='ir-pie'>Análisis automático: firmas, sellos, enmiendas y datos manuscritos "
        "deben verificarse en el documento original.</p></div>"
    )
    return "".join(parts)


def build_socio_text(result: dict[str, Any]) -> str:
    """Respuesta breve y accionable (sin lenguaje técnico)."""
    faltantes = list(result["generales"]["faltante"])
    revisar = list(result["generales"]["aModificar"])
    lineas = []
    for p in result["prestaciones"]:
        faltantes += p["faltante"]
        revisar += p["aModificar"]
        if not p["faltante"] and not p["aModificar"]:
            lineas.append(f"- {p['prestacionAnalizada']}: documentación completa.")
    vistos: set[str] = set()
    faltantes = [f for f in faltantes if not (f in vistos or vistos.add(f))]
    if faltantes:
        lineas.append("Para continuar con la gestión, se solicita presentar:")
        lineas += [
            f"- {f.rstrip('.')}".replace(" no identificado", "").replace(" no identificada", "")
            for f in faltantes[:MAX_ITEMS_SOCIO]
        ]
        if len(faltantes) > MAX_ITEMS_SOCIO:
            lineas.append(f"- y {len(faltantes) - MAX_ITEMS_SOCIO} documento(s) más (ver detalle).")
    if revisar:
        puntos = "; ".join(r.split(" (hoja")[0].rstrip(".") for r in revisar[:3])
        lineas.append(f"Además, revisar en el legajo: {puntos}.")
    if not faltantes and not revisar:
        lineas.append("La documentación presentada en el legajo se encuentra completa.")
    return "\n".join(lineas)
