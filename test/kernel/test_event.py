"""Event bus 模块的单元测试。

本项目的 Event 协议（硬性约束）：
- 订阅者签名统一为 `(event_name, params)`，其中 `params` 为 `dict[str, Any]`
- 订阅者返回 `(EventDecision, next_params)`
- `next_params` 的 key 集合必须与入参 params 完全一致，否则丢弃该订阅者影响
"""

from __future__ import annotations

import asyncio

import pytest

import src.kernel.event.core as event_core
from src.core.components.types import EventType
from src.kernel.event import EventBus, EventDecision


class TestEventBusBasics:
    def test_event_bus_initialization(self) -> None:
        bus = EventBus(name="test_bus")
        assert bus.name == "test_bus"
        assert bus.event_count == 0
        assert bus.handler_count == 0
        assert len(bus.subscribed_events) == 0

    def test_event_bus_default_name(self) -> None:
        bus = EventBus()
        assert bus.name == "default"

    def test_subscribe_and_unsubscribe(self) -> None:
        bus = EventBus()

        async def handler(event_name: str, params: dict):
            return (EventDecision.SUCCESS, params)

        unsubscribe = bus.subscribe("test_event", handler)
        assert bus.event_count == 1
        assert bus.handler_count == 1
        assert "test_event" in bus.subscribed_events
        assert handler in bus.get_subscribers("test_event")

        unsubscribe()
        assert bus.event_count == 0
        assert bus.handler_count == 0

    def test_unsubscribe_all(self) -> None:
        bus = EventBus()

        async def handler(event_name: str, params: dict):
            return (EventDecision.SUCCESS, params)

        bus.subscribe("a", handler)
        bus.subscribe("b", handler)
        assert bus.handler_count == 2

        removed = bus.unsubscribe_all(handler)
        assert removed == 2
        assert bus.handler_count == 0
        assert bus.event_count == 0

    def test_priority_ordering(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def low(event_name: str, params: dict):
            seen.append("low")
            return (EventDecision.SUCCESS, params)

        async def high(event_name: str, params: dict):
            seen.append("high")
            return (EventDecision.SUCCESS, params)

        bus.subscribe("e", low, priority=0)
        bus.subscribe("e", high, priority=10)

        # get_subscribers 应按 priority 从高到低
        subs = bus.get_subscribers("e")
        assert subs == [high, low]


class TestEventBusPublish:
    async def test_publish_no_subscribers_returns_success_and_copies_params(self) -> None:
        bus = EventBus()
        params = {"x": 1}
        decision, out = await bus.publish("nope", params)
        assert decision == EventDecision.SUCCESS
        assert out == {"x": 1}
        assert out is not params

    async def test_publish_valid_chain_success_pass_stop(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        async def step1(event_name: str, params: dict):
            calls.append("step1")
            params["v"] += 1
            return (EventDecision.SUCCESS, params)

        async def step2(event_name: str, params: dict):
            calls.append("step2")
            params["ignored"] = True
            return (EventDecision.PASS, params)

        async def step3(event_name: str, params: dict):
            calls.append("step3")
            params["v"] += 10
            params["stop"] = True
            return (EventDecision.STOP, params)

        async def step4(event_name: str, params: dict):
            calls.append("step4")
            params["v"] += 1000
            return (EventDecision.SUCCESS, params)

        bus.subscribe("e", step4, priority=0)
        bus.subscribe("e", step3, priority=10)
        bus.subscribe("e", step2, priority=20)
        bus.subscribe("e", step1, priority=30)

        decision, out = await bus.publish("e", {"v": 0, "ignored": False, "stop": False})
        assert calls == ["step1", "step2", "step3"]
        assert decision == EventDecision.STOP
        assert out["v"] == 11
        assert out["ignored"] is False  # PASS 不更新链式 params
        assert out["stop"] is True

    async def test_publish_invalid_return_is_discarded_and_does_not_mutate_chain(self) -> None:
        bus = EventBus()

        def mutating_but_invalid(event_name: str, params: dict):
            params["v"] = 999
            return None  # 非二元组：应丢弃影响

        async def next_handler(event_name: str, params: dict):
            params["v"] += 1
            return (EventDecision.SUCCESS, params)

        bus.subscribe("e", mutating_but_invalid, priority=20)   # type: ignore[misc]
        bus.subscribe("e", next_handler, priority=10)

        decision, out = await bus.publish("e", {"v": 0})
        assert decision == EventDecision.SUCCESS
        assert out == {"v": 1}

    async def test_publish_invalid_next_params_keys_discarded(self) -> None:
        bus = EventBus()

        async def bad_keys(event_name: str, params: dict):
            return (EventDecision.SUCCESS, {"other": 1})

        async def good(event_name: str, params: dict):
            params["x"] += 1
            return (EventDecision.SUCCESS, params)

        bus.subscribe("e", bad_keys, priority=20)
        bus.subscribe("e", good, priority=10)

        decision, out = await bus.publish("e", {"x": 1})
        assert decision == EventDecision.SUCCESS
        assert out == {"x": 2}

    async def test_publish_handler_exception_is_ignored(self) -> None:
        bus = EventBus()

        async def boom(event_name: str, params: dict):
            raise RuntimeError("boom")

        async def ok(event_name: str, params: dict):
            params["x"] = 2
            return (EventDecision.SUCCESS, params)

        bus.subscribe("e", boom, priority=20)
        bus.subscribe("e", ok, priority=10)

        decision, out = await bus.publish("e", {"x": 1})
        assert decision == EventDecision.SUCCESS
        assert out == {"x": 2}

    async def test_publish_handler_timeout_is_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bus = EventBus()

        async def hung(event_name: str, params: dict):
            await asyncio.Event().wait()
            return (EventDecision.SUCCESS, params)

        async def ok(event_name: str, params: dict):
            params["x"] = 2
            return (EventDecision.SUCCESS, params)

        monkeypatch.setattr(event_core, "EVENT_HANDLER_TIMEOUT_SECONDS", 0.01)

        bus.subscribe("e", hung, priority=20)
        bus.subscribe("e", ok, priority=10)

        decision, out = await bus.publish("e", {"x": 1})
        assert decision == EventDecision.SUCCESS
        assert out == {"x": 2}

    async def test_publish_input_validation(self) -> None:
        bus = EventBus()

        with pytest.raises(ValueError, match="事件名称必须是非空字符串"):
            await bus.publish("", {"x": 1})

        with pytest.raises(ValueError, match="params 必须是 dict"):
            await bus.publish("e", None)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="key 必须全部为 str"):
            await bus.publish("e", {1: "x"})  # type: ignore[dict-item]

    async def test_publish_accepts_event_type_str_enum(self) -> None:
        bus = EventBus()

        async def handler(event_name: str, params: dict):
            params["handled"] = event_name
            return (EventDecision.SUCCESS, params)

        bus.subscribe(EventType.ON_ALL_PLUGIN_LOADED, handler)

        decision, out = await bus.publish(
            EventType.ON_ALL_PLUGIN_LOADED,
            {"handled": ""},
        )
        assert decision == EventDecision.SUCCESS
        assert out == {"handled": EventType.ON_ALL_PLUGIN_LOADED.value}

    async def test_publish_sync_returns_task(self) -> None:
        bus = EventBus()

        async def handler(event_name: str, params: dict):
            params["x"] += 1
            return (EventDecision.SUCCESS, params)

        bus.subscribe("e", handler)
        task = bus.publish_sync("e", {"x": 1})
        assert isinstance(task, asyncio.Task)
        decision, out = await task
        assert decision == EventDecision.SUCCESS
        assert out == {"x": 2}

    async def test_concurrent_publish_isolated_params(self) -> None:
        bus = EventBus()
        seen: list[int] = []

        async def handler(event_name: str, params: dict):
            # 每次发布都应独立，不应互相串改
            seen.append(params["i"])
            return (EventDecision.SUCCESS, params)

        bus.subscribe("e", handler)
        tasks = [bus.publish("e", {"i": i}) for i in range(10)]
        results = await asyncio.gather(*tasks)

        assert all(decision == EventDecision.SUCCESS for decision, _ in results)
        assert sorted(seen) == list(range(10))


class TestSyncHandlerDispatch:
    """同步订阅者的执行契约。

    订阅者按优先级串行成链，所以链上任何一个同步 handler 只要在事件循环上
    做文件读或数据库查询，就会把整条链——连同循环上其他所有任务——一起停住。
    而 EVENT_HANDLER_TIMEOUT_SECONDS 对它原本完全不生效：wait_for
    只能作用于 awaitable，一个已经在循环上跑着的同步函数没有可取消点。
    """

    async def test_sync_handler_runs_off_event_loop(self, monkeypatch) -> None:
        import threading

        monkeypatch.setattr(event_core, "EVENT_HANDLER_TIMEOUT_SECONDS", 5.0)

        bus = EventBus()
        release = threading.Event()
        ticks = 0

        def blocking_handler(event_name: str, params: dict):
            # 若这里跑在事件循环上，_tick 永远不会推进，release 永不被 set
            release.wait(5.0)
            params["thread"] = threading.current_thread().name
            return (EventDecision.SUCCESS, params)

        async def _tick() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks += 1
            release.set()

        bus.subscribe("e", blocking_handler)
        ticker = asyncio.ensure_future(_tick())
        try:
            decision, out = await bus.publish("e", {"thread": ""})
            await ticker
        finally:
            release.set()

        assert decision == EventDecision.SUCCESS
        assert ticks == 5
        assert out["thread"].startswith("event-handler")

    async def test_sync_handler_timeout_is_enforced_and_chain_continues(
        self, monkeypatch
    ) -> None:
        """同步 handler 超时必须被跳过，且不阻断后续订阅者。"""
        import threading

        monkeypatch.setattr(event_core, "EVENT_HANDLER_TIMEOUT_SECONDS", 0.05)

        bus = EventBus()
        release = threading.Event()

        def stuck_handler(event_name: str, params: dict):
            release.wait(10.0)
            params["seen"] = params["seen"] + "stuck"
            return (EventDecision.SUCCESS, params)

        async def next_handler(event_name: str, params: dict):
            params["seen"] = params["seen"] + "next"
            return (EventDecision.SUCCESS, params)

        bus.subscribe("e", stuck_handler, priority=10)
        bus.subscribe("e", next_handler, priority=1)

        try:
            decision, out = await bus.publish("e", {"seen": ""})
        finally:
            release.set()

        assert decision == EventDecision.SUCCESS
        # 超时的 handler 影响被丢弃，后一个仍然执行
        assert out == {"seen": "next"}

    async def test_sync_handler_returning_awaitable_is_awaited(self) -> None:
        """返回协程的同步 handler：阻塞部分在线程里，等待部分回到循环。"""
        bus = EventBus()

        async def _finish(params: dict):
            await asyncio.sleep(0)
            params["v"] = params["v"] + 1
            return (EventDecision.SUCCESS, params)

        def factory_handler(event_name: str, params: dict):
            return _finish(params)

        bus.subscribe("e", factory_handler)
        decision, out = await bus.publish("e", {"v": 1})

        assert decision == EventDecision.SUCCESS
        assert out == {"v": 2}
