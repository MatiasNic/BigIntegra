# Informe: organización y mejora de BigPDF Desktop

Fecha: 2026-07-31 · Alcance: migración del `.7z` (versión 1.4.2) al repositorio GitHub
`MatiasNic/BigPDF`, evaluación de la organización existente y recomendaciones.

## 1. Qué es el proyecto (resumen técnico)

BigPDF es una aplicación de escritorio para Windows, hecha en Python con PySide6 +
QWebEngineView. La interfaz es HTML/CSS/JS local, sin servidor HTTP: la comunicación
entre la interfaz y la lógica de Python se hace con `QWebChannel`, un mecanismo de
Qt para exponer objetos Python a JavaScript directamente, sin red de por medio.

Funcionalmente cubre: importar PDFs/imágenes/Office, comprimir con varias estrategias
automáticas, organizar y reordenar páginas, exportar en PDF o ZIP, con progreso y
cancelación en tiempo real. El motor de compresión en particular (`compress_service.py`)
es sofisticado: analiza el contenido del PDF (texto vs. imágenes, tipo de escaneo) y
prueba varias estrategias en paralelo (limpieza estructural, recompresión de imágenes,
Ghostscript, rasterizado como último recurso), eligiendo el resultado más chico que
no rompa el documento.

## 2. Estado de la organización antes de la migración

**Lo que ya estaba bien:**
- Separación limpia entre capa Qt (`app/desktop/`) y lógica de PDF (`app/server/`,
  ahora `app/engine/`) — la segunda no dependía de PySide6, algo poco común y valioso
  en apps de escritorio (suele mezclarse todo).
- Trabajo pesado siempre fuera del hilo principal de Qt (workers/procesos), con
  cancelación real. Esto no es trivial de hacer bien y estaba resuelto correctamente.
- Sesión de trabajo aislada en carpeta temporal por ejecución, con limpieza automática
  al cerrar — evita acumular basura o dejar PDFs del usuario en carpetas persistentes.

**Lo que le faltaba para ser un repo "profesional":**
- Sin control de versiones — vivía como carpeta comprimida, sin historial de cambios,
  sin forma de volver atrás si algo se rompía, sin forma de colaborar.
- Sin tests. Cualquier cambio en el motor de compresión (el código más complejo del
  proyecto) se validaba solo probando manualmente en la app.
- Sin CI (integración continua) — nada revisaba automáticamente si un cambio rompía
  algo antes de que llegara a producción.
- Carpeta llamada `app/server/` sin servidor real — confuso para cualquiera que se
  sume al proyecto y lea el código por primera vez.
- Versión de la app duplicada manualmente en 3 archivos distintos (`main.py`,
  `auth_settings.py`, el `.spec` de PyInstaller) — riesgo de quedar desincronizada.
- Sin `.gitignore`, sin metadata de proyecto (`pyproject.toml`), sin README real.
- Un script referenciado en los comentarios (`tools/dev/encode_auth_url.py`) no
  estaba incluido en el paquete que se nos compartió.

## 3. Cambios aplicados en la migración

| Cambio | Por qué |
|---|---|
| `app/server/` → `app/engine/` | El nombre "server" implica HTTP/red, que no existe acá. Confunde más de lo que ayuda. |
| Versión única en `app/__init__.py` | Elimina la posibilidad de que 3 lugares digan versiones distintas. |
| `.spec`/`.bat` movidos a `packaging/` | Separa "cómo se distribuye" de "cómo se ejecuta". |
| `tools/dev/encode_auth_url.py` recreado | Sin él, no había forma de generar el valor ofuscado de una URL nueva. |
| `.gitignore`, `pyproject.toml`, `README.md` | Base mínima esperable en cualquier repo que se comparte o crece. |
| 12 tests unitarios en `app/engine/` | Cubre compresión, conversión y validación de modelos — la parte más compleja y más fácil de romper sin darse cuenta. |
| CI en GitHub Actions (ruff + pytest) | Cada cambio futuro se revisa automáticamente antes de mezclarse. |

