"""TTS 流式合成器。

将 LLM 的文本输出按句切分，
逐句调用 IndexTTS2 合成语音并流式返回。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any

from src.kernel.logger import get_logger

logger = get_logger("voice_live.tts_streamer", display="TTS Streamer")

# 句子结束标点
SENTENCE_ENDINGS = frozenset("。！？!?；;\n")

# 音频输出回调
AudioOutputCallback = Callable[[bytes, str], Awaitable[None]]  # (audio_bytes, format)


class TTSStreamer:
    """TTS 流式合成器。

    特性：
    - 按句切分（标点 + 最小字符阈值）
    - 并发合成（前一句播放时后一句已在合成）
    - 支持取消（barge-in 时停止所有待处理任务）
    """

    def __init__(
        self,
        tts_style: str = "default",
        sentence_min_chars: int = 8,
    ) -> None:
        self._tts_style = tts_style
        self._sentence_min_chars = sentence_min_chars

        # 生成计数器（用于取消旧任务）
        self._generation = 0
        self._pending_text: str = ""
        self._synthesis_tasks: set[asyncio.Task[None]] = set()
        self._output_callback: AudioOutputCallback | None = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def on_audio_output(self, callback: AudioOutputCallback) -> None:
        """注册音频输出回调。"""
        self._output_callback = callback

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------

    async def feed_text(self, text_delta: str) -> None:
        """输入文本增量。

        累积文本直到形成完整句子，然后触发合成。
        """
        self._pending_text += text_delta

        # 尝试切句
        while True:
            sentence = self._extract_sentence()
            if sentence is None:
                break
            # 启动合成任务
            task = asyncio.create_task(self._synthesize_sentence(sentence))
            self._synthesis_tasks.add(task)
            task.add_done_callback(self._synthesis_tasks.discard)

    async def flush(self) -> None:
        """刷新剩余文本（LLM 输出结束时调用）。"""
        if self._pending_text.strip():
            remaining = self._pending_text.strip()
            self._pending_text = ""
            task = asyncio.create_task(self._synthesize_sentence(remaining))
            self._synthesis_tasks.add(task)
            task.add_done_callback(self._synthesis_tasks.discard)

        # 等待所有合成任务完成
        if self._synthesis_tasks:
            await asyncio.gather(*self._synthesis_tasks, return_exceptions=True)

    async def cancel(self) -> None:
        """取消所有待处理的合成任务（barge-in）。"""
        self._generation += 1
        self._pending_text = ""

        # 取消所有进行中的任务
        for task in list(self._synthesis_tasks):
            task.cancel()
        if self._synthesis_tasks:
            await asyncio.gather(*self._synthesis_tasks, return_exceptions=True)
        self._synthesis_tasks.clear()
        logger.debug("TTS 已取消")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _extract_sentence(self) -> str | None:
        """从待处理文本中提取一个完整句子。"""
        text = self._pending_text

        # 查找句子结束标点
        for i, char in enumerate(text):
            if char in SENTENCE_ENDINGS:
                # 检查是否达到最小长度
                if i + 1 >= self._sentence_min_chars:
                    sentence = text[:i + 1].strip()
                    self._pending_text = text[i + 1:]
                    return sentence if sentence else None

        # 如果没有标点但文本过长，强制切分
        if len(text) >= self._sentence_min_chars * 4:
            # 在空格或逗号处切分
            for sep in ["，", ",", " ", "、"]:
                idx = text.rfind(sep, self._sentence_min_chars)
                if idx > 0:
                    sentence = text[:idx + 1].strip()
                    self._pending_text = text[idx + 1:]
                    return sentence if sentence else None

            # 硬切
            sentence = text[:self._sentence_min_chars * 2].strip()
            self._pending_text = text[self._sentence_min_chars * 2:]
            return sentence if sentence else None

        return None

    async def _synthesize_sentence(self, sentence: str) -> None:
        """合成单个句子。"""
        current_gen = self._generation

        try:
            tts_service = self._get_tts_service()
            if tts_service is None:
                logger.error("TTS 服务不可用")
                return

            # 检查是否已取消
            if self._generation != current_gen:
                return

            logger.debug(f"合成句子: {sentence[:30]}...")
            audio_b64 = await tts_service.generate_voice(
                sentence,
                self._tts_style,
            )

            # 检查是否已取消
            if self._generation != current_gen:
                return

            if audio_b64 and self._output_callback:
                audio_bytes = base64.b64decode(audio_b64)
                await self._output_callback(audio_bytes, "ogg")

        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error(f"TTS 合成失败: {exc}")

    @staticmethod
    def _get_tts_service() -> Any | None:
        """获取 TTS 服务实例。"""
        try:
            from src.core.managers import get_plugin_manager

            plugin = get_plugin_manager().get_plugin("tts_voice_plugin")
            service = getattr(plugin, "tts_service", None) if plugin else None
            if service and callable(getattr(service, "generate_voice", None)):
                return service
        except Exception:  # noqa: BLE001
            pass

        try:
            from src.app.plugin_system.api.service_api import get_service

            service = get_service("tts_voice_plugin:service:tts")
            if service and callable(getattr(service, "generate_voice", None)):
                return service
        except Exception:  # noqa: BLE001
            pass

        return None
