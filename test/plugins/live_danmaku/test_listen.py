from plugins.live_danmaku.listen import danmaku_from_command, wbi_query


def test_wbi_query_matches_the_known_vector() -> None:
    assert (
        wbi_query({"id": 6, "type": 0}, "a" * 32, now=1000)
        == "id=6&type=0&wts=1000&w_rid=545abef419b123cf588e76124ecbd7e3"
    )


def test_reply_keeps_a_revision_for_the_subtitle() -> None:
    from plugins.live_danmaku.listen import LiveDanmakuFeed

    feed = LiveDanmakuFeed()
    feed.note_reply("第一句")
    feed.note_reply("第二句")
    replies = [item for item in feed.snapshot()["messages"] if item["kind"] == "reply"]
    assert [item["revision"] for item in replies] == [1, 2]
    assert replies[-1]["name"] == "爱莉"
    assert replies[-1]["text"] == "第二句"


def test_live_message_uses_the_existing_text_path() -> None:
    from plugins.live_danmaku.bridge import PLATFORM, STREAM_ID, build_live_message
    from plugins.live_danmaku.listen import Danmaku

    message = build_live_message(
        Danmaku(uid=287347892, name="-风速机-", text="你好", at="17:38:29")
    )
    assert message.platform == PLATFORM == "live"
    assert message.chat_type == "group"
    assert message.stream_id == STREAM_ID
    assert message.sender_id == "287347892"
    assert message.processed_plain_text == "你好"
    assert message.extra["group_id"] == "31581575"
    item = danmaku_from_command(
        {
            "cmd": "DANMU_MSG",
            "info": [[0, 1, 2, 3, 1_700_000_000], "你好", [437782593, "观众甲"]],
        }
    )
    assert item is not None
    assert item.uid == 437782593
    assert item.name == "观众甲"
    assert item.text == "你好"
