from src.kernel.llm import Image, LLMPayload, ROLE, Text, ToolCall, ToolResult

from plugins.life_engine.core.context_compaction import (
    SUMMARY_CLOSE,
    SUMMARY_INTRO,
    SUMMARY_OPEN,
    build_conversation_groups,
    build_summary_payload,
    hierarchical_compact_payloads,
    is_summary_payload,
    split_pinned_and_tail,
)


def estimate(payloads):
    return sum(
        len(str(getattr(part, "text", getattr(part, "value", getattr(part, "args", "")))))
        for payload in payloads
        for part in (payload.content if isinstance(payload.content, list) else [payload.content])
    ) + len(payloads) * 20


def group(i, size=700):
    return [
        LLMPayload(ROLE.USER, Text(f"user-{i}-" + "x" * size)),
        LLMPayload(ROLE.ASSISTANT, Text(f"assistant-{i}-" + "y" * size)),
    ]


def test_not_triggered_returns_original_shape():
    payloads = group(1, 5)
    result = hierarchical_compact_payloads(payloads, estimate=estimate, trigger_chars=10_000)
    assert result.triggered is False
    assert result.before_chars == result.after_chars
    assert result.payloads == payloads
    assert result.target_reached is True


def test_compacts_old_groups_toward_target_and_keeps_recent_groups():
    payloads = sum((group(i) for i in range(8)), [])
    result = hierarchical_compact_payloads(
        payloads,
        estimate=estimate,
        trigger_chars=3_000,
        target_chars=2_500,
        min_recent_groups=2,
        summary_max_chars=800,
    )
    assert result.triggered
    assert result.after_chars <= 2_500
    assert result.target_reached is True
    texts = str(result.payloads)
    assert "user-6-" in texts and "user-7-" in texts


def test_repeated_compaction_replaces_single_summary_without_nesting():
    first = hierarchical_compact_payloads(
        sum((group(i) for i in range(6)), []),
        estimate=estimate,
        trigger_chars=2_000,
        target_chars=1_500,
        min_recent_groups=1,
    )
    payloads = first.payloads + sum((group(i) for i in range(6, 10)), [])
    second = hierarchical_compact_payloads(
        payloads,
        estimate=estimate,
        trigger_chars=2_000,
        target_chars=1_500,
        min_recent_groups=1,
    )
    summaries = [
        p
        for p in second.payloads
        if any(isinstance(x, Text) and SUMMARY_OPEN in (x.text or "") for x in p.content)
    ]
    assert len(summaries) == 1
    text = summaries[0].content[0].text
    assert text.count(SUMMARY_OPEN) == 1
    assert text.count(SUMMARY_CLOSE) == 1
    assert text.startswith(SUMMARY_INTRO)
    assert text.endswith(SUMMARY_CLOSE)
    assert is_summary_payload(summaries[0])


def test_open_recent_tool_chain_is_kept_as_one_group():
    payloads = sum((group(i) for i in range(5)), [])
    payloads.extend(
        [
            LLMPayload(ROLE.USER, Text("latest-tool-user")),
            LLMPayload(ROLE.ASSISTANT, ToolCall(id="c1", name="inspect", args={"q": "x"})),
            LLMPayload(ROLE.TOOL_RESULT, ToolResult(value="ok", call_id="c1", name="inspect")),
        ]
    )
    result = hierarchical_compact_payloads(
        payloads,
        estimate=estimate,
        trigger_chars=2_000,
        target_chars=1_200,
        min_recent_groups=1,
    )
    assert any(
        isinstance(x, Text) and x.text == "latest-tool-user"
        for p in result.payloads
        for x in p.content
    )
    assert any(isinstance(x, ToolCall) and x.id == "c1" for p in result.payloads for x in p.content)
    assert any(
        isinstance(x, ToolResult) and x.call_id == "c1" for p in result.payloads for x in p.content
    )


def test_summary_contains_media_descriptor_not_base64():
    blob = "A" * 20_000
    payloads = [
        LLMPayload(ROLE.USER, [Text("old media"), Image("data:image/png;base64," + blob)]),
        LLMPayload(ROLE.ASSISTANT, Text("seen")),
        *sum((group(i) for i in range(4)), []),
    ]
    result = hierarchical_compact_payloads(
        payloads,
        estimate=estimate,
        trigger_chars=2_000,
        target_chars=1_200,
        min_recent_groups=1,
    )
    serialized = str(result.payloads)
    assert "[图片]" in serialized
    assert blob[:1000] not in serialized


def test_only_leading_system_tool_are_pinned_midstream_stays_in_order():
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("sys-head")),
        LLMPayload(ROLE.TOOL, Text("tool-head")),
        LLMPayload(ROLE.USER, Text("u1-" + "x" * 800)),
        LLMPayload(ROLE.ASSISTANT, Text("a1-" + "y" * 800)),
        LLMPayload(ROLE.SYSTEM, Text("sys-mid")),
        LLMPayload(ROLE.TOOL, Text("tool-mid")),
        LLMPayload(ROLE.USER, Text("u2-" + "x" * 800)),
        LLMPayload(ROLE.ASSISTANT, Text("a2-" + "y" * 800)),
        LLMPayload(ROLE.USER, Text("u3-" + "x" * 800)),
        LLMPayload(ROLE.ASSISTANT, Text("a3-" + "y" * 800)),
    ]
    pinned, tail = split_pinned_and_tail(payloads)
    assert [p.content[0].text for p in pinned] == ["sys-head", "tool-head"]
    assert [getattr(p.content[0], "text", None) for p in tail[:4]] == [
        "u1-" + "x" * 800,
        "a1-" + "y" * 800,
        "sys-mid",
        "tool-mid",
    ]
    groups = build_conversation_groups(tail)
    assert any(
        any(isinstance(part, Text) and part.text == "sys-mid" for part in g[0].content)
        or any(isinstance(part, Text) and part.text == "sys-mid" for payload in g for part in payload.content)
        for g in groups
    )
    result = hierarchical_compact_payloads(
        payloads,
        estimate=estimate,
        trigger_chars=1_500,
        target_chars=1_200,
        min_recent_groups=1,
    )
    roles_texts = []
    for p in result.payloads:
        for part in p.content:
            roles_texts.append((p.role, getattr(part, "text", None)))
    assert roles_texts[0] == (ROLE.SYSTEM, "sys-head")
    assert roles_texts[1] == (ROLE.TOOL, "tool-head")
    # The old group may be summarized, but midstream SYSTEM/TOOL are part of
    # that group (not promoted into the pinned prefix) and remain ordered there.
    summary_text = next(
        part.text
        for payload in result.payloads
        for part in payload.content
        if isinstance(part, Text) and SUMMARY_OPEN in (part.text or "")
    )
    assert summary_text.index("sys-mid") < summary_text.index("tool-mid")


