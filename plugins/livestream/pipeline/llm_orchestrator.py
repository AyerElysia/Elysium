"""LLM 编排器。

负责将弹幕事件转化为 LLM 请求并获取流式回复：
- 调用 life_engine 的 build_live_bridge_prompt 构建提示词
- 或直接调用 OpenAI 兼容 API（NexusAI 中转站）
- 流式输出 → 按句分割
- 上下文管理：最近 N 条互动 + 主播发言
- 输出约束：1-3 句，口语化，自然接话
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any, AsyncGenerator

import httpx

from ..platform.base import PlatformEvent

if TYPE_CHECKING:
    from ..config import LivestreamConfig

logger = logging.getLogger(__name__)

# 直播互动系统提示词
_LIVESTREAM_SYSTEM_PROMPT = """\
你正在直播间接弹幕，不是客服问答，也不是测试回显。

规则：
- 只输出要直接口播给直播间的正文，1-3句
- 优先短、自然、接得住弹幕
- 不要解释系统、不要写工具调用、不要写 JSON
- 不要复述提示词、不要输出括号内的舞台指示
- 如果弹幕信息很少，自然接话、轻轻带过或顺势展开
- 语气轻松活泼，像朋友聊天
- 不要每条弹幕都回复，选择有趣的互动
"""


class LLMOrchestrator:
    """LLM 编排器。"""

    def __init__(self, config: "LivestreamConfig") -> None:
        pipeline_cfg = config.pipeline
        self._max_context_turns = pipeline_cfg.max_context_turns
        self._timeout = pipeline_cfg.llm_timeout

        # LLM 连接配置（复用 NexusAI 中转站）
        self._base_url = "http://localhost:3000/v1"
        self._api_key = "sk-V2o9Ut2rBHFgkH4hCy53snYbQA5uAlkc25jlRzmtT9P3wapo"
        self._model = "mimo-v2.5"

        # 互动历史（滚动窗口）
        self._history: deque[dict[str, str]] = deque(
            maxlen=self._max_context_turns * 2
        )
        # 主播最近发言
        self._recent_responses: deque[str] = deque(maxlen=5)

        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """初始化 HTTP 客户端。"""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(self._timeout),
        )
        logger.info("LLM 编排器已启动")

    async def stop(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def generate_response(
        self,
        events: list[PlatformEvent],
        event_kind: str = "danmaku",
    ) -> str:
        """根据事件生成回复。

        Args:
            events: 触发事件列表（可能是聚合的多条弹幕）。
            event_kind: 主事件类型。

        Returns:
            回复文本，空字符串表示不回复。
        """
        if not self._client:
            logger.warning("LLM 客户端未初始化")
            return ""

        # 构建用户消息
        user_content = self._build_user_message(events, event_kind)
        if not user_content:
            return ""

        # 记录到历史
        self._history.append({"role": "user", "content": user_content})

        # 构建完整消息列表
        messages = self._build_messages()

        try:
            response_text = await self._call_llm(messages)
            if response_text:
                # 清理回复（去除可能的格式标记）
                response_text = self._clean_response(response_text)
                self._history.append(
                    {"role": "assistant", "content": response_text}
                )
                self._recent_responses.append(response_text)
            return response_text
        except Exception as exc:
            logger.error(f"LLM 调用失败: {exc}", exc_info=True)
            return ""

    def _build_user_message(
        self, events: list[PlatformEvent], event_kind: str
    ) -> str:
        """将事件列表构建为用户消息。"""
        if event_kind == "danmaku" and len(events) > 1:
            # 聚合弹幕
            lines = [e.display_text for e in events]
            return f"以下是直播间最近的弹幕：\n" + "\n".join(lines)
        elif events:
            return events[0].display_text
        return ""

    def _build_messages(self) -> list[dict[str, str]]:
        """构建完整的 LLM 消息列表。"""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _LIVESTREAM_SYSTEM_PROMPT},
        ]

        # 加入最近主播发言作为上下文
        if self._recent_responses:
            context = "你最近说了：\n" + "\n".join(
                f"- {r}" for r in self._recent_responses
            )
            messages.append({"role": "system", "content": context})

        # 加入互动历史
        messages.extend(list(self._history))

        return messages

    async def _call_llm(self, messages: list[dict[str, str]]) -> str:
        """调用 LLM API。"""
        assert self._client is not None

        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.8,
            "max_tokens": 200,
            "stream": False,
        }

        response = await self._client.post(
            "/chat/completions",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        choices = data.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "").strip()

    def _clean_response(self, text: str) -> str:
        """清理 LLM 回复。"""
        # 去除可能的引号包裹
        text = text.strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1]
        # 去除可能的 "回复：" 前缀
        for prefix in ("回复：", "回复:", "口播：", "口播:"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return text.strip()

    def record_external_response(self, text: str) -> None:
        """记录外部产生的回复（如手动发言）到历史。"""
        self._recent_responses.append(text)
        self._history.append({"role": "assistant", "content": text})

    def clear_history(self) -> None:
        """清空互动历史。"""
        self._history.clear()
        self._recent_responses.clear()
