from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.context_compaction import (
    SUMMARY_CLOSE,
    SUMMARY_INTRO,
    SUMMARY_OPEN,
    hierarchical_compact_payloads,
    is_summary_payload,
)
from plugins.life_engine.core.context_stewardship import (
    CHECKPOINT_OPEN,
    COMPRESSION_REQUIRED_OPEN,
    OMISSION_OPEN,
    PRESSURE_OPEN,
    ContextStewardshipError,
    LifeAuthorSelfContinuityCheckpointAction,
    LifeReadContextGroupTool,
    SubjectCheckpointCommand,
    append_context_pressure_notice,
    apply_pending_subject_checkpoint,
    archive_context_groups,
    build_context_pressure_notice,
    build_group_manifest,
    build_mechanical_omission_payloads,
    ensure_compression_required_appended,
    has_compression_required_payload,
    mechanically_bound_payloads,
    prepare_subject_checkpoint,
    read_context_group_archive,
    reset_pending_subject_checkpoint,
    reset_transient_context_pressure_notices,
    strip_context_pressure_notices,
)
from plugins.life_engine.core.plugin import LifeEnginePlugin
from plugins.life_engine.service.tool_manifests import get_tool_manifest
from plugins.life_engine.storage.runtime_contracts import RuntimeStateConflict
from src.kernel.llm import ROLE, LLMPayload, ReasoningText, Text, ToolCall, ToolResult
from src.kernel.llm.context import LLMContextManager
from src.kernel.storage import canonical_json


def _estimate(payloads: list[LLMPayload] | tuple[LLMPayload, ...]) -> int:
    return len(str(payloads).encode("utf-8"))


def _conversation() -> list[LLMPayload]:
    return [
        LLMPayload(ROLE.SYSTEM, [Text("system contract")]),
        LLMPayload(ROLE.USER, [Text("old-user-one")]),
        LLMPayload(ROLE.ASSISTANT, [Text("old-assistant-one")]),
        LLMPayload(ROLE.USER, [Text("old-user-two")]),
        LLMPayload(ROLE.ASSISTANT, [Text("old-assistant-two")]),
        LLMPayload(ROLE.USER, [Text("current-user")]),
    ]


def _checkpoint_command(
    payloads: list[LLMPayload],
    *,
    continuity_text: str = "我记得我们已经谈过前两件事；现在继续回应眼前这一句。",
) -> SubjectCheckpointCommand:
    manifest = build_group_manifest(payloads)
    return SubjectCheckpointCommand(
        actor_consciousness_instance_id="chat_global",
        thought="旧工作上下文已经接近容量边界，我选择亲自留下连续性。",
        continuity_text=continuity_text,
        source_manifest_sha256=manifest.source_manifest_sha256,
        expected_revision=manifest.current_checkpoint_revision,
        release_through_group_ref=manifest.groups[0].group_ref,
        retain_exact_group_refs=(),
    )


def test_pressure_notice_is_task_aware_and_contains_no_group_bodies() -> None:
    payloads = _conversation()
    response = SimpleNamespace(
        payloads=payloads,
        model_set=[
            {
                "model_identifier": "gpt-4",
                "max_context": 1024,
                "context_tokens": 24,
                "max_tokens": 1,
                "extra_params": {},
            }
        ],
    )

    notice = build_context_pressure_notice(response, trigger_ratio=0.1)

    assert notice is not None
    assert notice.text.startswith(PRESSURE_OPEN)
    assert "ctxg_" in notice.text
    assert "old-user-one" not in notice.text
    assert "old-assistant-one" not in notice.text
    assert "只有你能决定" in notice.text


def test_pressure_notice_not_emitted_below_ratio() -> None:
    response = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, [Text("short")])],
        model_set=[
            {
                "model_identifier": "gpt-4",
                "max_context": 100_000,
                "context_tokens": 100_000,
                "max_tokens": 1,
                "extra_params": {},
            }
        ],
    )
    assert build_context_pressure_notice(response, trigger_ratio=0.9) is None


