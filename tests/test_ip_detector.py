"""Tests for Hermes Mobile Plugin IP detector module."""

from unittest.mock import patch, MagicMock
import json
import subprocess

from hermes_mobile_plugin.ip_detector import (
    get_ip_candidates,
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

    def test_parse_from_windows_ipconfig_output(self):
        """Windows: `ipconfig` (no `inet` keyword) — the Tailscale adapter is
        only visible there when the `tailscale` CLI is missing from PATH, so
        the parser must not depend on the iproute2/ifconfig wording."""
        output = """
Windows IP Configuration

Ethernet adapter Tailscale:

   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 100.101.102.103
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . :

Ethernet adapter Ethernet:

   IPv4 Address. . . . . . . . . . . : 192.168.1.100
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
"""
        result = _parse_tailscale_ip_from_output(output)
        assert result == "100.101.102.103"

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


class TestGetIPCandidates:
    """Stopped Tailscale must never produce a 100.x pairing candidate."""

    @staticmethod
    def _status(state: str) -> str:
        return json.dumps({
            "BackendState": state,
            "Self": {"TailscaleIPs": ["100.116.192.1"]},
        })

    @staticmethod
    def _lan_socket(ip: str = "192.168.1.3"):
        sock = MagicMock()
        sock.getsockname.return_value = (ip,)
        manager = MagicMock()
        manager.__enter__ = MagicMock(return_value=sock)
        manager.__exit__ = MagicMock(return_value=False)
        return manager

    def test_stopped_tailscale_uses_lan_and_no_100x(self):
        """The live defect: `ip -4` lies, LAN must be the primary."""
        with patch(
            "hermes_mobile_plugin.ip_detector._run_command",
            side_effect=[self._status("Stopped"), "100.116.192.1\n"],
        ), patch("socket.socket", return_value=self._lan_socket()):
            candidates = get_ip_candidates()

        assert candidates == [{
            "ip": "192.168.1.3", "kind": "lan", "label": "Same Wi-Fi",
        }]
        assert "100." not in str(candidates)

    def test_running_tailscale_primary_with_lan_alternate(self):
        with patch(
            "hermes_mobile_plugin.ip_detector._run_command",
            side_effect=[self._status("Running"), "100.116.192.1\n"],
        ), patch("socket.socket", return_value=self._lan_socket()):
            candidates = get_ip_candidates()

        assert [c["kind"] for c in candidates] == ["tailscale", "lan"]
        assert candidates[0]["ip"] == "100.116.192.1"
        assert candidates[1]["ip"] == "192.168.1.3"
        assert candidates[0]["label"] == "Tailscale"
        assert candidates[1]["label"] == "Same Wi-Fi"

    def test_no_lan_falls_back_to_loopback_with_warning(self, caplog):
        with patch(
            "hermes_mobile_plugin.ip_detector._run_command",
            return_value=self._status("Stopped"),
        ), patch("socket.socket", side_effect=OSError("no route")):
            with caplog.at_level("WARNING"):
                candidates = get_ip_candidates()

        assert candidates == [{
            "ip": "127.0.0.1", "kind": "loopback", "label": "This computer only",
        }]
        assert any("loopback" in r.message.lower() for r in caplog.records)

    def test_unreadable_status_does_not_trust_tailscale_ip(self):
        """A CLI that prints the IP as its status output is not proof of life."""
        with patch(
            "hermes_mobile_plugin.ip_detector._run_command",
            return_value="100.116.192.1\n",
        ), patch("socket.socket", return_value=self._lan_socket()):
            candidates = get_ip_candidates()

        assert [c["kind"] for c in candidates] == ["lan"]


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