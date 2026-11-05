"""Identifica qué documento es cada página comparando su texto con el catálogo.

Cada tipo del catálogo (``catalog/document_types.json``) tiene:

- ``titulos``: el título impreso del formulario oficial (o de un documento externo
  conocido). Es la señal más fuerte.
- ``pies``: el pie de página de los formularios oficiales ("Tratamientos de
  rehabilitación 2"). También es una señal fuerte.
- ``campos``: rótulos característicos ("Cantidad de sesiones semanales").
- ``palabras``: palabras sueltas, de peso bajo.
- ``excluye``: frases que indican que NO es ese documento (por ejemplo, el texto del
  instructivo menciona "presupuesto prestacional" pero no es un presupuesto).

Todo se compara contra texto normalizado (ver ``text_service.normalize``): sin
tildes, en minúsculas, ignorando signos. Para tolerar errores de OCR, un título del
que se leen casi todas las palabras suma la mitad del puntaje.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.engine.integra.text_service import normalize

CATALOG_DIR = Path(__file__).resolve().parent / "catalog"

# Umbrales de confianza (puntaje del mejor tipo y ventaja sobre el segundo).
ALTA_MIN, ALTA_MARGEN = 10.0, 4.0
MEDIA_MIN = 6.0
BAJA_MIN = 3.0

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass
class DocType:
    id: str
    nombre: str
    grupo: str
    oficial: bool = False
    prioritario: bool = False  # si coincide su título, gana siempre (instructivos)
    titulos: list[str] = field(default_factory=list)
    pies: list[str] = field(default_factory=list)
    campos: list[str] = field(default_factory=list)
    palabras: list[str] = field(default_factory=list)
    excluye: list[str] = field(default_factory=list)
    patrones: list[str] = field(default_factory=list)  # regex sobre el texto original
    nota: str = ""


@dataclass
class Catalog:
    version: str
    pesos: dict[str, float]
    tipos: list[DocType]

    def get(self, type_id: str) -> DocType | None:
        return next((t for t in self.tipos if t.id == type_id), None)


@dataclass
class Classification:
    """Resultado para una página."""

    type_id: str  # "desconocido" si no hay coincidencia suficiente
    nombre: str
    grupo: str
    score: float
    confianza: str  # "alta" | "media" | "baja" | "desconocido" | "continuacion" | "manual"
    coincidencias: list[str] = field(default_factory=list)
    alternativas: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


UNKNOWN_NAME = "Sin identificar"


@lru_cache(maxsize=4)
def load_catalog(path: str | None = None) -> Catalog:
    file = Path(path) if path else CATALOG_DIR / "document_types.json"
    raw = json.loads(file.read_text(encoding="utf-8"))
    known = set(DocType.__dataclass_fields__)
    tipos = [DocType(**{k: v for k, v in t.items() if k in known}) for t in raw["tipos"]]
    return Catalog(version=raw.get("version", ""), pesos=raw["pesos"], tipos=tipos)


@lru_cache(maxsize=2048)
def _phrase_regex(phrase: str) -> re.Pattern[str] | None:
    words = _WORD_RE.findall(normalize(phrase))
    if not words:
        return None
    body = r"[^a-z0-9]{0,6}".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")


def _has_phrase(text: str, phrase: str) -> bool:
    rx = _phrase_regex(phrase)
    return bool(rx and rx.search(text))


def _partial_title(tokens: set[str], phrase: str) -> bool:
    """Título leído con errores de OCR: están ≥ 80% de sus palabras significativas."""
    words = [w for w in _WORD_RE.findall(normalize(phrase)) if len(w) >= 3]
    if len(words) < 3:
        return False
    hits = sum(1 for w in words if w in tokens)
    return hits / len(words) >= 0.8


def score_type(
    text: str, tokens: set[str], doc_type: DocType, pesos: dict[str, float]
) -> tuple[float, list[str]]:
    score = 0.0
    hits: list[str] = []
    for phrase in doc_type.excluye:
        if _has_phrase(text, phrase):
            score += pesos["excluye"]
            hits.append(f"excluye: {phrase}")
            break
    for pattern in doc_type.patrones:
        if re.search(pattern, text, re.IGNORECASE):
            score += pesos["titulo"]
            hits.append("título: patrón característico")
            break
    best_title = 0.0
    for phrase in doc_type.titulos:
        if _has_phrase(text, phrase):
            best_title = pesos["titulo"]
            hits.append(f"título: {phrase}")
            break
        if _partial_title(tokens, phrase):
            best_title = max(best_title, pesos["titulo"] * pesos["titulo_parcial"])
    if best_title and not any(h.startswith("título") for h in hits):
        hits.append("título (parcial, posible OCR)")
    score += best_title
    for phrase in doc_type.pies:
        if _has_phrase(text, phrase):
            score += pesos["pie"]
            hits.append(f"pie: {phrase}")
            break
    campos = [p for p in doc_type.campos if _has_phrase(text, p)]
    if campos:
        score += min(len(campos) * pesos["campo"], pesos["campo_max"])
        hits.extend(f"campo: {p}" for p in campos)
    palabras = [p for p in doc_type.palabras if _has_phrase(text, p)]
    if palabras:
        score += min(len(palabras) * pesos["palabra"], pesos["palabra_max"])
        hits.extend(f"palabra: {p}" for p in palabras)
    return score, hits


def _confidence(best: float, second: float) -> str:
    if best >= ALTA_MIN and best - second >= ALTA_MARGEN:
        return "alta"
    if best >= MEDIA_MIN:
        return "media"
    if best >= BAJA_MIN:
        return "baja"
    return "desconocido"


def classify_text(raw_text: str, catalog: Catalog | None = None) -> Classification:
    """Clasifica el texto de UNA página."""
    catalog = catalog or load_catalog()
    text = normalize(raw_text)
    tokens = set(_WORD_RE.findall(text))
    scored = []
    for doc_type in catalog.tipos:
        score, hits = score_type(text, tokens, doc_type, catalog.pesos)
        scored.append((score, doc_type, hits))
    scored.sort(key=lambda item: item[0], reverse=True)
    # Las hojas de instructivo nunca son documentación presentada: si una frase propia
    # del instructivo aparece, gana aunque mencione otros documentos.
    for idx, (_score, doc_type, hits) in enumerate(scored):
        if doc_type.prioritario and any(h.startswith("título") for h in hits):
            scored.insert(0, scored.pop(idx))
            break
    best_score, best_type, best_hits = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    confianza = _confidence(best_score, second)
    alternativas = [
        {"type_id": t.id, "nombre": t.nombre, "score": round(s, 1)}
        for s, t, _ in scored[1:4]
        if s >= BAJA_MIN
    ]
    if confianza == "desconocido":
        return Classification(
            "desconocido", UNKNOWN_NAME, "desconocido", round(best_score, 1), confianza, [], alternativas
        )
    return Classification(
        best_type.id,
        best_type.nombre,
        best_type.grupo,
        round(best_score, 1),
        confianza,
        best_hits,
        alternativas,
    )


def classify_pages(pages: list[dict[str, Any]], catalog: Catalog | None = None) -> list[Classification]:
    """Clasifica una secuencia de páginas en orden y resuelve continuaciones.

    ``pages``: dicts con ``text`` y ``source_document_id``. Una página sin
    identificar (o de confianza baja sin título) que sigue a un documento del mismo
    archivo se toma como continuación de ese documento (hoja 2 de un informe, dorso
    de un título, etc.), marcada para revisar.
    """
    catalog = catalog or load_catalog()
    results: list[Classification] = []
    prev: Classification | None = None
    prev_source = None
    for page in pages:
        result = classify_text(page.get("text", ""), catalog)
        source = page.get("source_document_id")
        weak = result.confianza == "desconocido" or (
            result.confianza == "baja"
            and not any(h.startswith(("título", "pie")) for h in result.coincidencias)
        )
        if (
            weak
            and prev is not None
            and source == prev_source
            and prev.type_id not in ("desconocido", "instructivo")
        ):
            result = Classification(
                prev.type_id,
                prev.nombre,
                prev.grupo,
                result.score,
                "continuacion",
                ["continuación del documento anterior"],
                result.alternativas,
            )
        results.append(result)
        prev, prev_source = result, source
    return results


def manual_classification(type_id: str, catalog: Catalog | None = None) -> Classification:
    """Etiqueta elegida a mano por el usuario."""
    catalog = catalog or load_catalog()
    doc_type = catalog.get(type_id)
    if doc_type is None:
        return Classification("desconocido", UNKNOWN_NAME, "desconocido", 0.0, "manual")
    return Classification(doc_type.id, doc_type.nombre, doc_type.grupo, 0.0, "manual")
