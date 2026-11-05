# Big Integra — contexto para Claude Code

Este archivo se carga automáticamente por Claude Code al abrir el proyecto. Contiene
lo que necesitás saber antes de tocar código: qué es esto, cómo está armado, qué
convenciones seguir, y qué deuda técnica es consciente (no la "arregles" de sorpresa).

Ver también `docs/informe-organizacion.md` para el análisis completo y el backlog de
mejoras propuestas.

## Qué es

Big Integra = BigPDF (app de escritorio Windows, PySide6 + QWebEngineView, para
importar, organizar, separar, comprimir y exportar PDFs) **+ el panel Integra**, que
identifica cada hoja de un legajo de Discapacidad, arma el checklist de requisitos por
prestación, aplica controles exactos y propone el orden de armado. **Sin IA**: reglas +
OCR local (Tesseract integrado en PyMuPDF; idioma en `tools/tessdata/spa.traineddata`).
No hay IA aprobada en la empresa: no agregar llamadas a modelos ni servicios externos.

La interfaz es HTML/CSS/JS local dentro de un `QWebEngineView` que habla con Python vía
`QWebChannel` (`app/desktop/bridge.py`). **No hay servidor HTTP**: no FastAPI, no
Uvicorn, no puertos. Si ves "rutas", "server" o "API REST" en comentarios, son
remanentes de una versión web vieja.

### Integra (`app/engine/integra/`)

