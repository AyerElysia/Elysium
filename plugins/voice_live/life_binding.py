"""Bind Voice Live to the exact LifeEngine package identity used at runtime."""

from __future__ import annotations

import importlib
import sys
from typing import Any


def _package_roots() -> tuple[str, ...]:
    roots: list[str] = []
    # The plugin loader imports ``life_engine.*`` from the plugins directory.
    # Normal package imports and tests use ``plugins.life_engine.*``. Prefer
    # the already-running plugin identity so module-level registries are shared.
    if "life_engine" in sys.modules or any(
        name.startswith("life_engine.") for name in sys.modules
    ):
        roots.append("life_engine")
    roots.append("plugins.life_engine")
    return tuple(dict.fromkeys(roots))


def life_attr(module: str, name: str) -> Any:
    last_error: Exception | None = None
    for root in _package_roots():
        try:
            return getattr(importlib.import_module(f"{root}.{module}"), name)
        except (ImportError, AttributeError) as exc:
            last_error = exc
    raise ImportError(f"LifeEngine attribute is unavailable: {module}.{name}") from last_error


def get_running_life_service() -> Any | None:
    for root in _package_roots():
        try:
            registry = importlib.import_module(f"{root}.service.registry")
        except ImportError:
            continue
        service = registry.get_life_engine_service()
        if service is not None:
            return service
    return None


ConsciousnessInstance = life_attr("service.consciousness", "ConsciousnessInstance")
PerceptionFilter = life_attr("service.world_state", "PerceptionFilter")
get_tool_manifest = life_attr("service.tool_manifests", "get_tool_manifest")
