"""Hermes Mobile QR Plugin - Clean architecture implementation.

Provides QR code generation for Hermes Mobile App connection
to Hermes Agent Desktop gateway (port 8642).
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

from .constants import PLUGIN_VERSION
from .config import load_hermes_config, get_plugin_config
from .qr_generator import generate_qr

logger = logging.getLogger(__name__)


class HermesMobileQRPlugin:
    """Plugin that generates QR codes for mobile app connection to 8642 gateway."""

    def __init__(self, config: dict, plugin_dir: Path):
        """Initialize plugin.

        Args:
            config: Hermes Agent configuration dictionary.
            plugin_dir: Path to plugin directory.
        """
        self.config = config
        self.plugin_dir = plugin_dir
        self._qr_generated = False
        self._supervisor_thread: Optional[threading.Thread] = None

    async def startup(self) -> None:
        """Called when Hermes Agent starts up."""
        plugin_config = get_plugin_config(self.config)
        auto_generate = plugin_config.get("auto_generate", True)

        if auto_generate:
            logger.info("[Hermes Mobile QR v%s] Plugin starting...", PLUGIN_VERSION)
            await self._generate_qr_and_start_supervisor()
        else:
            logger.info("[Hermes Mobile QR v%s] Auto-generate disabled", PLUGIN_VERSION)

    async def shutdown(self) -> None:
        """Called when Hermes Agent shuts down."""
        logger.info("[Hermes Mobile QR] Plugin shutting down...")
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            # Note: Supervisor runs as daemon, can't be gracefully stopped from here
            pass

    async def _generate_qr_and_start_supervisor(self) -> None:
        """Generate QR code and start gateway supervisor."""
        if self._qr_generated:
            return

        try:
            await generate_qr(self.config, self.plugin_dir)
            self._qr_generated = True
            logger.info("[Hermes Mobile QR] QR code generated successfully")

            # Start supervisor in background thread
            from .supervisor import GatewaySupervisor
            self._supervisor_thread = threading.Thread(
                target=self._run_supervisor,
                daemon=True,
                name="gateway-supervisor",
            )
            self._supervisor_thread.start()
            logger.info("[Hermes Mobile QR] Gateway supervisor started (daemon thread)")

        except Exception as e:
            logger.warning("[Hermes Mobile QR] Failed to initialize: %s", e)

    def _run_supervisor(self) -> None:
        """Run supervisor in background thread."""
        supervisor = GatewaySupervisor()
        supervisor.run()


# Plugin entry point for Hermes
def create_plugin(config: dict, plugin_dir: Path) -> HermesMobileQRPlugin:
    """Factory function called by Hermes plugin loader.

    Args:
        config: Hermes Agent configuration.
        plugin_dir: Plugin directory path.

    Returns:
        Plugin instance.
    """
    return HermesMobileQRPlugin(config, plugin_dir)


async def startup_hook(config: dict, plugin_dir: Path) -> None:
    """Standalone startup hook if Hermes loader expects a function."""
    plugin = create_plugin(config, plugin_dir)
    await plugin.startup()