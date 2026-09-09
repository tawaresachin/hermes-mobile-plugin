from hermes_mobile_plugin.constants import PLUGIN_VERSION
"""Tests for Hermes Mobile Plugin QR generator module."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import json

from hermes_mobile_plugin.qr_generator import (
    build_connection_config,
    generate_qr_svg,
    get_html_template,
    generate_qr,
    QRConfigError,
)


class TestBuildConnectionConfig:
    """Tests for build_connection_config function."""

    def test_basic_config(self):
        """Test building config with basic settings."""
        config = {
            "platforms": {
                "api_server": {
                    "extra": {
                        "key": "test-api-key-12345",
                        "port": 8642
                    }
                }
            },
            "compression": {
                "enabled": True
            }
        }
        
        with patch('hermes_mobile_plugin.qr_generator.get_tailscale_ip', return_value='100.89.25.56'):
            result = build_connection_config(config)
        
        assert result['url'] == 'http://100.89.25.56:8642'
        assert result['api_key'] == 'test-api-key-12345'
        assert result['context_compression'] is True
        assert result['tailscale_ip'] == '100.89.25.56'
        assert result['version'] == PLUGIN_VERSION

    def test_missing_api_key(self):
        """Test config with missing API key."""
        config = {
            "platforms": {
                "api_server": {
                    "extra": {}
                }
            }
        }
        
        with patch('hermes_mobile_plugin.qr_generator.get_tailscale_ip', return_value='127.0.0.1'):
            result = build_connection_config(config)
        
        assert result['api_key'] == ''
        assert result['url'] == 'http://127.0.0.1:8642'

    def test_default_port(self):
        """Test default port when not specified."""
        config = {
            "platforms": {
                "api_server": {
                    "extra": {
                        "key": "test-key"
                    }
                }
            }
        }
        
        with patch('hermes_mobile_plugin.qr_generator.get_tailscale_ip', return_value='127.0.0.1'):
            result = build_connection_config(config)
        
        assert ':8642' in result['url']

    def test_compression_disabled(self):
        """Test when compression is disabled."""
        config = {
            "platforms": {
                "api_server": {
                    "extra": {
                        "key": "test-key",
                        "port": 9000
                    }
                }
            },
            "compression": {
                "enabled": False
            }
        }
        
        with patch('hermes_mobile_plugin.qr_generator.get_tailscale_ip', return_value='192.168.1.1'):
            result = build_connection_config(config)
        
        assert result['context_compression'] is False
        assert result['url'] == 'http://192.168.1.1:9000'


class TestGenerateQRSvg:
    """Tests for generate_qr_svg function."""

    def test_generate_valid_svg(self):
        """Test generating valid SVG QR code."""
        data = {
            "url": "http://100.89.25.56:8642",
            "api_key": "test-key",
            "context_compression": True,
            "tailscale_ip": "100.89.25.56",
            "version": PLUGIN_VERSION
        }
        
        svg = generate_qr_svg(data)
        
        assert '<svg' in svg
        assert '</svg>' in svg
        assert 'xmlns' in svg

    def test_missing_qrcode_library(self):
        """Test error when qrcode library is not available."""
        with patch('hermes_mobile_plugin.qr_generator.qrcode', None):
            with pytest.raises(QRConfigError, match="qrcode library not found"):
                generate_qr_svg({})


class TestGetHtmlTemplate:
    """Tests for get_html_template function."""

    def test_basic_template(self):
        """Test HTML template generation."""
        qr_svg = "<svg>test</svg>"
        data = {
            "url": "http://100.89.25.56:8642",
            "api_key": "test-key-12345",
            "context_compression": True,
            "tailscale_ip": "100.89.25.56",
            "version": PLUGIN_VERSION
        }
        
        html = get_html_template(qr_svg, data)
        
        assert "<!DOCTYPE html>" in html
        assert qr_svg in html
        assert "http://100.89.25.56:8642" in html
        assert "Enabled" in html
        "Plugin v" + PLUGIN_VERSION in html

    def test_missing_api_key_warning(self):
        """Test warning when API key is missing."""
        qr_svg = "<svg>test</svg>"
        data = {
            "url": "http://127.0.0.1:8642",
            "api_key": "",
            "context_compression": False,
            "tailscale_ip": "127.0.0.1",
            "version": PLUGIN_VERSION
        }
        
        html = get_html_template(qr_svg, data)
        
        assert "No API key found" in html
        assert "Disabled" in html


@pytest.mark.asyncio
class TestGenerateQR:
    """Tests for generate_qr async function."""

    @patch('hermes_mobile_plugin.qr_generator.build_connection_config')
    @patch('hermes_mobile_plugin.qr_generator.generate_qr_svg')
    @patch('hermes_mobile_plugin.qr_generator.get_html_template')
    async def test_generate_qr_saves_file(self, mock_html, mock_svg, mock_config, tmp_path):
        """Test that QR generation saves HTML file."""
        mock_config.return_value = {
            "url": "http://test:8642",
            "api_key": "test-key",
            "context_compression": True,
            "tailscale_ip": "100.89.25.56",
            "version": PLUGIN_VERSION
        }
        mock_svg.return_value = "<svg>test</svg>"
        mock_html.return_value = "<html>test</html>"
        
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        
        result = await generate_qr(
            config={"test": True},
            output_dir=output_dir,
            open_browser=False,
            save_file=True
        )
        
        assert result.exists()
        assert result.name == "qr_code.html"
        assert "test" in result.read_text()

    async def test_generate_qr_no_save(self, tmp_path):
        """Test QR generation without saving file."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        
        result = await generate_qr(
            config={"test": True},
            output_dir=output_dir,
            open_browser=False,
            save_file=False
        )
        
        # Should return path even if not saved
        assert result == output_dir / "qr_code.html"