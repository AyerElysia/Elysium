from __future__ import annotations

import zlib

import brotli
import httpx
import pytest

from plugins.livestream.platform.bilibili import (
    _OP_EVENT,
    BilibiliAdapter,
    BilibiliProtocolError,
    _connection_info_from_payload,
    _decode_packets,
    _legacy_connection_info_from_payload,
    _pack_packet,
    _wbi_query,
    event_from_command,
)


def test_packet_decoder_handles_multiple_and_nested_compression() -> None:
    first = _pack_packet(_OP_EVENT, b'{"cmd":"A"}')
    second = _pack_packet(_OP_EVENT, b'{"cmd":"B"}')
    nested = first + second
    zlib_frame = _pack_packet(_OP_EVENT, zlib.compress(nested), version=2)
    brotli_frame = _pack_packet(_OP_EVENT, brotli.compress(nested), version=3)

    assert _decode_packets(nested, max_packet_bytes=4096) == [
        (_OP_EVENT, b'{"cmd":"A"}'),
        (_OP_EVENT, b'{"cmd":"B"}'),
    ]
    assert _decode_packets(zlib_frame, max_packet_bytes=4096) == _decode_packets(
        nested, max_packet_bytes=4096
    )
    assert _decode_packets(brotli_frame, max_packet_bytes=4096) == _decode_packets(
        nested, max_packet_bytes=4096
    )


def test_packet_decoder_rejects_truncation_and_resource_overflow() -> None:
    with pytest.raises(BilibiliProtocolError, match="truncated"):
        _decode_packets(b"short", max_packet_bytes=100)
    with pytest.raises(BilibiliProtocolError, match="exceeds"):
        _decode_packets(b"x" * 101, max_packet_bytes=100)

    expanded = _pack_packet(_OP_EVENT, b"x" * 2048)
    for compressed in (
        _pack_packet(_OP_EVENT, zlib.compress(expanded), version=2),
        _pack_packet(_OP_EVENT, brotli.compress(expanded), version=3),
    ):
        with pytest.raises(BilibiliProtocolError, match="decompressed.*exceeds"):
            _decode_packets(compressed, max_packet_bytes=1024)


def test_danmaku_without_native_id_is_not_falsely_deduplicated() -> None:
    payload = {
        "cmd": "DANMU_MSG",
        "info": [[0, 0, 0, 0, 1_700_000_000_000], "same", [7, "viewer"]],
    }
    first = event_from_command(payload, "42")
    second = event_from_command(payload, "42")

    assert first is not None and second is not None
    assert first.kind == "danmaku"
    assert first.content == "same"
    assert first.timestamp == 1_700_000_000
    assert first.dedup_key is None
    assert first.event_id != second.event_id


def test_supported_paid_events_keep_native_identity_as_fact_not_priority() -> None:
    gift = event_from_command(
        {
            "cmd": "SEND_GIFT",
            "data": {
                "tid": "gift-9",
                "uid": 7,
                "uname": "viewer",
                "giftName": "flower",
                "num": 2,
                "price": 1000,
                "timestamp": 1_700_000_000,
            },
        },
        "42",
    )
    sc = event_from_command(
        {
            "cmd": "SUPER_CHAT_MESSAGE",
            "data": {
                "id": 99,
                "uid": 8,
                "user_info": {"uname": "another"},
                "message": "hello",
                "price": 30,
                "ts": 1_700_000_001,
            },
        },
        "42",
    )

    assert gift is not None and sc is not None
    assert gift.dedup_key == "SEND_GIFT:gift-9"
    assert gift.value == 2.0
    assert sc.dedup_key == "SUPER_CHAT_MESSAGE:99"
    assert sc.value == 30.0
    assert not hasattr(gift, "priority")


def test_malformed_danmaku_is_explicit_protocol_error() -> None:
    with pytest.raises(BilibiliProtocolError, match="missing info"):
        event_from_command({"cmd": "DANMU_MSG", "info": []}, "42")


