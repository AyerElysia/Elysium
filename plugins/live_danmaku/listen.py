"""Read-only Bilibili danmaku connection for one room.

The handshake follows the web player: buvid, nav, real room id, then a signed
getDanmuInfo. Nothing here sends danmaku or starts a broadcast.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import time
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import brotli
import httpx
from websockets.asyncio.client import connect as websocket_connect

ROOM_ID = "31581575"
SESSDATA_ENV = "ELYSIUM_BILIBILI_SESSDATA"
_HEADER = struct.Struct(">IHHII")
_OP_HEARTBEAT = 2
_OP_HEARTBEAT_REPLY = 3
_OP_EVENT = 5
_OP_AUTH = 7
_OP_AUTH_REPLY = 8
_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_ROOM_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
_DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
_WBI_MIXIN = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
)
_MAX_BODY = 1024 * 1024
_MAX_MESSAGES = 200


class ListenError(RuntimeError):
    pass


class NotLoggedIn(ListenError):
    pass


@dataclass(frozen=True, slots=True)
class Danmaku:
    uid: int
    name: str
    text: str
    at: str
    kind: str = "danmaku"
    revision: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "text": self.text,
            "at": self.at,
            "kind": self.kind,
            "revision": self.revision,
        }


def sessdata_from_env() -> str:
    return os.environ.get(SESSDATA_ENV, "").strip()


def wbi_mixin_key(img_url: str, sub_url: str) -> str:
    raw = f"{_url_filename(img_url)}{_url_filename(sub_url)}"
    return "".join(raw[index] if index < len(raw) else "" for index in _WBI_MIXIN)[:32]


def wbi_query(params: dict[str, str | int], mixin: str, *, now: int | None = None) -> str:
    if len(mixin) != 32:
        raise ListenError("WBI mixin key must be 32 characters")
    signed: dict[str, str | int] = {**params, "wts": int(time.time() if now is None else now)}
    query = "&".join(
        f"{_wbi_escape(key)}={_wbi_escape(str(signed[key]))}" for key in sorted(signed)
    )
    digest = hashlib.md5(f"{query}{mixin}".encode()).hexdigest()
    return f"{query}&w_rid={digest}"


def danmaku_from_command(payload: dict[str, Any]) -> Danmaku | None:
    command = str(payload.get("cmd", "")).split(":", 1)[0]
    if command != "DANMU_MSG":
        return None
    info = payload.get("info")
    if not isinstance(info, list) or len(info) < 3:
        raise ListenError("DANMU_MSG is missing info fields")
    user = info[2] if isinstance(info[2], list) else []
    uid = int(user[0]) if user else 0
    name = str(user[1] if len(user) > 1 else "观众")
    text = str(info[1] if len(info) > 1 else "")
    attributes = info[0] if isinstance(info[0], list) else []
    stamp = attributes[4] if len(attributes) > 4 else None
    return Danmaku(uid=uid, name=name, text=text, at=_clock(stamp))


class LiveDanmakuFeed:
    """One manual session. Construction and plugin load do not connect."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._status = "stopped"
        self._detail = ""
        self._messages: deque[Danmaku] = deque(maxlen=_MAX_MESSAGES)
        self._handoffs: set[asyncio.Task[None]] = set()
        self._reply_revision = 0

    def snapshot(self) -> dict[str, Any]:
        logged_in = bool(sessdata_from_env())
        status = self._status
        detail = self._detail
        if not logged_in and not self._running:
            status = "logged_out"
            detail = "未登录"
        return {
            "logged_in": logged_in,
            "running": self._running,
            "status": status,
            "detail": detail,
            "room_id": ROOM_ID,
            "messages": [item.as_dict() for item in self._messages],
        }

    async def start(self) -> dict[str, Any]:
        if self._running:
            return self.snapshot()
        if not sessdata_from_env():
            self._status = "logged_out"
            self._detail = "未登录"
            return self.snapshot()
        self._running = True
        self._status = "connecting"
        self._detail = ""
        self._task = asyncio.create_task(self._run(), name="live-danmaku")
        return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._status = "stopped"
        self._detail = ""
        return self.snapshot()

    async def _run(self) -> None:
        delay = 2.0
        while self._running:
            try:
                await self._listen_once()
                if self._running:
                    raise ListenError("连接已断开")
            except asyncio.CancelledError:
                raise
            except NotLoggedIn as exc:
                self._running = False
                self._status = "logged_out"
                self._detail = str(exc)
                return
            except Exception as exc:  # noqa: BLE001
                self._status = "error"
                self._detail = str(exc)
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)

    async def _listen_once(self) -> None:
        sessdata = sessdata_from_env()
        if not sessdata:
            raise NotLoggedIn("未登录")
        async with httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT},
            timeout=20.0,
            follow_redirects=False,
        ) as http:
            plan = await _handshake(http, sessdata)
        self._status = "connecting"
        self._detail = f"房间 {plan.real_room_id}"
        async with websocket_connect(
            plan.websocket_url,
            open_timeout=10.0,
            close_timeout=5.0,
            ping_interval=None,
            max_size=8 * 1024 * 1024,
            additional_headers={
                "Origin": "https://live.bilibili.com",
                "User-Agent": _USER_AGENT,
            },
        ) as websocket:
            await websocket.send(_pack( _OP_AUTH, json.dumps(plan.auth, separators=(",", ":")).encode()))
            await _wait_auth(websocket)
            self._status = "connected"
            self._detail = f"房间 {plan.real_room_id}"
            next_heartbeat = time.monotonic() + 30.0
            while self._running:
                timeout = max(0.0, next_heartbeat - time.monotonic())
                try:
                    frame = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                except TimeoutError:
                    await websocket.send(_pack(_OP_HEARTBEAT))
                    next_heartbeat = time.monotonic() + 30.0
                    continue
                for operation, body in _packets(frame):
                    if operation != _OP_EVENT:
                        continue
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    try:
                        item = danmaku_from_command(payload)
                    except ListenError:
                        continue
                    if item is not None:
                        self._messages.append(item)
                        task = asyncio.create_task(_hand_to_life(item))
                        self._handoffs.add(task)
                        task.add_done_callback(self._handoffs.discard)

    def note_reply(self, text: str) -> None:
        self._reply_revision += 1
        self._messages.append(
            Danmaku(
                uid=0,
                name="爱莉",
                text=text,
                at=_clock(None),
                kind="reply",
                revision=self._reply_revision,
            )
        )


