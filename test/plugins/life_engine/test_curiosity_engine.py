import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.curiosity.engine import (
    CuriosityEngine,
    CuriositySignal,
    EpistemicOpportunity,
    EpistemicOpportunityStateError,
    EpistemicOpportunityTimeoutError,
    format_curiosity_signal,
    format_epistemic_opportunity,
)


def _opportunity(
    *, occurrence_id: str = "evt-1", question: str = "她是在确认在线，还是确认被记得？"
) -> EpistemicOpportunity:
    return EpistemicOpportunity.build(
        source_occurrence_id=occurrence_id,
        source_stream_id="stream-1",
        source_instance_id="chat_global",
        observed_gap="同一句确认重复出现",
        open_question=question,
        possible_next_look="以后可再次观察相邻语境",
        generator_note="来源里保留了两种尚未区分的解释",
        generated_at="2026-08-06T12:00:00+08:00",
    )


def test_empty_signal_formats_to_empty_text():
    assert format_curiosity_signal(CuriositySignal.empty()) == ""
    assert format_epistemic_opportunity(None) == ""


def test_candidate_render_never_claims_subject_curiosity_or_importance():
    text = format_epistemic_opportunity(_opportunity())

    assert "### 认知机会候选（epistemic_opportunity）" in text
    assert "不是你的好奇、想法、偏好或任务" in text
    assert "不表示它重要" in text
    assert "只有你此刻亲自选择" in text
    assert "保持开放、忽略或不回应" in text
    assert "同一句确认重复出现" in text
    assert "好奇牵引" not in text
    assert "置信度" not in text
    assert "标签" not in text


def test_legacy_renderer_uses_candidate_semantics_and_drops_scores():
    text = format_curiosity_signal(
        CuriositySignal(
            active=True,
            anchor="反复问在不在",
            why="来源里仍有多种解释",
            unknown="是在确认在线，还是确认被记得？",
            approach="之后可以再观察",
            confidence=0.9,
            tags=["关系牵引"],
        )
    )

    assert "认知机会候选" in text
    assert "反复问在不在" in text
    assert "0.9" not in text
    assert "关系牵引" not in text
    assert "同一主体的异步好奇" not in text


def test_generator_prompt_is_external_and_has_no_scoring_schema():
    prompt = CuriosityEngine._build_system_prompt("主体上下文引用")

    assert "系统侧的候选生成器" in prompt
    assert "不是爱莉、不是她的内心" in prompt
    assert "不授予你代表主体" in prompt
    assert '"candidate_present"' in prompt
    assert '"open_question"' in prompt
    assert '"confidence"' not in prompt
    assert '"tags"' not in prompt
    assert '"priority"' not in prompt


def test_parse_opportunity_repairs_json_and_binds_source_identity():
    opportunity = CuriosityEngine._parse_opportunity(
        """
        {
          candidate_present: true,
          observed_gap: "图片摘要和实际气质还没有对照",
          open_question: "它是否真的适合作为电台封面？",
          possible_next_look: "可以直接观察原图",
          generator_note: "当前只有文字摘要"
        }
        """,
        source_occurrence_id="evt-1",
        source_stream_id="stream-1",
        source_instance_id="chat_global",
    )

    assert opportunity is not None
    assert opportunity.source_occurrence_id == "evt-1"
    assert opportunity.source_stream_id == "stream-1"
    assert opportunity.source_instance_id == "chat_global"
    assert opportunity.open_question == "它是否真的适合作为电台封面？"
    assert opportunity.opportunity_id.startswith("eop_")
    assert len(opportunity.payload_sha256) == 64


def test_false_generator_result_does_not_materialize_candidate():
    opportunity = CuriosityEngine._parse_opportunity(
        '{"candidate_present": false}',
        source_occurrence_id="evt-1",
        source_stream_id="stream-1",
        source_instance_id="chat_global",
    )

    assert opportunity is None


def test_legacy_parse_adapter_does_not_preserve_score_or_tags():
    signal = CuriosityEngine._parse_signal(
        """
        {
          active: true,
          anchor: "图片气质没闭合",
          unknown: "气质是否像爱莉",
          confidence: 0.8,
          tags: ["图片", "审美"]
        }
        """,
        source_event_id="evt-1",
        source_stream_id="stream-1",
    )

    assert signal.active is True
    assert signal.anchor == "图片气质没闭合"
    assert signal.source_event_id == "evt-1"
    assert signal.confidence == 0.0
    assert signal.tags == []


