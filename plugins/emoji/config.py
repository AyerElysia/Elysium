"""emoji 插件配置（合并自原 emoji_sender 与 elysia_generated_emoji）。

两套配置分别收纳在 sender / generated 命名空间下，互不干扰。
配置文件默认路径：config/plugins/emoji/config.toml
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class EmojiConfig(BaseConfig):
    """表情包统一插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "表情包插件配置（收藏发送 + 现场生成）"

    # ── sender：表情包收藏与发送（原 emoji_sender）──────────────

    @config_section("sender")
    class SenderSection(SectionBase):
        """表情包收藏与发送配置。"""

        @config_section("scheduler")
        class SchedulerSection(SectionBase):
            """调度相关配置。"""

            interval_seconds: int = Field(
                default=120,
                description="入库任务执行间隔（秒）",
            )

        @config_section("plugin")
        class PluginSection(SectionBase):
            """插件行为配置。"""

            inject_system_prompt: bool = Field(
                default=True,
                description="是否将表情包使用提示同步到 actor system reminder",
            )

        @config_section("prompt")
        class PromptSection(SectionBase):
            """自定义提示词配置。"""

            custom_instructions: str = Field(
                default="",
                description=(
                    "追加到 send_emoji_meme action 描述末尾的自定义指令。\n"
                    "可描述希望 AI 主动使用表情包的具体场景，"
                    "例如：在表达强烈情感、开玩笑或缓解尴尬时主动使用。\n"
                    "不会覆盖已有的触发条件，只是扩充场景说明。"
                ),
            )

        @config_section("ingest")
        class IngestSection(SectionBase):
            """入库相关配置。"""

            manual_memes_dir: str = Field(
                default="data/emoji_sender/manual_memes",
                description="手动放置表情包的目录（用法：关闭随机抽取，手动放表情包到此目录）",
            )

            sample_from_media_cache: bool = Field(
                default=True,
                description="是否从 data/media_cache/emojis 随机抽取候选表情包（关闭则使用手动目录，需要手动放置表情包）",
            )

        @config_section("vector")
        class VectorSection(SectionBase):
            """向量库相关配置。"""

            collection_name: str = Field(
                default="emoji_sender",
                description="向量集合名",
            )
            db_path: str = Field(
                default="data/emoji_sender/vector_db",
                description="向量数据库路径（ChromaDB）",
            )
            top_n: int = Field(
                default=8,
                description="检索候选数量 topN",
            )
            max_distance: float = Field(
                default=0.35,
                description="最大距离阈值（距离越小越相似）",
            )
            temperature: float = Field(
                default=0.3,
                description="检索结果采样温度（<=0 时固定选择最相似项，越大越随机）",
            )

        @config_section("storage")
        class StorageSection(SectionBase):
            """文件存储相关配置。"""

            data_dir: str = Field(
                default="data/emoji_sender/memes",
                description="插件表情包复制文件目录",
            )

            max_memes: int = Field(
                default=200,
                description="最大可用表情包数量上限（<=0 表示不限制）；达到上限后不再继续入库",
            )

        @config_section("visual")
        class VisualSection(SectionBase):
            """纯视觉检索相关配置（Qwen3-VL-Embedding 本地服务）。"""

            embed_enabled: bool = Field(
                default=True,
                description="是否启用纯视觉检索（关闭则回退，用于应急）",
            )
            embed_endpoint: str = Field(
                default="http://127.0.0.1:8848/v1/embeddings",
                description="视觉嵌入服务地址（OpenAI 兼容 /v1/embeddings）",
            )
            embed_dim: int = Field(
                default=2048,
                description="视觉嵌入向量维度（Qwen3-VL-Embedding-2B 默认 2048）",
            )
            collection_name: str = Field(
                default="emoji_sender_visual",
                description="视觉向量集合名（与旧文本库隔离，便于回退）",
            )
            query_instruction: str = Field(
                default="Retrieve a meme image that best expresses the given intent or emotion.",
                description="检索 query 的指令前缀（指令感知，提升文本意图→图像匹配效果）",
            )
            request_timeout: float = Field(
                default=60.0,
                description="调用视觉嵌入服务的超时时间（秒）",
            )
            match_min_cosine: float = Field(
                default=0.15,
                description="视觉检索的最低 cosine 相似度阈值（低于此值视为匹配不佳，但仍返回最接近的候选）",
            )

        @config_section("collection")
        class CollectionSection(SectionBase):
            """仿生收藏（候选池 + 图片库）相关配置。"""

            meme_db_path: str = Field(
                default="data/emoji/meme_candidates.db",
                description="候选池 SQLite 数据库路径（收到的表情包候选 + 浏览/收藏状态 + 溯源）",
            )
            meme_image_dir: str = Field(
                default="data/emoji/memes",
                description="收藏的表情包图片存储目录",
            )
            media_cache_dir: str = Field(
                default="data/media_cache/emojis",
                description="聊天收到表情包的来源目录（感知筛选扫描此处）",
            )
            browse_page_size: int = Field(
                default=8,
                description="browse_memes 一次返回的候选数量",
            )
            visual_dedup_threshold: float = Field(
                default=0.95,
                description="视觉去重阈值（cosine >= 此值视为近似重复，不重复收藏）",
            )

        scheduler: SchedulerSection = Field(default_factory=SchedulerSection)
        plugin: PluginSection = Field(default_factory=PluginSection)
        prompt: PromptSection = Field(default_factory=PromptSection)
        ingest: IngestSection = Field(default_factory=IngestSection)
        vector: VectorSection = Field(default_factory=VectorSection)
        storage: StorageSection = Field(default_factory=StorageSection)
        visual: VisualSection = Field(default_factory=VisualSection)
        collection: CollectionSection = Field(default_factory=CollectionSection)

    # ── generated：现场生成表情包（原 elysia_generated_emoji）────

    @config_section("generated")
    class GeneratedSection(SectionBase):
        """现场生成表情包配置。"""

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

        plugin: PluginSection = Field(default_factory=PluginSection)
        api: ApiSection = Field(default_factory=ApiSection)
        generation: GenerationSection = Field(default_factory=GenerationSection)
        identity: IdentitySection = Field(default_factory=IdentitySection)
        caption: CaptionSection = Field(default_factory=CaptionSection)

    sender: SenderSection = Field(default_factory=SenderSection)
    generated: GeneratedSection = Field(default_factory=GeneratedSection)
