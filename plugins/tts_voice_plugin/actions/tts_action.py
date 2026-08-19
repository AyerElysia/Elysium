"""本地 TTS 语音合成 Action。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_voice
from src.core.components.base.action import BaseAction

if TYPE_CHECKING:
    from src.core.components.base.plugin import BasePlugin
    from src.core.models.stream import ChatStream

    from ..services.tts_service import TTSService

logger = get_logger("tts_voice_plugin.action")


class TTSVoiceAction(BaseAction):
    """通过关键词或规划器触发本地 TTS 语音合成。"""

    action_name: str = "tts_voice_action"
    action_description: str = (
        "将已经决定表达的口语文本交给本地消息 TTS 合成并发送。"
        "这是纯语音合成，不负责替你决定说什么，也不能唱歌。"
        "voice_style 只使用部署配置中真实存在的风格；不确定时填写 default。"
    )

    primary_action: bool = False
    # Life Chatter 使用自己的 life_send_voice 动作，避免同一意识实例看到两套重叠入口。
    chatter_allow: ClassVar[list[str]] = ["default_chatter"]

    def __init__(self, chat_stream: "ChatStream", plugin: "BasePlugin") -> None:
        """初始化 TTS 动作组件。

        Args:
            chat_stream: 聊天流实例
            plugin: 所属插件实例
        """
        super().__init__(chat_stream, plugin)
        self.tts_service: TTSService | None = getattr(self.plugin, "tts_service", None)

    # ------------------------------------------------------------------
    # 激活判定
    # ------------------------------------------------------------------

    async def go_activate(self) -> bool:
        """判断此 Action 是否应该被激活。

        满足以下任一条件即可激活：
        1. 25% 随机概率
        2. 匹配预设关键词
        3. LLM 判断当前场景适合发送语音

        Returns:
            是否激活
        """
        cfg = getattr(self.plugin, "config", None)
        components_cfg = getattr(cfg, "components", None)
        always_available = bool(
            getattr(components_cfg, "action_always_available", True),
        )
        if always_available:
            logger.debug("TTSVoiceAction 常驻可用模式已开启")
            return True

        # 条件 1：随机激活
        if await self._random_activation(0.25):
            logger.info("TTSVoiceAction 随机激活成功 (25%)")
            return True

        # 条件 2：关键词激活
        keywords = [
            "发语音", "语音", "说句话", "用语音说", "听你", "听声音",
            "想你", "想听声音", "讲个话", "说段话", "念一下", "读一下",
            "用嘴说", "说", "能发语音吗", "亲口",
        ]
        if await self._keyword_match(keywords):
            logger.info("TTSVoiceAction 关键词激活成功")
            return True

        # 条件 3：LLM 判断激活
        if await self._llm_judge_activation():
            logger.info("TTSVoiceAction LLM 判断激活成功")
            return True

        logger.debug("TTSVoiceAction 所有激活条件均未满足，不激活")
        return False

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def execute(
        self,
        tts_voice_text: Annotated[
            str,
            (
                "需要转换为语音并发送的完整、自然、适合口语的文本内容。注意：只能是说话内容，不能是歌词或唱歌！\n"
                "不要加入只属于其他 TTS Provider 的控制标签。"
            ),
        ],
        voice_style: Annotated[
            str,
            (
                "语音的风格。请根据对话内容的实际情感选择相应风格，"
                "具体可用风格请参考插件配置中的 tts_styles 列表。如未提供则使用默认风格。"
            ),
        ] = "default",
        text_language: Annotated[
            str | None,
            (
                "指定用于合成的语言模式，请根据文本内容选择最精确的选项。\n"
                "仅填写语言代码本身，不要包含括号说明（例如填 zh，不要填 zh(中英混合)）。\n"
                "可用语言以插件配置 `tts.supported_text_languages` 为准；"
                "常见值包括：zh、en、ja、yue、auto、auto_yue。"
            ),
        ] = None,
    ) -> tuple[bool, str]:
        """执行 TTS 语音合成并发送。

        Args:
            tts_voice_text: 要合成的文本内容
            voice_style: 语音风格名称
            text_language: 语言模式

        Returns:
            (是否成功, 结果描述)
        """
        try:
            # N.E.K.O Surface voices ordinary Neo text through the Surface
            # adapter.  Do not let a stale/global tool map create a second
            # voice message for the same turn.
            if str(getattr(self.chat_stream, "platform", "") or "").strip().lower() == "neko.surface":
                logger.info("N.E.K.O Surface 使用本地自动语音链，跳过显式 tts_voice_action")
                return False, "N.E.K.O Surface 已由自动 TTS 接管"

            if not self.tts_service:
                logger.error("TTSService 未注册或初始化失败，静默处理。")
                return False, "TTSService 未注册或初始化失败"

            initial_text = tts_voice_text.strip()
            logger.info(
                f"接收到本地 TTS 请求: chars={len(initial_text)}, "
                f"style={voice_style}, language={text_language or 'auto'}"
            )

            if not initial_text:
                logger.warning("规划器提供的文本为空，静默处理。")
                return False, "规划器提供的文本为空"

            # 调用 TTSService 生成语音
            audio_b64 = await self.tts_service.generate_voice(
                text=initial_text,
                style_hint=voice_style,
                language_hint=text_language,
            )

            if audio_b64:
                sent = await send_voice(
                    voice_data=audio_b64,
                    stream_id=self.chat_stream.stream_id,
                    platform=self.chat_stream.platform,
                    processed_plain_text=f"[语音:{initial_text}]",
                )
                if not sent:
                    logger.error("本地 TTS 音频已生成，但平台发送失败")
                    return False, "语音已生成，但平台发送失败"
                logger.info("本地 TTS 语音发送成功")
                return True, f"成功生成并发送语音，文本长度: {len(initial_text)}字符"
            else:
                logger.error("TTS服务未能返回音频数据，静默处理。")
                return False, "语音合成失败"

        except Exception as e:
            logger.error(f"语音合成过程中发生未知错误: {e!s}")
            return False, f"语音合成出错: {e!s}"