def test_pressure_notice_has_a_strict_utf8_byte_budget() -> None:
    payloads: list[LLMPayload] = []
    for index in range(80):
        payloads.extend(
            [
                LLMPayload(ROLE.USER, [Text(f"private-user-{index}")]),
                LLMPayload(ROLE.ASSISTANT, [Text(f"private-answer-{index}")]),
            ]
        )
    payloads.append(LLMPayload(ROLE.USER, [Text("current")]))
    response = SimpleNamespace(
        payloads=payloads,
        model_set=[
            {
                "model_identifier": "gpt-4",
                "max_context": 1024,
                "context_tokens": 1,
                "max_tokens": 1,
                "extra_params": {},
            }
        ],
    )

    notice = build_context_pressure_notice(
        response,
        trigger_ratio=0.1,
        max_groups=64,
        max_bytes=8 * 1024,
    )

    assert notice is not None
    assert len(notice.text.encode("utf-8")) <= 8 * 1024
    assert "private-user" not in notice.text
    assert '"listed_group_count": 64' not in notice.text
    assert build_context_pressure_notice(
        response,
        trigger_ratio=0.1,
        max_groups=64,
        max_bytes=256,
    ) is None


def test_strict_pressure_strip_does_not_remove_ordinary_user_text() -> None:
    ordinary = Text(f"我只是提到 {PRESSURE_OPEN}，不是系统通知")
    response = SimpleNamespace(payloads=[LLMPayload(ROLE.USER, [ordinary])])
    strip_context_pressure_notices(response)
    assert response.payloads[0].content == [ordinary]


def test_pressure_strip_uses_transport_identity_not_spoofable_text() -> None:
    source = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, [Text("current")])],
        model_set=[
            {
                "model_identifier": "gpt-4",
                "max_context": 1024,
                "context_tokens": 24,
                "max_tokens": 1,
                "extra_params": {},
            }
        ],
    )
    notice = build_context_pressure_notice(source, trigger_ratio=0.1)
    assert notice is not None
    spoof = Text(notice.text)
    source.payloads[0].content.append(spoof)
    append_context_pressure_notice(source, notice)
    try:
        strip_context_pressure_notices(source)
        assert source.payloads[0].content == [Text("current"), spoof]
    finally:
        reset_transient_context_pressure_notices()


def test_manifest_is_stable_and_excludes_latest_inflight_group() -> None:
    payloads = _conversation()
    first = build_group_manifest(payloads)
    payloads.extend(
        [
            LLMPayload(
                ROLE.ASSISTANT,
                [ToolCall(id="current-call", name="current", args={"secret": "x"})],
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                [ToolResult(value="current-result", call_id="current-call", name="current")],
            ),
        ]
    )
    second = build_group_manifest(payloads)
    assert first.source_manifest_sha256 == second.source_manifest_sha256
    assert [row.group_ref for row in first.groups] == [
        row.group_ref for row in second.groups
    ]


def test_group_archive_identity_includes_reasoning_replay_metadata() -> None:
    payloads = [
        LLMPayload(ROLE.USER, [Text("old-user")]),
        LLMPayload(
            ROLE.ASSISTANT,
            [
                ReasoningText(
                    "subject reasoning",
                    signature="provider-signature",
                    redacted_data="provider-redacted-data",
                ),
                Text("old-response"),
            ],
        ),
        LLMPayload(ROLE.USER, [Text("current")]),
    ]

    record = build_group_manifest(payloads).groups[0]
    reasoning = record.record["payloads"][1]["content"][0]

    assert reasoning == {
        "type": "reasoning_text",
        "text": "subject reasoning",
        "signature": "provider-signature",
        "redacted_data": "provider-redacted-data",
    }