- `catalog/document_types.json`: tipos de documento. Señales con peso: `titulos`
  (título impreso del formulario oficial), `pies` (pie de página "Tratamientos de
  rehabilitación 2"), `campos`, `palabras`, `excluye`. `instructivo` es `prioritario`.
- `catalog/prestaciones.json`: requisitos generales y por prestación (con `cuando` según
  condiciones), `orden_armado`. Fuente: instructivos 2024 (ver
  `tests/fixtures/formularios_2024/`). Escolaridad Especial, MAIE y Maestra de Apoyo son
  `provisoria` (reglas del flujo de Power Automate anterior) hasta tener instructivo.
- `text_service.py` (capa de texto u OCR) → `classifier.py` → `extractors.py` →
  `rules.py` → `analyzer.py` (checklist con el esquema de los viejos subagentes:
  correcto / aModificar / faltante / observaciones) → `ordering.py` / `report.py`.
- `service.py`: punto de entrada del bridge; cachea el texto por (documento, hoja) en
  memoria para re-analizar al instante (corregir etiqueta, cambiar prestaciones).
- Bridge: `integraCatalog` (sync) e `integraAnalyze` (job async cancelable).
- UI: `app/ui/web/tools/integra-panel.js` (panel, etiquetas `.pg-doc-label` pintadas
  con un MutationObserver sin tocar `page-grid.js`, corrección manual por `<select>`),
  `WorkspaceState.applyProposedOrder` (con historial: Ctrl+Z).
- Regla de oro: ante una duda de lectura, **nunca** marcar "falta"; usar "revisar /
  verificar en documento original". Firmas y sellos siempre quedan para el humano.
- `tools/dev/integra_eval.py`: calibración con legajos reales en la PC del dueño (CSV
  con tipo detectado / tipo real). Los documentos reales nunca se suben al repo.

## Cómo correrla

```bash
pip install -r requirements.txt
python main.py
```

Necesita entorno gráfico real (Windows recomendado). Valida el acceso al arrancar
contra un Google Apps Script (`app/core/auth/access_control.py`). Sin internet,
funciona solo durante `diasGracia` días (columna de la planilla) desde la última
validación online; el caché está firmado (HMAC ligado al equipo), detecta reloj
atrasado y se borra si la planilla deniega el acceso. Arranque rápido: si validó
online hace < 24 h (`RECENT_VALIDATION_HOURS`), la ventana se muestra sin esperar la
planilla y la verificación corre por detrás (un "denegado" cierra la app); si no, la
ventana se arma en paralelo y se muestra al llegar el OK. Logs en
`%LOCALAPPDATA%\BigIntegra\logs\bigpdf.log`.
Ghostscript y LibreOffice son dependencias externas **opcionales** — sin ellas la app
sigue funcionando con degradación elegante (compresión vía PyMuPDF en vez de
Ghostscript; conversión de Office deshabilitada sin LibreOffice).

## Mapa de arquitectura

```
main.py                    Entry point. Ventana PySide6 + QWebEngineView.
                            IMPORTANTE: setea os.environ ANTES de importar PySide6
                            (por eso hay imports "fuera de orden" — es intencional).

app/core/auth/              Validación de acceso al arrancar (Google Apps Script).
                            URL ofuscada con XOR+base64 en auth_settings.py — NO es
                            cifrado real, solo evita texto plano a simple vista.

app/desktop/                Capa de integración con Qt.
  bridge.py                   Expone métodos Python a JS vía QWebChannel (@Slot).
                               Todo entra/sale como JSON string.
  workers.py                   JobManager: corre trabajo pesado en QThreadPool,
                               nunca en el hilo Qt (freeza la UI si no).
  export_process*.py          Exportar workspace corre en un PROCESO aparte
                               (no solo thread) para que Ghostscript/PyMuPDF no
                               compitan por CPU/memoria con QtWebEngine, y para
                               poder cancelarlo matando el proceso.

app/engine/                 Lógica de PDF pura. SIN dependencia de PySide6 —
                             por eso es la única capa con tests automatizados
                             (ver tests/server/). Antes se llamaba app/server/;
                             se renombró porque no es un servidor.
  services/compress_service.py    Motor de compresión: prueba varias estrategias
                                   (estructural, recompresión de imágenes,
                                   Ghostscript, rasterizado como último recurso)
                                   y elige el mejor resultado.
  services/conversion_service.py  Convierte imagen/Office → PDF.
  services/ghostscript_service.py Wrapper de Ghostscript con timeout y cancelación.
  services/workspace_export_service.py  Arma el PDF final página por página + ZIP.
  models/document_model.py        Modelos pydantic (validación de requests).

app/ui/web/                 Frontend: HTML/CSS/JS vanilla + pdf.js/jszip vendorizados.
                             Sin build step (no bundler, no npm). Se sirve como
                             archivo local (file://), no por HTTP.

packaging/                  BigIntegra_Desktop.spec (PyInstaller) + build_exe.bat.
                             SIEMPRE un único .exe (onefile), sin instalador:
                             las PCs del sector no permiten instalar programas.
                             El .exe se compila en GitHub Actions SOLO si el
                             dueño lo pide explícitamente (tag build-* o vX.Y.Z);
                             los minutos de GitHub son limitados. Él prueba cada
                             cambio desde VS Code con `python main.py`.
                             splash.png: imagen que el .exe muestra MIENTRAS se
                             descomprime (PyInstaller Splash, usa Tcl/Tk solo ahí);
                             main.py la cierra al mostrar la ventana de Qt. Se
                             regenera con tools/dev/make_splash.py.
tools/dev/                  Scripts de desarrollo (encode_auth_url.py).
tests/server/                Tests de app/engine (no requieren GUI).
tests/core/                  Tests del control de acceso (no requieren GUI).
tests/desktop/               Tests del bridge con Qt offscreen (importar, comprimir,
                             cancelar, exportar en proceso aparte, guardar). Se
                             saltean solos si no hay PySide6 (así corre el CI).
```

## Convenciones del proyecto

- **Versión única**: `app/__init__.py::__version__`. Todo lo demás la importa,
  incluido el `.spec` (nombre del `.exe` y VERSIONINFO) y `pyproject.toml`. No
  hardcodear el número de versión en otro lugar.
- **Cada tanda de cambios entregada = versión nueva** subiendo el último dígito
  (1.4.3 → 1.4.4 → …), con su entrada en `CHANGELOG.md`. Pedido explícito del dueño:
  no dejar cambios bajo el mismo número. Un test verifica que la versión de
  `app/__init__.py` tenga su sección en el CHANGELOG.
- **Dos carpetas de datos**: la sesión temporal (`%TEMP%\BigIntegra\<uuid>`, documentos
  del usuario, se borra al cerrar; protegida con `.session.lock` para que otra
  instancia no la purgue) y `APP_DATA_DIR` (`%LOCALAPPDATA%\BigIntegra`, persistente:
  caché de acceso firmado, `logs/`, `prefs.json`). Nunca guardar documentos del
  usuario en `APP_DATA_DIR`.
- **Rotación de páginas = delta** sobre el `/Rotate` propio de la página, igual en la
  UI (pdf.js: `pdfPage.rotate + page.rotation`) y en el motor
  (`workspace_export_service.build_pdf`). Si cambiás una capa, cambiá la otra.
- **Instancia única** (`QLocalServer` en `main.py`): abrir la app de nuevo trae al
  frente la ventana existente.
- **`app/engine/` no debe importar PySide6.** Es lo que permite testear la lógica de
  PDF sin GUI. Si agregás algo ahí que necesite Qt, probablemente pertenece a
  `app/desktop/` en su lugar.
- **Todo lo pesado va a un worker o proceso**, nunca al hilo principal de Qt
  (`bridge.py` usa `JobManager`/`ExportProcessManager` para esto). Bloquear el hilo Qt
  congela la ventana.
- **Comunicación bridge↔JS es JSON string**, no objetos Qt directos. Ver el patrón
  `_json_ok` / `_json_error` en `bridge.py`.
- Comentarios y mensajes de usuario están en español; identificadores en inglés.
  Mantené esa mezcla, no la "normalices" a un solo idioma.

## Deuda técnica conocida (no es un bug que se te escapó, ya está mapeada)

- `pyproject.toml` solo ignora `E402` en `main.py` (intencional: env vars antes de
  importar PySide6). El resto de la deuda de lint ya se limpió; no agregar
  `per-file-ignores` nuevos, corregir el código.
- `page-grid.js` y `organize.js` siguen siendo los archivos más grandes de la UI;
  se dividieron en módulos (ver `app/ui/web/components/`), seguir separando por tema
  cuando se toquen.
- Algunos docstrings todavía mencionan "rutas", "workspace_routes.py" o
  `run_in_threadpool` — vocabulario de la época en que esto era una app web. No
  afecta funcionalidad, solo puede confundir al leer.
- La ofuscación de la URL de acceso (XOR+base64) no es seguridad real. Aceptable
  mientras el repo sea privado (decisión tomada). **Si alguna vez se hace público**,
  antes: (1) cambiar la URL del Apps Script (publicar uno nuevo; la vieja quedó en el
  historial de git), (2) sacarla de `auth_settings.py` y leerla de `BIGPDF_ACCESS_URL`
  o de un secreto de GitHub Actions inyectado al compilar, (3) cambiar la clave
  `ACCESS_SOURCE_XOR_KEY`, que también firma el caché de acceso sin conexión.

## Flujo de trabajo

- **Una rama (y sesión) por tema** (`claude/<tema>`): un cambio se prueba y se fusiona
  por separado, y si sale mal se deshace solo ese.
- **El dueño prueba siempre desde `main`** (`Git: Pull` en `main` + F5 / `python main.py`).
  Por eso, cuando un cambio está terminado y validado (ruff + pytest + prueba real si
  aplica), se fusiona a `main` por Pull Request sin esperar a que él lo pruebe en la
  rama. Si algo sale mal, se corrige con un cambio nuevo (nunca reescribir `main`).
- **Al analista no se le agregan pasos ni funciones nuevas**: su uso es importar,
  organizar y guardar/descargar/comprimir. Toda validación o corrección tiene que
  hacerla el programa solo, sin cambiar el flujo ni los tiempos perceptibles.
- Todo cambio visible para el usuario se anota en `CHANGELOG.md` → "Sin publicar".
- Antes de distribuir un `.exe`: `docs/checklist-pruebas.md`.
- Antes de cualquier push: `ruff check .` y `pytest` (incluye `tests/desktop` si hay
  PySide6 instalado).

## Qué NO hacer sin preguntar primero

- No proponer ni pasar a onedir, instalador (Inno Setup/MSI) ni nada que requiera
  instalar: la distribución es un único `.exe` portátil (decisión del dueño del
  proyecto; en las PCs de trabajo no se pueden instalar aplicaciones). Las mejoras
  de arranque tienen que ser dentro de onefile (achicar el bundle, splash, etc.).

- No renombrar `app/engine/` ni tocar su independencia de PySide6 sin avisar — es la
  base de que haya tests automatizados.
- No "arreglar" los lint-ignores de `pyproject.toml` tocando lógica en
  `app/desktop/bridge.py` o `ghostscript_service.py` sin correr la app real primero:
  son áreas sensibles a temas de threading/timing que no se detectan con tests.
- No agregar un servidor HTTP, FastAPI, ni abrir puertos — es una decisión de diseño
  explícita del proyecto (ver docstring de `main.py`), no un descuido a corregir.
