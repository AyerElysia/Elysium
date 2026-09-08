"""Trusted bridge from capability operations to existing Life Engine tools.

The capability manifest is an operation allow-list, not a permission grant.
Every call is rebound to the exact ``LifeEngineCapabilityCallTool`` which
originated it and is checked against the target tool's existing chatter
boundary.  Workflow/Skill prose is never interpreted by this module.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from src.app.plugin_system.base import BaseTool

from ..service.tool_manifests import heartbeat_tool_classes
from ..tools.web_tools import LifeEngineBrowserFetchTool, LifeEngineWebSearchTool
from . import tools as opportunity_tools
from .catalog import CapabilityDescriptor
from .execution_context import bind_capability_execution
from .registry import CapabilityExecutor, CapabilityInvocation
from .runtime import OpportunityCaller

_SELF_AWAKEN_ID = "life.self_awaken"
_INITIATIVE_ID = "life.initiative_reencounter"
_INNER_RETURN_ID = "life.inner_return"
_SELF_AWAKEN_OPERATIONS = frozenset({"opportunity.schedule", "opportunity.query"})
_INITIATIVE_ACTIONS = frozenset(
    {
        "initiative.hold",
        "initiative.rewrite",
        "initiative.reencounter",
        "initiative.release",
    }
)
_INITIATIVE_QUERY_ARGUMENTS = frozenset(
    {"resource", "record_id", "include_inactive", "continuation", "max_bytes"}
)
_INITIATIVE_COMMAND_ARGUMENTS = frozenset(
    {
        "action",
        "record_id",
        "expected_revision",
        "statement",
        "related_entity_refs",
        "reencounter_after_minutes",
    }
)
_INNER_QUERY_ARGUMENTS = frozenset(
    {"resource", "record_id", "continuation", "max_bytes"}
)
_INNER_COMMAND_ARGUMENTS = frozenset({"action", "record_id", "statement"})
_NATIVE_TOOL_OPERATIONS = frozenset(
    {
        "nucleus_apply_patch",
        "nucleus_browser_fetch",
        "nucleus_edit_file",
        "nucleus_glob_file",
        "nucleus_grep_events",
        "nucleus_learn",
        "nucleus_list_files",
        "nucleus_memory_continuity_review",
        "nucleus_mkdir",
        "nucleus_proactive_command",
        "nucleus_proactive_query",
        "nucleus_read_file",
        "nucleus_search_memory",
        "nucleus_todo",
        "nucleus_web_search",
        "nucleus_write_file",
        "nucleus_write_narrative",
    }
)
_RESERVED_IDENTITY_ARGUMENTS = frozenset(
    {
        "actor_consciousness_instance_id",
        "source_instance_id",
        "source_occurrence_id",
        "decision_occurrence_id",
        "occurred_at",
        "caller_tool",
        "runtime",
        "writer_claim",
    }
)


class NativeCapabilityDispatchError(RuntimeError):
    """Base failure for the trusted native capability bridge."""


class NativeCapabilityCallerInvalid(NativeCapabilityDispatchError):
    """The invocation is not attributable to its original capability tool."""


class NativeCapabilityPermissionDenied(PermissionError):
    """The originating consciousness surface cannot call the target tool."""


class NativeCapabilityArgumentsInvalid(ValueError):
    """Operation arguments are missing, unknown, or attempt identity forgery."""


class NativeCapabilityExecutorMismatch(NativeCapabilityDispatchError):
    """A non-native executor was supplied to the native dispatcher."""


class OpportunityManagementFacade(Protocol):
    """The already-open runtime methods needed by ``life.self_awaken``."""

    async def manage(
        self,
        action: str,
        target_id: str,
        expected_revision: int,
        arguments: Mapping[str, Any],
        reason: str,
        caller: OpportunityCaller,
    ) -> dict[str, Any]: ...

    async def query(
        self,
        *,
        resource: str,
        record_id: str = "",
        continuation: str = "",
        offset_bytes: int = 0,
        max_bytes: int = 8192,
        limit: int = 20,
        revision: int = 0,
        family: str = "registration",
        actor_consciousness_instance_id: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class NativeCapabilityResult:
    """One JSON-compatible domain outcome returned through capability_call."""

    capability_id: str
    operation_id: str
    success: bool
    value: Any

    def public(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "operation_id": self.operation_id,
            "success": self.success,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class _NativeOperationPlan:
    capability_id: str
    package_sha256: str
    operation_id: str
    arguments: Mapping[str, Any]


class NativeCapabilityExecutor(CapabilityExecutor):
    """Descriptor-bound factory product; caller binding stays in dispatch."""

    def __init__(self, descriptor: CapabilityDescriptor) -> None:
        self.descriptor = descriptor

    async def execute(
        self,
        operation_id: str,
        arguments: Mapping[str, Any],
    ) -> _NativeOperationPlan:
        if not self.descriptor.declares_operation(operation_id):
            raise NativeCapabilityPermissionDenied(
                "native operation is absent from the capability manifest"
            )
        return _NativeOperationPlan(
            capability_id=self.descriptor.capability_id,
            package_sha256=self.descriptor.package_sha256,
            operation_id=operation_id,
            arguments=MappingProxyType(dict(arguments)),
        )


def _fixed_tool_classes() -> dict[str, type[BaseTool]]:
    classes = [
        *heartbeat_tool_classes(),
        LifeEngineWebSearchTool,
        LifeEngineBrowserFetchTool,
    ]
    resolved: dict[str, type[BaseTool]] = {}
    for candidate in classes:
        if not inspect.isclass(candidate) or not issubclass(candidate, BaseTool):
            continue
        name = str(getattr(candidate, "tool_name", "") or "").strip()
        if not name or name not in _NATIVE_TOOL_OPERATIONS:
            continue
        previous = resolved.get(name)
        if previous is not None and previous is not candidate:
            raise NativeCapabilityDispatchError(
                f"duplicate native tool operation: {name}"
            )
        resolved[name] = candidate
    missing = sorted(_NATIVE_TOOL_OPERATIONS - resolved.keys())
    if missing:
        raise NativeCapabilityDispatchError(
            "native capability tool table is incomplete: " + ", ".join(missing)
        )
    return resolved


def _tool_schema(tool_cls: type[BaseTool]) -> tuple[str, dict[str, Any]]:
    """Return a detached strict argument schema without constructing a tool."""

    raw = tool_cls.to_schema()
    if not isinstance(raw, Mapping) or raw.get("type") != "function":
        raise NativeCapabilityDispatchError("native tool schema is not a function")
    function = raw.get("function")
    if not isinstance(function, Mapping):
        raise NativeCapabilityDispatchError("native tool function schema is missing")
    parameters = function.get("parameters")
    if not isinstance(parameters, Mapping) or parameters.get("type") != "object":
        raise NativeCapabilityDispatchError("native tool argument schema is missing")
    detached = copy.deepcopy(dict(parameters))
    properties = detached.get("properties")
    if not isinstance(properties, Mapping):
        raise NativeCapabilityDispatchError("native tool schema properties are missing")
    forged = sorted(
        str(name) for name in properties if name in _RESERVED_IDENTITY_ARGUMENTS
    )
    if forged:
        raise NativeCapabilityDispatchError(
            "native tool schema exposes runtime identity: " + ", ".join(forged)
        )
    detached["properties"] = copy.deepcopy(dict(properties))
    detached["additionalProperties"] = False
    return str(function.get("description") or ""), detached


def _narrow_schema(
    schema: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    constants: Mapping[str, str],
) -> dict[str, Any]:
    narrowed = copy.deepcopy(dict(schema))
    source = narrowed.get("properties")
    if not isinstance(source, Mapping):
        raise NativeCapabilityDispatchError("native tool schema properties are missing")
    missing = sorted(allowed - source.keys())
    if missing:
        raise NativeCapabilityDispatchError(
            "native slice schema is incomplete: " + ", ".join(missing)
        )
    properties = {name: copy.deepcopy(source[name]) for name in sorted(allowed)}
    for name, value in constants.items():
        if name not in properties:
            raise NativeCapabilityDispatchError(
                f"native slice constant has no property: {name}"
            )
        field = properties[name]
        if not isinstance(field, dict):
            field = dict(field)
            properties[name] = field
        field.pop("enum", None)
        field["const"] = value
    required = [
        str(name) for name in narrowed.get("required", ()) if str(name) in allowed
    ]
    for name in constants:
        if name not in required:
            required.append(name)
    narrowed["properties"] = properties
    narrowed["required"] = required
    narrowed["additionalProperties"] = False
    return narrowed


def _self_awaken_schema(operation: str) -> tuple[str, dict[str, Any]]:
    if operation == "opportunity.schedule":
        schedule = {
            "type": "object",
            "properties": {
                "provider_id": {"type": "string"},
                "referent_kind": {"type": "string"},
                "referent_id": {"type": "string"},
                "referent_revision": {"type": "integer", "minimum": 0},
                "referent_sha256": {"type": "string"},
                "workflow_id": {"type": "string"},
                "workflow_revision": {"type": "integer", "minimum": 1},
                "workflow_sha256": {"type": "string"},
                "schedule": {"type": "string", "enum": ["manual", "at", "interval"]},
                "first_due_at": {"type": "string"},
                "interval_seconds": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
            "description": (
                "只修改已有登记，可只提交要修改的字段；首次登记使用 "
                "nucleus_opportunity_command 的 opportunity.open 并提供精确引用。"
                "调度只产生到期机会，不直接执行工作流。"
            ),
        }
        return (
            "显式配置或重排一条 self-awaken 机会；不会自动采纳模板或执行动作。",
            {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "expected_revision": {"type": "integer", "minimum": 1},
                    "arguments": schedule,
                    "reason": {"type": "string"},
                },
                "required": ["target_id", "expected_revision", "arguments"],
                "additionalProperties": False,
            },
        )
    if operation == "opportunity.query":
        return (
            "只读查询机会系统；actor 身份由原始调用绑定，不接受参数覆盖。",
            {
                "type": "object",
                "properties": {
                    "resource": {"type": "string"},
                    "record_id": {"type": "string"},
                    "continuation": {"type": "string"},
                    "offset_bytes": {"type": "integer", "minimum": 0},
                    "max_bytes": {"type": "integer", "minimum": 512, "maximum": 32768},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "revision": {"type": "integer", "minimum": 0},
                    "family": {"type": "string"},
                },
                "required": ["resource"],
                "additionalProperties": False,
            },
        )
    raise NativeCapabilityPermissionDenied(
        "self-awaken operation is not a runtime management operation"
    )


def _learning_schema(action: str) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    from ..learning import learn_tool

    description, wrapper = _tool_schema(learn_tool.NucleusLearnTool)
    if not action:
        listed = ["help", *sorted(learn_tool._OPERATION_TOOLS)]
        wrapper["properties"]["action"]["enum"] = listed
        return (
            description,
            "",
            wrapper,
            {
                "actions": listed,
                "next": "query operation_schema again with one action for its exact arguments",
            },
        )
    normalized = learn_tool.normalize_learn_action(action)
    if normalized == "help":
        wrapper["properties"]["action"] = {"type": "string", "const": "help"}
        wrapper["properties"]["arguments"] = {
            "type": "object",
            "maxProperties": 0,
            "additionalProperties": False,
        }
        return description, normalized, wrapper, {"inner_operation": "help"}
    target = learn_tool._OPERATION_TOOLS.get(normalized)
    if target is None:
        raise NativeCapabilityArgumentsInvalid(f"unknown learning operation: {action}")
    inner_description, inner = _tool_schema(target)
    wrapper["properties"]["action"] = {
        "type": "string",
        "const": normalized,
    }
    wrapper["properties"]["arguments"] = inner
    if inner.get("required") and "arguments" not in wrapper["required"]:
        wrapper["required"].append("arguments")
    return (
        description,
        normalized,
        wrapper,
        {
            "inner_operation": normalized,
            "inner_description": inner_description,
            "inner_arguments_schema": copy.deepcopy(inner),
        },
    )


def describe_operation_schema(
    capability_id: str,
    operation: str,
    *,
    action: str = "",
) -> dict[str, Any]:
    """Describe one admitted call shape without granting or executing anything.

    The catalog/binding checks remain the caller's responsibility.  This fixed
    engineering table only explains parameters for operations the dispatcher
    can already enforce.
    """

    capability = str(capability_id or "").strip()
    operation_id = str(operation or "").strip()
    requested_action = str(action or "").strip()
    if not capability or not operation_id:
        raise NativeCapabilityArgumentsInvalid(
            "capability_id and operation are required"
        )

    extra: dict[str, Any] = {}
    if capability == _SELF_AWAKEN_ID:
        description, arguments_schema = _self_awaken_schema(operation_id)
    elif operation_id in _SELF_AWAKEN_OPERATIONS:
        raise NativeCapabilityPermissionDenied(
            "runtime management operations belong only to life.self_awaken"
        )
    else:
        tool_cls = _fixed_tool_classes().get(operation_id)
        if tool_cls is None:
            raise NativeCapabilityPermissionDenied(
                f"operation has no fixed native tool binding: {operation_id}"
            )
        description, arguments_schema = _tool_schema(tool_cls)
        if operation_id == "nucleus_learn":
            description, requested_action, arguments_schema, extra = _learning_schema(
                requested_action
            )
        elif capability == _INITIATIVE_ID:
            if operation_id == "nucleus_proactive_query":
                arguments_schema = _narrow_schema(
                    arguments_schema,
                    allowed=_INITIATIVE_QUERY_ARGUMENTS,
                    constants={"resource": "initiative"},
                )
            elif operation_id == "nucleus_proactive_command":
                arguments_schema = _narrow_schema(
                    arguments_schema,
                    allowed=_INITIATIVE_COMMAND_ARGUMENTS,
                    constants={},
                )
                arguments_schema["properties"]["action"]["enum"] = sorted(
                    _INITIATIVE_ACTIONS
                )
            else:
                raise NativeCapabilityPermissionDenied(
                    "initiative capability operation is outside its fixed slice"
                )
        elif capability == _INNER_RETURN_ID:
            if operation_id == "nucleus_proactive_query":
                arguments_schema = _narrow_schema(
                    arguments_schema,
                    allowed=_INNER_QUERY_ARGUMENTS,
                    constants={"resource": "inner_dialogue"},
                )
            elif operation_id == "nucleus_proactive_command":
                arguments_schema = _narrow_schema(
                    arguments_schema,
                    allowed=_INNER_COMMAND_ARGUMENTS,
                    constants={"action": "inner.return"},
                )
                extra["caller_surface"] = "life_engine_internal"
            else:
                raise NativeCapabilityPermissionDenied(
                    "inner-return capability operation is outside its fixed slice"
                )
        elif operation_id in {
            "nucleus_proactive_query",
            "nucleus_proactive_command",
        }:
            raise NativeCapabilityPermissionDenied(
                "proactive shared tools require an explicit capability slice"
            )

    result = {
        "schema_version": 1,
        "capability_id": capability,
        "operation": operation_id,
        "action": requested_action,
        "description": description,
        "arguments_schema": arguments_schema,
        "identity_bound_by_runtime": True,
        "accepts_identity_arguments": False,
        "grants_permission": False,
        "workflow_text_executable": False,
    }
    result.update(extra)
    return result


def _require_arguments(
    arguments: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise NativeCapabilityArgumentsInvalid("operation arguments must be an object")
    copied: dict[str, Any] = {}
    for key, value in arguments.items():
        if not isinstance(key, str):
            raise NativeCapabilityArgumentsInvalid("argument names must be strings")
        copied[key] = value
    forged = sorted(copied.keys() & _RESERVED_IDENTITY_ARGUMENTS)
    if forged:
        raise NativeCapabilityArgumentsInvalid(
            "operation arguments cannot replace runtime identity: " + ", ".join(forged)
        )
    unknown = sorted(copied.keys() - allowed)
    if unknown:
        raise NativeCapabilityArgumentsInvalid(
            "unknown operation arguments: " + ", ".join(unknown)
        )
    missing = sorted(required - copied.keys())
    if missing:
        raise NativeCapabilityArgumentsInvalid(
            "missing operation arguments: " + ", ".join(missing)
        )
    return copied


def _strict_tool_arguments(
    tool: BaseTool, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    signature = inspect.signature(tool.execute)
    allowed = frozenset(
        name
        for name, parameter in signature.parameters.items()
        if name != "self"
        and parameter.kind
        not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    )
    payload = _require_arguments(arguments, allowed=allowed)
    try:
        signature.bind(**payload)
    except TypeError as exc:
        raise NativeCapabilityArgumentsInvalid(str(exc)) from exc
    return payload


def _strict_learning_arguments(arguments: Mapping[str, Any]) -> None:
    """Prevent NucleusLearnTool's compatibility layer from dropping fields."""

    from ..learning import learn_tool

    payload = _require_arguments(
        arguments,
        allowed=frozenset({"action", "arguments"}),
        required=frozenset({"action"}),
    )
    action = learn_tool.normalize_learn_action(str(payload["action"]))
    nested = payload.get("arguments")
    if nested is None:
        nested_payload: Mapping[str, Any] = {}
    elif isinstance(nested, Mapping):
        nested_payload = nested
    else:
        raise NativeCapabilityArgumentsInvalid(
            "learning operation arguments must be an object"
        )
    if action == "help":
        if nested_payload:
            raise NativeCapabilityArgumentsInvalid(
                "learning help does not accept operation arguments"
            )
        return
    target = learn_tool._OPERATION_TOOLS.get(action)
    if target is None:
        raise NativeCapabilityArgumentsInvalid(f"unknown learning operation: {action}")
    # The inner tool is not constructed here: signature validation has no side effects.
    signature = inspect.signature(target.execute)
    allowed = frozenset(
        name
        for name, parameter in signature.parameters.items()
        if name != "self"
        and parameter.kind
        not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    )
    inner = _require_arguments(nested_payload, allowed=allowed)
    try:
        signature.bind(None, **inner)
    except TypeError as exc:
        raise NativeCapabilityArgumentsInvalid(str(exc)) from exc