def test_subject_checkpoint_preserves_exact_authored_text_and_selected_history() -> None:
    payloads = _conversation()
    continuity = "这是我自己写的连续性：保留语气、关系与未完成的问题，不替我解释。"
    command = _checkpoint_command(payloads, continuity_text=continuity)

    prepared = prepare_subject_checkpoint(payloads, command)

    rendered = str(prepared.payloads)
    assert prepared.revision == 1
    assert prepared.released_groups[0].record["payloads"][0]["content"][0]["text"] == "old-user-one"
    assert continuity in rendered
    assert command.thought not in rendered
    assert "old-user-one" not in rendered
    assert "old-user-two" in rendered
    assert "current-user" in rendered
    assert any(
        isinstance(part, Text) and part.text.startswith(CHECKPOINT_OPEN)
        for payload in prepared.payloads
        for part in payload.content
    )
    LLMContextManager().validate_for_send(prepared.payloads)
    assert build_group_manifest(
        [*prepared.payloads, LLMPayload(ROLE.USER, [Text("next-user")])]
    ).current_checkpoint_revision == 1


def test_checkpoint_rejects_stale_manifest_unknown_ref_and_open_tool_chain() -> None:
    payloads = _conversation()
    command = _checkpoint_command(payloads)
    with pytest.raises(ContextStewardshipError, match="stale"):
        prepare_subject_checkpoint(
            payloads,
            replace(command, source_manifest_sha256="0" * 64),
        )
    with pytest.raises(ContextStewardshipError, match="not in"):
        prepare_subject_checkpoint(
            payloads,
            replace(command, release_through_group_ref="ctxg_" + "1" * 64),
        )

    open_payloads = [
        LLMPayload(ROLE.USER, [Text("old")]),
        LLMPayload(
            ROLE.ASSISTANT,
            [ToolCall(id="open", name="inspect", args={"q": "kept"})],
        ),
        LLMPayload(ROLE.USER, [Text("current")]),
    ]
    manifest = build_group_manifest(open_payloads)
    open_command = SubjectCheckpointCommand(
        actor_consciousness_instance_id="chat_global",
        thought="我决定检查边界。",
        continuity_text="我不会释放未闭合工具链。",
        source_manifest_sha256=manifest.source_manifest_sha256,
        expected_revision=0,
        release_through_group_ref=manifest.groups[0].group_ref,
        retain_exact_group_refs=(),
    )
    with pytest.raises(ContextStewardshipError, match="open tool chain"):
        prepare_subject_checkpoint(open_payloads, open_command)

    with pytest.raises(ContextStewardshipError, match="duplicates"):
        prepare_subject_checkpoint(
            payloads,
            replace(
                command,
                retain_exact_group_refs=(
                    command.release_through_group_ref,
                    command.release_through_group_ref,
                ),
            ),
        )


def test_mechanical_projection_never_copies_message_tool_args_or_results() -> None:
    dropped = [
        [
            LLMPayload(ROLE.USER, [Text("PRIVATE-USER-BODY")]),
            LLMPayload(
                ROLE.ASSISTANT,
                [ToolCall(id="c1", name="read", args={"secret": "PRIVATE-ARG"})],
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                [ToolResult(value="PRIVATE-RESULT", call_id="c1", name="read")],
            ),
        ]
    ]
    payloads = build_mechanical_omission_payloads(dropped, max_bytes=2048)
    rendered = str(payloads)
    assert OMISSION_OPEN in rendered
    assert "ctxg_" in rendered
    assert "PRIVATE-USER-BODY" not in rendered
    assert "PRIVATE-ARG" not in rendered
    assert "PRIVATE-RESULT" not in rendered


def test_mechanical_hard_bound_returns_exact_records_without_semantic_summary() -> None:
    payloads = [
        LLMPayload(ROLE.USER, [Text(f"user-{index}-" + "中" * 500)])
        for index in range(6)
    ]
    result, records = mechanically_bound_payloads(
        payloads,
        estimate=_estimate,
        hard_budget=1200,
        reference_max_bytes=1024,
    )
    assert result.triggered
    assert records
    assert _estimate(result.payloads) <= 1200
    rendered = str(result.payloads)
    assert OMISSION_OPEN in rendered
    assert "user-0-" not in rendered
    assert SUMMARY_OPEN not in rendered
    assert records[0].record["payloads"][0]["content"][0]["text"].startswith("user-0-")


