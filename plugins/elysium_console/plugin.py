"""Plugin entry point for the local Elysium data observatory."""

from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin

from .router import ElysiumConsoleRouter


@register_plugin
class ElysiumConsolePlugin(BasePlugin):
    """Expose a local, read-only view of Life Engine authority data."""

    plugin_name = "elysium_console"
    plugin_description = "Local read-only observatory for Elysium life data"
    plugin_version = "0.1.0"

    def get_components(self) -> list[type]:
        return [ElysiumConsoleRouter]
