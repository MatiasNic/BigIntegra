@echo off
rem Este script vive en packaging/; se posiciona en la raiz del proyecto
rem para que las rutas relativas (dist, build, .venv, requirements.txt) queden ahi.
cd /d "%~dp0\.."

if not exist .venv (
  python -m venv .venv
)

call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

pyinstaller --noconfirm --clean packaging\BigIntegra_Desktop.spec

if errorlevel 1 (
  echo.
  echo ERROR: la compilacion no finalizo correctamente.
  pause
  exit /b 1
)

echo.
echo Build ONEFILE finalizado.
echo Ejecutable en la carpeta dist\ (BigIntegra_^<version^>.exe)
echo.
pause
