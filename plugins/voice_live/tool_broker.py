"""Expose only the declared ``voice_live`` tool manifest to realtime models."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from src.core.components.registry import get_global_registry
from src.core.components.types import ComponentType
from src.core.models.message import Message, MessageType

from .life_binding import get_tool_manifest


@dataclass(slots=True, frozen=True)
class _ToolBinding:
    signature: str
    component_type: ComponentType
    context_parameters: frozenset[str]


_RUNTIME_CONTEXT_PARAMETERS = frozenset(
    {"scene_id", "consciousness_instance_id", "episode_id"}
)


class VoiceToolBroker:
    """Schema discovery and invocation for one voice consciousness instance."""

    def __init__(self, consciousness: Any, config: Any, store: Any) -> None:
        self._consciousness = consciousness
        self._config = config
        self._store = store
        self._bindings: dict[str, _ToolBinding] = {}

    def schemas(self) -> list[dict[str, Any]]:
        allowed = set(get_tool_manifest("voice_live"))
        registry = get_global_registry()
        schemas: list[dict[str, Any]] = []
        for component_type in (ComponentType.ACTION, ComponentType.TOOL):
            for signature, component in registry.get_by_type(component_type).items():
                try:
                    raw = component.to_schema()
                except Exception:
                    continue
                function = raw.get("function", raw) if isinstance(raw, dict) else {}
                name = str(function.get("name") or "")
                if name not in allowed:
                    continue
                parameters = dict(
                    function.get("parameters")
                    or {"type": "object", "properties": {}}
                )
                schema = {
                    "type": "function",
                    "name": name,
                    "description": str(function.get("description") or ""),
                    "parameters": parameters,
                }
                schemas.append(schema)
                properties = parameters.get("properties")
                property_names = set(properties) if isinstance(properties, dict) else set()
                self._bindings[name] = _ToolBinding(
                    signature,
                    component_type,
                    frozenset(property_names) & _RUNTIME_CONTEXT_PARAMETERS,
                )
        return schemas

    async def execute(self, name: str, arguments_json: str) -> dict[str, Any]:
        binding = self._bindings.get(name)
        if binding is None:
            raise ValueError(f"tool is not in the voice_live manifest: {name}")
        arguments = json.loads(arguments_json or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        runtime_context = {
            "scene_id": self._consciousness.stream_id,
            "consciousness_instance_id": self._consciousness.instance_id,
            "episode_id": self._store.episode_id,
        }
        for parameter in binding.context_parameters:
            arguments[parameter] = runtime_context[parameter]
        from src.core.managers import get_plugin_manager

        plugin_name = binding.signature.split(":", 1)[0]
        plugin = get_plugin_manager().get_plugin(plugin_name)
        if plugin is None:
            raise RuntimeError(f"tool plugin is not active: {plugin_name}")
        message = Message(
            message_id=f"voice-tool-{uuid.uuid4().hex}",
            content=f"voice tool call: {name}",
            processed_plain_text=f"voice tool call: {name}",
            message_type=MessageType.TEXT,
            sender_id=self._config.session.user_id,
            sender_name=self._config.session.user_name,
            platform="voice_live",
            chat_type="private",
            stream_id=self._consciousness.stream_id,
            extra={
                "episode_id": self._store.episode_id,
                "consciousness_instance_id": self._consciousness.instance_id,
            },
        )
        await self._store.append_async("tool.started", {"name": name, "arguments": arguments})
        if binding.component_type is ComponentType.ACTION:
            from src.core.managers.action_manager import get_action_manager

            success, result = await get_action_manager().execute_action(
                binding.signature, plugin, message, **arguments
            )
        else:
            from src.core.managers.tool_manager import get_tool_use

            success, result = await get_tool_use().execute_tool(
                binding.signature, plugin, message, **arguments
            )
        response = {"success": bool(success), "result": result}
        await self._store.append_async("tool.completed", {"name": name, **response})
        return response
