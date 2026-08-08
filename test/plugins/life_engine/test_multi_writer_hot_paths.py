"""Hot-path bridge integration tests for the multi-writer generation.

Covers the bridge surface that production hot paths actually call:

- inbound message facts + per-message stream turn claim (cross-instance dedup);
- stream turn commit fencing;
- outbox send intent settlement (sent / unknown / retryable);
- heartbeat operation claim exclusion between two nodes;
- per-node projection progress (strict +1 frontier);
- core transport hook registration/invocation for outbox settlement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.factory import LocalBackendSettings, StorageFactorySettings, open_storage_backend
from plugins.life_engine.storage.hot_path_bridge import MultiWriterHotPathBridge, _short_digest
from plugins.life_engine.storage.instance_identity import InstanceIdentity
from plugins.life_engine.storage.models import BackendGeneration, BackendKind, GenerationStatus
from plugins.life_engine.storage.outbox_adapters import SQLOutboxStore
from plugins.life_engine.storage.outbox_contracts import OutboxStatus
from plugins.life_engine.storage.projection_progress import SQLProjectionProgressStore
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema
from src.core.transport import multi_writer_hooks as hooks


async def _runtime(tmp_path: Path) -> Any:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path, registry_id="test")
    await registry.register_generation(
        BackendGeneration(
            "local-test",
            BackendKind.LOCAL,
            1,
            "a" * 64,
            {"x": "b" * 64},
            {"x": 0},
            "2026-08-07T00:00:00+00:00",
            "2026-08-07T00:00:00+00:00",
            GenerationStatus.VERIFIED,
        )
    )
    token = await registry.activate_generation(
        "local-test",
        expected_epoch=0,
        owner_id="owner",
        lease_seconds=120,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation="local-test",
            authority_provider="file",
            registry_id="test",
            authority_epoch=token.authority_epoch,
            authority_owner_id="owner",
            local=LocalBackendSettings(tmp_path / "db.sqlite3", authority_path),
        ),
        environment={"ELYSIUM_LIFE_STORAGE_FENCING_TOKEN": token.fencing_token},
    )
    await ensure_runtime_state_schema(runtime)
    return runtime


def _identity(instance_id: str) -> InstanceIdentity:
    return InstanceIdentity(
        deployment_id="test-deploy",
        instance_id=instance_id,
        boot_id=f"boot-{instance_id}",
        owner_id="test-deploy",
        protocol_version=1,
        schema_generation="gen-1",
        config_digest="c" * 64,
        workspace_revision="workspace-v1",
    )


class _Message:
    def __init__(
        self,
        message_id: str,
        platform: str = "feishu",
        stream_id: str = "stream-1",
        content: str = "hello",
    ) -> None:
        self.message_id = message_id
        self.platform = platform
        self.stream_id = stream_id
        self.content = content
        self.processed_plain_text = content
        self.extra: dict[str, Any] = {}
        self.sender_id = "user-1"
        self.chat_type = "private"
        self.time = "2026-08-07T00:00:00+00:00"


@pytest.mark.asyncio
async def test_message_fact_and_turn_claimed_once(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        bridge_a = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        bridge_b = MultiWriterHotPathBridge(runtime, _identity("node-b"))
        assert bridge_a.enabled and bridge_b.enabled
        message = _Message("msg-1")
        assert await bridge_a.record_inbound_message(message) is True
        turn = message.extra.get("multi_writer_turn")
        assert turn is not None
        assert turn["message_id"] == "msg-1"
        assert turn["claim_epoch"] >= 1
        # 同一事件到达第二个实例：fact 幂等，但 turn 已被 A 认领。
        assert await bridge_b.record_inbound_message(_Message("msg-1")) is False
        # A 自己可提交（claim 未过期且 owner 匹配）。
        assert await bridge_a.commit_stream_turn(
            turn_id=turn["turn_id"],
            claim_epoch=turn["claim_epoch"],
            message_id="msg-1",
        ) is True
        # 已完成 turn 不能被再次 claim。
        assert await bridge_a.open_stream_turn(
            stream_id="stream-1",
            message_id="msg-1",
        ) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_stream_turn_commit_fences_other_owner(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        bridge_a = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        bridge_b = MultiWriterHotPathBridge(runtime, _identity("node-b"))
        message = _Message("msg-2")
        assert await bridge_a.record_inbound_message(message) is True
        turn = message.extra["multi_writer_turn"]
        # B 从未认领该 turn：用 A 的 epoch 冒充提交必须被 fencing。
        assert await bridge_b.commit_stream_turn(
            turn_id=turn["turn_id"],
            claim_epoch=turn["claim_epoch"],
            message_id="msg-2",
        ) is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_outbox_settle_success_marks_sent(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        bridge = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        store = SQLOutboxStore(runtime)
        message = _Message("msg-3")
        assert await bridge.enqueue_outbox_action(message) is True
        action_id = f"outbox-{_short_digest('send', 'feishu', 'msg-3', length=40)}"
        assert (await store._get(action_id)).status == OutboxStatus.PENDING
        assert await bridge.settle_outbox_action(
            message,
            {"provider_receipt": {"status": "ok", "message_id": "feishu-123"}},
        ) is True
        settled = await store._get(action_id)
        assert settled.status == OutboxStatus.SENT
        assert settled.provider_receipt_id
        assert settled.claim_owner == bridge.claim_owner
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_outbox_settle_unknown_is_terminal(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        bridge = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        store = SQLOutboxStore(runtime)
        message = _Message("msg-4")
        await bridge.enqueue_outbox_action(message)
        action_id = f"outbox-{_short_digest('send', 'feishu', 'msg-4', length=40)}"
        assert await bridge.settle_outbox_action(
            message,
            {"delivery_unknown": True, "error_type": "TimeoutError"},
        ) is True
        assert (await store._get(action_id)).status == OutboxStatus.UNKNOWN
        # unknown 禁止盲目重发：任何节点都不能再 claim。
        other = MultiWriterHotPathBridge(runtime, _identity("node-b"))
        assert await other.settle_outbox_action(
            message,
            {"provider_receipt": {"status": "ok"}},
        ) is True
        assert (await store._get(action_id)).status == OutboxStatus.UNKNOWN
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_outbox_settle_retryable_can_be_reclaimed(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        bridge_a = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        store = SQLOutboxStore(runtime)
        message = _Message("msg-5")
        await bridge_a.enqueue_outbox_action(message)
        action_id = f"outbox-{_short_digest('send', 'feishu', 'msg-5', length=40)}"
        assert await bridge_a.settle_outbox_action(
            message,
            {"error_type": "NetworkError"},
        ) is True
        assert (await store._get(action_id)).status == OutboxStatus.RETRYABLE
        # retryable 允许另一个节点安全认领并重试。
        bridge_b = MultiWriterHotPathBridge(runtime, _identity("node-b"))
        claimed = await store.claim_action(
            action_id,
            owner_id=bridge_b.claim_owner,
            lease_seconds=30,
        )
        assert claimed is not None
        assert claimed.status == OutboxStatus.CLAIMED
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_outbox_settle_without_intent_is_noop(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        bridge = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        assert await bridge.settle_outbox_action(
            _Message("never-enqueued"),
            {"provider_receipt": {"status": "ok"}},
        ) is True
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_heartbeat_bridge_claim_exclusion(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        bridge_a = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        bridge_b = MultiWriterHotPathBridge(runtime, _identity("node-b"))
        registered = await bridge_a.register_heartbeat_operation(
            consciousness_instance_id="chat_global",
            sequence=1,
            input_frontier={"cursor": 0, "node": bridge_a.node_id},
        )
        assert registered is not None
        claim_a = await bridge_a.claim_heartbeat_operation(
            consciousness_instance_id="chat_global",
            sequence=1,
            lease_seconds=30,
        )
        assert claim_a is not None
        # 同一 sequence 只能有一个 owner。
        assert await bridge_b.claim_heartbeat_operation(
            consciousness_instance_id="chat_global",
            sequence=1,
            lease_seconds=30,
        ) is None
        committed = await bridge_a.commit_heartbeat_operation(
            consciousness_instance_id="chat_global",
            sequence=1,
            claim_epoch=claim_a.claim_epoch,
            input_frontier=0,
            committed_frontier=1,
            result_ref="heartbeat://run-1",
            result_digest="d" * 64,
        )
        assert committed is not None
        # 已完成 operation 不可再 claim。
        assert await bridge_b.claim_heartbeat_operation(
            consciousness_instance_id="chat_global",
            sequence=1,
            lease_seconds=30,
        ) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_projection_advance_continuous_and_conflict(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        bridge = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        store = SQLProjectionProgressStore(runtime)
        first = await bridge.advance_projection(
            projection_name="memory_index",
            expected_frontier=0,
            next_frontier=1,
            source_digest="a" * 64,
            config_digest="cfg-1",
            backlog=2,
        )
        assert first is not None and first.source_frontier == 1
        assert (await store.get("memory_index", bridge.node_id)).backlog == 2
        second = await bridge.advance_projection(
            projection_name="memory_index",
            expected_frontier=1,
            next_frontier=2,
            source_digest="b" * 64,
            config_digest="cfg-1",
        )
        assert second is not None and second.source_frontier == 2
        # frontier 冲突：bridge 捕获并返回 None（fail closed，不覆盖进度）。
        assert await bridge.advance_projection(
            projection_name="memory_index",
            expected_frontier=1,
            next_frontier=2,
            source_digest="c" * 64,
            config_digest="cfg-1",
        ) is None
        # 配置变化：拒绝。
        assert await bridge.advance_projection(
            projection_name="memory_index",
            expected_frontier=2,
            next_frontier=3,
            source_digest="d" * 64,
            config_digest="cfg-2",
        ) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_message_fact_float_timestamp_normalized(tmp_path: Path) -> None:
    """生产环境 Message.time 是 float 时间戳，必须规范化为 ISO 后落库。

    Core ``Message.time`` 在 message.py 被强制转成 float（epoch 秒）。
    若直接 ``str(time)`` 写入 MySQL ``datetime(6)`` 列会在 strict 模式
    下报 Error 1292，bridge 吞异常返回 False，消息被静默跳过表达层。
    """
    runtime = await _runtime(tmp_path)
    try:
        bridge = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        assert bridge.enabled
        # float epoch（生产形态）
        msg_float = _Message("msg-float-ts")
        msg_float.time = 1754667546.123
        assert await bridge.record_inbound_message(msg_float) is True
        turn = msg_float.extra.get("multi_writer_turn")
        assert turn is not None
        assert turn["message_id"] == "msg-float-ts"
        assert turn["claim_epoch"] >= 1
        # 落库的时间必须是 MySQL 可接受的 ISO 格式，而非 "1754667546.123"
        from sqlalchemy import text

        async with runtime.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT occurred_at, received_at FROM inbound_messages "
                        "WHERE message_id = 'msg-float-ts'"
                    )
                )
            ).mappings().first()
        assert row is not None
        occurred = str(row["occurred_at"])
        assert "1754667546" not in occurred
        # 1754667546.123 == 2025-08-08T15:39:06+00:00 UTC（由规范化 helper 转换）
        assert occurred == "2025-08-08T15:39:06+00:00"
        # 规范化后的 ISO 文本可被读取端 fromisoformat 解析
        from datetime import datetime

        assert datetime.fromisoformat(occurred.replace("Z", "+00:00")) is not None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_message_fact_numeric_string_and_empty_time_ok(tmp_path: Path) -> None:
    """数字字符串与空 time 都不会让消息落库失败。"""
    runtime = await _runtime(tmp_path)
    try:
        bridge = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        assert bridge.enabled
        msg_num = _Message("msg-num-ts")
        msg_num.time = "1754667546"  # 数字字符串也要被规范化
        assert await bridge.record_inbound_message(msg_num) is True
        assert msg_num.extra.get("multi_writer_turn") is not None

        msg_empty = _Message("msg-empty-ts")
        msg_empty.time = ""  # 空时间回退当前时间，不能炸
        assert await bridge.record_inbound_message(msg_empty) is True
        assert msg_empty.extra.get("multi_writer_turn") is not None
    finally:
        await runtime.close()


def test_message_reader_normalizes_mysql_datetime_round_trip() -> None:
    """MySQL DATETIME 列读回是 datetime 对象，reader 必须规范化为 UTC ISO 文本。

    回归保护：record_message 的不可变相等校验在 MySQL 上曾把
    ``str(datetime(2026,8,8,12,8,39))``（"2026-08-08 12:08:39"）与 fact 写入的
    UTC ISO 文本（"2026-08-08T12:08:39+00:00"）比较，永不相等，导致每条消息
    误报 MessageConflict 并被静默跳过（stream_turns 0 行、表达层不唤醒）。
    SQLite 把时间当 TEXT 存取，round-trip 后字符串一致，无法暴露该回归。
    """
    from datetime import datetime, timezone

    from plugins.life_engine.storage.message_stream_adapters import SQLMessageStreamStore

    row = {
        "message_id": "m1",
        "platform": "feishu",
        "platform_event_id": "m1",
        "occurrence_id": "occ-1",
        "payload_sha256": "d" * 64,
        "stream_id": "s1",
        "reply_target": "u1",
        "source": "feishu",
        # MySQL datetime(6) 读回形态：无 tzinfo 的 datetime 对象
        "occurred_at": datetime(2026, 8, 8, 12, 8, 39),
        "received_at": datetime(2026, 8, 8, 12, 8, 34),
        "raw_payload_ref": "runtime://message/feishu/m1",
    }
    msg = SQLMessageStreamStore._message(row)
    assert msg.occurred_at == "2026-08-08T12:08:39+00:00"
    assert msg.received_at == "2026-08-08T12:08:34+00:00"
    assert msg.raw_payload_ref == "runtime://message/feishu/m1"
    assert msg.platform == "feishu"
    assert msg.payload_sha256 == "d" * 64

    # 带 tzinfo 的形态（其他读回路径也可能给出 aware datetime）
    row["occurred_at"] = datetime(2026, 8, 8, 12, 8, 39, tzinfo=timezone.utc)
    msg = SQLMessageStreamStore._message(row)
    assert msg.occurred_at == "2026-08-08T12:08:39+00:00"


@pytest.mark.asyncio
async def test_record_message_equality_survives_datetime_reader(tmp_path: Path) -> None:
    """record_message 的相等校验在 reader 规范化后仍通过。

    通过真实 runtime 落库两次同 identity 消息：第二次走幂等读回路径，
    校验事实与读回结果一致（reader 已把时间规范化为 UTC ISO 文本）。
    """
    from sqlalchemy import text

    from plugins.life_engine.storage.message_stream_adapters import SQLMessageStreamStore
    from plugins.life_engine.storage.message_stream_contracts import InboundMessage

    runtime = await _runtime(tmp_path)
    try:
        store = SQLMessageStreamStore(runtime)
        fact = InboundMessage(
            message_id="m-roundtrip",
            platform="feishu",
            platform_event_id="m-roundtrip",
            occurrence_id="occ-roundtrip",
            payload_sha256="e" * 64,
            stream_id="s1",
            reply_target="u1",
            source="feishu",
            occurred_at="2026-08-08T12:08:39+00:00",
            received_at="2026-08-08T12:08:34+00:00",
            raw_payload_ref="runtime://message/feishu/m-roundtrip",
        )
        first = await store.record_message(fact)
        assert first == fact
        # 同一 identity 再次到达：幂等读回必须相等，不能误报冲突。
        replay = await store.record_message(fact)
        assert replay == fact
        # reader 对 MySQL 形态（datetime 对象）也必须解析出同样的 InboundMessage。
        async with runtime.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM inbound_messages WHERE message_id='m-roundtrip'")
                )
            ).mappings().first()
        assert row is not None
        # SQLite 存 TEXT；这里验证的是 reader 对 "datetime 形态值" 的处理：
        # 手工替换为 datetime 后 reader 仍应给出相同的 UTC ISO 文本。
        from datetime import datetime, timezone

        row = dict(row)
        row["occurred_at"] = datetime(2026, 8, 8, 12, 8, 39, tzinfo=timezone.utc)
        row["received_at"] = datetime(2026, 8, 8, 12, 8, 34, tzinfo=timezone.utc)
        parsed = SQLMessageStreamStore._message(row)
        assert parsed == fact
        # reader 与 fact 的 UTC ISO 文本形态一致（MySQL 规范化后可比对）
        assert parsed.occurred_at.endswith("+00:00")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_outbox_settle_hook_registration_and_invoke(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    # 全局 hook 槽可能被其他 service 级测试注册后未清理；备份并在结束时恢复。
    saved_inbound = hooks._inbound_fact_hook
    saved_intent = hooks._outbox_intent_hook
    saved_settle = hooks._outbox_settle_hook
    hooks._inbound_fact_hook = None
    hooks._outbox_intent_hook = None
    hooks._outbox_settle_hook = None
    try:
        bridge = MultiWriterHotPathBridge(runtime, _identity("node-a"))
        settle_hook = bridge.settle_outbox_action
        assert await hooks.invoke_outbox_settle_hook(_Message("x")) is None
        hooks.register_outbox_settle_hook(settle_hook)
        assert hooks.multi_writer_hooks_active() is True
        message = _Message("msg-6")
        await bridge.enqueue_outbox_action(message)
        # 成功收尾：hook 返回 True，outbox 状态为 sent。
        assert await hooks.invoke_outbox_settle_hook(
            message,
            provider_receipt={"status": "ok"},
        ) is True
        store = SQLOutboxStore(runtime)
        action_id = f"outbox-{_short_digest('send', 'feishu', 'msg-6', length=40)}"
        assert (await store._get(action_id)).status == OutboxStatus.SENT
        hooks.unregister_outbox_settle_hook(settle_hook)
        assert await hooks.invoke_outbox_settle_hook(_Message("y")) is None
        assert hooks.multi_writer_hooks_active() is False
    finally:
        hooks._inbound_fact_hook = saved_inbound
        hooks._outbox_intent_hook = saved_intent
        hooks._outbox_settle_hook = saved_settle
        await runtime.close()