@pytest.mark.asyncio
async def test_append_only_roundtrip_and_projection_clear(tmp_path):
    engine = CuriosityEngine(workspace_path=str(tmp_path), model_task_name="life")
    first = _opportunity()
    second = _opportunity(occurrence_id="evt-2", question="这次停顿来自哪里？")

    await engine.save_opportunity(first)
    await engine.save_opportunity(second)
    assert await engine.load_opportunity() == second

    ledger_before_clear = engine.ledger_path.read_bytes()
    rows = engine.ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2
    assert [json.loads(row)["opportunity_id"] for row in rows] == [
        first.opportunity_id,
        second.opportunity_id,
    ]

    await engine.clear()
    assert await engine.load_opportunity() is None
    assert engine.ledger_path.read_bytes() == ledger_before_clear
    projection = json.loads(engine.state_path.read_text(encoding="utf-8"))
    assert projection["current_opportunity_id"] is None
    assert projection["reason_code"] == "legacy_adapter_clear"
    assert set(projection) == {
        "kind",
        "schema_version",
        "projection_revision",
        "current_opportunity_id",
        "reason_code",
        "updated_at",
    }
    assert "open_question" not in projection
    assert "generator_note" not in projection


@pytest.mark.asyncio
async def test_selected_opportunity_uses_remote_event_and_content_free_projection(
    tmp_path,
):
    class _RuntimeStore:
        def __init__(self) -> None:
            self.state = None
            self.events = []

        async def get_state(self, namespace: str, state_key: str):
            assert (namespace, state_key) == (
                "life_epistemic.projection",
                "current",
            )
            return self.state

        async def put_state(self, **kwargs):
            revision = int(kwargs["expected_revision"]) + 1
            self.state = SimpleNamespace(
                revision=revision,
                payload=dict(kwargs["payload"]),
            )
            return self.state

        async def append_event(self, **kwargs):
            existing = next(
                (
                    item
                    for item in self.events
                    if item.occurrence_id == kwargs["occurrence_id"]
                ),
                None,
            )
            if existing is not None:
                return existing
            record = SimpleNamespace(
                position=len(self.events) + 1,
                namespace=kwargs["namespace"],
                occurrence_id=kwargs["occurrence_id"],
                event_kind=kwargs["event_kind"],
                payload=dict(kwargs["payload"]),
            )
            self.events.append(record)
            return record

        async def read_events(
            self,
            namespace: str,
            *,
            after_position: int = 0,
            limit: int = 100,
        ):
            assert namespace == "life_epistemic.opportunities"
            return [
                item
                for item in self.events
                if item.position > after_position
            ][:limit]

    store = _RuntimeStore()
    first = CuriosityEngine(
        workspace_path=str(tmp_path),
        model_task_name="life",
        runtime_store=store,
    )
    opportunity = _opportunity()

    await first.save_opportunity(opportunity)

    restarted = CuriosityEngine(
        workspace_path=str(tmp_path),
        model_task_name="life",
        runtime_store=store,
    )
    assert await restarted.load_opportunity() == opportunity
    assert len(store.events) == 1
    assert store.events[0].payload == opportunity.to_dict()
    assert set(store.state.payload) == {
        "kind",
        "schema_version",
        "projection_revision",
        "current_opportunity_id",
        "current_event_position",
        "reason_code",
        "updated_at",
    }
    assert "open_question" not in store.state.payload
    assert "generator_note" not in store.state.payload
    assert not first.storage_dir.exists()
    assert not first.legacy_state_path.exists()

    await restarted.clear()
    assert await restarted.load_opportunity() is None
    assert len(store.events) == 1
    assert store.state.payload["current_opportunity_id"] is None
    assert store.state.payload["current_event_position"] is None


@pytest.mark.asyncio
async def test_duplicate_candidate_is_idempotent(tmp_path):
    engine = CuriosityEngine(workspace_path=str(tmp_path))
    opportunity = _opportunity()
    replay = EpistemicOpportunity.build(
        source_occurrence_id="evt-1",
        source_stream_id="stream-1",
        source_instance_id="chat_global",
        observed_gap="同一句确认重复出现",
        open_question="她是在确认在线，还是确认被记得？",
        possible_next_look="以后可再次观察相邻语境",
        generator_note="来源里保留了两种尚未区分的解释",
        generated_at="2026-08-06T12:05:00+08:00",
    )

    await engine.save_opportunity(opportunity)
    await engine.save_opportunity(replay)

    assert replay.opportunity_id == opportunity.opportunity_id
    assert replay.payload_sha256 != opportunity.payload_sha256
    assert len(engine.ledger_path.read_text(encoding="utf-8").splitlines()) == 1
    assert await engine.load_opportunity() == opportunity


