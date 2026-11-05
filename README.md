# Big Integra

Herramienta de escritorio para **armar y revisar legajos de Discapacidad**. Parte de
[BigPDF](https://github.com/MatiasNic/BigPDF) (importar, organizar, separar, comprimir y
exportar PDFs) y agrega el panel **Integra**:

1. El usuario carga la documentación (PDF, JPG, escaneos, Office).
2. Marca qué prestaciones se están cargando y las condiciones del caso.
3. **Analizar**: el programa lee cada hoja (capa de texto u OCR local), identifica qué
   documento es, arma el checklist de requisitos según los instructivos de Integra y
   aplica controles exactos (vigencia del CUD, fechas, períodos, sesiones, CUIT/CBU…).
4. Propone el **orden de armado** y las partes con nombre; el usuario revisa en la
   grilla, corrige lo que haga falta y descarga.

**Sin IA y sin enviar datos afuera**: todo corre en la PC. El OCR es el Tesseract que ya
trae PyMuPDF, con el idioma español en `tools/tessdata/spa.traineddata` (viaja dentro del
`.exe`).

> Si usás [Claude Code](https://claude.ai/code), `CLAUDE.md` tiene el contexto de
> arquitectura y convenciones.

## Panel Integra: dónde está cada cosa

| Qué | Dónde |
|---|---|
| Tipos de documento (títulos y pies de los formularios oficiales, campos, palabras) | `app/engine/integra/catalog/document_types.json` |
| Requisitos por prestación, condiciones y orden de armado | `app/engine/integra/catalog/prestaciones.json` |
| Lectura de texto / OCR | `app/engine/integra/text_service.py` |
| Identificación de hojas | `app/engine/integra/classifier.py` |
| Datos (fechas, DNI, CUIT, CBU, períodos, sesiones) | `app/engine/integra/extractors.py` |
| Controles exactos | `app/engine/integra/rules.py` |
| Checklist, orden e informe | `analyzer.py`, `ordering.py`, `report.py` |
| Interfaz del panel | `app/ui/web/tools/integra-panel.js` |

**Actualizar los criterios** (instructivos nuevos): editar los dos JSON del catálogo y
correr `pytest tests/integra`. Los formularios oficiales usados en los tests están en
`tests/fixtures/formularios_2024/`.

**Medir el acierto con legajos reales** (en la PC local, los documentos no se comparten):

```bash
python tools/dev/integra_eval.py "C:\ruta\a\legajos"
python tools/dev/integra_eval.py --medir "C:\ruta\a\legajos\integra_eval.csv"
```

## Estructura del proyecto

```
app/
├── core/auth/       # Verificación de acceso global al arrancar (Google Apps Script)
├── desktop/         # Puente QWebChannel, workers y proceso de exportación
├── engine/          # Lógica de PDF (compresión, conversión, ghostscript, export)
│                     #   sin dependencia de PySide6 — testeable de forma aislada
└── ui/web/          # Interfaz (HTML/CSS/JS), vendored: pdf.js, jszip

packaging/           # BigIntegra_Desktop.spec (PyInstaller) + build_exe.bat
tools/dev/           # Scripts de desarrollo (encode_auth_url.py)
tests/               # Tests unitarios de app/engine
```

## Requisitos

- Python 3.10+ (desarrollado y probado con 3.13)
- Windows para el build final (`.exe`); en Linux/Mac se puede correr en modo desarrollo
  siempre que PySide6/QtWebEngine estén disponibles.
- **Opcionales** (la app funciona sin ellos, con degradación elegante):
  - Ghostscript instalado en el sistema, para el motor de compresión. Sin él, la app
    usa un fallback basado en PyMuPDF.
  - LibreOffice instalado, para convertir Word/Excel/PowerPoint a PDF. Sin él, esa
    conversión específica no está disponible (el resto de la app funciona igual).

## Desarrollo

### Quickstart en VS Code (Windows)

1. Cloná el repositorio (`Ctrl+Shift+P` → `Git: Clone` → pegá la URL del repo).
2. Abrí la carpeta clonada en VS Code.
3. Abrí una terminal integrada: `` Ctrl + ` `` (o menú "Terminal → New Terminal").
4. Corré, una sola vez:
   ```bash
   pip install -r requirements.txt
   ```
5. Cada vez que quieras abrir la app:
   ```bash
   python main.py
   ```

Si `pip` o `python` no se reconocen como comando, instalá Python desde
[python.org](https://www.python.org/downloads/) marcando la opción **"Add python.exe
to PATH"** durante la instalación, y volvé a abrir la terminal de VS Code.

### Probar cambios hechos por Claude Code (sesión en la nube)

Claude trabaja en una rama propia (`claude/...`) y la sube a GitHub. Para probarla:

1. En VS Code, abajo a la izquierda, hacé clic en el nombre de la rama actual
   (o `Ctrl+Shift+P` → `Git: Checkout to...`) y elegí `origin/claude/<nombre-de-rama>`.
2. Cada vez que Claude avise que subió algo nuevo: `Ctrl+Shift+P` → `Git: Pull`
   (o `git pull` en la terminal).
3. `F5` corre la app con el depurador (configuración "Big Integra: ejecutar app" en
   `.vscode/launch.json`), o `python main.py` en la terminal.
4. Si funciona bien, mergeá la rama a `main` (vía Pull Request en GitHub).

Al abrir el proyecto, VS Code ofrece instalar las extensiones recomendadas
(Python, Ruff, Claude Code) — aceptá.

### Setup completo (con entorno virtual y herramientas de desarrollo)

```bash
python -m venv .venv
source .venv/bin/activate   # o .venv\Scripts\activate en Windows
pip install -r requirements.txt -r requirements-dev.txt

python main.py
```

### Lint y tests

```bash
ruff check .
pytest
```

Los tests cubren `app/engine/` (servicios de PDF), el control de acceso (`tests/core/`)
y el bridge de escritorio (`tests/desktop/`, con Qt sin ventana: importar, comprimir,
cancelar, exportar y guardar). Los de `tests/desktop/` se saltean si PySide6 no está
instalado; así el CI de GitHub no necesita instalar Qt. La interfaz completa se prueba
a mano con `docs/checklist-pruebas.md` antes de distribuir un `.exe`.

Cambios visibles para el usuario: anotarlos en `CHANGELOG.md`.

## Acceso y credenciales

Big Integra valida el acceso al arrancar contra un Google Apps Script (`app/core/auth/access_control.py`).
La URL del script está ofuscada (XOR + base64, **no es cifrado real**) en
`app/core/auth/auth_settings.py::ACCESS_SOURCE_URLS_B64`.

**Uso sin internet:** si no hay conexión (o el Apps Script falla), la app funciona solo
durante los días indicados en la columna `diasGracia` que devuelve la planilla, contados
desde la última validación online exitosa. Con `diasGracia = 0` no se permite usar sin
conexión. El registro de la última validación vive en
`%LOCALAPPDATA%\BigIntegra\access_cache.json`, firmado y ligado al equipo y usuario de
Windows: si se edita, se copia a otra PC o se atrasa el reloj, se rechaza. Si la planilla
deniega el acceso, ese registro se borra y la app deja de funcionar también sin conexión.

Para desarrollo local, definí la variable de entorno en vez de tocar el código:

```bash
export BIGPDF_ACCESS_URL="https://script.google.com/macros/s/.../exec"
```

Para generar el valor ofuscado a pegar en `ACCESS_SOURCE_URLS_B64`:

```bash
python tools/dev/encode_auth_url.py "https://script.google.com/macros/s/.../exec"
```

**Antes de hacer este repositorio público** (la ofuscación actual solo evita que la URL
quede en texto plano; no protege contra alguien con acceso al repo):

1. Publicar un Apps Script nuevo (URL nueva): la actual queda en el historial de git
   aunque se borre del código.
2. Sacar la URL de `auth_settings.py`: leerla de `BIGPDF_ACCESS_URL` o inyectarla al
   compilar desde un secreto de GitHub Actions.
3. Cambiar `ACCESS_SOURCE_XOR_KEY` (también firma el caché de acceso sin conexión).

## Build del ejecutable (Windows)

```bat
packaging\build_exe.bat
```

Genera `dist\BigIntegra_<versión>.exe` (PyInstaller onefile, ver `packaging/BigIntegra_Desktop.spec`).
El nombre y los metadatos de Windows (VERSIONINFO) salen de `app/__init__.py`.

Al abrirse, el `.exe` muestra `packaging/splash.png` (el logo) mientras se descomprime,
antes de que arranque la app. Requiere que el Python que compila tenga `tkinter` (el
instalador oficial de python.org lo trae por defecto); si no lo tiene, se compila
igual pero sin esa imagen. Para cambiar el diseño: `python tools/dev/make_splash.py`.

### Compilar sin tener Python instalado (GitHub Actions)

El `.exe` se compila en una PC Windows de GitHub (`.github/workflows/build-exe.yml`)
**solo cuando se pide**, para no gastar minutos de GitHub:

- Tag `build-AAAAMMDD` (u otro `build-*`): compilación de prueba.
- Tag `vX.Y.Z`: versión publicada; además crea un **Release** con el `.exe` (no vence).

Para descargarlo: pestaña **Actions** → ejecución de **"Compilar BigIntegra.exe"** →
sección **Artifacts** → bajar el `.zip` y extraer el `.exe` (se conserva 14 días).

## Diagnóstico

- Logs: `%LOCALAPPDATA%\BigIntegra\logs\bigpdf.log` (y `bigpdf-export.log` para el
  proceso de exportación). Pedírselos al usuario cuando reporte un problema.
- Pantalla negra o cierres por drivers de video: abrir con `BigIntegra.exe --modo-compatible`
  (desactiva la GPU), o poner `"disable_gpu": true` en `%LOCALAPPDATA%\BigIntegra\prefs.json`.
- Si la interfaz se ve de otro tamaño que antes en pantallas con escalado (125–150 %),
  `BIGPDF_DISABLE_HIDPI=1` vuelve al comportamiento anterior.

## Versión

La versión vive en un solo lugar: `app/__init__.py::__version__`. Se referencia desde
`main.py` (título de ventana, AppUserModelID de Windows) y `app/core/auth/auth_settings.py`
(`APP_VERSION`). El nombre del ejecutable en `packaging/BigIntegra_Desktop.spec` se actualiza
manualmente en cada release.
