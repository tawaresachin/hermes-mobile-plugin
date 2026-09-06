"""Tests for Hermes Mobile Plugin IP detector module."""

import pytest
from unittest.mock import patch, MagicMock
import subprocess

from hermes_mobile_plugin.ip_detector import (
    get_tailscale_ip,
    is_valid_ip,
    _run_command,
    _parse_tailscale_ip_from_output,
)


class TestRunCommand:
    """Tests for _run_command helper."""

    def test_success(self):
        """Test successful command execution."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout="100.89.25.56\n", returncode=0)
            
            result = _run_command(["tailscale", "ip", "-4"])
            
            assert result == "100.89.25.56"
            mock_run.assert_called_once()

    def test_command_failure(self):
        """Test command failure returns None."""
        with patch('subprocess.run', side_effect=subprocess.SubprocessError("failed")):
            result = _run_command(["nonexistent-command"])
            
            assert result is None

    def test_command_not_found(self):
        """Test command not found returns None."""
        with patch('subprocess.run', side_effect=FileNotFoundError):
            result = _run_command(["nonexistent"])
            
            assert result is None


class TestParseTailscaleIP:
    """Tests for _parse_tailscale_ip_from_output function."""

    def test_parse_from_ip_output(self):
        """Test parsing IP from 'ip addr' output."""
        output = """
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast state UP qlen 1000
    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0
    inet 100.89.25.56/32 scope global tailscale0
"""
        result = _parse_tailscale_ip_from_output(output)
        assert result == "100.89.25.56"

    def test_parse_from_ifconfig_output(self):
        """Test parsing IP from ifconfig output."""
        output = """
tailscale0: flags=31<UP,BROADCAST,RUNNING,MULTICAST> mtu 1280
        inet 100.89.25.56 netmask 0xffffffff
"""
        result = _parse_tailscale_ip_from_output(output)
        assert result == "100.89.25.56"

    def test_no_tailscale_ip(self):
        """Test when no Tailscale IP is present."""
        output = """
eth0: flags=31<UP,BROADCAST,RUNNING,MULTICAST> mtu 1500
        inet 192.168.1.100 netmask 0xffffff00
"""
        result = _parse_tailscale_ip_from_output(output)
        assert result is None

    def test_empty_output(self):
        """Test empty output."""
        result = _parse_tailscale_ip_from_output("")
        assert result is None


class TestGetTailscaleIP:
    """Tests for get_tailscale_ip function."""

    def test_udp_fallback(self):
        """Test UDP socket fallback."""
        with patch('hermes_mobile_plugin.ip_detector._run_command', return_value=None):
            with patch('socket.socket') as mock_socket:
                mock_sock = MagicMock()
                mock_sock.getsockname.return_value = ("192.168.1.100",)
                mock_socket.return_value.__enter__ = MagicMock(return_value=mock_sock)
                mock_socket.return_value.__exit__ = MagicMock(return_value=False)
                
                result = get_tailscale_ip()
                
                assert result == "192.168.1.100"

    def test_loopback_fallback(self):
        """Test loopback fallback when all methods fail."""
        with patch('hermes_mobile_plugin.ip_detector._run_command', return_value=None):
            with patch('socket.socket') as mock_socket:
                mock_socket.side_effect = OSError("failed")
                
                result = get_tailscale_ip()
                
                assert result == "127.0.0.1"


class TestIsValidIP:
    """Tests for is_valid_ip function."""

    def test_valid_ipv4(self):
        """Test valid IPv4 addresses."""
        assert is_valid_ip("192.168.1.1") is True
        assert is_valid_ip("100.89.25.56") is True
        assert is_valid_ip("127.0.0.1") is True

    def test_invalid_ip(self):
        """Test invalid IP addresses."""
        assert is_valid_ip("not-an-ip") is False
        assert is_valid_ip("") is False
        assert is_valid_ip("256.256.256.256") is False
        # Note: "192.168.1" is accepted by socket.inet_aton on some systems
        # We test for clearly invalid inputs
        assert is_valid_ip("abc") is False
        assert is_valid_ip("192.168.1.1.1") is False
        assert is_valid_ip("::1") is False  # IPv6