"""Elysia 生成表情包配置。"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


@config_section("plugin")
class PluginSection(SectionBase):
    """插件基础配置。"""

    enabled: bool = Field(default=False, description="是否启用 Elysia 生成表情包")


@config_section("api")
class ApiSection(SectionBase):
    """NovelAI API 配置。"""

    api_keys: list[str] = Field(
        default_factory=list,
        description="NovelAI Persistent API token 列表；也可用环境变量 NOVELAI_API_KEY/NOVELAI_TOKEN/NAI_API_KEY",
    )
    generate_url: str = Field(
        default="https://image.novelai.net/ai/generate-image",
        description="NovelAI 生图接口",
    )
    proxy: str = Field(default="", description="HTTP 代理，留空则不用")
    timeout_seconds: int = Field(default=180, description="单次生图超时时间")
    cooldown_seconds: int = Field(default=15, description="串行队列每次请求之间的最小冷却")
    notify_on_failure: bool = Field(default=True, description="后台生图失败时是否向当前会话发送失败提示")


@config_section("generation")
class GenerationSection(SectionBase):
    """生图参数。"""

    model: str = Field(default="nai-diffusion-4-5-curated", description="NovelAI 图像模型")
    steps: int = Field(default=28, description="采样步数")
    scale: float = Field(default=5.0, description="CFG 引导比例")
    cfg_rescale: float = Field(default=0.0, description="V4 cfg_rescale")
    sampler: str = Field(default="k_euler", description="采样器")
    noise_schedule: str = Field(default="karras", description="噪声调度")
    default_resolution: str = Field(default="1024x1024", description="默认画幅")
    output_dir: str = Field(default="data/elysia_generated_emoji/generated", description="生成表情包保存目录")
    negative_prompt: str = Field(
        default=(
            "nsfw, nude, naked, explicit, sexual, gore, worst quality, low quality, "
            "bad anatomy, bad hands, extra fingers, missing fingers, extra limbs, "
            "malformed limbs, disfigured, blurry, jpeg artifacts, watermark, logo, "
            "signature, username, text, speech bubble"
        ),
        description="全局负面提示词；文字由本地叠字完成，默认禁止模型直接生成文字",
    )


@config_section("identity")
class IdentitySection(SectionBase):
    """角色与表情风格锚定。"""

    character_prompt: str = Field(
        default=(
            "Elysia from Honkai Impact 3rd, long pastel pink hair, crystal blue eyes, "
            "delicate elf ears, elegant black and gold crystal hair ornaments, warm charming smile"
        ),
        description="爱莉外观锚点，会自动进入表情包 prompt",
    )
    studio_signature: str = Field(
        default=(
            "drawn as an original expressive companion illustration, polished anime art, "
            "clean composition, emotionally readable pose, no in-image text"
        ),
        description="生成表情包统一风格签名",
    )


@config_section("caption")
class CaptionSection(SectionBase):
    """本地叠字配置。"""

    enabled: bool = Field(default=True, description="是否允许本地叠字")
    max_chars: int = Field(default=18, description="caption 最大字符数")
    font_size: int = Field(default=52, description="基础字号")
    font_paths: list[str] = Field(
        default_factory=lambda: [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ],
        description="按顺序尝试的字体路径",
    )


class ElysiaGeneratedEmojiConfig(BaseConfig):
    """Elysia 生成表情包插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "Elysia 生成表情包配置"

    plugin: PluginSection = Field(default_factory=PluginSection)
    api: ApiSection = Field(default_factory=ApiSection)
    generation: GenerationSection = Field(default_factory=GenerationSection)
    identity: IdentitySection = Field(default_factory=IdentitySection)
    caption: CaptionSection = Field(default_factory=CaptionSection)
