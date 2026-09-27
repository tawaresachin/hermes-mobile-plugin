"""Regression tests for the 2026-09-27 audit findings.

Invariants pinned here (all were untested before, which is how they shipped
broken):
  * auth key resolution is ADAPTER-FIRST (a gateway holding a scoped key
    must never see these routes run keyless),
  * attachment download rejects traversal,
  * an over-cap JSON body is 413, not a 400 or a crash,
  * `tailscale ip -4`'s bare-IP output is actually parsed (the old parser
    demanded "inet" and the CLI method was silently dead).
"""
from __future__ import annotations

import sys
import types

import pytest
from aiohttp import web
from unittest.mock import patch

from hermes_mobile_plugin import audio_routes
from hermes_mobile_plugin.audio import AudioBackendError


class _Adapter:
    def __init__(self, key):
        self._key = key

    def _expected_api_key(self):
        return self._key


def _fake_app(adapter):
    app = {}
    if adapter is not None:
        app["api_server_adapter"] = adapter
    return app


def test_expected_key_prefers_live_adapter_over_config():
    """Gateway has a scoped key; config.yaml/env see nothing. The plugin must
    take the adapter's answer — the old config+env chain returned None and
    every gated route silently ran keyless."""
    app = _fake_app(_Adapter("scoped-secret-key"))
    with patch.object(audio_routes, "load_hermes_config", create=True):
        assert audio_routes._expected_key(app) == "scoped-secret-key"


def test_authorized_denies_when_adapter_has_key_and_bearer_missing():
    app = _fake_app(_Adapter("scoped-secret-key"))
    req = types.SimpleNamespace(app=app, headers={})
    assert audio_routes._authorized(req) is False
    req_ok = types.SimpleNamespace(app=app, headers={"Authorization": "Bearer scoped-secret-key"})
    assert audio_routes._authorized(req_ok) is True


def test_keyless_adapter_falls_back_to_config_chain():
    """No adapter (CLI/tests): the config chain still answers, and a keyless
    gateway posture (None) must stay None — fail-open is only allowed when
    the gateway ITSELF runs keyless."""
    with patch.object(audio_routes, "_expected_key_cache", (sys.float_info.max, None)):
        assert audio_routes._expected_key(None) is None


def test_download_rejects_traversal():
    route = getattr(audio_routes._download_route, "__wrapped__", audio_routes._download_route)
    for bad in ("../etc", "a/../../b", ".."):
        req = types.SimpleNamespace(match_info={"session_id": "s1", "filename": bad})
        resp = __import__("asyncio").run(route(req))
        assert resp.status == 404, f"traversal {bad!r} must 404"


@pytest.mark.asyncio
async def test_oversized_json_body_is_413():
    class _Req:
        async def read(self):
            raise web.HTTPRequestEntityTooLarge(
                max_size=1, actual_size=2, text="too big")

    with pytest.raises(AudioBackendError) as ei:
        await audio_routes._read_json(_Req())
    assert ei.value.status == 413


def test_tailscale_cli_bare_ip_is_parsed():
    """`tailscale ip -4` prints a bare IP (no "inet" token). The old parser
    matched only interface output, so method 1 never returned."""
    from hermes_mobile_plugin.ip_detector import get_tailscale_ip

    with patch("hermes_mobile_plugin.ip_detector._run_command",
               return_value="100.89.25.56\n") as m:
        assert get_tailscale_ip() == "100.89.25.56"
    m.assert_called_once()  # CLI hit resolved it; no interface fallback ran
