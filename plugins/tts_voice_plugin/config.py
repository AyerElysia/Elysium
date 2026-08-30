"""TTS Voice 插件配置。

定义本地语音合成插件的配置项，包括基础设置、风格列表、高级参数和空间音效。
后端由 ``[tts].backend`` 显式选择 ``legacy_compat``（GPT-SoVITS api_v2）或
``vllm_omni``（IndexTTS2.5）；本机当前 live 配置以 ignored ``config.toml`` 为准。
"""

from typing import ClassVar, Literal

from src.core.components.base.config import (
    BaseConfig,
    Field,
    SectionBase,
    config_section,
)


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

    backend: Literal["legacy_compat", "vllm_omni"] = Field(
        default="legacy_compat",
        description=(
            "TTS HTTP 后端协议；legacy_compat 使用历史 /tts，"
            "vllm_omni 使用 OpenAI-compatible /v1/audio/speech"
        ),
    )
    server: str = Field(default="http://127.0.0.1:9880", description="本地 TTS 服务地址")
    model: str = Field(
        default="indextts25-timbre",
        description="vLLM-Omni 对外暴露的 IndexTTS2.5 served model name",
    )
    api_key_env: str = Field(
        default="",
        description=(
            "可选的 vLLM-Omni Bearer token 环境变量名；为空时不发送 Authorization"
        ),
    )
    timeout: int = Field(default=180, description="TTS 请求超时秒数")
    max_text_length: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description="单次表达允许合成的完整文本上限；超限显式失败，禁止静默截断",
    )
    long_text_split_enabled: bool = Field(
        default=True,
        description="长文本是否在 TTS 内部按自然句与有界片段顺序合成，再拼成一条音频",
    )
    segment_max_units: int = Field(
        default=24,
        ge=16,
        le=200,
        description=(
            "不具备原生切分合同的 TTS transport 单片段近似语音单位硬上限；"
            "中日韩字符按一单位、连续拉丁数字按约四字符一单位估算。"
            "legacy_compat 把完整表达交给后端原生 text_split_method"
        ),
    )
    segment_min_units: int = Field(
        default=8,
        ge=1,
        le=64,
        description="允许与相邻片段合并的短片段阈值，不改变正文与句子顺序",
    )
    segment_concurrency: int = Field(
        default=2,
        ge=1,
        le=4,
        description=(
            "vLLM-Omni 单条长表达的片段并发上限；legacy_compat 始终串行，"
            "所有结果仍按原顺序拼接"
        ),
    )
    idle_shutdown_seconds: float = Field(
        default=1800.0,
        ge=0.0,
        le=86_400.0,
        description=(
            "插件自有 TTS 服务在最后一条完整表达结束后的闲置关闭秒数；"
            "0 表示保持常驻。外部运行的服务永不由插件关闭"
        ),
    )
    phrase_pause_ms: int = Field(
        default=120,
        ge=0,
        le=2_000,
        description="逗号、顿号或工程硬切片之间追加的停顿毫秒数",
    )
    clause_pause_ms: int = Field(
        default=200,
        ge=0,
        le=2_000,
        description="分号、冒号后追加的停顿毫秒数",
    )
    sentence_pause_ms: int = Field(
        default=320,
        ge=0,
        le=2_000,
        description="句号、问号、感叹号后追加的停顿毫秒数",
    )
    paragraph_pause_ms: int = Field(
        default=520,
        ge=0,
        le=3_000,
        description="段落边界后追加的停顿毫秒数",
    )
    auto_start: bool = Field(
        default=True,
        description="调用前若服务未运行，是否自动拉起 TTS 服务进程",
    )
    legacy_owned_startup_weights_ready: bool = Field(
        default=False,
        description=(
            "仅 legacy_compat 可用：部署者声明 start_command 启动的自有进程已经加载"
            " default 风格配置中的 GPT/SoVITS 权重。只有插件刚启动且仍持有该精确"
            "进程时才会据此跳过第一次重复加载；外部服务永不信任此声明。"
            "启动脚本不能保证该条件时必须保持 false"
        ),
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
        description="主参考音频路径；允许时长由当前本地 TTS 后端决定",
    )
    voice: str = Field(
        default="",
        description=(
            "可选的 vLLM-Omni 已上传命名音色；设置后优先于每次传输参考音频"
        ),
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
    gpt_weights_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-fA-F]{64})$",
        description="GPT 权重的 SHA-256；legacy_compat 合成前必须匹配",
    )
    sovits_weights_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-fA-F]{64})$",
        description="SoVITS 权重的 SHA-256；legacy_compat 合成前必须匹配",
    )
    speed_factor: float = Field(
        default=0.9,
        ge=0.5,
        le=2.0,
        description=(
            "语速因子；vLLM-Omni 中数值越大越快，legacy_compat 沿用后端既有语义"
        ),
    )
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
    sample_steps: int = Field(
        default=32,
        ge=1,
        description="GPT-SoVITS V3 采样步数；v2ProPlus 不消费该质量旋钮",
    )
    super_sampling: bool = Field(
        default=False,
        description="GPT-SoVITS V3 超采样；v2ProPlus 不消费该质量旋钮",
    )
    seed: int = Field(
        default=-1,
        ge=-1,
        description="GPT-SoVITS 语义采样种子；-1 表示每次随机",
    )
    text_normalization: bool = Field(
        default=True,
        description="是否启用 IndexTTS2.5 文本规范化；仅 vLLM-Omni 消费",
    )


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
