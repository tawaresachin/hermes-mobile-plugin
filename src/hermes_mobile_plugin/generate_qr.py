"""QR code generation for Hermes Mobile App connection."""

import os
import subprocess
import webbrowser
import logging
from pathlib import Path
from urllib.parse import quote
import json

logger = logging.getLogger(__name__)

try:
    import qrcode
    from qrcode.image.svg import SvgPathImage
except ImportError:
    qrcode = None
    SvgPathImage = None

VERSION = "1.0.1"


def get_tailscale_ip() -> str:
    """Get Tailscale IP address or fallback to WiFi IP."""
    # Method 1: tailscale CLI
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
        ip = result.stdout.strip()
        if ip:
            logger.debug(f"Tailscale IP detected via CLI: {ip}")
            return ip
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Method 2: Look for 100.x.x.x IPs in interface list (Standard Tailscale range)
    try:
        # Linux/macOS/Android
        for cmd in [["ip", "addr"], ["ifconfig"]]:
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                for line in res.stdout.splitlines():
                    if "inet " in line and " 100." in line:
                        parts = line.strip().split()
                        # handle 'inet 100.x.x.x/32' or 'inet 100.x.x.x netmask...'
                        ip_part = parts[1].split("/")[0]
                        logger.debug(f"Tailscale IP detected via {cmd[0]}: {ip_part}")
                        return ip_part
            except (subprocess.SubprocessError, FileNotFoundError):
                continue
    except Exception as e:
        logger.debug(f"Interface scanning failed: {e}")

    # Method 3: Default Local IP fallback
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        # doesn't even have to be reachable
        s.connect(("8.8.8.8", 1))
        local_ip = s.getsockname()[0]
        s.close()
        logger.debug(f"Falling back to local IP: {local_ip}")
        return local_ip
    except Exception:
        return "127.0.0.1"


def build_connection_config(config: dict) -> dict:
    """Extract connection details from Hermes config."""
    api_server = config.get("platforms", {}).get("api_server", {})
    api_key = api_server.get("extra", {}).get("key", "")

    if not api_key:
        logger.warning(
            "No API key found in platforms.api_server.extra.key. "
            "Mobile app will fail to authenticate."
        )

    port = api_server.get("extra", {}).get("port", 8642)
    ip = get_tailscale_ip()

    # Read context compression preference from config
    compression_enabled = config.get("compression", {}).get("enabled", True)

    return {
        "url": f"http://{ip}:{port}",
        "api_key": api_key,
        "context_compression": compression_enabled,
        "tailscale_ip": ip,
        "version": VERSION,
    }


def generate_qr_code(data: dict) -> str:
    """Generate SVG QR code from data dict."""
    if qrcode is None:
        raise ImportError("qrcode library not found. Install with: pip install qrcode")

    qr_json = json.dumps(data)
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_json)
    qr.make(fit=True)

    img = qr.make_image(image_factory=SvgPathImage)
    return img.to_string(encoding="unicode")


def get_html_template(qr_svg: str, data: dict) -> str:
    """Build the HTML page to display the QR code."""
    safe_key = data["api_key"]
    display_key = (
        (safe_key[:20] + "...") if len(safe_key) > 20 else (safe_key or "[MISSING]")
    )

    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>Hermes Mobile Connection</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #1a1a1a; color: #e0e0e0; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; text-align: center; }}
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

        {"<div class='warning'>⚠️ No API key found in config. Connection will fail. Set 'platforms.api_server.extra.key' in config.yaml</div>" if not data['api_key'] else ""}
    </div>

    <div class="footer">
        Plugin v{VERSION} &bull; Generated for {data['tailscale_ip']}<br>
        Keep this QR code private.
    </div>
</body>
</html>
"""


async def generate_and_open_qr(
    config: dict, output_dir: Path, open_browser: bool = True, save_file: bool = True
) -> Path:
    """Main entry point for generating and optionally displaying the QR."""
    data = build_connection_config(config)
    qr_svg = generate_qr_code(data)
    html = get_html_template(qr_svg, data)

    output_path = output_dir / "qr_code.html"

    if save_file:
        output_path.write_text(html)
        logger.info(f"QR code HTML saved to: {output_path}")

        if open_browser:
            webbrowser.open(f"file://{output_path.absolute()}")

    return output_path
