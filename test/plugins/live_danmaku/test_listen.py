from plugins.live_danmaku.listen import danmaku_from_command, wbi_query


def test_wbi_query_matches_the_known_vector() -> None:
    assert (
        wbi_query({"id": 6, "type": 0}, "a" * 32, now=1000)
        == "id=6&type=0&wts=1000&w_rid=545abef419b123cf588e76124ecbd7e3"
    )


def test_danmaku_keeps_uid_name_and_text() -> None:
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
