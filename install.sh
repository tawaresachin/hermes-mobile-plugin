#!/usr/bin/env bash
# Hermes Mobile Plugin Installer
# Cross-platform installer for Linux, macOS, Windows (Git Bash/WSL), Android (Termux)

set -euo pipefail

PLUGIN_NAME="hermes-mobile-plugin"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# PLUGIN_DIR is resolved after the home is discovered below (never guessed).
PLUGIN_DIR=""

echo "📱 Hermes Mobile Plugin Installer"
echo "================================="

# Locate the Python that OWNS the hermes CLI — that interpreter's
# environment is where the plugin must be installed, and the only one that
# resolves the REAL Hermes home (profile-aware: ~/.hermes on Linux/Termux,
# XDG dirs on macOS, %LOCALAPPDATA%\hermes on Windows/WSL). A bare python3
# from PATH is frequently a DIFFERENT interpreter than the one hermes runs
# on — installing there silently does nothing.
HERMES_BIN="$(command -v hermes 2>/dev/null || true)"
HERMES_PY=""
if [ -n "$HERMES_BIN" ]; then
    BIN_DIR="$(cd "$(dirname "$HERMES_BIN")" && pwd)"
    for cand in \
        "$BIN_DIR/python3" \
        "$BIN_DIR/python" \
        "$BIN_DIR/../hermes-agent/venv/bin/python3" \
        "$BIN_DIR/../venv/bin/python3"; do
        if [ -x "$cand" ]; then HERMES_PY="$cand"; break; fi
    done
fi
if [ -z "$HERMES_PY" ] && command -v python3 &> /dev/null; then
    # No hermes-owned interpreter found; a bare python3 can still resolve
    # the home if hermes-agent is importable (pipx/user installs).
    if python3 -c "import hermes_constants" 2> /dev/null; then
        HERMES_PY="$(command -v python3)"
    fi
fi
if [ -z "$HERMES_PY" ]; then
    echo "❌ Hermes Agent not found, or its Python environment could not be located."
    echo "   Install Hermes Agent, run 'hermes' once to initialize configuration,"
    echo "   then retry this installer."
    exit 1
fi
PYTHON_VERSION=$("$HERMES_PY" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✅ Hermes Agent found (Python $PYTHON_VERSION)"

# Resolve the REAL Hermes home through hermes-agent itself — never guess it.
SOURCE_DIR_BEFORE_HOME="$SOURCE_DIR"
HERMES_HOME_DIR=$("$HERMES_PY" -c \
    "import sys, pathlib; sys.path.insert(0, r'$SOURCE_DIR_BEFORE_HOME/src'); import hermes_constants; print(pathlib.Path(hermes_constants.get_hermes_home()))" 2>/dev/null || true)
if [ -z "$HERMES_HOME_DIR" ]; then
    echo "❌ Could not resolve the Hermes home directory."
    echo "   Run 'hermes' once to initialize configuration, then retry."
    exit 1
fi
echo "✅ Hermes home: $HERMES_HOME_DIR"
HERMES_CONFIG="$HERMES_HOME_DIR/config.yaml"
if [[ ! -f "$HERMES_CONFIG" ]]; then
    echo "❌ Hermes config not found at $HERMES_CONFIG"
    echo "   Please run 'hermes' first to initialize configuration"
    exit 1
fi
echo "✅ Hermes config found"

# Dependencies. Termux: the hermes-agent environment already provides both;
# a bare pip install there risks building C extensions against the wrong
# NDK toolchain, so skip it.
if [ -z "${TERMUX_VERSION:-}" ]; then
    echo ""
    echo "🔧 Installing Python dependencies (pyyaml, qrcode)..."
    "$HERMES_PY" -m pip install --quiet "pyyaml>=6.0" "qrcode>=7.4" || \
        echo "⚠️  pip install failed - if Hermes Agent already provides these, ignore this."
fi

# Install plugin files to Hermes plugins directory
echo ""
echo "🔧 Installing plugin to Hermes..."
PLUGIN_DIR="$HERMES_HOME_DIR/plugins/hermes-mobile-qr"
mkdir -p "$PLUGIN_DIR"

# Copy plugin files. NOTE the trailing "/.": a plain `cp -r src dest` NESTS
# the package inside itself (hermes_mobile_plugin/hermes_mobile_plugin) when
# the destination already exists — which is exactly what broke every upgrade.
mkdir -p "$PLUGIN_DIR/hermes_mobile_plugin"
cp -R "$SOURCE_DIR/src/hermes_mobile_plugin/." "$PLUGIN_DIR/hermes_mobile_plugin/"
cp "$SOURCE_DIR/plugin.yaml" "$PLUGIN_DIR/"

# Copy the real __init__.py (forwards BOTH register(ctx) and create_plugin).
# Do not regenerate a stub here — a stub without register() makes the plugin
# loader skip it and ALL /api/audio/* routes 404.
cp "$SOURCE_DIR/__init__.py" "$PLUGIN_DIR/__init__.py"

echo "✅ Plugin installed to $PLUGIN_DIR"

# Generate QR code
# `hermes-mobile-qr` was never a real console script (only hermes-mobile-plugin
# and hermes-mobile-install exist in pyproject.toml), and the module fallback
# only worked if the package was importable from the current directory — so
# the old final step failed on stock checkouts. The PYTHONPATH fallback below
# always works.
echo ""
echo "📱 Generating QR code..."
if command -v hermes-mobile-plugin &> /dev/null; then
    hermes-mobile-plugin install
else
    PYTHONPATH="$SOURCE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$HERMES_PY" -m hermes_mobile_plugin.cli install
fi

echo ""
echo "✅ Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Hermes Agent will auto-generate QR on startup"
echo "  2. Run 'hermes-mobile-qr' anytime to regenerate"
echo "  3. Install Hermes Mobile APK on your phone"
echo "  4. Scan QR code in app Settings → Scan QR"
echo ""
echo "For cross-network: Ensure Tailscale is running on both devices"
echo "  Desktop: tailscale up"
echo "  Mobile:  Install Tailscale app + login"