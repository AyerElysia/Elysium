from __future__ import annotations

import json
from dataclasses import dataclass

from plugins.live_bridge.sts2_operator import (
    build_fallback_decision,
    extract_decision_result,
    parse_sts2_decision_request,
)


@dataclass(slots=True)
class MessageStub:
    role: str
    content: str


def _request_payload() -> dict:
    return {
        "request_id": "req_1",
        "snapshot_id": "snap_1",
        "actor_id": "ai_1",
        "decision_kind": "combat",
        "context": {
            "screen": "combat",
            "summary": "enemy attacks for 8, player has 3 energy",
        },
        "legal_actions": [
            {
                "actionId": "play_strike_target_0",
                "actionType": "play_card",
                "description": "Play Strike on Cultist",
                "energyCost": 1,
            },
            {
                "actionId": "end_turn_ai_1",
                "actionType": "end_turn",
                "description": "End turn",
            },
        ],
    }


def test_parse_sts2_decision_request_from_openai_messages() -> None:
    messages = [
        MessageStub("system", "You are an action selector for a Slay the Spire 2 AI teammate."),
        MessageStub("user", json.dumps(_request_payload())),
    ]

    request = parse_sts2_decision_request(messages)

    assert request is not None
    assert request.request_id == "req_1"
    assert request.snapshot_id == "snap_1"
    assert request.legal_action_ids == ["play_strike_target_0", "end_turn_ai_1"]


def test_extract_decision_result_validates_legal_action() -> None:
    request = parse_sts2_decision_request(
        [
            MessageStub("system", "Slay the Spire 2 AI teammate"),
            MessageStub("user", json.dumps(_request_payload())),
        ]
    )
    assert request is not None

    result = extract_decision_result(
        '{"chosen_action_id":"play_strike_target_0","ranked_action_ids":["play_strike_target_0"],"reason":"lethal setup"}',
        request,
    )

    assert result is not None
    assert result.chosen_action_id == "play_strike_target_0"
    assert result.ranked_action_ids == ["play_strike_target_0", "end_turn_ai_1"]


def test_extract_decision_result_rejects_illegal_action() -> None:
    request = parse_sts2_decision_request(
        [
            MessageStub("system", "Slay the Spire 2 AI teammate"),
            MessageStub("user", json.dumps(_request_payload())),
        ]
    )
    assert request is not None

    result = extract_decision_result('{"chosen_action_id":"not_legal"}', request)

    assert result is None


def test_fallback_skips_high_risk_action_when_possible() -> None:
    payload = _request_payload()
    payload["legal_actions"] = [
        {"actionId": "abandon_run", "actionType": "danger"},
        {"actionId": "end_turn_ai_1", "actionType": "end_turn"},
    ]
    request = parse_sts2_decision_request(
        [
            MessageStub("system", "Slay the Spire 2 AI teammate"),
            MessageStub("user", json.dumps(payload)),
        ]
    )
    assert request is not None

    result = build_fallback_decision(request, "bad reply")

    assert result.chosen_action_id == "end_turn_ai_1"
