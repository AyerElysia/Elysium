from __future__ import annotations

from pathlib import Path
import pytest

from plugins.life_engine.core.trajectory import LifeTrajectory, TrajectoryTurn
from src.kernel.llm import ROLE, LLMPayload, ReasoningText, Text, ToolCall, ToolResult


def test_trajectory_basic_turn_flow() -> None:
    traj = LifeTrajectory(domain="subconscious")
    assert len(traj.turns) == 0

    # Turn 1: Inbound observation
    turn1 = traj.begin_turn("阿耶发来消息：晚上吃什么？", metadata={"source": "qq"})
    assert len(traj.turns) == 1
    assert turn1.user_payload is not None
    assert turn1.user_payload.role == ROLE.USER

    # Model replies and calls a tool
    traj.record_assistant_turn([
        ReasoningText("阿耶问我晚上吃什么，我需要查一下冰箱备忘录。"),
        Text("让我想想看哦~"),
        ToolCall(id="call-1", name="nucleus_read_file", args={"path": "fridge.txt"}),
    ])
    assert turn1.assistant_payload is not None
    assert turn1.assistant_payload.role == ROLE.ASSISTANT

    # Tool returns result
    traj.record_tool_result(ToolResult(value="西红柿、鸡蛋、牛奶", call_id="call-1", name="nucleus_read_file"))
    assert len(turn1.tool_payloads) == 1
    assert turn1.tool_payloads[0].role == ROLE.TOOL

    # Export to payloads
    payloads = traj.to_payloads()
    assert len(payloads) == 3
    assert payloads[0].role == ROLE.USER
    assert payloads[1].role == ROLE.ASSISTANT
    assert payloads[2].role == ROLE.TOOL


def test_trajectory_serialization_roundtrip(tmp_path: Path) -> None:
    traj = LifeTrajectory(domain="chatter", session_id="test-session-1")
    traj.begin_turn("你好爱莉！")
    traj.record_assistant_turn("阿耶好呀~♪")

    save_path = tmp_path / "trajectory.json"
    traj.save(save_path)
    assert save_path.exists()

    loaded = LifeTrajectory.load(save_path)
    assert loaded.domain == "chatter"
    assert loaded.session_id == "test-session-1"
    assert len(loaded.turns) == 1
    p = loaded.to_payloads()
    assert len(p) == 2
    assert p[0].role == ROLE.USER
    assert p[1].role == ROLE.ASSISTANT


def test_trajectory_compaction_pruning_and_folding() -> None:
    traj = LifeTrajectory(domain="subconscious")

    # Generate 6 turns with large tool results
    for i in range(6):
        traj.begin_turn(f"第 {i+1} 拍心跳感知事件")
        large_data = f"巨大日志数据 {i} " * 200 # ~2400 chars
        traj.record_assistant_turn([
            Text(f"处理第 {i+1} 件事"),
            ToolCall(id=f"c-{i}", name="fetch_data", args={"page": i}),
        ])
        traj.record_tool_result(ToolResult(value=large_data, call_id=f"c-{i}", name="fetch_data"))

    before_chars = traj.total_chars()
    assert before_chars > 10_000

    # Compact with tight budget
    res = traj.compact(
        trigger_chars=5_000,
        target_chars=3_000,
        min_recent_turns=2, # keep last 2 turns intact
        tool_result_prune_threshold=300,
    )

    assert res.triggered is True
    assert res.compacted_turns_count == 4 # 6 - 2 = 4 turns folded
    assert res.pruned_tool_results_count > 0

    # Total turns should now be 1 (folded) + 2 (recent) = 3 turns!
    assert len(traj.turns) == 3
    assert traj.turns[0].is_compacted is True
    assert "<compacted_trajectory_history>" in traj.turns[0].user_payload.content[0].text

    # Recent turns are untouched
    assert traj.turns[1].is_compacted is False
    assert traj.turns[2].is_compacted is False
    assert "第 6 拍心跳感知事件" in traj.turns[2].user_payload.content[0].text

    # Valid payload sequence maintained
    payloads = traj.to_payloads()
    assert len(payloads) >= 3
    assert payloads[0].role == ROLE.USER


def test_trajectory_from_payloads_conversion() -> None:
    raw_payloads = [
        LLMPayload(ROLE.USER, [Text("你好")]),
        LLMPayload(ROLE.ASSISTANT, [Text("你好！"), ToolCall(id="1", name="t", args={})]),
        LLMPayload(ROLE.TOOL, [ToolResult(value="ok", call_id="1", name="t")]),
        LLMPayload(ROLE.USER, [Text("再见")]),
        LLMPayload(ROLE.ASSISTANT, [Text("拜拜~")]),
    ]
    traj = LifeTrajectory.from_payloads(raw_payloads, domain="chatter")
    assert len(traj.turns) == 2
    assert traj.turns[0].user_payload.content[0].text == "你好"
    assert len(traj.turns[0].tool_payloads) == 1
    assert traj.turns[1].user_payload.content[0].text == "再见"

    exported = traj.to_payloads()
    assert len(exported) == 5
    assert exported[0].role == ROLE.USER
    assert exported[1].role == ROLE.ASSISTANT
    assert exported[2].role == ROLE.TOOL
    assert exported[3].role == ROLE.USER
    assert exported[4].role == ROLE.ASSISTANT
