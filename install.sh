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

# Copy plugin files
cp -r "$SOURCE_DIR/src/hermes_mobile_plugin" "$PLUGIN_DIR/hermes_mobile_plugin"
cp "$SOURCE_DIR/plugin.yaml" "$PLUGIN_DIR/"

# Create __init__.py for plugin discovery
cat > "$PLUGIN_DIR/__init__.py" << 'EOF'
"""Hermes Mobile QR Plugin - Auto-load entry point."""
from .hermes_mobile_plugin import create_plugin
EOF

echo "✅ Plugin installed to $PLUGIN_DIR"

# Generate QR code
echo ""
echo "📱 Generating QR code..."
if command -v hermes-mobile-qr &> /dev/null; then
    hermes-mobile-qr
else
    python3 -m hermes_mobile_plugin.cli
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