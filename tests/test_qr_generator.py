"""Tests for Hermes Mobile Plugin QR generator module."""

from unittest.mock import patch

import pytest

from hermes_mobile_plugin.constants import PLUGIN_VERSION
from hermes_mobile_plugin.qr_generator import (
    build_connection_config,
    generate_qr,
    generate_qr_svg,
    get_html_template,
    print_connection_details,
    QRConfigError,
)


def _base_config(port=8642, key="test-api-key-12345"):
    return {
        "platforms": {"api_server": {"extra": {"key": key, "port": port}}},
        "compression": {"enabled": True},
    }


def _candidates(primary="192.168.1.3", alternate=None):
    cands = [{"ip": primary, "kind": "lan", "label": "Same Wi-Fi"}]
    if alternate:
        cands.insert(0, {
            "ip": alternate, "kind": "tailscale", "label": "Tailscale",
        })
    return cands


class TestBuildConnectionConfig:
    """Tests for build_connection_config function."""

    def test_basic_config(self):
        with patch(
            "hermes_mobile_plugin.qr_generator.get_ip_candidates",
            return_value=_candidates(alternate="100.89.25.56"),
        ), patch(
            "hermes_mobile_plugin.qr_generator.is_tailscale_running",
            return_value=True,
        ):
            result = build_connection_config(_base_config())

        assert result["url"] == "http://100.89.25.56:8642"
        assert result["api_key"] == "test-api-key-12345"
        assert result["context_compression"] is True
        assert result["tailscale_ip"] == "100.89.25.56"
        assert result["primary"]["kind"] == "tailscale"
        assert [c["kind"] for c in result["candidates"]] == ["tailscale", "lan"]
        assert result["version"] == PLUGIN_VERSION

    def test_missing_api_key(self):
        cands = [{
            "ip": "127.0.0.1", "kind": "loopback", "label": "This computer only",
        }]
        with patch(
            "hermes_mobile_plugin.qr_generator.get_ip_candidates",
            return_value=cands,
        ):
            result = build_connection_config(
                {"platforms": {"api_server": {"extra": {}}}}
            )

        assert result["api_key"] == ""
        assert result["url"] == "http://127.0.0.1:8642"
        assert any("loopback" in w.lower() for w in result["warnings"])

    def test_default_port(self):
        config = {"platforms": {"api_server": {"extra": {"key": "test-key"}}}}
        with patch(
            "hermes_mobile_plugin.qr_generator.get_ip_candidates",
            return_value=_candidates("127.0.0.1"),
        ):
            result = build_connection_config(config)

        assert ":8642" in result["url"]
        assert result["port"] == 8642

    def test_compression_disabled(self):
        config = _base_config(port=9000, key="test-key")
        config["compression"]["enabled"] = False
        with patch(
            "hermes_mobile_plugin.qr_generator.get_ip_candidates",
            return_value=_candidates("192.168.1.1"),
        ):
            result = build_connection_config(config)

        assert result["context_compression"] is False
        assert result["url"] == "http://192.168.1.1:9000"

    def test_stopped_tailscale_never_reaches_url(self):
        """The Round 14 defect: a dead 100.x must not become the primary URL."""
        with patch(
            "hermes_mobile_plugin.qr_generator.get_ip_candidates",
            return_value=_candidates("192.168.1.3"),
        ):
            result = build_connection_config(_base_config())

        assert result["url"] == "http://192.168.1.3:8642"
        assert "100." not in str(result["candidates"])

    def test_ip_override_drives_page_and_summary(self, capsys):
        config = _base_config()
        data = build_connection_config(config, ip_override="10.0.0.9")
        html = get_html_template("<svg></svg>", data)
        print_connection_details(config, ip_override="10.0.0.9")
        out = capsys.readouterr().out

        assert data["url"] == "http://10.0.0.9:8642"
        assert [c["ip"] for c in data["candidates"]] == ["10.0.0.9"]
        assert "http://10.0.0.9:8642" in html
        assert "http://10.0.0.9:8642" in out
        assert "Primary (Specified IP): http://10.0.0.9:8642" in out


class TestGenerateQRSvg:
    """Tests for generate_qr_svg function."""

    def test_generate_valid_svg(self):
        data = {
            "url": "http://100.89.25.56:8642",
            "api_key": "test-key",
            "context_compression": True,
            "tailscale_ip": "100.89.25.56",
            "version": PLUGIN_VERSION,
        }
        svg = generate_qr_svg(data)
        assert "<svg" in svg
        assert "</svg>" in svg
        assert "xmlns" in svg

    def test_alternate_url_is_encoded(self):
        data = {"url": "http://a:8642", "api_key": "k", "version": PLUGIN_VERSION}
        assert generate_qr_svg(data) != generate_qr_svg(
            data, url="http://b:8642"
        )

    def test_missing_qrcode_library(self):
        with patch("hermes_mobile_plugin.qr_generator.qrcode", None):
            with pytest.raises(QRConfigError, match="qrcode library not found"):
                generate_qr_svg({})


