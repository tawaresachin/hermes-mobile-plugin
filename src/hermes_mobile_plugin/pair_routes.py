"""Live, loopback-only pairing page served by the gateway api_server.

``GET /pair`` renders the SAME pairing HTML the CLI writes to
``qr_code.html`` — generated fresh per request from the current reachable
address (the 0.0.43 candidate/primary logic, so no dead Tailscale address).

SECURITY: the page embeds the gateway API key, so it may ONLY ever be served to
a loopback caller. Any remote address outside loopback gets a short 403 and the
key is never rendered. The key must not be served over the LAN, where a phone
(or anyone on the subnet) could read it; the phone's own IP is never the caller
of this route.

Mounted via ``ctx.register_platform_handler("api_server", _wire)`` in
``__init__.register(ctx)``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any, Callable

from aiohttp import web

from .constants import PAIR_PATH
from .qr_generator import QRConfigError, render_pairing_html

logger = logging.getLogger(__name__)


def is_loopback(remote: Any) -> bool:
    """True when the caller came in on a loopback transport address.

    ``ipaddress.ip_address(...).is_loopback`` covers 127.0.0.0/8 and ::1, so
    it covers both ``127.0.0.1`` and the IPv6 loopback without a string list.
    """
    raw = str(remote or "").strip()
    if not raw:
        return False
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def _render_pairing_page() -> str:
    """Render the page (config read here, off the event loop)."""
    from .config import load_hermes_config

    return render_pairing_html(load_hermes_config())


async def _pair_route(request: web.Request) -> web.Response:
    # SECURITY: loopback-only. The page embeds the API key, so a LAN caller
    # must never get it. No auth header can be obtained from a LAN client and
    # no path/param can smuggle the key to a remote peer.
    if not is_loopback(request.remote):
        logger.info(
            "Pairing page refused: non-loopback caller from %s", request.remote
        )
        return web.Response(
            text=(
                "The pairing page is only available on the host machine.\n"
                "Open it from the computer running the Hermes gateway."
            ),
            content_type="text/plain",
            status=403,
        )

    html = await asyncio.to_thread(_render_pairing_page)
    return web.Response(
        text=html,
        content_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex",
        },
    )


def register(native_app: web.Application) -> None:
    """Attach the live pairing route to the api_server's native web.Application."""
    native_app.router.add_get(PAIR_PATH, _pair_route)


__all__ = ["register", "is_loopback"]
