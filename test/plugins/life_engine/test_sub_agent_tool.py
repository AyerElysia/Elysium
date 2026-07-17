"""life_chatter 子代理委托工具测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.agents import register_builtin_agents
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.plugin import LifeEnginePlugin
from plugins.life_engine.core.sub_agent_tool import LifeRunAgentTool


@pytest.fixture(autouse=True, scope="module")
def _ensure_builtin_agents_registered() -> None:
    """确保内置子代理类型已注册（idempotent）。"""
    try:
        register_builtin_agents()
    except RuntimeError:
        pass


def test_life_run_agent_tool_metadata() -> None:
    """工具元数据应符合 life_chatter 委托工具规范。"""
    assert LifeRunAgentTool.tool_name == "life_run_agent"
    assert LifeRunAgentTool.chatter_allow == ["life_chatter"]


def test_plugin_registers_life_run_agent_only_when_enabled() -> None:
    """enable_sub_agent=false 时不注册；true 时注册。"""
    config = LifeEngineConfig()
    config.chatter.enabled = True
    config.chatter.enable_sub_agent = False
    plugin = LifeEnginePlugin(config=config)
    names = {getattr(c, "__name__", "") for c in plugin.get_components()}
    assert "LifeRunAgentTool" not in names

    config2 = LifeEngineConfig()
    config2.chatter.enabled = True
    config2.chatter.enable_sub_agent = True
    plugin2 = LifeEnginePlugin(config=config2)
    names2 = {getattr(c, "__name__", "") for c in plugin2.get_components()}
    assert "LifeRunAgentTool" in names2


def test_tool_rejects_when_sub_agent_disabled() -> None:
    """enable_sub_agent=false 时工具执行直接拒绝。"""
    config = LifeEngineConfig()
    config.chatter.enable_sub_agent = False
    plugin = SimpleNamespace(config=config)
    tool = LifeRunAgentTool(plugin=plugin)  # type: ignore[arg-type]

    ok, msg = _run_sync(tool.execute(task="测试任务"))

    assert ok is False
    assert "未启用" in str(msg)


def test_tool_rejects_empty_task() -> None:
    """空 task 应被拒绝。"""
    config = LifeEngineConfig()
    config.chatter.enable_sub_agent = True
    plugin = SimpleNamespace(config=config)
    tool = LifeRunAgentTool(plugin=plugin)  # type: ignore[arg-type]

    ok, msg = _run_sync(tool.execute(task=""))

    assert ok is False
    assert "task" in str(msg).lower()


def test_tool_rejects_unknown_agent_type() -> None:
    """未知 agent_type 应被拒绝。"""
    config = LifeEngineConfig()
    config.chatter.enable_sub_agent = True
    plugin = SimpleNamespace(config=config)
    tool = LifeRunAgentTool(plugin=plugin)  # type: ignore[arg-type]

    ok, msg = _run_sync(tool.execute(task="测试", agent_type="not_a_real_type"))

    assert ok is False
    assert "未知子代理类型" in str(msg)


def test_tool_rejects_mcp_when_delegation_disabled() -> None:
    """sub_agent_allow_mcp=false 时拒绝 mcp_servers 参数。"""
    config = LifeEngineConfig()
    config.chatter.enable_sub_agent = True
    config.chatter.sub_agent_allow_mcp = False
    plugin = SimpleNamespace(config=config)
    tool = LifeRunAgentTool(plugin=plugin)  # type: ignore[arg-type]

    ok, msg = _run_sync(
        tool.execute(task="测试", mcp_servers=["some_server"])
    )

    assert ok is False
    assert "MCP 委托未启用" in str(msg)


def test_tool_validates_mcp_server_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp_servers 参数应校验已连接的服务器名。"""
    config = LifeEngineConfig()
    config.chatter.enable_sub_agent = True
    config.chatter.sub_agent_allow_mcp = True
    plugin = SimpleNamespace(config=config)
    tool = LifeRunAgentTool(plugin=plugin)  # type: ignore[arg-type]

    fake_metadata = SimpleNamespace(server_name="connected_server")
    fake_manager = SimpleNamespace(
        get_connected_server_metadata=lambda: [fake_metadata],
    )
    monkeypatch.setattr(
        "src.core.managers.tool_manager.get_mcp_manager",
        lambda: fake_manager,
    )

    ok, msg = _run_sync(
        tool.execute(task="测试", mcp_servers=["connected_server", "ghost"])
    )

    assert ok is False
    payload = msg if isinstance(msg, dict) else {}
    assert payload.get("invalid_mcp_servers") == ["ghost"]


