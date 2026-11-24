@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   TransOverlay (Google Translate Overlay) Windows EXE Build
echo ============================================================
echo.

taskkill /f /im TransOverlay.exe >nul 2>nul
taskkill /f /im ScreenTranslateOverlay.exe >nul 2>nul

set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD (
    where python >nul 2>nul && set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [ERROR] Python 3.10+ not found in PATH.
    pause
    exit /b 1
)

echo [1/3] Creating virtual environment (.venv_trans)...
if not exist ".venv_trans\Scripts\python.exe" (
    %PY_CMD% -m venv .venv_trans
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

set "VENV_PY=.venv_trans\Scripts\python.exe"

echo [2/3] Installing PyQt6, pynput, Pillow, PyInstaller...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [ERROR] Failed to install packages.
    pause
    exit /b 1
)

echo [3/3] Building TransOverlay.exe with custom icon...
"%VENV_PY%" -m PyInstaller --noconfirm --clean --noconsole --onefile --name "TransOverlay" --icon "app_icon.ico" --add-data "app_icon.png;." --add-data "app_icon.ico;." --hidden-import "pynput.keyboard._win32" --hidden-import "pynput.mouse._win32" screen_translate_overlay.py
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   BUILD COMPLETE: dist\TransOverlay.exe
echo ============================================================
explorer dist
pause
