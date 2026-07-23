"""LearningMetrics：学习曲线追踪。

类比 VibeGamer 的 learningCurve.ts。
追踪学习系统的健康度和成长轨迹。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import InsightStatus, LearningMetricsPoint
from .store import InsightStore

logger = logging.getLogger("life_engine.learning.metrics")


class LearningMetrics:
    """学习指标追踪器。"""

    def __init__(self, *, store: InsightStore) -> None:
        self._store = store
        self._metrics_path = store.root / "metrics.jsonl"

    def snapshot(self) -> LearningMetricsPoint:
        """生成当前学习状态快照并追加到 metrics.jsonl。"""
        stats = self._store.get_stats()
        by_status = stats.get("by_status", {})
        total = stats.get("total", 0)
        validated = by_status.get(InsightStatus.VALIDATED.value, 0)
        rejected = by_status.get(InsightStatus.REJECTED.value, 0)
        candidate = by_status.get(InsightStatus.CANDIDATE.value, 0)

        # 计算偏误检测次数
        bias_count = 0
        for ins in self._store.list_all():
            bias_count += len(ins.anti_bias_flags)

        # 知识版本
        manifest = self._store.load_knowledge_manifest()
        knowledge_version = int(manifest.get("current_version", 0))

        point = LearningMetricsPoint(
            timestamp=_now_iso(),
            total_insights=total,
            validated_count=validated,
            rejected_count=rejected,
            candidate_count=candidate,
            validation_rate=validated / total if total > 0 else 0.0,
            bias_detection_count=bias_count,
            knowledge_version=knowledge_version,
            topic_coverage=stats.get("topics", {}),
        )

        self._append(point)
        return point

    def recent_points(self, limit: int = 20) -> list[LearningMetricsPoint]:
        """获取最近的学习数据点。"""
        if not self._metrics_path.exists():
            return []
        points: list[LearningMetricsPoint] = []
        try:
            for line in self._metrics_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                if isinstance(raw, dict):
                    points.append(LearningMetricsPoint.from_dict(raw))
        except (json.JSONDecodeError, OSError):
            pass
        return points[-limit:]

    def format_summary(self) -> str:
        """格式化为简短摘要（用于 prompt 注入或命令输出）。"""
        stats = self._store.get_stats()
        by_status = stats.get("by_status", {})
        total = stats.get("total", 0)
        validated = by_status.get(InsightStatus.VALIDATED.value, 0)
        rejected = by_status.get(InsightStatus.REJECTED.value, 0)
        candidate = by_status.get(InsightStatus.CANDIDATE.value, 0)

        manifest = self._store.load_knowledge_manifest()
        kv = int(manifest.get("current_version", 0))

        lines = [
            f"洞察总数: {total}",
            f"已验证: {validated} | 已否定: {rejected} | 待审: {candidate}",
            f"验证率: {stats.get('validation_rate', 0):.0%}",
            f"自我认知版本: v{kv}",
        ]

        topics = stats.get("topics", {})
        if topics:
            top_topics = sorted(topics.items(), key=lambda x: -x[1])[:5]
            topic_str = ", ".join(f"{k}({v})" for k, v in top_topics)
            lines.append(f"主题覆盖: {topic_str}")

        return "\n".join(lines)

    def format_progress_for_prompt(self) -> str:
        """格式化为心跳 prompt 注入的简短进展。"""
        stats = self._store.get_stats()
        total = stats.get("total", 0)
        if total == 0:
            return ""

        by_status = stats.get("by_status", {})
        validated = by_status.get(InsightStatus.VALIDATED.value, 0)
        candidate = by_status.get(InsightStatus.CANDIDATE.value, 0)

        manifest = self._store.load_knowledge_manifest()
        kv = int(manifest.get("current_version", 0))

        parts = []
        if candidate > 0:
            parts.append(f"{candidate}条洞察待审")
        if validated > 0:
            parts.append(f"{validated}条已验证")
        if kv > 0:
            parts.append(f"自我认知v{kv}")

        if not parts:
            return ""
        return "### 学习进展\n" + "、".join(parts)

    def _append(self, point: LearningMetricsPoint) -> None:
        """追加数据点到 metrics.jsonl。"""
        self._store._ensure_dirs()
        with self._metrics_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(point.to_dict(), ensure_ascii=False) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