def test_tool_rejects_when_mcp_module_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP manager 抛错时应安全降级为 invalid。"""
    config = LifeEngineConfig()
    config.chatter.enable_sub_agent = True
    config.chatter.sub_agent_allow_mcp = True
    plugin = SimpleNamespace(config=config)
    tool = LifeRunAgentTool(plugin=plugin)  # type: ignore[arg-type]

    def _raise() -> Any:
        raise RuntimeError("mcp not ready")

    monkeypatch.setattr(
        "src.core.managers.tool_manager.get_mcp_manager",
        _raise,
    )

    ok, msg = _run_sync(
        tool.execute(task="测试", mcp_servers=["any"])
    )

    assert ok is False


def test_tool_sync_mode_passes_resolved_config_and_runtime_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步模式应将配置与当前消息上下文传给运行器。"""
    config = LifeEngineConfig()
    config.chatter.enable_sub_agent = True
    config.chatter.sub_agent_default_max_rounds = 12
    config.chatter.sub_agent_task_name = "configured_sub_agent"
    plugin = SimpleNamespace(config=config)
    tool = LifeRunAgentTool(plugin=plugin)  # type: ignore[arg-type]
    trigger_message = SimpleNamespace(stream_id="sync-stream")
    tool._bind_runtime_context(
        stream_id="fallback-stream",
        message=trigger_message,  # type: ignore[arg-type]
    )

    captured: dict[str, Any] = {}

    class _FakeResult:
        success = True
        rounds_used = 3
        tool_use_count = 7
        duration_ms = 1234
        result_text = "我是子代理的结果"

    class _FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs

        async def run(self) -> _FakeResult:
            return _FakeResult()

    from plugins.life_engine.agents.definitions import AgentTypeDefinition

    real_def = AgentTypeDefinition(
        agent_type="explore",
        when_to_use="",
        system_prompt=lambda: "",
        max_rounds=5,
        is_read_only=True,
    )

    monkeypatch.setattr(
        "plugins.life_engine.agents.registry.get_agent_type_registry",
        lambda: SimpleNamespace(get=lambda agent_type: real_def),
    )
    monkeypatch.setattr(
        "plugins.life_engine.agents.runner.AgentRunner",
        _FakeRunner,
    )

    ok, msg = _run_sync(tool.execute(task="做一件复杂事", agent_type="explore"))

    assert ok is True
    assert isinstance(msg, dict)
    assert msg["action"] == "run_agent"
    assert msg["agent_type"] == "explore"
    assert msg["max_rounds"] == 12
    assert msg["model_task_name"] == "configured_sub_agent"
    assert msg["rounds"] == 3
    assert msg["tool_calls"] == 7
    assert msg["result"] == "我是子代理的结果"
    assert captured["kwargs"]["task_prompt"] == "做一件复杂事"
    assert captured["kwargs"]["extra_mcp_server_names"] == []
    assert captured["kwargs"]["stream_id"] == "sync-stream"
    assert captured["kwargs"]["trigger_message"] is trigger_message
    assert captured["kwargs"]["agent_type_def"].max_rounds == 12
    assert captured["kwargs"]["agent_type_def"].model_task_name == "configured_sub_agent"