@pytest.mark.asyncio
async def test_legacy_snapshot_migrates_without_modifying_source(tmp_path):
    engine = CuriosityEngine(workspace_path=str(tmp_path))
    legacy = {
        "active": True,
        "anchor": "一句话没说透",
        "why": "来源中仍有两个解释",
        "unknown": "她想被接住还是想转移话题？",
        "approach": "以后可以继续观察",
        "updated_at": "2026-08-06T12:00:00+08:00",
        "source_event_id": "legacy-event",
        "source_stream_id": "legacy-stream",
        "confidence": 0.7,
        "tags": ["旧标签"],
    }
    raw_bytes = json.dumps(legacy, ensure_ascii=False, indent=2).encode("utf-8")
    engine.legacy_state_path.write_bytes(raw_bytes)

    opportunity = await engine.load_opportunity()

    assert opportunity is not None
    assert opportunity.provenance == "legacy_curiosity_signal"
    assert opportunity.legacy_source_sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert opportunity.source_occurrence_id == "legacy-event"
    assert engine.legacy_state_path.read_bytes() == raw_bytes
    assert len(engine.ledger_path.read_text(encoding="utf-8").splitlines()) == 1
    legacy_view = await engine.load_signal()
    assert legacy_view.confidence == 0.0
    assert legacy_view.tags == []


@pytest.mark.asyncio
async def test_invalid_legacy_snapshot_fails_explicitly(tmp_path):
    engine = CuriosityEngine(workspace_path=str(tmp_path))
    engine.legacy_state_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(EpistemicOpportunityStateError) as exc_info:
        await engine.load_opportunity()

    assert exc_info.value.path == engine.legacy_state_path
    assert "JSONDecodeError" in exc_info.value.reason


@pytest.mark.asyncio
async def test_prompt_projection_uses_hard_utf8_budget_and_marks_omission(tmp_path):
    engine = CuriosityEngine(workspace_path=str(tmp_path))
    opportunity = _opportunity(question="星" * 600)
    await engine.save_opportunity(opportunity)

    text = await engine.format_for_prompt(max_chars=512)

    assert len(text.encode("utf-8")) <= 512
    text.encode("utf-8").decode("utf-8")
    assert "传输投影已按 UTF-8 字节预算省略" in text
    assert opportunity.opportunity_id in text


def test_candidate_field_budget_fails_instead_of_silently_cutting():
    with pytest.raises(ValueError, match="UTF-8 byte budget"):
        EpistemicOpportunity.build(
            source_occurrence_id="evt-1",
            observed_gap="星" * 2000,
            open_question="还没闭合的是什么？",
        )


def test_epistemic_opportunity_defaults_to_utility_with_realistic_deadline():
    config = LifeEngineConfig()

    assert config.curiosity.task_name == "utility"
    assert config.curiosity.timeout_seconds == 45.0


@pytest.mark.asyncio
async def test_generation_uses_one_deadline_across_send_and_response(tmp_path):
    engine = CuriosityEngine(
        workspace_path=str(tmp_path),
        model_task_name="utility",
    )
    engine.timeout_seconds = 0.03
    request = MagicMock()
    cleaned = asyncio.Event()

    async def response():
        try:
            await asyncio.sleep(3600)
        finally:
            cleaned.set()

    async def send(**_kwargs):
        await asyncio.sleep(0.02)
        return response()

    request.send = send
    started = asyncio.get_running_loop().time()
    with (
        patch(
            "plugins.life_engine.curiosity.engine.get_model_set_by_task",
            return_value=("utility-model",),
        ) as model_set,
        patch(
            "plugins.life_engine.curiosity.engine.create_llm_request",
            return_value=request,
        ),
        pytest.raises(EpistemicOpportunityTimeoutError) as exc_info,
    ):
        await engine.review_opportunity(
            prefix_prompt="private-prefix",
            history_text="private-history",
            new_event_text="private-event",
            source_occurrence_id="evt-deadline",
        )

    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.15
    assert cleaned.is_set()
    model_set.assert_called_once_with("utility")
    message = str(exc_info.value)
    assert "configured_timeout=0.030s" in message
    assert "task_name=utility" in message
    assert "source_occurrence_id=evt-deadline" in message
    assert "private-prefix" not in message
    assert "private-history" not in message
    assert "private-event" not in message


@pytest.mark.asyncio
async def test_send_stage_timeout_preserves_previous_candidate(tmp_path):
    engine = CuriosityEngine(workspace_path=str(tmp_path), model_task_name="utility")
    engine.timeout_seconds = 0.02
    previous = _opportunity(occurrence_id="evt-previous")
    await engine.save_opportunity(previous)
    request = MagicMock()
    cleaned = asyncio.Event()

    async def send(**_kwargs):
        try:
            await asyncio.sleep(3600)
        finally:
            cleaned.set()

    request.send = send
    with (
        patch(
            "plugins.life_engine.curiosity.engine.get_model_set_by_task",
            return_value=("utility-model",),
        ),
        patch(
            "plugins.life_engine.curiosity.engine.create_llm_request",
            return_value=request,
        ),
        pytest.raises(EpistemicOpportunityTimeoutError),
    ):
        await engine.review_opportunity(
            prefix_prompt="",
            history_text="",
            new_event_text="",
            source_occurrence_id="evt-send-timeout",
        )

    assert cleaned.is_set()
    assert await engine.load_opportunity() == previous
