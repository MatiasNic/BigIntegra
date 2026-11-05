# Análisis de mejoras — BigPDF Desktop 1.4.2

Fecha: 2026-10-16 · Alcance: revisión completa del código (motor PDF, capa de
escritorio Qt, interfaz JS). No se modificó código; este documento es el backlog.

**Leyenda**
- **VERIFICADO**: reproducido ejecutando código o con camino claro leído en el código.
- **PROBABLE**: razonado, hay que confirmarlo en Windows real.
- Esfuerzo: 🟢 bajo (minutos/horas) · 🟡 medio (1–2 días) · 🔴 alto.

Complementa a `docs/informe-organizacion.md` (que trata la organización del repo).

> **Estado (2026-10-16):** implementado todo el backlog salvo lo listado abajo, en la
> rama `claude/gallant-curie-4i9zuv`. Pendiente a propósito:
> - Ghostscript en paralelo con PyMuPDF (1.8): hay que medirlo en notebooks de 2 núcleos.
> - Bajar la resolución de GS "high" (1.8): decisión de calidad, requiere probar con GS real.
> - ~~Pasar a onedir + instalador~~: descartado. La app se distribuye SIEMPRE como un
>   único `.exe` (en las PCs de trabajo no se puede instalar nada). En su lugar se
>   achicó el bundle (300 → 228 MB) para que descomprima más rápido en cada arranque.
>   Firmar el `.exe` sigue pendiente (requiere certificado de la empresa).
> - Límite del drop por base64 del lado Python (2.8): el JS ya muestra el error real.
> - Virtualizar la grilla y unificar `_setupDnD`/`_setupFilesDnD` (3.6, 3.12): riesgo alto.
> - Cargar QtWebEngine en paralelo con la verificación de acceso (2.3): hoy se muestra la
>   ventana "Verificando acceso…" y la interfaz se crea después.

---

## 0. Resumen: lo primero que conviene arreglar

Arreglos chicos con mucho impacto, en el orden sugerido:

