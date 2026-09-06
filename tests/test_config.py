"""Tests for Hermes Mobile Plugin configuration module."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import yaml

from hermes_mobile_plugin.config import (
    load_hermes_config,
    validate_config,
    get_plugin_config,
    ConfigError,
)


class TestLoadHermesConfig:
    """Tests for load_hermes_config function."""

    def test_load_valid_config(self, tmp_path):
        """Test loading a valid configuration file."""
        config_data = {
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
        
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))
        
        with patch('hermes_mobile_plugin.config.CONFIG_PATH', config_file):
            result = load_hermes_config()
            
        assert result == config_data
        assert result["platforms"]["api_server"]["extra"]["key"] == "test-api-key-12345"

    def test_missing_config_file(self, tmp_path):
        """Test loading when config file doesn't exist."""
        config_file = tmp_path / "nonexistent.yaml"
        
        with patch('hermes_mobile_plugin.config.CONFIG_PATH', config_file):
            result = load_hermes_config()
            
        assert result == {}

    def test_invalid_yaml(self, tmp_path):
        """Test loading invalid YAML file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("invalid: yaml: {{")
        
        with patch('hermes_mobile_plugin.config.CONFIG_PATH', config_file):
            with pytest.raises(ConfigError):
                load_hermes_config()

    def test_empty_yaml(self, tmp_path):
        """Test loading empty YAML file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        
        with patch('hermes_mobile_plugin.config.CONFIG_PATH', config_file):
            result = load_hermes_config()
            
        assert result == {}


class TestValidateConfig:
    """Tests for validate_config function."""

    def test_valid_config(self):
        """Test validation of valid configuration."""
        config = {
            "platforms": {
                "api_server": {
                    "extra": {
                        "key": "a" * 64,
                        "port": 8642
                    }
                }
            },
            "compression": {
                "enabled": True
            }
        }
        
        warnings = validate_config(config)
        assert warnings == []

    def test_missing_api_key(self):
        """Test validation when API key is missing."""
        config = {
            "platforms": {
                "api_server": {
                    "extra": {}
                }
            }
        }
        
        warnings = validate_config(config)
        assert len(warnings) == 1
        assert "No API key found" in warnings[0]

    def test_short_api_key(self):
        """Test validation with short API key."""
        config = {
            "platforms": {
                "api_server": {
                    "extra": {
                        "key": "short"
                    }
                }
            }
        }
        
        warnings = validate_config(config)
        assert len(warnings) == 1
        assert "too short" in warnings[0]

    def test_invalid_port_type(self):
        """Test validation with invalid port type."""
        config = {
            "platforms": {
                "api_server": {
                    "extra": {
                        "key": "a" * 64,
                        "port": "not-a-number"
                    }
                }
            }
        }
        
        warnings = validate_config(config)
        assert len(warnings) == 1
        assert "Invalid port" in warnings[0]


class TestGetPluginConfig:
    """Tests for get_plugin_config function."""

    def test_default_config(self):
        """Test getting default plugin configuration."""
        config = {}
        result = get_plugin_config(config)
        
        assert result == {"auto_generate": True, "auto_start_supervisor": True}

    def test_custom_config(self):
        """Test getting custom plugin configuration."""
        config = {
            "plugins": {
                "hermes-mobile-qr": {
                    "auto_generate": False,
                    "auto_start_supervisor": False
                }
            }
        }
        result = get_plugin_config(config)
        
        assert result["auto_generate"] is False
        assert result["auto_start_supervisor"] is False