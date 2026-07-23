"""InsightStore：洞察实验账本。

类比 VibeGamer 的 hypothesisStore + experimentLedger + researchAgenda。
核心职责：
- Append-only 审计日志（所有状态变更先写日志再改快照）
- 洞察快照（当前所有洞察的权威状态）
- 研究议程（决定下一步该验证什么）
- 去重与冷却
- 预算控制
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    AuditRecord,
    Evidence,
    Insight,
    InsightNextAction,
    InsightStatus,
    KnowledgeVersion,
)

logger = logging.getLogger("life_engine.learning.store")

LEARNING_DIR_NAME = ".life_learning"
STORE_VERSION = 1

# 去重相似度阈值（简单文本匹配）
_DEDUP_OVERLAP_THRESHOLD = 0.7
# Topic 冷却：连续失败次数
_TOPIC_COOLDOWN_FAILURES = 3


class InsightStore:
    """洞察实验账本：持久化、状态流转、议程调度。"""

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / LEARNING_DIR_NAME
        self.insights_path = self.root / "insights.json"
        self.audit_log_path = self.root / "insights_audit.jsonl"
        self.knowledge_dir = self.root / "knowledge"
        self.state_path = self.root / "state.json"
        self._insights: list[Insight] = []
        self._loaded = False

    # ── 加载 / 保存 ──────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        """从磁盘加载洞察快照。"""
        if self._loaded:
            return
        self._ensure_dirs()
        if not self.insights_path.exists():
            self._insights = []
            self._loaded = True
            return
        try:
            raw = json.loads(self.insights_path.read_text(encoding="utf-8"))
            version = raw.get("version", STORE_VERSION)
            items = raw.get("insights", [])
            self._insights = [
                Insight.from_dict(item)
                for item in items
                if isinstance(item, dict)
            ]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"加载洞察账本失败: {exc}")
            self._insights = []
        self._loaded = True

    def _save(self) -> None:
        """原子写入洞察快照。"""
        self._ensure_dirs()
        payload = {
            "version": STORE_VERSION,
            "updated_at": _now_iso(),
            "insights": [ins.to_dict() for ins in self._insights],
        }
        temp = self.insights_path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.insights_path)

    def _append_audit(self, event: dict[str, Any]) -> None:
        """Append-only 审计日志。"""
        self._ensure_dirs()
        event.setdefault("audit_event_id", f"ae_{uuid4().hex[:12]}")
        event.setdefault("timestamp", _now_iso())
        with self.audit_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ── CRUD ─────────────────────────────────────────────────

    def add_insight(self, insight: Insight) -> bool:
        """添加新洞察（带去重检查）。返回是否成功添加。"""
        self.load()
        if self._is_duplicate(insight):
            logger.info(f"洞察去重: '{insight.claim[:40]}...' 与已有洞察重复")
            return False
        if self._is_topic_cooled_down(insight.topic_key):
            logger.info(f"Topic '{insight.topic_key}' 冷却中，暂不接收")
            return False
        self._insights.append(insight)
        self._append_audit({
            "action": "insight_created",
            "insight_id": insight.insight_id,
            "claim": insight.claim,
            "category": insight.category,
        })
        self._save()
        return True

    def get_insight(self, insight_id: str) -> Insight | None:
        self.load()
        for ins in self._insights:
            if ins.insight_id == insight_id:
                return ins
        return None

    def update_insight(self, insight: Insight) -> None:
        """更新洞察并记录审计。"""
        self.load()
        for i, existing in enumerate(self._insights):
            if existing.insight_id == insight.insight_id:
                self._insights[i] = insight
                break
        insight.updated_at = _now_iso()
        self._save()

    def add_evidence(self, insight_id: str, evidence: Evidence) -> bool:
        """为洞察添加证据。"""
        insight = self.get_insight(insight_id)
        if insight is None:
            return False
        insight.add_evidence(evidence)
        self._append_audit({
            "action": "evidence_added",
            "insight_id": insight_id,
            "evidence_id": evidence.evidence_id,
            "supports": evidence.supports,
            "kind": evidence.kind,
        })
        self._save()
        return True

    # ── 状态流转 ─────────────────────────────────────────────

    def transition_status(
        self,
        insight_id: str,
        new_status: InsightStatus | str,
        *,
        next_action: InsightNextAction | str | None = None,
        reason: str = "",
        audit_record: AuditRecord | None = None,
    ) -> bool:
        """执行状态流转，写审计日志。"""
        insight = self.get_insight(insight_id)
        if insight is None:
            return False

        old_status = insight.status
        new_status_val = new_status.value if isinstance(new_status, InsightStatus) else str(new_status)
        insight.status = new_status_val

        if next_action is not None:
            na_val = next_action.value if isinstance(next_action, InsightNextAction) else str(next_action)
            insight.next_action = na_val

        if audit_record is not None:
            insight.audit_history.append(audit_record)
            insight.review_count += 1
            if audit_record.bias_detected:
                insight.anti_bias_flags.extend(audit_record.bias_detected)

        insight.updated_at = _now_iso()

        self._append_audit({
            "action": "status_transition",
            "insight_id": insight_id,
            "from_status": old_status,
            "to_status": new_status_val,
            "next_action": insight.next_action,
            "reason": reason,
        })
        self._save()
        return True

    # ── 查询 / 议程 ─────────────────────────────────────────

    def list_all(self) -> list[Insight]:
        self.load()
        return list(self._insights)

    def list_by_status(self, status: InsightStatus | str) -> list[Insight]:
        self.load()
        status_val = status.value if isinstance(status, InsightStatus) else str(status)
        return [ins for ins in self._insights if ins.status == status_val]

    def list_validated(self) -> list[Insight]:
        return self.list_by_status(InsightStatus.VALIDATED)

    def list_candidates_for_review(self) -> list[Insight]:
        """获取可被审计的候选洞察（按优先级排序）。"""
        self.load()
        candidates = [
            ins for ins in self._insights
            if ins.can_review
        ]
        # 优先级：证据多的优先、等待时间长的优先
        return sorted(
            candidates,
            key=lambda ins: (
                -len(ins.evidence),
                ins.born_at,
            ),
        )

    def list_for_compression(self) -> list[Insight]:
        """获取可被慢环压缩的 validated 洞察。"""
        return [
            ins for ins in self.list_validated()
            if ins.next_action == InsightNextAction.PROMOTE.value
        ]

    def get_stats(self) -> dict[str, Any]:
        """账本统计。"""
        self.load()
        stats: dict[str, int] = {}
        for ins in self._insights:
            stats[ins.status] = stats.get(ins.status, 0) + 1
        total = len(self._insights)
        validated = stats.get(InsightStatus.VALIDATED.value, 0)
        return {
            "total": total,
            "by_status": stats,
            "validation_rate": validated / total if total > 0 else 0.0,
            "topics": self._topic_distribution(),
        }

    # ── 去重与冷却 ───────────────────────────────────────────

    def _is_duplicate(self, new_insight: Insight) -> bool:
        """简单文本重叠度去重。"""
        if not new_insight.claim:
            return False
        new_tokens = set(_tokenize(new_insight.claim))
        for existing in self._insights:
            if existing.status in (InsightStatus.ARCHIVED.value,):
                continue
            if existing.topic_key and new_insight.topic_key:
                if existing.topic_key != new_insight.topic_key:
                    continue
            existing_tokens = set(_tokenize(existing.claim))
            if not existing_tokens:
                continue
            overlap = len(new_tokens & existing_tokens) / max(len(new_tokens), 1)
            if overlap >= _DEDUP_OVERLAP_THRESHOLD:
                return True
        return False

    def _is_topic_cooled_down(self, topic_key: str) -> bool:
        """检查 topic 是否因连续失败而冷却。"""
        if not topic_key:
            return False
        self.load()
        topic_insights = [
            ins for ins in self._insights
            if ins.topic_key == topic_key
        ]
        if len(topic_insights) < _TOPIC_COOLDOWN_FAILURES:
            return False
        # 最近 N 条是否全是 rejected/archived
        recent = sorted(topic_insights, key=lambda i: i.born_at, reverse=True)[
            :_TOPIC_COOLDOWN_FAILURES
        ]
        return all(
            ins.status in (InsightStatus.REJECTED.value, InsightStatus.ARCHIVED.value)
            for ins in recent
        )

    def _topic_distribution(self) -> dict[str, int]:
        topics: dict[str, int] = {}
        for ins in self._insights:
            key = ins.topic_key or "uncategorized"
            topics[key] = topics.get(key, 0) + 1
        return topics

    # ── 知识版本管理 ─────────────────────────────────────────

    def load_knowledge_manifest(self) -> dict[str, Any]:
        manifest_path = self.knowledge_dir / "manifest.json"
        if not manifest_path.exists():
            return {"versions": [], "current_version": 0}
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"versions": [], "current_version": 0}

    def save_knowledge_manifest(self, manifest: dict[str, Any]) -> None:
        self._ensure_dirs()
        manifest_path = self.knowledge_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_current_knowledge_path(self) -> Path:
        return self.knowledge_dir / "self_knowledge.md"

    def read_current_knowledge(self) -> str:
        path = self.get_current_knowledge_path()
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def write_knowledge_version(
        self,
        content: str,
        version: int,
        insight_ids: list[str],
        edit_count: int,
        promoted: bool,
        reason: str = "",
    ) -> KnowledgeVersion:
        """写入新版本自我认知文档。"""
        self._ensure_dirs()
        # 写版本文件
        version_path = self.knowledge_dir / f"v{version}.md"
        version_path.write_text(content, encoding="utf-8")

        kv = KnowledgeVersion(
            version=version,
            timestamp=_now_iso(),
            file_path=f"knowledge/v{version}.md",
            insight_ids=insight_ids,
            edit_count=edit_count,
            promoted=promoted,
            selection_reason=reason,
        )

        # 更新 manifest
        manifest = self.load_knowledge_manifest()
        versions = manifest.get("versions", [])
        versions.append(kv.to_dict())
        manifest["versions"] = versions
        if promoted:
            manifest["current_version"] = version
            # 同时写入 self_knowledge.md
            self.get_current_knowledge_path().write_text(content, encoding="utf-8")
        self.save_knowledge_manifest(manifest)

        self._append_audit({
            "action": "knowledge_version_written",
            "version": version,
            "promoted": promoted,
            "insight_count": len(insight_ids),
            "edit_count": edit_count,
        })
        return kv

    # ── 状态持久化 ───────────────────────────────────────────

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save_state(self, state: dict[str, Any]) -> None:
        self._ensure_dirs()
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ── 工具函数 ──────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _tokenize(text: str) -> list[str]:
    """简单分词（中文按字，英文按空格）。"""
    tokens: list[str] = []
    for char in text:
        if char.isascii() and char.isalnum():
            tokens.append(char.lower())
        elif not char.isascii() and char.strip():
            tokens.append(char)
    return tokens
