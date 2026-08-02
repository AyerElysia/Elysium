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
import os
from collections import deque
from typing import TYPE_CHECKING, Any

import httpx

from ..platform.base import PlatformEvent

if TYPE_CHECKING:
    from ..config import LivestreamConfig

logger = logging.getLogger(__name__)

# 流无关的表达提示；直播间等场景事实只进入当前 user turn。
_EXPRESSION_SYSTEM_PROMPT = """\
只输出本次要直接表达的正文，1-3句。

建议：
- 优先简短、自然地回应当前信息
- 不要解释系统、不要写工具调用、不要写 JSON
- 不要复述提示词、不要输出括号内的舞台指示
"""


class LLMOrchestrator:
    """LLM 编排器。"""

    def __init__(
        self,
        config: "LivestreamConfig",
        *,
        consciousness: Any | None = None,
    ) -> None:
        """Create an orchestrator bound to an optional real consciousness."""

        pipeline_cfg = config.pipeline
        self._max_context_turns = pipeline_cfg.max_context_turns
        self._timeout = pipeline_cfg.llm_timeout

        self._base_url = str(pipeline_cfg.llm_base_url).strip().rstrip("/")
        self._api_key_env = str(pipeline_cfg.llm_api_key_env).strip()
        self._model = str(pipeline_cfg.llm_model).strip()
        if not self._base_url:
            raise ValueError("livestream pipeline.llm_base_url 不能为空")
        if not self._api_key_env:
            raise ValueError("livestream pipeline.llm_api_key_env 不能为空")
        if not self._model:
            raise ValueError("livestream pipeline.llm_model 不能为空")

        # 互动历史（滚动窗口）
        self._history: deque[dict[str, str]] = deque(
            maxlen=self._max_context_turns * 2
        )
        # 主播最近发言
        self._recent_responses: deque[str] = deque(maxlen=5)

        self._client: httpx.AsyncClient | None = None
        self._consciousness = consciousness

    async def start(self) -> None:
        """初始化 HTTP 客户端。"""
        api_key = os.environ.get(self._api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(
                f"直播 LLM 凭据缺失：环境变量 {self._api_key_env} 为空"
            )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
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

        prepared = None
        transient_context = ""
        if self._consciousness is not None:
            prepared = await asyncio.to_thread(
                self._consciousness.prepare_perception
            )
            transient_context = prepared.content

        # 构建完整消息列表
        messages = self._build_messages(transient_context)

        try:
            response_text = await self._call_llm(messages)
            if prepared is not None:
                await asyncio.to_thread(
                    self._consciousness.commit_perception,
                    prepared,
                )
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
            detail = "以下是直播间最近的弹幕：\n" + "\n".join(lines)
        elif events:
            detail = events[0].display_text
        else:
            return ""
        return (
            f"<current_scene platform=livestream event_kind={event_kind}>\n"
            f"{detail}\n"
            "</current_scene>"
        )

    def _build_messages(
        self,
        transient_context: str = "",
    ) -> list[dict[str, str]]:
        """构建完整的 LLM 消息列表。"""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _EXPRESSION_SYSTEM_PROMPT},
        ]

        # 加入最近主播发言作为上下文
        if self._recent_responses:
            context = "你最近说了：\n" + "\n".join(
                f"- {r}" for r in self._recent_responses
            )
            messages.append({"role": "system", "content": context})

        # 加入互动历史；世界投影只进入本次请求，不写入滚动历史。
        history = list(self._history)
        if transient_context:
            insertion = max(0, len(history) - 1)
            history.insert(
                insertion,
                {
                    "role": "user",
                    "content": (
                        "<transient_world_perception>\n"
                        f"{transient_context}\n"
                        "</transient_world_perception>"
                    ),
                },
            )
        messages.extend(history)

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
