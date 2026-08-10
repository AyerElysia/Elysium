"""Versioned self-knowledge integration.

Validated insights, rejected counterexamples, and reconsidered beliefs are
presented without recency truncation.  The integration process decides the
coherent scope of each revision, an independent gate reviews it, and every
proposal is versioned so later interpretations never erase earlier ones.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import json_repair

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import ROLE, LLMPayload, Text

from .models import Insight, InsightStatus
from .projection import project_learning_text
from .prompts import (
    KNOWLEDGE_COMPRESS_SYSTEM,
    KNOWLEDGE_COMPRESS_USER,
    SELECTION_GATE_SYSTEM,
    SELECTION_GATE_USER,
    format_insights_for_compression,
    format_reconsidered_for_compression,
)
from .store import InsightStore
from .timeouts import DEFAULT_LEARNING_LLM_TIMEOUT_SECONDS, send_with_deadline

logger = logging.getLogger("life_engine.learning.knowledge")

# 默认参数
_DEFAULT_TRIGGER_COUNT = 5  # 触发压缩的 validated 数量
_DEFAULT_INTERVAL_HOURS = 48.0  # 压缩最小间隔

# 后台知识整合 / 门禁的单次 LLM 往返总预算；质量优先且仍保持有界。
# 两次独立模型往返各自领取一份预算，单次往返内部仍只有一个 monotonic deadline。
_DEFAULT_TIMEOUT_SECONDS = DEFAULT_LEARNING_LLM_TIMEOUT_SECONDS
_MIN_TIMEOUT_SECONDS = 30.0
_INITIAL_KNOWLEDGE = """\
# 学习派生观察候选

