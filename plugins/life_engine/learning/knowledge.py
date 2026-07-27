"""SelfKnowledge：慢环——自我认知压缩。

类比 VibeGamer 的 SkillOpt + best_skill.md。
核心思想：把 validated 洞察压缩成一份活着的"自我认知文档"，注入日常 prompt。

触发条件：
- validated 洞察积累到 N 条（默认 5）
- 或距上次压缩超过 M 小时（默认 48h）

压缩流程（借鉴 SkillOpt 的 minibatch 反思）：
1. Harvest：收集 validated 洞察 + 近期 rejected 反例
2. 有界编辑：对当前 self_knowledge.md 做最多 K 处修改
3. Selection Gate：新版本必须严格优于旧版本才 promote
4. 版本化：保存 vN.md，更新 manifest
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json_repair

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import LLMPayload, ROLE, Text

from .models import Insight, InsightNextAction, InsightStatus
from .prompts import (
    KNOWLEDGE_COMPRESS_SYSTEM,
    KNOWLEDGE_COMPRESS_USER,
    SELECTION_GATE_SYSTEM,
    SELECTION_GATE_USER,
    format_insights_for_compression,
    format_reconsidered_for_compression,
)
from .store import InsightStore

logger = logging.getLogger("life_engine.learning.knowledge")

# 默认参数
_DEFAULT_TRIGGER_COUNT = 5       # 触发压缩的 validated 数量
_DEFAULT_INTERVAL_HOURS = 48.0   # 压缩最小间隔
_DEFAULT_MAX_EDITS = 4           # 每次最多编辑数
_INITIAL_KNOWLEDGE = """\
# 自我认知

这份文档是我对自己的理解——基于我验证过的经历，而非猜测。

## 社交模式

（还没有验证过的社交认知。我正在积累。）

## 行为边界

（还在探索自己的边界。）

## 情感模式

（还在理解自己的情感反应。）

## 成长方向

- 正在学习：通过经历来认识自己

## 反例备忘

