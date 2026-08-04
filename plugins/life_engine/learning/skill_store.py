"""SkillStore：技能模式存储。

技能是她从经验中发展出的做事方式（程序性记忆）。
系统只负责让她"知道自己有这个技能"（边界提醒），
用不用、什么时候用，完全由她在推理中自主判断。

设计原则：
- 渐进式加载：L1 目录（always-on）→ L2 正文（按需）→ L3 经验（反思时）
- 成熟度是她的判断，不由计数器自动推进
- 有界编辑 + 拒绝缓存（SkillOpt 结构纪律）
- protected 标记保护核心模式不被快更新覆盖
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from src.kernel.storage import canonical_json

from .projection import project_learning_text

logger = logging.getLogger("life_engine.learning.skill_store")

# 成熟度标签（用于 L1 目录展示）
_MATURITY_LABELS = {
    "emerging": "正在练习",
    "practiced": "较熟练",
    "embodied": "已成为直觉",
}

# L1 目录中每个 skill 的最大描述长度


class SkillMaturity(Enum):
    """技能成熟度（仿 Fitts & Posner 三阶段）。"""

    EMERGING = "emerging"  # 认知期：刚意识到，刻意练习中
    PRACTICED = "practiced"  # 联结期：用过几次，较流畅
    EMBODIED = "embodied"  # 自主期：已成为直觉/身份的一部分


@dataclass(slots=True)
class SkillPattern:
    """一条技能模式——她发展出的一种做事方式。"""

    skill_id: str
    name: str  # kebab-case 标识符
    description: str  # L1: 一句话（始终在 prompt 中）
    instructions: str  # L2: 具体怎么做、边界、注意事项
    maturity: str  # SkillMaturity value
    origin_insight_ids: list[str] = field(default_factory=list)
    use_observations: list[str] = field(default_factory=list)
    rejected_edits: list[dict[str, Any]] = field(default_factory=list)
    protected: bool = False  # 核心模式标记
    created_at: str = ""
    last_refined_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.last_refined_at:
            self.last_refined_at = self.created_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillPattern:
        return cls(
            skill_id=str(data.get("skill_id", "") or _gen_id("skl")),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            instructions=str(data.get("instructions", "") or ""),
            maturity=str(data.get("maturity", "") or ""),
            origin_insight_ids=list(data.get("origin_insight_ids", []) or []),
            use_observations=list(data.get("use_observations", []) or []),
            rejected_edits=list(data.get("rejected_edits", []) or []),
            protected=bool(data.get("protected", False)),
            created_at=str(data.get("created_at", "") or _now_iso()),
            last_refined_at=str(data.get("last_refined_at", "") or ""),
        )

    @classmethod
    def create(
        cls,
        *,
        name: str,
        description: str,
        instructions: str = "",
        maturity: SkillMaturity | str = "",
        origin_insight_ids: list[str] | None = None,
        protected: bool = False,
    ) -> SkillPattern:
        return cls(
            skill_id=_gen_id("skl"),
            name=name,
            description=description,
            instructions=instructions,
            maturity=maturity.value
            if isinstance(maturity, SkillMaturity)
            else str(maturity),
            origin_insight_ids=origin_insight_ids or [],
            protected=protected,
        )

    @property
    def maturity_label(self) -> str:
        return _MATURITY_LABELS.get(self.maturity, self.maturity)


SkillCandidateStatus = Literal["open", "accepted", "rejected"]
SkillDecisionKind = Literal["accepted", "rejected", "kept_open"]


class SkillCandidateConflict(RuntimeError):
    """Raised when candidate/decision identity is reused with different bytes."""


@dataclass(slots=True)
class SkillCandidate:
    """A traceable skill suggestion outside the current skill authority."""

    candidate_id: str
    candidate_revision: int
    candidate_occurrence_id: str
    candidate_sha256: str
    subject_revision: str
    source_occurrence_id: str
    target_skill_id: str
    proposed_skill_id: str
    name: str
    description: str
    instructions: str
    insight_ids: list[str]
    source_event_ids: list[str]
    gate_recommended: bool
    occurred_at: str
    status: SkillCandidateStatus = "open"
    decisions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        subject_revision: str,
        source_occurrence_id: str,
        target_skill_id: str,
        proposed_skill_id: str,
        name: str,
        description: str,
        instructions: str,
        insight_ids: list[str],
        source_event_ids: list[str],
        gate_recommended: bool,
        candidate_id: str = "",
        candidate_revision: int = 1,
        candidate_occurrence_id: str = "",
        occurred_at: str = "",
    ) -> SkillCandidate:
        identity = candidate_id or f"skill_candidate_{uuid4().hex}"
        occurrence = candidate_occurrence_id or (
            f"skill_candidate:{identity}:{candidate_revision}:{uuid4().hex}"
        )
        revision = str(subject_revision).strip().lower()
        if len(revision) != 64 or any(
            character not in "0123456789abcdef" for character in revision
        ):
            raise ValueError("subject_revision must be a 64-hex digest")
        semantic = {
            "candidate_id": identity,
            "candidate_revision": int(candidate_revision),
            "subject_revision": revision,
            "source_occurrence_id": str(source_occurrence_id),
            "target_skill_id": str(target_skill_id),
            "proposed_skill_id": str(proposed_skill_id),
            "name": str(name),
            "description": str(description),
            "instructions": str(instructions),
            "insight_ids": [str(item) for item in insight_ids],
            "source_event_ids": [str(item) for item in source_event_ids],
            "gate_recommended": bool(gate_recommended),
        }
        return cls(
            **semantic,
            candidate_occurrence_id=occurrence,
            candidate_sha256=hashlib.sha256(
                canonical_json(semantic).encode("utf-8")
            ).hexdigest(),
            occurred_at=occurred_at or _now_iso(),
        )

    def semantic_payload(self) -> dict[str, Any]:
        """Return the exact bytes covered by ``candidate_sha256``."""

        return {
            "candidate_id": self.candidate_id,
            "candidate_revision": self.candidate_revision,
            "subject_revision": self.subject_revision,
            "source_occurrence_id": self.source_occurrence_id,
            "target_skill_id": self.target_skill_id,
            "proposed_skill_id": self.proposed_skill_id,
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "insight_ids": list(self.insight_ids),
            "source_event_ids": list(self.source_event_ids),
            "gate_recommended": self.gate_recommended,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SkillCandidate:
        insight_ids = value.get("insight_ids", [])
        source_event_ids = value.get("source_event_ids", [])
        decisions = value.get("decisions", [])
        if not isinstance(insight_ids, list) or not isinstance(source_event_ids, list):
            raise TypeError("skill candidate source identities must be lists")
        if not isinstance(decisions, list) or not all(
            isinstance(item, dict) for item in decisions
        ):
            raise TypeError("skill candidate decisions must be objects")
        candidate = cls(
            candidate_id=str(value.get("candidate_id", "")),
            candidate_revision=int(value.get("candidate_revision", 0) or 0),
            candidate_occurrence_id=str(value.get("candidate_occurrence_id", "")),
            candidate_sha256=str(value.get("candidate_sha256", "")),
            subject_revision=str(value.get("subject_revision", "")),
            source_occurrence_id=str(value.get("source_occurrence_id", "")),
            target_skill_id=str(value.get("target_skill_id", "")),
            proposed_skill_id=str(value.get("proposed_skill_id", "")),
            name=str(value.get("name", "")),
            description=str(value.get("description", "")),
            instructions=str(value.get("instructions", "")),
            insight_ids=[str(item) for item in insight_ids],
            source_event_ids=[str(item) for item in source_event_ids],
            gate_recommended=bool(value.get("gate_recommended", False)),
            occurred_at=str(value.get("occurred_at", "")),
            status=str(value.get("status", "open")),  # type: ignore[arg-type]
            decisions=[dict(item) for item in decisions],
        )
        if candidate.status not in {"open", "accepted", "rejected"}:
            raise ValueError("invalid skill candidate status")
        digest = hashlib.sha256(
            canonical_json(candidate.semantic_payload()).encode("utf-8")
        ).hexdigest()
        if digest != candidate.candidate_sha256:
            raise ValueError("skill candidate hash mismatch")
        seen_occurrences: dict[str, str] = {}
        for decision in candidate.decisions:
            occurrence = str(decision.get("decision_occurrence_id", "")).strip()
            decision_sha256 = str(decision.get("decision_sha256", "")).strip()
            if not occurrence or len(decision_sha256) != 64:
                raise ValueError("skill candidate decision identity is incomplete")
            material = {
                key: decision.get(key)
                for key in (
                    "candidate_id",
                    "candidate_revision",
                    "candidate_sha256",
                    "decision_occurrence_id",
                    "decision_kind",
                    "actor_consciousness_instance_id",
                    "expected_subject_revision",
                    "reason",
                    "accepted",
                )
            }
            computed = hashlib.sha256(
                canonical_json(material).encode("utf-8")
            ).hexdigest()
            if computed != decision_sha256:
                raise ValueError("skill candidate decision hash mismatch")
            previous = seen_occurrences.setdefault(occurrence, decision_sha256)
            if previous != decision_sha256:
                raise ValueError("skill candidate decision occurrence conflicts")
        return candidate


class SkillStore:
    """技能持久化：skills.json + skills_audit.jsonl。"""

    def __init__(self, workspace: Path | str) -> None:
        from .store import LEARNING_DIR_NAME

        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / LEARNING_DIR_NAME
        self.skills_path = self.root / "skills.json"
        self.audit_log_path = self.root / "skills_audit.jsonl"
        self._skills: list[SkillPattern] = []
        self._candidates: list[SkillCandidate] = []
        self._loaded = False
        # 读不出来时置位：_save 会拒绝写，避免把"读失败"写成"她没有任何技能"
        self._load_failed = False
        # 这一版代码看不懂的行，原样留着，保存时一并写回去
        self._unreadable_rows: list[dict[str, Any]] = []
        self._unreadable_candidate_rows: list[dict[str, Any]] = []
        self._last_catalog_projection: dict[str, Any] = {}

    # ── 加载/保存 ─────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        """从磁盘加载技能。

        和洞察账本同样的道理：_save 是整份覆写。原来的实现在解析失败时
        直接"重置"成空表并标记已加载，于是一次读失败会在下一次写入时
        变成"她从来没练出过任何做事方式"。技能是她的程序性记忆，
        丢了就是把练过的手感抹掉，所以这里分两种失败处理：

        - 整份读不出来：备份原文件，置 _load_failed，_save 拒绝写。
        - 单行读不出来：跳过那一行，但原始 dict 收着，保存时写回去。
        """
        if self._loaded:
            return
        self._ensure_dirs()
        self._unreadable_rows = []
        self._unreadable_candidate_rows = []
        if not self.skills_path.exists():
            self._skills = []
            self._candidates = []
            self._loaded = True
            self._load_failed = False
            return
        try:
            raw = json.loads(self.skills_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            self._load_failed = True
            self._skills = []
            self._loaded = True
            backup = self._backup_unreadable_skills()
            logger.error(
                "❌ 技能档读不出来，本轮不会写入以免覆盖: %s%s",
                type(exc).__name__,
                f"（原文件已备份到 {backup.name}）" if backup else "",
            )
            return

        if not isinstance(raw, dict):
            items: Any = None
            candidate_items: Any = None
        else:
            items = raw.get("skills", [])
            candidate_items = raw.get("candidates", [])
        if not isinstance(items, list) or not isinstance(candidate_items, list):
            self._load_failed = True
            self._skills = []
            self._candidates = []
            self._loaded = True
            backup = self._backup_unreadable_skills()
            logger.error(
                "Skill ledger structure is invalid; writes are disabled"
                + (f" (backup: {backup.name})" if backup else "")
            )
            return
        parsed: list[SkillPattern] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                parsed.append(SkillPattern.from_dict(item))
            except Exception as exc:  # noqa: BLE001
                # 一行读不动不该让这一行凭空消失
                self._unreadable_rows.append(item)
                logger.warning(
                    "跳过一条读不出来的技能（原样保留）: %s - %s",
                    item.get("skill_id", "?"),
                    type(exc).__name__,
                )
        self._skills = parsed
        parsed_candidates: list[SkillCandidate] = []
        for item in candidate_items:
            if not isinstance(item, dict):
                continue
            try:
                parsed_candidates.append(SkillCandidate.from_dict(item))
            except Exception as exc:  # noqa: BLE001 - preserve unknown rows
                self._unreadable_candidate_rows.append(item)
                logger.warning(
                    "Skipping one unreadable skill candidate while preserving it: "
                    "%s - %s",
                    item.get("candidate_id", "?"),
                    exc,
                )
        self._candidates = parsed_candidates
        self._loaded = True
        self._load_failed = False
        if self._unreadable_rows:
            logger.warning(
                f"技能档里有 {len(self._unreadable_rows)} 条这一版代码读不动，已原样保留"
            )

    def _backup_unreadable_skills(self) -> Path | None:
        """把读不出来的技能档留一份，便于事后人工救回。"""
        try:
            stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d_%H%M%S")
            backup = self.skills_path.with_name(f"skills.broken_{stamp}.json")
            backup.write_bytes(self.skills_path.read_bytes())
            return backup
        except OSError as exc:  # noqa: BLE001
            logger.warning("备份损坏技能档失败: %s", type(exc).__name__)
            return None

    def _save(self) -> None:
        """原子写入技能档。

        load 失败时拒绝写：整份覆写 + 空的 _skills = 抹掉她练过的一切。
        """
        if self._load_failed:
            logger.error("❌ 技能档处于读失败状态，拒绝写入（保护已有记录）")
            raise RuntimeError("LearningSkillStoreUnavailable")
        self._ensure_dirs()
        rows: list[dict[str, Any]] = [s.to_dict() for s in self._skills]
        rows.extend(self._unreadable_rows)
        candidate_rows = [candidate.to_dict() for candidate in self._candidates]
        candidate_rows.extend(self._unreadable_candidate_rows)
        payload = {
            "version": 1,
            "updated_at": _now_iso(),
            "skills": rows,
            "candidates": candidate_rows,
        }
        temp = self.skills_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.skills_path)

    def _append_audit(self, event: dict[str, Any]) -> None:
        self._ensure_dirs()
        event.setdefault("audit_event_id", f"sae_{uuid4().hex[:12]}")
        event.setdefault("timestamp", _now_iso())
        with self.audit_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ── CRUD ─────────────────────────────────────────────────

    def list_skills(self) -> list[SkillPattern]:
        self.load()
        return list(self._skills)

    def get_skill(self, skill_id: str) -> SkillPattern | None:
        self.load()
        for s in self._skills:
            if s.skill_id == skill_id:
                return s
        return None

    def get_skill_by_name(self, name: str) -> SkillPattern | None:
        self.load()
        normalized = name.strip().lower()
        for s in self._skills:
            if s.name == normalized:
                return s
        return None

    def add_skill(self, pattern: SkillPattern) -> bool:
        """添加新技能（同 name 去重）。"""
        self.load()
        if self.get_skill_by_name(pattern.name) is not None:
            logger.info(f"技能已存在: {pattern.name}")
            return False
        self._skills.append(pattern)
        self._append_audit(
            {
                "action": "skill_created",
                "skill_id": pattern.skill_id,
                "name": pattern.name,
                "description": pattern.description,
                "maturity": pattern.maturity,
            }
        )
        self._save()
        return True

    def update_skill(self, pattern: SkillPattern) -> bool:
        """更新已有技能。"""
        self.load()
        for i, s in enumerate(self._skills):
            if s.skill_id == pattern.skill_id:
                self._skills[i] = pattern
                pattern.last_refined_at = _now_iso()
                self._append_audit(
                    {
                        "action": "skill_updated",
                        "skill_id": pattern.skill_id,
                        "name": pattern.name,
                    }
                )
                self._save()
                return True
        return False

    def remove_skill(self, skill_id: str) -> bool:
        """移除技能（归档用，非物理删除）。"""
        self.load()
        for i, s in enumerate(self._skills):
            if s.skill_id == skill_id:
                removed = self._skills.pop(i)
                self._append_audit(
                    {
                        "action": "skill_archived",
                        "skill_id": skill_id,
                        "name": removed.name,
                    }
                )
                self._save()
                return True
        return False

    # ── 主体所有的技能候选决策 ─────────────────────────────────────────────

    def list_candidates(
        self,
        *,
        status: SkillCandidateStatus | None = None,
    ) -> list[SkillCandidate]:
        """Return durable skill candidates without treating them as skills."""

        self.load()
        return [
            SkillCandidate.from_dict(candidate.to_dict())
            for candidate in self._candidates
            if status is None or candidate.status == status
        ]

    def get_candidate(self, candidate_id: str) -> SkillCandidate | None:
        """Resolve one current candidate projection by stable identity."""

        self.load()
        identity = str(candidate_id or "").strip()
        for candidate in self._candidates:
            if candidate.candidate_id == identity:
                return SkillCandidate.from_dict(candidate.to_dict())
        return None

    def append_candidate(self, candidate: SkillCandidate) -> bool:
        """Append a skill suggestion without applying or archiving anything."""

        self.load()
        candidate = SkillCandidate.from_dict(candidate.to_dict())
        for existing in self._candidates:
            if existing.candidate_id != candidate.candidate_id:
                continue
            if (
                existing.candidate_revision == candidate.candidate_revision
                and existing.candidate_sha256 == candidate.candidate_sha256
                and existing.candidate_occurrence_id
                == candidate.candidate_occurrence_id
            ):
                return False
            raise SkillCandidateConflict(candidate.candidate_id)
        if candidate.target_skill_id:
            if self.get_skill(candidate.target_skill_id) is None:
                raise ValueError("skill candidate target does not exist")
        elif not candidate.proposed_skill_id:
            raise ValueError("new skill candidate requires proposed_skill_id")
        self._candidates.append(candidate)
        self._append_audit(
            {
                "audit_event_id": candidate.candidate_occurrence_id,
                "timestamp": candidate.occurred_at,
                "action": "skill_candidate_proposed",
                "candidate": candidate.to_dict(),
            }
        )
        self._save()
        return True

    def record_candidate_decision(
        self,
        *,
        candidate_id: str,
        candidate_revision: int,
        candidate_sha256: str,
        decision_occurrence_id: str,
        decision_kind: SkillDecisionKind,
        actor_consciousness_instance_id: str,
        expected_subject_revision: str,
        reason: str,
        accepted_name: str = "",
        accepted_description: str = "",
        accepted_instructions: str = "",
        occurred_at: str = "",
    ) -> list[str]:
        """Commit one explicit skill decision and return its source insight ids.

        Presence and the current unified subject revision are validated by the
        service-owned authority facade before this synchronous projection
        mutation is entered.
        """

        self.load()
        identity = str(candidate_id or "").strip()
        actor = str(actor_consciousness_instance_id or "").strip()
        occurrence = str(decision_occurrence_id or "").strip()
        if decision_kind not in {"accepted", "rejected", "kept_open"}:
            raise ValueError("invalid skill decision kind")
        if not actor or not occurrence or not str(reason or "").strip():
            raise ValueError("skill decision requires actor, occurrence and reason")
        candidate = next(
            (item for item in self._candidates if item.candidate_id == identity),
            None,
        )
        if candidate is None:
            raise KeyError(identity)
        if candidate.candidate_revision != int(
            candidate_revision
        ) or candidate.candidate_sha256 != str(candidate_sha256):
            raise SkillCandidateConflict(identity)
        expected_revision = str(expected_subject_revision).strip().lower()
        if expected_revision != candidate.subject_revision:
            raise SkillCandidateConflict("stale subject revision")

        accepted = {
            "name": str(accepted_name or candidate.name).strip(),
            "description": str(accepted_description or candidate.description).strip(),
            "instructions": str(
                accepted_instructions or candidate.instructions
            ).strip(),
        }
        if decision_kind != "accepted" and any(
            (accepted_name, accepted_description, accepted_instructions)
        ):
            raise ValueError("non-accepting decision cannot carry rewritten content")
        if decision_kind == "accepted" and not all(accepted.values()):
            raise ValueError("accepted skill content must be complete")
        decision_material = {
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "decision_occurrence_id": occurrence,
            "decision_kind": decision_kind,
            "actor_consciousness_instance_id": actor,
            "expected_subject_revision": expected_revision,
            "reason": str(reason).strip(),
            "accepted": accepted if decision_kind == "accepted" else {},
        }
        decision_sha256 = hashlib.sha256(
            canonical_json(decision_material).encode("utf-8")
        ).hexdigest()
        for decision in candidate.decisions:
            if str(decision.get("decision_occurrence_id", "")) != occurrence:
                continue
            if str(decision.get("decision_sha256", "")) != decision_sha256:
                raise SkillCandidateConflict("decision occurrence payload changed")
            return list(candidate.insight_ids)
        if candidate.status != "open":
            raise SkillCandidateConflict(
                f"skill candidate is already {candidate.status}"
            )

        if decision_kind == "accepted":
            target = (
                self.get_skill(candidate.target_skill_id)
                if candidate.target_skill_id
                else None
            )
            if candidate.target_skill_id and target is None:
                raise SkillCandidateConflict("target skill disappeared")
            if target is None:
                if self.get_skill(candidate.proposed_skill_id) is not None:
                    raise SkillCandidateConflict("proposed skill id already exists")
                if self.get_skill_by_name(accepted["name"]) is not None:
                    raise SkillCandidateConflict("accepted skill name already exists")
                target = SkillPattern(
                    skill_id=candidate.proposed_skill_id,
                    name=accepted["name"],
                    description=accepted["description"],
                    instructions=accepted["instructions"],
                    # The active consciousness accepted the content, but did not
                    # make a maturity judgment. Code must not invent one.
                    maturity="",
                    origin_insight_ids=list(candidate.insight_ids),
                )
                self._skills.append(target)
            else:
                target.name = accepted["name"]
                target.description = accepted["description"]
                target.instructions = accepted["instructions"]
                target.last_refined_at = _now_iso()
                for insight_id in candidate.insight_ids:
                    if insight_id not in target.origin_insight_ids:
                        target.origin_insight_ids.append(insight_id)
            candidate.status = "accepted"
        elif decision_kind == "rejected":
            candidate.status = "rejected"

        decision = {
            **decision_material,
            "decision_sha256": decision_sha256,
            "occurred_at": occurred_at or _now_iso(),
        }
        candidate.decisions.append(decision)
        self._append_audit(
            {
                "audit_event_id": occurrence,
                "timestamp": decision["occurred_at"],
                "action": "skill_candidate_decision",
                **decision_material,
                "decision_sha256": decision_sha256,
                "resulting_status": candidate.status,
            }
        )
        self._save()
        return list(candidate.insight_ids)

    # ── L1 目录（边界提醒注入）─────────────────────────────────

    def get_catalog_text(self, max_chars: int = 0) -> str:
        """生成 L1 技能目录文本，用于 prompt 注入。

        格式：轻量级自我意识——"你知道自己已经发展出这些做事方式"。
        只呈现 name + description + 成熟度标签，不呈现完整 instructions。
        """
        self.load()
        if not self._skills:
            return ""

        lines: list[str] = []
        for s in self._skills:
            maturity = f"（账本标记：{s.maturity_label}）" if s.maturity else ""
            lines.append(f"- {s.description}{maturity}")

        header = (
            "以下来自程序性学习账本，不是 SOUL/USER/MEMORY 主体权威，也不证明"
            "能力或成熟度；是否采用由当前意识决定："
        )
        text = header + "\n" + "\n".join(lines)

        projection = project_learning_text(
            text,
            max_bytes=max_chars,
            projection_kind="learning_skill_catalog",
        )
        self._last_catalog_projection = projection.stats()
        return projection.text

    def catalog_projection_health(self) -> dict[str, Any]:
        """Return content-free trace data for the latest catalog projection."""

        return dict(self._last_catalog_projection)

    # ── L2 正文（按需加载）────────────────────────────────────

    def get_skill_detail(self, name: str) -> str:
        """获取某个技能的完整内容（L2 + L3）。"""
        skill = self.get_skill_by_name(name)
        if skill is None:
            return ""
        parts = [
            f"# {skill.name}",
            "来源：程序性学习账本（非主体权威，可质疑、可不用）",
        ]
        if skill.maturity:
            parts.append(f"主体保留的成熟度标记：{skill.maturity_label}")
        parts.extend(
            [
                f"描述：{skill.description}",
                "",
                "## 具体方式",
                skill.instructions or "（还没有详细记录。）",
            ]
        )
        if skill.use_observations:
            parts.append("")
            parts.append("## 使用观察")
            for obs in skill.use_observations:
                parts.append(f"- {obs}")
        if skill.rejected_edits:
            parts.append("")
            parts.append("## 试过的弯路")
            for rej in skill.rejected_edits:
                summary = rej.get("summary", "")
                reason = rej.get("reason", "")
                parts.append(f"- {summary}（{reason}）")
        return "\n".join(parts)

    # ── 使用观察（L3 经验）────────────────────────────────────

    def append_use_observation(self, skill_id: str, observation: str) -> bool:
        """记录一次使用观察（事实记录，不自动改变成熟度）。"""
        skill = self.get_skill(skill_id)
        if skill is None:
            return False
        stamped = f"[{_now_iso()[:16]}] {observation}"
        skill.use_observations.append(stamped)
        self._append_audit(
            {
                "action": "use_observation_added",
                "skill_id": skill_id,
                "observation": observation,
            }
        )
        self._save()
        return True

    # ── 拒绝缓存（SkillOpt 纪律）─────────────────────────────

    def append_rejected_edit(
        self,
        skill_id: str,
        edit_summary: str,
        reason: str,
    ) -> bool:
        """记录被拒绝的编辑方向（试过的弯路不重复）。"""
        skill = self.get_skill(skill_id)
        if skill is None:
            return False
        skill.rejected_edits.append(
            {
                "summary": edit_summary,
                "reason": reason,
                "timestamp": _now_iso(),
            }
        )
        self._append_audit(
            {
                "action": "rejected_edit_recorded",
                "skill_id": skill_id,
                "summary": edit_summary,
                "reason": reason,
            }
        )
        self._save()
        return True

    # ── 成熟度推进（由她主动判断）────────────────────────────

    def advance_maturity(
        self, skill_id: str, new_maturity: SkillMaturity | str
    ) -> bool:
        """推进技能成熟度。只由她主动调用（工具）或反思时提议。"""
        skill = self.get_skill(skill_id)
        if skill is None:
            return False
        target = (
            new_maturity.value
            if isinstance(new_maturity, SkillMaturity)
            else str(new_maturity)
        )
        old = skill.maturity
        skill.maturity = target
        skill.last_refined_at = _now_iso()
        # embodied 技能自动标记为 protected
        if target == SkillMaturity.EMBODIED.value:
            skill.protected = True
        self._append_audit(
            {
                "action": "maturity_advanced",
                "skill_id": skill_id,
                "from": old,
                "to": target,
            }
        )
        self._save()
        return True

    # ── 统计 ─────────────────────────────────────────────────

    def count(self) -> int:
        self.load()
        return len(self._skills)

    def list_for_distillation(self) -> list[SkillPattern]:
        """列出可被蒸馏精炼的技能（非 protected 或 emerging/practiced）。"""
        self.load()
        return [
            s
            for s in self._skills
            if not s.protected or s.maturity != SkillMaturity.EMBODIED.value
        ]


# ── 工具函数 ─────────────────────────────────────────────────


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()
