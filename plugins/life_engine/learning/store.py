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
    ValidationExperiment,
    EvidenceKind,
)
from .semantic_matcher import (
    match_insight_pattern,
    semantic_overlap,
    batch_match,
    REINFORCE_THRESHOLD,
    MERGE_THRESHOLD,
)

logger = logging.getLogger("life_engine.learning.store")

LEARNING_DIR_NAME = ".life_learning"
STORE_VERSION = 1
EXPERIMENTS_FILE = "validation_experiments.json"

## 阈值由 semantic_matcher 统一管理（基于 BGE-M3 embedding 校准）：
#   REINFORCE_THRESHOLD = 0.65  (cosine >= 此值 → 同一模式，可强化)
#   MERGE_THRESHOLD = 0.75      (cosine >= 此值 → 高度重复，应合并)
_REINFORCE_OVERLAP_WITH_TOPIC = REINFORCE_THRESHOLD
_REINFORCE_OVERLAP_NO_TOPIC = REINFORCE_THRESHOLD
_DEDUP_OVERLAP_THRESHOLD = REINFORCE_THRESHOLD
# 可被强化的状态（活跃且尚未验证/否定/归档）
_REINFORCEABLE_STATUSES = (
    InsightStatus.CANDIDATE.value,
    InsightStatus.UNDER_REVIEW.value,
    InsightStatus.NEEDS_MORE_EVIDENCE.value,
)


