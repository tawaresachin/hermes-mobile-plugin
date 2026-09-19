@echo off
REM Hermes Mobile Plugin Installer for Windows
REM Run in Command Prompt or PowerShell

setlocal

echo 📱 Hermes Mobile Plugin Installer
echo =================================

REM Check Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ❌ Python not found. Please install Python 3.10+ from python.org
    exit /b 1
)

for /f "tokens=2 delims= " %%a in ('python --version') do set PYTHON_VERSION=%%a
echo ✅ Python %PYTHON_VERSION% found

REM Check Hermes config
set HERMES_CONFIG=%USERPROFILE%\.hermes\config.yaml
if not exist "%HERMES_CONFIG%" (
    echo ❌ Hermes config not found at %HERMES_CONFIG%
    echo    Please run 'hermes' first to initialize configuration
    exit /b 1
)
echo ✅ Hermes config found

REM Install Python dependencies (pyyaml + qrcode) into the SAME interpreter
REM Hermes Agent runs with.
echo.
echo 🔧 Installing Python dependencies...
python -m pip install --quiet "pyyaml>=6.0" "qrcode>=7.4"

REM Install plugin files to Hermes plugins directory. The quoted destination
REM has the backslash OUTSIDE the quotes — the old line had it inside
REM ("%%PLUGIN_DIR\"), which never expanded and left the path unquoted.
echo.
echo 🔧 Installing plugin to Hermes...
set PLUGIN_DIR=%USERPROFILE%\.hermes\plugins\hermes-mobile-qr
if not exist "%PLUGIN_DIR%" mkdir "%PLUGIN_DIR%"

REM Copy plugin files
xcopy /E /I /Y "%~dp0src\hermes_mobile_plugin" "%PLUGIN_DIR%\hermes_mobile_plugin" >nul
copy /Y "%~dp0plugin.yaml" "%PLUGIN_DIR%\" >nul
copy /Y "%~dp0__init__.py" "%PLUGIN_DIR%\" >nul

echo ✅ Plugin installed to %PLUGIN_DIR%

REM Install the plugin package so the console scripts land on PATH. If this
REM environment has no usable pip the plugin itself still works - only the
REM 'hermes-mobile-plugin' convenience command is missing.
python -m pip install --quiet -e "%~dp0." 2>nul
if %errorlevel% neq 0 (
    echo ⚠️  Could not pip install the plugin - the 'hermes-mobile-plugin'
    echo    console script will not be on PATH, but the plugin itself works.
)

REM Generate QR code
echo.
echo 📱 Generating QR code...
where hermes-mobile-plugin >nul 2>nul
if %errorlevel% equ 0 (
    call hermes-mobile-plugin install
) else (
    python -m hermes_mobile_plugin.cli install
)

echo.
echo ✅ Installation complete!
echo.
echo Next steps:
echo   1. Hermes Agent will auto-generate QR on startup
echo   2. Run 'hermes-mobile-plugin qr' anytime to regenerate
echo   3. Install Hermes Mobile APK on your phone
echo   4. Scan QR code in app Settings ^> Scan QR
echo.
echo For cross-network: Ensure Tailscale is running on both devices
echo   Desktop: tailscale up
echo   Mobile:  Install Tailscale app + login
pause