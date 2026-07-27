"""Elysia 生成表情包动作。"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.send_api import send_image, send_text
from src.core.components.base.action import BaseAction
from src.core.components.types import ChatType
from src.kernel.concurrency import get_task_manager
from src.kernel.logger import get_logger

from ..config import EmojiConfig
from .prompt import list_style_names, resolve_style
from .service import ElysiaGeneratedEmojiService, EmojiGenerationRequest

logger = get_logger("emoji.generated.action")


class GenerateEmojiMemeAction(BaseAction):
    """生成一张新的表情包/视觉表达。"""

    action_name = "generate_emoji_meme"
    action_description = (
        "现场生成一张新的表情包/贴纸/聊天视觉表达。"
        "这个动作不从表情包数据库里选图，也不会回退旧表情包；每次都是生成。\n"
        "适合：用户明确要表情包、贴纸、反应图，或者你想用视觉方式表达抱抱、晚安、叉腰、"
        "委屈、夸张吐槽、工作台陪伴等非文字动作。\n"
        "只在用户消息触发的会话轮次使用，不要在无人触发的主动心跳里自动刷图。\n"
        f"可选 style：{', '.join(list_style_names())}。\n"
        "caption 会由本地后处理叠到图上，不要要求模型在画面内生成文字。"
    )
    primary_action = False
    chat_type = ChatType.ALL

    async def execute(
        self,
        intent: Annotated[
            str,
            "这张图要表达的核心意图，例如：安抚小星星、叉腰催睡、震惊吐槽、陪她调日志。",
        ],
        scene: Annotated[
            str,
            "具体画面描述，建议用英文或中英混合描述主体动作、表情、构图和氛围。",
        ],
        style: Annotated[
            str,
            "表情风格名。可选：chibi_sticker, soft_illustration, meme_reaction, sleepy_goodnight, angry_cute, workbench。",
        ] = "chibi_sticker",
        caption: Annotated[
            str,
            "要叠在图上的短文字，中文也可以；没有就留空。建议 2-10 个字。",
        ] = "",
        resolution: Annotated[
            str,
            "画幅：square/landscape/portrait/1:1/16:9/9:16 或 1024x1024 这种格式。",
        ] = "",
        negative_prompt: Annotated[
            str,
            "额外负面提示词，英文逗号分隔。无特殊需求留空。",
        ] = "",
    ) -> tuple[bool, str]:
        """开始后台绘制并在完成后发送。"""

        service = getattr(self.plugin, "emoji_service", None)
        if not isinstance(service, ElysiaGeneratedEmojiService):
            return False, "Elysia 生成表情包服务未加载"

        full_cfg = getattr(self.plugin, "config", None)
        if not isinstance(full_cfg, EmojiConfig) or not full_cfg.generated.plugin.enabled:
            return False, "Elysia 生成表情包未启用"
        cfg = full_cfg.generated

        preset = resolve_style(style)
        prompt = self._build_scene_prompt(intent=intent, scene=scene)
        request = EmojiGenerationRequest(
            prompt=prompt,
            style=preset,
            caption=caption,
            resolution=resolution,
            negative_prompt=negative_prompt,
        )
        stream_id = self.chat_stream.stream_id

        async def _paint_and_send() -> None:
            success, message, path = await service.generate(request)
            if success and path:
                try:
                    await send_image(
                        service.read_image_base64(path),
                        stream_id=stream_id,
                        processed_plain_text="[内部：已发送现场生成表情包]",
                    )
                    logger.info(f"生成表情包已发送: {path}")
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"生成表情包发送图片失败: {exc}", exc_info=exc)
                    if cfg.api.notify_on_failure:
                        await self._safe_send_text(stream_id, "表情包生成好了，但是发送图片时出错了。")
                return

            logger.error(f"生成表情包失败: {message}")
            if cfg.api.notify_on_failure:
                await self._safe_send_text(stream_id, f"表情包这次没生成出来：{message}")

        get_task_manager().create_task(_paint_and_send(), name=f"elysia_generated_emoji_{stream_id[:8]}")
        return True, "[内部：已开始现场生成表情包，完成后会自动发送图片]"

    @staticmethod
    def _build_scene_prompt(*, intent: str, scene: str) -> str:
        parts = []
        if intent.strip():
            parts.append(f"emotional intent: {intent.strip()}")
        if scene.strip():
            parts.append(scene.strip())
        return ", ".join(parts)

    @staticmethod
    async def _safe_send_text(stream_id: str, text: str) -> None:
        try:
            await send_text(text, stream_id=stream_id)
        except Exception:
            logger.debug("生成表情包失败提示发送失败", exc_info=True)