class TestGetHtmlTemplate:
    """Tests for get_html_template function."""

    @staticmethod
    def _data(url="http://100.89.25.56:8642", ip="100.89.25.56"):
        return {
            "url": url,
            "api_key": "test-key-12345",
            "context_compression": True,
            "tailscale_ip": ip,
            "port": 8642,
            "primary": {"ip": ip, "kind": "tailscale", "label": "Tailscale"},
            "candidates": [{"ip": ip, "kind": "tailscale", "label": "Tailscale"}],
            "warnings": [],
            "version": PLUGIN_VERSION,
        }

    def test_basic_template(self):
        html = get_html_template("<svg>test</svg>", self._data())

        assert "<!DOCTYPE html>" in html
        assert "<svg>test</svg>" in html
        assert "http://100.89.25.56:8642" in html
        assert "Enabled" in html
        assert "Plugin v" + PLUGIN_VERSION in html
        assert "Primary connection: Tailscale (100.89.25.56)" in html

    def test_missing_api_key_warning(self):
        data = self._data(url="http://127.0.0.1:8642", ip="127.0.0.1")
        data["api_key"] = ""
        data["context_compression"] = False
        html = get_html_template("<svg>test</svg>", data)

        assert "No API key found" in html
        assert "Disabled" in html

    def test_two_candidates_render_two_labelled_qrs(self):
        data = self._data()
        data["candidates"] = _candidates(alternate="100.89.25.56")
        data["primary"] = data["candidates"][0]
        data["url"] = "http://100.89.25.56:8642"
        html = get_html_template(
            "<svg>primary</svg>", data, alternate_qr_svg="<svg>alternate</svg>"
        )

        assert "<svg>primary</svg>" in html
        assert "<svg>alternate</svg>" in html
        assert "Same Wi-Fi" in html
        assert "Tailscale" in html
        assert "http://192.168.1.3:8642" in html
        assert "Primary connection" in html

    def test_stopped_tailscale_page_never_claims_100x(self):
        data = self._data(url="http://192.168.1.3:8642", ip="192.168.1.3")
        data["primary"] = {"ip": "192.168.1.3", "kind": "lan", "label": "Same Wi-Fi"}
        data["candidates"] = _candidates("192.168.1.3")
        html = get_html_template("<svg>lan</svg>", data)

        assert "100." not in html
        assert "Same Wi-Fi" in html


@pytest.mark.asyncio
class TestGenerateQR:
    """Tests for generate_qr async function."""

    @patch("hermes_mobile_plugin.qr_generator.build_connection_config")
    @patch("hermes_mobile_plugin.qr_generator.generate_qr_svg")
    @patch("hermes_mobile_plugin.qr_generator.get_html_template")
    async def test_generate_qr_saves_file(self, mock_html, mock_svg, mock_config, tmp_path):
        mock_config.return_value = {
            "url": "http://test:8642",
            "api_key": "test-key",
            "context_compression": True,
            "tailscale_ip": "100.89.25.56",
            "version": PLUGIN_VERSION,
        }
        mock_svg.return_value = "<svg>test</svg>"
        mock_html.return_value = "<html>test</html>"

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = await generate_qr(
            config={"test": True},
            output_dir=output_dir,
            open_browser=False,
            save_file=True,
        )

        assert result.exists()
        assert result.name == "qr_code.html"
        assert "test" in result.read_text()

    async def test_generate_qr_no_save(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = await generate_qr(
            config={"test": True},
            output_dir=output_dir,
            open_browser=False,
            save_file=False,
        )

        assert result == output_dir / "qr_code.html"

    async def test_page_contains_both_candidate_qrs(self, tmp_path):
        with patch(
            "hermes_mobile_plugin.qr_generator.get_ip_candidates",
            return_value=_candidates(alternate="100.89.25.56"),
        ), patch(
            "hermes_mobile_plugin.qr_generator.is_tailscale_running",
            return_value=True,
        ):
            out = await generate_qr(
                config=_base_config(), output_dir=tmp_path, open_browser=False
            )
        html = out.read_text(encoding="utf-8")

        assert "Tailscale" in html and "Same Wi-Fi" in html
        assert "http://100.89.25.56:8642" in html
        assert "http://192.168.1.3:8642" in html
        assert html.count("<svg") >= 2
