import pytest
from pathlib import Path
from hermes_mobile_plugin.generate_qr import (
    get_tailscale_ip,
    build_connection_config,
    generate_qr_code,
)


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


def test_generate_qr_code():
    """Test that QR generation returns an SVG string."""
    data = {
        "url": "http://127.0.0.1:8642",
        "api_key": "test",
        "context_compression": True,
        "tailscale_ip": "127.0.0.1",
        "version": "1.0.1",
    }
    svg = generate_qr_code(data)
    assert "<svg" in svg
    assert "</svg>" in svg
