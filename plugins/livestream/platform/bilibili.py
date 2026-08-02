"""Bounded Bilibili live-room protocol adapter.

The adapter owns the public web-room connection directly instead of relying on
an undeclared third-party daemon. It obtains a short-lived room token, performs
the binary WebSocket handshake, sends heartbeats, decodes zlib/brotli envelopes,
and emits source observations without assigning response priority.
"""

from __future__ import annotations

import asyncio
import json
import random
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import brotli
import httpx
from websockets.asyncio.client import connect as websocket_connect

from src.kernel.concurrency import get_task_manager
from src.kernel.concurrency.task_info import TaskInfo
from src.kernel.logger import get_logger

from ..domain import PlatformEvent
from .base import BasePlatformAdapter, PlatformHealth

logger = get_logger("livestream.bilibili", display="B站直播")

_HEADER = struct.Struct(">IHHII")
_HEADER_LENGTH = _HEADER.size
_OP_HEARTBEAT = 2
_OP_HEARTBEAT_REPLY = 3
_OP_EVENT = 5
_OP_AUTH = 7
_OP_AUTH_REPLY = 8
_DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
_DANMU_CONF_URL = "https://api.live.bilibili.com/room/v1/Danmu/getConf"
_MAX_DANMU_INFO_BYTES = 1024 * 1024


class BilibiliProtocolError(RuntimeError):
    """Raised when Bilibili returns an invalid or unsupported protocol frame."""


@dataclass(frozen=True, slots=True)
class BilibiliConnectionInfo:
    token: str
    websocket_url: str


def _pack_packet(operation: int, body: bytes = b"", *, version: int = 1) -> bytes:
    length = _HEADER_LENGTH + len(body)
    return _HEADER.pack(length, _HEADER_LENGTH, version, operation, 1) + body


def _decompress_zlib_bounded(body: bytes, limit: int) -> bytes:
    decoder = zlib.decompressobj()
    try:
        output = decoder.decompress(body, limit + 1)
    except zlib.error as exc:
        raise BilibiliProtocolError("invalid zlib Bilibili packet") from exc
    if len(output) > limit or decoder.unconsumed_tail:
        raise BilibiliProtocolError(
            f"decompressed Bilibili packet exceeds {limit} bytes"
        )
    if not decoder.eof or decoder.unused_data:
        raise BilibiliProtocolError("invalid zlib Bilibili packet")
    return output


def _decompress_brotli_bounded(body: bytes, limit: int) -> bytes:
    decoder = brotli.Decompressor()
    output = bytearray()
    try:
        # The Python Brotli API has no max-output argument. Small input slices
        # let us stop expansion as soon as the configured output bound is hit.
        for offset in range(0, len(body), 64):
            chunk = decoder.process(body[offset : offset + 64])
            if len(output) + len(chunk) > limit:
                raise BilibiliProtocolError(
                    f"decompressed Bilibili packet exceeds {limit} bytes"
                )
            output.extend(chunk)
    except brotli.error as exc:
        raise BilibiliProtocolError("invalid brotli Bilibili packet") from exc
    if not decoder.is_finished():
        raise BilibiliProtocolError("invalid brotli Bilibili packet")
    return bytes(output)


def _decode_packets(
    frame: bytes,
    *,
    max_packet_bytes: int,
    depth: int = 0,
) -> list[tuple[int, bytes]]:
    """Decode one frame, including bounded nested compression envelopes."""

    if depth > 4:
        raise BilibiliProtocolError("Bilibili packet nesting exceeds 4 levels")
    if len(frame) > max_packet_bytes:
        raise BilibiliProtocolError(
            f"Bilibili frame exceeds {max_packet_bytes} bytes"
        )
    offset = 0
    packets: list[tuple[int, bytes]] = []
    while offset < len(frame):
        if len(frame) - offset < _HEADER_LENGTH:
            raise BilibiliProtocolError("truncated Bilibili packet header")
        packet_length, header_length, version, operation, _sequence = _HEADER.unpack_from(
            frame, offset
        )
        if (
            header_length < _HEADER_LENGTH
            or packet_length < header_length
            or offset + packet_length > len(frame)
            or packet_length > max_packet_bytes
        ):
            raise BilibiliProtocolError("invalid Bilibili packet length")
        body = frame[offset + header_length : offset + packet_length]
        if version == 2:
            decompressed = _decompress_zlib_bounded(body, max_packet_bytes)
            packets.extend(
                _decode_packets(
                    decompressed,
                    max_packet_bytes=max_packet_bytes,
                    depth=depth + 1,
                )
            )
        elif version == 3:
            decompressed = _decompress_brotli_bounded(body, max_packet_bytes)
            packets.extend(
                _decode_packets(
                    decompressed,
                    max_packet_bytes=max_packet_bytes,
                    depth=depth + 1,
                )
            )
        elif version in {0, 1}:
            packets.append((operation, body))
        else:
            raise BilibiliProtocolError(
                f"unsupported Bilibili protocol version: {version}"
            )
        offset += packet_length
    return packets


