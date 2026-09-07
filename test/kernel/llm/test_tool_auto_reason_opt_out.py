"""Explicit automatic-reason opt-outs preserve provider and action contracts."""

from __future__ import annotations

from copy import deepcopy
import inspect
from types import SimpleNamespace
from typing import Annotated

import pytest

from plugins.life_engine.core.context_stewardship import (
    LifeAuthorSelfContinuityCheckpointAction,
)
from plugins.life_engine.service.core import LifeEngineService
from src.core.components.base.action import BaseAction
from src.kernel.llm import ROLE, LLMPayload, Text
from src.kernel.llm.model_client.anthropic_client import (
    _payloads_to_anthropic_messages,
    _to_anthropic_tool,
)
from src.kernel.llm.model_client.openai_client import (
    _payloads_to_openai_messages,
    _to_openai_tool,
)
from src.kernel.llm.payload.tooling import ToolRegistry


_CHECKPOINT_FIELDS = {
    "thought",
    "continuity_text",
    "source_manifest_sha256",
    "expected_revision",
    "release_through_group_ref",
    "retain_exact_group_refs",
}


@pytest.fixture(params=["openai", "anthropic"])
def provider(request):
    return request.param


def _converted_parameters(provider, tool):
    if provider == "openai":
        return _to_openai_tool(tool)["function"]["parameters"]
    return _to_anthropic_tool(tool)["input_schema"]


class _ThoughtAction(BaseAction):
    action_name = "synthetic_thought_action"
    action_description = "Synthetic action whose thought is not an implicit opt-out."

    async def execute(
        self, thought: Annotated[str, "Explicit synthetic thought"],
    ) -> tuple[bool, str]:
        return True, thought


class _OptedOutAction(_ThoughtAction):
    auto_reason_parameter = False


class _DeclaredReasonAction(BaseAction):
    action_name = "synthetic_declared_reason"
    action_description = "Synthetic action with its own required reason."
    auto_reason_parameter = False

    async def execute(self, reason: str) -> tuple[bool, str]:
        return True, reason


def test_checkpoint_provider_schema_requires_only_declared_fields(provider):
    parameters = _converted_parameters(
        provider, LifeAuthorSelfContinuityCheckpointAction,
    )

    assert set(parameters["properties"]) == _CHECKPOINT_FIELDS
    assert set(parameters["required"]) == _CHECKPOINT_FIELDS
    assert parameters["properties"]["thought"]["type"] == "string"
    assert "default" not in parameters["properties"]["thought"]
    assert parameters["properties"]["retain_exact_group_refs"]["items"] == {
        "type": "string"
    }
    signature = inspect.signature(LifeAuthorSelfContinuityCheckpointAction.execute)
    assert signature.parameters["thought"].default is inspect.Parameter.empty


def test_checkpoint_opt_out_reaches_real_payload_conversion(provider):
    payloads = [
        LLMPayload(ROLE.TOOL, [LifeAuthorSelfContinuityCheckpointAction, _ThoughtAction]),
        LLMPayload(ROLE.USER, Text("synthetic schema-only request")),
    ]
    if provider == "openai":
        _messages, tools = _payloads_to_openai_messages(payloads)
        parameters = [item["function"]["parameters"] for item in tools]
    else:
        _messages, tools, _system = _payloads_to_anthropic_messages(payloads)
        parameters = [item["input_schema"] for item in tools]

    assert "reason" not in parameters[0]["properties"]
    assert "thought" in parameters[0]["required"]
    assert "reason" in parameters[1]["properties"]
    assert "reason" in parameters[1]["required"]


def test_default_action_still_adds_reason_even_when_thought_exists(provider):
    assert BaseAction.auto_reason_parameter is True
    parameters = _converted_parameters(provider, _ThoughtAction)

    assert set(parameters["properties"]) == {"thought", "reason"}
    assert set(parameters["required"]) == {"thought", "reason"}


def test_explicit_opt_out_preserves_declared_thought(provider):
    parameters = _converted_parameters(provider, _OptedOutAction)

    assert set(parameters["properties"]) == {"thought"}
    assert parameters["required"] == ["thought"]


def test_opt_out_never_removes_a_declared_required_reason(provider):
    parameters = _converted_parameters(provider, _DeclaredReasonAction)

    assert set(parameters["properties"]) == {"reason"}
    assert parameters["required"] == ["reason"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_legacy_schema_without_metadata_keeps_reason_injection(provider, wrapped):
    class LegacyTool:
        @classmethod
        def to_schema(cls):
            function = {
                "name": "synthetic_legacy",
                "description": "Synthetic legacy usable without metadata.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
            return {"type": "function", "function": function} if wrapped else function

    parameters = _converted_parameters(provider, LegacyTool)

    assert set(parameters["properties"]) == {"query", "reason"}
    assert parameters["required"] == ["query", "reason"]


def test_opt_out_preserves_explicit_optional_reason_schema(provider):
    original = {
        "name": "synthetic_optional_reason",
        "description": "Synthetic explicitly optional reason.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "reason": {"type": "string", "description": "original optional reason"},
            },
            "required": ["query"],
        },
    }

    class OptionalReasonTool:
        auto_reason_parameter = False

        @classmethod
        def to_schema(cls):
            return deepcopy(original)

    assert _converted_parameters(provider, OptionalReasonTool) == original["parameters"]


async def test_reason_does_not_supply_missing_checkpoint_thought():
    action = LifeAuthorSelfContinuityCheckpointAction.__new__(
        LifeAuthorSelfContinuityCheckpointAction
    )
    action._action_origin_extra = lambda: pytest.fail(
        "Missing thought must fail binding before the checkpoint body runs"
    )
    service = SimpleNamespace(
        _resolve_heartbeat_tool_class=lambda *_args: LifeAuthorSelfContinuityCheckpointAction,
        _instantiate_heartbeat_usable=lambda *_args, **_kwargs: action,
    )
    arguments = {
        "reason": "synthetic reason must never be adopted as thought",
        "continuity_text": "synthetic continuity",
        "source_manifest_sha256": "synthetic-manifest",
        "expected_revision": 0,
        "release_through_group_ref": "synthetic-group",
        "retain_exact_group_refs": [],
    }

    result, success = await LifeEngineService._run_heartbeat_tool_call_execution(
        service, "author_self_continuity_checkpoint", arguments, ToolRegistry(),
    )

    assert success is False
    assert "thought" in result
    assert "required" in result
    assert "thought" not in arguments
    assert arguments["reason"] == "synthetic reason must never be adopted as thought"

