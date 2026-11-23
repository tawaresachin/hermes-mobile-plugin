"""QR code generation for Hermes Mobile App connection."""

from html import escape
from pathlib import Path
from urllib.parse import quote
import logging
import webbrowser
from typing import Optional

try:
    import qrcode
    from qrcode.image.svg import SvgPathImage
except ImportError:
    qrcode = None
    SvgPathImage = None

from .config import load_hermes_config, validate_config
from .constants import (
    QR_BORDER,
    QR_BOX_SIZE,
    QR_ERROR_CORRECTION,
    QR_HTML_FILE,
    API_KEY_DISPLAY_TRUNCATION,
    PLUGIN_VERSION,
    TAILSCALE_IP_PREFIX,
    resolve_gateway_port,
)
from .ip_detector import get_ip_candidates, is_tailscale_running

logger = logging.getLogger(__name__)


class QRConfigError(Exception):
    """Raised when QR configuration is invalid."""
    pass


def build_connection_config(config: dict, ip_override: Optional[str] = None) -> dict:
    """Build connection configuration from Hermes config.

    Args:
        config: Hermes Agent configuration dictionary.
        ip_override: Force a specific IP address (e.g., Tailscale IP).

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
    port = api_server.get("extra", {}).get("port")
    if not isinstance(port, int):
        # Keep the QR's fallback in lock-step with the supervisor/status:
        # config extra.port > API_SERVER_PORT env > default (8642).
        port = resolve_gateway_port(config)

    if not api_key:
        logger.warning("No API key configured - mobile app will fail to authenticate")

    # Detect IP candidates (or honor an explicit override). One source of
    # truth feeds both the page and the CLI summary — never two detections.
    if ip_override:
        ip = ip_override.strip()
        candidates = [{"ip": ip, "kind": "manual", "label": "Specified IP"}]
        logger.debug("Using overridden IP: %s", ip)
    else:
        candidates = get_ip_candidates()
        ip = candidates[0]["ip"]

    warnings: list[str] = []
    if candidates[0]["kind"] == "loopback":
        warnings.append(
            "Only the loopback address (127.0.0.1) was found — this QR works "
            "on this computer only; a phone cannot connect."
        )
    # Detection never advertises a stopped Tailscale IP; an explicit --ip can
    # still force one, so warn rather than silently hand the phone a dead URL.
    if ip.startswith(TAILSCALE_IP_PREFIX) and not is_tailscale_running():
        warnings.append(
            "Tailscale is stopped — this address may be unreachable until it starts."
        )

    # Read compression preference
    compression_enabled = config.get("compression", {}).get("enabled", True)

    return {
        "url": f"http://{ip}:{port}",
        "api_key": api_key,
        "context_compression": compression_enabled,
        "tailscale_ip": ip,  # legacy field: now the primary candidate
        "port": port,
        "candidates": candidates,
        "primary": candidates[0],
        "warnings": warnings,
        "version": PLUGIN_VERSION,
    }


def generate_qr_svg(data: dict, url: Optional[str] = None) -> str:
    """Generate SVG QR code from connection data.

    Args:
        data: Connection configuration dictionary.
        url: Optional URL to encode (defaults to the data's primary URL). Used
            for the alternate-candidate QR on the same connection page.

    Returns:
        SVG string of QR code.

    Raises:
        QRConfigError: If qrcode library not available.
    """
    if qrcode is None:
        raise QRConfigError(
            "qrcode library not found. Install with: pip install qrcode"
        )

    # URI-scheme payload; both values percent-encoded so special characters
    # (e.g. a key containing '&') cannot corrupt the query parse on the phone.
    # ponytail: ship version too so the phone's About row can render it
    # offline. Backward-compatible: older app builds ignore unknown params.
    qr_uri = "hermes://connect?url={}&key={}&version={}".format(
        quote(str(url if url is not None else data["url"]), safe=""),
        quote(str(data["api_key"]), safe=""),
        quote(str(data.get("version", "")), safe=""),
    )

    qr = qrcode.QRCode(
        version=1,
        error_correction=getattr(qrcode.constants, QR_ERROR_CORRECTION),
        box_size=QR_BOX_SIZE,
        border=QR_BORDER,
    )
    qr.add_data(qr_uri)
    qr.make(fit=True)

    img = qr.make_image(image_factory=SvgPathImage)
    return img.to_string(encoding="unicode")


def get_html_template(
    qr_svg: str, data: dict, alternate_qr_svg: Optional[str] = None
) -> str:
    """Generate HTML page for QR code display.

    Args:
        qr_svg: SVG string of the primary (big) QR code.
        data: Connection configuration.
        alternate_qr_svg: Optional SVG for a second reachable address.

    Returns:
        Complete, self-contained HTML document string.
    """
    api_key = data.get("api_key", "")
    display_key = (
        f"{api_key[:API_KEY_DISPLAY_TRUNCATION]}..."
        if len(api_key) > API_KEY_DISPLAY_TRUNCATION
        else (api_key or "[MISSING]")
    )
    # Escape every config-derived string before HTML interpolation — a key
    # or hostname containing markup must not inject into this page.
    url_h = escape(str(data["url"]), quote=True)
    key_h = escape(display_key, quote=True)
    ip_h = escape(str(data["tailscale_ip"]), quote=True)

    primary = data.get("primary") or {
        "ip": data.get("tailscale_ip", ""),
        "label": "Server",
    }
    primary_label_h = escape(str(primary.get("label", "Primary")), quote=True)
    primary_ip_h = escape(str(primary.get("ip", "")), quote=True)

    candidates = data.get("candidates") or []
    alternate_html = ""
    if alternate_qr_svg and len(candidates) > 1:
        alt = candidates[1]
        alt_label_h = escape(str(alt.get("label", "Alternate")), quote=True)
        alt_url_h = escape(
            f"http://{alt['ip']}:{data.get('port', 8642)}", quote=True
        )
        alternate_html = f"""
        <div class="alternate">
            <div class="qr-label">ALSO WORKS ON &middot; {alt_label_h}</div>
            <div class="qr-container alt-qr">{alternate_qr_svg}</div>
            <div class="detail">
                <span class="label">{alt_label_h} URL</span>
                {alt_url_h}
            </div>
        </div>"""

    warning_html = ""
    if not api_key:
        warning_html = (
            "<div class='warning'>⚠️ No API key found. "
            "Set 'platforms.api_server.extra.key' in config.yaml</div>"
        )
    for warning in data.get("warnings", []):
        warning_html += (
            f"<div class='warning'>⚠️ {escape(str(warning), quote=True)}</div>"
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
        .qr-label {{ color: #8ab4f8; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }}
        .alternate {{ border-top: 1px solid #444; margin-top: 24px; padding-top: 18px; }}
        .alt-qr svg {{ max-width: 180px; }}
        h1 {{ margin: 0 0 10px 0; color: #fff; font-size: 24px; }}
        p {{ color: #b0b0b0; line-height: 1.5; margin: 5px 0; }}
        .primary-note {{ color: #8ab4f8; font-size: 13px; margin-top: 10px; }}
        .detail {{ background: #3d3d3d; padding: 10px; border-radius: 8px; margin-top: 15px; text-align: left; font-family: monospace; font-size: 13px; word-break: break-all; }}
        .label {{ color: #888; margin-bottom: 4px; display: block; font-size: 11px; text-transform: uppercase; }}
        .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
        .warning {{ color: #ffab00; font-size: 12px; margin-top: 10px; border: 1px solid #ffab0033; padding: 8px; border-radius: 6px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Connect Hermes Mobile</h1>
        <p>Scan the QR code below in the Hermes Mobile app settings to connect.</p>
        <p class="primary-note">Primary connection: {primary_label_h} ({primary_ip_h})</p>

        <div class="qr-label">PRIMARY &middot; {primary_label_h}</div>
        <div class="qr-container">
            {qr_svg}
        </div>

        <div class="detail">
            <span class="label">Server URL</span>
            {url_h}
        </div>
        <div class="detail">
            <span class="label">API Key</span>
            {key_h}
        </div>
        <div class="detail">
            <span class="label">Context Compression</span>
            {"Enabled" if data['context_compression'] else "Disabled"}
        </div>

        {warning_html}
        {alternate_html}
    </div>

    <div class="footer">
        Plugin v{PLUGIN_VERSION} &bull; Primary connection: {primary_label_h} ({ip_h})<br>
        Keep these QR codes private.
    </div>
</body>
</html>"""


async def generate_qr(config: Optional[dict] = None, output_dir: Optional[Path] = None,
                      open_browser: bool = True, save_file: bool = True,
                      ip_override: Optional[str] = None) -> Path:
    """Generate QR code for Hermes Mobile connection.

    Args:
        config: Hermes configuration dictionary. If None, loads from default path.
        output_dir: Directory to save HTML file. Defaults to plugin directory.
        open_browser: Whether to open browser after generating.
        save_file: Whether to save HTML file.
        ip_override: Force a specific IP address.

    Returns:
        Path to generated HTML file.

    Raises:
        QRConfigError: If QR generation fails.
    """
    if config is None:
        config = load_hermes_config()

    if output_dir is None:
        # HERMES_HOME, NOT Path.home()/".hermes" — hermes-agent's home is
        # profile-aware (AppData\Local\hermes on Windows, ~/.hermes on
        # Linux/Termux). Hardcoding the fallback wrote the QR into a home the
        # gateway never reads.
        from .constants import PLUGIN_DIR
        output_dir = PLUGIN_DIR

    output_dir.mkdir(parents=True, exist_ok=True)

    # Build connection config
    data = build_connection_config(config, ip_override=ip_override)

    # Generate the primary QR, plus a clearly labelled alternate when the
    # host has a second reachable address (e.g. Wi-Fi + running Tailscale).
    qr_svg = generate_qr_svg(data)
    alternate_qr_svg = None
    if len(data.get("candidates", [])) > 1:
        alt = data["candidates"][1]
        alternate_qr_svg = generate_qr_svg(
            data, url=f"http://{alt['ip']}:{data['port']}"
        )

    # Generate HTML
    html = get_html_template(qr_svg, data, alternate_qr_svg=alternate_qr_svg)

    # Save file
    output_path = output_dir / QR_HTML_FILE
    if save_file:
        output_path.write_text(html, encoding="utf-8")
        try:
            # The page embeds the full API key — keep it owner-only.
            output_path.chmod(0o600)
        except OSError:
            pass
        logger.info("QR code HTML saved to: %s", output_path)

        if open_browser:
            webbrowser.open(Path(output_path).absolute().as_uri())

    return output_path


def print_connection_details(
    config: Optional[dict] = None, ip_override: Optional[str] = None
) -> None:
    """Print the exact URLs the QR page encodes to stdout.

    Args:
        config: Hermes configuration. If None, loads from default.
        ip_override: Same --ip value used to generate the page.
    """
    if config is None:
        config = load_hermes_config()

    data = build_connection_config(config, ip_override=ip_override)

    print("\n" + "=" * 50)
    print("📱 Hermes Mobile Connection Details")
    print("=" * 50)
    for index, candidate in enumerate(data["candidates"]):
        role = "Primary" if index == 0 else "Alternate"
        url = f"http://{candidate['ip']}:{data['port']}"
        print(f"{role} ({candidate['label']}): {url}")
    print(f"API Key:     {data['api_key'][:API_KEY_DISPLAY_TRUNCATION]}...")
    print(f"Compression: {'Enabled' if data['context_compression'] else 'Disabled'}")
    print(f"Version:     {data['version']}")
    for warning in data["warnings"]:
        print(f"⚠️  {warning}")
    print("=" * 50 + "\n")