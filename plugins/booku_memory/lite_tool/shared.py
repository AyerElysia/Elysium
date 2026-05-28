"""Booku Memory 轻量工作流共享工具。"""

from __future__ import annotations

import json
from typing import Any

from json_repair import repair_json

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import LLMPayload, ROLE, Text

from ..agent.shared import get_internal_task_name
from ..config import BookuMemoryConfig


def parse_json_object(message: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON 对象，失败时返回空字典。"""
    content = message.strip()
    if not content:
        return {}

    try:
        data = json.loads(content)
    except Exception:
        try:
            data = json.loads(repair_json(content))
        except Exception:
            return {}

    return data if isinstance(data, dict) else {}


async def call_llm_json(
    system_prompt: str,
    payload: dict[str, Any],
    *,
    config: BookuMemoryConfig,
    request_name: str,
) -> dict[str, Any]:
    """调用内部模型并解析 JSON 对象响应。"""
    model_set = get_model_set_by_task(get_internal_task_name(config))
    request = create_llm_request(
        model_set=model_set,
        request_name=request_name,
    )
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
    request.add_payload(
        LLMPayload(ROLE.USER, Text(json.dumps(payload, ensure_ascii=False)))
    )
    response = await request.send(stream=False)
    await response
    return parse_json_object(response.message or "")
