"""编排追踪：记录使命和任务的全链路执行轨迹。

每个 Mission 生成一个 trace 文件（JSON Lines），记录：
- 使命创建、规划结果
- 每个任务的启动、每轮执行、工具调用、完成/失败
- 使命最终状态

追踪文件存储在 {workspace}/.life_trace/orchestration/ 目录下。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.kernel.logger import get_logger

logger = get_logger("life_engine.orchestration.tracing")

_ORCHESTRATION_TRACE_DIR = "orchestration"


class MissionTracer:
    """单个使命的追踪器。线程不安全，由 Scheduler 在单协程内使用。"""

    def __init__(self, workspace_path: str, mission_id: str, enabled: bool = True) -> None:
        self._enabled = enabled
        self._mission_id = mission_id
        self._start_time = time.monotonic()

        if enabled:
            trace_dir = Path(workspace_path) / ".life_trace" / _ORCHESTRATION_TRACE_DIR
            trace_dir.mkdir(parents=True, exist_ok=True)
            self._path = trace_dir / f"{mission_id}.jsonl"
        else:
            self._path = None

    def trace_mission_start(self, goal: str, task_count: int, config: dict[str, Any]) -> None:
        """记录使命启动。"""
        self._write({
            "event": "mission_start",
            "mission_id": self._mission_id,
            "goal": goal[:500],
            "task_count": task_count,
            "config": config,
        })

    def trace_plan(self, reasoning: str, task_ids: list[str]) -> None:
        """记录规划结果。"""
        self._write({
            "event": "plan",
            "mission_id": self._mission_id,
            "reasoning": reasoning[:500],
            "task_ids": task_ids,
        })

    def trace_task_start(self, task_id: str, kind: str, brief: str) -> None:
        """记录任务启动。"""
        self._write({
            "event": "task_start",
            "mission_id": self._mission_id,
            "task_id": task_id,
            "kind": kind,
            "brief": brief[:300],
        })

    def trace_worker_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """记录 worker 内部事件（round_start/round_end/tool_call/done）。"""
        self._write({
            "event": f"worker.{event_type}",
            "mission_id": self._mission_id,
            **payload,
        })

    def trace_task_end(
        self,
        task_id: str,
        status: str,
        duration_ms: int,
        rounds: int,
        tokens: int,
        error: str | None = None,
    ) -> None:
        """记录任务完成。"""
        self._write({
            "event": "task_end",
            "mission_id": self._mission_id,
            "task_id": task_id,
            "status": status,
            "duration_ms": duration_ms,
            "rounds": rounds,
            "tokens": tokens,
            "error": (error or "")[:500] or None,
        })

    def trace_mission_end(
        self,
        status: str,
        total_duration_ms: int,
        total_tokens: int,
        tasks_succeeded: int,
        tasks_failed: int,
    ) -> None:
        """记录使命完成。"""
        self._write({
            "event": "mission_end",
            "mission_id": self._mission_id,
            "status": status,
            "total_duration_ms": total_duration_ms,
            "total_tokens": total_tokens,
            "tasks_succeeded": tasks_succeeded,
            "tasks_failed": tasks_failed,
        })

    # ------------------------------------------------------------------
    # 作为 TraceHook 使用
    # ------------------------------------------------------------------

    def as_trace_hook(self):
        """返回一个兼容 Worker TraceHook 签名的回调。"""
        def hook(event_type: str, payload: dict[str, Any]) -> None:
            self.trace_worker_event(event_type, payload)
        return hook

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _write(self, record: dict[str, Any]) -> None:
        if not self._enabled or self._path is None:
            return
        record["ts"] = time.time()
        record["elapsed"] = round(time.monotonic() - self._start_time, 3)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.debug(f"追踪写入失败: {exc}")
