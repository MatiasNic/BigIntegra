"""Extracción de datos de una página: fechas, DNI, CUIT/CUIL, CBU y campos de formulario.

Los formularios oficiales tienen rótulos fijos ("Cantidad de sesiones semanales:",
"Periodo: desde:"), así que se lee el valor que sigue al rótulo. Además se validan los
dígitos verificadores de CUIT/CUIL y CBU: un número con verificador inválido casi
siempre es un error de OCR o de tipeo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.engine.integra.text_service import normalize

MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

_DATE_NUM_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})(?!\d)")
_DATE_TXT_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:de\s+)?(" + "|".join(MESES) + r")\s*(?:de(?:l)?\s+)?(\d{4})"
)
_CUIT_RE = re.compile(r"(?<!\d)(\d{2})\s*[-.\s]?\s*(\d{8})\s*[-.\s]?\s*(\d)(?!\d)")
# 22 dígitos seguidos o en sus dos bloques (8 + 14). No se aceptan dígitos sueltos separados
# por espacios: en tablas numeradas (FIM) eso arma "CBU" falsos.
_CBU_RE = re.compile(r"(?<!\d)(\d{8}[\s-]?\d{14})(?!\d)")
_DNI_RE = re.compile(r"(?:d\.?\s?n\.?\s?i\.?|documento)[^0-9]{0,25}(\d{1,2}[.\s]?\d{3}[.\s]?\d{3})")


def _year(y: str) -> int:
    value = int(y)
    return value + 2000 if value < 100 else value


def _safe_date(d: int, m: int, y: int) -> date | None:
    try:
        result = date(y, m, d)
    except ValueError:
        return None
    return result if 1990 <= result.year <= 2100 else None


def find_dates(text: str) -> list[date]:
    """Todas las fechas (dd/mm/aaaa, dd-mm-aa, "5 de marzo de 2025"), en orden de aparición."""
    norm = normalize(text)
    found: list[tuple[int, date]] = []
    for m in _DATE_NUM_RE.finditer(norm):
        value = _safe_date(int(m.group(1)), int(m.group(2)), _year(m.group(3)))
        if value:
            found.append((m.start(), value))
    for m in _DATE_TXT_RE.finditer(norm):
        value = _safe_date(int(m.group(1)), MESES[m.group(2)], int(m.group(3)))
        if value:
            found.append((m.start(), value))
    return [d for _, d in sorted(found, key=lambda item: item[0])]


def cuit_is_valid(cuit: str) -> bool:
    digits = re.sub(r"\D", "", cuit)
    if len(digits) != 11:
        return False
    weights = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(digits[:10], weights, strict=True))
    check = 11 - total % 11
    check = 0 if check == 11 else 9 if check == 10 else check
    return check == int(digits[10])


def cbu_is_valid(cbu: str) -> bool:
    digits = re.sub(r"\D", "", cbu)
    if len(digits) != 22:
        return False
    w1 = (7, 1, 3, 9, 7, 1, 3)
    check1 = (10 - sum(int(d) * w for d, w in zip(digits[:7], w1, strict=True)) % 10) % 10
    w2 = (3, 9, 7, 1, 3, 9, 7, 1, 3, 9, 7, 1, 3)
    check2 = (10 - sum(int(d) * w for d, w in zip(digits[8:21], w2, strict=True)) % 10) % 10
    return check1 == int(digits[7]) and check2 == int(digits[21])


def find_cuits(text: str) -> list[dict]:
    """CUIT/CUIL encontrados con su validez (prefijos válidos: 20, 23, 24, 27, 30, 33, 34)."""
    out, seen = [], set()
    for m in _CUIT_RE.finditer(text):
        value = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if m.group(1) not in {"20", "23", "24", "27", "30", "33", "34"} or value in seen:
            continue
        seen.add(value)
        out.append({"valor": value, "valido": cuit_is_valid(value)})
    return out


def find_cbus(text: str) -> list[dict]:
    out, seen = [], set()
    for m in _CBU_RE.finditer(text):
        value = re.sub(r"\D", "", m.group(1))
        if value in seen:
            continue
        seen.add(value)
        out.append({"valor": value, "valido": cbu_is_valid(value)})
    return out


def find_dnis(text: str) -> list[str]:
    norm = normalize(text)
    out: list[str] = []
    for m in _DNI_RE.finditer(norm):
        value = re.sub(r"\D", "", m.group(1))
        if 6 <= len(value) <= 8 and value not in out:
            out.append(value)
    return out


def field_value(text: str, label: str, max_len: int = 80) -> str:
    """Valor escrito después de un rótulo, hasta el fin de línea ("" si está vacío).

    Ejemplo: field_value(texto, "Cantidad de sesiones semanales") → "5".
    """
    words = re.findall(r"[a-z0-9]+", normalize(label))
    if not words:
        return ""
    # Rótulos cortos ("Prestación", "Edad") exigen los dos puntos: si no, coinciden
    # dentro de títulos ("prestaciones de rehabilitación"). Los largos toleran que el
    # OCR se coma los dos puntos.
    colon = r"\s*:?\s*" if len(words) >= 3 else r"\s*:\s*"
    label_rx = r"[^a-z0-9]{0,6}".join(re.escape(w) for w in words)
    rx = re.compile(r"(?<![a-z0-9])" + label_rx + colon + r"([^\n]*)")
    lines = [normalize(line) for line in text.splitlines()]
    for idx, line in enumerate(lines):
        m = rx.search(line)
        if not m:
            continue
        value = m.group(1).strip(" .:_-")
        if not value and idx + 1 < len(lines) and ":" not in lines[idx + 1]:
            value = lines[idx + 1].strip(" .:_-")  # valor en la línea siguiente (OCR)
        # Si lo que sigue es otro rótulo ("DNI: Edad:"), el campo está vacío.
        if re.match(r"^[a-z ./]{1,25}:", value):
            return ""
        return value[:max_len]
    return ""


# Número de CUD: ARG-<2>-<11>-<AAAAMMDD emisión>-<AAAAMMDD vencimiento>-<PROV>-<nnn>.
# La parte con las fechas es fija; la cola (provincia de 2-3 letras, 3-5 dígitos) varía y
# a veces falta. Se toleran espacios/puntos del OCR y prefijos mal leídos (VEN, IRG…).
# Verificado contra 327 CUD leídos por el agente anterior.
_SEP = r"\s*[-–.]\s*"
_CUD_RE = re.compile(
    rf"\b([A-Z]{{3}}){_SEP}(\d{{2}}){_SEP}(\d{{11}}){_SEP}(\d{{8}}){_SEP}(\d{{8}})"
    rf"(?:{_SEP}([A-Z]{{2,3}}(?:\s[A-Z]{{2}})?|\d{{2}}))?(?:{_SEP}(\d{{1,5}}))?",
    re.IGNORECASE,
)


def _yyyymmdd(value: str) -> date | None:
    return _safe_date(int(value[6:8]), int(value[4:6]), int(value[:4]))


def find_cud_code(text: str) -> dict | None:
    """Número de CUD con sus fechas de emisión y vencimiento (codificadas en el número)."""
    m = _CUD_RE.search(text)
    if not m:
        return None
    emision, vencimiento = _yyyymmdd(m.group(4)), _yyyymmdd(m.group(5))
    if vencimiento and emision and vencimiento <= emision:
        vencimiento = None  # bloque mal leído: no se usa
    return {
        "numero": "-".join(g for g in m.groups() if g).upper(),
        "emision": emision.isoformat() if emision else None,
        "vencimiento": vencimiento.isoformat() if vencimiento else None,
    }


_CUE_RE = re.compile(r"\bc\.?\s?u\.?\s?e\.?\s*(?:n[°ºo.]*)?\s*:?\s*(\d{7}\s*[-/]?\s*\d{2})\b", re.IGNORECASE)


def find_cue(text: str) -> str:
    """CUE (Clave Única de Establecimiento): 9 dígitos (7 + 2 de anexo)."""
    m = _CUE_RE.search(text)
    return re.sub(r"\D", "", m.group(1)) if m else ""


def first_int(value: str) -> int | None:
    m = re.search(r"\d+", value or "")
    return int(m.group()) if m else None


@dataclass
class Periodo:
    desde: date | None
    hasta: date | None

    def to_dict(self) -> dict:
        return {
            "desde": self.desde.isoformat() if self.desde else None,
            "hasta": self.hasta.isoformat() if self.hasta else None,
        }


def find_periodo(text: str) -> Periodo:
    """Período "desde ... hasta ..." (primer par de fechas a partir de "periodo")."""
    norm = normalize(text)
    m = re.search(r"periodo[^\n]{0,30}?desde(.{0,160})", norm)
    if not m:
        return Periodo(None, None)
    segment = m.group(1)
    if "hasta" in segment:
        before, after = segment.split("hasta", 1)
        desde = next(iter(find_dates(before)), None)
        hasta = next(iter(find_dates(after)), None)
        return Periodo(desde, hasta)
    dates = find_dates(segment)
    return Periodo(dates[0] if dates else None, dates[1] if len(dates) > 1 else None)


def find_si_no(text: str, label: str) -> str:
    """Lee una casilla SÍ/NO marcada con X a continuación de un rótulo. "" si no se puede."""
    value = field_value(text, label, max_len=30)
    if re.search(r"(si|sí)\s*[x✓✔]|[x✓✔]\s*(si|sí)\b", value):
        return "SI"
    if re.search(r"\bno\s*[x✓✔]|[x✓✔]\s*no\b", value):
        return "NO"
    return ""


ZONAS_DESFAVORABLES = (
    "chubut",
    "la pampa",
    "neuquen",
    "rio negro",
    "santa cruz",
    "tierra del fuego",
    "patagones",
)


def find_zona_desfavorable(text: str) -> str:
    norm = normalize(text)
    return next((z for z in ZONAS_DESFAVORABLES if re.search(rf"\b{z}\b", norm)), "")


def extract_fields(text: str, type_id: str) -> dict:
    """Datos relevantes de una página según su tipo de documento."""
    data: dict = {
        "fechas": [d.isoformat() for d in find_dates(text)],
        "dni": find_dnis(text),
        "cuit": find_cuits(text),
        "cbu": find_cbus(text),
    }
    if type_id.startswith(("prescripcion", "presupuesto")) or type_id in (
        "conformidad_prestacional",
        "conformidad_transporte",
    ):
        data["periodo"] = find_periodo(text).to_dict()
    if type_id in ("prescripcion_rehabilitacion", "presupuesto_profesional"):
        data["sesiones_semanales"] = first_int(field_value(text, "Cantidad de sesiones semanales"))
    if type_id == "presupuesto_profesional":
        data["sesiones_mensuales"] = first_int(field_value(text, "Cantidad de sesiones mensuales"))
    if type_id.startswith(("prescripcion", "presupuesto")):
        data["prestacion"] = field_value(text, "Prestación")
    if type_id in (
        "prescripcion_terapeutico_educativa",
        "prescripcion_transporte",
        "presupuesto_terapeutico_educativo",
        "presupuesto_transporte",
    ):
        data["dependencia"] = find_si_no(text, "Dependencia")
    if type_id in ("prescripcion_terapeutico_educativa", "presupuesto_terapeutico_educativo"):
        jornada = field_value(text, "Tipo de jornada", 40)
        data["jornada"] = (
            "doble"
            if "doble" in jornada and "simple" not in jornada
            else ("simple" if "simple" in jornada and "doble" not in jornada else "")
        )
    if type_id == "presupuesto_terapeutico_educativo":
        data["categoria"] = field_value(text, "Categoría", 20)
    if type_id.startswith("presupuesto") or type_id in ("habilitacion_transporte",):
        data["zona_desfavorable"] = find_zona_desfavorable(text)
    if type_id == "presupuesto_transporte":
        data["km_mensuales"] = first_int(field_value(text, "Total Kms. mensuales", 20))
    if type_id == "cud":
        codigo = find_cud_code(text)
        data["cud"] = codigo
        vto = re.search(
            r"vencimiento[^0-9]{0,30}(\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4})", normalize(text)
        )
        leido = find_dates(vto.group(1))[0].isoformat() if vto and find_dates(vto.group(1)) else None
        # El vencimiento codificado en el número de CUD es más confiable que el rótulo leído
        # por OCR (en 274 de 281 legajos del agente anterior coincidían ambos).
        data["vencimiento"] = (codigo or {}).get("vencimiento") or leido
    if type_id in ("alumno_regular", "acta_acuerdo") or "cue" in normalize(text).split():
        data["cue"] = find_cue(text)
    return data
