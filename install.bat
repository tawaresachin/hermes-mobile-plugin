@echo off
REM Hermes Mobile Plugin Installer for Windows
REM Run in Command Prompt or PowerShell

setlocal

echo 📱 Hermes Mobile Plugin Installer
echo =================================

REM Locate the Python that OWNS the `hermes` CLI — that interpreter's
REM environment is where the plugin must be installed, and the only one that
REM resolves the REAL Hermes home (profile-aware: %LOCALAPPDATA%\hermes on
REM Windows, ~/.hermes on Linux/Termux, XDG dirs on macOS). A bare `python`
REM from PATH is frequently a DIFFERENT interpreter than the one hermes runs
REM on — installing there silently does nothing.
set "HERMES_PY="
set "HERMES_BIN="
for /f "delims=" %%p in ('where hermes 2^>nul') do (
    if not defined HERMES_BIN set "HERMES_BIN=%%p"
)
REM Single-line statements ONLY below: %VAR% inside a parenthesized block
REM expands at PARSE time, before the `for` sets it — that made every
REM `exist` probe test a relative path and the locator always fail.
set "BIN_DIR="
if defined HERMES_BIN for %%p in ("%HERMES_BIN%") do set "BIN_DIR=%%~dpp"
if defined BIN_DIR if exist "%BIN_DIR%python.exe" set "HERMES_PY=%BIN_DIR%python.exe"
if not defined HERMES_PY if defined BIN_DIR if exist "%BIN_DIR%..\hermes-agent\venv\Scripts\python.exe" set "HERMES_PY=%BIN_DIR%..\hermes-agent\venv\Scripts\python.exe"
if not defined HERMES_PY if defined BIN_DIR if exist "%BIN_DIR%..\venv\Scripts\python.exe" set "HERMES_PY=%BIN_DIR%..\venv\Scripts\python.exe"
if not defined HERMES_PY (
    echo ❌ Hermes Agent not found, or its Python environment could not be located.
    echo    Install Hermes Agent, run 'hermes' once to initialize configuration,
    echo    then retry this installer.
    exit /b 1
)
for /f "tokens=2 delims= " %%a in ('"%HERMES_PY%" --version') do set PYTHON_VERSION=%%a
echo ✅ Hermes Agent found (Python %PYTHON_VERSION%)

REM Resolve the REAL Hermes home through hermes-agent itself — never guess it.
REM No sys.path games: hermes_constants belongs to hermes-agent (already
REM importable from the owning interpreter) — interpolating this checkout's
REM path into the -c string was cargo cult and broke on paths containing
REM quotes. Written to a temp file and read with set /p instead of for /f:
REM for /f runs via cmd /c, whose legacy quote-stripping mangles a command
REM that STARTS with a quoted exe path containing spaces.
set "HERMES_HOME_FILE=%TEMP%\hermes_home_hmq.txt"
"%HERMES_PY%" -c "import pathlib, hermes_constants; print(pathlib.Path(hermes_constants.get_hermes_home()))" > "%HERMES_HOME_FILE%" 2>nul
set "HERMES_HOME_DIR="
set /p HERMES_HOME_DIR=<"%HERMES_HOME_FILE%"
del "%HERMES_HOME_FILE%" >nul 2>nul
if not defined HERMES_HOME_DIR (
    echo ❌ Could not resolve the Hermes home directory.
    echo    Run 'hermes' once to initialize configuration, then retry.
    exit /b 1
)
echo ✅ Hermes home: %HERMES_HOME_DIR%
set "HERMES_CONFIG=%HERMES_HOME_DIR%\config.yaml"
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
"%HERMES_PY%" -m pip install --quiet "pyyaml>=6.0" "qrcode>=7.4" "aiohttp>=3.8"

REM Install plugin files to Hermes plugins directory. The quoted destination
REM has the backslash OUTSIDE the quotes — the old line had it inside
REM ("%%PLUGIN_DIR\"), which never expanded and left the path unquoted.
echo.
echo 🔧 Installing plugin to Hermes...
set "PLUGIN_DIR=%HERMES_HOME_DIR%\plugins\hermes-mobile-qr"
if not exist "%PLUGIN_DIR%" mkdir "%PLUGIN_DIR%"

REM Copy plugin files
xcopy /E /I /Y "%~dp0src\hermes_mobile_plugin" "%PLUGIN_DIR%\hermes_mobile_plugin" >nul
copy /Y "%~dp0plugin.yaml" "%PLUGIN_DIR%\" >nul
copy /Y "%~dp0__init__.py" "%PLUGIN_DIR%\" >nul

echo ✅ Plugin installed to %PLUGIN_DIR%

REM Install the plugin package so the console scripts land on PATH. If this
REM environment has no usable pip the plugin itself still works - only the
REM 'hermes-mobile-plugin' convenience command is missing.
"%HERMES_PY%" -m pip install --quiet -e "%~dp0." 2>nul
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
    "%HERMES_PY%" -m hermes_mobile_plugin.cli install
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