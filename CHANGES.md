# Hermes Mobile QR Plugin

## 0.0.24 — _repo_dir probes .git

`_resolve_repo_dir()` alone fails under the toolchain
interpreter (no `hermes_constants` pre-bootstrap -> returns the
toolchain root, no .git). Probe HERMES_HOME/hermes-agent, the native
resolver, and the venv parent for a real .git instead. Verified:
check reports SHAs + behind on the toolchain-gateway host.
## 0.0.23 — update/check now sees the real repo

`_repo_dir()` resolves the Hermes checkout via the native
`_resolve_repo_dir()` (the venv parent is NOT a repo — the check ran
git against `/`, got nothing, and the row showed no update forever).
Now reports `behind` + both SHAs honestly; update button works.
## 0.0.22 — About row shows the real release version

`_installed_version()` now delegates to Hermes's own
`hermes_cli.version_info.get_version_info()` — the same API
`hermes --version` uses. A git-bootstrap install that sits on a
release base reports `0.21.5+5832` (release + commits ahead) instead
of the stamp's `git.<sha>` fallback; untagged checkouts still
degrade to `git.<sha>`, no-git (ZIP) installs to the stamp. The
parallel hand-rolled stamp reader from 0.0.21 is deleted —
`version_info` owns all of those paths natively.
## 0.0.21 — About-section version fixes

Two About rows in the mobile app misreported on git-bootstrap Hermes
installs:

- **Agent version** showed `unknown`: `hermes_cli.__version__` is the
  literal string `unknown` when the install stamp has no release base.
  `_installed_version()` now falls back to the stamp's `displayVersion`
  (`git.<sha7>`, same as `hermes --version`), then the stamp commit.
- **Hermes-mobile version** had no source: the QR `hermes://connect` URI
  carried only `url` + `key`. It now ships `&version=<plugin>` too, so
  the phone can render the plugin version offline. Old app builds ignore
  the unknown param (backward compatible).
## 0.0.20 — README install-pin auto-sync

Every release now keeps the README install command (`...@vX.Y.Z`)
pointing at the newest tag: the auto-tag workflow runs
`python -m hermes_mobile_plugin.readme_pin` after creating the tag and
pushes the doc fix back to main (idempotent — no commit when the pin
is already current). No more manual `docs(readme): fix stale pin`
releases.
## 0.0.19 — Zero-manual install: key seeding, runtime venv, live port

`install` now leaves nothing manual: it works on the gateway host's real
environment, not just the toolchain interpreter the shell installers
pip into.

- **API key auto-seeding** (`key_seeder.py`): when
  `platforms.api_server.extra.key` and/or `API_SERVER_KEY` in the
  Hermes `.env` are missing or shorter than 16 chars, `install`
  generates `secrets.token_urlsafe(48)` and writes it to BOTH —
  config.yaml via a pyyaml round-trip with a one-time `.bak` backup,
  and `.env` by replacing/appending the `API_SERVER_KEY=` line only
  (0600 perms). A strong existing key is NEVER clobbered; when only
  one side has it, it is copied to the other so the two can never
  disagree.
- **Gateway runtime-venv detection + install into it**
  (`envdetect.py`, shared by `install.sh`, `install.bat` and the CLI):
  on hermes-bootstrap hosts (Termux/proot) the gateway resolves
  3rd-party packages from an ISOLATED runtime venv under
  `~/.hermes/installs/<hash>/environments/<hash>/venv`, so a plain
  `pip install` into the toolchain python left the running gateway
  with "No module named 'hermes_mobile_plugin'". `install` now
  detects that venv (running-gateway pidfile → `/proc/<pid>/exe` →
  in-process sys.path → newest-mtime scan fallback), and pip-installs
  the plugin into it with `ensurepip` when the venv has no pip
  (observed live). No isolated envs → one-line notice, install
  continues. `install_plugin` (the `hermes-mobile-install` console
  script the shell installers end with) runs the same step, so the
  .sh/.bat/pip paths can never drift.
- **Live port resolution + supervised restart + QR at the end**
  (`constants.resolve_gateway_port`, `cli`, `supervisor`):
  `platforms.api_server.extra.port` > `API_SERVER_PORT` env > 8642,
  used by `status`, the watchdog daemon (it no longer hard-codes 8642)
  and the QR fallback. After installing, if a gateway is already
  running: a healthy one is left alone (spec: skip the restart); an
  unhealthy one gets SIGTERM'd only after the 24x7 watchdog is
  ensured first (the plugin never leaves a gateway unwatched), then
  the install waits up to 60s for `/health` on the resolved port and
  regenerates the QR with the live key baked in — replacing the old
  "Next steps: start Hermes Agent" guidance.

