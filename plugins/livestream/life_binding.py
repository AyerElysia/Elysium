"""Resolve the exact running LifeEngine package identity."""

from __future__ import annotations

import importlib
import sys
from typing import Any


def package_roots() -> tuple[str, ...]:
    roots: list[str] = []
    if "life_engine" in sys.modules or any(
        name.startswith("life_engine.") for name in sys.modules
    ):
        roots.append("life_engine")
    roots.append("plugins.life_engine")
    return tuple(dict.fromkeys(roots))


def life_attr(module: str, name: str) -> Any:
    last_error: Exception | None = None
    for root in package_roots():
        try:
            return getattr(importlib.import_module(f"{root}.{module}"), name)
        except (ImportError, AttributeError) as exc:
            last_error = exc
    raise ImportError(f"LifeEngine attribute unavailable: {module}.{name}") from last_error


def get_running_life_service() -> Any | None:
    for root in package_roots():
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
SceneState = life_attr("service.world_state", "SceneState")
PreparedPerception = life_attr("service.perception_gateway", "PreparedPerception")
LifeEngineEvent = life_attr("service.event_builder", "LifeEngineEvent")
LifeEventType = life_attr("service.event_builder", "EventType")


__all__ = [
    "ConsciousnessInstance",
    "LifeEngineEvent",
    "LifeEventType",
    "PerceptionFilter",
    "PreparedPerception",
    "SceneState",
    "get_running_life_service",
]
