"""InsightStore：洞察实验账本。

类比 VibeGamer 的 hypothesisStore + experimentLedger + researchAgenda。
核心职责：
- Append-only 审计日志（所有状态变更先写日志再改快照）
- 洞察快照（当前所有洞察的权威状态）
- 研究议程（决定下一步该验证什么）
- 显式关系下的证据累积
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
    EvidenceKind,
    Insight,
    InsightNextAction,
    InsightStatus,
    KnowledgeVersion,
    ValidationExperiment,
)

logger = logging.getLogger("life_engine.learning.store")

LEARNING_DIR_NAME = ".life_learning"
STORE_VERSION = 1
EXPERIMENTS_FILE = "validation_experiments.json"

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
        # 账本读不出来时置位：_save 会拒绝写，避免把"读失败"写成"她什么都没学过"
        self._load_failed = False
        # 这一版代码看不懂的行，原样留着，保存时一并写回去
        self._unreadable_rows: list[dict[str, Any]] = []

    # ── 加载 / 保存 ──────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        """从磁盘加载洞察快照。

        这里的谨慎是有代价换来的：_save 是整份覆写。原来的实现在读失败时
        把 _insights 设成空表并标记已加载，于是"文件读不出来"会在下一次
        写入时变成"她从来没学过任何东西"——账本被自己的恢复逻辑抹掉。

        所以现在分两种失败：
        - 整份读不出来（JSON 坏了 / IO 错）：备份原文件，置 _load_failed，
          _save 拒绝写。宁可这一轮不记录，也不能覆盖掉。
        - 单行读不出来（字段是新版本加的、或那一行确实坏了）：跳过这一行，
          但把原始 dict 原样收着，保存时写回去。她的记录不因为代码换版本而消失。
        """
        if self._loaded:
            return
        self._ensure_dirs()
        self._unreadable_rows = []
        if not self.insights_path.exists():
            self._insights = []
            self._loaded = True
            self._load_failed = False
            return
        try:
            raw = json.loads(self.insights_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            self._load_failed = True
            self._insights = []
            self._loaded = True
            backup = self._backup_unreadable_ledger()
            logger.error(
                "❌ 洞察账本读不出来，本轮不会写入以免覆盖: %s%s",
                type(exc).__name__,
                f"（原文件已备份到 {backup.name}）" if backup else "",
            )
            return

        items = raw.get("insights", []) if isinstance(raw, dict) else []
        parsed: list[Insight] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                parsed.append(Insight.from_dict(item))
            except Exception as exc:  # noqa: BLE001
                # 一行读不动不该拖垮整个账本，更不该让这一行凭空消失
                self._unreadable_rows.append(item)
                logger.warning(
                    "跳过一条读不出来的洞察（原样保留）: %s - %s",
                    item.get("insight_id", "?"),
                    type(exc).__name__,
                )
        self._insights = parsed
        self._loaded = True
        self._load_failed = False
        if self._unreadable_rows:
            logger.warning(
                f"账本里有 {len(self._unreadable_rows)} 条这一版代码读不动，已原样保留"
            )

    def _backup_unreadable_ledger(self) -> Path | None:
        """把读不出来的账本原文件留一份，便于事后人工救回。"""
        try:
            stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
            backup = self.insights_path.with_name(f"insights.broken_{stamp}.json")
            backup.write_bytes(self.insights_path.read_bytes())
            return backup
        except OSError as exc:  # noqa: BLE001
            logger.warning("备份损坏账本失败: %s", type(exc).__name__)
            return None

    def _save(self) -> None:
        """原子写入洞察快照。

        load 失败时拒绝写：整份覆写 + 空的 _insights = 抹掉她学过的一切。
        读不动的单行原样写回，不因为代码版本变化而丢。
        """
        if self._load_failed:
            logger.error("❌ 账本处于读失败状态，拒绝写入（保护已有记录）")
            raise RuntimeError("LearningInsightStoreUnavailable")
        self._ensure_dirs()
        rows: list[dict[str, Any]] = [ins.to_dict() for ins in self._insights]
        rows.extend(self._unreadable_rows)
        payload = {
            "version": STORE_VERSION,
            "updated_at": _now_iso(),
            "insights": rows,
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
        """添加新洞察；相似文本不会被代码静默合并。"""
        self.load()
        for existing in self._insights:
            if existing.insight_id != insight.insight_id:
                continue
            if existing.to_dict() != insight.to_dict():
                raise ValueError(f"InsightIdentityConflict:{insight.insight_id}")
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

    # ── 重新审视（螺旋上升）─────────────────────────────────

    def reconsider_insight(
        self,
        insight_id: str,
        *,
        reason: str = "",
    ) -> bool:
        """把一条已经下过判断的洞察重新放回审计队列。

        学习是螺旋上升的：validated 不等于永远为真。当她发现一条曾经
        验证过的认知不再符合现在的自己（或者反例累积到让她怀疑），
        这条认知应该能重新进入审视，而不是被永久钉死。

        做什么：
        - status → candidate，next_action → await_review（重新排队）
        - 记录 revision_note（为什么要重新想）
        - 保留全部证据、审计历史、knowledge_versions：
          这是重新审视，不是删除。旧的知识文档版本仍是不可变的历史。
        - 写 append-only 审计日志

        不做什么：
        - 不清除 confidence，也不自动降低它。重新想一遍不等于已经错了，
          结论由后面的审计和她自己给出。
        - 不删除任何已发布的 knowledge/vN.md。历史保持原样，
          修正体现在下一个版本里。

        只由她主动发起（或她授权的路径）。系统自己不会替她推翻任何结论。
        """
        insight = self.get_insight(insight_id)
        if insight is None:
            return False

        old_status = insight.status
        old_action = insight.next_action

        insight.status = InsightStatus.CANDIDATE.value
        insight.next_action = InsightNextAction.AWAIT_REVIEW.value
        if reason:
            insight.revision_note = reason
        # 单独记时间：revision_note 也被去重合并写（"已合并入 …"），
        # 用它判断"是否重新想过"会把 47 条归档合并误当成重新审视。
        insight.reconsidered_at = _now_iso()
        insight.reconsider_count += 1
        insight.updated_at = _now_iso()

        self._append_audit({
            "action": "insight_reconsidered",
            "insight_id": insight_id,
            "from_status": old_status,
            "from_next_action": old_action,
            "to_status": insight.status,
            "next_action": insight.next_action,
            "reason": reason,
            "evidence_count": len(insight.evidence),
            "negative_count": insight.negative_evidence_count,
            "knowledge_versions": list(insight.knowledge_versions),
            "reconsider_count": insight.reconsider_count,
        })
        self._save()
        return True

    def record_knowledge_version(self, insight_id: str, version: int) -> bool:
        """记录一条洞察被写进了哪个版本的知识文档。

        用途：重新审视 → 再次验证之后，压缩器知道这条认知在旧版本里
        已经有对应表述，应该更新它，而不是当成全新认知重复写一遍。
        """
        insight = self.get_insight(insight_id)
        if insight is None:
            return False
        if version not in insight.knowledge_versions:
            insight.knowledge_versions.append(version)
            insight.updated_at = _now_iso()
            self._save()
        return True

    def list_reconsidered(self) -> list[Insight]:
        """列出她主动拿回来重新想过的洞察。

        这是"反例备忘"的真实来源之一：不只是被审计否定的洞察，
        还有曾经写进自我认知、后来她自己觉得需要改的那些。

        判据是 reconsidered_at，而不是 revision_note——后者同时被
        去重合并复用，会把归档的合并记录一起算进来。
        """
        self.load()
        return [ins for ins in self._insights if ins.reconsidered_at]

    def reconcile_knowledge_versions(self) -> int:
        """按 manifest 回填 knowledge_versions。

        为什么需要：v1 是在 record_knowledge_version 存在之前写的，
        里面那两条洞察的 knowledge_versions 是空的。如果她现在重新
        审视其中一条，压缩器就不知道这条认知已经在知识文档里有表述，
        修正会丢。

        只补不删，幂等；只搬 manifest 里已有的事实，不做任何判断。
        返回补了多少条。
        """
        self.load()
        manifest = self.load_knowledge_manifest()
        versions = manifest.get("versions")
        if not isinstance(versions, list):
            return 0

        fixed = 0
        for entry in versions:
            if not isinstance(entry, dict) or not entry.get("promoted"):
                continue
            try:
                version = int(entry.get("version", 0))
            except (TypeError, ValueError):
                continue
            if version <= 0:
                continue
            ids = entry.get("insight_ids")
            if not isinstance(ids, list):
                continue
            for raw_id in ids:
                insight = self.get_insight(str(raw_id))
                if insight is None:
                    continue
                if version not in insight.knowledge_versions:
                    insight.knowledge_versions.append(version)
                    insight.knowledge_versions.sort()
                    fixed += 1

        if fixed:
            self._save()
            logger.info(f"📚 回填 knowledge_versions: {fixed} 条")
        return fixed

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
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError) as exc:
            raise RuntimeError("LearningKnowledgeManifestUnavailable") from exc
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("versions", []), list
        ):
            raise RuntimeError("LearningKnowledgeManifestCorrupt")
        return dict(manifest)

    def save_knowledge_manifest(self, manifest: dict[str, Any]) -> None:
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("versions", []), list
        ):
            raise TypeError("learning knowledge manifest must be an object")
        self._ensure_dirs()
        manifest_path = self.knowledge_dir / "manifest.json"
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(manifest_path)

    def get_current_knowledge_path(self) -> Path:
        return self.knowledge_dir / "self_knowledge.md"

    def read_current_knowledge(self) -> str:
        path = self.get_current_knowledge_path()
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("LearningCurrentKnowledgeUnavailable") from exc

    def read_knowledge_version(self, version: int) -> str:
        """Read one immutable local candidate version without fallback."""

        identity = int(version)
        if identity <= 0:
            raise ValueError("knowledge version must be positive")
        path = self.knowledge_dir / f"v{identity}.md"
        if not path.exists():
            raise FileNotFoundError(path)
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("LearningKnowledgeVersionUnavailable") from exc

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
        if int(version) <= 0:
            raise ValueError("knowledge version must be positive")
        # 写版本文件
        version_path = self.knowledge_dir / f"v{version}.md"
        if version_path.exists():
            raise ValueError(f"KnowledgeVersionConflict:{version}")
        version_temporary = version_path.with_suffix(".md.tmp")
        version_temporary.write_text(content, encoding="utf-8")
        version_temporary.replace(version_path)

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
            current_path = self.get_current_knowledge_path()
            current_temporary = current_path.with_suffix(".md.tmp")
            current_temporary.write_text(content, encoding="utf-8")
            current_temporary.replace(current_path)
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
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("LearningStateUnavailable") from exc
        if not isinstance(state, dict):
            raise RuntimeError("LearningStateCorrupt")
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise TypeError("learning state must be an object")
        self._ensure_dirs()
        temp = self.state_path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.state_path)

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
            logger.warning("加载验证实验失败: %s", type(exc).__name__)
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
        logger.info("✅ 添加验证实验: %s", exp.experiment_id)
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
        evidence = Evidence.create(
            kind=EvidenceKind.VALIDATION_EXPERIMENT,
            description=f"预测: {exp.hypothesis}\n实际: {actual_outcome}\n结果: {result_type}",
            source_ref=experiment_id,
            supports=supports,
            context=notes,
        )

        # 添加证据到洞察
        insight = self.get_insight(exp.insight_id)
        if insight:
            insight.add_evidence(evidence)

            # 如果被否定，记录反例挑战
            # 注意：contradiction_count 已由 add_evidence 统一累加
            #（supports=False 的证据一律计数），这里不再重复 +1。
            if result_type == "contradicted":
                logger.info(
                    f"❌ 洞察被现实否定 [{insight.insight_id}] "
                    f"(contradiction_count={insight.contradiction_count})"
                )
            elif result_type == "confirmed":
                logger.info("✅ 洞察收到确认经历 [%s]", insight.insight_id)

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
