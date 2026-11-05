"""Calibración del identificador de documentos con legajos reales (se corre en la PC local).

Los documentos NO salen de la computadora: el script lee una carpeta, identifica cada
hoja y escribe un CSV. Después se completa a mano la columna ``tipo_real`` en las filas
que estén mal y se vuelve a correr con ``--medir`` para ver el acierto por tipo.

Uso (desde la raíz del repo):

    python tools/dev/integra_eval.py "C:\\Legajos\\prueba"            -> genera integra_eval.csv
    python tools/dev/integra_eval.py --medir "C:\\Legajos\\prueba\\integra_eval.csv"

Columnas del CSV: archivo, hoja, lectura (texto/ocr/vacia), tipo_detectado, nombre,
confianza, puntaje, coincidencias, alternativas, tipo_real (vacía = "está bien").
Para mejorar el catálogo alcanza con compartir las columnas tipo_detectado, tipo_real
y coincidencias; nunca hace falta compartir el contenido de los documentos.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.engine.integra.classifier import classify_pages, load_catalog  # noqa: E402
from app.engine.integra.text_service import extract_page_text, find_tessdata_dir, open_as_pdf  # noqa: E402

EXTENSIONES = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
COLUMNAS = [
    "archivo",
    "hoja",
    "lectura",
    "tipo_detectado",
    "nombre",
    "confianza",
    "puntaje",
    "coincidencias",
    "alternativas",
    "tipo_real",
]


def generar(carpeta: Path, salida: Path) -> None:
    tessdata = find_tessdata_dir()
    if tessdata is None:
        print("AVISO: no se encontró tools/tessdata/spa.traineddata; los escaneos no se van a leer.")
    archivos = sorted(p for p in carpeta.rglob("*") if p.suffix.lower() in EXTENSIONES)
    if not archivos:
        sys.exit(f"No hay PDF ni imágenes en {carpeta}")
    filas = []
    inicio = time.monotonic()
    for archivo in archivos:
        rel = str(archivo.relative_to(carpeta))
        try:
            doc = open_as_pdf(archivo)
        except Exception as exc:  # archivo dañado
            print(f"  ! {rel}: {exc}")
            continue
        with doc:
            textos = [extract_page_text(page, tessdata) for page in doc]
        paginas = [{"text": t.text, "source_document_id": rel} for t in textos]
        for idx, (texto, cls) in enumerate(zip(textos, classify_pages(paginas), strict=True)):
            filas.append(
                {
                    "archivo": rel,
                    "hoja": idx + 1,
                    "lectura": texto.source,
                    "tipo_detectado": cls.type_id,
                    "nombre": cls.nombre,
                    "confianza": cls.confianza,
                    "puntaje": cls.score,
                    "coincidencias": " | ".join(cls.coincidencias),
                    "alternativas": " | ".join(f"{a['type_id']}:{a['score']}" for a in cls.alternativas),
                    "tipo_real": "",
                }
            )
        print(f"  {rel}: {len(textos)} hoja(s)")
    with salida.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNAS, delimiter=";")
        writer.writeheader()
        writer.writerows(filas)
    conf = Counter(f["confianza"] for f in filas)
    print(f"\n{len(filas)} hojas en {time.monotonic() - inicio:.0f} s. Confianza: {dict(conf)}")
    print(f"CSV: {salida}\nCompletá 'tipo_real' solo donde esté mal y corré con --medir.")
    print("Tipos válidos: " + ", ".join(t.id for t in load_catalog().tipos) + ", desconocido")


def medir(csv_path: Path) -> None:
    with csv_path.open(encoding="utf-8-sig") as fh:
        filas = list(csv.DictReader(fh, delimiter=";"))
    por_tipo: dict[str, Counter] = defaultdict(Counter)
    errores = Counter()
    for fila in filas:
        real = (fila.get("tipo_real") or "").strip() or fila["tipo_detectado"]
        ok = real == fila["tipo_detectado"]
        por_tipo[real]["total"] += 1
        por_tipo[real]["ok"] += ok
        if not ok:
            errores[(real, fila["tipo_detectado"])] += 1
    total = sum(c["total"] for c in por_tipo.values())
    aciertos = sum(c["ok"] for c in por_tipo.values())
    print(f"Acierto general: {aciertos}/{total} ({aciertos * 100 / max(total, 1):.1f} %)\n")
    print(f"{'tipo real':40} {'acierto':>10}")
    for tipo, c in sorted(por_tipo.items(), key=lambda kv: kv[1]["ok"] / kv[1]["total"]):
        print(f"{tipo:40} {c['ok']:>4}/{c['total']:<4} ({c['ok'] * 100 / c['total']:.0f} %)")
    if errores:
        print("\nConfusiones más frecuentes (real -> detectado):")
        for (real, det), n in errores.most_common(15):
            print(f"  {real} -> {det}: {n}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("ruta", type=Path, help="Carpeta con legajos, o el CSV con --medir")
    parser.add_argument("--medir", action="store_true", help="Medir acierto de un CSV ya revisado")
    parser.add_argument("--salida", type=Path, default=None, help="Ruta del CSV a generar")
    args = parser.parse_args()
    if args.medir:
        medir(args.ruta)
    else:
        generar(args.ruta, args.salida or (args.ruta / "integra_eval.csv"))


if __name__ == "__main__":
    main()
