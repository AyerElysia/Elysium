"""生成阶段三 `/api/v1` 完整 OpenAPI schema 快照。

只注册路由（FastAPI ``include_router``），不执行任何 endpoint 函数体，
因此全部 provider 使用轻量假对象即可。产物写入 ``docs/api/openapi.json``，
供前端类型生成与文档浏览使用；schema 不含任何凭据或运行数据。

用法：
    python scripts/generate_api_openapi.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.werewolf_game.domain import WerewolfDomainService
from plugins.werewolf_game.ledger import WerewolfLedger
from src.app.api.v1.admin import AdminFacade
from src.app.api.v1.admin_store import AdminStore
from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.chat import ChatQueryService
from src.app.api.v1.events import EventQueryService
from src.app.api.v1.foundation import FoundationProjection
from src.app.api.v1.livestream import StaticLivestreamProvider
from src.app.api.v1.media_objects import ManagedMediaService, MediaObjectStore
from src.app.api.v1.p312 import P312Providers
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.tokens import SignedValueCodec
from src.kernel.commands import CommandDispatcher, CommandStore, HandlerRegistry


class _VoiceProvider:
    """仅满足路由注册所需的 VoiceCallProvider 形状。"""

    def router(self) -> None:
        return None

    def store(self, call_id: str) -> None:
        del call_id


def build_context(tmp: Path) -> APIContext:
    """构造注册全部阶段三域路由所需的完整 context。"""
    codec = SignedValueCodec("o" * 48)
    auth = AuthStore(":memory:", installation_id="schema-gen")
    admin_store = AdminStore(tmp / "admin.sqlite3")
    commands = CommandStore(tmp / "commands.sqlite3")
    registry = HandlerRegistry()
    dispatcher = CommandDispatcher(commands, registry=registry)
    foundation = FoundationProjection(node_id="schema-gen")
    media_store = MediaObjectStore(tmp / "media.sqlite3", tmp / "media")
    media = ManagedMediaService(media_store)
    tabletop_service = WerewolfDomainService(WerewolfLedger(tmp / "tabletop.sqlite3"))
    return APIContext(
        store=auth,
        codec=codec,
        installation_id="schema-gen",
        allowed_origins=("http://localhost:5173",),
        foundation=foundation,
        events=EventQueryService(node_id="schema-gen", codec=codec),
        chat=ChatQueryService(codec=codec, store_provider=lambda: None),
        media=media,
        command_store=commands,
        command_dispatcher=dispatcher,
        chat_commands_enabled=True,
        livestream=StaticLivestreamProvider(None, None),
        voice_calls=_VoiceProvider(),
        tabletop=tabletop_service,
        admin=AdminFacade(
            foundation=foundation,
            auth=auth,
            admin=admin_store,
            commands=commands,
        ),
        p312=P312Providers(),
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        context = build_context(Path(tmp))
        app = create_api_app(context)
        schema = app.openapi()
        try:
            context.media.store.close()
            context.command_store.close()
            context.tabletop.ledger.close()
            context.admin.admin.close()
        finally:
            context.store.close()
    target = Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    paths = schema.get("paths", {})
    operations = sum(
        1
        for methods in paths.values()
        for method in methods
        if method in {"get", "post", "put", "delete", "patch"}
    )
    print(f"wrote {target}")
    print(f"paths={len(paths)} operations={operations}")


if __name__ == "__main__":
    main()
