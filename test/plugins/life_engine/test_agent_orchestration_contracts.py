"""Mission 编排与意识工具清单的架构契约测试。

这些测试锁住当前系统最重要的边界：任务图必须保持 DAG，失败和取消必须
完整传播并出现在 Mission 结果中，Worker 只能收到已成功的上游输出；意识实例
必须显式声明能力，memory_witness 不能获得行动工具。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any

import pytest

from plugins.life_engine.agents import scheduler as scheduler_module
from plugins.life_engine.agents.contracts import (
    FailurePolicy,
    Mission,
    MissionBudget,
    MissionStatus,
    TaskContract,
    TaskKind,
    TaskResult,
    TaskStatus,
)
from plugins.life_engine.agents.scheduler import Scheduler
from plugins.life_engine.agents.task_graph import CycleDetectedError, TaskGraph
from plugins.life_engine.service.tool_manifests import (
    CONSCIOUSNESS_TOOL_MANIFESTS,
    get_tool_manifest,
)


def _task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    priority: int = 5,
    retry_max: int = 1,
) -> TaskContract:
    return TaskContract(
        task_id=task_id,
        mission_id="mission_contract",
        kind=TaskKind.CUSTOM,
        brief=f"执行 {task_id}",
        depends_on=depends_on,
        priority=priority,
        retry_max=retry_max,
    )


def _mission(
    tasks: list[TaskContract],
    *,
    failure_policy: FailurePolicy = FailurePolicy.CONTINUE_OTHERS,
    max_duration_seconds: int = 30,
) -> tuple[Mission, TaskGraph]:
    graph = TaskGraph()
    graph.add_tasks(tasks)
    mission = Mission(
        mission_id="mission_contract",
        goal="验证编排契约",
        tasks={task.task_id: task for task in tasks},
        budget=MissionBudget(max_duration_seconds=max_duration_seconds),
        failure_policy=failure_policy,
    )
    return mission, graph


class _ScriptedWorker:
    outcomes: dict[str, deque[TaskResult]] = defaultdict(deque)
    calls: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.task = kwargs["task"]
        self.upstream_outputs = kwargs.get("upstream_outputs", {})
        self.calls.append(
            {
                "task_id": self.task.task_id,
                "upstream_outputs": dict(self.upstream_outputs),
            }
        )

    async def run(self) -> TaskResult:
        return self.outcomes[self.task.task_id].popleft()

    @classmethod
    def reset(cls) -> None:
        cls.outcomes = defaultdict(deque)
        cls.calls = []


@pytest.fixture
def scripted_worker(monkeypatch: pytest.MonkeyPatch) -> type[_ScriptedWorker]:
    _ScriptedWorker.reset()
    monkeypatch.setattr(scheduler_module, "Worker", _ScriptedWorker)
    return _ScriptedWorker


def _result(
    task_id: str,
    status: TaskStatus,
    *,
    output: dict[str, Any] | str = "",
    error: str | None = None,
    tokens_used: int = 0,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        status=status,
        output=output,
        error=error,
        tokens_used=tokens_used,
    )


class TestTaskGraphContracts:
    def test_batch_add_sorts_dependencies_and_ready_tasks_by_priority(self) -> None:
        root_slow = _task("root_slow", priority=8)
        root_fast = _task("root_fast", priority=1)
        leaf = _task("leaf", depends_on=("root_slow", "root_fast"))
        graph = TaskGraph()

        graph.add_tasks([leaf, root_slow, root_fast])

        order = graph.topological_order()
        assert order.index("root_slow") < order.index("leaf")
        assert order.index("root_fast") < order.index("leaf")
        assert [task.task_id for task in graph.get_ready_tasks()] == [
            "root_fast",
            "root_slow",
        ]

        graph.set_status("root_fast", TaskStatus.SUCCEEDED)
        assert [task.task_id for task in graph.get_ready_tasks()] == ["root_slow"]
        graph.set_status("root_slow", TaskStatus.SUCCEEDED)
        assert [task.task_id for task in graph.get_ready_tasks()] == ["leaf"]

    def test_missing_dependency_is_rejected_without_partial_insert(self) -> None:
        graph = TaskGraph()

        with pytest.raises(ValueError, match="依赖不存在"):
            graph.add_task(_task("orphan", depends_on=("missing",)))

        assert graph.task_ids == []

    def test_cycle_in_batch_is_rejected_without_partial_insert(self) -> None:
        graph = TaskGraph()
        first = _task("first", depends_on=("second",))
        second = _task("second", depends_on=("first",))

        with pytest.raises(CycleDetectedError, match="环依赖"):
            graph.add_tasks([first, second])

        assert graph.task_ids == []

    def test_cascade_skip_marks_all_downstream_but_not_independent_branch(self) -> None:
        root = _task("root")
        child = _task("child", depends_on=("root",))
        grandchild = _task("grandchild", depends_on=("child",))
        independent = _task("independent")
        graph = TaskGraph()
        graph.add_tasks([grandchild, independent, child, root])

        skipped = set(graph.cascade_skip("root"))

        assert skipped == {"child", "grandchild"}
        assert graph.get_status("child") == TaskStatus.SKIPPED
        assert graph.get_status("grandchild") == TaskStatus.SKIPPED
        assert graph.get_status("independent") == TaskStatus.PENDING

    def test_cascade_cancel_preserves_existing_terminal_states(self) -> None:
        tasks = [_task("done"), _task("failed"), _task("running"), _task("pending")]
        graph = TaskGraph()
        graph.add_tasks(tasks)
        graph.set_status("done", TaskStatus.SUCCEEDED)
        graph.set_status("failed", TaskStatus.FAILED)
        graph.set_status("running", TaskStatus.RUNNING)

        cancelled = set(graph.cascade_cancel())

        assert cancelled == {"running", "pending"}
        assert graph.get_status("done") == TaskStatus.SUCCEEDED
        assert graph.get_status("failed") == TaskStatus.FAILED
        assert graph.all_terminal() is True


class TestSchedulerContracts:
    async def test_dependency_output_reaches_downstream_worker(
        self,
        scripted_worker: type[_ScriptedWorker],
    ) -> None:
        research = _task("research")
        synthesize = _task("synthesize", depends_on=("research",))
        mission, graph = _mission([synthesize, research])
        scripted_worker.outcomes["research"].append(
            _result("research", TaskStatus.SUCCEEDED, output={"fact": "evidence"})
        )
        scripted_worker.outcomes["synthesize"].append(
            _result("synthesize", TaskStatus.SUCCEEDED, output="完成")
        )

        result = await Scheduler(
            plugin=object(),
            mission=mission,
            graph=graph,
            retry_max_attempts=1,
        ).run()

        assert result.status == MissionStatus.SUCCEEDED
        assert result.progress == (2, 2)
        downstream_call = next(
            call for call in scripted_worker.calls if call["task_id"] == "synthesize"
        )
        assert downstream_call["upstream_outputs"] == {
            "research": {"fact": "evidence"}
        }

    async def test_continue_others_records_skips_and_finishes_partial(
        self,
        scripted_worker: type[_ScriptedWorker],
    ) -> None:
        failed = _task("failed")
        blocked = _task("blocked", depends_on=("failed",))
        independent = _task("independent")
        mission, graph = _mission([blocked, independent, failed])
        scripted_worker.outcomes["failed"].append(
            _result("failed", TaskStatus.FAILED, error="boom")
        )
        scripted_worker.outcomes["independent"].append(
            _result("independent", TaskStatus.SUCCEEDED, output="ok")
        )

        result = await Scheduler(
            plugin=object(),
            mission=mission,
            graph=graph,
            retry_max_attempts=1,
            failure_policy=FailurePolicy.CONTINUE_OTHERS,
        ).run()

        assert result.status == MissionStatus.PARTIAL
        assert result.results["blocked"].status == TaskStatus.SKIPPED
        assert result.progress == (3, 3)
        assert graph.get_status("blocked") == TaskStatus.SKIPPED

    async def test_fail_fast_reports_failed_and_records_cancelled_siblings(
        self,
        scripted_worker: type[_ScriptedWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failed = _task("failed", priority=0)
        sibling = _task("sibling", priority=1)
        mission, graph = _mission(
            [failed, sibling],
            failure_policy=FailurePolicy.FAIL_FAST,
        )
        scripted_worker.outcomes["failed"].append(
            _result("failed", TaskStatus.FAILED, error="fatal")
        )

        class _WaitingWorker(_ScriptedWorker):
            async def run(self) -> TaskResult:
                if self.task.task_id == "failed":
                    return _result("failed", TaskStatus.FAILED, error="fatal")
                await asyncio.Event().wait()
                raise AssertionError("cancelled worker should not finish normally")

        monkeypatch.setattr(scheduler_module, "Worker", _WaitingWorker)
        result = await Scheduler(
            plugin=object(),
            mission=mission,
            graph=graph,
            max_concurrency=2,
            retry_max_attempts=1,
            failure_policy=FailurePolicy.FAIL_FAST,
        ).run()

        assert result.status == MissionStatus.FAILED
        assert result.results["failed"].status == TaskStatus.FAILED
        assert result.results["sibling"].status == TaskStatus.CANCELLED
        assert result.progress == (2, 2)

    async def test_retry_then_skip_uses_final_attempt_count(
        self,
        scripted_worker: type[_ScriptedWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _no_wait(_: float) -> None:
            return None

        monkeypatch.setattr(scheduler_module.asyncio, "sleep", _no_wait)
        flaky = _task("flaky", retry_max=2)
        blocked = _task("blocked", depends_on=("flaky",))
        mission, graph = _mission(
            [blocked, flaky],
            failure_policy=FailurePolicy.RETRY_THEN_SKIP,
        )
        scripted_worker.outcomes["flaky"].extend(
            [
                _result("flaky", TaskStatus.FAILED, error="first"),
                _result("flaky", TaskStatus.FAILED, error="second"),
            ]
        )

        result = await Scheduler(
            plugin=object(),
            mission=mission,
            graph=graph,
            retry_max_attempts=1,
            failure_policy=FailurePolicy.RETRY_THEN_SKIP,
        ).run()

        assert result.status == MissionStatus.FAILED
        assert result.results["flaky"].attempts == 2
        assert result.results["blocked"].status == TaskStatus.SKIPPED
        assert result.progress == (2, 2)

    async def test_unexpected_worker_exception_uses_failure_propagation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ExplodingWorker:
            def __init__(self, **kwargs: Any) -> None:
                self.task = kwargs["task"]

            async def run(self) -> TaskResult:
                raise RuntimeError("worker escaped its normal error boundary")

        monkeypatch.setattr(scheduler_module, "Worker", _ExplodingWorker)
        failed = _task("failed")
        blocked = _task("blocked", depends_on=("failed",))
        mission, graph = _mission([blocked, failed])

        result = await Scheduler(
            plugin=object(),
            mission=mission,
            graph=graph,
            retry_max_attempts=1,
            failure_policy=FailurePolicy.CONTINUE_OTHERS,
        ).run()

        assert result.status == MissionStatus.FAILED
        assert result.results["failed"].status == TaskStatus.FAILED
        assert "normal error boundary" in str(result.results["failed"].error)
        assert result.results["blocked"].status == TaskStatus.SKIPPED
        assert result.progress == (2, 2)

    async def test_task_timeout_remains_timeout_after_retries_exhausted(
        self,
        scripted_worker: type[_ScriptedWorker],
    ) -> None:
        timed = _task("timed")
        mission, graph = _mission([timed])
        scripted_worker.outcomes["timed"].append(
            _result("timed", TaskStatus.TIMEOUT, error="task timeout")
        )

        result = await Scheduler(
            plugin=object(),
            mission=mission,
            graph=graph,
            retry_max_attempts=1,
        ).run()

        assert result.results["timed"].status == TaskStatus.TIMEOUT
        assert graph.get_status("timed") == TaskStatus.TIMEOUT
        assert result.status == MissionStatus.FAILED

    async def test_explicit_cancel_stops_worker_and_records_terminal_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class _BlockingWorker:
            def __init__(self, **kwargs: Any) -> None:
                self.task = kwargs["task"]

            async def run(self) -> TaskResult:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                raise AssertionError("unreachable")

        monkeypatch.setattr(scheduler_module, "Worker", _BlockingWorker)
        task = _task("long_running")
        mission, graph = _mission([task])
        scheduler = Scheduler(
            plugin=object(),
            mission=mission,
            graph=graph,
            retry_max_attempts=1,
        )
        run_task = asyncio.create_task(scheduler.run())
        await started.wait()

        scheduler.cancel()
        result = await run_task

        assert cancelled.is_set()
        assert result.status == MissionStatus.CANCELLED
        assert result.results["long_running"].status == TaskStatus.CANCELLED
        assert result.progress == (1, 1)

    async def test_token_budget_cancels_live_sibling_without_leak(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        slow_started = asyncio.Event()
        slow_cancelled = asyncio.Event()

        class _BudgetWorker:
            def __init__(self, **kwargs: Any) -> None:
                self.task = kwargs["task"]

            async def run(self) -> TaskResult:
                if self.task.task_id == "costly":
                    return _result(
                        "costly",
                        TaskStatus.SUCCEEDED,
                        output="done",
                        tokens_used=10,
                    )
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    slow_cancelled.set()
                    raise
                raise AssertionError("unreachable")

        monkeypatch.setattr(scheduler_module, "Worker", _BudgetWorker)
        costly = _task("costly", priority=0)
        slow = _task("slow", priority=1)
        mission, graph = _mission([costly, slow])
        mission.budget = MissionBudget(
            max_tokens_total=10,
            max_duration_seconds=30,
            max_tasks=2,
            max_concurrency=2,
        )

        result = await Scheduler(
            plugin=object(),
            mission=mission,
            graph=graph,
            max_concurrency=2,
            retry_max_attempts=1,
        ).run()

        await slow_started.wait()
        assert slow_cancelled.is_set()
        assert result.status == MissionStatus.PARTIAL
        assert result.results["costly"].status == TaskStatus.SUCCEEDED
        assert result.results["slow"].status == TaskStatus.CANCELLED
        assert result.progress == (2, 2)

    async def test_mission_timeout_cancels_live_worker_without_leak(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class _TimeoutWorker:
            def __init__(self, **kwargs: Any) -> None:
                self.task = kwargs["task"]

            async def run(self) -> TaskResult:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                raise AssertionError("unreachable")

        monkeypatch.setattr(scheduler_module, "Worker", _TimeoutWorker)
        task = _task("never_finishes")
        mission, graph = _mission([task], max_duration_seconds=0.01)  # type: ignore[arg-type]

        result = await Scheduler(
            plugin=object(),
            mission=mission,
            graph=graph,
            retry_max_attempts=1,
        ).run()

        await started.wait()
        await asyncio.sleep(0)
        assert cancelled.is_set()
        assert result.status == MissionStatus.TIMEOUT
        assert result.results["never_finishes"].status == TaskStatus.CANCELLED
        assert result.progress == (1, 1)


class TestConsciousnessManifestContracts:
    def test_memory_witness_has_no_injected_tools(self) -> None:
        assert CONSCIOUSNESS_TOOL_MANIFESTS["memory_witness"] == []

    def test_chat_manifest_contains_deep_memory_and_platform_capabilities(self) -> None:
        manifest = set(get_tool_manifest("chat"))
        assert {
            "tool-nucleus_grep_events",
            "tool-nucleus_search_memory",
            "tool-nucleus_view_relations",
            "tool-nucleus_memory_stats",
            "action-life_send_image",
            "action-life_send_voice",
            "tool-recognize_voice",
            "tool-nucleus_save_media",
            "tool-platform_action",
        } <= manifest

    def test_chat_manifest_sends_directly_without_legacy_think_action(self) -> None:
        manifest = set(get_tool_manifest("chat"))

        assert "action-life_send_text" in manifest
        assert "action-think" not in manifest
        assert "tool-conversation_evidence" in manifest
        assert "tool-fetch_chat_history" not in manifest

    @pytest.mark.parametrize("kind", ["chat", "livestream", "voice_live"])
    def test_conversation_evidence_replaces_unbounded_history_tool(self, kind: str) -> None:
        manifest = set(get_tool_manifest(kind))
        assert "tool-conversation_evidence" in manifest
        assert "tool-fetch_chat_history" not in manifest

    @pytest.mark.parametrize("kind", ["minecraft", "livestream"])
    def test_non_chat_manifests_keep_legacy_think_action(self, kind: str) -> None:
        assert "action-think" in get_tool_manifest(kind)

    @pytest.mark.parametrize("kind", ["voice_live", "livestream"])
    def test_live_scenes_are_explicit_and_can_report_state(self, kind: str) -> None:
        assert kind in CONSCIOUSNESS_TOOL_MANIFESTS
        manifest = set(get_tool_manifest(kind))
        assert "action-report_state" in manifest
        assert "tool-inner_query" in manifest

    def test_unknown_kind_must_declare_manifest(self) -> None:
        assert "future_scene" not in CONSCIOUSNESS_TOOL_MANIFESTS
        with pytest.raises(KeyError, match="not declared"):
            get_tool_manifest("future_scene")
