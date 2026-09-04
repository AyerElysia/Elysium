"""Operator-facing heartbeat activity panels."""

from __future__ import annotations

from types import SimpleNamespace

from plugins.life_engine.service.activity_panel import (
    RECEIPT_BODY_MAX_CHARS,
    close_heartbeat_panel_file,
    format_decision_panel,
    format_skip_panel,
    format_stall_panel,
    format_tool_receipt_panel,
    print_activity_panel,
)
from plugins.life_engine.service.core import LifeEngineService
from src.kernel.llm import ToolCall, ToolResult


def test_decision_panel_keeps_thought_monologue_and_hides_reason() -> None:
    body = format_decision_panel(
        thought="完整推理应当出现。",
        monologue="我想留下这条关注。",
        call_list=[
            ToolCall(
                id="call:1",
                name="nucleus_proactive_command",
                args={
                    "action": "attention.open",
                    "reason": "内部理由",
                    "statement": "留下关注",
                },
            )
        ],
        header_lines=("心跳序号：#12", "本轮：0"),
    )

    assert "心跳序号：#12" in body
    assert "本轮：0" in body
    assert "思考：完整推理应当出现。" in body
    assert "独白：我想留下这条关注。" in body
    assert "nucleus_proactive_command (action: attention.open, statement: 留下关注)" in body
    assert "内部理由" not in body


def test_decision_panel_still_shows_monologue_when_tools_are_present() -> None:
    body = format_decision_panel(
        thought="",
        monologue="先把这条线留下来。",
        call_list=[
            ToolCall(
                id="call:2",
                name="nucleus_proactive_command",
                args={"action": "attention.open"},
            )
        ],
    )

    assert "独白：先把这条线留下来。" in body
    assert "思考：（无）" in body


def test_failed_receipt_surfaces_error_message() -> None:
    body = format_tool_receipt_panel(
        [
            ToolResult(
                name="nucleus_proactive_command",
                value={
                    "error": "RuntimeError",
                    "error_message": "ProactiveSourceTimeRequired",
                    "authority_committed": False,
                    "operation": "attention.open",
                },
            )
        ]
    )

    assert "- nucleus_proactive_command: 失败" in body
    assert "error: RuntimeError" in body
    assert "error_message: ProactiveSourceTimeRequired" in body
    assert "authority_committed: False" in body


def test_stringified_failed_receipt_is_parsed() -> None:
    payload = {
        "error": "RuntimeError",
        "error_message": "ProactiveSourceTimeRequired",
        "authority_committed": False,
    }
    body = format_tool_receipt_panel(
        [
            ToolResult(
                name="nucleus_proactive_command",
                value=f"执行失败: {payload}",
            )
        ]
    )

    assert "失败" in body
    assert "error_message: ProactiveSourceTimeRequired" in body


def test_receipt_body_is_bounded() -> None:
    huge = "x" * (RECEIPT_BODY_MAX_CHARS + 40)
    body = format_tool_receipt_panel(
        [ToolResult(name="nucleus_bash", value=huge)]
    )

    assert "truncated 40 chars" in body
    assert huge not in body


def test_skip_panel_includes_rest_fields() -> None:
    body = format_skip_panel(
        reason="想先歇一会儿",
        remaining="180min",
        until="2026-09-03T06:13:00+08:00",
    )

    assert body == (
        "原因：想先歇一会儿\n"
        "剩余：180min\n"
        "直到：2026-09-03T06:13:00+08:00"
    )


def test_stall_panel_names_tools_without_bodies() -> None:
    body = format_stall_panel(
        heartbeat_count=2710,
        reason="consecutive_tool_stalls",
        stall_kind="protocol_failure+no_progress",
        stage="tool_round",
        model_turns=2,
        tools=["nucleus_proactive_command:fail"],
        consecutive_no_progress=2,
        consecutive_protocol_failures=2,
        consecutive_same_failure=1,
    )

    assert "心跳序号：#2710" in body
    assert "原因：consecutive_tool_stalls" in body
    assert "类型：protocol_failure+no_progress" in body
    assert "末轮工具：nucleus_proactive_command:fail" in body
    assert "计数：无进展=2 协议失败=2 同失败=1" in body
    assert "未知工具" not in body


def test_print_activity_panel_is_silent_without_print_panel() -> None:
    print_activity_panel(
        SimpleNamespace(),
        "body",
        title="Life Engine 潜意识",
        border_style="cyan",
    )


def test_log_heartbeat_false_does_not_print_panels(
    monkeypatch: object,
) -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    service._state = SimpleNamespace(heartbeat_count=3)
    service._cfg = lambda: SimpleNamespace(
        settings=SimpleNamespace(log_heartbeat=False)
    )
    panels: list[tuple[str, str, str]] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "plugins.life_engine.service.core.logger",
        SimpleNamespace(
            print_panel=lambda content, title, border_style: panels.append(
                (content, title, border_style)
            )
        ),
    )

    service._print_heartbeat_decision_panel(
        SimpleNamespace(
            reasoning_content="不该出现",
            message="也不该出现",
            call_list=[],
        ),
        turn_index=0,
    )
    service._print_heartbeat_receipt_panel([])
    service._print_heartbeat_skip_panel(reason="主动休息")

    assert panels == []


