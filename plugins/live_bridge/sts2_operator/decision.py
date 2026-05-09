"""Pure STS2 teammate decision parsing and formatting helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


_STS2_SYSTEM_MARKERS = (
    "Slay the Spire 2 AI teammate",
    "action selector for a Slay the Spire 2",
)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.IGNORECASE | re.DOTALL)
_ACTION_KEY_RE = re.compile(
    r'"?(?:chosen_action_id|chosenActionId|action_id|action|chosen)"?\s*[:=]\s*"?(?P<id>[A-Za-z0-9_.:\-]+)"?',
    re.IGNORECASE,
)
_HIGH_RISK_ACTIONS = {
    "abandon_run",
    "run_console_command",
}


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _shorten(value: Any, *, limit: int = 180) -> str:
    text = " ".join(_as_text(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def _read_action_id(action: Mapping[str, Any]) -> str:
    return _as_text(
        action.get("action_id")
        or action.get("actionId")
        or action.get("ActionId")
        or action.get("id")
    ).strip()


def _read_action_field(action: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        if key in action and action[key] is not None:
            return _as_text(action[key]).strip()
    return ""


def _loads_json_object(text: str) -> dict[str, Any] | None:
    raw = _as_text(text).strip()
    if not raw:
        return None

    candidates = [raw]
    for match in _JSON_BLOCK_RE.finditer(raw):
        candidates.append(match.group("body").strip())

    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(raw[first_brace : last_brace + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


@dataclass(slots=True)
class Sts2DecisionRequest:
    request_id: str
    snapshot_id: str
    actor_id: str
    decision_kind: str
    context: dict[str, Any] = field(default_factory=dict)
    legal_actions: list[dict[str, Any]] = field(default_factory=list)
    source_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def legal_action_ids(self) -> list[str]:
        return [action_id for action_id in (_read_action_id(action) for action in self.legal_actions) if action_id]


@dataclass(slots=True)
class Sts2DecisionResult:
    chosen_action_id: str
    ranked_action_ids: list[str]
    reason: str
    source: str = "elysia"

    def to_openai_content(self) -> str:
        return json.dumps(
            {
                "chosen_action_id": self.chosen_action_id,
                "ranked_action_ids": self.ranked_action_ids,
                "reason": self.reason,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def parse_sts2_decision_request(messages: Sequence[Any]) -> Sts2DecisionRequest | None:
    """Return an STS2 decision request when an OpenAI payload matches the mod contract."""

    system_text = "\n".join(
        _as_text(getattr(message, "content", ""))
        for message in messages
        if _as_text(getattr(message, "role", "")).strip().lower() == "system"
    )
    has_system_marker = any(marker in system_text for marker in _STS2_SYSTEM_MARKERS)

    user_content = ""
    for message in reversed(messages):
        if _as_text(getattr(message, "role", "")).strip().lower() == "user":
            user_content = _as_text(getattr(message, "content", ""))
            break
    if not user_content and messages:
        user_content = _as_text(getattr(messages[-1], "content", ""))

    payload = _loads_json_object(user_content)
    if not payload:
        return None

    legal_actions = payload.get("legal_actions") or payload.get("legalActions")
    if not isinstance(legal_actions, list):
        return None
    if not all(isinstance(action, dict) for action in legal_actions):
        return None

    request_id = _as_text(payload.get("request_id") or payload.get("requestId")).strip()
    snapshot_id = _as_text(payload.get("snapshot_id") or payload.get("snapshotId")).strip()
    actor_id = _as_text(payload.get("actor_id") or payload.get("actorId")).strip()
    decision_kind = _as_text(payload.get("decision_kind") or payload.get("decisionKind")).strip()
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}

    if not has_system_marker and not (request_id and snapshot_id and actor_id and legal_actions):
        return None

    return Sts2DecisionRequest(
        request_id=request_id or "sts2_request_unknown",
        snapshot_id=snapshot_id or "snapshot_unknown",
        actor_id=actor_id or "actor_unknown",
        decision_kind=decision_kind or "unknown",
        context=dict(context),
        legal_actions=[dict(action) for action in legal_actions],
        source_payload=payload,
    )


def build_decision_prompt(request: Sts2DecisionRequest) -> str:
    """Compress the STS2 request into a main-consciousness decision prompt."""

    context_lines = []
    for key, value in sorted(request.context.items(), key=lambda item: str(item[0])):
        if value is None or value == "":
            continue
        context_lines.append(f"- {key}: {_shorten(value, limit=260)}")

    action_lines = []
    for index, action in enumerate(request.legal_actions, start=1):
        action_id = _read_action_id(action)
        if not action_id:
            continue
        action_type = _read_action_field(action, "action_type", "actionType", "ActionType")
        label = _read_action_field(action, "label", "Label")
        summary = _read_action_field(action, "summary", "Summary")
        description = _read_action_field(action, "description", "Description")
        target = _read_action_field(action, "target_label", "targetLabel", "TargetLabel")
        energy = action.get("energy_cost", action.get("energyCost", action.get("EnergyCost")))
        tags = action.get("priority_tags") or action.get("priorityTags") or action.get("PriorityTags")
        tag_text = ", ".join(_as_text(item) for item in tags[:6]) if isinstance(tags, list) else ""
        fields = [
            f"id={action_id}",
            f"type={action_type}" if action_type else "",
            f"label={_shorten(label, limit=80)}" if label else "",
            f"target={_shorten(target, limit=80)}" if target else "",
            f"energy={energy}" if energy is not None else "",
            f"summary={_shorten(summary or description, limit=180)}" if (summary or description) else "",
            f"tags={_shorten(tag_text, limit=100)}" if tag_text else "",
        ]
        action_lines.append(f"{index}. " + " | ".join(part for part in fields if part))

    return (
        "【STS2 操作AI -> 爱莉决策请求】\n"
        "你是统一主意识，只负责判断战略和选择动作；具体操作由 STS2 操作AI 执行。\n"
        "请只回复严格 JSON，不要解释到 JSON 外面："
        '{"chosen_action_id":"从候选动作中原样复制一个 id","ranked_action_ids":["按偏好排序的动作 id"],"reason":"一句话原因"}\n\n'
        f"request_id: {request.request_id}\n"
        f"snapshot_id: {request.snapshot_id}\n"
        f"actor_id: {request.actor_id}\n"
        f"decision_kind: {request.decision_kind}\n\n"
        "当前局势摘要：\n"
        + ("\n".join(context_lines) if context_lines else "- 无额外上下文")
        + "\n\n合法候选动作：\n"
        + ("\n".join(action_lines) if action_lines else "- 无合法动作")
    )


def build_fallback_decision(request: Sts2DecisionRequest, reason: str) -> Sts2DecisionResult:
    legal_ids = request.legal_action_ids
    if not legal_ids:
        raise ValueError("STS2 decision request has no legal action ids")

    chosen = next((action_id for action_id in legal_ids if action_id not in _HIGH_RISK_ACTIONS), legal_ids[0])
    return Sts2DecisionResult(
        chosen_action_id=chosen,
        ranked_action_ids=legal_ids,
        reason=f"operator fallback: {reason}",
        source="operator_fallback",
    )


def extract_decision_result(reply_text: str, request: Sts2DecisionRequest) -> Sts2DecisionResult | None:
    """Parse Elysia's reply and validate it against current legal action ids."""

    legal_ids = request.legal_action_ids
    if not legal_ids:
        return None

    parsed = _loads_json_object(reply_text)
    chosen = ""
    ranked: list[str] = []
    reason = ""
    if parsed:
        chosen = _as_text(
            parsed.get("chosen_action_id")
            or parsed.get("chosenActionId")
            or parsed.get("action_id")
            or parsed.get("action")
            or parsed.get("chosen")
        ).strip()
        raw_ranked = parsed.get("ranked_action_ids") or parsed.get("rankedActionIds")
        if isinstance(raw_ranked, list):
            ranked = [_as_text(item).strip() for item in raw_ranked if _as_text(item).strip()]
        reason = _as_text(parsed.get("reason") or parsed.get("reasoning")).strip()

    if not chosen:
        match = _ACTION_KEY_RE.search(_as_text(reply_text))
        if match:
            chosen = match.group("id").strip()

    if chosen not in legal_ids:
        for action_id in sorted(legal_ids, key=len, reverse=True):
            if action_id and action_id in _as_text(reply_text):
                chosen = action_id
                break

    if chosen not in legal_ids:
        return None

    ranked_valid = _dedupe([action_id for action_id in ranked if action_id in legal_ids])
    if chosen not in ranked_valid:
        ranked_valid.insert(0, chosen)
    for action_id in legal_ids:
        if action_id not in ranked_valid:
            ranked_valid.append(action_id)

    return Sts2DecisionResult(
        chosen_action_id=chosen,
        ranked_action_ids=ranked_valid,
        reason=_shorten(reason or "爱莉选择了该合法动作。", limit=240),
    )


def _dedupe(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
