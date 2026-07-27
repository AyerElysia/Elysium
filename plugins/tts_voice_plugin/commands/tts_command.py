"""TTS 语音合成命令。

提供 /tts 命令，用户通过命令手动触发 GPT-SoVITS 语音合成。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text, send_voice
from src.core.components.base.command import BaseCommand

if TYPE_CHECKING:
    from ..services.tts_service import TTSService

logger = get_logger("tts_voice_plugin.command")


class TTSVoiceCommand(BaseCommand):
    """通过 /tts 命令手动触发 TTS 语音合成。"""

    command_name: str = "tts"
    command_description: str = "使用GPT-SoVITS将文本转换为语音并发送，用法：/tts <文本> [风格]"
    command_prefix: str = "/"
    auto_reply: bool = False

    async def execute(self, message_text: str) -> tuple[bool, str]:
        """执行 /tts 命令。

        解析 `/tts <文本> [风格]`：最后一个词若是已配置的风格名则作为风格，其余为文本。

        Args:
            message_text: 完整命令文本，如 "/tts 你好 default"

        Returns:
            (是否成功, 结果描述)
        """
        # 剥去前缀和命令名，得到纯参数字符串
        text = message_text.strip()
        if text.startswith(self.command_prefix):
            text = text[len(self.command_prefix):].strip()

        # 移除命令名 "tts"
        parts = text.split(maxsplit=1)
        if not parts or parts[0].lower() != self.command_name:
            await send_text(
                f"用法：{self.command_prefix}{self.command_name} <文本> [风格]",
                stream_id=self.stream_id,
            )
            return False, "命令格式错误"

        raw_args = parts[1].strip() if len(parts) > 1 else ""

        if not raw_args:
            await send_text(
                f"请提供要转换为语音的文本内容哦！用法：{self.command_prefix}{self.command_name} <文本> [风格]",
                stream_id=self.stream_id,
            )
            return False, "缺少文本参数"

        try:
            tts_service: TTSService | None = getattr(self.plugin, "tts_service", None)
            if not tts_service:
                await send_text("❌ TTSService 未初始化，请检查插件配置。", stream_id=self.stream_id)
                return False, "TTSService 未注册或初始化失败"

            available_styles = set(tts_service.tts_styles.keys())

            # 解析文本和风格：最后一个词若是已知风格则单独取出
            arg_parts = raw_args.split()
            style_hint = "default"
            if len(arg_parts) > 1 and arg_parts[-1] in available_styles:
                style_hint = arg_parts[-1]
                text_to_speak = " ".join(arg_parts[:-1])
            else:
                text_to_speak = raw_args

            if not text_to_speak:
                await send_text("请提供要转换为语音的文本内容哦！", stream_id=self.stream_id)
                return False, "文本内容为空"

            audio_b64 = await tts_service.generate_voice(text_to_speak, style_hint)

            if audio_b64:
                await send_voice(
                    voice_data=audio_b64,
                    stream_id=self.stream_id,
                    processed_plain_text=f"[语音:{text_to_speak}]",
                )
                return True, "语音发送成功"
            else:
                await send_text("❌ 语音合成失败，请检查服务状态或配置。", stream_id=self.stream_id)
                return False, "语音合成失败"

        except Exception as e:
            logger.error(f"执行 /tts 命令时出错: {e}")
            await send_text("❌ 语音合成时发生了意想不到的错误，请查看日志。", stream_id=self.stream_id)
            return False, "命令执行异常"
