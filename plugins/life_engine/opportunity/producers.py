"""Collect due opportunity facts. Producers never write cooldown on collect."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.app.plugin_system.api import log_api

from .contracts import OpportunityOffer
from .render import clip_utf8

logger = log_api.get_logger("life_engine.opportunity")

CommitHook = Callable[[Any], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class CollectedOffer:
    offer: OpportunityOffer
    commit: CommitHook | None = None


def _now() -> datetime:
    return datetime.now(UTC).astimezone()


def _now_iso(now: datetime | None = None) -> str:
    return (now or _now()).isoformat()


def _config(service: Any, override: Any | None = None) -> Any | None:
    if override is not None:
        return override
    cfg_fn = getattr(service, "_cfg", None)
    if callable(cfg_fn):
        return cfg_fn()
    plugin = getattr(service, "plugin", None)
    return getattr(plugin, "config", None)


async def collect_continuity_offers(service: Any) -> list[CollectedOffer]:
    """Read open AttentionThread / InitiativeSeed. Never create or rewrite them."""

    collected: list[CollectedOffer] = []
    observed_at = _now_iso()
    authority = getattr(service, "_proactive_authority", None)
    if authority is None:
        return collected

    page_fn = getattr(service, "page_attention_threads", None)
    if callable(page_fn):
        try:
            from ..attention_threads.contracts import (
                ATTENTION_THREAD_MIN_PAGE_BYTES,
                AttentionThreadPageQuery,
            )

            page = await page_fn(
                AttentionThreadPageQuery(
                    statuses=("open", "paused"),
                    limit=16,
                    max_bytes=ATTENTION_THREAD_MIN_PAGE_BYTES,
                    projection_kind="heartbeat_opportunity_continuity",
                    focus_instance_id="chat_global",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "continuity attention projection unavailable: "
                f"error_type={type(exc).__name__}"
            )
        else:
            for item in tuple(getattr(page, "items", ()) or ()):
                thread_id = str(getattr(item, "thread_id", "") or "").strip()
                if not thread_id:
                    continue
                excerpt = str(getattr(item, "statement_excerpt", "") or "").strip()
                collected.append(
                    CollectedOffer(
                        OpportunityOffer(
                            offer_id=f"attention:{thread_id}",
                            kind="continuity",
                            domain="attention",
                            producer="proactive_authority",
                            observed_at=observed_at,
                            facts={
                                "thread_id": thread_id,
                                "status": str(getattr(item, "status", "") or ""),
                                "revision": int(getattr(item, "revision", 0) or 0),
                                "excerpt": clip_utf8(excerpt, 160),
                                "excerpt_complete": bool(
                                    getattr(item, "excerpt_complete", True)
                                ),
                                "statement_bytes": int(
                                    getattr(item, "statement_bytes", 0) or 0
                                ),
                            },
                            disclosure_ref=(
                                "nucleus_proactive_query",
                                "nucleus_proactive_command",
                            ),
                        )
                    )
                )

    list_fn = getattr(service, "list_initiatives", None)
    if callable(list_fn):
        try:
            seeds = await list_fn(include_released=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "continuity initiative projection unavailable: "
                f"error_type={type(exc).__name__}"
            )
            seeds = ()
        for seed in tuple(seeds or ()):
            status = str(getattr(seed, "status", "") or "").strip()
            if status and status != "open":
                continue
            seed_id = str(getattr(seed, "seed_id", "") or "").strip()
            if not seed_id:
                continue
            statement = str(getattr(seed, "current_statement", "") or "").strip()
            collected.append(
                CollectedOffer(
                    OpportunityOffer(
                        offer_id=f"initiative:{seed_id}",
                        kind="continuity",
                        domain="initiative",
                        producer="proactive_authority",
                        observed_at=observed_at,
                        facts={
                            "seed_id": seed_id,
                            "status": status or "open",
                            "revision": int(getattr(seed, "revision", 0) or 0),
                            "excerpt": clip_utf8(statement, 160),
                            "statement_bytes": len(statement.encode("utf-8")),
                        },
                        disclosure_ref=(
                            "nucleus_proactive_query",
                            "nucleus_proactive_command",
                        ),
                    )
                )
            )
    return collected


async def collect_learning_invitation(service: Any) -> CollectedOffer | None:
    scheduler = getattr(service, "_learning_scheduler", None)
    if scheduler is None or not bool(getattr(scheduler, "projector_owner", True)):
        return None
    collect = getattr(scheduler, "collect_subject_review_offer_facts", None)
    if not callable(collect):
        return None
    facts = await collect()
    if not isinstance(facts, dict) or not facts.get("due_count"):
        return None
    observed_at = _now_iso()

    async def commit(receipt: Any) -> bool:
        pending = getattr(scheduler, "get_pending_subject_review_offer", None)
        offer = pending() if callable(pending) else None
        if not isinstance(offer, dict):
            return False
        identity = str(offer.get("delivery_id") or "").strip()
        if not identity:
            return False
        return bool(
            await scheduler.commit_subject_review_offer_delivery(identity, receipt)
        )

    return CollectedOffer(
        OpportunityOffer(
            offer_id="learning:subject_review",
            kind="invitation",
            domain="learning",
            producer="learning_scheduler",
            observed_at=observed_at,
            facts=facts,
            disclosure_ref=(
                "nucleus_learn",
                "skills/learning",
            ),
        ),
        commit=commit,
    )


async def collect_memory_invitation(service: Any) -> CollectedOffer | None:
    from ..memory.prompting import (
        analyze_memory_text,
        should_emit_memory_maintenance_prompt,
    )

    read = getattr(service, "read_subject_authority_texts", None)
    if not callable(read):
        return None
    texts = await read()
    memory_raw = ""
    if isinstance(texts, dict):
        memory_raw = str(texts.get("MEMORY.md") or "")
    memory_data = analyze_memory_text(memory_raw)
    last_at = getattr(service, "_last_memory_maintenance_prompt_at", None)
    if not should_emit_memory_maintenance_prompt(memory_data, last_at):
        return None
    observed_at = _now_iso()

    async def commit(_receipt: Any) -> bool:
        service._last_memory_maintenance_prompt_at = _now_iso()
        return True

    return CollectedOffer(
        OpportunityOffer(
            offer_id="memory:maintenance",
            kind="invitation",
            domain="memory",
            producer="memory_prompting",
            observed_at=observed_at,
            facts={
                "size_bytes": int(memory_data.size_bytes),
                "durable_count": len(memory_data.durable_items),
                "active_count": len(memory_data.active_items),
                "fading_count": len(memory_data.fading_items),
                "reasons": list(memory_data.maintenance_reasons[:3]),
            },
            disclosure_ref=("nucleus_memory_continuity_review",),
        ),
        commit=commit,
    )


def narrative_invitation_enabled(config: Any | None, service: Any) -> bool:
    if config is None or getattr(service, "_workspace_dir", None) is None:
        return False
    narrative_cfg = getattr(config, "narrative", None)
    if narrative_cfg is None:
        return False
    return bool(getattr(narrative_cfg, "enabled", True)) and bool(
        getattr(narrative_cfg, "inject_to_heartbeat", True)
    )


async def collect_narrative_invitation(
    service: Any,
    *,
    config: Any | None = None,
    now: datetime | None = None,
) -> CollectedOffer | None:
    from ..narrative.store import _parse_iso

    cfg = _config(service, config)
    if not narrative_invitation_enabled(cfg, service):
        return None
    narrative_cfg = cfg.narrative
    store = service.narrative_store()
    state = await store.load_state()
    moment = now or _now()
    last_consolidated = _parse_iso(state.get("last_consolidated_at", ""))
    if last_consolidated is not None:
        elapsed_hours = (moment - last_consolidated).total_seconds() / 3600.0
        if elapsed_hours < float(narrative_cfg.min_interval_hours):
            return None
    last_invited = _parse_iso(state.get("last_invited_at", ""))
    if last_invited is not None:
        since_invite = (moment - last_invited).total_seconds() / 3600.0
        if since_invite < float(narrative_cfg.invite_cooldown_hours):
            return None
    records = await service.life_trace_store().recent(limit=500)
    pending = store.pending_moments(records, state)
    if len(pending) < int(narrative_cfg.min_moments):
        return None
    shown = pending[-int(narrative_cfg.max_moments_shown) :]
    moments = []
    for record in shown:
        label = record.summary or record.path or record.operation
        moments.append(
            {
                "timestamp": str(record.timestamp)[:16],
                "kind": record.kind,
                "label": clip_utf8(str(label), 80),
            }
        )
    last_entry = await store.last_entry()
    last_excerpt = ""
    if last_entry is not None and last_entry.text:
        last_excerpt = clip_utf8(last_entry.text, 80)
    observed_at = _now_iso(moment)

    async def commit(_receipt: Any) -> bool:
        await store.mark_invited(now=moment)
        return True

    return CollectedOffer(
        OpportunityOffer(
            offer_id="narrative:river",
            kind="invitation",
            domain="narrative",
            producer="narrative_store",
            observed_at=observed_at,
            facts={
                "pending_count": len(pending),
                "moments": moments,
                "older_count": max(0, len(pending) - len(shown)),
                "last_entry_excerpt": last_excerpt,
            },
            disclosure_ref=("nucleus_write_narrative",),
        ),
        commit=commit,
    )


async def collect_file_care_invitation(
    service: Any,
    *,
    now: datetime | None = None,
) -> CollectedOffer | None:
    from ..prompts.sections import (
        _FILE_CARE_COOLDOWN_HOURS,
        _read_file_care_invited_at,
        _write_file_care_invited_at,
        inspect_handwritten_diary_clutter,
    )

    workspace_fn = getattr(service, "_workspace_dir", None)
    if workspace_fn is None:
        return None
    try:
        workspace = workspace_fn()
    except Exception:  # noqa: BLE001
        return None
    moment = now or _now()
    last_invited = _read_file_care_invited_at(workspace)
    if last_invited is not None:
        elapsed = (moment - last_invited).total_seconds() / 3600.0
        if elapsed < _FILE_CARE_COOLDOWN_HOURS:
            return None
    census = inspect_handwritten_diary_clutter(workspace / "diaries")
    if census is None:
        return None
    observed_at = _now_iso(moment)

    async def commit(_receipt: Any) -> bool:
        try:
            _write_file_care_invited_at(workspace, moment)
        except OSError:
            logger.debug("file care cooldown state could not be written")
            return False
        return True

    return CollectedOffer(
        OpportunityOffer(
            offer_id="file_care:diaries",
            kind="invitation",
            domain="file_care",
            producer="file_care_census",
            observed_at=observed_at,
            facts=dict(census),
            disclosure_ref=("nucleus_mkdir", "nucleus_read_file"),
        ),
        commit=commit,
    )


async def collect_epistemic_invitation(
    service: Any,
    *,
    config: Any | None = None,
) -> CollectedOffer | None:
    cfg = _config(service, config)
    curiosity_cfg = getattr(cfg, "curiosity", None) if cfg is not None else None
    enabled = curiosity_cfg is None or (
        bool(getattr(curiosity_cfg, "enabled", True))
        and bool(getattr(curiosity_cfg, "inject_to_heartbeat", True))
    )
    if not enabled:
        return None
    engine_fn = getattr(service, "_get_curiosity_engine", None)
    if not callable(engine_fn):
        return None
    try:
        opportunity = await engine_fn().load_opportunity()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "epistemic opportunity unavailable: "
            f"error_type={type(exc).__name__}"
        )
        return None
    if opportunity is None:
        return None
    observed_at = _now_iso()
    return CollectedOffer(
        OpportunityOffer(
            offer_id=f"epistemic:{opportunity.opportunity_id}",
            kind="invitation",
            domain="epistemic",
            producer="curiosity_engine",
            observed_at=observed_at,
            facts={
                "opportunity_id": opportunity.opportunity_id,
                "source_occurrence_id": opportunity.source_occurrence_id,
                "observed_gap": clip_utf8(opportunity.observed_gap, 200),
                "open_question": clip_utf8(opportunity.open_question, 200),
                "possible_next_look": clip_utf8(opportunity.possible_next_look, 200),
                "generator_note": clip_utf8(opportunity.generator_note, 200),
            },
            disclosure_ref=("nucleus_proactive_query", "nucleus_proactive_command"),
        )
    )


async def collect_all_offers(
    service: Any,
    *,
    config: Any | None = None,
    now: datetime | None = None,
) -> tuple[list[CollectedOffer], tuple[str, ...]]:
    """Collect every producer; isolate failures as error_type names only."""

    collected: list[CollectedOffer] = []
    errors: list[str] = []

    async def _run(name: str, factory: Callable[[], Awaitable[Any]]) -> None:
        try:
            result = await factory()
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
            logger.debug(
                f"opportunity producer failed: producer={name} "
                f"error_type={type(exc).__name__}"
            )
            return
        if result is None:
            return
        if isinstance(result, list):
            collected.extend(result)
            return
        collected.append(result)

    await _run("continuity", lambda: collect_continuity_offers(service))
    await _run("learning", lambda: collect_learning_invitation(service))
    await _run("memory", lambda: collect_memory_invitation(service))
    await _run(
        "narrative",
        lambda: collect_narrative_invitation(service, config=config, now=now),
    )
    await _run("file_care", lambda: collect_file_care_invitation(service, now=now))
    await _run(
        "epistemic",
        lambda: collect_epistemic_invitation(service, config=config),
    )
    return collected, tuple(dict.fromkeys(errors))
