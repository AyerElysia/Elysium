"""降级管线 LLM 客户端。

调用 MiMo-V2.5 多模态模型进行音频理解，
通过 OpenAI 兼容的 Chat Completions API。
"""

from __future__ import annotations

import base64
import io
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import aiohttp

from src.kernel.logger import get_logger

logger = get_logger("voice_live.llm_client", display="Degraded LLM")


@dataclass(slots=True)
class ConversationTurn:
    """对话轮次。"""

    role: str  # "user" | "assistant"
    text: str = ""
    audio_b64: str = ""  # base64 编码的音频（仅 user）


class DegradedLLMClient:
    """降级管线 LLM 客户端。

    使用 MiMo-V2.5 的多模态能力直接理解音频输入，
    无需单独的 STT 步骤。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        max_context_turns: int = 20,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_context_turns = max_context_turns

        # 对话历史
        self._history: deque[ConversationTurn] = deque(maxlen=max_context_turns)
        self._system_prompt: str = ""

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def set_system_prompt(self, prompt: str) -> None:
        """设置系统提示词。"""
        self._system_prompt = prompt

    def clear_history(self) -> None:
        """清空对话历史。"""
        self._history.clear()

    def add_assistant_turn(self, text: str) -> None:
        """添加助手回复到历史。"""
        self._history.append(ConversationTurn(role="assistant", text=text))

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------

    async def stream_response(
        self,
        audio_bytes: bytes,
        sample_rate: int = 16000,
    ) -> AsyncIterator[str]:
        """流式获取模型响应。

        Args:
            audio_bytes: PCM16 原始音频字节
            sample_rate: 采样率

        Yields:
            文本增量
        """
        # 将 PCM 转换为 WAV 格式（MiMo 需要标准音频格式）
        wav_b64 = self._pcm_to_wav_b64(audio_bytes, sample_rate)

        # 构建消息
        messages = self._build_messages(wav_b64)

        # 记录用户轮次
        self._history.append(ConversationTurn(
            role="user",
            audio_b64=wav_b64[:100] + "...",  # 只记录摘要
        ))

        # 发送请求
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "max_tokens": 1000,
            "temperature": 0.7,
        }

        full_response = ""
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"LLM 请求失败: {resp.status} - {error_text}")
                        yield f"[错误: {resp.status}]"
                        return

                    async for line in resp.content:
                        line_str = line.decode("utf-8").strip()
                        if not line_str or not line_str.startswith("data: "):
                            continue
                        data_str = line_str[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            import json
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_response += content
                                yield content
                        except Exception:  # noqa: BLE001
                            continue

        except Exception as exc:  # noqa: BLE001
            logger.error(f"LLM 流式请求异常: {exc}")
            yield f"[错误: {exc}]"

        # 记录完整回复
        if full_response:
            self.add_assistant_turn(full_response)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_messages(self, audio_wav_b64: str) -> list[dict[str, Any]]:
        """构建 OpenAI 格式的消息列表。"""
        messages: list[dict[str, Any]] = []

        # 系统消息
        if self._system_prompt:
            messages.append({
                "role": "system",
                "content": self._system_prompt,
            })

        # 历史对话（只保留文本摘要）
        for turn in list(self._history)[:-1]:  # 排除刚添加的当前轮
            if turn.role == "user" and turn.text:
                messages.append({"role": "user", "content": turn.text})
            elif turn.role == "assistant" and turn.text:
                messages.append({"role": "assistant", "content": turn.text})

        # 当前用户消息（音频）
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": audio_wav_b64,
                        "format": "wav",
                    },
                },
                {
                    "type": "text",
                    "text": "请根据上面的语音内容进行回复。",
                },
            ],
        })

        return messages

    @staticmethod
    def _pcm_to_wav_b64(pcm_bytes: bytes, sample_rate: int) -> str:
        """将 PCM16 转换为 WAV 格式的 base64。"""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)  # 单声道
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_bytes)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
