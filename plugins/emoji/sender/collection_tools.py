"""表情包仿生收藏工具：暴露给主体的自主收藏接口。

让她可以主动浏览最近收到的表情包、收藏喜欢的、跳过不想要的。
收藏与否完全是她的决定——这些工具只是把选择权交到她手里。

- nucleus_browse_memes: 浏览未收藏的表情包候选
- nucleus_collect_meme: 收藏一张喜欢的（可附"为什么喜欢"）
- nucleus_dismiss_meme: 跳过一张不想收藏的
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseTool

from .service import EmojiSenderService

logger = get_logger("emoji.collection_tools")

_EMOJI_SERVICE_SIGNATURE = "emoji:service:emoji_sender"


def _get_service() -> EmojiSenderService | None:
    svc = get_service(_EMOJI_SERVICE_SIGNATURE)
    if svc is None:
        return None
    return cast(EmojiSenderService, svc)


class BrowseMemesTool(BaseTool):
    """浏览最近收到的、还没收藏的表情包。"""

    tool_name: str = "nucleus_browse_memes"
    tool_description: str = (
        "翻看最近收到的表情包（还没收藏的）。返回每张的简短描述和 candidate_id。"
        "你可以看看有没有喜欢的，然后用 nucleus_collect_meme 收藏，或用 nucleus_dismiss_meme 跳过。"
        "完全随你心意，不想看也可以不看。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        limit: Annotated[int, "想看多少张，默认 8"] = 8,
    ) -> tuple[bool, str | dict]:
        service = _get_service()
        if service is None:
            return False, "表情包服务未加载"

        try:
            # 浏览前轻量感知一次，确保候选池是新鲜的（感知是前注意的，不是收藏决定）
            await service.perception_scan(max_scan=12)
            candidates = await service.browse_candidates(limit=int(limit or 8))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"浏览表情包失败: {exc}")
            return False, f"浏览表情包时出错: {exc}"

        if not candidates:
            return True, "最近没有新的表情包可看。"

        lines = [f"这里有 {len(candidates)} 张最近收到的表情包："]
        for i, c in enumerate(candidates, 1):
            brief = c.get("brief") or "（没有描述）"
            lines.append(f"{i}. [{c.get('candidate_id', '')[:12]}] {brief}")
        lines.append("喜欢哪张就用 nucleus_collect_meme 收藏（可以写下为什么喜欢），不想要就 nucleus_dismiss_meme 跳过。")
        return True, "\n".join(lines)


class CollectMemeTool(BaseTool):
    """收藏一张喜欢的表情包。"""

    tool_name: str = "nucleus_collect_meme"
    tool_description: str = (
        "收藏一张表情包到自己手里，以后就能在聊天时用它表达情绪了。"
        "需要提供 browse 时看到的 candidate_id，还可以写一句你为什么喜欢它。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        candidate_id: Annotated[str, "要收藏的表情包 candidate_id（browse 时看到的）"],
        note: Annotated[str, "你为什么喜欢它（可选，比如'这张怼人好用'）"] = "",
    ) -> tuple[bool, str | dict]:
        service = _get_service()
        if service is None:
            return False, "表情包服务未加载"

        cid = str(candidate_id or "").strip()
        if not cid:
            return False, "请告诉我要收藏哪张（candidate_id）。"

        try:
            # candidate_id 在 browse 里截断展示过，这里支持前缀匹配
            ok, msg = await service.collect_meme(cid, note=str(note or ""))
            if not ok and len(cid) <= 12:
                # 尝试按前缀补全 candidate_id
                full_id = await self._resolve_prefix(service, cid)
                if full_id:
                    ok, msg = await service.collect_meme(full_id, note=str(note or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"收藏表情包失败: {exc}")
            return False, f"收藏时出错: {exc}"

        return ok, msg

    @staticmethod
    async def _resolve_prefix(service: EmojiSenderService, prefix: str) -> str | None:
        try:
            candidates = await service.browse_candidates(limit=50)
            for c in candidates:
                if str(c.get("candidate_id", "")).startswith(prefix):
                    return str(c["candidate_id"])
        except Exception:  # noqa: BLE001
            pass
        return None


class DismissMemeTool(BaseTool):
    """跳过一张不想收藏的表情包。"""

    tool_name: str = "nucleus_dismiss_meme"
    tool_description: str = (
        "跳过一张不想收藏的表情包（browse 时看到的 candidate_id）。"
        "跳过之后它就不会再出现在浏览里了。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        candidate_id: Annotated[str, "要跳过的表情包 candidate_id"],
    ) -> tuple[bool, str | dict]:
        service = _get_service()
        if service is None:
            return False, "表情包服务未加载"

        cid = str(candidate_id or "").strip()
        if not cid:
            return False, "请告诉我要跳过哪张（candidate_id）。"

        try:
            ok, msg = await service.dismiss_meme(cid)
            if not ok and len(cid) <= 12:
                full_id = await CollectMemeTool._resolve_prefix(service, cid)
                if full_id:
                    ok, msg = await service.dismiss_meme(full_id)
        except Exception as exc:  # noqa: BLE001
            return False, f"跳过时出错: {exc}"

        return ok, msg


EMOJI_COLLECTION_TOOLS = [
    BrowseMemesTool,
    CollectMemeTool,
    DismissMemeTool,
]