def _source_timestamp(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return time.time()
    if parsed > 10_000_000_000:
        parsed /= 1000.0
    return parsed if parsed > 0 else time.time()


def _native_identity(payload: dict[str, Any], data: dict[str, Any]) -> str | None:
    for source in (data, payload):
        for name in ("id_str", "message_id", "msg_id", "id", "tid"):
            value = source.get(name)
            if value not in (None, "", 0, "0"):
                return str(value)
    return None


def _trusted_connection_info(
    token: str,
    hosts: Any,
) -> BilibiliConnectionInfo:
    if not token:
        raise BilibiliProtocolError("Bilibili connection response returned no token")
    if not isinstance(hosts, list) or not hosts:
        raise BilibiliProtocolError("Bilibili connection response returned no hosts")
    for host in hosts:
        if not isinstance(host, dict):
            continue
        host_name = str(host.get("host", "")).strip().rstrip(".").casefold()
        if not (
            host_name == "bilibili.com" or host_name.endswith(".bilibili.com")
        ):
            continue
        try:
            port = int(host.get("wss_port", 443) or 443)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return BilibiliConnectionInfo(
                token=token,
                websocket_url=f"wss://{host_name}:{port}/sub",
            )
    raise BilibiliProtocolError("Bilibili returned no trusted WebSocket host")


def _connection_info_from_payload(payload: dict[str, Any]) -> BilibiliConnectionInfo:
    if int(payload.get("code", -1)) != 0:
        raise BilibiliProtocolError(
            f"getDanmuInfo failed: code={payload.get('code')}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BilibiliProtocolError("getDanmuInfo returned no token")
    return _trusted_connection_info(
        str(data.get("token", "")),
        data.get("host_list"),
    )


def _legacy_connection_info_from_payload(
    payload: dict[str, Any],
) -> BilibiliConnectionInfo:
    if int(payload.get("code", -1)) != 0:
        raise BilibiliProtocolError(
            f"legacy getConf failed: code={payload.get('code')}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BilibiliProtocolError("legacy getConf returned invalid data")
    return _trusted_connection_info(
        str(data.get("token", "")),
        data.get("host_server_list"),
    )


def event_from_command(payload: dict[str, Any], room_id: str) -> PlatformEvent | None:
    """Map supported Bilibili commands to factual, immutable observations."""

    raw_command = str(payload.get("cmd", ""))
    command = raw_command.split(":", 1)[0]
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    native_id = _native_identity(payload, data)
    event_id = native_id or uuid4().hex
    dedup_key = f"{command}:{native_id}" if native_id else None
    common = {
        "event_id": event_id,
        "platform": "bilibili",
        "room_id": room_id,
        "source_sequence": native_id,
        "dedup_key": dedup_key,
        "raw_payload": payload,
    }

    if command == "DANMU_MSG":
        info = payload.get("info")
        if not isinstance(info, list) or len(info) < 3:
            raise BilibiliProtocolError("DANMU_MSG is missing info fields")
        attributes = info[0] if isinstance(info[0], list) else []
        user = info[2] if isinstance(info[2], list) else []
        metadata = {
            "uid": user[0] if user else 0,
            "fans_medal": info[3] if len(info) > 3 else [],
        }
        timestamp = attributes[4] if len(attributes) > 4 else None
        return PlatformEvent(
            kind="danmaku",
            user_name=str(user[1] if len(user) > 1 else "观众"),
            content=str(info[1]),
            timestamp=_source_timestamp(timestamp),
            metadata=metadata,
            **common,
        )
    if command == "SEND_GIFT":
        number = int(data.get("num", 1) or 1)
        price = float(data.get("price", 0) or 0)
        return PlatformEvent(
            kind="gift",
            user_name=str(data.get("uname", "观众")),
            content=f"送出 {data.get('giftName', '礼物')}x{number}",
            value=price * number / 1000.0,
            timestamp=_source_timestamp(data.get("timestamp")),
            metadata={
                "uid": data.get("uid", 0),
                "gift_name": data.get("giftName", "礼物"),
                "gift_num": number,
                "coin_type": data.get("coin_type", ""),
            },
            **common,
        )
    if command == "SUPER_CHAT_MESSAGE":
        user_info = data.get("user_info")
        user_info = user_info if isinstance(user_info, dict) else {}
        return PlatformEvent(
            kind="super_chat",
            user_name=str(user_info.get("uname", "观众")),
            content=str(data.get("message", "")),
            value=float(data.get("price", 0) or 0),
            timestamp=_source_timestamp(data.get("ts", data.get("start_time"))),
            metadata={"uid": data.get("uid", 0), "price": data.get("price", 0)},
            **common,
        )
    if command == "GUARD_BUY":
        levels = {1: "总督", 2: "提督", 3: "舰长"}
        level = levels.get(int(data.get("guard_level", 0) or 0), "大航海")
        return PlatformEvent(
            kind="guard",
            user_name=str(data.get("username", data.get("uname", "观众"))),
            content=f"开通了{level}",
            value=float(data.get("price", 0) or 0) / 1000.0,
            timestamp=_source_timestamp(data.get("start_time")),
            metadata={
                "uid": data.get("uid", 0),
                "guard_level": level,
                "num": data.get("num", 1),
            },
            **common,
        )
    if command == "INTERACT_WORD" and int(data.get("msg_type", 0) or 0) == 1:
        return PlatformEvent(
            kind="enter",
            user_name=str(data.get("uname", "观众")),
            content="进入直播间",
            timestamp=_source_timestamp(data.get("timestamp")),
            metadata={"uid": data.get("uid", 0)},
            **common,
        )
    if command == "LIKE_INFO_V3_CLICK":
        return PlatformEvent(
            kind="like",
            user_name=str(data.get("uname", "观众")),
            content=str(data.get("like_text", "点赞")),
            timestamp=_source_timestamp(data.get("timestamp")),
            metadata={"uid": data.get("uid", 0)},
            **common,
        )
    return None


class BilibiliAdapter(BasePlatformAdapter):
    """Read Bilibili room events with owned reconnect and shutdown semantics."""

    def __init__(
        self,
        room_id: str,
        sessdata: str = "",
        buvid3: str = "",
        reconnect_interval: float = 2.0,
        *,
        max_reconnect_interval: float = 60.0,
        heartbeat_interval: float = 30.0,
        startup_timeout: float = 30.0,
        max_packet_bytes: int = 8 * 1024 * 1024,
        connector: Callable[..., Any] = websocket_connect,
    ) -> None:
        super().__init__()
        if not room_id.isdigit() or int(room_id) <= 0:
            raise ValueError("Bilibili room_id must be a positive integer")
        if reconnect_interval <= 0 or max_reconnect_interval < reconnect_interval:
            raise ValueError("invalid Bilibili reconnect bounds")
        if heartbeat_interval <= 0 or startup_timeout <= 0 or max_packet_bytes <= 0:
            raise ValueError("invalid Bilibili resource bounds")
        self._room_id = str(int(room_id))
        self._sessdata = sessdata
        self._buvid3 = buvid3
        self._reconnect_interval = reconnect_interval
        self._max_reconnect_interval = max_reconnect_interval
        self._heartbeat_interval = heartbeat_interval
        self._startup_timeout = startup_timeout
        self._max_packet_bytes = max_packet_bytes
        self._connector = connector
        self._http: httpx.AsyncClient | None = None
        self._websocket: Any = None
        self._task_info: TaskInfo | None = None
        self._running = False
        self._startup_future: asyncio.Future[None] | None = None
        self._reconnect_count = 0
        self._connected_at: float | None = None
        self._last_event_at: float | None = None
        self._last_error = ""
        self._prefer_legacy_connection_info = False

    def platform_name(self) -> str:
        return "bilibili"

    @property
    def health(self) -> PlatformHealth:
        return PlatformHealth(
            connected=self._connected,
            reconnect_count=self._reconnect_count,
            connected_at=self._connected_at,
            last_event_at=self._last_event_at,
            last_error=self._last_error,
        )

    async def connect(self) -> None:
        if self._running:
            return
        self._running = True
        cookies = {}
        if self._sessdata:
            cookies["SESSDATA"] = self._sessdata
        if self._buvid3:
            cookies["buvid3"] = self._buvid3
        self._http = httpx.AsyncClient(
            cookies=cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=httpx.Timeout(10.0),
        )
        self._startup_future = asyncio.get_running_loop().create_future()
        self._task_info = get_task_manager().create_task(
            self._run_loop(),
            name=f"livestream-bilibili-{self._room_id}",
            daemon=True,
            metadata={"component": "livestream", "room_id": self._room_id},
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(self._startup_future),
                timeout=self._startup_timeout,
            )
        except asyncio.CancelledError:
            await self.disconnect()
            raise
        except Exception:
            await self.disconnect()
            if self._last_error:
                raise RuntimeError(
                    f"Bilibili startup failed: {self._last_error}"
                ) from None
            raise

    async def disconnect(self) -> None:
        if not self._running and self._task_info is None and self._http is None:
            return
        self._running = False
        errors: list[str] = []
        websocket = self._websocket
        if websocket is not None:
            try:
                await websocket.close(code=1000, reason="livestream stopped")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"关闭 B站 WebSocket 时连接已失效: {exc}")
            finally:
                if self._websocket is websocket:
                    self._websocket = None
        info = self._task_info
        task_stopped = True
        if info is not None and info.task is not None and not info.task.done():
            info.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(info.task), timeout=5.0)
            except asyncio.CancelledError:
                if not (info.task.cancelled() or info.task.cancelling()):
                    raise
            except TimeoutError:
                errors.append("Bilibili receive task did not stop within 5s")
                task_stopped = False
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Bilibili receive task failed during stop: {exc}")
        if task_stopped:
            self._task_info = None
            http = self._http
            if http is not None:
                try:
                    await http.aclose()
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Bilibili HTTP client close failed: {exc}")
                else:
                    if self._http is http:
                        self._http = None
        self._connected = False
        self._connected_at = None
        if errors:
            raise RuntimeError("; ".join(errors))

    async def send_danmaku(self, text: str) -> bool:
        # Sending requires account CSRF state and is intentionally outside the
        # read-only ingestion authority of this adapter.
        return False

    async def _run_loop(self) -> None:
        delay = self._reconnect_interval
        while self._running:
            try:
                info = await self._fetch_connection_info()
                await self._connect_and_listen(info)
                if self._running:
                    raise ConnectionError("Bilibili WebSocket closed")
            except asyncio.CancelledError:
                raise
            # Network and protocol implementations expose heterogeneous error
            # types. This ownership boundary records all before bounded retry.
            except Exception as exc:  # noqa: BLE001
                was_connected = self._connected_at is not None
                self._connected = False
                self._connected_at = None
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._reconnect_count += 1
                logger.warning(
                    "B站直播连接失败，准备重连: "
                    f"room={self._room_id} attempt={self._reconnect_count} "
                    f"error={self._last_error}"
                )
                if was_connected:
                    delay = self._reconnect_interval
                jittered = min(
                    self._max_reconnect_interval,
                    delay * random.uniform(0.8, 1.2),
                )
                await asyncio.sleep(jittered)
                delay = min(self._max_reconnect_interval, delay * 2)

    async def _fetch_connection_info(self) -> BilibiliConnectionInfo:
        if self._http is None:
            raise RuntimeError("Bilibili HTTP client is not started")
        if self._prefer_legacy_connection_info:
            payload = await self._fetch_connection_payload(
                _DANMU_CONF_URL,
                {"room_id": self._room_id, "platform": "pc", "player": "web"},
            )
            return _legacy_connection_info_from_payload(payload)
        try:
            payload = await self._fetch_connection_payload(
                _DANMU_INFO_URL,
                {"id": self._room_id, "type": 0},
            )
            return _connection_info_from_payload(payload)
        except (httpx.HTTPError, BilibiliProtocolError) as exc:
            self._prefer_legacy_connection_info = True
            logger.warning(
                "B站主弹幕信息接口不可用，尝试兼容入口: "
                f"room={self._room_id} error={exc}"
            )
        payload = await self._fetch_connection_payload(
            _DANMU_CONF_URL,
            {"room_id": self._room_id, "platform": "pc", "player": "web"},
        )
        return _legacy_connection_info_from_payload(payload)

    async def _fetch_connection_payload(
        self,
        url: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if self._http is None:
            raise RuntimeError("Bilibili HTTP client is not started")
        async with self._http.stream(
            "GET",
            url,
            params=params,
            headers={
                "Referer": f"https://live.bilibili.com/{self._room_id}",
                "Origin": "https://live.bilibili.com",
            },
        ) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_DANMU_INFO_BYTES:
                    raise BilibiliProtocolError("getDanmuInfo response exceeds limit")
                body.extend(chunk)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BilibiliProtocolError("getDanmuInfo returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise BilibiliProtocolError("Bilibili connection response must be an object")
        return payload

    async def _connect_and_listen(self, info: BilibiliConnectionInfo) -> None:
        async with self._connector(
            info.websocket_url,
            open_timeout=10.0,
            close_timeout=5.0,
            ping_interval=None,
            max_size=self._max_packet_bytes,
        ) as websocket:
            self._websocket = websocket
            try:
                auth_body = json.dumps(
                    {
                        "uid": 0,
                        "roomid": int(self._room_id),
                        "protover": 3,
                        "buvid": self._buvid3,
                        "platform": "web",
                        "type": 2,
                        "key": info.token,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                await websocket.send(_pack_packet(_OP_AUTH, auth_body))
                await self._wait_for_auth(websocket)
                self._connected = True
                self._connected_at = time.time()
                self._last_error = ""
                if self._startup_future is not None and not self._startup_future.done():
                    self._startup_future.set_result(None)
                logger.info(f"B站直播间已连接: room={self._room_id}")

                next_heartbeat = time.monotonic() + self._heartbeat_interval
                while self._running:
                    timeout = max(0.0, next_heartbeat - time.monotonic())
                    try:
                        frame = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                    except TimeoutError:
                        await websocket.send(_pack_packet(_OP_HEARTBEAT))
                        next_heartbeat = time.monotonic() + self._heartbeat_interval
                        continue
                    await self._handle_frame(frame)
            finally:
                if self._websocket is websocket:
                    self._websocket = None
                self._connected = False

    async def _wait_for_auth(self, websocket: Any) -> None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            frame = await asyncio.wait_for(
                websocket.recv(),
                timeout=max(0.01, deadline - time.monotonic()),
            )
            for operation, body in self._packets_from_frame(frame):
                if operation != _OP_AUTH_REPLY:
                    continue
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BilibiliProtocolError("invalid Bilibili auth reply") from exc
                if int(payload.get("code", -1)) != 0:
                    raise BilibiliProtocolError(
                        f"Bilibili auth rejected: code={payload.get('code')}"
                    )
                return
        raise TimeoutError("Bilibili auth reply timed out")

    async def _handle_frame(self, frame: str | bytes) -> None:
        for operation, body in self._packets_from_frame(frame):
            if operation in {_OP_HEARTBEAT_REPLY, _OP_AUTH_REPLY}:
                continue
            if operation != _OP_EVENT:
                continue
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BilibiliProtocolError("invalid Bilibili event JSON") from exc
            if not isinstance(payload, dict):
                raise BilibiliProtocolError("Bilibili event payload must be an object")
            event = event_from_command(payload, self._room_id)
            if event is not None:
                await self._emit(event)
                self._last_event_at = time.time()

    def _packets_from_frame(self, frame: str | bytes) -> list[tuple[int, bytes]]:
        if isinstance(frame, str):
            encoded = frame.encode("utf-8")
            if len(encoded) > self._max_packet_bytes:
                raise BilibiliProtocolError("Bilibili text frame exceeds limit")
            return [(_OP_EVENT, encoded)]
        return _decode_packets(frame, max_packet_bytes=self._max_packet_bytes)