这份文档只是学习系统基于经历整理的可修订观察，不属于 SOUL、USER、MEMORY
主体权威，也不替当前意识实例下结论。

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
    """慢环：把审计支持的洞察压缩为未获授权的版本化观察候选。"""

    def __init__(
        self,
        *,
        store: InsightStore,
        workspace_path: str | Path,
        model_task_name: str = "life",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        trigger_count: int = _DEFAULT_TRIGGER_COUNT,
        interval_hours: float = _DEFAULT_INTERVAL_HOURS,
        max_edits: int | None = None,
    ) -> None:
        self._store = store
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = str(model_task_name or "life").strip() or "life"
        self._timeout = max(
            _MIN_TIMEOUT_SECONDS, float(timeout_seconds or _DEFAULT_TIMEOUT_SECONDS)
        )
        self._trigger_count = max(2, int(trigger_count or _DEFAULT_TRIGGER_COUNT))
        self._interval_hours = max(
            6.0, float(interval_hours or _DEFAULT_INTERVAL_HOURS)
        )
        # 兼容旧配置入口；认知内容的修改范围由整合过程自行判断。
        del max_edits
        self._lock = asyncio.Lock()
        self._last_projection_stats: dict[str, Any] = {}

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
                now = datetime.now(UTC).astimezone()
                hours_elapsed = (now - last_dt).total_seconds() / 3600.0
                if hours_elapsed >= self._interval_hours and promotable:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    async def run_compression(self) -> bool:
        """Create one immutable proposal; never accept it on the subject's behalf."""
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
            rejected = self._store.list_by_status(InsightStatus.REJECTED)
            reconsidered = self.collect_reconsidered_memo()

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

            # 4. Independent assessment. This is a recommendation, not the
            # subject's acceptance and therefore cannot promote by itself.
            recommended = await self._selection_gate(
                old_content=current_knowledge,
                new_content=new_content,
                insight_count=len(promotable),
            )

            # 5. Allocate an immutable candidate version independently from
            # the current accepted projection head.
            manifest = self._store.load_knowledge_manifest()
            version_numbers = [int(manifest.get("current_version", 0) or 0)]
            versions = manifest.get("versions", [])
            if isinstance(versions, list):
                version_numbers.extend(
                    int(item.get("version", 0) or 0)
                    for item in versions
                    if isinstance(item, dict)
                )
            next_version = max(version_numbers, default=0) + 1

            # 6. Persist a proposal outside accepted self/subject authority.
            insight_ids = [ins.insight_id for ins in promotable]
            self._store.write_knowledge_version(
                content=new_content,
                version=next_version,
                insight_ids=insight_ids,
                edit_count=self._count_change_regions(current_knowledge, new_content),
                promoted=False,
                reason=(
                    "independent_gate_recommended"
                    if recommended
                    else "independent_gate_not_recommended"
                ),
            )

            state = self._store.load_state()
            state["last_compress_at"] = _now_iso()
            state["last_knowledge_candidate_version"] = next_version
            state["last_knowledge_candidate_recommended"] = recommended
            self._store.save_state(state)
            logger.info(
                "自我认知候选 v%s 已保存（独立评估=%s，尚未由主体接受）",
                next_version,
                recommended,
            )
            return True

    async def _compress(
        self,
        *,
        current_knowledge: str,
        validated_insights: list[Insight],
        rejected_insights: list[Insight],
        reconsidered_insights: list[Insight] | None = None,
    ) -> str:
        """调用 LLM 执行有界压缩。"""
        system_prompt = KNOWLEDGE_COMPRESS_SYSTEM
        layers = {
            "current": project_learning_text(
                current_knowledge,
                max_bytes=16 * 1024,
                projection_kind="knowledge_current",
            ),
            "validated": project_learning_text(
                format_insights_for_compression(
                    [ins.to_dict() for ins in validated_insights]
                ),
                max_bytes=48 * 1024,
                projection_kind="knowledge_validated_insights",
            ),
            "rejected": project_learning_text(
                format_insights_for_compression(
                    [ins.to_dict() for ins in rejected_insights]
                ),
                max_bytes=16 * 1024,
                projection_kind="knowledge_rejected_insights",
            ),
            "reconsidered": project_learning_text(
                format_reconsidered_for_compression(
                    [ins.to_dict() for ins in (reconsidered_insights or [])]
                ),
                max_bytes=16 * 1024,
                projection_kind="knowledge_reconsidered_insights",
            ),
        }
        user_prompt = KNOWLEDGE_COMPRESS_USER.format(
            current_knowledge=layers["current"].text,
            validated_insights=layers["validated"].text,
            rejected_insights=layers["rejected"].text,
            reconsidered_insights=layers["reconsidered"].text,
        )
        delivered = project_learning_text(
            user_prompt,
            max_bytes=96 * 1024,
            projection_kind="knowledge_compression_request",
        )
        self._last_projection_stats = {
            "request": delivered.stats(),
            "layers": {name: layer.stats() for name, layer in layers.items()},
        }

        request = create_llm_request(
            get_model_set_by_task(self._model_task_name),
            request_name="life_learning_compress",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(delivered.text)))

        raw_text = await send_with_deadline(request, self._timeout)
        return raw_text.strip()

    async def _selection_gate(
        self,
        *,
        old_content: str,
        new_content: str,
        insight_count: int,
    ) -> bool:
        """Selection Gate：判断新版本是否严格优于旧版本。"""
        user_prompt = SELECTION_GATE_USER.format(
            old_content=old_content,
            new_content=new_content,
            insight_count=insight_count,
        )
        delivered = project_learning_text(
            user_prompt,
            max_bytes=64 * 1024,
            projection_kind="knowledge_gate_request",
        )
        self._last_projection_stats["gate"] = delivered.stats()

        try:
            request = create_llm_request(
                get_model_set_by_task(self._model_task_name),
                request_name="life_learning_gate",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(SELECTION_GATE_SYSTEM)))
            request.add_payload(LLMPayload(ROLE.USER, Text(delivered.text)))

            raw_text = await send_with_deadline(request, self._timeout)
            return self._parse_gate_result(raw_text)
        except Exception as exc:
            logger.warning(
                "Selection gate 调用失败: %s",
                type(exc).__name__,
            )
            raise

    def _parse_gate_result(self, raw_text: str) -> bool:
        """解析 selection gate 结果。"""
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)

        if not isinstance(parsed, dict):
            raise ValueError("KnowledgeSelectionGateOutputMustBeObject")
        if parsed.get("promote") not in {True, False}:
            raise ValueError("KnowledgeSelectionGateDecisionMissing")
        return parsed["promote"] is True

    @staticmethod
    def _count_change_regions(old_content: str, new_content: str) -> int:
        """Count actual contiguous diff regions for audit metadata only."""
        matcher = difflib.SequenceMatcher(
            a=old_content.splitlines(),
            b=new_content.splitlines(),
            autojunk=False,
        )
        return sum(1 for tag, *_ in matcher.get_opcodes() if tag != "equal")

    def collect_reconsidered_memo(self, limit: int = 0) -> list[Insight]:
        """挑出"曾经写进认知文档、后来她自己不这么想了"的洞察。

        为什么要 knowledge_versions 这层过滤：
        list_reconsidered() 返回她重新想过的全部洞察，包含还在候选期就
        改了主意的——那只是想法在流动，不是"我曾经以为"。反例备忘要的是
        后者：确实写进过自我认知、后来被她自己拿回来的那些。

        只做筛选和排序，不下结论。
        """
        published = [
            ins for ins in self._store.list_reconsidered() if ins.knowledge_versions
        ]
        published.sort(key=lambda ins: ins.reconsidered_at)
        return published[-limit:] if limit > 0 else published

    def get_knowledge_for_prompt(self, max_chars: int = 0) -> str:
        """Return a bounded, explicitly non-authoritative learning projection."""
        content = self._store.read_current_knowledge()
        if not content:
            return ""
        framed = (
            "[学习观察投影｜非主体权威]\n"
            "来源：学习系统的历史派生账本；它不属于 SOUL.md、USER.md、MEMORY.md，"
            "不能覆盖三份主体权威。\n"
            "使用方式：只把下文当作可质疑、可重审的观察与假设；是否采纳以及如何"
            "表述，由当前活跃意识实例决定。\n\n" + content
        )
        projection = project_learning_text(
            framed,
            max_bytes=max_chars,
            projection_kind="learning_derived_observations",
        )
        stats = projection.stats()
        stats.update(
            {
                "authority": "derived_learning_observation",
                "authoritative": False,
                "content_source_sha256": hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
                "content_original_bytes": len(content.encode("utf-8")),
            }
        )
        self._last_projection_stats["prompt"] = stats
        return projection.text

    def projection_health(self) -> dict[str, Any]:
        """Return content-free hashes and budgets for the latest prompt layers."""

        return dict(self._last_projection_stats)


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()