| # | Problema | Impacto para el usuario | Esfuerzo |
|---|---|---|---|
| 1 | Exportar duplica fuentes/imágenes en cada página | PDFs exportados hasta 25–60× más grandes y lentos; % de ahorro falso | 🟢 1 línea |
| 2 | Sin internet la app nunca abre (el caché de acceso se borra en cada arranque) | El "período de gracia" offline no funciona nunca | 🟢 |
| 3 | Abrir la app dos veces borra los archivos de la primera ventana | Exportaciones fallidas o PDFs con páginas faltantes | 🟢/🟡 |
| 4 | La vista "Archivos" nunca muestra miniaturas | Tarjetas vacías siempre | 🟢 1 línea |
| 5 | El mismo PDF se carga hasta 7 veces en la interfaz | Mucho consumo de RAM, posibles cierres con PDFs grandes | 🟢 1 línea |
| 6 | Ghostscript se puede trabar si emite muchos avisos | Esperas de 3 a 15 minutos para nada con PDFs dañados | 🟢 |
| 7 | Los logs nunca se activan (y se borrarían al cerrar) | Imposible diagnosticar un error reportado por un usuario | 🟢 |
| 8 | Rotación inconsistente entre miniatura y PDF exportado | Escaneos se exportan girados distinto a lo que se ve | 🟡 |
| 9 | El arranque se bloquea hasta 20–40 s sin mostrar nada | El usuario cree que no abrió y hace doble clic (dispara #3) | 🟡 |
| 10 | No hay deshacer (Ctrl+Z) | Es fácil perder trabajo | 🟡 |

---

## 1. Motor de PDF (`app/engine/`)

### 1.1 La exportación duplica recursos compartidos — VERIFICADO 🟢
- **Dónde:** `app/engine/services/workspace_export_service.py:88`
  `dst.insert_pdf(src, from_page=pn, to_page=pn)`.
- **Qué pasa:** `insert_pdf` usa por defecto `final=True`, que descarta el registro de
  objetos ya copiados después de cada llamada. Como se copia página por página, la fuente
  embebida, el logo o el fondo compartido se copian **una vez por página**.
- **Medido:** 200 páginas con una fuente embebida: 461 KB → **79 MB**. 600 páginas con un
  logo: 2,5 MB → **162 MB** en 2,45 s (con `final=False`: 2,5 MB en 0,11 s). Reproducido
  también con un caso propio: 18 KB → 491 KB.
- **Efecto extra:** si hay compresión, `garbage=4` recupera el tamaño final, pero el
  `size_before` queda inflado y la interfaz muestra un ahorro falso ("99,5 %").
- **Arreglo:** `final=False` en el bucle (los mapas se liberan al cerrar `dst`).

### 1.2 Ghostscript se traba si escribe mucho en stderr — VERIFICADO (mecanismo) 🟢
- **Dónde:** `ghostscript_service.py:234-274`. Se lanza con `stdout=PIPE, stderr=PIPE` y
  solo se hace `poll()`; los pipes se leen recién al final. Cuando el buffer se llena
  (≈4 KB en Windows), GS se bloquea hasta el timeout.
- **Escenario:** un PDF dañado que genere ~100 líneas de `**** Error: ... repairing` hace
  esperar 180 s (900 s en lote grande) y el resultado se descarta.
- **Arreglo:** `stdout=DEVNULL` y `stderr` a un archivo temporal.

### 1.3 Rotación: la UI y el motor la interpretan distinto — VERIFICADO 🟡
- **Dónde:** `workspace_export_service.py:89-90` vs `page-grid.js:1295` y `page-viewer.js:179`.
- **Qué pasa:** pdf.js usa la rotación como **absoluta** (ignora el `/Rotate` propio de la
  página); el motor la **pisa** solo si no es 0. Con una página que trae `/Rotate 90`
  (muy común en escáneres):

  | Rotación en la UI | `/Rotate` exportado |
  |---|---|
  | 0 | 90 |
  | 90 | 90 |
  | 180 | 180 |
  | 270 | 270 |

  La miniatura se ve de costado pero se exporta derecha; si el usuario la "corrige", sale al revés.
- **Arreglo:** tratar la rotación como delta en las dos capas:
  UI `rotation: (pdfPage.rotate + page.rotation) % 360`, motor
  `set_rotation((src[pn].rotation + pc.rotation) % 360)`.

### 1.4 Páginas descartadas en silencio al exportar — VERIFICADO 🟢
- **Dónde:** `workspace_export_service.py:81-83` (`continue` si falta la fuente) y `:87`
  (página fuera de rango).
- Se pidieron 3 páginas y salió 1, sin aviso. Además `page_num=-1` pasa la validación y
  `insert_pdf(from_page=-1, ...)` inserta **el documento entero**.
- **Arreglo:** fallar o devolver advertencias a la UI; `page_num: int = Field(ge=0)` en el modelo.

### 1.5 Conversión de Office con rutas con espacios/acentos — VERIFICADO (Linux) 🟢
- **Dónde:** `conversion_service.py:102-112`. `f"-env:UserInstallation=file:///{...as_posix()}"`
  sin codificar: con un espacio en la ruta, LibreOffice se colgó hasta el timeout; con
  `Path.as_uri()` funcionó.
- Escenario: usuario `C:\Users\Juan Pérez\...`.
- `subprocess.TimeoutExpired` no se captura (el usuario ve un mensaje técnico en inglés) y
  en Windows puede quedar `soffice.bin` vivo (PROBABLE). El perfil se crea en `%TEMP%`
  fuera de la sesión y nunca se limpia si la app muere.
- **Arreglo:** `profile_dir.as_uri()`, capturar el timeout con mensaje en español, matar el
  árbol de procesos, crear el perfil dentro de `WORKING_DIR`.

### 1.6 La recompresión pasa capturas y diagramas a JPEG — VERIFICADO 🟢
- **Dónde:** `compress_service.py:311-321`, `rewrite(..., lossless=True, ...)`.
- Imágenes sin pérdida (capturas de pantalla, planos) se convierten a JPEG → artefactos
  alrededor del texto.
- **Arreglo:** `lossless=False` en el perfil `mixed`; JPEG solo para escaneos/fotos. Medir
  cuánto ahorro se pierde.

### 1.7 Temporales que quedan al cancelar o fallar — VERIFICADO 🟢
- **Dónde:** `compress_service.py:402-553` (limpieza al final, no en `finally`) y
  `workspace_export_service.py:137-173`.
- Al cancelar quedan `*.structural.tmp.pdf`, `*.images_q65.tmp.pdf`, `tmp_*.pdf`. Con lotes
  grandes puede llenar `%TEMP%` hasta que se cierre la app.
- **Arreglo:** `try/finally`; y que el proceso padre borre los temporales del job tras matar al hijo.

### 1.8 Rendimiento del pipeline de compresión — VERIFICADO en parte 🟢/🟡
- Recomprimir un PDF ya comprimido corre la recompresión completa (9,4 s) para terminar en
  "sin reducción útil". Se puede saltear si `image_bytes_ratio < ~0,1`. VERIFICADO.
- Ghostscript corre después de PyMuPDF; al ser otro proceso podría correr en paralelo.
  PROBABLE, hay que medirlo en notebooks de 2 núcleos.
- `_xref_stream_length` (`compress_service.py:111-119`) devuelve 0 cuando `/Length` es una
  referencia indirecta → escaneos con OCR se clasifican mal (q70 en vez de q65 y GS corre de
  más). VERIFICADO. Arreglo: resolver la referencia con `doc.xref_object(...)`.
- GS en nivel "high" baja a 130 dpi: puede dejar borroso el texto chico de escaneos a
  300 dpi, y contradice el docstring "sin reducir resolución". PROBABLE (no hay GS para probar).

### 1.9 Compresión individual sin progreso ni cancelación — VERIFICADO 🟡
- `bridge.py:584` llama `compress_pdf(src, level)` sin `progress_fn` ni `cancel_check`.
  `rewrite_images` tardó 8 s en 60 páginas (≈70 s en 500) con la barra fija en 43 %.
  `cancelJob` no la cancela.
- **Arreglo:** pasar `progress_fn`/`cancel_check` desde el bridge y emitir un "latido" con
  tiempo transcurrido durante `rewrite_images`, como ya se hace con GS.

### 1.10 Menores — VERIFICADO salvo indicación 🟢
- `conversion_service.py:146, 184-187`: rechaza PDFs válidos con bytes antes de `%PDF`
  (BOM/CRLF). Buscar `%PDF` en los primeros 1024 bytes.
- `bridge.py:379-397`: si la importación se rechaza por límites (más de 1000 páginas,
  contraseña), el PDF ya movido a `uploads/` queda ocupando espacio.
- `workspace_export_service.py:218`: ZIP con `ZIP_DEFLATED` sobre PDFs que ya están
  comprimidos (150 MB: 5,3 s vs 0,34 s con `ZIP_STORED`, mismo tamaño).
- `build_zip`: `Informe.pdf` e `informe.pdf` chocan al extraer en Windows.
- PDF con contraseña de propietario se exporta **sin** restricciones; es una decisión que
  conviene tomar a conciencia y avisar.
- `sanitize_filename` no contempla nombres reservados de Windows (`CON`, `NUL`…). PROBABLE.
- Código muerto: fallback de rasterizado (`compress_service.py:497-521`, `583-603`),
  inalcanzable con PyMuPDF ≥ 1.26.5; `prune_older_than`; `get_page_count`.

---

## 2. Capa de escritorio (`main.py`, `app/desktop/`, `app/core/auth/`)

### 2.1 Sin internet la app nunca abre — VERIFICADO 🟢
- **Dónde:** `access_control.py:35` `_CACHE_PATH = Path(STORAGE_DIR) / "access_cache.json"`.
- `STORAGE_DIR` es `%TEMP%\BigPDF\<uuid nuevo en cada arranque>` y las sesiones previas se
  borran al iniciar (`config.py:17-30`). El caché de la última validación **nunca** se
  encuentra en el siguiente arranque, así que `diasGracia` no sirve: sin red o con el Apps
  Script caído, la app siempre queda bloqueada.
- **Arreglo:** guardar el caché en una ruta persistente (`%LOCALAPPDATA%\BigPDF\`).

### 2.2 Dos instancias: la segunda borra la sesión de la primera — VERIFICADO 🟢/🟡
- **Dónde:** `config.py:17-30, 85-90`. No hay control de instancia única.
- La ventana A falla con "Archivos fuente no encontrados", o peor, si estaba exportando,
  genera un PDF con páginas faltantes sin aviso (ver 1.4).
- **Arreglo:** instancia única con `QLocalServer` (la segunda trae al frente la primera y
  sale), o `QLockFile` por sesión y purgar solo sesiones sin lock.

### 2.3 Arranque bloqueante y mensajes de acceso confusos — VERIFICADO 🟡
- **Dónde:** `main.py:486-500`, `auth_settings.py:19`, `access_control.py:105-186`.
- El chequeo corre en el hilo principal **antes de mostrar cualquier ventana**. El timeout
  de 20 s es por operación (no total) y el redirect de Apps Script suma otro: con wifi
  inestable pueden pasar 20–40 s sin nada en pantalla.
- Cualquier excepción (incluida una página HTML de error de Google) termina en "Sin
  conexión…"; el título dice siempre "Acceso denegado"; no hay "Reintentar".
- `access_control.py:186` `int(data.get("diasGracia") or 0)` está fuera del `try`: si la
  planilla devuelve texto ("7 días"), la app se cierra con un traceback.
- **Arreglo:** ventana/splash inmediata "Verificando acceso…", chequeo en un hilo en
  paralelo con la carga de QtWebEngine, timeout total (~8 s), mensajes distintos para "sin
  red" / "respuesta inválida" / "acceso denegado", botón "Reintentar".

### 2.4 El logging nunca se activa — VERIFICADO 🟢
- `logging_config.setup_logging()` no se llama en ningún lado; todos los `logger.info/warning`
  se pierden. Además `LOGS_DIR` está dentro de la sesión temporal, así que se borraría.
- **Arreglo:** llamarlo en `main()` y mover los logs a `%LOCALAPPDATA%\BigPDF\logs`
  (el `RotatingFileHandler` ya está armado). Fundamental para dar soporte.

### 2.5 Carrera entre `submit()` y `connect()` — VERIFICADO (probabilidad baja) 🟢
- **Dónde:** `bridge.py:442-443, 510-511, 594-595`. El worker arranca antes de conectar
  `finished`; si termina muy rápido, la señal se pierde y la UI queda con el overlay
  hasta el timeout de 10 minutos. La importación ya usa el patrón correcto
  (`create_job` → `connect` → `start_job`).
- **Arreglo:** usar ese mismo patrón en los tres lugares.

### 2.6 Cerrar la app con trabajo en curso — VERIFICADO 🟢/🟡
- `main.py:444-460` cierra sin preguntar: una exportación de 20 minutos se pierde con un
  clic en la X, y el `taskkill` síncrono puede congelar la ventana.
- Comprimir y convertir Office no se cancelan al cerrar: siguen corriendo en segundo
  plano mientras se borra la carpeta de sesión (PROBABLE: proceso invisible un rato y
  posible crash al salir).
- Si la app crashea o se mata desde el Administrador de tareas, el proceso de
  exportación (y su Ghostscript) queda huérfano (`export_process_manager.py:112-117`).
- **Arreglo:** confirmar al cerrar si hay jobs o documentos; evento de cancelación por job
  en `JobManager` y `waitForDone` antes de borrar; Job Object de Windows con
  `KILL_ON_JOB_CLOSE` o `--parent-pid` en el hijo.

### 2.7 Operaciones simultáneas fallan a los 8 s — VERIFICADO 🟢
- `concurrency.py:20`: "**El servidor** está procesando otra operación pesada…". Pasa si se
  comprime mientras se importan muchos archivos.
- **Arreglo:** mensaje de escritorio y encolar en vez de fallar (o deshabilitar acciones en la UI).

### 2.8 Otros — VERIFICADO salvo indicación
- **Exportación reportada como fallida siendo exitosa** (PROBABLE, poco frecuente):
  `export_process_manager.py:144-161` marca como leída una línea incompleta del archivo de
  eventos. Arreglo: avanzar solo sobre líneas terminadas en `\n`. 🟢
- **Cancelar una exportación puede borrar temporales de una compresión en curso**
  (PROBABLE): ambos usan los mismos patrones en `EXPORTS_DIR`. Arreglo: subcarpeta por job. 🟢
- **HiDPI** (PROBABLE): `main.py:466-473` setea `QT_ENABLE_HIGHDPI_SCALING=0` y
  `QT_SCALE_FACTOR=1`, lo opuesto a lo que dice el comentario; en pantallas al 125–150 %
  la interfaz se vería chica. Verificar en una notebook. 🟢
- **`importFileData` con base64 grande** se parsea en el hilo de Qt (congela segundos con
  ~90 MB); el límite se evalúa tarde. Solo afecta drops que no son archivos locales. 🟡
- **Diálogos de archivo** siempre abren en Descargas y no recuerdan la última carpeta;
  "Elegir ubicación al guardar" se pierde al reiniciar → guardar con `QSettings`. 🟢
- **Soltar una carpeta** se ignora en lugar de importar sus PDFs/imágenes. 🟢
- **Guardados rápidos con el mismo nombre** pueden pisarse; el archivo aparece a medio
  copiar en Descargas → reservar nombre y copiar a `.part` + renombrar. 🟢
- **Modo compatibilidad de GPU** solo por variable de entorno → ofrecer argumento/ajuste. 🟢
- **Event filter global** (`main.py:329`): todos los eventos de Qt pasan por Python. Impacto
  bajo; instalarlo solo en el widget de render. 🟢

### 2.9 Empaquetado (`packaging/BigPDF_Desktop.spec`) — config VERIFICADA, magnitudes PROBABLES 🟡
- Es **onefile**: en cada arranque descomprime cientos de MB de QtWebEngine en
  `%TEMP%\_MEI…` (varios segundos, más con el antivirus escaneando), en serie con el
  chequeo de red. Si la app crashea, esa carpeta queda.
- Sin `version_file` (VERSIONINFO) ni firma → patrón típico de falsos positivos y aviso de SmartScreen.
- **Arreglo:** onedir + instalador (Inno Setup), VERSIONINFO, firmar si es posible; excluir
  `tkinter`/`unittest`, filtrar `qtwebengine_locales` salvo es/en, no empaquetar `tools/`.
- Bien resuelto: `upx=False`, `console=False`.

---

## 3. Interfaz (`app/ui/web/`)

pdf.js vendorizado: **4.4.168** (no afectado por CVE-2024-4367). `api.js` ya no hace
llamadas HTTP; lo que queda de la versión web es código muerto (ver 3.12).

### 3.1 El mismo PDF se carga hasta 7 veces y nunca se libera — VERIFICADO 🟢
- **Dónde:** `components/page-grid.js:201-208`. Se guarda en caché el documento ya
  resuelto, no la promesa: las 7 miniaturas concurrentes iniciales llaman `getDocument()`
  cada una. Quedan 6 documentos huérfanos sin `.destroy()`, y probablemente un Web Worker
  con el archivo entero en memoria por cada uno.
- Con un escaneo de 400 MB puede llegar a ~2,8 GB de RAM y cerrar el renderer.
- **Arreglo:** cachear la promesa (y quitarla del Map si falla).

### 3.2 La vista "Archivos" nunca muestra miniaturas — VERIFICADO 🟢
- **Dónde:** `page-grid.js:1023-1024` `if (canvas.width > 0) return;`. Un `<canvas>` nuevo
  tiene `width = 300` por defecto (el template en la línea 994 no lo fija), así que la
  función siempre sale antes de dibujar.
- **Arreglo:** usar un flag (`thumb.dataset.rendered`).

### 3.3 Miniaturas que quedan en blanco tras un re-render — VERIFICADO 🟢/🟡
- **Dónde:** `page-grid.js:253-263, 303-309, 1252-1269`. Las tareas encoladas apuntan a la
  tarjeta vieja; tras `render()` la tarjeta nueva no se encola (está "en curso") o se
  marca como ya renderizada. Pasa al cambiar Hojas↔Archivos, agregar archivos o expandir
  bloques mientras cargan las miniaturas.
- **Arreglo:** buscar la tarjeta al ejecutar la tarea y saltearla si no está en el DOM;
  limpiar la cola en `render()`; si ya está renderizada, dibujar desde la caché.

### 3.4 Memoria con muchas páginas — VERIFICADO (cálculo) 🟡
- `page-grid.js:6`: fuerza DPR ≥ 2 incluso en pantallas 1x (~0,73 MB por miniatura). Los
  canvas fuera de la vista nunca se vacían; el LRU de 120 solo limita los bitmaps en caché.
  500 páginas recorridas ≈ 365–825 MB.
- **Arreglo:** DPR real y ancho real de la tarjeta; seguir observando y poner
  `canvas.width = 0` al salir de la vista; `pdfPage.cleanup()` tras renderizar.

### 3.5 Rotar rápido deja la miniatura mal o en error — VERIFICADO 🟢/🟡
- `page-grid.js:428-445`, `organize.js:315-326, 420-423`: un segundo render sobre el mismo
  canvas mientras el primero sigue → error de pdf.js; la miniatura puede mostrar 90° con el
  estado en 180°. Rotar 500 páginas re-renderiza las 500, incluidas las que no se ven.
- **Arreglo:** rotar con CSS `transform: rotate()` (instantáneo); si se re-renderiza, solo
  lo visible y cancelando el render anterior.

### 3.6 Cola de miniaturas sin prioridad para lo visible — VERIFICADO 🟡
- Cola FIFO: si arrastrás la barra de la página 1 a la 500, lo que tenés en pantalla se
  dibuja al final. Agregar un archivo de 1 página a un workspace de 500 reconstruye las 500
  tarjetas y re-renderiza ~380 miniaturas.
- **Arreglo:** LIFO o chequear visibilidad al sacar de la cola; cambios incrementales en
  vez de `render()` completo. (Virtualizar la grilla completa sería 🔴.)

### 3.7 Varias exportaciones a la vez — VERIFICADO 🟢
- `organize.js:782-792, 866-871, 691-694, 923-925`: cada botón solo se deshabilita a sí
  mismo. Se pueden lanzar PDF + ZIP + "Todas" en paralelo y los primeros ya no se pueden
  cancelar (el chip solo sigue el último).
- **Arreglo:** flag global `_exportInFlight` que deshabilite todos los botones de exportación.

### 3.8 Es fácil perder trabajo — VERIFICADO 🟡
- No hay deshacer/rehacer (Ctrl+Z / Ctrl+Y).
- Las páginas eliminadas no se pueden restaurar de a una (`pointer-events: none`), solo
  "Restaurar todas".
- Salir del modo Separar borra todos los cortes sin confirmar (`page-grid.js:164`).
- **Arreglo:** pila de estados `{pages, splitCuts}` en `WorkspaceState` (son objetos chicos)
  con Ctrl+Z/Ctrl+Y; clic en página eliminada para restaurarla; conservar los cortes.

### 3.9 Nombres/compresión de partes se pegan a la parte equivocada — VERIFICADO 🟢
- `organize.js:45-46, 579, 628`: se indexan por posición. Si nombrás la Parte 2 "Anexo" y
  agregás un corte antes, "Anexo" queda en otra parte. `_switchMode` limpia nombres pero
  no la compresión por parte.
- **Arreglo:** usar como clave el id de la primera página del grupo.

### 3.10 Otros bugs de la interfaz — VERIFICADO 🟢
- **Páginas restauradas no se pueden arrastrar** hasta el próximo render completo
  (`page-grid.js:784`). Arreglo: listener delegado en la grilla.
- **Visor:** doble clic en "Ver ampliado" abre dos visores superpuestos; el mensaje "No se
  pudo cargar la página" queda pegado para las siguientes; la rueda sin Ctrl siempre hace
  zoom (con trackpad recorre todos los niveles de golpe); no maneja el foco.
- **Importación:** si `importSelectedFiles` falla, el error real se tapa con otro mensaje
  (o los archivos chicos se importan dos veces); el `input` no se resetea, así que no se
  puede reintentar con el mismo archivo (`desktop-bridge.js:658-675`, `organize.js:198`).
- **Diálogo de confirmación:** Enter confirma siempre, incluso "Limpiar workspace"; no
  enfoca ningún botón (`confirm-dialog.js:33-36`).
- **Nombre de archivo insertado con `innerHTML`** (`page-grid.js:1158-1163`): casi
  imposible de explotar en Windows, pero es el único lugar sin escapar. Usar `textContent`.
- **Fuentes estándar y CMaps de pdf.js no vendorizados** (PROBABLE): en PDFs con fuentes
  CJK no embebidas el texto puede faltar en miniaturas y visor (la exportación sale bien).
- **Miniaturas borrosas en "2 por fila"**: la resolución es fija (180 px) y no se
  re-renderiza al cambiar el tamaño de grilla.

### 3.11 Accesibilidad y usabilidad — VERIFICADO 🟡
- Las tarjetas no son enfocables: sin navegación con flechas ni Enter/Espacio.
- Los botones de la tarjeta solo aparecen con `:hover` (falta `:focus-within`).
- Sin Shift+clic para seleccionar rangos; el clic simple tiene 200 ms de demora para
  distinguirlo del doble clic.
- `#toast-container` sin `aria-live`.

### 3.12 Código muerto y duplicación — VERIFICADO 🟢
- `index.html:33` carga `jszip.min.js` y no se usa; `window.PDFJS_CDN` tampoco.
- `api.js`: `upload`, `uploadImages`, `exportPdf`, `merge`, `split`, `jpgToPdf`, `compress`,
  `cleanup` y alias de status sin uso. `router.js`: rutas `/tool/*` de la versión web.
- CSS: `home.css` casi todo muerto; `viewer.css` ~19 clases sin uso.
- `_setupDnD` y `_setupFilesDnD` (`page-grid.js`) repiten casi igual unas 300 líneas.
- `confirm-dialog.js:3` todavía menciona `http://127.0.0.1:<puerto>`.

---

## 4. Lo que ya está bien (no tocar)

- Motor sin dependencia de Qt, trabajo pesado fuera del hilo de Qt, exportación en proceso
  aparte con cancelación real.
- Señales entre hilos correctamente encoladas por Qt.
- Importación por rutas (sin base64); pdf.js lee `file://` directo.
- Grilla con carga diferida (`IntersectionObserver`), caché de miniaturas, arrastre de
  varias páginas, modo Separar con descargas por parte, progreso con etapa/tiempo/cancelar.
- Atajos existentes: Ctrl+A, Supr, Esc; en el visor ←/→, +/−, 0, R/Shift+R.
- `upx=False` y `console=False` en el empaquetado.

## 5. Lo que no se pudo verificar desde la nube

Comportamiento real de Ghostscript (no está instalado en el entorno de análisis), todo lo
específico de Windows (tamaño de pipes, bloqueo de archivos, HiDPI, `soffice.bin`
huérfano) y la interfaz gráfica en ejecución. Esos puntos están marcados como PROBABLE.