@dataclass(frozen=True, slots=True)
class _Plan:
    websocket_url: str
    auth: dict[str, Any]
    real_room_id: int


async def _handshake(http: httpx.AsyncClient, sessdata: str) -> _Plan:
    spi = await _get_json(http, _SPI_URL, "")
    buvid = str((spi.get("data") or {}).get("b_3") or "").strip()
    if not buvid:
        raise ListenError("没有拿到 buvid3")
    cookie = f"buvid3={buvid}; SESSDATA={sessdata}"
    nav = await _get_json(http, _NAV_URL, cookie)
    nav_data = nav.get("data") if isinstance(nav.get("data"), dict) else {}
    mid = int(nav_data.get("mid") or 0)
    if mid <= 0:
        raise NotLoggedIn("未登录")
    wbi = nav_data.get("wbi_img") if isinstance(nav_data.get("wbi_img"), dict) else {}
    mixin = wbi_mixin_key(str(wbi.get("img_url") or ""), str(wbi.get("sub_url") or ""))
    room_payload = await _get_json(http, f"{_ROOM_URL}?room_id={ROOM_ID}", cookie)
    if int(room_payload.get("code", -1)) != 0:
        raise ListenError(f"取房间信息失败: {room_payload.get('message') or room_payload.get('code')}")
    room = room_payload.get("data") if isinstance(room_payload.get("data"), dict) else {}
    real_room_id = int(room.get("room_id") or 0)
    if real_room_id <= 0:
        raise ListenError("没有拿到真实房间号")
    query = wbi_query({"id": real_room_id, "type": 0}, mixin)
    dm = await _get_json(http, f"{_DANMU_INFO_URL}?{query}", cookie)
    if int(dm.get("code", -1)) != 0:
        raise ListenError(f"取弹幕服务器失败: {dm.get('code')}")
    data = dm.get("data") if isinstance(dm.get("data"), dict) else {}
    url = _websocket_url(data.get("host_list"))
    token = str(data.get("token") or "")
    if not token:
        raise ListenError("弹幕服务器没有返回 token")
    return _Plan(
        websocket_url=url,
        real_room_id=real_room_id,
        auth={
            "uid": mid,
            "roomid": real_room_id,
            "protover": 3,
            "platform": "web",
            "type": 2,
            "buvid": buvid,
            "key": token,
        },
    )


async def _get_json(http: httpx.AsyncClient, url: str, cookie: str) -> dict[str, Any]:
    headers = {
        "Referer": f"https://live.bilibili.com/{ROOM_ID}",
        "Origin": "https://live.bilibili.com",
    }
    if cookie:
        headers["Cookie"] = cookie
    response = await http.get(url, headers=headers)
    if response.status_code == 412:
        raise ListenError("HTTP 412")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ListenError("B 站返回的不是对象")
    return payload