@pytest.mark.asyncio
async def test_exact_archive_is_content_addressed_and_utf8_pageable(tmp_path: Path) -> None:
    payloads = [
        LLMPayload(ROLE.USER, [Text("精确归档-" + "爱莉希雅" * 200)]),
        LLMPayload(ROLE.ASSISTANT, [Text("完整回应")]),
        LLMPayload(ROLE.USER, [Text("current")]),
    ]
    record = build_group_manifest(payloads).groups[0]
    await archive_context_groups([record], service=None, workspace_path=str(tmp_path))
    archive = await read_context_group_archive(
        record.group_ref,
        service=None,
        workspace_path=str(tmp_path),
    )
    assert archive["record"] == record.record

    plugin = SimpleNamespace(
        service=None,
        config=SimpleNamespace(
            settings=SimpleNamespace(workspace_path=str(tmp_path))
        ),
    )
    tool = LifeReadContextGroupTool(plugin)
    offset = 0
    chunks: list[str] = []
    while True:
        ok, page = await tool.execute(
            record.group_ref,
            offset_bytes=offset,
            max_bytes=256,
        )
        assert ok
        chunks.append(page["content"])
        assert page["delivered_bytes"] <= 256
        offset = page["next_offset_bytes"]
        if page["complete"]:
            break
    assert "".join(chunks) == canonical_json(record.record)

    archive_path = tmp_path / "runtime" / "context_archive" / f"{record.group_ref}.json"
    archive_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ContextStewardshipError, match="mismatch"):
        await read_context_group_archive(
            record.group_ref,
            service=None,
            workspace_path=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_local_archive_is_idempotent_under_concurrent_writers(
    tmp_path: Path,
) -> None:
    record = build_group_manifest(_conversation()).groups[0]

    await asyncio.gather(
        *(
            archive_context_groups(
                [record],
                service=None,
                workspace_path=str(tmp_path),
            )
            for _ in range(8)
        )
    )

    archive = await read_context_group_archive(
        record.group_ref,
        service=None,
        workspace_path=str(tmp_path),
    )
    assert archive["record"] == record.record
    archive_dir = tmp_path / "runtime" / "context_archive"
    assert list(archive_dir.glob("*.tmp-*")) == []


@pytest.mark.asyncio
async def test_selected_store_archive_accepts_only_matching_cas_winner() -> None:
    record = build_group_manifest(_conversation()).groups[0]
    stored: dict[str, object] = {}

    class RacingStore:
        async def get_state(self, namespace: str, state_key: str):
            assert namespace == "life_chatter.context_archive"
            return stored.get(state_key)

        async def put_state(self, **kwargs):
            stored[kwargs["state_key"]] = SimpleNamespace(payload=kwargs["payload"])
            raise RuntimeStateConflict("simulated concurrent create")

    service = SimpleNamespace(runtime_state_store=lambda: RacingStore())
    await archive_context_groups([record], service=service, workspace_path="")

    stored[record.group_ref] = SimpleNamespace(payload={"schema": "wrong"})
    with pytest.raises(ContextStewardshipError, match="mismatch"):
        await archive_context_groups([record], service=service, workspace_path="")


@pytest.mark.asyncio
async def test_local_archive_fails_closed_without_explicit_workspace() -> None:
    record = build_group_manifest(_conversation()).groups[0]
    with pytest.raises(ContextStewardshipError, match="workspace_path is missing"):
        await archive_context_groups([record], service=None, workspace_path="")
    with pytest.raises(ContextStewardshipError, match="workspace_path is missing"):
        await read_context_group_archive(
            record.group_ref,
            service=None,
            workspace_path="",
        )
    with pytest.raises(ContextStewardshipError, match="group_ref is invalid"):
        await read_context_group_archive(
            "ctxg_" + "." * 64,
            service=None,
            workspace_path="/should/not/be/read",
        )


@pytest.mark.asyncio
async def test_read_tool_archives_a_live_mechanical_reference(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from plugins.life_engine.core.chatter import LifeChatter

    payloads = _conversation()
    record = build_group_manifest(
        payloads,
        exclude_latest_group=False,
    ).groups[0]
    monkeypatch.setattr(
        LifeChatter,
        "_GLOBAL_RUNTIME",
        SimpleNamespace(response=SimpleNamespace(payloads=payloads)),
    )
    tool = LifeReadContextGroupTool(
        SimpleNamespace(
            service=None,
            config=SimpleNamespace(
                settings=SimpleNamespace(workspace_path=str(tmp_path))
            ),
        )
    )

    ok, page = await tool.execute(record.group_ref, max_bytes=512)

    assert ok
    assert page["group_ref"] == record.group_ref
    assert page["delivered_bytes"] <= 512
    archive_path = (
        tmp_path
        / "runtime"
        / "context_archive"
        / f"{record.group_ref}.json"
    )
    assert archive_path.exists()


def test_checkpoint_embeds_into_current_tool_turn_without_breaking_chain() -> None:
    payloads = _conversation()
    command = _checkpoint_command(payloads)
    payloads.extend(
        [
            LLMPayload(
                ROLE.ASSISTANT,
                [
                    ToolCall(
                        id="checkpoint-call",
                        name="action-author_self_continuity_checkpoint",
                        args={"source_manifest_sha256": command.source_manifest_sha256},
                    )
                ],
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                [
                    ToolResult(
                        value="archived",
                        call_id="checkpoint-call",
                        name="action-author_self_continuity_checkpoint",
                    )
                ],
            ),
        ]
    )

    prepared = prepare_subject_checkpoint(payloads, command)

    LLMContextManager().validate_for_send(prepared.payloads)
    assistant_with_call = next(
        payload
        for payload in prepared.payloads
        if payload.role == ROLE.ASSISTANT
        and any(isinstance(part, ToolCall) for part in payload.content)
    )
    assert any(
        isinstance(part, Text) and part.text.startswith(CHECKPOINT_OPEN)
        for part in assistant_with_call.content
    )
    assert any(isinstance(part, ToolCall) for part in assistant_with_call.content)


@pytest.mark.asyncio
async def test_action_archives_before_safe_boundary_install(monkeypatch, tmp_path: Path) -> None:
    from plugins.life_engine.core.chatter import LifeChatter

    payloads = _conversation()
    command = _checkpoint_command(payloads)
    monkeypatch.setattr(
        LifeChatter,
        "_GLOBAL_RUNTIME",
        SimpleNamespace(response=SimpleNamespace(payloads=payloads)),
    )
    plugin = SimpleNamespace(
        service=None,
        config=SimpleNamespace(
            settings=SimpleNamespace(workspace_path=str(tmp_path)),
            chatter=SimpleNamespace(self_continuity_checkpoint_max_bytes=32 * 1024),
        ),
    )
    action = LifeAuthorSelfContinuityCheckpointAction(
        SimpleNamespace(stream_id="stream", platform="test"),
        plugin,
    )
    action._trigger_message = SimpleNamespace(
        extra={
            "life_turn_scope": {
                "consciousness_instance_id": "chat_global",
                "turn_key": "turn-1",
            }
        }
    )
    reset_pending_subject_checkpoint("chat_global")
    try:
        ok, detail = await action.execute(
            thought=command.thought,
            continuity_text=command.continuity_text,
            source_manifest_sha256=command.source_manifest_sha256,
            expected_revision=command.expected_revision,
            release_through_group_ref=command.release_through_group_ref,
            retain_exact_group_refs=[],
        )
        assert ok, detail
        archive_path = (
            tmp_path
            / "runtime"
            / "context_archive"
            / f"{command.release_through_group_ref}.json"
        )
        assert archive_path.exists()
        installed = apply_pending_subject_checkpoint("chat_global", payloads)
        assert installed.triggered
        assert command.continuity_text in str(installed.payloads)
    finally:
        reset_pending_subject_checkpoint("chat_global")


def test_action_schema_requires_subject_fields() -> None:
    schema = LifeAuthorSelfContinuityCheckpointAction.to_schema()["function"]
    assert set(schema["parameters"]["required"]) == {
        "thought",
        "continuity_text",
        "source_manifest_sha256",
        "expected_revision",
        "release_through_group_ref",
        "retain_exact_group_refs",
    }


def test_context_stewardship_tools_are_registered_and_visible_to_chat() -> None:
    config = LifeEngineConfig()
    config.chatter.enabled = True
    components = LifeEnginePlugin(config).get_components()
    component_names = {component.__name__ for component in components}
    chat_manifest = set(get_tool_manifest("chat"))

    assert "LifeAuthorSelfContinuityCheckpointAction" in component_names
    assert "LifeReadContextGroupTool" in component_names
    assert "action-author_self_continuity_checkpoint" in chat_manifest
    assert "tool-read_context_group" in chat_manifest


def test_retired_summary_is_recognized_and_compact_does_not_rewrite() -> None:
    legacy = LLMPayload(
        ROLE.USER,
        [Text(f"{SUMMARY_INTRO}\n{SUMMARY_OPEN}\nlegacy body\n{SUMMARY_CLOSE}")],
    )
    payloads = [legacy, *_conversation()[1:]]
    assert is_summary_payload(legacy)
    result = hierarchical_compact_payloads(
        payloads,
        estimate=_estimate,
        trigger_chars=1,
        target_chars=700,
        force=True,
    )
    assert result.triggered
    assert result.dropped_groups == 0
    assert result.payloads == payloads
    assert "legacy body" in str(result.payloads)


def test_compression_required_list_appends_once_without_group_bodies() -> None:
    payloads = _conversation()
    first = ensure_compression_required_appended(
        payloads,
        estimate=_estimate,
        trigger_chars=8,
        max_groups=8,
        max_bytes=8 * 1024,
    )
    second = ensure_compression_required_appended(
        first,
        estimate=_estimate,
        trigger_chars=8,
        max_groups=8,
        max_bytes=8 * 1024,
    )

    assert has_compression_required_payload(first)
    assert first is not payloads
    assert second == first
    rendered = str(first[-1].content[0].text)
    assert rendered.startswith(COMPRESSION_REQUIRED_OPEN)
    assert "ctxg_" in rendered
    assert "old-user-one" not in rendered
    assert "old-assistant-one" not in rendered
    assert "author_self_continuity_checkpoint" in rendered
    listed = [payload for payload in first if payload is not first[-1]]
    assert listed == payloads


def test_compression_control_does_not_change_checkpoint_manifest() -> None:
    payloads = _conversation()
    command = _checkpoint_command(payloads)
    with_control = ensure_compression_required_appended(
        payloads,
        estimate=_estimate,
        trigger_chars=1,
        max_groups=8,
        max_bytes=8 * 1024,
    )

    assert build_group_manifest(with_control) == build_group_manifest(payloads)
    prepared = prepare_subject_checkpoint(with_control, command)

    assert not has_compression_required_payload(prepared.payloads)
    assert CHECKPOINT_OPEN in str(prepared.payloads)
    assert "current-user" in str(prepared.payloads)


def test_successful_checkpoint_snapshot_must_not_use_mechanical_omission() -> None:
    payloads = _conversation()
    prepared = prepare_subject_checkpoint(payloads, _checkpoint_command(payloads))
    rendered = str(prepared.payloads)
    assert OMISSION_OPEN not in rendered
    assert CHECKPOINT_OPEN in rendered
    assert "我记得我们已经谈过前两件事" in rendered