def _runtime_surface(caller_tool: BaseTool) -> str:
    task_name = str(getattr(caller_tool, "_runtime_task_name", "") or "").strip()
    if task_name == "core":
        return "life_engine_internal"
    if not task_name:
        return "life_chatter"
    raise NativeCapabilityCallerInvalid(
        f"unsupported capability caller runtime: {task_name}"
    )


def _check_chatter_boundary(usable: type[BaseTool], surface: str) -> None:
    allowed = tuple(str(item) for item in getattr(usable, "chatter_allow", ()) or ())
    # This is the framework's existing semantics: an empty list is unrestricted.
    if allowed and surface not in allowed:
        raise NativeCapabilityPermissionDenied(
            f"operation is not allowed for caller surface: {surface}"
        )


def _verified_caller(value: Any) -> tuple[OpportunityCaller, BaseTool, str]:
    if type(value) is not OpportunityCaller:
        raise NativeCapabilityCallerInvalid("caller context must be OpportunityCaller")
    caller = value
    tool = caller.caller_tool
    if type(tool) is not opportunity_tools.LifeEngineCapabilityCallTool:
        raise NativeCapabilityCallerInvalid(
            "caller tool must be the original capability_call instance"
        )
    _service, actor = opportunity_tools._service_actor(tool)
    expected = {
        "actor_consciousness_instance_id": actor,
        "source_instance_id": opportunity_tools._source_instance(tool, actor),
        "source_occurrence_id": opportunity_tools._source_occurrence(tool),
        "decision_occurrence_id": opportunity_tools._decision_occurrence(tool),
        "occurred_at": opportunity_tools._occurred_at(tool),
    }
    for field, expected_value in expected.items():
        if str(getattr(caller, field, "") or "") != str(expected_value or ""):
            raise NativeCapabilityCallerInvalid(
                f"caller attribution does not match original tool: {field}"
            )
    surface = _runtime_surface(tool)
    _check_chatter_boundary(type(tool), surface)
    return caller, tool, surface


