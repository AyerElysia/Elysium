"""Bridge delegated model turns into the subject's unified activity ledger."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


def _tool_call_payloads(calls: list[Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        call_id = str(getattr(call, "id", "") or "").strip()
        if not call_id:
            call_id = f"delegated-call-{index}"
        tool_name = str(getattr(call, "name", "") or "").strip()
        raw_args = getattr(call, "args", {})
        if isinstance(raw_args, Mapping):
            arguments = {str(key): value for key, value in raw_args.items()}
        else:
            arguments = {
                "raw_arguments": str(raw_args or ""),
                "decode_status": "non_mapping",
            }
        payloads.append(
            {
                "call_id": call_id,
                "tool_name": tool_name or "<unknown>",
                "arguments": arguments,
            }
        )
    return payloads


@dataclass(slots=True)
class DelegatedActivityRecorder:
    """Record one delegated worker without declaring a second subject."""

    plugin: Any
    stream_id: str = ""
    trigger_message: Any | None = None
    surface: str = "life_engine_agent"
    run_occurrence_id: str = field(
        default_factory=lambda: f"delegated-run:{uuid4().hex}"
    )

    def _service(self) -> Any | None:
        # Do not touch the public ``plugin.service`` property: constructing a
        # new LifeEngineService from an isolated test or a stopped plugin would
        # create a false authority.  Only the already-owned runtime may record.
        return getattr(self.plugin, "_service", None)

    def _source_instance_id(self, service: Any) -> str:
        message_extra = getattr(self.trigger_message, "extra", None)
        if isinstance(message_extra, Mapping):
            scope = message_extra.get("life_turn_scope")
            if isinstance(scope, Mapping):
                instance_id = str(
                    scope.get("consciousness_instance_id") or ""
                ).strip()
                if instance_id:
                    return instance_id
            instance_id = str(
                message_extra.get("consciousness_instance_id") or ""
            ).strip()
            if instance_id:
                return instance_id
        if self.stream_id:
            resolver = getattr(service, "resolve_consciousness_instance", None)
            if callable(resolver):
                resolved = str(resolver(self.stream_id) or "").strip()
                if resolved:
                    return resolved
            return "chat_global"
        return "life_engine_subconscious"

    async def record_model_turn(
        self,
        response: Any,
        calls: list[Any],
        *,
        turn_index: int,
    ) -> dict[str, str]:
        """Append one generated turn and all selected tool arguments."""

        service = self._service()
        recorder = getattr(service, "record_conscious_model_turn", None)
        if not callable(recorder):
            return {}
        call_payloads = _tool_call_payloads(calls)
        turn_occurrence_id = (
            f"{self.run_occurrence_id}:model-turn:{int(turn_index)}"
        )
        return dict(
            await recorder(
                stream_id=str(self.stream_id or "life_engine_internal"),
                source_instance_id=self._source_instance_id(service),
                turn_occurrence_id=turn_occurrence_id,
                transport_request_id=str(
                    getattr(response, "request_record_id", "")
                    or f"{turn_occurrence_id}:transport"
                ),
                provider_reasoning_content=str(
                    getattr(response, "reasoning_content", "") or ""
                ),
                assistant_message=str(getattr(response, "message", "") or ""),
                calls=call_payloads,
                surface=self.surface,
            )
        )

    async def record_tool_results(
        self,
        *,
        turn_index: int,
        activity_ids: Mapping[str, str],
        results: list[Mapping[str, Any]],
    ) -> None:
        """Append exact delegated tool outcomes after their execution."""

        if not activity_ids:
            return
        service = self._service()
        recorder = getattr(service, "record_conscious_tool_results", None)
        if not callable(recorder):
            return
        await recorder(
            stream_id=str(self.stream_id or "life_engine_internal"),
            source_instance_id=self._source_instance_id(service),
            turn_occurrence_id=(
                f"{self.run_occurrence_id}:model-turn:{int(turn_index)}"
            ),
            activity_ids=activity_ids,
            results=list(results),
            surface=self.surface,
        )
