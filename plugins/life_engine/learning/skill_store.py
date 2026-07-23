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

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger("life_engine.learning.skill_store")

# 成熟度标签（用于 L1 目录展示）
_MATURITY_LABELS = {
    "emerging": "正在练习",
    "practiced": "较熟练",
    "embodied": "已成为直觉",
}

# L1 目录中每个 skill 的最大描述长度
_CATALOG_DESC_MAX = 80


class SkillMaturity(Enum):
    """技能成熟度（仿 Fitts & Posner 三阶段）。"""

    EMERGING = "emerging"      # 认知期：刚意识到，刻意练习中
    PRACTICED = "practiced"    # 联结期：用过几次，较流畅
    EMBODIED = "embodied"      # 自主期：已成为直觉/身份的一部分


@dataclass(slots=True)
class SkillPattern:
    """一条技能模式——她发展出的一种做事方式。"""

    skill_id: str
    name: str                          # kebab-case 标识符
    description: str                   # L1: 一句话（始终在 prompt 中）
    instructions: str                  # L2: 具体怎么做、边界、注意事项
    maturity: str                      # SkillMaturity value
    origin_insight_ids: list[str] = field(default_factory=list)
    use_observations: list[str] = field(default_factory=list)
    rejected_edits: list[dict[str, Any]] = field(default_factory=list)
    protected: bool = False            # 核心模式标记
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
    def from_dict(cls, data: dict[str, Any]) -> "SkillPattern":
        return cls(
            skill_id=str(data.get("skill_id", "") or _gen_id("skl")),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            instructions=str(data.get("instructions", "") or ""),
            maturity=str(data.get("maturity", "") or SkillMaturity.EMERGING.value),
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
        maturity: SkillMaturity | str = SkillMaturity.EMERGING,
        origin_insight_ids: list[str] | None = None,
        protected: bool = False,
    ) -> "SkillPattern":
        return cls(
            skill_id=_gen_id("skl"),
            name=name,
            description=description,
            instructions=instructions,
            maturity=maturity.value if isinstance(maturity, SkillMaturity) else str(maturity),
            origin_insight_ids=origin_insight_ids or [],
            protected=protected,
        )

    @property
    def maturity_label(self) -> str:
        return _MATURITY_LABELS.get(self.maturity, self.maturity)


