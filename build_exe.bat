@echo off
chcp 65001 > nul

echo =============================================
echo  SitemapGenerator exe Build Script
echo =============================================
echo.

python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)

echo [1/4] Installing dependencies...
pip install playwright pyinstaller --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo [2/4] Installing Chromium browser...
python -m playwright install chromium
if errorlevel 1 (
    echo [ERROR] Chromium install failed.
    pause
    exit /b 1
)

echo [3/4] Building exe...
python build_helper.py
if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo =============================================
echo  Build complete!
echo =============================================
echo.
echo  Run: dist\SitemapGenerator\SitemapGenerator.exe
echo  Distribute the entire dist\SitemapGenerator\ folder.
echo.
pause