（暂无。）
"""


class SelfKnowledgeCompressor:
    """慢环：将 validated 洞察压缩为版本化自我认知文档。"""

    def __init__(
        self,
        *,
        store: InsightStore,
        workspace_path: str | Path,
        model_task_name: str = "life",
        timeout_seconds: float = 90.0,
        trigger_count: int = _DEFAULT_TRIGGER_COUNT,
        interval_hours: float = _DEFAULT_INTERVAL_HOURS,
        max_edits: int = _DEFAULT_MAX_EDITS,
    ) -> None:
        self._store = store
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = str(model_task_name or "life").strip() or "life"
        self._timeout = max(30.0, float(timeout_seconds or 90.0))
        self._trigger_count = max(2, int(trigger_count or _DEFAULT_TRIGGER_COUNT))
        self._interval_hours = max(6.0, float(interval_hours or _DEFAULT_INTERVAL_HOURS))
        self._max_edits = max(1, int(max_edits or _DEFAULT_MAX_EDITS))
        self._lock = asyncio.Lock()

    def should_compress(self) -> bool:
        """判断是否应该触发压缩。"""
        promotable = self._store.list_for_compression()
        if len(promotable) >= self._trigger_count:
            return True

        # 检查时间间隔
        state = self._store.load_state()
        last_compress = state.get("last_compress_at", "")
        if not last_compress and promotable:
            return True
        if last_compress:
            try:
                last_dt = datetime.fromisoformat(last_compress)
                now = datetime.now(timezone.utc).astimezone()
                hours_elapsed = (now - last_dt).total_seconds() / 3600.0
                if hours_elapsed >= self._interval_hours and promotable:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    async def run_compression(self) -> bool:
        """执行一次压缩周期。返回是否成功 promote 新版本。"""
        async with self._lock:
            promotable = self._store.list_for_compression()
            if not promotable:
                logger.debug("无可压缩的 validated 洞察")
                return False

            logger.info(f"📝 开始自我认知压缩: {len(promotable)} 条 validated 洞察")

            # 1. 读取当前知识文档
            current_knowledge = self._store.read_current_knowledge()
            if not current_knowledge:
                current_knowledge = _INITIAL_KNOWLEDGE

            # 2. 收集反例来源（两条渠道）
            #    a) rejected：被审计明确否定的
            #    b) reconsidered：曾写进知识文档、后来她自己拿回来重想的
            #    只有 (a) 的话这个渠道基本是空的——审计几乎不出 rejected，
            #    而"我以前这么以为，现在不这么想了"恰恰是螺旋上升的主要形态。
            all_rejected = self._store.list_by_status(InsightStatus.REJECTED)
            rejected = all_rejected[-5:]
            reconsidered = self._store.list_reconsidered()[-5:]

            # 3. 调用 LLM 压缩
            new_content = await self._compress(
                current_knowledge=current_knowledge,
                validated_insights=promotable,
                rejected_insights=rejected,
                reconsidered_insights=reconsidered,
            )
            if not new_content or new_content.strip() == current_knowledge.strip():
                logger.info("压缩未产生变化")
                return False

            # 4. Selection Gate
            promote = await self._selection_gate(
                old_content=current_knowledge,
                new_content=new_content,
                edit_count=self._max_edits,
                insight_count=len(promotable),
            )

            # 5. 确定版本号
            manifest = self._store.load_knowledge_manifest()
            next_version = int(manifest.get("current_version", 0)) + 1

            # 6. 写入版本
            insight_ids = [ins.insight_id for ins in promotable]
            self._store.write_knowledge_version(
                content=new_content,
                version=next_version,
                insight_ids=insight_ids,
                edit_count=self._max_edits,
                promoted=promote,
                reason="selection_gate_passed" if promote else "selection_gate_rejected",
            )

            if promote:
                # 标记已压缩的洞察为 promoted（从 promote 队列移除）
                for ins in promotable:
                    ins.next_action = InsightNextAction.ARCHIVE.value
                    # 记下它进过哪个版本：万一以后她把这条拿回来重想、
                    # 又重新验证了，压缩器能认出"这条在旧版里已有表述，
                    # 该更新而不是再写一遍"。
                    if next_version not in ins.knowledge_versions:
                        ins.knowledge_versions.append(next_version)
                    self._store.update_insight(ins)

                # 更新状态
                state = self._store.load_state()
                state["last_compress_at"] = _now_iso()
                state["current_knowledge_version"] = next_version
                self._store.save_state(state)

                logger.info(f"✅ 自我认知 v{next_version} 已提升")
            else:
                logger.info(f"⏸️ 自我认知 v{next_version} 未通过 selection gate")

            return promote

    async def _compress(
        self,
        *,
        current_knowledge: str,
        validated_insights: list[Insight],
        rejected_insights: list[Insight],
        reconsidered_insights: list[Insight] | None = None,
    ) -> str:
        """调用 LLM 执行有界压缩。"""
        system_prompt = KNOWLEDGE_COMPRESS_SYSTEM.format(max_edits=self._max_edits)
        user_prompt = KNOWLEDGE_COMPRESS_USER.format(
            current_knowledge=current_knowledge,
            validated_insights=format_insights_for_compression(
                [ins.to_dict() for ins in validated_insights]
            ),
            rejected_insights=format_insights_for_compression(
                [ins.to_dict() for ins in rejected_insights]
            ),
            reconsidered_insights=format_reconsidered_for_compression(
                [ins.to_dict() for ins in (reconsidered_insights or [])]
            ),
            max_edits=self._max_edits,
        )

        request = create_llm_request(
            get_model_set_by_task(self._model_task_name),
            request_name="life_learning_compress",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        response = await asyncio.wait_for(
            request.send(auto_append_response=False, stream=False),
            timeout=self._timeout,
        )
        raw_text = await asyncio.wait_for(response, timeout=self._timeout)
        return str(raw_text or "").strip()

    async def _selection_gate(
        self,
        *,
        old_content: str,
        new_content: str,
        edit_count: int,
        insight_count: int,
    ) -> bool:
        """Selection Gate：判断新版本是否严格优于旧版本。"""
        user_prompt = SELECTION_GATE_USER.format(
            old_content=old_content[:3000],
            new_content=new_content[:3000],
            edit_count=edit_count,
            insight_count=insight_count,
        )

        try:
            request = create_llm_request(
                get_model_set_by_task(self._model_task_name),
                request_name="life_learning_gate",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(SELECTION_GATE_SYSTEM)))
            request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

            response = await asyncio.wait_for(
                request.send(auto_append_response=False, stream=False),
                timeout=self._timeout,
            )
            raw_text = await asyncio.wait_for(response, timeout=self._timeout)
            return self._parse_gate_result(str(raw_text or ""))
        except Exception as exc:
            logger.warning(f"Selection gate 调用失败，默认拒绝: {exc}")
            return False

    def _parse_gate_result(self, raw_text: str) -> bool:
        """解析 selection gate 结果。"""
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)

        if not isinstance(parsed, dict):
            return False
        return bool(parsed.get("promote", False))

    def get_knowledge_for_prompt(self, max_chars: int = 2000) -> str:
        """获取当前自我认知文档（用于 prompt 注入）。"""
        content = self._store.read_current_knowledge()
        if not content:
            return ""
        if max_chars > 0 and len(content) > max_chars:
            return content[:max_chars - 1].rstrip() + "…"
        return content


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