class SkillStore:
    """技能持久化：skills.json + skills_audit.jsonl。"""

    def __init__(self, workspace: Path | str) -> None:
        from .store import LEARNING_DIR_NAME

        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / LEARNING_DIR_NAME
        self.skills_path = self.root / "skills.json"
        self.audit_log_path = self.root / "skills_audit.jsonl"
        self._skills: list[SkillPattern] = []
        self._loaded = False

    # ── 加载/保存 ─────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        if self._loaded:
            return
        self._ensure_dirs()
        if self.skills_path.exists():
            try:
                raw = json.loads(self.skills_path.read_text(encoding="utf-8"))
                self._skills = [
                    SkillPattern.from_dict(d)
                    for d in raw.get("skills", [])
                ]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(f"skills.json 解析失败，重置: {exc}")
                self._skills = []
        else:
            self._skills = []
        self._loaded = True

    def _save(self) -> None:
        self._ensure_dirs()
        payload = {
            "version": 1,
            "updated_at": _now_iso(),
            "skills": [s.to_dict() for s in self._skills],
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
        self._append_audit({
            "action": "skill_created",
            "skill_id": pattern.skill_id,
            "name": pattern.name,
            "description": pattern.description,
            "maturity": pattern.maturity,
        })
        self._save()
        return True

    def update_skill(self, pattern: SkillPattern) -> bool:
        """更新已有技能。"""
        self.load()
        for i, s in enumerate(self._skills):
            if s.skill_id == pattern.skill_id:
                self._skills[i] = pattern
                pattern.last_refined_at = _now_iso()
                self._append_audit({
                    "action": "skill_updated",
                    "skill_id": pattern.skill_id,
                    "name": pattern.name,
                })
                self._save()
                return True
        return False

    def remove_skill(self, skill_id: str) -> bool:
        """移除技能（归档用，非物理删除）。"""
        self.load()
        for i, s in enumerate(self._skills):
            if s.skill_id == skill_id:
                removed = self._skills.pop(i)
                self._append_audit({
                    "action": "skill_archived",
                    "skill_id": skill_id,
                    "name": removed.name,
                })
                self._save()
                return True
        return False

    # ── L1 目录（边界提醒注入）─────────────────────────────────

    def get_catalog_text(self, max_chars: int = 600) -> str:
        """生成 L1 技能目录文本，用于 prompt 注入。

        格式：轻量级自我意识——"你知道自己已经发展出这些做事方式"。
        只呈现 name + description + 成熟度标签，不呈现完整 instructions。
        """
        self.load()
        if not self._skills:
            return ""

        lines: list[str] = []
        for s in self._skills:
            desc = s.description[:_CATALOG_DESC_MAX]
            if len(s.description) > _CATALOG_DESC_MAX:
                desc += "…"
            lines.append(f"- {desc}（{s.maturity_label}）")

        header = "你知道自己已经发展出这些做事方式："
        text = header + "\n" + "\n".join(lines)

        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars - 1].rstrip() + "…"
        return text

    # ── L2 正文（按需加载）────────────────────────────────────

    def get_skill_detail(self, name: str) -> str:
        """获取某个技能的完整内容（L2 + L3）。"""
        skill = self.get_skill_by_name(name)
        if skill is None:
            return ""
        parts = [
            f"# {skill.name}",
            f"成熟度：{skill.maturity_label}",
            f"描述：{skill.description}",
            "",
            "## 具体方式",
            skill.instructions or "（还没有详细记录。）",
        ]
        if skill.use_observations:
            parts.append("")
            parts.append("## 使用观察")
            for obs in skill.use_observations[-5:]:
                parts.append(f"- {obs}")
        if skill.rejected_edits:
            parts.append("")
            parts.append("## 试过的弯路")
            for rej in skill.rejected_edits[-3:]:
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
        # 保留最近 20 条
        if len(skill.use_observations) > 20:
            skill.use_observations = skill.use_observations[-20:]
        self._append_audit({
            "action": "use_observation_added",
            "skill_id": skill_id,
            "observation": observation[:200],
        })
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
        skill.rejected_edits.append({
            "summary": edit_summary,
            "reason": reason,
            "timestamp": _now_iso(),
        })
        # 保留最近 10 条
        if len(skill.rejected_edits) > 10:
            skill.rejected_edits = skill.rejected_edits[-10:]
        self._append_audit({
            "action": "rejected_edit_recorded",
            "skill_id": skill_id,
            "summary": edit_summary[:200],
            "reason": reason[:200],
        })
        self._save()
        return True

    # ── 成熟度推进（由她主动判断）────────────────────────────

    def advance_maturity(self, skill_id: str, new_maturity: SkillMaturity | str) -> bool:
        """推进技能成熟度。只由她主动调用（工具）或反思时提议。"""
        skill = self.get_skill(skill_id)
        if skill is None:
            return False
        target = new_maturity.value if isinstance(new_maturity, SkillMaturity) else str(new_maturity)
        old = skill.maturity
        skill.maturity = target
        skill.last_refined_at = _now_iso()
        # embodied 技能自动标记为 protected
        if target == SkillMaturity.EMBODIED.value:
            skill.protected = True
        self._append_audit({
            "action": "maturity_advanced",
            "skill_id": skill_id,
            "from": old,
            "to": target,
        })
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
            s for s in self._skills
            if not s.protected or s.maturity != SkillMaturity.EMBODIED.value
        ]


# ── 工具函数 ─────────────────────────────────────────────────


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
