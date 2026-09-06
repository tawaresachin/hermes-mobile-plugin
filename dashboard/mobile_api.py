"""
Dashboard plugin API entry point.

This file is loaded by the dashboard plugin system and must expose
a `router` (FastAPI APIRouter).
"""

from hermes_mobile_plugin.mobile_api import router

__all__ = ["router"]
