#!/usr/bin/env bash
# Hermes Mobile Plugin Installer
# Cross-platform installer for Linux, macOS, Windows (Git Bash/WSL), Android (Termux)

set -euo pipefail

PLUGIN_NAME="hermes-mobile-plugin"
PLUGIN_DIR="$HOME/.hermes/plugins/hermes-mobile-qr"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "📱 Hermes Mobile Plugin Installer"
echo "================================="

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.10+"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✅ Python $PYTHON_VERSION found"

# Dependencies. Termux: the hermes-agent environment already provides both;
# a bare pip install there risks building C extensions against the wrong
# NDK toolchain, so skip it.
if [ -z "${TERMUX_VERSION:-}" ]; then
    echo ""
    echo "🔧 Installing Python dependencies (pyyaml, qrcode)..."
    python3 -m pip install --quiet "pyyaml>=6.0" "qrcode>=7.4" || \
        echo "⚠️  pip install failed - if Hermes Agent already provides these, ignore this."
fi

# Check Hermes config
HERMES_CONFIG="$HOME/.hermes/config.yaml"
if [[ ! -f "$HERMES_CONFIG" ]]; then
    echo "❌ Hermes config not found at $HERMES_CONFIG"
    echo "   Please run 'hermes' first to initialize configuration"
    exit 1
fi
echo "✅ Hermes config found"

# Install plugin files to Hermes plugins directory
echo ""
echo "🔧 Installing plugin to Hermes..."
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
    PYTHONPATH="$SOURCE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m hermes_mobile_plugin.cli install
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