def _bind_original_runtime(source: BaseTool, target: BaseTool) -> None:
    target._bind_runtime_context(
        stream_id=source.get_current_stream_id(),
        message=source.trigger_message,
        tool_call_id=str(getattr(source, "_tool_call_id", "") or ""),
    )
    for name in (
        "_runtime_task_name",
        "_life_source_occurrence_id",
        "_life_source_occurred_at",
        "_life_source_instance_id",
        "_context_runtime_key",
        "chat_stream",
    ):
        if hasattr(source, name):
            setattr(target, name, getattr(source, name))


class NativeCapabilityDispatch:
    """Execute fixed native operations with per-call caller revalidation."""

    def __init__(
        self,
        *,
        opportunity_runtime: Callable[[], OpportunityManagementFacade],
    ) -> None:
        if not callable(opportunity_runtime):
            raise TypeError("opportunity_runtime must be a callable provider")
        self._opportunity_runtime = opportunity_runtime
        self._tools = _fixed_tool_classes()

    async def __call__(
        self,
        executor: CapabilityExecutor,
        invocation: CapabilityInvocation,
    ) -> NativeCapabilityResult:
        if type(executor) is not NativeCapabilityExecutor:
            raise NativeCapabilityExecutorMismatch(
                "native dispatch requires NativeCapabilityExecutor"
            )
        plan = await executor.execute(
            invocation.operation_id,
            invocation.arguments,
        )
        descriptor = executor.descriptor
        if (
            plan.capability_id != invocation.capability_id
            or plan.capability_id != descriptor.capability_id
            or plan.package_sha256 != descriptor.package_sha256
            or plan.operation_id != invocation.operation_id
            or dict(plan.arguments) != dict(invocation.arguments)
        ):
            raise NativeCapabilityExecutorMismatch(
                "native executor plan does not match immutable invocation"
            )
        caller, caller_tool, surface = _verified_caller(invocation.caller_context)

        if plan.capability_id == _SELF_AWAKEN_ID:
            result = await self._execute_self_awaken(plan, caller)
            return NativeCapabilityResult(
                capability_id=plan.capability_id,
                operation_id=plan.operation_id,
                success=True,
                value=result,
            )

        tool_cls = self._tools.get(plan.operation_id)
        if tool_cls is None:
            raise NativeCapabilityPermissionDenied(
                f"operation has no fixed native tool binding: {plan.operation_id}"
            )
        _check_chatter_boundary(tool_cls, surface)
        self._check_capability_slice(plan, surface)
        plugin = getattr(caller_tool, "plugin", None)
        if plugin is None:
            raise NativeCapabilityCallerInvalid("original caller plugin is missing")
        target = tool_cls(plugin=plugin)
        _bind_original_runtime(caller_tool, target)
        arguments = _strict_tool_arguments(target, plan.arguments)
        if plan.operation_id == "nucleus_learn":
            _strict_learning_arguments(arguments)
        # This task-local proof is narrower than caller permission: it only
        # establishes that this exact call passed the installed/enabled
        # capability admission above.  Legacy domain facades may require it,
        # while their actor and operation checks continue to apply.
        with bind_capability_execution(plan.capability_id):
            outcome = target.execute(**arguments)
            if not inspect.isawaitable(outcome):
                raise NativeCapabilityDispatchError(
                    "native tool execute must return an awaitable"
                )
            resolved = await outcome
        if (
            not isinstance(resolved, tuple)
            or len(resolved) != 2
            or type(resolved[0]) is not bool
        ):
            raise NativeCapabilityDispatchError(
                "native tool must return (bool, result)"
            )
        success, value = resolved
        return NativeCapabilityResult(
            capability_id=plan.capability_id,
            operation_id=plan.operation_id,
            success=success,
            value=value,
        )

    @staticmethod
    def _check_capability_slice(
        plan: _NativeOperationPlan,
        surface: str,
    ) -> None:
        if plan.operation_id in {
            "nucleus_proactive_query",
            "nucleus_proactive_command",
        } and plan.capability_id not in {_INITIATIVE_ID, _INNER_RETURN_ID}:
            raise NativeCapabilityPermissionDenied(
                "proactive shared tools require an explicit capability slice"
            )
        if plan.capability_id == _INITIATIVE_ID:
            if plan.operation_id == "nucleus_proactive_query":
                _require_arguments(
                    plan.arguments,
                    allowed=_INITIATIVE_QUERY_ARGUMENTS,
                    required=frozenset({"resource"}),
                )
                if str(plan.arguments.get("resource") or "") != "initiative":
                    raise NativeCapabilityPermissionDenied(
                        "initiative capability may only query initiative records"
                    )
            elif plan.operation_id == "nucleus_proactive_command":
                _require_arguments(
                    plan.arguments,
                    allowed=_INITIATIVE_COMMAND_ARGUMENTS,
                    required=frozenset({"action"}),
                )
                action = str(plan.arguments.get("action") or "")
                if action not in _INITIATIVE_ACTIONS:
                    raise NativeCapabilityPermissionDenied(
                        "initiative capability may only call its declared actions"
                    )
        if plan.capability_id == _INNER_RETURN_ID:
            if plan.operation_id == "nucleus_proactive_query":
                _require_arguments(
                    plan.arguments,
                    allowed=_INNER_QUERY_ARGUMENTS,
                    required=frozenset({"resource"}),
                )
                if str(plan.arguments.get("resource") or "") != "inner_dialogue":
                    raise NativeCapabilityPermissionDenied(
                        "inner-return capability may only query inner_dialogue"
                    )
            elif plan.operation_id == "nucleus_proactive_command":
                _require_arguments(
                    plan.arguments,
                    allowed=_INNER_COMMAND_ARGUMENTS,
                    required=frozenset({"action"}),
                )
                if str(plan.arguments.get("action") or "") != "inner.return":
                    raise NativeCapabilityPermissionDenied(
                        "inner-return capability may only call inner.return"
                    )
                if surface != "life_engine_internal":
                    raise NativeCapabilityPermissionDenied(
                        "inner.return is restricted to the heartbeat consciousness"
                    )

    async def _execute_self_awaken(
        self,
        plan: _NativeOperationPlan,
        caller: OpportunityCaller,
    ) -> Any:
        if plan.operation_id not in _SELF_AWAKEN_OPERATIONS:
            raise NativeCapabilityPermissionDenied(
                "self-awaken operation is not a runtime management operation"
            )
        runtime = self._opportunity_runtime()
        if inspect.isawaitable(runtime) or runtime is None:
            raise NativeCapabilityDispatchError(
                "opportunity runtime provider must synchronously return the facade"
            )
        if plan.operation_id == "opportunity.schedule":
            payload = _require_arguments(
                plan.arguments,
                allowed=frozenset(
                    {"target_id", "expected_revision", "arguments", "reason"}
                ),
                required=frozenset({"target_id", "expected_revision", "arguments"}),
            )
            nested = payload["arguments"]
            schedule_fields = frozenset(
                {
                    "provider_id",
                    "referent_kind",
                    "referent_id",
                    "referent_revision",
                    "referent_sha256",
                    "workflow_id",
                    "workflow_revision",
                    "workflow_sha256",
                    "schedule",
                    "first_due_at",
                    "interval_seconds",
                }
            )
            nested_payload = _require_arguments(nested, allowed=schedule_fields)
            revision = payload["expected_revision"]
            if type(revision) is not int or revision < 1:
                raise NativeCapabilityArgumentsInvalid(
                    "expected_revision must identify an existing registration "
                    "(integer >= 1); use opportunity.open for first registration"
                )
            call = runtime.manage(
                "opportunity.schedule",
                str(payload["target_id"]),
                payload["expected_revision"],
                nested_payload,
                str(payload.get("reason") or ""),
                caller,
            )
        else:
            payload = _require_arguments(
                plan.arguments,
                allowed=frozenset(
                    {
                        "resource",
                        "record_id",
                        "continuation",
                        "offset_bytes",
                        "max_bytes",
                        "limit",
                        "revision",
                        "family",
                    }
                ),
                required=frozenset({"resource"}),
            )
            call = runtime.query(
                resource=str(payload["resource"]),
                record_id=str(payload.get("record_id") or ""),
                continuation=str(payload.get("continuation") or ""),
                offset_bytes=payload.get("offset_bytes", 0),
                max_bytes=payload.get("max_bytes", 8192),
                limit=payload.get("limit", 20),
                revision=payload.get("revision", 0),
                family=str(payload.get("family") or "registration"),
                actor_consciousness_instance_id=(
                    caller.actor_consciousness_instance_id
                ),
            )
        if not inspect.isawaitable(call):
            raise NativeCapabilityDispatchError(
                "opportunity runtime facade methods must be asynchronous"
            )
        return await call


__all__ = [
    "NativeCapabilityArgumentsInvalid",
    "NativeCapabilityCallerInvalid",
    "NativeCapabilityDispatch",
    "NativeCapabilityDispatchError",
    "NativeCapabilityExecutor",
    "NativeCapabilityExecutorMismatch",
    "NativeCapabilityPermissionDenied",
    "NativeCapabilityResult",
    "OpportunityManagementFacade",
    "describe_operation_schema",
]