class InsightStore:
    """洞察实验账本：持久化、状态流转、议程调度。"""

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / LEARNING_DIR_NAME
        self.insights_path = self.root / "insights.json"
        self.audit_log_path = self.root / "insights_audit.jsonl"
        self.knowledge_dir = self.root / "knowledge"
        self.state_path = self.root / "state.json"
        self.experiments_path = self.root / EXPERIMENTS_FILE
        self._insights: list[Insight] = []
        self._experiments: dict[str, list[ValidationExperiment]] = {
            "pending": [],
            "completed": [],
        }
        self._loaded = False
        self._experiments_loaded = False

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
        """添加新洞察。返回是否创建了新洞察。

        若这条观察表达的是已有洞察的同一模式，不再静默丢弃——
        把它携带的证据并入那条已有洞察。反思提示词承诺过
        "复现即证据"，这里是那句承诺的落地处。
        """
        self.load()
        dup = self._find_duplicate(insight)
        if dup is not None:
            merged = self._merge_as_evidence(dup, insight)
            logger.info(
                f"洞察复现: '{insight.claim[:40]}...' → "
                f"并入 {dup.insight_id}（证据 {'已' if merged else '未'}累积）"
            )
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

    # ── 强化（证据累积）──────────────────────────────────

    def find_reinforce_target(self, new_insight: Insight) -> Insight | None:
        """为一条新观察寻找可强化的已有洞察。

        匹配逻辑（使用语义匹配）：
        - 目标必须处于活跃状态（candidate/under_review/needs_more_evidence）
        - claim 语义重叠 ≥ 0.45 即视为"同一模式的改述"，累积为证据
        - topic_key 不同不再直接排除：她每次反思都自由命名 topic，
          topic 相同本就罕见，用它做硬门禁会让改述永远合并不上
        返回匹配度最高的匹配，或 None。
        """
        self.load()
        if not new_insight.claim:
            return None

        best: Insight | None = None
        best_score = 0.0

        for existing in self._insights:
            if existing.status not in _REINFORCEABLE_STATUSES:
                continue

            # 使用语义匹配算法
            score = match_insight_pattern(
                existing.claim,
                new_insight.claim,
                topic1=existing.topic_key,
                topic2=new_insight.topic_key,
                same_topic_threshold=_REINFORCE_OVERLAP_WITH_TOPIC,
                diff_topic_threshold=_REINFORCE_OVERLAP_NO_TOPIC,
            )

            if score > best_score:
                best = existing
                best_score = score

        return best

    def reinforce_insight(
        self,
        insight_id: str,
        evidence: Evidence,
        *,
        source_events: list[str] | None = None,
    ) -> bool:
        """用一条新观察强化已有洞察。

        效果：
        - 补充证据（add_evidence 同时记录触碰）
        - 合并来源事件
        - 关键：若此前被打回收集证据（gather_evidence），重置为 await_review 重新排队待审
        - 写 append-only 审计日志

        注意：不自动修改置信度。确信度由她的主动质疑和独立他者的评估决定，
        不由重复次数机械累加。
        """
        insight = self.get_insight(insight_id)
        if insight is None:
            return False

        insight.add_evidence(evidence)  # 内部已 touch_count++ / last_touched_at
        if source_events:
            for sid in source_events:
                if sid and sid not in insight.source_events:
                    insight.source_events.append(sid)

        # 关键闭环：被打回收集证据的洞察，有了新证据后重新排队等待审计
        if (
            insight.status == InsightStatus.CANDIDATE.value
            and insight.next_action == InsightNextAction.GATHER_EVIDENCE.value
        ):
            insight.next_action = InsightNextAction.AWAIT_REVIEW.value

        insight.updated_at = _now_iso()
        self._append_audit({
            "action": "insight_reinforced",
            "insight_id": insight_id,
            "evidence_id": evidence.evidence_id,
            "supports": evidence.supports,
            "evidence_count": len(insight.evidence),
            "positive_count": insight.positive_evidence_count,
            "next_action": insight.next_action,
        })
        self._save()
        return True

    def _merge_as_evidence(self, target: Insight, observation: Insight) -> bool:
        """把一条"复现的观察"并入已有洞察，作为新证据。

        - 活跃状态的目标 → 走 reinforce_insight（会重新排队待审）
        - 已 validated / rejected 的目标 → 只附证据，不改状态：
          她（或独立审计）已经对那条下过判断，新观察是记录，不是推翻指令
        """
        items = list(observation.evidence)
        if not items:
            items = [Evidence(
                evidence_id=f"ev_{uuid4().hex[:12]}",
                timestamp=_now_iso(),
                kind=EvidenceKind.PATTERN_MATCH.value,
                description=observation.rationale or observation.claim,
                source_ref=observation.source_events[0] if observation.source_events else "",
            )]

        reinforceable = target.status in _REINFORCEABLE_STATUSES
        merged_any = False
        for ev in items:
            # 保留这次的表述，便于她看到同一模式的不同说法
            if not ev.context:
                ev.context = f"复现表述: {observation.claim}"
            if reinforceable:
                merged_any = self.reinforce_insight(
                    target.insight_id,
                    ev,
                    source_events=observation.source_events,
                ) or merged_any
            else:
                target.add_evidence(ev)
                merged_any = True

        if merged_any and not reinforceable:
            target.updated_at = _now_iso()
            self._append_audit({
                "action": "evidence_merged",
                "insight_id": target.insight_id,
                "status": target.status,
                "evidence_count": len(target.evidence),
                "reappeared_claim": observation.claim,
            })
            self._save()

        return merged_any

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

    def get_stale_insights(self, staleness_threshold_days: int = 90) -> list[tuple[Insight, int]]:
        """获取长期未被验证的洞察（供主体观察，不强制改变）。

        **尊重主体性原则**：系统不强制遗忘，只提供观察。
        主体可以选择：
        - 重新验证这些洞察
        - 发现它们仍然有效
        - 意识到它们已过时
        - 或者保持原样

        Args:
            staleness_threshold_days: 陈旧阈值（天），默认90天

        Returns:
            (洞察, 陈旧天数) 列表，按陈旧程度排序
        """
        self.load()
        stale_insights: list[tuple[Insight, int]] = []

        for ins in self._insights:
            if ins.status != InsightStatus.VALIDATED.value:
                continue

            staleness = ins.get_staleness_days()
            if staleness >= staleness_threshold_days:
                stale_insights.append((ins, staleness))

        # 按陈旧程度排序
        stale_insights.sort(key=lambda x: x[1], reverse=True)
        return stale_insights

    # ── 存量去重合并 ─────────────────────────────────────────

    def merge_duplicates(self, *, threshold: float | None = None) -> int:
        """扫描所有 candidate 洞察，合并高度重复的条目。

        使用 Embedding cosine similarity 进行全配对比较：
        - cosine >= threshold (默认 MERGE_THRESHOLD=0.75) → 合并
        - 保留证据最多的作为主体，其余的 evidence 并入主体
        - 被合并的洞察标记为 archived

        Returns:
            合并的洞察数量（被归档的条目数）
        """
        self.load()
        merge_threshold = threshold if threshold is not None else MERGE_THRESHOLD

        # 只合并活跃状态的洞察
        active = [
            ins for ins in self._insights
            if ins.status in _REINFORCEABLE_STATUSES
        ]
        if len(active) < 2:
            return 0

        claims = [ins.claim for ins in active]
        matrix = batch_match(claims)
        if matrix is None:
            logger.warning("merge_duplicates: Embedding 不可用，跳过")
            return 0

        # 用 Union-Find 聚类：cosine >= threshold 的归为一组
        n = len(active)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(n):
            for j in range(i + 1, n):
                if matrix[i][j] >= merge_threshold:
                    union(i, j)

        # 按组聚合
        groups: dict[int, list[int]] = {}
        for i in range(n):
            root = find(i)
            groups.setdefault(root, []).append(i)

        merged_count = 0
        for indices in groups.values():
            if len(indices) < 2:
                continue
            # 选证据最多的作为主体
            insights_in_group = [active[i] for i in indices]
            insights_in_group.sort(key=lambda ins: len(ins.evidence), reverse=True)
            primary = insights_in_group[0]

            for secondary in insights_in_group[1:]:
                # 把 secondary 的证据并入 primary
                for ev in secondary.evidence:
                    if ev.evidence_id not in {e.evidence_id for e in primary.evidence}:
                        primary.add_evidence(ev)
                # 合并来源事件
                for sid in secondary.source_events:
                    if sid not in primary.source_events:
                        primary.source_events.append(sid)
                # 归档 secondary
                secondary.status = InsightStatus.ARCHIVED.value
                secondary.next_action = InsightNextAction.ARCHIVE.value
                secondary.revision_note = f"已合并入 {primary.insight_id}"
                merged_count += 1

                self._append_audit({
                    "action": "insight_merged",
                    "merged_id": secondary.insight_id,
                    "into_id": primary.insight_id,
                    "similarity": float(matrix[
                        indices[insights_in_group.index(secondary)]
                    ][indices[0]]),
                })

            primary.updated_at = _now_iso()
            logger.info(
                f"合并 {len(indices)-1} 条重复洞察入 [{primary.insight_id}]: "
                f"{primary.claim[:50]}... (证据: {len(primary.evidence)})"
            )

        if merged_count > 0:
            self._save()
            logger.info(f"存量去重完成: {merged_count} 条被合并归档")
        return merged_count

    # ── 去重与冷却 ───────────────────────────────────────────

    def _find_duplicate(self, new_insight: Insight) -> Insight | None:
        """找出与新洞察表达同一模式的已有洞察（语义重叠度）。

        与 find_reinforce_target 用同一条判定线，区别只在于这里也会命中
        已 validated / rejected 的洞察——那类不再强化，但也不该被当成
        全新洞察重复记一遍。

        topic_key 不参与门禁：她每次反思自由命名 topic，用它做硬排除
        会让同一模式的改述被当成新洞察反复创建。
        """
        if not new_insight.claim:
            return None

        best: Insight | None = None
        best_overlap = 0.0
        for existing in self._insights:
            if existing.status in (InsightStatus.ARCHIVED.value,):
                continue
            overlap = semantic_overlap(existing.claim, new_insight.claim)
            if overlap >= _DEDUP_OVERLAP_THRESHOLD and overlap > best_overlap:
                best = existing
                best_overlap = overlap

        return best

    def _is_duplicate(self, new_insight: Insight) -> bool:
        """兼容保留：是否与已有洞察表达同一模式。"""
        return self._find_duplicate(new_insight) is not None

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

    # ── 验证实验管理 ────────────────────────────────────────────

    def _load_experiments(self) -> None:
        """加载验证实验数据。"""
        if self._experiments_loaded:
            return
        self._ensure_dirs()
        if not self.experiments_path.exists():
            self._experiments = {"pending": [], "completed": []}
            self._experiments_loaded = True
            return
        try:
            raw = json.loads(self.experiments_path.read_text(encoding="utf-8"))
            pending = raw.get("pending", [])
            completed = raw.get("completed", [])
            self._experiments = {
                "pending": [
                    ValidationExperiment.from_dict(e) for e in pending if isinstance(e, dict)
                ],
                "completed": [
                    ValidationExperiment.from_dict(e) for e in completed if isinstance(e, dict)
                ],
            }
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"加载验证实验失败: {exc}")
            self._experiments = {"pending": [], "completed": []}
        self._experiments_loaded = True

    def _save_experiments(self) -> None:
        """保存验证实验数据。"""
        self._ensure_dirs()
        payload = {
            "version": STORE_VERSION,
            "updated_at": _now_iso(),
            "pending": [e.to_dict() for e in self._experiments["pending"]],
            "completed": [e.to_dict() for e in self._experiments["completed"]],
        }
        temp = self.experiments_path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.experiments_path)

    def add_validation_experiment(self, exp: ValidationExperiment) -> bool:
        """添加验证实验到待验证队列。

        Args:
            exp: 验证实验

        Returns:
            是否成功添加
        """
        self._load_experiments()
        # 检查是否已存在
        for existing in self._experiments["pending"]:
            if existing.experiment_id == exp.experiment_id:
                logger.info(f"验证实验 {exp.experiment_id} 已存在")
                return False
        self._experiments["pending"].append(exp)
        self._save_experiments()
        logger.info(f"✅ 添加验证实验: {exp.hypothesis[:50]}...")
        return True

    def list_pending_experiments(self, insight_id: str | None = None) -> list[ValidationExperiment]:
        """列出待验证的实验。

        Args:
            insight_id: 可选，只列出与特定洞察相关的实验

        Returns:
            待验证实验列表
        """
        self._load_experiments()
        if insight_id:
            return [e for e in self._experiments["pending"] if e.insight_id == insight_id]
        return list(self._experiments["pending"])

    def list_completed_experiments(self, insight_id: str | None = None) -> list[ValidationExperiment]:
        """列出已完成的实验。

        Args:
            insight_id: 可选，只列出与特定洞察相关的实验

        Returns:
            已完成实验列表
        """
        self._load_experiments()
        if insight_id:
            return [e for e in self._experiments["completed"] if e.insight_id == insight_id]
        return list(self._experiments["completed"])

    def get_experiment(self, experiment_id: str) -> ValidationExperiment | None:
        """获取实验（pending 或 completed）。"""
        self._load_experiments()
        for exp in self._experiments["pending"]:
            if exp.experiment_id == experiment_id:
                return exp
        for exp in self._experiments["completed"]:
            if exp.experiment_id == experiment_id:
                return exp
        return None

    def complete_experiment(
        self,
        experiment_id: str,
        actual_outcome: str,
        result_type: str,
        notes: str = "",
    ) -> bool:
        """完成验证实验，生成证据并反馈到洞察。

        **尊重主体性**：由主体或用户主动调用，不自动触发。

        Args:
            experiment_id: 实验ID
            actual_outcome: 实际结果描述
            result_type: "confirmed" | "contradicted" | "inconclusive"
            notes: 补充说明

        Returns:
            是否成功完成
        """
        self._load_experiments()
        self.load()

        # 找到实验
        exp = None
        for i, e in enumerate(self._experiments["pending"]):
            if e.experiment_id == experiment_id:
                exp = e
                self._experiments["pending"].pop(i)
                break

        if exp is None:
            logger.warning(f"验证实验 {experiment_id} 不存在或已完成")
            return False

        # 更新实验状态
        exp.actual_outcome = actual_outcome
        exp.result_type = result_type
        exp.completed_at = _now_iso()
        exp.notes = notes
        self._experiments["completed"].append(exp)

        # 生成证据
        supports = result_type == "confirmed"
        weight = 2.0  # 验证实验的证据权重更高

        evidence = Evidence.create(
            kind=EvidenceKind.VALIDATION_EXPERIMENT,
            description=f"预测: {exp.hypothesis}\n实际: {actual_outcome}\n结果: {result_type}",
            source_ref=experiment_id,
            supports=supports,
            weight=weight,
            context=notes,
        )

        # 添加证据到洞察
        insight = self.get_insight(exp.insight_id)
        if insight:
            insight.add_evidence(evidence)

            # 如果被否定，记录反例挑战
            if result_type == "contradicted":
                insight.record_contradiction()
                logger.info(
                    f"❌ 洞察被现实否定 [{insight.insight_id}]: {insight.claim[:40]}... "
                    f"(contradiction_count={insight.contradiction_count})"
                )
            elif result_type == "confirmed":
                logger.info(
                    f"✅ 洞察被现实确认 [{insight.insight_id}]: {insight.claim[:40]}..."
                )

            self._save()

        self._save_experiments()

        self._append_audit({
            "action": "validation_experiment_completed",
            "experiment_id": experiment_id,
            "insight_id": exp.insight_id,
            "result_type": result_type,
            "supports": supports,
        })

        return True


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
