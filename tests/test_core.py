import pytest
from pathlib import Path
from hermes_mobile_plugin.ip_detector import get_tailscale_ip
from hermes_mobile_plugin.qr_generator import (
    build_connection_config,
    generate_qr_svg,
)
from hermes_mobile_plugin.constants import PLUGIN_VERSION


def test_get_tailscale_ip():
    """Test that we always get some IP string."""
    ip = get_tailscale_ip()
    assert isinstance(ip, str)
    assert len(ip.split(".")) == 4


def test_build_connection_config_empty():
    """Test build config with empty dict."""
    config = {}
    data = build_connection_config(config)
    assert "url" in data
    assert data["api_key"] == ""
    assert data["context_compression"] is True
    assert data["version"] == PLUGIN_VERSION


def test_build_connection_config_with_data():
    """Test build config with provided data."""
    config = {
        "platforms": {
            "api_server": {"extra": {"key": "test-key", "port": 9999}}
        },
        "compression": {"enabled": False},
    }
    data = build_connection_config(config)
    assert ":9999" in data["url"]
    assert data["api_key"] == "test-key"
    assert data["context_compression"] is False


def test_build_connection_config_ip_override():
    config = {"platforms": {"api_server": {"extra": {"key": "k"}}}}
    data = build_connection_config(config, ip_override="100.1.2.3")
    assert data["url"] == "http://100.1.2.3:8642"
    assert data["tailscale_ip"] == "100.1.2.3"


def test_generate_qr_svg():
    """Test that QR generation returns an SVG string."""
    data = {
        "url": "http://127.0.0.1:8642",
        "api_key": "test",
        "context_compression": True,
        "tailscale_ip": "127.0.0.1",
        "version": PLUGIN_VERSION,
    }
    svg = generate_qr_svg(data)
    assert "<svg" in svg
    assert "</svg>" in svg


def test_qr_uri_uses_connect_scheme_and_encodes(monkeypatch):
    """QR payload must be the hermes:// URI the app parses, with both
    values percent-encoded so an '&' inside a key cannot corrupt the query."""
    captured = {}

    class FakeQR:
        version = None

        def __init__(self, **kwargs):
            pass

        def add_data(self, d):
            captured["payload"] = d

        def make(self, fit=True):
            pass

        def make_image(self, image_factory=None):
            class Img:
                def to_string(self, encoding=None):
                    return "<svg></svg>"
            return Img()

    import qrcode as qrcode_module
    monkeypatch.setattr("hermes_mobile_plugin.qr_generator.qrcode.QRCode", FakeQR)
    data = {
        "url": "http://100.1.2.3:8642",
        "api_key": "key&with=chars",
        "context_compression": True,
        "tailscale_ip": "100.1.2.3",
        "version": PLUGIN_VERSION,
    }
    generate_qr_svg(data)
    payload = captured["payload"]
    assert payload.startswith("hermes://connect?url=")
    # exactly one raw '&' separator (between url= and key=); the key's own
    # '&' must be encoded
    assert payload.count("&") == 1
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(payload)
    qs = parse_qs(parsed.query)
    assert qs["url"] == ["http://100.1.2.3:8642"]
    assert qs["key"] == ["key&with=chars"]