def test_tool_background_mode_passes_resolved_config_and_runtime_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台模式应将已解析配置与当前消息上下文传给协调器。"""
    config = LifeEngineConfig()
    config.chatter.enable_sub_agent = True
    config.chatter.sub_agent_default_max_rounds = 12
    config.chatter.sub_agent_task_name = "configured_sub_agent"
    plugin = SimpleNamespace(config=config)
    tool = LifeRunAgentTool(plugin=plugin)  # type: ignore[arg-type]
    trigger_message = SimpleNamespace(stream_id="background-stream")
    tool._bind_runtime_context(
        stream_id="fallback-stream",
        message=trigger_message,  # type: ignore[arg-type]
    )

    spawn_calls: list[dict[str, Any]] = []

    class _FakeCoordinator:
        def __init__(self, plugin: Any = None) -> None:
            self.plugin = plugin

        async def spawn(self, **kwargs: Any) -> str:
            spawn_calls.append(kwargs)
            return "agent-xyz"

    from plugins.life_engine.agents.definitions import AgentTypeDefinition

    real_def = AgentTypeDefinition(
        agent_type="general-purpose",
        when_to_use="",
        system_prompt=lambda: "",
        max_rounds=5,
    )

    monkeypatch.setattr(
        "plugins.life_engine.agents.registry.get_agent_type_registry",
        lambda: SimpleNamespace(get=lambda agent_type: real_def),
    )
    monkeypatch.setattr(
        "plugins.life_engine.agents.coordinator.AgentCoordinator",
        _FakeCoordinator,
    )

    ok, msg = _run_sync(
        tool.execute(
            task="后台任务",
            agent_type="general-purpose",
            run_in_background=True,
        )
    )

    assert ok is True
    assert isinstance(msg, dict)
    assert msg["action"] == "run_agent_background"
    assert msg["agent_id"] == "agent-xyz"
    assert msg["agent_type"] == "general-purpose"
    assert msg["max_rounds"] == 12
    assert msg["model_task_name"] == "configured_sub_agent"
    assert msg["status"] == "running"
    assert spawn_calls and spawn_calls[0]["agent_type"] == "general-purpose"
    assert spawn_calls[0]["stream_id"] == "background-stream"
    assert spawn_calls[0]["trigger_message"] is trigger_message
    resolved_def = spawn_calls[0]["agent_type_def"]
    assert resolved_def.max_rounds == 12
    assert resolved_def.model_task_name == "configured_sub_agent"


def test_tool_rejects_mcp_for_read_only_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """只读类型不能通过 MCP 获得未声明副作用能力。"""
    config = LifeEngineConfig()
    config.chatter.enable_sub_agent = True
    plugin = SimpleNamespace(config=config)
    tool = LifeRunAgentTool(plugin=plugin)  # type: ignore[arg-type]

    from plugins.life_engine.agents.definitions import AgentTypeDefinition

    read_only_def = AgentTypeDefinition(
        agent_type="explore",
        when_to_use="",
        system_prompt=lambda: "",
        is_read_only=True,
    )
    monkeypatch.setattr(
        "plugins.life_engine.agents.registry.get_agent_type_registry",
        lambda: SimpleNamespace(get=lambda agent_type: read_only_def),
    )
    monkeypatch.setattr(
        "src.core.managers.tool_manager.get_mcp_manager",
        lambda: pytest.fail("只读子代理不应查询 MCP 工具"),
    )

    ok, message = _run_sync(
        tool.execute(task="只读检查", agent_type="explore", mcp_servers=["server"])
    )

    assert ok is False
    assert "只读子代理类型" in str(message)


def test_runner_does_not_load_mcp_for_read_only_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """绕过工具层直接构造 runner 也不能向只读类型注入 MCP。"""
    from plugins.life_engine.agents.definitions import AgentTypeDefinition
    from plugins.life_engine.agents.runner import AgentRunner
    import plugins.life_engine.agents.runner as runner_module

    class _FakeResponse:
        call_list: list[object] = []

        def __await__(self):  # type: ignore[no-untyped-def]
            async def _result() -> str:
                return "完成"

            return _result().__await__()

        def add_payload(self, payload: Any) -> None:
            pass

    class _FakeRequest:
        def __init__(self) -> None:
            self.response = _FakeResponse()

        def add_payload(self, payload: Any) -> None:
            pass

        async def send(self, *, stream: bool) -> _FakeResponse:
            return self.response

    fake_request = _FakeRequest()
    monkeypatch.setattr(runner_module, "get_model_set_by_task", lambda name: object())
    monkeypatch.setattr(runner_module, "create_llm_request", lambda **kwargs: fake_request)
    monkeypatch.setattr(
        runner_module,
        "get_agent_type_registry",
        lambda: SimpleNamespace(filter_tools_for_agent=lambda *args: []),
    )
    monkeypatch.setattr(
        "src.core.managers.tool_manager.get_mcp_manager",
        lambda: pytest.fail("只读 runner 不应查询 MCP 工具"),
    )

    config = LifeEngineConfig()
    definition = AgentTypeDefinition(
        agent_type="explore",
        when_to_use="",
        system_prompt=lambda: "",
        model_task_name="sub_actor",
        max_rounds=1,
        is_read_only=True,
    )
    result = _run_sync(
        AgentRunner(
            plugin=SimpleNamespace(config=config),
            agent_type_def=definition,
            task_prompt="只读任务",
            extra_mcp_server_names=["side-effect-server"],
        ).run()
    )

    assert result.success is True
    assert result.result_text == "完成"


def test_runner_uses_unified_executor_for_reason_and_chat_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner 应剥离自动 reason，并把当前流与触发消息绑定到 fetch 工具。"""
    from src.core.components.base.tool import BaseTool
    from src.kernel.llm import ToolCall

    from plugins.life_engine.agents.definitions import AgentTypeDefinition
    from plugins.life_engine.agents.runner import AgentRunner
    import plugins.life_engine.agents.runner as runner_module

    trigger_message = SimpleNamespace(stream_id="current-stream")
    chat_stream = SimpleNamespace(stream_id="current-stream")
    seen: dict[str, Any] = {}

    class _FetchContextProbeTool(BaseTool):
        tool_name = "fetch_context_probe"
        tool_description = "读取当前聊天流上下文"

        async def execute(self, query: str) -> tuple[bool, str]:
            seen.update(
                query=query,
                trigger_message=self.trigger_message,
                chat_stream=getattr(self, "chat_stream", None),
                current_stream_id=self.get_current_stream_id(),
            )
            return True, query

    class _FakeResponse:
        def __init__(self) -> None:
            self.phase = 0
            self.payloads: list[Any] = []

        @property
        def call_list(self) -> list[Any]:
            if self.phase == 0:
                return [
                    ToolCall(
                        id="fetch-call",
                        name="tool-fetch_context_probe",
                        args={"query": "当前会话", "reason": "provider 自动注入"},
                    )
                ]
            return []

        def __await__(self):  # type: ignore[no-untyped-def]
            async def _result() -> str:
                return "" if self.phase == 0 else "完成"

            return _result().__await__()

        def add_payload(self, payload: Any) -> None:
            self.payloads.append(payload)

        async def send(self, *, stream: bool) -> _FakeResponse:
            self.phase += 1
            return self

    class _FakeRequest:
        def __init__(self) -> None:
            self.response = _FakeResponse()

        def add_payload(self, payload: Any) -> None:
            pass

        async def send(self, *, stream: bool) -> _FakeResponse:
            return self.response

    fake_request = _FakeRequest()
    monkeypatch.setattr(runner_module, "get_model_set_by_task", lambda name: object())
    monkeypatch.setattr(runner_module, "create_llm_request", lambda **kwargs: fake_request)
    monkeypatch.setattr(
        runner_module,
        "get_agent_type_registry",
        lambda: SimpleNamespace(
            filter_tools_for_agent=lambda *args: [_FetchContextProbeTool]
        ),
    )
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: SimpleNamespace(_streams={"current-stream": chat_stream}),
    )

    config = LifeEngineConfig()
    definition = AgentTypeDefinition(
        agent_type="general-purpose",
        when_to_use="",
        system_prompt=lambda: "",
        model_task_name="sub_actor",
        max_rounds=2,
    )
    result = _run_sync(
        AgentRunner(
            plugin=SimpleNamespace(config=config),
            agent_type_def=definition,
            task_prompt="读取当前上下文",
            stream_id="current-stream",
            trigger_message=trigger_message,  # type: ignore[arg-type]
        ).run()
    )

    assert result.success is True
    assert result.result_text == "完成"
    assert result.tool_use_count == 1
    assert seen == {
        "query": "当前会话",
        "trigger_message": trigger_message,
        "chat_stream": chat_stream,
        "current_stream_id": "current-stream",
    }
    assert fake_request.response.payloads[0].content[0].value == "当前会话"


