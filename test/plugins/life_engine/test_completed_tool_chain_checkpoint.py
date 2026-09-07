"""Synthetic checkpoint boundaries for fully returned versus broken tool chains."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from plugins.life_engine.core import context_stewardship as stewardship
from plugins.life_engine.core.context_stewardship import (
    ContextStewardshipError,
    SubjectCheckpointCommand,
    build_group_manifest,
    prepare_subject_checkpoint,
)
from src.kernel.llm import ROLE, LLMPayload, Text, ToolCall, ToolResult
from src.kernel.llm.context import LLMContextManager


def _paired_payloads() -> list[LLMPayload]:
    return [
        LLMPayload(ROLE.USER, Text("synthetic old request")),
        LLMPayload(
            ROLE.ASSISTANT,
            ToolCall(id="synthetic-call", name="synthetic-tool", args={}),
        ),
        LLMPayload(
            ROLE.TOOL_RESULT, ToolResult(value={"ok": True}, call_id="synthetic-call")
        ),
        LLMPayload(ROLE.USER, Text("synthetic new request")),
    ]


def _command(
    payloads: list[LLMPayload],
    *,
    release_index: int = 0,
    retain_indexes: tuple[int, ...] = (),
) -> SubjectCheckpointCommand:
    manifest = build_group_manifest(payloads)
    return SubjectCheckpointCommand(
        actor_consciousness_instance_id="synthetic-instance",
        thought="SCRIPTED engineering fixture; no model or subject output.",
        continuity_text="SCRIPTED continuity for an exact completed tool group.",
        source_manifest_sha256=manifest.source_manifest_sha256,
        expected_revision=manifest.current_checkpoint_revision,
        release_through_group_ref=manifest.groups[release_index].group_ref,
        retain_exact_group_refs=tuple(
            manifest.groups[index].group_ref for index in retain_indexes
        ),
    )


def test_fully_paired_history_followed_by_new_user_is_closed() -> None:
    payloads = _paired_payloads()
    before = deepcopy(payloads)
    LLMContextManager().validate_for_send(payloads)
    manifest = build_group_manifest(payloads)
    assert len(manifest.groups) == 1
    assert manifest.groups[0].open_tool_chain is False
    assert payloads == before


def test_fully_paired_history_can_be_released_without_an_invented_assistant_tail() -> (
    None
):
    payloads = _paired_payloads()
    before = deepcopy(payloads)
    LLMContextManager().validate_for_send(payloads)
    prepared = prepare_subject_checkpoint(payloads, _command(payloads))
    assert len(prepared.released_groups) == 1
    assert prepared.released_groups[0].open_tool_chain is False
    assert prepared.retained_groups == ()
    assert (
        prepared.released_groups[0].record["payloads"][2]["role"]
        == ROLE.TOOL_RESULT.value
    )
    LLMContextManager().validate_for_send(prepared.payloads)
    assert prepared.payloads[0].content[0] == Text("synthetic new request")
    assert payloads == before


def _call(call_id: str | None = "call-a") -> ToolCall:
    return ToolCall(id=call_id, name="synthetic-tool", args={"nested": [1, 2]})


def _result(call_id: str | None = "call-a", *, ok: bool = True) -> ToolResult:
    return ToolResult(value={"ok": ok, "nested": [3, 4]}, call_id=call_id)


def _calls(*call_ids: str | None) -> LLMPayload:
    return LLMPayload(ROLE.ASSISTANT, [_call(call_id) for call_id in call_ids])


def _results(*call_ids: str | None) -> LLMPayload:
    return LLMPayload(ROLE.TOOL_RESULT, [_result(call_id) for call_id in call_ids])


def _history(*middle: LLMPayload) -> list[LLMPayload]:
    return [
        LLMPayload(ROLE.USER, Text("synthetic old request")),
        *middle,
        LLMPayload(ROLE.USER, Text("synthetic new request")),
    ]


@pytest.mark.parametrize("ok", [True, False], ids=["success", "returned-failure"])
def test_result_outcome_does_not_change_completed_pairing(ok: bool) -> None:
    payloads = _history(_calls("call-a"), LLMPayload(ROLE.TOOL_RESULT, _result(ok=ok)))
    before = deepcopy(payloads)
    LLMContextManager().validate_for_send(payloads)
    prepared = prepare_subject_checkpoint(payloads, _command(payloads))
    assert prepared.released_groups[0].open_tool_chain is False
    archived_result = prepared.released_groups[0].record["payloads"][2]["content"][0]
    assert archived_result["value"]["ok"] is ok
    assert payloads == before


@pytest.mark.parametrize(
    "middle",
    [
        [_calls("call-a", "call-b"), _results("call-b", "call-a")],
        [_calls("call-a", "call-b"), _results("call-b"), _results("call-a")],
        [
            _calls("call-a"),
            _results("call-a"),
            _calls("call-b"),
            _results("call-b"),
        ],
        [
            _calls("call-a"),
            _results("call-a"),
            _calls("call-a"),
            _results("call-a"),
        ],
    ],
    ids=[
        "merged-reverse-results",
        "split-reverse-results",
        "two-batches",
        "id-reused-in-next-batch",
    ],
)
def test_each_complete_tool_batch_is_releasable(middle: list[LLMPayload]) -> None:
    payloads = _history(*middle)
    before = deepcopy(payloads)
    LLMContextManager().validate_for_send(payloads)
    manifest = build_group_manifest(payloads)
    assert manifest.groups[0].open_tool_chain is False
    prepared = prepare_subject_checkpoint(payloads, _command(payloads))
    assert prepared.released_groups == manifest.groups
    LLMContextManager().validate_for_send(prepared.payloads)
    assert payloads == before


def test_latest_completed_result_tail_remains_conservative_and_unselectable() -> None:
    payloads = [
        LLMPayload(ROLE.USER, Text("older closed group")),
        *_paired_payloads()[:-1],
    ]
    before = deepcopy(payloads)
    LLMContextManager().validate_for_send(payloads)
    eligible = build_group_manifest(payloads)
    diagnostic = build_group_manifest(payloads, exclude_latest_group=False)
    assert len(eligible.groups) == 1
    assert len(diagnostic.groups) == 2
    assert diagnostic.groups[-1].open_tool_chain is True
    assert diagnostic.groups[-1].group_ref not in {
        group.group_ref for group in eligible.groups
    }
    with pytest.raises(ContextStewardshipError, match="not in the current manifest"):
        prepare_subject_checkpoint(
            payloads,
            replace(
                _command(payloads),
                release_through_group_ref=diagnostic.groups[-1].group_ref,
            ),
        )
    prepared = prepare_subject_checkpoint(payloads, _command(payloads))
    assert prepared.payloads[0].content[0] == before[1].content[0]
    assert prepared.payloads[-1] == before[-1]
    assert [
        part for part in prepared.payloads[-2].content if isinstance(part, ToolCall)
    ] == before[-2].content
    LLMContextManager().validate_for_send(prepared.payloads)
    assert payloads == before


def test_only_actual_next_user_changes_result_tail_eligibility_not_archive_identity() -> (
    None
):
    payloads = _paired_payloads()[:-1]
    before = deepcopy(payloads)
    assert build_group_manifest(payloads).groups == ()
    current_manifest = build_group_manifest(payloads, exclude_latest_group=False)
    historical_manifest = build_group_manifest(
        [*payloads, LLMPayload(ROLE.USER, Text("actual next request"))]
    )
    current = current_manifest.groups[0]
    historical = historical_manifest.groups[0]
    mechanical_default = stewardship._group_record(payloads, ordinal=1)
    assert mechanical_default.open_tool_chain is True
    assert mechanical_default.group_ref == current.group_ref
    assert (
        current_manifest.source_manifest_sha256
        == historical_manifest.source_manifest_sha256
    )
    assert current.open_tool_chain is True
    assert historical.open_tool_chain is False
    assert current.group_ref == historical.group_ref
    assert current.record == historical.record
    assert current.utf8_bytes == historical.utf8_bytes
    assert payloads == before


@pytest.mark.parametrize(
    "middle",
    [
        [_calls("call-a")],
        [_calls("call-a", "call-b"), _results("call-a")],
        [_results("call-a")],
        [_calls("call-a", "call-a"), _results("call-a")],
        [_calls("call-a"), _results("call-a", "call-a")],
        [_calls("call-a"), _results("call-a"), _results("call-a")],
        [_calls(None), _results("call-a")],
        [_calls(""), _results("call-a")],
        [_calls("call-a"), _results(None)],
        [_calls("call-a"), _results("")],
        [_calls("call-a"), _results("wrong-call")],
        [_results("call-a"), _calls("call-a"), _results("call-a")],
        [_calls("call-a"), LLMPayload(ROLE.ASSISTANT, Text("not a result"))],
        [
            _calls("call-a", "call-b"),
            _results("call-a"),
            _calls("call-b"),
            _results("call-b"),
        ],
        [
            _calls("call-a"),
            _results("call-a"),
            _calls("call-b"),
        ],
        [_calls("call-a"), LLMPayload(ROLE.TOOL_RESULT, Text("not a ToolResult"))],
        [LLMPayload(ROLE.ASSISTANT, [_call(), _result()]), _results("call-a")],
        [_calls("call-a"), LLMPayload(ROLE.TOOL_RESULT, [_result(), _call()])],
        [LLMPayload(ROLE.ASSISTANT, _result())],
        [LLMPayload(ROLE.TOOL_RESULT, _call())],
    ],
    ids=[
        "unreturned-call",
        "partially-returned-batch",
        "orphan-result",
        "duplicate-call-in-batch",
        "duplicate-results-in-payload",
        "duplicate-results-across-payloads",
        "missing-call-id-none",
        "missing-call-id-empty",
        "missing-result-id-none",
        "missing-result-id-empty",
        "mismatched-result-id",
        "result-before-call",
        "assistant-text-cannot-close-missing-result",
        "later-batch-cannot-supply-previous-batch-missing-result",
        "later-batch-still-open",
        "empty-result-payload",
        "result-in-assistant-role",
        "call-in-result-role",
        "orphan-result-in-assistant-role",
        "orphan-call-in-result-role",
    ],
)
def test_malformed_historical_tool_chain_stays_open_and_cannot_be_released(
    middle: list[LLMPayload],
) -> None:
    payloads = _history(*middle)
    before = deepcopy(payloads)
    manifest = build_group_manifest(payloads)
    assert manifest.groups[0].open_tool_chain is True
    with pytest.raises(ContextStewardshipError, match="open tool chain"):
        prepare_subject_checkpoint(payloads, _command(payloads))
    # In particular, classification must not invoke kernel's missing-ID repair.
    assert payloads == before


def test_results_in_a_later_user_group_cannot_close_an_earlier_group() -> None:
    payloads = [
        LLMPayload(ROLE.USER, Text("first request")),
        _calls("call-a"),
        LLMPayload(ROLE.USER, Text("second request")),
        _results("call-a"),
        LLMPayload(ROLE.USER, Text("current request")),
    ]
    before = deepcopy(payloads)
    manifest = build_group_manifest(payloads)
    assert [group.open_tool_chain for group in manifest.groups] == [True, True]
    with pytest.raises(ContextStewardshipError, match="open tool chain"):
        prepare_subject_checkpoint(payloads, _command(payloads, release_index=1))
    assert payloads == before


@pytest.mark.parametrize("wrong_part", [_call(), _result()], ids=["call", "result"])
def test_tool_content_in_user_role_is_not_a_completed_history(
    wrong_part: object,
) -> None:
    payloads = _history(_calls("call-a"), _results("call-a"))
    payloads[0].content.append(wrong_part)
    before = deepcopy(payloads)
    assert build_group_manifest(payloads).groups[0].open_tool_chain is True
    with pytest.raises(ContextStewardshipError, match="open tool chain"):
        prepare_subject_checkpoint(payloads, _command(payloads))
    assert payloads == before


def test_open_selected_group_can_be_retained_exactly_while_closed_neighbor_is_released() -> (
    None
):
    # This deliberately models malformed stored history. Retention must preserve
    # it, not silently repair it or claim that it is safe to send to a provider.
    payloads = [
        LLMPayload(ROLE.USER, Text("open group retained by explicit choice")),
        _calls("still-open"),
        *_paired_payloads(),
    ]
    before = deepcopy(payloads)
    manifest = build_group_manifest(payloads)
    assert [group.open_tool_chain for group in manifest.groups] == [True, False]
    command = _command(payloads, release_index=1, retain_indexes=(0,))
    prepared = prepare_subject_checkpoint(payloads, command)
    assert prepared.retained_groups == (manifest.groups[0],)
    assert prepared.released_groups == (manifest.groups[1],)
    assert prepared.payloads[:2] == before[:2]
    assert prepared.payloads[0] is payloads[0]
    assert prepared.payloads[1] is payloads[1]
    assert prepared.payloads[2].content[0] == before[-1].content[0]
    assert payloads == before


@pytest.mark.parametrize("assistant_completion", [False, True])
def test_ordinary_closed_history_keeps_existing_release_behavior(
    assistant_completion: bool,
) -> None:
    middle = (
        [LLMPayload(ROLE.ASSISTANT, Text("ordinary reply"))]
        if assistant_completion
        else []
    )
    payloads = _history(*middle)
    before = deepcopy(payloads)
    LLMContextManager().validate_for_send(payloads)
    prepared = prepare_subject_checkpoint(payloads, _command(payloads))
    assert prepared.released_groups[0].open_tool_chain is False
    LLMContextManager().validate_for_send(prepared.payloads)
    assert payloads == before


def test_live_group_read_keeps_latest_completed_result_tail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = _paired_payloads()[:-1]
    before = deepcopy(payloads)
    group_ref = (
        build_group_manifest(payloads, exclude_latest_group=False).groups[0].group_ref
    )
    monkeypatch.setattr(stewardship, "_live_window_payloads", lambda _key: payloads)
    monkeypatch.setattr(stewardship, "get_live_context", lambda _key: None)
    record, window = stewardship._live_context_group(
        group_ref, runtime_key="synthetic-readonly-runtime"
    )
    assert record is not None
    assert record.group_ref == group_ref
    assert record.open_tool_chain is True
    assert window is not None
    assert window.payloads == before
    assert payloads == before


@pytest.mark.parametrize("pinned_role", [ROLE.SYSTEM, ROLE.TOOL])
def test_pure_pinned_content_inside_tool_batch_does_not_break_completed_pairing(
    pinned_role: ROLE,
) -> None:
    payloads = _history(
        _calls("call-a"),
        LLMPayload(pinned_role, Text("synthetic pinned schema or instruction")),
        _results("call-a"),
    )
    before = deepcopy(payloads)
    LLMContextManager().validate_for_send(payloads)
    prepared = prepare_subject_checkpoint(payloads, _command(payloads))
    assert prepared.released_groups[0].open_tool_chain is False
    assert (
        prepared.released_groups[0].record["payloads"][2]["role"] == pinned_role.value
    )
    LLMContextManager().validate_for_send(prepared.payloads)
    assert payloads == before


@pytest.mark.parametrize("pinned_role", [ROLE.SYSTEM, ROLE.TOOL])
@pytest.mark.parametrize("wrong_part", [_call(), _result()], ids=["call", "result"])
def test_pinned_role_cannot_hide_tool_call_or_result_content(
    pinned_role: ROLE,
    wrong_part: object,
) -> None:
    payloads = _history(
        _calls("call-a"),
        LLMPayload(pinned_role, wrong_part),
        _results("call-a"),
    )
    before = deepcopy(payloads)
    assert build_group_manifest(payloads).groups[0].open_tool_chain is True
    with pytest.raises(ContextStewardshipError, match="open tool chain"):
        prepare_subject_checkpoint(payloads, _command(payloads))
    assert payloads == before
