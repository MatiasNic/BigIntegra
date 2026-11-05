# Checklist de pruebas antes de distribuir un `.exe`

Unos 10 minutos. Hacerlo con el **`.exe` compilado** (no con `python main.py`), en una
PC del sector, antes de pasarlo a los compañeros. Marcá cada punto; si algo falla,
no distribuir y avisar (adjuntar `%LOCALAPPDATA%\BigPDF\logs\bigpdf.log`).

Tené a mano: un PDF grande (100+ páginas), un escaneo, una imagen JPG y, si hay
LibreOffice, un Word.

## Arranque
- [ ] Doble clic en el `.exe`: el logo aparece enseguida (mientras se prepara), después
      la ventana "Verificando acceso…" en el mismo lugar, y luego la app.
- [ ] Clic derecho → Propiedades → Detalles: dice "Big PDF" y la versión correcta.
- [ ] El antivirus no lo bloquea ni lo pone en cuarentena.
- [ ] Doble clic otra vez con la app abierta: no abre otra, trae al frente la existente.

## Importar y ver
- [ ] Botón para seleccionar archivos: importa el PDF grande, la imagen y el Word.
- [ ] Arrastrar un PDF desde el Explorador de Windows: se importa.
- [ ] Arrastrar una **carpeta** con PDFs: se importan sus archivos.
- [ ] Scrollear hasta el final y volver: las miniaturas no se ponen grises.
- [ ] Vista "Archivos": se ven las miniaturas de cada archivo.
- [ ] Doble clic en una página: se abre el visor; flechas cambian de página; Esc cierra.
- [ ] En el visor, la rueda del mouse hace zoom hacia el puntero y no baja del 100 %.
- [ ] Con zoom puesto, pasar a la página siguiente con la flecha: vuelve al 100 %.

## Organizar
- [ ] Rotar una página (y un escaneo que ya venía girado): la miniatura gira bien.
- [ ] Eliminar una página y restaurarla con clic.
- [ ] Reordenar arrastrando una página y un bloque (vista Archivos).
- [ ] Ctrl+Z deshace el último cambio; Ctrl+Y lo rehace.

## Exportar
- [ ] "Descargar PDF" sin compresión: se guarda en Descargas y se abre bien, con las
      rotaciones y el orden correctos.
- [ ] Exportar con compresión: el archivo sale más chico y se ve bien.
- [ ] Comprimir un escaneo en blanco y negro / grises: abrir el resultado y revisar
      TODAS las páginas (ninguna desplazada, ampliada ni con líneas repetidas).
- [ ] Modo Separar con 2 cortes → "ZIP": el ZIP trae las partes con sus nombres.
- [ ] Separar: nombrar las partes, eliminar todas las hojas de la parte 1 y arrastrar la
      primera hoja de otra parte: cada nombre sigue en su parte (panel y grilla iguales).
- [ ] Guardar con un nombre con "/" o ":" (p. ej. "Expte 12/2024: final"): se guarda
      como "Expte 12-2024 - final.pdf" y el aviso muestra ese nombre.
- [ ] Exportar un PDF con índice (marcadores): el exportado conserva el índice y los
      enlaces internos llevan a la página correcta.
- [ ] Comprimir un PDF con formulario completado: los campos siguen con sus valores.
- [ ] Cancelar una exportación larga: se cancela y la app sigue usable.
- [ ] Cerrar la ventana durante una exportación: pide confirmación.

## Sin internet
- [ ] Con la PC desconectada: la app abre y avisa cuántos días de uso sin conexión quedan.
- [ ] (Opcional) Con `diasGracia = 0` en la planilla y sin conexión: no abre y lo explica.

## Cierre
- [ ] Cerrar la app: en `%TEMP%` no quedan carpetas `_MEI...` ni `BigPDF\<código>` de esta sesión.
