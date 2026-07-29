"""TTS Voice 插件配置。

定义本地语音合成插件的配置项，包括基础设置、风格列表、高级参数和空间音效。
引擎后端可替换（当前默认对接 GPT-SoVITS API 协议）。
"""

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


@config_section("plugin")
class PluginSection(SectionBase):
    """插件基本配置。"""

    enable: bool = Field(default=False, description="是否启用插件")
    keywords: list[str] = Field(
        default_factory=lambda: [
            "发语音", "语音", "说句话", "用语音说", "听你", "听声音",
            "想听你", "想听声音", "讲个话", "说段话", "念一下", "读一下",
            "用嘴说", "说", "能发语音吗", "亲口",
        ],
        description="触发语音合成的关键词列表",
    )


@config_section("components")
class ComponentsSection(SectionBase):
    """组件启用控制。"""

    action_enabled: bool = Field(default=True, description="是否启用 Action 组件")
    action_always_available: bool = Field(
        default=True,
        description=(
            "是否让 tts_voice_action 始终可用。"
            "开启后不会依赖随机/关键词/LLM 判定，适合 default_chatter 稳定调用 TTS。"
        ),
    )
    command_enabled: bool = Field(default=True, description="是否启用 Command 组件")


@config_section("prompt")
class PromptSection(SectionBase):
    """自定义提示词配置。"""

    custom_instructions: str = Field(
        default="",
        description=(
            "追加到 tts_voice_action action 描述末尾的自定义指令。\n"
            "可描述希望 AI 主动使用语音功能的具体场景，"
            "例如：在表达亲密感、讲故事或用户明确要求听声音时主动使用。\n"
            "不会覆盖已有的触发条件，只是扩充场景说明。"
        ),
    )


@config_section("tts")
class TTSSection(SectionBase):
    """TTS 语音合成基础配置。"""

    server: str = Field(default="http://127.0.0.1:9880", description="本地 TTS 服务地址")
    timeout: int = Field(default=180, description="TTS 请求超时秒数")
    max_text_length: int = Field(default=1000, description="最大合成文本长度")
    auto_start: bool = Field(
        default=True,
        description="调用前若服务未运行，是否自动拉起 TTS 服务进程",
    )
    server_dir: str = Field(
        default="/root/Elysia/GPT-SoVITS",
        description="TTS 服务的工作目录（启动命令的 cwd）",
    )
    start_command: str = Field(
        default="python3 api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml",
        description="用于拉起 TTS 服务的 shell 命令（在 server_dir 下执行）",
    )
    startup_timeout: int = Field(
        default=120,
        description="自动拉起后等待服务就绪的最大秒数",
    )
    supported_text_languages: list[str] = Field(
        default_factory=lambda: ["zh", "en", "ja", "yue", "auto", "auto_yue"],
        description=(
            "允许发送给 TTS API 的 text_lang 语言代码列表。"
            "支持带说明格式（如 zh(中英混合)），服务端会自动提取括号前代码。"
        ),
    )
    fallback_text_language: str = Field(
        default="zh",
        description="当输入语言非法或不受支持时的兜底语言代码",
    )


@config_section("tts_styles")
class TTSStyle(SectionBase):
    """TTS 风格参数配置，每个实例代表一种独立的语音风格。"""

    style_name: str = Field(default="default", description="风格唯一标识符，必须有一个名为 default")
    name: str = Field(default="默认", description="显示名称")
    refer_wav_path: str = Field(
        default="C:/path/to/your/reference.wav",
        description="主参考音频路径（时长必须在 3~10 秒之间，超出会被引擎拒绝）",
    )
    aux_refer_wav_paths: list[str] = Field(
        default_factory=list,
        description=(
            "辅助参考音频路径列表（可选）。用于音色融合，不受 3~10 秒限制，"
            "适合把多段音频的音色特征叠加到主参考之上。"
        ),
    )
    prompt_text: str = Field(
        default="这是一个示例文本，请替换为您自己的参考音频文本。",
        description="参考音频文本（必须与主参考音频内容一致）",
    )
    prompt_language: str = Field(default="zh", description="参考音频语言")
    gpt_weights: str = Field(default="C:/path/to/your/gpt_weights.ckpt", description="GPT 模型路径")
    sovits_weights: str = Field(default="C:/path/to/your/sovits_weights.pth", description="SoVITS 模型路径")
    speed_factor: float = Field(default=1.0, description="语速因子")
    text_language: str = Field(default="auto", description="文本语言模式 (zh/ja/en/auto 等)")


@config_section("tts_advanced")
class TTSAdvancedSection(SectionBase):
    """TTS 高级参数配置（语速、采样、批处理等）。"""

    media_type: str = Field(default="wav", description="输出音频格式")
    top_k: int = Field(default=9, description="Top-K 采样参数")
    top_p: float = Field(default=0.8, description="Top-P 核采样参数")
    temperature: float = Field(default=0.8, description="温度参数")
    batch_size: int = Field(default=6, description="批处理大小")
    batch_threshold: float = Field(default=0.75, description="批处理阈值")
    text_split_method: str = Field(default="cut5", description="文本分割方法")
    repetition_penalty: float = Field(default=1.4, description="重复惩罚因子")
    sample_steps: int = Field(default=150, description="采样步数")
    super_sampling: bool = Field(default=True, description="是否启用超采样")


@config_section("spatial_effects")
class SpatialEffectsSection(SectionBase):
    """空间音效配置。"""

    enabled: bool = Field(default=False, description="是否启用空间音效处理")
    reverb_enabled: bool = Field(default=False, description="是否启用标准混响效果")
    room_size: float = Field(default=0.2, description="混响的房间大小 (0.0-1.0)")
    damping: float = Field(default=0.6, description="混响的阻尼/高频衰减 (0.0-1.0)")
    wet_level: float = Field(default=0.3, description="混响的湿声比例 (0.0-1.0)")
    dry_level: float = Field(default=0.8, description="混响的干声比例 (0.0-1.0)")
    width: float = Field(default=1.0, description="混响的立体声宽度 (0.0-1.0)")
    convolution_enabled: bool = Field(default=False, description="是否启用卷积混响（需要 assets/small_room_ir.wav）")
    convolution_mix: float = Field(default=0.7, description="卷积混响的干湿比 (0.0-1.0)")


class TTSVoiceConfig(BaseConfig):
    """TTS Voice 插件主配置类。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "本地语音合成插件配置"

    plugin: PluginSection = Field(default_factory=PluginSection)
    components: ComponentsSection = Field(default_factory=ComponentsSection)
    prompt: PromptSection = Field(default_factory=PromptSection)
    tts: TTSSection = Field(default_factory=TTSSection)
    tts_styles: list[TTSStyle] = Field(
        default_factory=lambda: [TTSStyle()],
        description="TTS 风格列表，每项为一种独立的语音风格配置",
    )
    tts_advanced: TTSAdvancedSection = Field(default_factory=TTSAdvancedSection)
    spatial_effects: SpatialEffectsSection = Field(default_factory=SpatialEffectsSection)
