"""Owner-scoped extension contracts for independently loaded scene plugins."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SceneExtension:
    kind: str
    tools: tuple[str, ...]
    chat_tools: tuple[str, ...] = ()
    result_before_reply_tools: tuple[str, ...] = ()
    evidence_budget_bytes: int = 8192


_extensions: dict[str, tuple[object, SceneExtension]] = {}


def register_scene_extension(owner: object, extension: SceneExtension) -> None:
    """Register a declared capability boundary, rejecting duplicate owners/kinds."""

    from .tool_manifests import CONSCIOUSNESS_TOOL_MANIFESTS

    if not extension.kind or extension.kind in CONSCIOUSNESS_TOOL_MANIFESTS:
        raise ValueError("SceneExtensionKindConflict")
    if extension.evidence_budget_bytes < 1024:
        raise ValueError("SceneExtensionBudgetInvalid")
    previous = _extensions.get(extension.kind)
    if previous is not None:
        if previous[0] is owner and previous[1] == extension:
            return
        raise RuntimeError("SceneExtensionAlreadyRegistered")
    _extensions[extension.kind] = (owner, extension)


def unregister_scene_extension(owner: object) -> None:
    """Only remove declarations owned by the exact unloading plugin instance."""

    for kind, (registered_owner, _) in list(_extensions.items()):
        if registered_owner is owner:
            del _extensions[kind]


def get_scene_extension(kind: str) -> SceneExtension | None:
    registered = _extensions.get(kind)
    return registered[1] if registered is not None else None


def scene_chat_tools() -> list[str]:
    return list(dict.fromkeys(tool for _, extension in _extensions.values()
                             for tool in extension.chat_tools))


def requires_result_before_reply(tool_name: str) -> bool:
    normalized = tool_name.removeprefix("tool-")
    return any(normalized == tool.removeprefix("tool-")
               for _, extension in _extensions.values()
               for tool in extension.result_before_reply_tools)
