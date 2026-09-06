"""QR code generation for Hermes Mobile App connection."""

import json
import logging
import webbrowser
from pathlib import Path
from typing import Optional

try:
    import qrcode
    from qrcode.image.svg import SvgPathImage
except ImportError:
    qrcode = None
    SvgPathImage = None

from .config import load_hermes_config, validate_config
from .constants import (
    DEFAULT_GATEWAY_PORT,
    QR_BORDER,
    QR_BOX_SIZE,
    QR_ERROR_CORRECTION,
    QR_HTML_FILE,
    PLUGIN_VERSION,
)
from .ip_detector import get_tailscale_ip

logger = logging.getLogger(__name__)


class QRConfigError(Exception):
    """Raised when QR configuration is invalid."""
    pass


def build_connection_config(config: dict) -> dict:
    """Build connection configuration from Hermes config.

    Args:
        config: Hermes Agent configuration dictionary.

    Returns:
        Dictionary with connection details for QR code.

    Raises:
        QRConfigError: If required configuration is missing.
    """
    # Validate config first
    warnings = validate_config(config)
    for warning in warnings:
        logger.warning("Config warning: %s", warning)

    # Extract API server config
    api_server = config.get("platforms", {}).get("api_server", {})
    api_key = api_server.get("extra", {}).get("key", "")
    port = api_server.get("extra", {}).get("port", DEFAULT_GATEWAY_PORT)

    if not api_key:
        logger.warning("No API key configured - mobile app will fail to authenticate")

    # Detect IP
    ip = get_tailscale_ip()

    # Read compression preference
    compression_enabled = config.get("compression", {}).get("enabled", True)

    return {
        "url": f"http://{ip}:{port}",
        "api_key": api_key,
        "context_compression": compression_enabled,
        "tailscale_ip": ip,
        "version": PLUGIN_VERSION,
    }


def generate_qr_svg(data: dict) -> str:
    """Generate SVG QR code from connection data.

    Args:
        data: Connection configuration dictionary.

    Returns:
        SVG string of QR code.

    Raises:
        QRConfigError: If qrcode library not available.
    """
    if qrcode is None:
        raise QRConfigError(
            "qrcode library not found. Install with: pip install qrcode"
        )

    qr_json = json.dumps(data, separators=(",", ":"))

    qr = qrcode.QRCode(
        version=1,
        error_correction=getattr(qrcode.constants, QR_ERROR_CORRECTION),
        box_size=QR_BOX_SIZE,
        border=QR_BORDER,
    )
    qr.add_data(qr_json)
    qr.make(fit=True)

    img = qr.make_image(image_factory=SvgPathImage)
    return img.to_string(encoding="unicode")


def get_html_template(qr_svg: str, data: dict) -> str:
    """Generate HTML page for QR code display.

    Args:
        qr_svg: SVG string of QR code.
        data: Connection configuration.

    Returns:
        Complete HTML document string.
    """
    api_key = data.get("api_key", "")
    display_key = (
        f"{api_key[:20]}..." if len(api_key) > 20 else (api_key or "[MISSING]")
    )

    warning_html = ""
    if not api_key:
        warning_html = (
            "<div class='warning'>⚠️ No API key found. "
            "Set 'platforms.api_server.extra.key' in config.yaml</div>"
        )

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Hermes Mobile Connection</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #1a1a1a; color: #e0e0e0; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; text-align: center; }}
        .card {{ background-color: #2d2d2d; border-radius: 16px; padding: 30px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); max-width: 400px; width: 100%; }}
        .qr-container {{ background: white; padding: 20px; border-radius: 12px; margin: 20px 0; display: flex; justify-content: center; }}
        .qr-container svg {{ width: 100%; height: auto; max-width: 300px; }}
        h1 {{ margin: 0 0 10px 0; color: #fff; font-size: 24px; }}
        p {{ color: #b0b0b0; line-height: 1.5; margin: 5px 0; }}
        .detail {{ background: #3d3d3d; padding: 10px; border-radius: 8px; margin-top: 15px; text-align: left; font-family: monospace; font-size: 13px; word-break: break-all; }}
        .label {{ color: #888; margin-bottom: 4px; display: block; font-size: 11px; text-transform: uppercase; }}
        .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
        .warning {{ color: #ffab00; font-size: 12px; margin-top: 10px; border: 1px solid #ffab0033; padding: 8px; border-radius: 6px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Connect Hermes Mobile</h1>
        <p>Scan this QR code in the Hermes Mobile app settings to connect.</p>

        <div class="qr-container">
            {qr_svg}
        </div>

        <div class="detail">
            <span class="label">Server URL</span>
            {data['url']}
        </div>
        <div class="detail">
            <span class="label">API Key</span>
            {display_key}
        </div>
        <div class="detail">
            <span class="label">Context Compression</span>
            {"Enabled" if data['context_compression'] else "Disabled"}
        </div>

        {warning_html}
    </div>

    <div class="footer">
        Plugin v{PLUGIN_VERSION} &bull; Generated for {data['tailscale_ip']}<br>
        Keep this QR code private.
    </div>
</body>
</html>"""


async def generate_qr(config: Optional[dict] = None, output_dir: Optional[Path] = None,
                      open_browser: bool = True, save_file: bool = True) -> Path:
    """Generate QR code for Hermes Mobile connection.

    Args:
        config: Hermes configuration dictionary. If None, loads from default path.
        output_dir: Directory to save HTML file. Defaults to plugin directory.
        open_browser: Whether to open browser after generating.
        save_file: Whether to save HTML file.

    Returns:
        Path to generated HTML file.

    Raises:
        QRConfigError: If QR generation fails.
    """
    if config is None:
        config = load_hermes_config()

    if output_dir is None:
        output_dir = Path.home() / ".hermes" / "plugins" / "hermes-mobile-qr"
    
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build connection config
    data = build_connection_config(config)

    # Generate QR SVG
    qr_svg = generate_qr_svg(data)

    # Generate HTML
    html = get_html_template(qr_svg, data)

    # Save file
    output_path = output_dir / QR_HTML_FILE
    if save_file:
        output_path.write_text(html, encoding="utf-8")
        logger.info("QR code HTML saved to: %s", output_path)

        if open_browser:
            webbrowser.open(f"file://{output_path.absolute()}")

    return output_path


def print_connection_details(config: Optional[dict] = None) -> None:
    """Print connection details to stdout.

    Args:
        config: Hermes configuration. If None, loads from default.
    """
    if config is None:
        config = load_hermes_config()

    data = build_connection_config(config)
    
    print("\n" + "="*50)
    print("📱 Hermes Mobile Connection Details")
    print("="*50)
    print(f"Server URL:  {data['url']}")
    print(f"API Key:     {data['api_key'][:20]}...")
    print(f"Compression: {'Enabled' if data['context_compression'] else 'Disabled'}")
    print(f"Version:     {data['version']}")
    print("="*50 + "\n")