def test_heartbeat_decision_panel_uses_cyan_title(
    monkeypatch: object,
) -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    service._state = SimpleNamespace(heartbeat_count=8)
    service._cfg = lambda: SimpleNamespace(
        settings=SimpleNamespace(log_heartbeat=True)
    )
    panels: list[tuple[str, str, str]] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "plugins.life_engine.service.core.logger",
        SimpleNamespace(
            print_panel=lambda content, title, border_style: panels.append(
                (content, title, border_style)
            )
        ),
    )

    service._print_heartbeat_decision_panel(
        SimpleNamespace(
            reasoning_content="完整推理应当出现。",
            message="我想留下这条关注。",
            call_list=[
                ToolCall(
                    id="call:1",
                    name="nucleus_proactive_command",
                    args={
                        "action": "attention.open",
                        "reason": "内部理由",
                    },
                )
            ],
        ),
        turn_index=0,
    )

    assert len(panels) == 1
    content, title, border_style = panels[0]
    assert title == "Life Engine 潜意识"
    assert border_style == "cyan"
    assert "心跳序号：#8" in content
    assert "完整推理应当出现。" in content
    assert "我想留下这条关注。" in content
    assert "内部理由" not in content


def test_heartbeat_file_sink_does_not_print_to_stdout_logger(
    tmp_path, monkeypatch
) -> None:
    close_heartbeat_panel_file()
    target = tmp_path / "heartbeat.console"
    try:
        service = LifeEngineService.__new__(LifeEngineService)
        service._state = SimpleNamespace(heartbeat_count=8)
        service._cfg = lambda: SimpleNamespace(
            settings=SimpleNamespace(
                log_heartbeat=True,
                heartbeat_panel_sink="file",
                heartbeat_panel_path=str(target),
            )
        )
        panels: list[tuple[str, str, str]] = []
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "plugins.life_engine.service.core.logger",
            SimpleNamespace(
                print_panel=lambda content, title, border_style: panels.append(
                    (content, title, border_style)
                )
            ),
        )
        service._print_heartbeat_decision_panel(
            SimpleNamespace(
                reasoning_content="完整推理应当出现。",
                message="我想留下这条关注。",
                call_list=[],
            ),
            turn_index=0,
        )
        assert panels == []
        text = target.read_text(encoding="utf-8")
        assert "Life Engine 潜意识" in text
        assert "完整推理应当出现。" in text
        assert "我想留下这条关注。" in text
    finally:
        close_heartbeat_panel_file()


def test_heartbeat_both_sink_writes_file_and_stdout(
    tmp_path, monkeypatch
) -> None:
    close_heartbeat_panel_file()
    target = tmp_path / "heartbeat.console"
    try:
        service = LifeEngineService.__new__(LifeEngineService)
        service._state = SimpleNamespace(heartbeat_count=9)
        service._cfg = lambda: SimpleNamespace(
            settings=SimpleNamespace(
                log_heartbeat=True,
                heartbeat_panel_sink="both",
                heartbeat_panel_path=str(target),
            )
        )
        panels: list[tuple[str, str, str]] = []
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "plugins.life_engine.service.core.logger",
            SimpleNamespace(
                print_panel=lambda content, title, border_style: panels.append(
                    (content, title, border_style)
                )
            ),
        )
        service._print_heartbeat_skip_panel(reason="主动休息")
        assert len(panels) == 1
        assert panels[0][1] == "Life Engine 本轮跳过"
        assert "主动休息" in target.read_text(encoding="utf-8")
    finally:
        close_heartbeat_panel_file()


def test_heartbeat_panel_file_rotates_when_too_large(
    tmp_path, monkeypatch
) -> None:
    from plugins.life_engine.service import activity_panel as panel_mod

    close_heartbeat_panel_file()
    monkeypatch.setattr(panel_mod, "HEARTBEAT_PANEL_FILE_MAX_BYTES", 64)
    target = tmp_path / "heartbeat.console"
    target.write_text("x" * 80, encoding="utf-8")
    try:
        print_activity_panel(
            SimpleNamespace(),
            "rotated body",
            title="Life Engine 潜意识",
            border_style="cyan",
            sink="file",
            path=str(target),
        )
        backup = target.with_name("heartbeat.console.1")
        assert backup.exists()
        assert "x" * 80 in backup.read_text(encoding="utf-8")
        assert "rotated body" in target.read_text(encoding="utf-8")
    finally:
        close_heartbeat_panel_file()


def test_chatter_decision_panel_title_and_magenta_are_unchanged(
    monkeypatch: object,
) -> None:
    from plugins.life_engine.core.chatter import LifeChatter

    calls = [
        ToolCall(
            id="send-1",
            name="action-life_send_text",
            args={"content": "你好", "reason": "内部理由"},
        )
    ]
    response = SimpleNamespace(
        reasoning_content="先确认对方是不是在问新逻辑。",
        message="我感觉到一点变化。",
        call_list=calls,
    )
    chat_stream = SimpleNamespace(stream_name="始源之地", stream_id="stream-1")
    panels: list[tuple[str, str, str]] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "plugins.life_engine.core.chatter.logger",
        SimpleNamespace(
            print_panel=lambda content, title, border_style: panels.append(
                (content, title, border_style)
            )
        ),
    )

    LifeChatter._print_life_decision_panel(chat_stream, response)

    assert panels
    content, title, border_style = panels[0]
    assert title == "Life Chatter 决策"
    assert border_style == "magenta"
    assert "聊天流名称：始源之地" in content
    assert "思考：先确认对方是不是在问新逻辑。" in content
    assert "独白：我感觉到一点变化。" in content
    assert "action-life_send_text (content: 你好)" in content
    assert "内部理由" not in content
