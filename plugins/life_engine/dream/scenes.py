"""梦境场景数据结构与生成函数。

包含 DreamScene、DreamTrace 数据类，
以及调用 LLM 生成结构化梦境的函数。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import LLMPayload, ROLE, Text

from .residue import (
    _clean_text,
    DreamResidue,
    _parse_json_payload,
)

if TYPE_CHECKING:
    from .seeds import DreamSeed
    from .residue import REMReport


logger = logging.getLogger("life_engine.dream")

# 常量
_MAX_SCENES = 5
_MAX_DREAM_SEEDS = 3
_DREAM_SCENE_TIMEOUT_SECONDS = 600.0
_DREAM_SCENE_MAX_RETRIES = 3


@dataclass
class DreamScene:
    """梦中的一个片段。"""

    title: str = ""
    summary: str = ""
    imagery: list[str] = field(default_factory=list)
    emotion_shift: str = ""
    refs: list[str] = field(default_factory=list)


@dataclass
class DreamTrace:
    """结构化梦迹。"""

    scenes: list[DreamScene] = field(default_factory=list)
    motifs: list[str] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)


async def build_dream_scene(
    seeds: list["DreamSeed"],
    rem_report: "REMReport",
    event_history: list[Any],
    model_task_name: str,
    inner_state_summary: str,
    recent_context_summary: list[str],
    reference_previews: list[dict[str, str]],
    recent_dream_summaries: list[str] | None = None,
    emit_visual_event: Any = None,
) -> tuple[DreamTrace, str, DreamResidue] | None:
    """用 LLM 将入梦种子变形成梦境。

    失败时返回 None（不伪造梦境）。

    Args:
        seeds: 入梦种子列表
        rem_report: REM 报告
        event_history: 事件历史
        model_task_name: 模型任务名
        inner_state_summary: 内在状态摘要
        recent_context_summary: 近期上下文摘要
        reference_previews: 引用预览列表
        recent_dream_summaries: 最近梦境摘要（用于避重）
        emit_visual_event: 可视化事件发射函数

    Returns:
        (DreamTrace, dream_text, DreamResidue) 或 None
    """
    last_error: Exception | None = None

    for attempt in range(_DREAM_SCENE_MAX_RETRIES):
        try:
            payload = await generate_scene_payload(
                seeds=seeds,
                rem_report=rem_report,
                event_history=event_history,
                model_task_name=model_task_name,
                inner_state_summary=inner_state_summary,
                recent_context_summary=recent_context_summary,
                reference_previews=reference_previews,
                recent_dream_summaries=recent_dream_summaries,
                emit_visual_event=emit_visual_event,
            )
            trace = _trace_from_payload(payload.get("dream_trace"))
            dream_text = _clean_text(payload.get("dream_text"))
            residue = _residue_from_payload(payload.get("dream_residue"))
            if dream_text and residue.summary:
                return trace, dream_text, residue
            logger.warning(
                f"DreamSceneBuilder 第{attempt+1}次返回空文本，重试中..."
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                f"DreamSceneBuilder 第{attempt+1}次异常：{exc}，重试中..."
            )
            await asyncio.sleep(2)

    logger.error(
        f"做梦失败：DreamSceneBuilder {_DREAM_SCENE_MAX_RETRIES}次尝试均失败。"
        f"最后错误：{last_error}"
    )
    return None


async def generate_scene_payload(
    seeds: list["DreamSeed"],
    rem_report: "REMReport",
    event_history: list[Any],
    model_task_name: str,
    inner_state_summary: str,
    recent_context_summary: list[str],
    reference_previews: list[dict[str, str]],
    recent_dream_summaries: list[str] | None = None,
    emit_visual_event: Any = None,
) -> dict[str, Any]:
    """调用模型生成结构化梦境。

    Args:
        seeds: 入梦种子列表
        rem_report: REM 报告
        event_history: 事件历史
        model_task_name: 模型任务名
        inner_state_summary: 内在状态摘要
        recent_context_summary: 近期上下文摘要
        reference_previews: 引用预览列表
        recent_dream_summaries: 最近梦境摘要（用于避重）
        emit_visual_event: 可视化事件发射函数

    Returns:
        结构化的梦境 payload 字典
    """
    import json

    from .residue import _seed_to_dict

    model_set = get_model_set_by_task(model_task_name)
    request = create_llm_request(
        model_set=model_set,
        request_name="life_engine_dream_scene",
    )
    request.add_payload(
        LLMPayload(
            ROLE.SYSTEM,
            Text(
                "\n".join(
                    [
                        "你是一个数字生命的 DreamSceneBuilder。",
                        "你的职责不是复述白天，而是把心理张力簇变形成主观梦境。",
                        "请输出严格 JSON，不要输出 markdown，不要输出解释。",
                        "梦必须具备：场景错位、时间折叠、情绪迁移、轻微象征化。",
                        "禁止：把梦写成计划、总结、工具调用说明、系统日志。",
                        "",
                        "多样性约束（极重要）：",
                        "- 每个梦的场景类型必须不同：不要总用走廊/房间/追赶，尝试水域、天空、声音、温度、光影、季节、时间折叠。",
                        "- 情绪基调必须变化：怅然只是一种可能，还有好奇、微甜、荒诞、安宁、不安、恍惚、炽热等。",
                        "- 叙事视角可以变化：第一人称、旁观者、片段式意识流、倒叙、多线交织。",
                        "- 将 seeds 中的具体内容（TODO标题、文件名）变形为象征意象，不要直接引用原文。",
                        "- 如果素材中有 avoid_recent_themes 字段，本次梦境必须在主题、意象和情绪上与它们明显不同。",
                        "",
                        "JSON 结构必须是：",
                        "{",
                        '  "dream_trace": {',
                        '    "scenes": [{"title":"", "summary":"", "imagery":[""], "emotion_shift":"", "refs":[""]}],',
                        '    "motifs": [""],',
                        '    "transitions": [""]',
                        "  },",
                        '  "dream_text": "一段 220-420 字的中文梦札正文",',
                        '  "dream_residue": {',
                        '    "summary": "一句 30-80 字的余韵总结",',
                        '    "life_payload": "120-220 字左右，给 life_engine 的梦后余韵",',
                        '    "dfc_payload": "80-140 字左右，给 DFC 当前轮 payload 的梦后余韵",',
                        '    "dominant_affect": "主导情绪，如怅然/期待/不安/温暖",',
                        '    "strength": "light 或 medium",',
                        '    "tags": [""]',
                        "  }",
                        "}",
                    ]
                )
            ),
        )
    )

    brief = {
        "seeds": [_seed_to_dict(seed) for seed in seeds[:_MAX_DREAM_SEEDS]],
        "rem_report": {
            "nodes_activated": rem_report.nodes_activated,
            "new_edges_created": rem_report.new_edges_created,
            "seed_node_ids": rem_report.seed_node_ids,
        },
        "inner_state": inner_state_summary,
        "recent_context": recent_context_summary,
        "reference_previews": reference_previews,
    }

    # 避重上下文
    if recent_dream_summaries:
        brief["avoid_recent_themes"] = list(recent_dream_summaries)

    request.add_payload(
        LLMPayload(
            ROLE.USER,
            Text(
                "以下是本轮做梦的结构化素材，请据此生成梦境 JSON：\n"
                + json.dumps(brief, ensure_ascii=False, indent=2)
            ),
        )
    )

    try:
        if emit_visual_event:
            emit_visual_event("dream.scene_generating", {"status": "request_sent"})
        response = await asyncio.wait_for(
            request.send(stream=False),
            timeout=_DREAM_SCENE_TIMEOUT_SECONDS,
        )
        response_text = str(
            await asyncio.wait_for(
                response,
                timeout=_DREAM_SCENE_TIMEOUT_SECONDS,
            )
            or ""
        ).strip()
        payload = _parse_json_payload(response_text)
        if not isinstance(payload, dict):
            raise ValueError("dream scene payload 非法")
        if emit_visual_event:
            emit_visual_event("dream.scene_generated", {"payload": payload})
        return payload
    except Exception as e:
        if emit_visual_event:
            emit_visual_event("dream.scene_failed", {"error": str(e)})
        raise


def _trace_from_payload(raw: Any) -> DreamTrace:
    """从 payload 转换为 DreamTrace。"""
    if not isinstance(raw, dict):
        return DreamTrace()
    scenes_raw = raw.get("scenes")
    scenes: list[DreamScene] = []
    if isinstance(scenes_raw, list):
        for item in scenes_raw[:_MAX_SCENES]:
            if not isinstance(item, dict):
                continue
            scenes.append(
                DreamScene(
                    title=_clean_text(item.get("title", "")),
                    summary=_clean_text(item.get("summary", "")),
                    imagery=[_clean_text(v) for v in item.get("imagery", []) if _clean_text(v)],
                    emotion_shift=_clean_text(item.get("emotion_shift", "")),
                    refs=[_clean_text(v) for v in item.get("refs", []) if _clean_text(v)],
                )
            )
    return DreamTrace(
        scenes=scenes,
        motifs=[_clean_text(v) for v in raw.get("motifs", []) if _clean_text(v)],
        transitions=[_clean_text(v) for v in raw.get("transitions", []) if _clean_text(v)],
    )


def _residue_from_payload(raw: Any) -> DreamResidue:
    """从 payload 转换为 DreamResidue。"""
    if not isinstance(raw, dict):
        return DreamResidue()
    return DreamResidue(
        summary=_clean_text(raw.get("summary", "")),
        life_payload=_clean_text(raw.get("life_payload", "")),
        dfc_payload=_clean_text(raw.get("dfc_payload", "")),
        dominant_affect=_clean_text(raw.get("dominant_affect", "")),
        strength=_clean_text(raw.get("strength", "light")) or "light",
        tags=[_clean_text(tag) for tag in raw.get("tags", []) if _clean_text(tag)],
        expires_at=float(raw.get("expires_at", 0.0) or 0.0),
    )


def build_recent_context_summary(event_history: list[Any]) -> list[str]:
    """构造模型可用的近期上下文摘要。"""
    from .seeds import _event_type_value, _event_value

    lines: list[str] = []
    for event in event_history[-6:]:
        etype = _event_type_value(event)
        content = _clean_text(_event_value(event, "content", ""))
        if not content:
            continue
        lines.append(f"{etype}: {content[:100]}")
    return lines


def build_reference_previews(
    seeds: list["DreamSeed"],
    workspace: Any,
    normalize_ref_func: Any,
    read_preview_func: Any,
) -> list[dict[str, str]]:
    """为 DreamSceneBuilder 提供 refs 预览。"""
    if workspace is None:
        return []

    from .residue import _iter_seed_file_refs

    previews: list[dict[str, str]] = []
    refs = _iter_seed_file_refs(seeds, workspace)
    for ref in refs[:6]:
        abs_path = workspace / ref
        previews.append(
            {
                "ref": ref,
                "preview": read_preview_func(abs_path, 140),
            }
        )
    return previews