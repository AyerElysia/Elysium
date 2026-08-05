"""ThoughtStream 管理器。"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import ThoughtStream

logger = logging.getLogger("life_engine.streams")


class ThoughtStreamManager:
    """思考流管理器：CRUD、评分、持久化。

    同时承担注意力脑区中"可表达单元"的角色：
    - lazy 半衰期衰减 curiosity_score
    - 独立维护 last_focused_at 用于焦点判定
    - 单调递增 revision 用于 chatter 端 delta 追踪
    """

    def __init__(
        self,
        workspace_path: str,
        max_active: int = 5,
        dormancy_hours: int = 24,
        *,
        curiosity_decay_half_life_hours: float = 12.0,
        curiosity_floor: float = 0.15,
    ) -> None:
        """初始化思考流管理器。

        Args:
            workspace_path: 工作空间路径
            max_active: 最大活跃思考流数量
            dormancy_hours: 超过此小时数未推进则自动休眠
            curiosity_decay_half_life_hours: curiosity 半衰期（小时）
            curiosity_floor: curiosity 衰减下限
        """
        self._workspace = Path(workspace_path)
        self._thoughts_dir = self._workspace / "thoughts"
        self._thoughts_dir.mkdir(parents=True, exist_ok=True)
        self._index_file = self._thoughts_dir / "streams.json"
        self._max_active = max_active
        self._dormancy_hours = dormancy_hours
        self._half_life_hours = max(0.5, float(curiosity_decay_half_life_hours))
        self._curiosity_floor = max(0.0, min(0.99, float(curiosity_floor)))
        self._streams: dict[str, ThoughtStream] = {}
        self._global_revision: int = 0
        self._load()

    # ── 内部辅助 ──────────────────────────────────────────────

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _bump_revision(self, ts: ThoughtStream) -> None:
        """单调递增全局与本流的 revision。"""
        self._global_revision += 1
        ts.revision = self._global_revision

    def _apply_lazy_decay(self, ts: ThoughtStream) -> bool:
        """根据 last_decay_at 与半衰期 lazy 衰减 curiosity。

        Returns:
            是否真的发生衰减（用于决定是否需要 _save）。
        """
        if ts.curiosity_score <= self._curiosity_floor:
            # 仍需推进 last_decay_at 锚点，避免下次重启后产生大跳变
            if not ts.last_decay_at:
                ts.last_decay_at = self._now_iso()
                return True
            return False
        if not ts.last_decay_at:
            ts.last_decay_at = self._now_iso()
            return True
        try:
            last = datetime.fromisoformat(ts.last_decay_at)
        except (ValueError, TypeError):
            ts.last_decay_at = self._now_iso()
            return True
        now = datetime.now(timezone.utc)
        hours = (now - last).total_seconds() / 3600.0
        if hours <= 0:
            return False
        # 用整小时步长衰减，避免亚秒抖动每次访问都触发 _save
        if hours < 0.25:
            return False
        factor = math.pow(0.5, hours / self._half_life_hours)
        new_score = max(self._curiosity_floor, ts.curiosity_score * factor)
        if abs(new_score - ts.curiosity_score) < 1e-4:
            ts.last_decay_at = now.isoformat()
            return False
        ts.curiosity_score = new_score
        ts.last_decay_at = now.isoformat()
        return True

    def _decay_all(self) -> None:
        """对全部流执行 lazy 衰减；如有变更则持久化一次。"""
        changed = False
        for ts in self._streams.values():
            if self._apply_lazy_decay(ts):
                changed = True
        if changed:
            self._save()

    @property
    def current_revision(self) -> int:
        """全局最高 revision（用于 chatter 端 cursor 提交）。"""
        return self._global_revision

    # ── 持久化 ────────────────────────────────────────────────

    def _load(self) -> None:
        """从磁盘加载思考流索引。"""
        if not self._index_file.exists():
            return
        try:
            data = json.loads(self._index_file.read_text(encoding="utf-8"))
            for item in data.get("streams", []):
                ts = ThoughtStream(
                    id=item["id"],
                    title=item["title"],
                    created_at=item["created_at"],
                    last_advanced_at=item["last_advanced_at"],
                    advance_count=item.get("advance_count", 0),
                    curiosity_score=item.get("curiosity_score", 0.7),
                    last_thought=item.get("last_thought", ""),
                    related_memories=item.get("related_memories", []),
                    status=item.get("status", "active"),
                    last_focused_at=item.get("last_focused_at", ""),
                    last_decay_at=item.get("last_decay_at", ""),
                    revision=int(item.get("revision", 0) or 0),
                )
                self._streams[ts.id] = ts
                if ts.revision > self._global_revision:
                    self._global_revision = ts.revision
            # 兼容老数据：缺失 revision 的流补一个递增号
            for ts in self._streams.values():
                if ts.revision <= 0:
                    self._global_revision += 1
                    ts.revision = self._global_revision
        except Exception as e:
            logger.warning(f"加载思考流索引失败: {e}")

    def _save(self) -> None:
        """持久化到磁盘。"""
        data = {
            "schema_version": 2,
            "global_revision": self._global_revision,
            "streams": [
                {
                    "id": ts.id,
                    "title": ts.title,
                    "created_at": ts.created_at,
                    "last_advanced_at": ts.last_advanced_at,
                    "advance_count": ts.advance_count,
                    "curiosity_score": ts.curiosity_score,
                    "last_thought": ts.last_thought,
                    "related_memories": ts.related_memories,
                    "status": ts.status,
                    "last_focused_at": ts.last_focused_at,
                    "last_decay_at": ts.last_decay_at,
                    "revision": ts.revision,
                }
                for ts in self._streams.values()
            ],
        }
        try:
            self._index_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存思考流索引失败: {e}")

    def _make_room_for_active(self, exclude_id: str | None = None) -> None:
        """确保再激活一条思考流后仍不超过活跃上限。"""
        active = [
            s for s in self._streams.values()
            if s.is_active() and s.id != exclude_id
        ]
        while len(active) >= self._max_active:
            active.sort(key=lambda s: s.curiosity_score)
            weakest = active.pop(0)
            weakest.status = "dormant"
            self._bump_revision(weakest)
            logger.info(f"思考流达上限，{weakest.title} 转入休眠")

    # ── CRUD ──────────────────────────────────────────────────

    def create(
        self,
        title: str,
        reason: str = "",
        related_memories: list[str] | None = None,
    ) -> ThoughtStream:
        """创建新思考流。create 不刷新 last_focused_at（避免被误判为焦点）。"""
        now = self._now_iso()
        ts = ThoughtStream(
            id=f"ts_{uuid4().hex[:8]}",
            title=title,
            created_at=now,
            last_advanced_at=now,
            related_memories=related_memories or [],
            last_decay_at=now,
        )
        self._make_room_for_active()
        self._bump_revision(ts)
        self._streams[ts.id] = ts
        self._save()
        logger.info(f"创建思考流: {ts.title} ({ts.id})")
        return ts

    def list_active(self) -> list[ThoughtStream]:
        """列出所有活跃思考流，按好奇心排序。"""
        self._decay_all()
        active = [s for s in self._streams.values() if s.is_active()]
        active.sort(key=lambda s: s.curiosity_score, reverse=True)
        return active

    def list_all(self) -> list[ThoughtStream]:
        """列出所有思考流。"""
        self._decay_all()
        return list(self._streams.values())

    def list_for_projection(
        self,
        *,
        include_dormant: bool = False,
    ) -> list[ThoughtStream]:
        """返回旧工具可安全投影的思考流快照。

        该查询保持旧 ``include_dormant`` 参数的兼容语义，但不会把
        ``completed`` 等终态记录混入结果。排序只依赖持久字段，供上层
        使用 revision-bound cursor 做稳定分页；好奇心分数仍可展示，
        但不再作为分页位置。该投影查询是纯读取，不执行衰减、休眠或
        任何持久化写入。
        """
        allowed_statuses = {"active"}
        if include_dormant:
            allowed_statuses.add("dormant")

        streams = [
            stream
            for stream in self._streams.values()
            if stream.status in allowed_statuses
        ]

        def _sort_key(stream: ThoughtStream) -> tuple[int, float, str]:
            try:
                advanced_at = datetime.fromisoformat(
                    stream.last_advanced_at
                ).timestamp()
            except (TypeError, ValueError):
                advanced_at = 0.0
            status_rank = 0 if stream.status == "active" else 1
            return status_rank, -advanced_at, stream.id

        streams.sort(key=_sort_key)
        return streams

    def get(self, stream_id: str) -> ThoughtStream | None:
        """获取指定思考流。"""
        ts = self._streams.get(stream_id)
        if ts is not None:
            if self._apply_lazy_decay(ts):
                self._save()
        return ts

    def advance(
        self,
        stream_id: str,
        thought: str,
        curiosity_delta: float = 0.0,
    ) -> tuple[bool, str]:
        """推进一条思考流。同时刷新 last_focused_at 与 revision。"""
        ts = self._streams.get(stream_id)
        if not ts:
            return False, f"思考流 {stream_id} 不存在"
        if ts.status == "completed":
            return False, f"思考流「{ts.title}」已完成，无法继续推进"
        if ts.status == "dormant":
            self._make_room_for_active(exclude_id=ts.id)
            ts.status = "active"
            ts.curiosity_score = max(ts.curiosity_score, 0.5)
            logger.info(f"休眠思考流自动激活: {ts.title}")
        elif ts.status != "active":
            return False, f"思考流「{ts.title}」当前状态为 {ts.status}，无法推进"

        # 先 lazy 衰减再施加 delta，避免衰减把本次 delta 吃掉
        self._apply_lazy_decay(ts)

        ts.advance_count += 1
        ts.last_thought = thought[:500]
        now_iso = self._now_iso()
        ts.last_advanced_at = now_iso
        ts.last_focused_at = now_iso
        ts.last_decay_at = now_iso
        ts.curiosity_score = max(0.0, min(1.0, ts.curiosity_score + curiosity_delta))
        self._bump_revision(ts)

        self._save()
        logger.info(f"推进思考流: {ts.title} (第{ts.advance_count}次)")
        return True, f"已推进思考流「{ts.title}」(第{ts.advance_count}次，好奇心: {ts.curiosity_score:.2f})"

    def retire(
        self,
        stream_id: str,
        new_status: str = "completed",
        conclusion: str = "",
    ) -> tuple[bool, str]:
        """结束或休眠一条思考流。"""
        ts = self._streams.get(stream_id)
        if not ts:
            return False, f"思考流 {stream_id} 不存在"

        old_status = ts.status
        ts.status = new_status
        if conclusion:
            ts.last_thought = conclusion[:500]
        self._bump_revision(ts)

        self._save()
        logger.info(f"思考流 {ts.title}: {old_status} -> {new_status}")
        return True, f"思考流「{ts.title}」已标记为 {new_status}"

    def reactivate(self, stream_id: str) -> tuple[bool, str]:
        """重新激活一条休眠的思考流。reactivate 视为重新进入焦点。"""
        ts = self._streams.get(stream_id)
        if not ts:
            return False, f"思考流 {stream_id} 不存在"
        if ts.status == "active":
            return False, f"思考流「{ts.title}」已经是活跃状态"
        if ts.status == "completed":
            return False, "已完成的思考流不能重新激活"

        self._make_room_for_active(exclude_id=ts.id)
        ts.status = "active"
        ts.curiosity_score = max(ts.curiosity_score, 0.5)
        now_iso = self._now_iso()
        ts.last_advanced_at = now_iso
        ts.last_focused_at = now_iso
        ts.last_decay_at = now_iso
        self._bump_revision(ts)
        self._save()
        return True, f"思考流「{ts.title}」已重新激活"

    def check_dormancy(self) -> list[str]:
        """检查并自动休眠超时的思考流。"""
        dormant_ids: list[str] = []
        for ts in self._streams.values():
            if ts.should_go_dormant(self._dormancy_hours):
                ts.status = "dormant"
                self._bump_revision(ts)
                dormant_ids.append(ts.id)
        if dormant_ids:
            self._save()
            logger.info(f"自动休眠思考流: {dormant_ids}")
        return dormant_ids

    # ── 渲染 ──────────────────────────────────────────────────

    @staticmethod
    def _format_relative_time(iso_str: str) -> str:
        try:
            last = datetime.fromisoformat(iso_str)
            now = datetime.now(timezone.utc)
            minutes_ago = int((now - last).total_seconds() / 60)
            if minutes_ago < 1:
                return "刚刚"
            if minutes_ago < 60:
                return f"{minutes_ago}分钟前"
            hours_ago = minutes_ago // 60
            if hours_ago < 24:
                return f"{hours_ago}小时前"
            days_ago = hours_ago // 24
            return f"{days_ago}天前"
        except (ValueError, TypeError):
            return "未知"

    def _render_one(self, ts: ThoughtStream, *, is_delta: bool, mark_delta: bool) -> list[str]:
        prefix = "🔄(刚推进) " if (is_delta and mark_delta) else ""
        time_str = self._format_relative_time(ts.last_advanced_at)
        lines = [
            f"- {prefix}**{ts.title}** "
            f"(好奇心: {ts.curiosity_score:.0%}, 上次推进: {time_str})"
        ]
        if ts.last_thought:
            lines.append(f"  最近想法: {ts.last_thought[:200]}")
        return lines

    def format_for_prompt(
        self,
        max_items: int = 3,
        *,
        focus_window_minutes: int | None = None,
        revision_cursor: int = 0,
        mark_delta: bool = True,
        grouped: bool = False,
    ) -> str:
        """格式化为 prompt 片段。

        Args:
            max_items: 最多展示的活跃思考流总数
            focus_window_minutes: 焦点窗口（分钟）。仅在 grouped=True 时生效
            revision_cursor: 已被消费方看过的最高 revision；超过此值的流会被加 🔄(刚推进) 标记
            mark_delta: 是否启用 delta 标记
            grouped: 是否分"当前焦点 / 背景在意"两组渲染

        返回的文本不再包含顶层 `### 当前思考流` 标题，由调用方自行包装。
        """
        active = self.list_active()
        if not active:
            return ""

        capped = active[: max(1, int(max_items))]

        if not grouped or focus_window_minutes is None or focus_window_minutes <= 0:
            lines: list[str] = []
            for ts in capped:
                is_delta = ts.revision > int(revision_cursor or 0)
                lines.extend(self._render_one(ts, is_delta=is_delta, mark_delta=mark_delta))
                lines.append("")
            if len(active) > len(capped):
                lines.append(f"... 还有 {len(active) - len(capped)} 条活跃思考流")
            return "\n".join(lines).rstrip() + "\n"

        focused: list[ThoughtStream] = []
        background: list[ThoughtStream] = []
        for ts in capped:
            if ts.is_focused(focus_window_minutes):
                focused.append(ts)
            else:
                background.append(ts)

        sections: list[str] = []
        if focused:
            sections.append("**当前焦点**")
            for ts in focused:
                is_delta = ts.revision > int(revision_cursor or 0)
                sections.extend(self._render_one(ts, is_delta=is_delta, mark_delta=mark_delta))
            sections.append("")
        if background:
            sections.append("**背景在意**")
            for ts in background:
                is_delta = ts.revision > int(revision_cursor or 0)
                sections.extend(self._render_one(ts, is_delta=is_delta, mark_delta=mark_delta))
            sections.append("")
        if len(active) > len(capped):
            sections.append(f"... 还有 {len(active) - len(capped)} 条活跃思考流")

        return "\n".join(sections).rstrip() + "\n"
