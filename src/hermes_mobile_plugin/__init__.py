"""Hermes Mobile QR Plugin - Clean architecture implementation.

Generates a QR code for the Hermes Mobile App to connect directly to the
Hermes Agent Desktop gateway (port 8642), and exposes voice STT/TTS routes
on the gateway so the mobile app can speak and listen without an extra
service.

Both behaviors are opt-in via ``plugins.hermes-mobile-qr`` in
``~/.hermes/config.yaml``:

    plugins:
      hermes-mobile-qr:
        auto_generate: true     # QR code on startup
        audio: true             # voice routes on api_server

Audio routes are mounted via the standard plugin handler mechanism
(``ctx.register_platform_handler("api_server", _wire)``), which gives
the plugin a handle on the api_server's aiohttp ``web.Application``.
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from .constants import PLUGIN_VERSION

logger = logging.getLogger(__name__)


class HermesMobileQRPlugin:
    """Plugin that generates QR codes for mobile app connection to 8642 gateway.

    Used by the legacy entry-point style loader. The standard
    ``register(ctx)`` function below is the path the modern plugin loader
    uses; both entry points are kept for backward compatibility.
    """

    def __init__(self, config: dict, plugin_dir: Path):
        self.config = config
        self.plugin_dir = plugin_dir
        self._qr_generated = False

    async def startup(self) -> None:
        """Called when Hermes Agent starts up (legacy entry-point style)."""
        try:
            from .config import get_plugin_config
            plugin_config = get_plugin_config(self.config)
        except ImportError:
            plugin_config = self.config.get("plugins", {}).get("hermes-mobile-qr", {})

        if plugin_config.get("auto_generate", True):
            logger.info("[Hermes Mobile QR v%s] Plugin starting...", PLUGIN_VERSION)
            await self._generate_qr_and_start_supervisor()
        else:
            logger.info("[Hermes Mobile QR v%s] Auto-generate disabled", PLUGIN_VERSION)

    async def shutdown(self) -> None:
        logger.info("[Hermes Mobile QR] Plugin shutting down...")

    async def _generate_qr_and_start_supervisor(self) -> None:
        if self._qr_generated:
            return
        try:
            from .qr_generator import generate_qr
            await generate_qr(self.config, self.plugin_dir)
            self._qr_generated = True
            logger.info("[Hermes Mobile QR] QR code generated successfully")

            from .supervisor import ensure_running
            ensure_running()
            logger.info("[Hermes Mobile QR] Gateway watchdog verified/spawned")

        except Exception as e:
            logger.warning("[Hermes Mobile QR] Failed to initialize: %s", e)

# ---------------------------------------------------------------------------
# Standard plugin entry point (register(ctx))
# ---------------------------------------------------------------------------

_audio_registered = False
_audio_lock = threading.Lock()


def _wire_audio_routes(native: Any, adapter: Any) -> None:
    """Factory passed to ``ctx.register_platform_handler("api_server", ...)``.

    Receives the api_server's aiohttp ``web.Application`` as ``native``
    and registers the plugin's voice routes on it. Guarded so a second
    connect (or another plugin loader) doesn't double-register.
    """
    global _audio_registered
    with _audio_lock:
        if _audio_registered:
            logger.debug("[hermes-mobile-qr] Audio routes already registered; skipping")
            return
        try:
            from .audio_routes import register as _register_routes
            from .system_routes import register as _register_system_routes
        except Exception as exc:
            logger.warning(
                "[hermes-mobile-qr] Could not import route modules: %s", exc
            )
            return
        if native is None:
            logger.debug(
                "[hermes-mobile-qr] api_server native is None; "
                "audio routes not mounted on this run"
            )
            return
        try:
            _register_routes(native)
            _register_system_routes(native)
        except Exception as exc:
            logger.exception(
                "[hermes-mobile-qr] Failed to register audio routes: %s", exc
            )
            return
        _audio_registered = True


async def _maybe_generate_qr(ctx: Any) -> None:
    """Best-effort QR generation on startup, mirroring the legacy flow.

    ``PluginContext`` exposes no ``.config``/``.plugin_dir`` attributes — the
    QR payload needs the REAL platforms.api_server config, so load it from
    config.yaml directly, and write the HTML next to the plugin package
    (never the gateway's CWD).
    """
    try:
        from .config import load_hermes_config, get_plugin_config
        hermes_config = load_hermes_config()
        plugin_config = get_plugin_config(hermes_config)
    except ImportError:
        hermes_config, plugin_config = {}, {}

    if not plugin_config.get("auto_generate", True):
        return

    try:
        from .constants import PLUGIN_DIR
        from .qr_generator import generate_qr
        out_dir = Path(ctx.plugin_dir) if getattr(ctx, "plugin_dir", None) else PLUGIN_DIR
        await generate_qr(hermes_config, out_dir)
        logger.info("[Hermes Mobile QR] QR code generated via register(ctx)")
    except Exception as exc:
        logger.warning("[Hermes Mobile QR] QR generation skipped: %s", exc)


def register(ctx: Any) -> None:
    """Standard plugin entry point. Called by Hermes plugin loader.

    Registers the plugin's voice STT/TTS routes on the api_server
    platform, and (if enabled) generates a QR code on startup.
    """
    logger.info(
        "[Hermes Mobile QR v%s] register(ctx) called", PLUGIN_VERSION
    )

    # Register the api_server handler — always safe (idempotent guard).
    try:
        ctx.register_platform_handler("api_server", _wire_audio_routes)
    except Exception as exc:
        logger.warning(
            "[Hermes Mobile QR] Could not register api_server handler: %s", exc
        )

    # Keep-alive: spawn the detached gateway watchdog. It lives OUTSIDE the
    # gateway process, so if the gateway dies (crash, OOM, reboot script) it
    # restarts it and the app reconnects. Idempotent via PID file.
    try:
        from .supervisor import ensure_running
        ensure_running()
    except Exception as exc:
        logger.warning("[Hermes Mobile QR] Watchdog spawn failed: %s", exc)

    # Fire-and-forget QR generation so we don't block loader.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_maybe_generate_qr(ctx))
        else:
            loop.run_until_complete(_maybe_generate_qr(ctx))
    except RuntimeError:
        # No event loop in this context — skip.
        pass


# ---------------------------------------------------------------------------
# Legacy entry point (kept for backward compatibility)
# ---------------------------------------------------------------------------


def create_plugin(config: dict, plugin_dir: Path) -> HermesMobileQRPlugin:
    """Factory function called by the legacy Hermes plugin loader."""
    return HermesMobileQRPlugin(config, plugin_dir)


async def startup_hook(config: dict, plugin_dir: Path) -> None:
    """Standalone startup hook if Hermes loader expects a function."""
    plugin = create_plugin(config, plugin_dir)
    await plugin.startup()
