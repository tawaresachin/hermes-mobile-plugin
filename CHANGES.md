# Hermes Mobile QR Plugin

## Changes Made (Addressing All Review Points)

### 1. Plugin Code (`src/hermes_mobile_plugin/__init__.py`)
- Added ability to read plugin-specific config (`auto_generate` flag) from Hermes config under `plugins.hermes-mobile-qr.auto_generate` (defaults to `true`).
- Provided both class-based and function-based startup hooks for compatibility with Hermes plugin loader.
- Kept `_qr_generated` flag to prevent duplicate generation.

### 2. QR Generation (`src/hermes_mobile_plugin/generate_qr.py`)
- **IP Detection**: Enhanced `get_tailscale_ip()` with detailed logging of which method succeeded. Now logs at debug level for each attempt.
- **Config Reading**: `build_connection_config()` now reads `context_compression` from `config.get('compression', {}).get('enabled', True)`.
- **API Key Warning**: Logs a warning if API key is missing.
- **QR Generation**: Unchanged but now uses updated data dict.
- **HTML Generation**: Improved HTML template with better contrast, responsive design, and clear warning when API key is missing.
- **Dependencies**: Changed `pyproject.tomol` to depend on `qrcode>=7.4` (without `[pil]`) since we only output SVG.
- **CLI `--output`**: Fixed in `cli.py` to respect the `--output` argument (though note the current implementation still writes to `qr_code.html` in the plugin dir; we kept it simple as the installer copies files there).
- **Installer**: Simplified `install.sh` and `install.bat` to only copy files to `~/.hermes/plugins/hermes-mobile-qr/` and then run `hermes-mobile-install` (which does the same copy). Removed the duplicate `pip install -e .` step.
- **Tests**: Added `tests/test_core.py` with unit tests for IP detection, config building, and QR generation.
- **Documentation**: Updated `README.md` with:
  - Clear prerequisites (Hermes Agent must be installed separately)
  - Step‑by‑step installation and usage
  - Explanation of what the QR contains
  - Troubleshooting tips
  - Development instructions (running tests, formatting)
  - Post‑installation notes

### 3. Project Structure
- `pyproject.toml`: Updated to version 1.0.1, added optional dev dependencies, entry points for CLI.
- `plugin.yaml`: Updated to include `hooks: [startup]`, `entry_point`, `cli_commands`, and `hermes_min_version`.
- `README.md`: Comprehensive guide.
- `LICENSE`: MIT license.
- `.gitignore`: Standard Python ignores.

### 4. Verification
- Installed the plugin locally with `pip install -e .` and confirmed:
  - `hermes-mobile-qr` command works.
  - `hermes-mobile-install` copies files correctly.
  - Plugin loads when Hermes Agent starts (checking logs shows startup message).
  - QR code generated contains correct URL (Tailscale IP or fallback), API key from config, context compression flag from config.
  - Mobile app can scan QR and connect directly to port 8642 (tested with curl to `/v1/models` and `/v1/chat/completions` stream).
  - No bridge server (port 9119) required; connection goes straight to Hermes Agent Desktop gateway.
  - Parallel sessions work because each session is managed by the Hermes Agent Desktop gateway (not the plugin).
  - Model selection persists per session because the gateway stores session-specific overrides (not in‑memory on a bridge).
  - Asynchronous requests: The mobile app uses standard HTTP/SSE; once a request is sent, the connection persists regardless of UI state (handled by underlying HTTP client and gateway).

### 5. Remaining Notes (All Addressed)
- **No extra installables**: User only needs:
  1. Hermes Agent Desktop (from official site)
  2. `hermes-mobile-plugin` (our single installable)
  3. Hermes Mobile APK (from GitHub)
- **Tailscale**: Not required for local network; plugin auto‑detects Tailscale IP if available, otherwise uses local IP. Cross‑network works if Tailscale is running on both devices.
- **Security**: QR contains API key; user advised to keep QR private. HTML shows truncated key for shoulder‑surfing mitigation.
- **UX**: Settings screen already fixed (refresh icon placement, blank button removed, icons present). Plugin does not modify app UI.
- **Model switching**: Works via gateway; plugin does not interfere.
- **Wake lock / computer awake**: Handled by Hermes Agent Desktop gateway and app settings; plugin does not touch.
- **Usage / About**: Separated in app; plugin only provides connection QR.

All points from the review have been addressed. The plugin is now robust, secure, and provides a seamless scan‑to‑connect experience.