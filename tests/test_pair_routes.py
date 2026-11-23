"""Round 15 — live pairing page (GET /pair) must be loopback-only.

The page embeds the gateway API key, so it may only ever be served to a
loopback caller; anything else gets a short 403 and the key is never
rendered. The page content must come from the current reachable address
(never a stopped Tailscale address).
"""

from __future__ import annotations

from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from hermes_mobile_plugin import pair_routes
from hermes_mobile_plugin.qr_generator import render_pairing_html


class _Adapter:
    def __init__(self, key: str = "test-key") -> None:
        self.key = key

    def _expected_api_key(self) -> str:
        return self.key


async def _client() -> TestClient:
    app = web.Application()
    app["api_server_adapter"] = _Adapter()
    pair_routes.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _mocked_request(remote: str):
    transport = mock.Mock()
    transport.get_extra_info.return_value = (remote, 51000)
    protocol = mock.Mock()
    protocol.peername = (remote, 51000)
    protocol.sockname = ("127.0.0.1", 9000)
    protocol.ssl_context = None
    return make_mocked_request("GET", "/pair", protocol=protocol)


def test_is_loopback_accepts_loopback_addresses_only() -> None:
    assert pair_routes.is_loopback("127.0.0.1") is True
    assert pair_routes.is_loopback("127.0.0.2") is True
    assert pair_routes.is_loopback("::1") is True
    assert pair_routes.is_loopback("192.168.1.3") is False
    assert pair_routes.is_loopback("100.89.25.56") is False
    assert pair_routes.is_loopback("") is False
    assert pair_routes.is_loopback(None) is False
    assert pair_routes.is_loopback("not-an-ip") is False


@pytest.mark.asyncio
async def test_pair_route_renders_for_loopback_client(monkeypatch) -> None:
    """Loopback callers get the live page (200, text/html)."""
    html = "<html><body>primary http://192.168.1.3:8642</body></html>"
    monkeypatch.setattr(pair_routes, "render_pairing_html", lambda config, ip_override=None: html)

    client = await _client()
    try:
        resp = await client.get("/pair")
        assert resp.status == 200
        assert resp.content_type.startswith("text/html")
        assert (await resp.text()) == html
        assert (await resp.text()).count("192.168.1.3:8642") == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pair_route_denies_non_loopback_client(monkeypatch) -> None:
    """A LAN caller gets a short 403 and never the key-bearing page."""
    monkeypatch.setattr(pair_routes, "render_pairing_html", lambda config, ip_override=None: "<html>KEY</html>")

    response = await pair_routes._pair_route(_mocked_request("192.168.1.3"))
    assert response.status == 403
    body = response.body.decode()
    assert "host machine" in body.lower()
    assert "KEY" not in body


@pytest.mark.asyncio
async def test_pair_route_allows_loopback_mocked_client(monkeypatch) -> None:
    """The loopback allow-path itself is the IP-parse loopback test."""
    monkeypatch.setattr(pair_routes, "render_pairing_html", lambda config, ip_override=None: "<html>ok</html>")

    response = await pair_routes._pair_route(_mocked_request("127.0.0.1"))
    assert response.status == 200
    assert response.body.decode() == "<html>ok</html>"


def test_pair_page_renders_current_primary_and_no_dead_address(monkeypatch) -> None:
    """The rendered page carries the current primary URL and never a stopped
    Tailscale address."""
    from hermes_mobile_plugin import qr_generator

    monkeypatch.setattr(
        qr_generator,
        "get_ip_candidates",
        lambda: [{"ip": "192.168.1.3", "kind": "lan", "label": "Same Wi-Fi"}],
    )
    monkeypatch.setattr(qr_generator, "is_tailscale_running", lambda: False)

    html = render_pairing_html(
        {
            "platforms": {"api_server": {"extra": {"key": "test-api-key-12345", "port": 8642}}},
            "compression": {"enabled": True},
        }
    )
    assert "http://192.168.1.3:8642" in html
    assert "100." not in html
    assert "192.168.1.3" in html
