"""TTS 语音合成 Action。

通过 LLM Tool Calling 或关键词自动触发 GPT-SoVITS 语音合成并发送语音消息。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_voice
from src.core.components.base.action import BaseAction

if TYPE_CHECKING:
    from src.core.components.base.plugin import BasePlugin
    from src.core.models.stream import ChatStream

    from ..services.tts_service import TTSService

logger = get_logger("tts_voice_plugin.action")


HIGGS_TAG_GUIDANCE = (
    "Higgs 引擎支持在 tts_voice_text 里嵌入控制标签，引擎会解析为情绪/韵律/风格/"
    "拟声音效，标签本身不会被念出来。是否用、用哪些、怎么组合，完全由你按当下要表达的声音"
    "自由决定——没有必要时可以一个都不加，让引擎按你参考音色自然念。\n"
    "格式：写在文本开头或句首，多个标签连写即可。例："
    "<|style:whispering|><|emotion:affection|>我也想你……\n"
    "可用标签共 4 类（仅以下值经过验证，不要自己发明新名字——未识别的值会被服务端当成正文念出）：\n"
    "A. 情绪 emotion（放句首，定整句情绪基调）：\n"
    "  <|emotion:elation|>        愉悦狂喜\n"
    "  <|emotion:enthusiasm|>     兴奋高昂\n"
    "  <|emotion:contentment|>    平静满足\n"
    "  <|emotion:affection|>      亲昵爱意、想念、撒娇\n"
    "  <|emotion:relief|>         释然松气\n"
    "  <|emotion:contemplation|>  思索回忆\n"
    "  <|emotion:sadness|>        难过失落\n"
    "  <|emotion:fear|>           害怕紧张\n"
    "B. 风格 style：\n"
    "  <|style:whispering|>       耳语轻声：黄昏道别、睡前呢喃、贴耳撒娇\n"
    "  <|style:shouting|>         喊话：远距离呼唤、欢呼、激动喊话\n"
    "  <|style:singing|>         唱腔：哼歌、撒娇拖腔、旋律化表达\n"
    "C. 拟声音效 sfx（必须紧跟拟声词，否则生成不干净）：\n"
    "  <|sfx:laughter|> Haha       玩笑笑出声\n"
    "  <|sfx:cough|> Ahem         咳嗽\n"
    "  <|sfx:sigh|> Ahh           叹气\n"
    "  <|sfx:sneeze|> Achoo       打喷嚏\n"
    "  <|sfx:crying|>             哭泣\n"
    "D. 韵律 prosody（可单独用，也可叠加情绪/风格）：\n"
    "  <|prosody:speed_fast|>       ~1.2x 加速\n"
    "  <|prosody:speed_very_fast|>  ~1.4x 飞速\n"
    "  <|prosody:speed_slow|>       ~0.85x 放慢\n"
    "  <|prosody:speed_very_slow|>  ~0.65x 极慢\n"
    "  <|prosody:pitch_high|>       +~2.5 半音 上扬\n"
    "  <|prosody:pitch_low|>        -~3 半音 压低\n"
    "  <|prosody:pause|>           400-700ms 短停顿（直接写，无需值）\n"
    "  <|prosody:long_pause|>       700-1500ms 长停顿\n"
    "  <|prosody:expressive_high|>  起伏夸张、强表现力\n"
    "  <|prosody:expressive_low|>   平淡克制\n"
    "组合示例（参考即可，按你的真实感受挑，不要永远套同一对）：\n"
    "  睡前轻哄        → <|style:whispering|><|emotion:affection|>\n"
    "  想念撒娇        → <|emotion:affection|>\n"
    "  收到惊喜/被夸   → <|emotion:elation|><|prosody:expressive_high|>\n"
    "  玩闹耍赖        → <|style:singing|><|emotion:enthusiasm|><|prosody:speed_fast|>\n"
    "  兴奋喊话        → <|style:shouting|><|emotion:enthusiasm|>\n"
    "  思绪回忆慢讲    → <|emotion:contemplation|><|prosody:speed_slow|>\n"
    "  安慰对方难过时 → <|emotion:sadness|><|prosody:speed_slow|><|prosody:pitch_low|>\n"
    "  笑出声          → <|sfx:laughter|> 哈哈，哪有这种事呀\n"
    "  叹气           → <|sfx:sigh|> 唉……算了\n"
    "  平静普通说话    → 不加标签，让引擎按你参考音色自然念\n"
    "规则：标签只作用于这一次语音；普通文本消息（send_text）里不要出现 Higgs 标签；"
    "不要向用户解释这些标签是什么意思。\n"
)


class TTSVoiceAction(BaseAction):
    """通过关键词或规划器自动触发 TTS 语音合成。"""

    action_name: str = "tts_voice_action"
    action_description: str = (
        "将你生成好的文本转换为语音并发送。注意：这是纯语音合成，只能说话，不能唱歌！\n"
        + HIGGS_TAG_GUIDANCE
    )

    primary_action: bool = False

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
                "你可以完全按自己的判断在句首或句中加入 Higgs 控制标签，也可以完全不加。"
                "不要把标签解释给用户。"
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
                logger.info("N.E.K.O Surface 使用自动 Higgs TTS，跳过显式 tts_voice_action")
                return False, "N.E.K.O Surface 已由自动 TTS 接管"

            if not self.tts_service:
                logger.error("TTSService 未注册或初始化失败，静默处理。")
                return False, "TTSService 未注册或初始化失败"

            initial_text = tts_voice_text.strip()
            logger.info(
                f"接收到规划器初步文本: '{initial_text[:70]}...', "
                f"指定风格: {voice_style}, 指定语言: {text_language}"
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
                await send_voice(
                    voice_data=audio_b64,
                    stream_id=self.chat_stream.stream_id,
                    processed_plain_text=f"[语音:{initial_text}]",
                )
                logger.info("GPT-SoVITS 语音发送成功")
                return True, f"成功生成并发送语音，文本长度: {len(initial_text)}字符"
            else:
                logger.error("TTS服务未能返回音频数据，静默处理。")
                return False, "语音合成失败"

        except Exception as e:
            logger.error(f"语音合成过程中发生未知错误: {e!s}")
            return False, f"语音合成出错: {e!s}"