def _websocket_url(hosts: Any) -> str:
    if not isinstance(hosts, list):
        raise ListenError("弹幕服务器列表为空")
    for host in hosts:
        if not isinstance(host, dict):
            continue
        name = str(host.get("host") or "").strip().rstrip(".").casefold()
        if name != "bilibili.com" and not name.endswith(".bilibili.com"):
            continue
        port = int(host.get("wss_port") or 443)
        if 1 <= port <= 65535:
            return f"wss://{name}:{port}/sub"
    raise ListenError("没有可用的弹幕服务器")


def _pack(operation: int, body: bytes = b"") -> bytes:
    return _HEADER.pack(_HEADER.size + len(body), _HEADER.size, 1, operation, 1) + body


def _packets(frame: str | bytes) -> list[tuple[int, bytes]]:
    if isinstance(frame, str):
        return [(_OP_EVENT, frame.encode("utf-8"))]
    return _decode(frame)


def _decode(frame: bytes, depth: int = 0) -> list[tuple[int, bytes]]:
    if depth > 4:
        raise ListenError("弹幕包嵌套过深")
    if len(frame) > 8 * 1024 * 1024:
        raise ListenError("弹幕包过大")
    offset = 0
    packets: list[tuple[int, bytes]] = []
    while offset < len(frame):
        if len(frame) - offset < _HEADER.size:
            raise ListenError("弹幕包头被截断")
        packet_length, header_length, version, operation, _sequence = _HEADER.unpack_from(frame, offset)
        if header_length < _HEADER.size or packet_length < header_length or offset + packet_length > len(frame):
            raise ListenError("弹幕包长度无效")
        body = frame[offset + header_length : offset + packet_length]
        if version == 2:
            packets.extend(_decode(_inflate(body, zlib.decompressobj()), depth + 1))
        elif version == 3:
            packets.extend(_decode(_inflate_brotli(body), depth + 1))
        elif version in {0, 1}:
            packets.append((operation, body))
        else:
            raise ListenError(f"不认识的弹幕包版本: {version}")
        offset += packet_length
    return packets


def _inflate(body: bytes, decoder: zlib.Decompress) -> bytes:
    try:
        output = decoder.decompress(body, _MAX_BODY + 1)
    except zlib.error as exc:
        raise ListenError("弹幕包解压失败") from exc
    if len(output) > _MAX_BODY or decoder.unconsumed_tail or not decoder.eof:
        raise ListenError("弹幕包解压失败")
    return output


def _inflate_brotli(body: bytes) -> bytes:
    decoder = brotli.Decompressor()
    output = bytearray()
    try:
        for offset in range(0, len(body), 64):
            chunk = decoder.process(body[offset : offset + 64])
            if len(output) + len(chunk) > _MAX_BODY:
                raise ListenError("弹幕包过大")
            output.extend(chunk)
    except brotli.error as exc:
        raise ListenError("弹幕包解压失败") from exc
    if not decoder.is_finished():
        raise ListenError("弹幕包解压失败")
    return bytes(output)


async def _wait_auth(websocket: Any) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        frame = await asyncio.wait_for(websocket.recv(), timeout=max(0.01, deadline - time.monotonic()))
        for operation, body in _packets(frame):
            if operation != _OP_AUTH_REPLY:
                continue
            payload = json.loads(body.decode("utf-8") or "{}")
            if int(payload.get("code", -1)) != 0:
                raise ListenError(f"认证失败: {payload.get('code')}")
            return
    raise ListenError("认证超时")


def _url_filename(url: str) -> str:
    return url.rsplit("/", 1)[-1].split(".", 1)[0]


def _wbi_escape(value: str) -> str:
    cleaned = "".join(char for char in value if char not in "!'()*")
    return quote(cleaned, safe="-_.~")


def _clock(value: Any) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = time.time()
    if parsed > 10_000_000_000:
        parsed /= 1000.0
    if parsed <= 0:
        parsed = time.time()
    return time.strftime("%H:%M:%S", time.localtime(parsed))


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_FEED: LiveDanmakuFeed | None = None


def get_feed() -> LiveDanmakuFeed:
    global _FEED
    if _FEED is None:
        _FEED = LiveDanmakuFeed()
    return _FEED


async def _hand_to_life(item: Danmaku) -> None:
    from .bridge import submit_danmaku

    try:
        await submit_danmaku(item)
    except Exception as exc:  # noqa: BLE001
        get_feed()._detail = f"交给爱莉失败: {exc}"