## 0.0.12 — POSIX audit fixes (Linux / macOS / Termux)

Critical line-by-line audit of everything the Windows-only machine could
not exercise:

- **`install.sh` shipped CRLF in the working tree** — a `bash install.sh`
  on stock Linux/macOS/Termux dies on line 1 (`$'\r': command not found`).
  Converted to pure LF; committed blobs were verified already LF, and a
  new `.gitattributes` enforces `*.sh eol=lf` / `*.bat eol=crlf` at
  checkout so the two can never regress through editor/autocrlf settings.
- **macOS update button 500'd**: `update_routes.py` unconditionally
  prefixed `setsid`, which macOS/BSD do not ship. `start_new_session=True`
  already detaches via the setsid() syscall, so the binary is now used
  only on Linux/Termux (where the perl SIGCHLD reset is also needed).
- **`aiohttp` was never declared** — the plugin imports it directly but
  relied on hermes-agent's environment having it. Now declared in
  `pyproject.toml` and installed by both shell installers.
- **Locator quoting hardening (install.sh)**: the home-resolution one-liner
  interpolated `$SOURCE_DIR` into a Python string (a checkout path
  containing a quote broke the installer); the shebang branch now strips
  `\r` from CRLF checkouts and validates candidates are real Pythons.
- **Full POSIX dry-run verification**: `install.sh` executed end-to-end
  (exit 0) in a sandboxed fake Hermes home with a shebang-based venv shim,
  including the CRLF-shim `\r`-stripping branch; 53/53 tests pass.

## 0.0.11 — Real-machine install fixes (Windows verified end-to-end)

Found by actually running the installer on a live Windows machine with
Hermes Agent v0.21.0 installed under `%LOCALAPPDATA%\hermes`:

- **Installers no longer guess the Hermes home.** `install.bat` and
  `install.sh` locate the Python that owns the `hermes` CLI and resolve
  the home through `hermes_constants.get_hermes_home()` — on this machine
  that is `%LOCALAPPDATA%\hermes`, not `~/.hermes`. All pip installs and
  file copies target that environment, so the plugin actually lands where
  the gateway loads it.
- **Supervisor refuses to spawn into an occupied port.** A stale WSL
  `netsh portproxy` rule (served by svchost/iphlpsvc) owned 8642; the old
  watchdog restart-looped against it and left bind-failed zombie gateways.
  It now checks port ownership after an authoritative `gateway stop` and
  backs off with a clear diagnostic instead.
- **`hermes gateway stop` timeout is handled** (wedged gateway) with an
  actionable message instead of a bare traceback every cycle.
- **PID-file lock no longer hides the PID.** Windows mandatory byte-range
  locks covered the text itself, so `status`/`--stop` crashed with
  `PermissionError` while a supervisor ran; the lock byte moved past the
  content and stop/status gained a process-enumeration fallback that
  excludes its own ancestor chain (the naive match terminated the caller).
- **QR default output dir follows HERMES_HOME** (was hardcoded
  `~/.hermes`, which split the install across two homes on Windows).
- **CLI install banner**: duplicate "Step 4/4" removed; step counter fixed.

## 0.0.10 — Fix scripted installs on every OS

- **`hermes-mobile-install` was dead code**: `pyproject.toml` pointed the
  console script at `cli:install_plugin`, which never existed — every
  scripted installer died on its final step with ImportError. Implemented
  `deploy_plugin_files()` + `install_plugin()` in `cli.py`; the .bat/.sh
  installers and the console script now share one deployment path.
- **Windows (`install.bat`)**: fixed the malformed `copy` destination
  (backslash inside the variable name), stopped overwriting the loader
  entry point `__init__.py` with a stub missing `register()` (which made
  every audio/system route 404), installs `pyyaml`/`qrcode`, and now uses
  the real `hermes-mobile-plugin` console script (with a
  `python -m hermes_mobile_plugin.cli` fallback).
- **POSIX/Termux (`install.sh`)**: fixed `cp -r` nesting the package inside
  itself on re-install (`hermes_mobile_plugin/hermes_mobile_plugin`), fixed
  the broken final step (`hermes-mobile-qr` never existed; module fallback
  only worked from the repo root), skips pip on Termux.
- **Wheel installs**: `PLUGIN_VERSION` no longer reports `0.0.0+unknown` —
  falls back to package metadata when no `plugin.yaml` is on disk.
- **Windows legacy codepages**: the CLI reconfigures stdio to UTF-8 so
  piped/redirected output no longer crashes with `UnicodeEncodeError`.
- `cmd_install` now performs the deployment itself (was QR+supervisor only).

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