Todo esto ya está en `main`, mergeado (PR #1).

## 4. Recomendaciones para seguir mejorando (no aplicadas todavía — a tu criterio)

Ordenadas por relación costo/beneficio, de mayor a menor prioridad sugerida.

### Alta prioridad

1. **Tests de integración del bridge** (`app/desktop/bridge.py`). Hoy los 12 tests
   cubren `app/engine/`, pero el punto de entrada real que usa la interfaz
   (`importSelectedFiles`, `compress`, `exportWorkspace`, etc.) no tiene cobertura.
   Es más difícil de testear porque depende de Qt, pero se puede probar con
   `pytest-qt` sin abrir ventana real. Es el código que más directamente rompe la
   experiencia de usuario si falla.

2. **Manejo de errores hacia el usuario**: varios `except Exception: pass` silenciosos
   (por ejemplo en `main.py::force_windows_taskbar_icon`, partes de `bridge.py`).
   Están puestos a propósito para que fallos cosméticos no tumben la app — razonable
   — pero convendría al menos loguearlos siempre (con `logger.debug`), para poder
   diagnosticar problemas reportados por usuarios sin tener que reproducir a ciegas.

3. **Limpiar docstrings que mencionan una arquitectura vieja** ("rutas",
   `workspace_routes.py`, `run_in_threadpool"): quedaron de una versión anterior que
   sí era una app web. No rompen nada, pero confunden a cualquiera (yo incluido, al
   principio) tratando de entender el sistema actual.

### Media prioridad

4. **Logging estructurado y persistente accesible al usuario**: `app/engine/logging_config.py`
   existe pero conviene confirmar que en un release real los logs queden en un lugar
   fácil de pedirle al usuario ("mandame la carpeta de logs") cuando reporte un bug,
   sin que tenga que buscar en `%TEMP%`.

5. **Feature flags o configuración externa para los perfiles de compresión**: los
   parámetros de Ghostscript (DPI, calidad JPEG por nivel) están hardcodeados en
   `ghostscript_service.py`. Si en el futuro querés ajustar la calidad de compresión
   sin recompilar, convendría moverlos a un archivo de configuración.

6. **Automatizar el build del `.exe`** en CI (lo dejamos afuera a propósito en esta
   primera etapa). Cuando el flujo de desarrollo esté estable, un workflow en runner
   Windows que genere el `.exe` en cada tag de versión ahorra el paso manual y
   garantiza que el build "de referencia" siempre viene del código en `main`, no de
   una copia local de alguien.

### Baja prioridad / a más largo plazo

7. **Actualizar vendored libs documentadas**: `pdf.js` y `jszip` están vendorizados
   sin indicar versión exacta en el repo. Alcanza con un comentario o `VENDOR.md` con
   versión + fecha, para saber cuándo conviene actualizarlos por seguridad.

8. **Considerar `mypy` como chequeo real (no solo configurado)**: hoy está en
   `requirements-dev.txt` y configurado en `pyproject.toml`, pero no corre en CI
   todavía. Vale la pena una vez que el volumen de type hints existente sea mayor.

9. **Revisar si `MAX_CONCURRENT_PDF_OPS = 1`** (una sola operación pesada a la vez,
   pensado para notebooks de 2 núcleos) sigue siendo el valor correcto, o si conviene
   hacerlo configurable según el hardware del usuario.

## 5. Lo que decidiste dejar para más adelante

- Repo **privado** (correcto dado que la URL de acceso, aunque ofuscada, sigue
  siendo un dato sensible).
- **Sin build automático del `.exe`** en CI por ahora — priorizamos la base de
  organización y pruebas antes de sumar esa complejidad.

## 6. Cómo seguir trabajando desde acá

A partir de ahora vas a trabajar 100% local. Este repositorio en GitHub queda como:
- **Resguardo**: si algo se rompe en tu máquina, el código está a salvo acá.
- **Historial**: cada cambio relevante que quieras conservar, se sube con un mensaje
  que explica qué y por qué — eso arma una bitácora del proyecto.
- **Contexto para Claude Code**: el archivo `CLAUDE.md` en la raíz se carga
  automáticamente cuando abrís este proyecto con Claude Code en tu VS Code, así que
  no hace falta que reexpliques la arquitectura cada vez — ya la tiene.

Cuando quieras que se revise o discuta un cambio grande, la vía recomendada sigue
siendo: rama nueva → Pull Request → revisión → merge. Para cambios chicos y de bajo
riesgo (como este informe), commit directo a `main` es razonable.
