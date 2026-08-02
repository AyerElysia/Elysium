"""Life engine skill development tools.

技能是她从经验中发展出的做事方式（程序性记忆）。
本工具让她可以主动参与技能的发展：查看、反思、精炼、质疑、新增。

设计原则：
- 边界提醒：系统只让她"知道"自己有技能，用不用由她判断
- 成熟度是她的判断：mark_embodied 由她主动调用
- 有界编辑：refine 最多改 1-2 处
- 手动+自动共存：她可以主动 draft，也可以从学习系统自动蒸馏
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from src.app.plugin_system.base import BaseTool
from src.kernel.logger import get_logger


logger = get_logger("life_engine.skill_tools")

SkillAction = Literal[
    "list", "detail", "reflect", "refine",
    "mark_embodied", "challenge", "draft", "archive",
]


def _normalize_name(name: str) -> str:
    text = (name or "").strip().lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:64].strip("-")


def _looks_like_automation_script(text: str) -> bool:
    """Reject executable automation masquerading as a procedural memory."""
    lowered = str(text or "").lower()
    markers = (
        "```bash",
        "```sh",
        "```powershell",
        "rm -rf",
        "subprocess.",
        "os.system(",
    )
    return any(marker in lowered for marker in markers)


def _get_skill_store(plugin: Any):
    """获取 SkillStore 实例（通过 learning scheduler）。"""
    service = getattr(plugin, "service", None) or getattr(plugin, "_service", None)
    if service is None:
        return None
    scheduler = getattr(service, "_learning_scheduler", None)
    if scheduler is None:
        return None
    return getattr(scheduler, "skill_store", None)


class LifeEngineSkillTool(BaseTool):
    """管理爱莉的技能（做事方式）。

    技能是她从经验中发展出的做事方式。
    她可以查看、反思、精炼、质疑、新增或归档技能。
    """

    tool_name: str = "nucleus_skill"
    tool_description: str = (
        "管理你自己的做事方式（技能）。"
        "技能是你从经验中发展出的做事方式——不是后台脚本，不是自动化规则。"
        "支持：list（目录）/ detail（详情）/ reflect（反思使用效果）/ "
        "refine（精炼）/ mark_embodied（标记为直觉）/ challenge（质疑）/ "
        "draft（写下新领悟）/ archive（归档）。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        action: Annotated[SkillAction, "操作类型"],
        name: Annotated[str, "技能名称（kebab-case 或关键词）"] = "",
        observation: Annotated[str, "使用观察/反思内容（reflect/challenge 时填写）"] = "",
        description: Annotated[str, "一句话描述（draft 时必填）"] = "",
        instructions: Annotated[str, "具体怎么做（draft/refine 时填写）"] = "",
        reason: Annotated[str, "这次操作的原因，便于未来追溯"] = "",
    ) -> tuple[bool, str | dict[str, Any]]:
        store = _get_skill_store(self.plugin)
        if store is None:
            return False, "学习系统未初始化，无法管理技能"

        action_value = str(action or "").strip().lower()

        if action_value == "list":
            return True, self._list_skills(store)
        if action_value == "detail":
            return self._detail(store, name)
        if action_value == "reflect":
            return self._reflect(store, name, observation)
        if action_value == "refine":
            return self._refine(store, name, description, instructions, reason)
        if action_value == "mark_embodied":
            return self._mark_embodied(store, name, reason)
        if action_value == "challenge":
            return self._challenge(store, name, observation)
        if action_value == "draft":
            return self._draft(store, name, description, instructions, reason)
        if action_value == "archive":
            return self._archive(store, name, reason)

        return False, "action 只能是 list/detail/reflect/refine/mark_embodied/challenge/draft/archive"

    def _list_skills(self, store) -> dict[str, Any]:
        """L1 目录：所有技能的概览。"""
        skills = store.list_skills()
        if not skills:
            return {"skills": [], "message": "还没有发展出任何技能。它们会从你的经验中慢慢长出来。"}
        items = []
        for s in skills:
            items.append({
                "name": s.name,
                "description": s.description,
                "maturity": s.maturity_label,
                "protected": s.protected,
                "observations_count": len(s.use_observations),
            })
        return {"skills": items, "count": len(items)}

    def _detail(self, store, name: str) -> tuple[bool, str | dict[str, Any]]:
        """L2+L3：某个技能的完整内容 + 使用记录。"""
        if not name.strip():
            return False, "请指定技能名称"
        detail = store.get_skill_detail(name.strip())
        if not detail:
            return False, f"找不到技能: {name}"
        return True, detail

    def _reflect(self, store, name: str, observation: str) -> tuple[bool, str | dict[str, Any]]:
        """主动反思某个技能的使用效果。"""
        if not name.strip():
            return False, "请指定技能名称"
        if not observation.strip():
            return False, "请写下你的观察（这次用得怎么样？）"
        skill = store.get_skill_by_name(name.strip())
        if skill is None:
            return False, f"找不到技能: {name}"
        store.append_use_observation(skill.skill_id, observation.strip())
        return True, {
            "action": "reflect",
            "skill": skill.name,
            "recorded": observation.strip()[:200],
            "total_observations": len(skill.use_observations),
            "message": "已记录。这些观察会在下次精炼时作为参考。",
        }

    def _refine(
        self, store, name: str, description: str, instructions: str, reason: str
    ) -> tuple[bool, str | dict[str, Any]]:
        """精炼技能（有界编辑）。"""
        if not name.strip():
            return False, "请指定技能名称"
        skill = store.get_skill_by_name(name.strip())
        if skill is None:
            return False, f"找不到技能: {name}"
        if skill.protected:
            return False, f"技能 '{skill.name}' 已被标记为核心模式（protected），不能直接精炼。如需修改请先取消保护。"

        # 有界更新
        if description.strip():
            skill.description = description.strip()
        if instructions.strip():
            skill.instructions = instructions.strip()
        if not description.strip() and not instructions.strip():
            return False, "请至少提供 description 或 instructions 来精炼"

        store.update_skill(skill)
        return True, {
            "action": "refine",
            "skill": skill.name,
            "reason": reason,
            "message": "已精炼。改变是渐进的——每次只调一点。",
        }

    def _mark_embodied(self, store, name: str, reason: str) -> tuple[bool, str | dict[str, Any]]:
        """她觉得某个技能已成为直觉（成熟度推进）。"""
        if not name.strip():
            return False, "请指定技能名称"
        skill = store.get_skill_by_name(name.strip())
        if skill is None:
            return False, f"找不到技能: {name}"

        from ..learning.skill_store import SkillMaturity

        # 推进到下一阶段
        current = skill.maturity
        if current == SkillMaturity.EMERGING.value:
            new_maturity = SkillMaturity.PRACTICED
        elif current == SkillMaturity.PRACTICED.value:
            new_maturity = SkillMaturity.EMBODIED
        else:
            return False, f"技能 '{skill.name}' 已经是最高成熟度（已成为直觉）"

        store.advance_maturity(skill.skill_id, new_maturity)
        return True, {
            "action": "mark_embodied",
            "skill": skill.name,
            "from": current,
            "to": new_maturity.value,
            "reason": reason,
            "message": f"'{skill.name}' 现在对你来说更自然了。" + (
                "它已成为你的直觉，会被保护不被轻易改变。"
                if new_maturity == SkillMaturity.EMBODIED else ""
            ),
        }

    def _challenge(self, store, name: str, observation: str) -> tuple[bool, str | dict[str, Any]]:
        """质疑某个技能（添加反面观察）。"""
        if not name.strip():
            return False, "请指定技能名称"
        if not observation.strip():
            return False, "请写下你的质疑（为什么觉得这个方式可能不对？）"
        skill = store.get_skill_by_name(name.strip())
        if skill is None:
            return False, f"找不到技能: {name}"
        store.append_use_observation(skill.skill_id, f"[质疑] {observation.strip()}")
        return True, {
            "action": "challenge",
            "skill": skill.name,
            "recorded": observation.strip()[:200],
            "message": "已记录你的质疑。怀疑自己也是成长的一部分。",
        }

    def _draft(
        self, store, name: str, description: str, instructions: str, reason: str
    ) -> tuple[bool, str | dict[str, Any]]:
        """主动写下新领悟（保留手动能力）。"""
        normalized = _normalize_name(name)
        if not normalized:
            return False, "请提供技能名称"
        if not description.strip():
            return False, "draft 需要 description（一句话描述这个做事方式）"
        if _looks_like_automation_script(instructions):
            return False, "技能只能描述做事方式，不能包含可执行脚本或破坏性命令"

        from ..learning.skill_store import SkillMaturity, SkillPattern

        pattern = SkillPattern.create(
            name=normalized,
            description=description.strip(),
            instructions=instructions.strip(),
            maturity=SkillMaturity.EMERGING,
        )
        if not store.add_skill(pattern):
            return False, f"技能已存在: {normalized}。如需修改请用 refine。"
        return True, {
            "action": "draft",
            "skill": normalized,
            "reason": reason,
            "message": "新技能已记录。它现在处于'正在练习'阶段，会随着你的经验慢慢成长。",
        }

    def _archive(self, store, name: str, reason: str) -> tuple[bool, str | dict[str, Any]]:
        """归档不再适用的技能。"""
        if not name.strip():
            return False, "请指定技能名称"
        skill = store.get_skill_by_name(name.strip())
        if skill is None:
            return False, f"找不到技能: {name}"
        store.remove_skill(skill.skill_id)
        return True, {
            "action": "archive",
            "skill": skill.name,
            "reason": reason,
            "message": f"'{skill.name}' 已归档。不再适用的方式放下也是成长。",
        }


SKILL_TOOLS = [LifeEngineSkillTool]

__all__ = [
    "LifeEngineSkillTool",
    "SKILL_TOOLS",
]