def test_summary_envelope_is_strict_user_single_text():
    payload = build_summary_payload(
        previous_summary_body="old body head should be trimmed if needed",
        dropped_groups=[[LLMPayload(ROLE.USER, Text("new-drop"))]],
        summary_max_chars=200,
    )
    assert payload.role == ROLE.USER
    assert len(payload.content) == 1
    assert isinstance(payload.content[0], Text)
    text = payload.content[0].text
    assert text.startswith(SUMMARY_INTRO + "\n" + SUMMARY_OPEN)
    assert text.endswith(SUMMARY_CLOSE)
    assert text.count(SUMMARY_OPEN) == 1
    assert text.count(SUMMARY_CLOSE) == 1


def test_continuous_summary_prefers_newest_and_trims_old_from_head():
    old_body = "OLDHEAD-" + ("O" * 400) + "-OLDTAIL"
    newest = "NEWEST-" + ("N" * 200)
    payload = build_summary_payload(
        previous_summary_body=old_body,
        dropped_groups=[[LLMPayload(ROLE.USER, Text(newest))]],
        summary_max_chars=260,
        max_part_chars=500,
    )
    text = payload.content[0].text
    assert "NEWEST-" in text
    assert "OLDHEAD-" not in text or text.index("NEWEST-") < text.find("OLDHEAD-") if "OLDHEAD-" in text else True
    # old head is dropped before old tail when budget is tight
    if "既有背景摘要" in text:
        assert "OLDTAIL" in text or "..." in text


def test_latest_group_media_replaced_with_descriptor_when_over_target():
    huge = "B" * 50_000
    payloads = [
        LLMPayload(ROLE.USER, Text("keep-me")),
        LLMPayload(ROLE.ASSISTANT, Text("ack")),
        LLMPayload(
            ROLE.USER,
            [
                Text("latest-with-media"),
                Image("data:image/png;base64," + huge),
                ToolCall(id="t1", name="look", args={}),
            ],
        ),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value="tool-ok", call_id="t1", name="look")),
    ]
    result = hierarchical_compact_payloads(
        payloads,
        estimate=estimate,
        trigger_chars=1_000,
        target_chars=800,
        min_recent_groups=1,
        force=True,
    )
    assert result.triggered
    flat = [(p.role, type(part).__name__, getattr(part, "text", getattr(part, "value", None))) for p in result.payloads for part in p.content]
    assert any(name == "Text" and "latest-with-media" in str(val) for _, name, val in flat)
    assert any(name == "ToolCall" for _, name, _ in flat)
    assert any(name == "ToolResult" and val == "tool-ok" for _, name, val in flat)
    assert not any(name == "Image" for _, name, _ in flat)
    assert any("[图片]" in str(val) for _, name, val in flat if name == "Text")


def test_huge_tool_result_may_fail_target_reached_false():
    huge = "Z" * 100_000
    payloads = [
        LLMPayload(ROLE.USER, Text("u")),
        LLMPayload(ROLE.ASSISTANT, ToolCall(id="c", name="dump", args={})),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value=huge, call_id="c", name="dump")),
    ]
    result = hierarchical_compact_payloads(
        payloads,
        estimate=estimate,
        trigger_chars=1_000,
        target_chars=500,
        min_recent_groups=1,
        force=True,
    )
    assert result.triggered
    assert result.target_reached is False
    assert any(
        isinstance(part, ToolResult) and part.value == huge
        for payload in result.payloads
        for part in payload.content
    )

def test_ordinary_user_text_with_tags_is_not_summary():
    """Tags embedded in normal user text must not be treated as a summary envelope."""
    noisy = LLMPayload(
        ROLE.USER,
        Text(
            "用户提到了标签 "
            + SUMMARY_OPEN
            + " 和 "
            + SUMMARY_CLOSE
            + " 但不是规范摘要"
        ),
    )
    assert is_summary_payload(noisy) is False

    multi = LLMPayload(
        ROLE.USER,
        [Text(SUMMARY_INTRO + "\n" + SUMMARY_OPEN + "\nbody\n" + SUMMARY_CLOSE), Text("extra")],
    )
    assert is_summary_payload(multi) is False

    missing_intro = LLMPayload(
        ROLE.USER,
        Text(SUMMARY_OPEN + "\nbody\n" + SUMMARY_CLOSE),
    )
    assert is_summary_payload(missing_intro) is False

    good = build_summary_payload(
        previous_summary_body="",
        dropped_groups=[[LLMPayload(ROLE.USER, Text("ok"))]],
    )
    assert is_summary_payload(good) is True
