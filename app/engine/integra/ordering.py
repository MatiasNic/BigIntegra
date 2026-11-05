"""Orden de armado propuesto para el legajo.

Ordena los documentos según ``orden_armado`` del catálogo de prestaciones (el orden
de los instructivos). Lo no identificado va al final y las hojas de instructivo, que
no se presentan, quedan en una parte aparte al final para que el usuario las borre.
"""

from __future__ import annotations

from typing import Any

DESCARTABLE = "instructivo"


def propose_order(docs: list[dict[str, Any]], orden_armado: list[str], *, modo: str = "integracion") -> dict:
    """Devuelve ``{"page_ids": [...], "partes": [{"nombre", "inicio", "cantidad"}]}``.

    - modo "entrada": una parte por documento (para exportar un PDF por documento).
    - modo "integracion": una parte con todo el legajo ordenado (+ parte de
      descartables/no identificados si los hay, para revisar antes de exportar).
    """
    rank = {type_id: idx for idx, type_id in enumerate(orden_armado)}

    def key(item: tuple[int, dict]) -> tuple[int, int]:
        idx, doc = item
        if doc["type_id"] == DESCARTABLE:
            return (len(rank) + 2, idx)
        return (rank.get(doc["type_id"], len(rank) + 1), idx)

    ordered = [doc for _, doc in sorted(enumerate(docs), key=key)]
    page_ids: list[str] = []
    partes: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for doc in ordered:
        start = len(page_ids)
        page_ids.extend(doc["page_ids"])
        if modo == "entrada":
            counts[doc["nombre"]] = counts.get(doc["nombre"], 0) + 1
            nombre = (
                doc["nombre"] if counts[doc["nombre"]] == 1 else f"{doc['nombre']} ({counts[doc['nombre']]})"
            )
            if doc["type_id"] == DESCARTABLE:
                nombre = "NO PRESENTAR - Instructivo"
            partes.append({"nombre": nombre, "inicio": start, "cantidad": len(doc["page_ids"])})
        else:
            grupo = "descartable" if doc["type_id"] == DESCARTABLE else "legajo"
            if not partes or partes[-1]["_grupo"] != grupo:
                partes.append(
                    {
                        "nombre": "Legajo" if grupo == "legajo" else "NO PRESENTAR - Instructivo",
                        "inicio": start,
                        "cantidad": 0,
                        "_grupo": grupo,
                    }
                )
            partes[-1]["cantidad"] += len(doc["page_ids"])
    for parte in partes:
        parte.pop("_grupo", None)
    return {"page_ids": page_ids, "partes": partes, "modo": modo}
