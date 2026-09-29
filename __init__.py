"""Hermes Mobile QR + Voice plugin package.

The standard plugin loader (``hermes_cli.plugins.PluginManager._load_plugin``)
imports this package's ``__init__.py`` and looks for a ``register(ctx)``
function. The real implementation lives in
``hermes_mobile_plugin/__init__.py`` — we forward both entry points
(``register`` and ``create_plugin``) so the modern register(ctx) path
and the legacy class-based entry point both work.

The except branch covers the flat layout (installed plugin dir, or this
repo root imported as a namespace-less module by pytest) where the inner
``hermes_mobile_plugin`` package is importable from sys.path instead.
"""

# src-layout: the real package sits under <plugin>/src/hermes_mobile_plugin.
# Make it importable regardless of which layout this dir was installed from.
import os, sys
_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(_src) and _src not in sys.path:
    sys.path.insert(0, _src)

try:
    from hermes_mobile_plugin import register, create_plugin, PLUGIN_VERSION  # noqa: F401
except ImportError:  # nested-layout fallback (package beside this file)
    from .hermes_mobile_plugin import register, create_plugin, PLUGIN_VERSION  # noqa: F401

__all__ = ["register", "create_plugin", "PLUGIN_VERSION"]
