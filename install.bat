@echo off
REM Hermes Mobile Plugin Installer for Windows
REM Run in PowerShell or Command Prompt

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

REM Install plugin files to Hermes plugins directory
echo.
echo 🔧 Installing plugin to Hermes...
set PLUGIN_DIR=%USERPROFILE%\.hermes\plugins\hermes-mobile-qr
if not exist "%PLUGIN_DIR%" mkdir "%PLUGIN_DIR%"

REM Copy plugin files
xcopy /E /I /Y "%~dp0src\hermes_mobile_plugin" "%PLUGIN_DIR%\hermes_mobile_plugin" >nul
copy /Y "%~dp0plugin.yaml" "%PLUGIN_DIR\" >nul

REM Create __init__.py for plugin discovery
echo from .hermes_mobile_plugin import create_plugin > "%PLUGIN_DIR%\__init__.py"

echo ✅ Plugin installed to %PLUGIN_DIR%

REM Generate QR code
echo.
echo 📱 Generating QR code...
hermes-mobile-qr

echo.
echo ✅ Installation complete!
echo.
echo Next steps:
echo   1. Hermes Agent will auto-generate QR on startup
echo   2. Run 'hermes-mobile-qr' anytime to regenerate
echo   3. Install Hermes Mobile APK on your phone
echo   4. Scan QR code in app Settings ^> Scan QR
echo.
echo For cross-network: Ensure Tailscale is running on both devices
echo   Desktop: tailscale up
echo   Mobile:  Install Tailscale app + login
pause