def test_agent_coordinator_preserves_background_runtime_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """coordinator 构造后台 runner 时应保留当前流和触发消息。"""
    from plugins.life_engine.agents.coordinator import AgentCoordinator
    from plugins.life_engine.agents.definitions import AgentResult, AgentTypeDefinition

    captured: dict[str, Any] = {}

    class _FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self) -> AgentResult:
            return AgentResult(
                agent_type="general-purpose",
                success=True,
                result_text="完成",
            )

    monkeypatch.setattr(
        "plugins.life_engine.agents.coordinator.AgentRunner",
        _FakeRunner,
    )

    async def _exercise() -> None:
        trigger_message = SimpleNamespace(stream_id="background-stream")
        coordinator = AgentCoordinator(SimpleNamespace())
        definition = AgentTypeDefinition(
            agent_type="general-purpose",
            when_to_use="",
            system_prompt=lambda: "",
        )
        agent_id = await coordinator.spawn(
            agent_type="general-purpose",
            task="后台任务",
            context="调用背景",
            extra_mcp_server_names=["read-server"],
            agent_type_def=definition,
            stream_id="background-stream",
            trigger_message=trigger_message,  # type: ignore[arg-type]
        )
        results = await coordinator.collect_results(timeout_seconds=1.0)

        assert results[agent_id].result_text == "完成"
        assert captured["task_prompt"] == "后台任务"
        assert captured["context"] == "调用背景"
        assert captured["extra_mcp_server_names"] == ["read-server"]
        assert captured["stream_id"] == "background-stream"
        assert captured["trigger_message"] is trigger_message

    _run_sync(_exercise())


