"""
hermes-mobile plugin for Hermes Agent.

This plugin registers hooks and provides the mobile API endpoints
via the dashboard plugin system.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register plugin with Hermes Agent."""
    logger.info("hermes-mobile plugin registered")
    
    # Hook: app_startup - initialize background tasks
    @ctx.hook("app_startup")
    async def on_startup() -> None:
        logger.info("hermes-mobile: startup hook")
        # Could start background cleanup tasks here
    
    # Hook: app_shutdown - cleanup
    @ctx.hook("app_shutdown")
    async def on_shutdown() -> None:
        logger.info("hermes-mobile: shutdown hook")


__all__ = ["register"]
