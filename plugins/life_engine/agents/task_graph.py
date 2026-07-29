"""任务图：有向无环图（DAG）管理。

管理任务之间的依赖关系，提供：
- 环检测（拒绝非法依赖）
- 拓扑排序（确定执行顺序）
- 就绪集计算（依赖已满足、可立即执行的任务）
- 动态追加任务（执行中发现需要额外步骤）
- 下游级联标记（上游失败时跳过所有下游）
"""

from __future__ import annotations

from collections import deque

from .contracts import TaskContract, TaskStatus


class CycleDetectedError(Exception):
    """任务图中检测到环。"""


class TaskGraph:
    """线程不安全的任务 DAG——由 Scheduler 在单协程内操作。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskContract] = {}
        self._status: dict[str, TaskStatus] = {}
        # 邻接表：task_id → 依赖它的下游 task_id 集合
        self._dependents: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------

    def add_task(self, task: TaskContract) -> None:
        """添加任务。如果引入环则抛出 CycleDetectedError。"""
        if task.task_id in self._tasks:
            raise ValueError(f"任务已存在: {task.task_id}")

        # 验证依赖存在
        for dep in task.depends_on:
            if dep not in self._tasks:
                raise ValueError(
                    f"任务 {task.task_id} 依赖不存在的任务: {dep}"
                )

        # 试探性添加后检测环
        self._tasks[task.task_id] = task
        self._status[task.task_id] = TaskStatus.PENDING
        self._dependents.setdefault(task.task_id, set())
        for dep in task.depends_on:
            self._dependents[dep].add(task.task_id)

        if self._has_cycle():
            # 回滚
            del self._tasks[task.task_id]
            del self._status[task.task_id]
            del self._dependents[task.task_id]
            for dep in task.depends_on:
                self._dependents[dep].discard(task.task_id)
            raise CycleDetectedError(
                f"添加任务 {task.task_id} 会引入环依赖"
            )

    def add_tasks(self, tasks: list[TaskContract]) -> None:
        """批量添加任务（按依赖顺序）。"""
        for task in self._topological_sort_new(tasks):
            self.add_task(task)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def task_ids(self) -> list[str]:
        return list(self._tasks.keys())

    def get_task(self, task_id: str) -> TaskContract | None:
        return self._tasks.get(task_id)

    def get_status(self, task_id: str) -> TaskStatus | None:
        return self._status.get(task_id)

    def set_status(self, task_id: str, status: TaskStatus) -> None:
        if task_id not in self._status:
            raise KeyError(f"未知任务: {task_id}")
        self._status[task_id] = status

    @property
    def total_count(self) -> int:
        return len(self._tasks)

    def has_pending(self) -> bool:
        """是否还有未完成的任务。"""
        non_terminal = {
            TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING,
        }
        return any(s in non_terminal for s in self._status.values())

    def get_ready_tasks(self) -> list[TaskContract]:
        """返回所有依赖已满足、可立即执行的任务（按优先级排序）。"""
        ready: list[TaskContract] = []
        for task_id, status in self._status.items():
            if status != TaskStatus.PENDING:
                continue
            task = self._tasks[task_id]
            if self._deps_satisfied(task):
                ready.append(task)
        # 优先级数值越小越先执行
        ready.sort(key=lambda t: t.priority)
        return ready

    def get_downstream(self, task_id: str) -> set[str]:
        """获取某任务的所有直接和间接下游任务 ID。"""
        visited: set[str] = set()
        queue = deque(self._dependents.get(task_id, set()))
        while queue:
            nid = queue.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            queue.extend(self._dependents.get(nid, set()))
        return visited

    def all_terminal(self) -> bool:
        """所有任务是否都到达终态。"""
        terminal = {
            TaskStatus.SUCCEEDED, TaskStatus.FAILED,
            TaskStatus.CANCELLED, TaskStatus.TIMEOUT, TaskStatus.SKIPPED,
        }
        return all(s in terminal for s in self._status.values())

    # ------------------------------------------------------------------
    # 级联操作
    # ------------------------------------------------------------------

    def cascade_skip(self, failed_task_id: str) -> list[str]:
        """将失败任务的所有下游标记为 SKIPPED，返回被跳过的 task_id 列表。"""
        downstream = self.get_downstream(failed_task_id)
        skipped: list[str] = []
        for tid in downstream:
            if self._status[tid] in (TaskStatus.PENDING, TaskStatus.READY):
                self._status[tid] = TaskStatus.SKIPPED
                skipped.append(tid)
        return skipped

    def cascade_cancel(self) -> list[str]:
        """取消所有未完成任务，返回被取消的 task_id 列表。"""
        cancelled: list[str] = []
        cancellable = {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}
        for tid, status in self._status.items():
            if status in cancellable:
                self._status[tid] = TaskStatus.CANCELLED
                cancelled.append(tid)
        return cancelled

    # ------------------------------------------------------------------
    # 拓扑排序
    # ------------------------------------------------------------------

    def topological_order(self) -> list[str]:
        """返回所有任务的拓扑排序。"""
        in_degree: dict[str, int] = {tid: 0 for tid in self._tasks}
        for task in self._tasks.values():
            for dep in task.depends_on:
                if dep in in_degree:
                    in_degree[task.task_id] += 1

        queue = deque(
            tid for tid, deg in in_degree.items() if deg == 0
        )
        order: list[str] = []
        while queue:
            tid = queue.popleft()
            order.append(tid)
            for dependent in self._dependents.get(tid, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(self._tasks):
            raise CycleDetectedError("任务图包含环")
        return order

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _deps_satisfied(self, task: TaskContract) -> bool:
        """检查任务的所有依赖是否已成功完成。"""
        for dep in task.depends_on:
            dep_status = self._status.get(dep)
            if dep_status != TaskStatus.SUCCEEDED:
                return False
        return True

    def _has_cycle(self) -> bool:
        """Kahn 算法检测环。"""
        in_degree: dict[str, int] = {tid: 0 for tid in self._tasks}
        for task in self._tasks.values():
            for dep in task.depends_on:
                if dep in in_degree:
                    in_degree[task.task_id] += 1

        queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
        visited = 0
        while queue:
            tid = queue.popleft()
            visited += 1
            for dependent in self._dependents.get(tid, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        return visited != len(self._tasks)

    @staticmethod
    def _topological_sort_new(tasks: list[TaskContract]) -> list[TaskContract]:
        """对一批新任务按依赖关系排序（被依赖的先添加）。"""
        by_id = {t.task_id: t for t in tasks}
        in_degree: dict[str, int] = {t.task_id: 0 for t in tasks}
        adj: dict[str, list[str]] = {t.task_id: [] for t in tasks}

        for t in tasks:
            for dep in t.depends_on:
                if dep in by_id:
                    in_degree[t.task_id] += 1
                    adj[dep].append(t.task_id)

        queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
        ordered: list[TaskContract] = []
        while queue:
            tid = queue.popleft()
            ordered.append(by_id[tid])
            for child in adj[tid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(ordered) != len(tasks):
            raise CycleDetectedError("批量添加的任务中包含环依赖")
        return ordered