def test_connection_info_accepts_only_bilibili_websocket_hosts() -> None:
    payload = {
        "code": 0,
        "data": {
            "token": "short-lived-token",
            "host_list": [
                {"host": "attacker.example", "wss_port": 443},
                {"host": "broadcastlv.chat.bilibili.com", "wss_port": 2245},
            ],
        },
    }
    info = _connection_info_from_payload(payload)
    assert info.websocket_url == "wss://broadcastlv.chat.bilibili.com:2245/sub"

    payload["data"]["host_list"] = [{"host": "bilibili.com.evil.example"}]
    with pytest.raises(BilibiliProtocolError, match="trusted"):
        _connection_info_from_payload(payload)


def test_legacy_connection_info_uses_the_same_trusted_host_boundary() -> None:
    info = _legacy_connection_info_from_payload(
        {
            "code": 0,
            "data": {
                "token": "legacy-token",
                "host_server_list": [
                    {"host": "zj-cn-live-comet.chat.bilibili.com", "wss_port": 443}
                ],
            },
        }
    )
    assert info.token == "legacy-token"
    assert info.websocket_url.endswith(":443/sub")


def test_wbi_query_matches_the_web_player_signature() -> None:
    query = _wbi_query({"id": 6, "type": 0}, "a" * 32, now=1000)
    assert query == "id=6&type=0&wts=1000&w_rid=545abef419b123cf588e76124ecbd7e3"


@pytest.mark.asyncio
async def test_adapter_signs_getdanmuinfo_before_connecting() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/spi"):
            return httpx.Response(200, json={"code": 0, "data": {"b_3": "buvid-test"}})
        if request.url.path.endswith("/nav"):
            assert request.headers["cookie"] == "buvid3=buvid-test"
            return httpx.Response(
                200,
                json={
                    "code": -101,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png",
                        }
                    },
                },
            )
        assert "w_rid=" in request.url.query.decode()
        assert "wts=" in request.url.query.decode()
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "token": "signed-token",
                    "host_list": [
                        {"host": "zj-cn-live-comet.chat.bilibili.com", "wss_port": 443}
                    ],
                },
            },
        )

    adapter = BilibiliAdapter("31581575")
    adapter._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        info = await adapter._fetch_connection_info()
    finally:
        await adapter._http.aclose()
        adapter._http = None

    assert info.token == "signed-token"
    assert info.websocket_url == "wss://zj-cn-live-comet.chat.bilibili.com:443/sub"
    assert adapter._auth_buvid == "buvid-test"
    assert calls == [
        "/x/frontend/finger/spi",
        "/x/web-interface/nav",
        "/xlive/web-room/v1/index/getDanmuInfo",
    ]


@pytest.mark.asyncio
async def test_adapter_sticks_to_legacy_endpoint_after_primary_risk_control() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/spi"):
            return httpx.Response(200, json={"code": 0, "data": {"b_3": "buvid-test"}})
        if request.url.path.endswith("/nav"):
            return httpx.Response(
                200,
                json={
                    "code": -101,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png",
                        }
                    },
                },
            )
        if request.url.path.endswith("getDanmuInfo"):
            return httpx.Response(200, json={"code": -352, "message": "-352"})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "token": "legacy-token",
                    "host_server_list": [
                        {
                            "host": "broadcastlv.chat.bilibili.com",
                            "wss_port": 443,
                        }
                    ],
                },
            },
        )

    adapter = BilibiliAdapter("6")
    adapter._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await adapter._fetch_connection_info()
        second = await adapter._fetch_connection_info()
    finally:
        await adapter._http.aclose()
        adapter._http = None

    assert first == second
    assert calls == [
        "/x/frontend/finger/spi",
        "/x/web-interface/nav",
        "/xlive/web-room/v1/index/getDanmuInfo",
        "/room/v1/Danmu/getConf",
        "/room/v1/Danmu/getConf",
    ]
