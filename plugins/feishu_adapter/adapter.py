"""Feishu HTTP callback adapter."""

from __future__ import annotations

import base64
import asyncio
import json
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

import httpx
from mofox_wire import CoreSink, MessageEnvelope

from src.core.components.base.adapter import BaseAdapter
from src.kernel.logger import get_logger

from .config import FeishuAdapterConfig

logger = get_logger("FeishuAdapter", color="#00D6B9")

PLATFORM = "feishu"
_ADAPTER_INSTANCE: "FeishuAdapter | None" = None


def get_feishu_adapter() -> "FeishuAdapter | None":
    return _ADAPTER_INSTANCE


def set_feishu_adapter(adapter: "FeishuAdapter | None") -> None:
    global _ADAPTER_INSTANCE
    _ADAPTER_INSTANCE = adapter


class FeishuAdapter(BaseAdapter):
    """Feishu self-built app adapter.

    入方向：HTTP event callback -> MessageEnvelope -> CoreSink。
    出方向：life_chatter 回复 -> Feishu IM message API。
    """

    adapter_name = "feishu_adapter"
    adapter_version = "0.1.0"
    adapter_description = "Feishu self-built app HTTP callback adapter"
    platform = PLATFORM
    run_in_subprocess = False

    def __init__(self, core_sink: CoreSink, plugin: Any | None = None, **kwargs: Any) -> None:
        super().__init__(core_sink, plugin=plugin, transport=None, **kwargs)
        self._tenant_access_token: str = ""
        self._tenant_access_token_expires_at: float = 0.0
        self._seen_event_ids: list[str] = []
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._long_connection_thread: threading.Thread | None = None
        self._long_connection_client: Any | None = None
        set_feishu_adapter(self)
        logger.info("FeishuAdapter 初始化完成")

    async def on_adapter_loaded(self) -> None:
        config = self._config()
        if not config.plugin.enabled:
            logger.info("FeishuAdapter 已禁用")
            return
        if not config.app.app_id or not config.app.app_secret:
            logger.warning("FeishuAdapter 缺少 app_id/app_secret；入站可接收，出站发送会失败")
        if (
            config.connection.subscription_mode == "long_connection"
            and config.connection.auto_start_long_connection
        ):
            self._start_long_connection()
        logger.info("FeishuAdapter 已加载，等待飞书事件回调")

    async def on_adapter_unloaded(self) -> None:
        await self._stop_long_connection()
        set_feishu_adapter(None)
        self._tenant_access_token = ""
        self._tenant_access_token_expires_at = 0.0
        self._seen_event_ids.clear()
        logger.info("FeishuAdapter 已关闭")

    async def health_check(self) -> bool:
        return self._config().plugin.enabled

    def is_connected(self) -> bool:  # type: ignore[override]
        config = self._config()
        if not config.plugin.enabled:
            return False
        if config.connection.subscription_mode == "long_connection":
            thread = self._long_connection_thread
            return bool(thread and thread.is_alive())
        return True

    async def get_bot_info(self) -> dict[str, str]:  # type: ignore[override]
        config = self._config()
        return {
            "bot_id": config.bot.bot_open_id or "feishu_bot",
            "bot_name": config.bot.bot_name or "爱莉",
        }

    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle a Feishu event callback payload."""
        if not self._config().plugin.enabled:
            return {"success": False, "ignored": True, "reason": "adapter_disabled"}

        if "encrypt" in payload:
            raise ValueError("Feishu encrypted callbacks are not supported yet; disable Encrypt Key first")

        envelope = await self.from_platform_message(payload)
        if envelope is None:
            return {"success": True, "ignored": True}
        await self.core_sink.send(envelope)
        return {"success": True, "message_id": envelope["message_info"]["message_id"]}

    async def send_message(self, raw_message: dict[str, Any]) -> dict[str, Any]:
        """Local test helper: inject a normalized Feishu-like text message."""
        envelope = await self.from_platform_message(raw_message)
        if envelope is None:
            raise ValueError("无法转换飞书消息")
        await self.core_sink.send(envelope)
        return raw_message

    def _start_long_connection(self) -> None:
        config = self._config()
        if not config.app.app_id or not config.app.app_secret:
            logger.warning("飞书长连接未启动：缺少 app_id/app_secret")
            return
        if self._long_connection_thread and self._long_connection_thread.is_alive():
            logger.info("飞书长连接已经在运行")
            return

        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._main_loop = None

        thread = threading.Thread(
            target=self._run_long_connection_client,
            name="feishu_long_connection",
            daemon=True,
        )
        self._long_connection_thread = thread
        thread.start()
        logger.info("飞书长连接后台线程已启动")

    async def _stop_long_connection(self) -> None:
        client = self._long_connection_client
        self._long_connection_client = None
        if client is None:
            return
        # lark-oapi 的 ws.Client 当前没有公开 stop()。这里尽力断开底层连接；
        # daemon 线程会在进程退出时自动释放，插件重载时由运行状态检查避免重复启动。
        try:
            disconnect = getattr(client, "_disconnect", None)
            if disconnect is not None:
                await asyncio.to_thread(self._run_sdk_disconnect, client)
        except Exception as exc:
            logger.warning(f"飞书长连接关闭失败: {exc}")

    @staticmethod
    def _run_sdk_disconnect(client: Any) -> None:
        try:
            import lark_oapi.ws.client as ws_client_module

            ws_client_module.loop.run_until_complete(client._disconnect())
        except Exception:
            raise

    def _run_long_connection_client(self) -> None:
        try:
            import lark_oapi as lark
            import lark_oapi.ws as lark_ws
        except Exception as exc:
            logger.error(
                f"飞书长连接启动失败：缺少 lark-oapi。请安装 `pip install lark-oapi`。error={exc}",
                exc_info=True,
            )
            return

        config = self._config()
        event_handler = self._build_lark_event_handler(lark)
        log_level = getattr(lark.LogLevel, config.connection.long_connection_log_level, lark.LogLevel.INFO)
        client = lark_ws.Client(
            app_id=config.app.app_id,
            app_secret=config.app.app_secret,
            log_level=log_level,
            event_handler=event_handler,
            domain=config.app.api_base_url,
            auto_reconnect=True,
            source="neo-mofox-feishu-adapter",
        )
        self._long_connection_client = client
        logger.info("飞书长连接正在连接开放平台")
        try:
            client.start()
        except Exception as exc:
            logger.error(f"飞书长连接已退出: {exc}", exc_info=True)

    def _build_lark_event_handler(self, lark_module: Any) -> Any:
        config = self._config()

        def on_message(event: Any) -> None:
            payload = self._lark_event_to_payload(event)
            loop = self._main_loop
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self.handle_event(payload), loop)
                try:
                    future.result(timeout=30)
                except Exception as exc:
                    logger.error(f"飞书长连接事件投递失败: {exc}", exc_info=True)
            else:
                asyncio.run(self.handle_event(payload))

        return (
            lark_module.EventDispatcherHandler
            .builder(config.app.encrypt_key, config.app.verification_token)
            .register_p2_im_message_receive_v1(on_message)
            .build()
        )

    @staticmethod
    def _lark_event_to_payload(event: Any) -> dict[str, Any]:
        try:
            from lark_oapi.core.json import JSON

            serialized = JSON.marshal(event)
            if isinstance(serialized, str):
                loaded = json.loads(serialized)
                if isinstance(loaded, dict):
                    return loaded
        except Exception:
            pass

        header = getattr(event, "header", None)
        data = getattr(event, "event", None)
        sender = getattr(data, "sender", None)
        message = getattr(data, "message", None)
        sender_id = getattr(sender, "sender_id", None)
        return {
            "schema": "2.0",
            "header": {
                "event_id": str(getattr(header, "event_id", "") or ""),
                "event_type": str(getattr(header, "event_type", "im.message.receive_v1") or ""),
                "token": str(getattr(header, "token", "") or ""),
            },
            "event": {
                "sender": {
                    "sender_type": str(getattr(sender, "sender_type", "") or ""),
                    "sender_id": {
                        "open_id": str(getattr(sender_id, "open_id", "") or ""),
                        "user_id": str(getattr(sender_id, "user_id", "") or ""),
                        "union_id": str(getattr(sender_id, "union_id", "") or ""),
                    },
                },
                "message": {
                    "message_id": str(getattr(message, "message_id", "") or ""),
                    "root_id": str(getattr(message, "root_id", "") or ""),
                    "parent_id": str(getattr(message, "parent_id", "") or ""),
                    "create_time": getattr(message, "create_time", None),
                    "chat_id": str(getattr(message, "chat_id", "") or ""),
                    "chat_type": str(getattr(message, "chat_type", "") or ""),
                    "message_type": str(getattr(message, "message_type", "") or ""),
                    "content": getattr(message, "content", "") or "",
                    "mentions": getattr(message, "mentions", None) or [],
                },
            },
        }

    async def from_platform_message(  # type: ignore[override]
        self,
        raw: dict[str, Any],
    ) -> MessageEnvelope | None:
        try:
            normalized = self._normalize_incoming(raw)
            if normalized is None:
                return None
            if not self._should_process(normalized):
                return None

            message_id = normalized["message_id"]
            chat_id = normalized["chat_id"]
            chat_type = normalized["chat_type"]
            open_id = normalized["open_id"]
            user_id = normalized.get("user_id", "")
            union_id = normalized.get("union_id", "")
            sender_name = normalized["sender_name"]
            content = normalized["content"]
            timestamp = normalized["timestamp"]
            media_refs = normalized.get("media_refs") or []

            extra: dict[str, Any] = {
                "source": "feishu",
                "feishu_event_id": normalized.get("event_id", ""),
                "feishu_message_id": message_id,
                "feishu_chat_id": chat_id,
                "chat_id": chat_id,
                "open_id": open_id,
                "feishu_open_id": open_id,
                "feishu_user_id": user_id,
                "feishu_union_id": union_id,
                "sender_type": normalized.get("sender_type", ""),
                "feishu_message_type": normalized.get("message_type", ""),
                "format_info": {"accept_format": ["text", "image"]},
            }
            if media_refs:
                extra["feishu_media_refs"] = media_refs
            if chat_type == "group":
                extra["target_group_id"] = chat_id
            else:
                extra["target_user_id"] = open_id

            message_info: dict[str, Any] = {
                "platform": PLATFORM,
                "message_id": message_id,
                "time": timestamp,
                "user_info": {
                    "platform": PLATFORM,
                    "user_id": open_id,
                    "user_nickname": sender_name,
                },
                "extra": extra,
            }
            if chat_type == "group":
                message_info["group_info"] = {
                    "platform": PLATFORM,
                    "group_id": chat_id,
                    "group_name": normalized.get("chat_name") or chat_id,
                }

            segments: list[dict[str, Any]] = []
            if normalized.get("root_message_id"):
                segments.append({"type": "reply", "data": normalized["root_message_id"]})
            segments.extend(self._mention_segments(normalized.get("mentions") or []))
            media_segments = await self._download_incoming_media_segments(message_id, media_refs)
            if media_segments:
                if content and content != "[图片]":
                    segments.append({"type": "text", "data": content})
                segments.extend(media_segments)
            elif content:
                segments.append({"type": "text", "data": content})

            envelope: MessageEnvelope = {  # type: ignore[typeddict-item]
                "direction": "incoming",
                "message_info": message_info,
                "message_segment": segments,  # type: ignore[typeddict-item]
                "metadata": {"raw": raw, "feishu": True},
            }
            logger.info(
                f"收到飞书消息: chat_type={chat_type} sender={sender_name} content={content[:80]}"
            )
            return envelope
        except Exception as exc:
            logger.error(f"飞书消息转换失败: {exc}", exc_info=True)
            return None

    async def _send_platform_message(  # type: ignore[override]
        self,
        envelope: MessageEnvelope,
    ) -> None:
        outgoing = self._extract_outgoing_message(envelope)
        text = outgoing["text"]
        reply_to = outgoing["reply_to"]
        voice_data = outgoing["voice_data"]

        if not text and not voice_data:
            logger.info("飞书出站消息为空，跳过发送")
            return

        message_info = envelope.get("message_info", {}) or {}
        group_info = message_info.get("group_info") or {}
        user_info = message_info.get("user_info") or {}
        chat_id = str(group_info.get("group_id") or "")
        open_id = str(user_info.get("user_id") or "")

        if voice_data:
            await self._send_audio_message(
                chat_id=chat_id,
                open_id=open_id,
                reply_to=reply_to,
                voice_data=voice_data,
            )
            return

        if self._config().behavior.reply_to_message and reply_to:
            await self._reply_text(reply_to, text)
            logger.info(f"飞书引用回复发送成功: reply_to={reply_to} text={text[:80]}")
            return

        if chat_id:
            await self._send_text(receive_id_type="chat_id", receive_id=chat_id, text=text)
            logger.info(f"飞书群消息发送成功: chat_id={chat_id} text={text[:80]}")
            return

        if open_id:
            await self._send_text(receive_id_type="open_id", receive_id=open_id, text=text)
            logger.info(f"飞书私聊消息发送成功: open_id={open_id} text={text[:80]}")
            return

        raise ValueError("飞书出站消息缺少 chat_id/open_id，无法确定发送目标")

    def verify_callback_token(self, payload: dict[str, Any]) -> bool:
        expected = self._config().app.verification_token
        if not expected:
            return True
        token = str(payload.get("token") or payload.get("header", {}).get("token") or "")
        return token == expected

    def _config(self) -> FeishuAdapterConfig:
        if self.plugin and isinstance(getattr(self.plugin, "config", None), FeishuAdapterConfig):
            return cast(FeishuAdapterConfig, self.plugin.config)
        return FeishuAdapterConfig()

    def _normalize_incoming(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        if self._is_event_duplicate(raw):
            return None

        if "event" in raw and "header" in raw:
            event = raw.get("event") or {}
            header = raw.get("header") or {}
            message = event.get("message") or {}
            sender = event.get("sender") or {}
            sender_id = sender.get("sender_id") or {}
            message_type = str(message.get("message_type") or "")
            content_text = self._parse_content_text(
                message_type=message_type,
                content=message.get("content"),
            )
            media_refs = self._extract_incoming_media_refs(
                message_type=message_type,
                content=message.get("content"),
            )
            if not content_text and not media_refs:
                return None

            chat_type = "private" if message.get("chat_type") == "p2p" else "group"
            return {
                "event_id": str(header.get("event_id") or ""),
                "message_id": str(message.get("message_id") or f"feishu_{uuid.uuid4().hex}"),
                "message_type": message_type,
                "chat_id": str(message.get("chat_id") or ""),
                "chat_type": chat_type,
                "chat_name": str(message.get("chat_name") or ""),
                "open_id": str(sender_id.get("open_id") or sender_id.get("user_id") or ""),
                "user_id": str(sender_id.get("user_id") or ""),
                "union_id": str(sender_id.get("union_id") or ""),
                "sender_name": self._sender_name(sender, sender_id),
                "sender_type": str(sender.get("sender_type") or ""),
                "content": content_text,
                "timestamp": self._parse_time(message.get("create_time")),
                "mentions": message.get("mentions") or [],
                "root_message_id": message.get("root_id") or message.get("parent_id") or "",
                "media_refs": media_refs,
            }

        # Normalized/local test payload.
        message_type = str(raw.get("message_type") or "text")
        media_refs = self._extract_incoming_media_refs(
            message_type=message_type,
            content=raw.get("content"),
        )
        return {
            "event_id": str(raw.get("event_id") or ""),
            "message_id": str(raw.get("message_id") or f"feishu_local_{uuid.uuid4().hex}"),
            "message_type": message_type,
            "chat_id": str(raw.get("chat_id") or raw.get("group_id") or ""),
            "chat_type": str(raw.get("chat_type") or "private"),
            "chat_name": str(raw.get("chat_name") or ""),
            "open_id": str(raw.get("open_id") or raw.get("user_id") or ""),
            "user_id": str(raw.get("user_id") or ""),
            "union_id": str(raw.get("union_id") or ""),
            "sender_name": self._sender_name(raw, raw),
            "sender_type": str(raw.get("sender_type") or "user"),
            "content": str(raw.get("content") or ""),
            "timestamp": float(raw.get("timestamp") or time.time()),
            "mentions": raw.get("mentions") or [],
            "root_message_id": raw.get("root_message_id") or raw.get("reply_to") or "",
            "media_refs": raw.get("media_refs") or media_refs,
        }

    def _is_event_duplicate(self, raw: dict[str, Any]) -> bool:
        event_message_id = ""
        event = raw.get("event")
        if isinstance(event, dict):
            message = event.get("message")
            if isinstance(message, dict):
                event_message_id = str(message.get("message_id") or "")
        dedupe_key = event_message_id or str(
            raw.get("message_id") or raw.get("header", {}).get("event_id") or raw.get("event_id") or ""
        )
        if not dedupe_key:
            return False
        if dedupe_key in self._seen_event_ids:
            logger.debug(f"忽略重复飞书消息事件: {dedupe_key}")
            return True
        self._seen_event_ids.append(dedupe_key)
        if len(self._seen_event_ids) > 2000:
            del self._seen_event_ids[:500]
        return False

    def _should_process(self, message: dict[str, Any]) -> bool:
        config = self._config()
        if config.behavior.ignore_bot_messages:
            sender_type = str(message.get("sender_type") or "")
            open_id = str(message.get("open_id") or "")
            if sender_type == "app" or (config.bot.bot_open_id and open_id == config.bot.bot_open_id):
                logger.debug("忽略飞书 Bot 自身消息")
                return False

        if message.get("chat_type") == "group":
            return self._in_list_mode(
                value=str(message.get("chat_id") or ""),
                mode=config.behavior.group_list_type,
                items=config.behavior.group_list,
            )
        return self._in_list_mode(
            value=str(message.get("open_id") or ""),
            mode=config.behavior.private_list_type,
            items=config.behavior.private_list,
        )

    @staticmethod
    def _in_list_mode(value: str, mode: str, items: list[str]) -> bool:
        normalized = {str(item) for item in items}
        if mode == "whitelist":
            return value in normalized
        return value not in normalized

    @staticmethod
    def _parse_content_payload(content: Any) -> Any:
        parsed: Any = content
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = {"text": content}
        return parsed

    @staticmethod
    def _parse_content_text(message_type: str, content: Any) -> str:
        parsed = FeishuAdapter._parse_content_payload(content)
        if not isinstance(parsed, dict):
            return str(parsed or "")
        if message_type == "text":
            return str(parsed.get("text") or "")
        if message_type == "post":
            return FeishuAdapter._flatten_post_content(parsed)
        if message_type:
            if message_type == "image" and parsed.get("image_key"):
                return "[图片]"
            return f"[飞书{message_type}消息: {json.dumps(parsed, ensure_ascii=False)[:300]}]"
        return str(parsed.get("text") or parsed.get("content") or "")

    @staticmethod
    def _extract_incoming_media_refs(message_type: str, content: Any) -> list[dict[str, str]]:
        parsed = FeishuAdapter._parse_content_payload(content)
        refs: list[dict[str, str]] = []
        if not isinstance(parsed, dict):
            return refs

        if message_type == "image":
            image_key = str(parsed.get("image_key") or "").strip()
            if image_key:
                refs.append({"type": "image", "key": image_key})
            return refs

        if message_type == "post":
            refs.extend(FeishuAdapter._extract_post_image_refs(parsed))
        return refs

    @staticmethod
    def _extract_post_image_refs(content: dict[str, Any]) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        post = content.get("post")
        if not isinstance(post, dict):
            return refs

        for locale_content in post.values():
            if not isinstance(locale_content, dict):
                continue
            for line in locale_content.get("content") or []:
                if not isinstance(line, list):
                    continue
                for item in line:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("tag") or "") not in {"img", "image"}:
                        continue
                    image_key = str(item.get("image_key") or "").strip()
                    if image_key:
                        refs.append({"type": "image", "key": image_key})
        return refs

    async def _download_incoming_media_segments(
        self,
        message_id: str,
        media_refs: list[Any],
    ) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        for media_ref in media_refs:
            if not isinstance(media_ref, dict):
                continue
            media_type = str(media_ref.get("type") or "").strip()
            media_key = str(media_ref.get("key") or "").strip()
            if media_type != "image" or not media_key:
                continue
            try:
                image_base64 = await self._download_message_resource_as_base64(
                    message_id=message_id,
                    resource_key=media_key,
                    resource_type="image",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"飞书图片下载失败，保留文本占位: message_id={message_id}, image_key={media_key}, error={exc}"
                )
                continue
            segments.append({"type": "image", "data": image_base64})
        return segments

    async def _download_message_resource_as_base64(
        self,
        *,
        message_id: str,
        resource_key: str,
        resource_type: str,
    ) -> str:
        token = await self._get_tenant_access_token()
        url = self._api_url(
            f"/open-apis/im/v1/messages/{message_id}/resources/{resource_key}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                params={"type": resource_type},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )

        content_type = str(resp.headers.get("content-type") or "").lower()
        if resp.status_code >= 400:
            if "json" in content_type:
                raise RuntimeError(f"Feishu resource API failed: {self._decode_response(resp)}")
            raise RuntimeError(f"Feishu resource API http error: status={resp.status_code}")
        if "json" in content_type:
            data = self._decode_response(resp)
            if int(data.get("code", 0)) != 0:
                raise RuntimeError(f"Feishu resource API failed: {data}")
            raise RuntimeError(f"Feishu resource API returned json without binary resource: {data}")
        if not resp.content:
            raise RuntimeError("Feishu resource API returned empty body")
        return base64.b64encode(resp.content).decode("ascii")

    @staticmethod
    def _flatten_post_content(content: dict[str, Any]) -> str:
        parts: list[str] = []
        post = content.get("post")
        if isinstance(post, dict):
            for locale_content in post.values():
                if not isinstance(locale_content, dict):
                    continue
                title = locale_content.get("title")
                if title:
                    parts.append(str(title))
                for line in locale_content.get("content") or []:
                    if not isinstance(line, list):
                        continue
                    for item in line:
                        if isinstance(item, dict) and item.get("text"):
                            parts.append(str(item["text"]))
        return "\n".join(part for part in parts if part).strip()

    def _sender_name(self, sender: dict[str, Any], sender_id: dict[str, Any]) -> str:
        alias = self._sender_name_alias(sender, sender_id)
        if alias:
            return alias

        for key in ("sender_name", "name", "union_id", "user_id", "open_id"):
            value = sender.get(key) or sender_id.get(key)
            if value:
                return str(value)
        return "Feishu User"

    def _sender_name_alias(self, sender: dict[str, Any], sender_id: dict[str, Any]) -> str:
        aliases = self._parse_user_name_aliases(self._config().identity.user_name_aliases)
        if not aliases:
            return ""

        for key in ("open_id", "user_id", "union_id"):
            value = str(sender_id.get(key) or sender.get(key) or "").strip()
            if value and value in aliases:
                return aliases[value]
        return ""

    @staticmethod
    def _parse_user_name_aliases(items: list[str]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for item in items:
            raw = str(item or "").strip()
            if not raw or "=" not in raw:
                continue
            key, name = raw.split("=", 1)
            key = key.strip()
            name = name.strip()
            if key and name:
                aliases[key] = name
        return aliases

    @staticmethod
    def _parse_time(raw_time: Any) -> float:
        if raw_time is None:
            return time.time()
        try:
            value = float(raw_time)
        except (TypeError, ValueError):
            return time.time()
        if value > 10_000_000_000:
            return value / 1000.0
        return value

    @staticmethod
    def _mention_segments(mentions: list[Any]) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_id = mention.get("id") or {}
            user_id = mention_id.get("open_id") if isinstance(mention_id, dict) else mention_id
            if not user_id:
                continue
            segments.append({
                "type": "at",
                "data": str(user_id),
                "name": str(mention.get("name") or ""),
            })
        return segments

    @staticmethod
    def _extract_outgoing_text(envelope: MessageEnvelope) -> tuple[str, str]:
        outgoing = FeishuAdapter._extract_outgoing_message(envelope)
        return outgoing["text"], outgoing["reply_to"]

    @staticmethod
    def _extract_outgoing_message(envelope: MessageEnvelope) -> dict[str, Any]:
        segments = envelope.get("message_segment", []) or []
        if isinstance(segments, dict):
            segments = [segments]
        text_parts: list[str] = []
        reply_to = ""
        voice_data = ""
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data")
            if seg_type == "reply" and not reply_to:
                reply_to = str(data or "")
            elif seg_type == "text":
                text_parts.append(str(data or ""))
            elif seg_type == "voice" and not voice_data:
                voice_data = FeishuAdapter._stringify_media_data(data)
        return {
            "text": "".join(text_parts).strip(),
            "reply_to": reply_to,
            "voice_data": voice_data,
        }

    async def _send_audio_message(
        self,
        *,
        chat_id: str,
        open_id: str,
        reply_to: str,
        voice_data: str,
    ) -> None:
        try:
            file_key, duration_ms = await self._upload_audio(voice_data)
        except Exception as exc:
            logger.error(f"飞书语音上传失败，将降级为文本提示: {exc}", exc_info=True)
            fallback_text = "[语音发送失败：飞书音频上传没有成功]"
            if self._config().behavior.reply_to_message and reply_to:
                await self._reply_text(reply_to, fallback_text)
                return
            if chat_id:
                await self._send_text("chat_id", chat_id, fallback_text)
                return
            if open_id:
                await self._send_text("open_id", open_id, fallback_text)
                return
            raise ValueError("飞书出站语音缺少 chat_id/open_id，无法确定发送目标") from exc

        if self._config().behavior.reply_to_message and reply_to:
            await self._reply_audio(reply_to, file_key, duration_ms)
            logger.info(f"飞书引用语音发送成功: reply_to={reply_to} file_key={file_key}")
            return

        if chat_id:
            await self._send_audio(
                receive_id_type="chat_id",
                receive_id=chat_id,
                file_key=file_key,
                duration_ms=duration_ms,
            )
            logger.info(f"飞书群语音发送成功: chat_id={chat_id} file_key={file_key}")
            return

        if open_id:
            await self._send_audio(
                receive_id_type="open_id",
                receive_id=open_id,
                file_key=file_key,
                duration_ms=duration_ms,
            )
            logger.info(f"飞书私聊语音发送成功: open_id={open_id} file_key={file_key}")
            return

        raise ValueError("飞书出站语音缺少 chat_id/open_id，无法确定发送目标")

    async def _upload_audio(self, voice_data: str) -> tuple[str, int]:
        audio_bytes = self._decode_media_data(voice_data)
        opus_bytes, duration_ms = await asyncio.to_thread(self._convert_audio_to_opus, audio_bytes)
        token = await self._get_tenant_access_token()
        url = self._api_url("/open-apis/im/v1/files")
        filename = "voice.opus"
        files = {
            "file": (
                filename,
                opus_bytes,
                "audio/opus",
            ),
        }
        data = {
            "file_type": "opus",
            "file_name": filename,
            "duration": str(duration_ms),
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data=data,
                files=files,
            )
        payload = self._decode_response(resp)
        file_key = str((payload.get("data") or {}).get("file_key") or "")
        if not file_key:
            raise ValueError(f"飞书文件上传响应缺少 file_key: {payload}")
        return file_key, duration_ms

    async def _reply_audio(self, message_id: str, file_key: str, duration_ms: int) -> dict[str, Any]:
        return await self._post_json(
            f"/open-apis/im/v1/messages/{message_id}/reply",
            {
                "msg_type": "audio",
                "content": json.dumps(
                    {"file_key": file_key, "duration": duration_ms},
                    ensure_ascii=False,
                ),
            },
        )

    async def _send_audio(
        self,
        receive_id_type: str,
        receive_id: str,
        file_key: str,
        duration_ms: int,
    ) -> dict[str, Any]:
        return await self._post_json(
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "audio",
                "content": json.dumps(
                    {"file_key": file_key, "duration": duration_ms},
                    ensure_ascii=False,
                ),
            },
        )

    @staticmethod
    def _convert_audio_to_opus(audio_bytes: bytes) -> tuple[bytes, int]:
        if not audio_bytes:
            raise ValueError("音频数据为空")

        with tempfile.TemporaryDirectory(prefix="feishu_audio_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "input_audio"
            output_path = tmp_path / "voice.opus"
            input_path.write_bytes(audio_bytes)

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(input_path),
                    "-acodec",
                    "libopus",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(output_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            duration_ms = FeishuAdapter._probe_audio_duration_ms(output_path)
            return output_path.read_bytes(), duration_ms

    @staticmethod
    def _probe_audio_duration_ms(path: Path) -> int:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            seconds = float(result.stdout.strip() or "0")
            return max(1, int(seconds * 1000))
        except Exception:
            return 1

    @staticmethod
    def _stringify_media_data(data: Any) -> str:
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("data", "base64", "url", "path"):
                value = data.get(key)
                if value:
                    return str(value)
        return str(data or "")

    @staticmethod
    def _decode_media_data(data: str) -> bytes:
        raw = str(data or "").strip()
        if not raw:
            raise ValueError("音频数据为空")
        if raw.startswith("data:"):
            _, _, raw = raw.partition(",")
        if raw.startswith("base64|"):
            raw = raw.removeprefix("base64|")
        if raw.startswith(("http://", "https://")):
            raise ValueError("飞书语音暂不支持 URL 直传")
        if len(raw) < 4096:
            try:
                path = Path(raw)
                if path.exists() and path.is_file():
                    return path.read_bytes()
            except OSError:
                pass
        return base64.b64decode(raw, validate=False)

    async def _reply_text(self, message_id: str, text: str) -> dict[str, Any]:
        return await self._post_json(
            f"/open-apis/im/v1/messages/{message_id}/reply",
            {
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    async def _send_text(self, receive_id_type: str, receive_id: str, text: str) -> dict[str, Any]:
        return await self._post_json(
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._get_tenant_access_token()
        url = self._api_url(path)
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=body,
            )
        data = self._decode_response(resp)
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"Feishu API failed: path={path}, response={data}")
        return data

    async def _get_tenant_access_token(self) -> str:
        now = time.time()
        if self._tenant_access_token and self._tenant_access_token_expires_at > now:
            return self._tenant_access_token

        config = self._config()
        if not config.app.app_id or not config.app.app_secret:
            raise RuntimeError("Feishu app_id/app_secret 未配置")

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                self._api_url("/open-apis/auth/v3/tenant_access_token/internal"),
                json={
                    "app_id": config.app.app_id,
                    "app_secret": config.app.app_secret,
                },
            )
        data = self._decode_response(resp)
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"Feishu tenant_access_token failed: {data}")
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError(f"Feishu tenant_access_token missing: {data}")
        expire = float(data.get("expire") or 7200)
        self._tenant_access_token = token
        self._tenant_access_token_expires_at = now + max(60.0, expire - 300.0)
        return token

    def _api_url(self, path: str) -> str:
        base = self._config().app.api_base_url.rstrip("/")
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{base}{path}"

    @staticmethod
    def _decode_response(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Feishu API returned non-json: status={resp.status_code}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"Feishu API http error: status={resp.status_code}, response={data}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Feishu API returned invalid json: {data}")
        return data