def test_agent_coordinator_shutdown_cancels_running_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """协调器关闭后应取消任务并拒绝继续创建，供插件卸载/重载复用。"""
    import asyncio

    from plugins.life_engine.agents.coordinator import AgentCoordinator
    from plugins.life_engine.agents.definitions import AgentTypeDefinition

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self) -> Any:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(
        "plugins.life_engine.agents.coordinator.AgentRunner",
        _FakeRunner,
    )

    async def _exercise() -> None:
        coordinator = AgentCoordinator(SimpleNamespace())
        definition = AgentTypeDefinition(
            agent_type="general-purpose",
            when_to_use="",
            system_prompt=lambda: "",
        )
        await coordinator.spawn(
            agent_type="general-purpose",
            task="后台任务",
            agent_type_def=definition,
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await coordinator.shutdown()

        assert cancelled.is_set()
        assert coordinator.is_closed is True
        assert coordinator.get_pending_count() == 0
        with pytest.raises(RuntimeError, match="已停止"):
            await coordinator.spawn(
                agent_type="general-purpose",
                task="新任务",
                agent_type_def=definition,
            )

    _run_sync(_exercise())


def test_plugin_unload_shuts_down_and_releases_agent_coordinator() -> None:
    """插件卸载应停止并移除 coordinator，重载不会复用旧任务。"""
    config = LifeEngineConfig()
    plugin = LifeEnginePlugin(config=config)

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        async def shutdown(self) -> None:
            self.shutdown_calls += 1

    coordinator = _FakeCoordinator()
    plugin._agent_coordinator = coordinator

    _run_sync(plugin._shutdown_agent_coordinator())

    assert coordinator.shutdown_calls == 1
    assert plugin._agent_coordinator is None


# ── helpers ────────────────────────────────────────────────────


def _run_sync(coro: Any) -> Any:
    """同步执行协程并返回结果。"""
    import asyncio

    return asyncio.run(coro)
