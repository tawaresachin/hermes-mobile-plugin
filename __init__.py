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

try:
    from .hermes_mobile_plugin import register, create_plugin, PLUGIN_VERSION  # noqa: F401
except ImportError:  # flat import context
    from hermes_mobile_plugin import register, create_plugin, PLUGIN_VERSION  # noqa: F401

__all__ = ["register", "create_plugin", "PLUGIN_VERSION"]
