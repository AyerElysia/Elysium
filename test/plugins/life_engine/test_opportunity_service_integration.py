"""Lightweight integration contracts; no running service or real database."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.opportunity.execution_context import (
    bind_capability_execution,
    current_capability_execution,
)
from plugins.life_engine.opportunity.legacy_gate import require_optional_capability
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.storage.opportunity_contracts import ProviderStatus


def _service(*, managed: bool = True, enabled: bool = True):
    state = SimpleNamespace(installed=enabled, enabled=enabled)
    binding = SimpleNamespace(
        status=ProviderStatus.ENABLED if enabled else ProviderStatus.PAUSED
    )
    return SimpleNamespace(
        opportunity_managed=managed,
        _opportunity_runtime=SimpleNamespace(
            stores=SimpleNamespace(
                authority=SimpleNamespace(
                    get_provider=AsyncMock(return_value=binding),
                    get_opportunity=AsyncMock(return_value=None),
                ),
                delivery=SimpleNamespace(
                    latest_publication=AsyncMock(return_value=None)
                ),
            ),
            registry=SimpleNamespace(state=AsyncMock(return_value=state)),
        ),
    )


@pytest.mark.parametrize(
    "capability",
    [
        "life.learning",
        "life.memory_review",
        "life.narrative_review",
        "life.initiative_reencounter",
        "life.inner_return",
    ],
)
async def test_managed_legacy_tool_requires_trusted_admission(capability: str) -> None:
    service = _service()
    with pytest.raises(PermissionError, match="RequiresCapabilityCall"):
        await require_optional_capability(service, capability)
    with bind_capability_execution(capability):
        await require_optional_capability(service, capability)
    assert current_capability_execution() == ""


async def test_pause_and_missing_runtime_fail_closed_even_with_admission() -> None:
    service = _service(enabled=False)
    with bind_capability_execution("life.memory_review"):
        with pytest.raises(PermissionError, match="NotEnabled"):
            await require_optional_capability(service, "life.memory_review")
        service._opportunity_runtime = None
        with pytest.raises(RuntimeError, match="NotReady"):
            await require_optional_capability(service, "life.memory_review")


async def test_wrong_capability_and_cancelled_context_do_not_leak() -> None:
    service = _service()
    with pytest.raises(asyncio.CancelledError):
        with bind_capability_execution("life.file_care"):
            with pytest.raises(PermissionError):
                await require_optional_capability(service, "life.memory_review")
            raise asyncio.CancelledError
    assert current_capability_execution() == ""


async def test_unmigrated_legacy_mode_does_not_need_registry() -> None:
    service = _service(managed=False)
    service._opportunity_runtime = None
    await require_optional_capability(service, "life.memory_review")


def test_durable_cutover_cannot_be_undone_by_old_config_default() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    service._cfg = lambda: SimpleNamespace(opportunity=SimpleNamespace(enabled=False))
    assert not service.opportunity_managed
    service._opportunity_cutover_marker = object()
    assert service.opportunity_managed


def _learning_lifecycle_service(scheduler):
    service = LifeEngineService.__new__(LifeEngineService)
    service._learning_scheduler = None
    service._learning_capability_ready = False
    service._learning_scheduler_parameters = {"workspace_path": "test-only"}
    service._learning_maintenance_task_id = None
    service._selectable_storage_enabled = False
    service._build_learning_runtime = Mock(return_value=scheduler)
    service._shared_learning_state = SimpleNamespace(close=AsyncMock())
    return service


async def test_learning_failed_initialize_does_not_leave_enabled_worker() -> None:
    scheduler = SimpleNamespace(
        initialize=AsyncMock(side_effect=OSError("initialization unavailable")),
        close=AsyncMock(),
    )
    service = _learning_lifecycle_service(scheduler)
    with pytest.raises(OSError):
        await service._apply_opportunity_capability_state(
            "life.learning", ProviderStatus.ENABLED
        )
    assert service._learning_scheduler is None
    assert not service._learning_capability_ready
    scheduler.close.assert_awaited_once()
    service._shared_learning_state.close.assert_not_awaited()


async def test_learning_partial_cleanup_is_retained_and_retryable() -> None:
    scheduler = SimpleNamespace(
        initialize=AsyncMock(side_effect=OSError("initialization unavailable")),
        close=AsyncMock(side_effect=[OSError("close unavailable"), None]),
    )
    service = _learning_lifecycle_service(scheduler)
    with pytest.raises(OSError):
        await service._apply_opportunity_capability_state(
            "life.learning", ProviderStatus.ENABLED
        )
    assert service._learning_scheduler is scheduler
    with pytest.raises(RuntimeError, match="CleanupRequired"):
        await service._apply_opportunity_capability_state(
            "life.learning", ProviderStatus.ENABLED
        )
    service._build_learning_runtime.assert_called_once()
    await service._apply_opportunity_capability_state(
        "life.learning", ProviderStatus.UNINSTALLED
    )
    assert service._learning_scheduler is None
    service._shared_learning_state.close.assert_not_awaited()


async def test_learning_enable_requires_projection_owner_and_disable_preserves_shared() -> (
    None
):
    scheduler = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
    service = _learning_lifecycle_service(scheduler)
    service._selectable_storage_enabled = True
    service._learning_stores = None
    with pytest.raises(RuntimeError, match="ProjectionOwnerUnavailable"):
        await service._apply_opportunity_capability_state(
            "life.learning", ProviderStatus.ENABLED
        )
    service._build_learning_runtime.assert_not_called()
    service._selectable_storage_enabled = False
    await service._apply_opportunity_capability_state(
        "life.learning", ProviderStatus.ENABLED
    )
    await service._apply_opportunity_capability_state(
        "life.learning", ProviderStatus.ENABLED
    )
    scheduler.initialize.assert_awaited_once()
    assert service._learning_capability_ready
    await service._apply_opportunity_capability_state(
        "life.learning", ProviderStatus.PAUSED
    )
    await service._apply_opportunity_capability_state(
        "life.learning", ProviderStatus.PAUSED
    )
    scheduler.close.assert_awaited_once()
    assert not service._learning_capability_ready
    assert service._learning_scheduler is None
    service._shared_learning_state.close.assert_not_awaited()


async def test_managed_legacy_schedule_neither_restores_nor_executes(
    monkeypatch,
) -> None:
    from plugins.life_engine.tools import schedule_tools

    plugin = SimpleNamespace(service=SimpleNamespace(opportunity_managed=True))
    monkeypatch.setattr(
        schedule_tools, "_get_store", lambda _: pytest.fail("old registry opened")
    )
    assert await schedule_tools.restore_life_schedules_when_ready(plugin) == {}
    record = SimpleNamespace(kind="heartbeat")
    with pytest.raises(RuntimeError, match="LegacyLifeScheduleRetired"):
        await schedule_tools._build_callback(plugin, record)()
    tool = schedule_tools.LifeEngineManageScheduleTool(plugin=plugin)
    success, result = await tool.execute(action="create", title="must not run")
    assert not success and result["history_preserved"]


def test_managed_message_does_not_auto_start_epistemic_llm() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    service._opportunity_cutover_marker = object()
    # No config, task manager, curiosity engine or message body is needed:
    # the retired producer must stop before touching any of those dependencies.
    service._schedule_curiosity_review(object(), object())


@pytest.mark.parametrize("selected", [False, True])
async def test_attach_borrows_exact_runtime_and_never_initializes_schema(
    selected: bool,
    monkeypatch,
) -> None:
    from plugins.life_engine.service import opportunity_runtime as module

    claim = object()
    storage = SimpleNamespace(acquire_singleton_writer=AsyncMock(return_value=claim))
    stores = SimpleNamespace(authority=object(), scheduler=object(), delivery=object())
    attach = AsyncMock(return_value=stores)
    monkeypatch.setattr(module, "open_opportunity_stores", attach)
    actor_check = AsyncMock(return_value=True)
    hold = object()
    service = SimpleNamespace(
        storage_runtime=storage if selected else None,
        _local_proactive_runtime=(
            None if selected else SimpleNamespace(runtime=storage, lease_seconds=30)
        ),
        _selectable_storage_enabled=selected,
        _storage_factory_settings=SimpleNamespace(authority_lease_seconds=30),
        _storage_writer_instance_id="instance:test",
        _validate_learning_decision_actor=actor_check,
        _proactive_actor_gate=SimpleNamespace(hold=hold),
        _opportunity_wake_event=asyncio.Event(),
        _get_life_event_store=lambda: SimpleNamespace(append=AsyncMock()),
        _apply_opportunity_capability_state=AsyncMock(),
    )
    if not selected:
        with pytest.raises(RuntimeError, match="SelectedStorageRequired"):
            await module.attach_opportunity_runtime(service)
        attach.assert_not_awaited()
        storage.acquire_singleton_writer.assert_not_awaited()
        return
    runtime = await module.attach_opportunity_runtime(service)
    assert runtime.stores is stores
    assert attach.await_count == 2
    for call in attach.await_args_list:
        assert call.args == (storage,)
        assert call.kwargs["initialize_schema"] is False
    final = attach.await_args_list[-1].kwargs
    assert final["writer_claim"] is claim
    assert final["validate_active_actor"] is None
    assert final["actor_decision_guard"] is None
    # Discovery attaches no native executors and adopts no defaults.
    service._apply_opportunity_capability_state.assert_not_awaited()
    assert not service._opportunity_wake_event.is_set()


async def test_cutover_load_reads_the_already_open_runtime(monkeypatch) -> None:
    from plugins.life_engine.storage import opportunity_schema

    storage, marker = object(), object()
    read = AsyncMock(return_value=marker)
    monkeypatch.setattr(opportunity_schema, "read_opportunity_runtime_marker", read)
    service = LifeEngineService.__new__(LifeEngineService)
    service._storage_runtime = storage
    service._selectable_storage_enabled = True
    service._local_proactive_runtime = None
    await service._load_opportunity_runtime_mode()
    read.assert_awaited_once_with(storage)
    assert service._opportunity_cutover_marker is marker
    assert service.opportunity_managed


async def test_unmanaged_startup_does_not_attach_opportunity_runtime(
    monkeypatch,
) -> None:
    from plugins.life_engine.service import opportunity_runtime as module

    attach = AsyncMock(side_effect=AssertionError("must not attach"))
    monkeypatch.setattr(module, "attach_opportunity_runtime", attach)
    service = LifeEngineService.__new__(LifeEngineService)
    service._cfg = lambda: SimpleNamespace(opportunity=SimpleNamespace(enabled=False))
    service._opportunity_cutover_marker = None
    service._opportunity_runtime = None
    await service._initialize_opportunity_runtime()
    attach.assert_not_called()
    assert service._opportunity_runtime is None


async def test_enabled_switch_without_schema_fails_closed_and_names_prepare_script(
    monkeypatch,
) -> None:
    from plugins.life_engine.service import opportunity_runtime as module
    from plugins.life_engine.storage.opportunity_schema import OpportunitySchemaNotReady

    monkeypatch.setattr(
        module,
        "attach_opportunity_runtime",
        AsyncMock(
            side_effect=OpportunitySchemaNotReady(
                "OpportunitySchemaNotReady:missing_table:opportunity_provider_events"
            )
        ),
    )
    service = LifeEngineService.__new__(LifeEngineService)
    service._cfg = lambda: SimpleNamespace(opportunity=SimpleNamespace(enabled=True))
    service._opportunity_cutover_marker = None
    with pytest.raises(OpportunitySchemaNotReady, match="prepare_opportunity_runtime"):
        await service._initialize_opportunity_runtime()


async def test_startup_wakes_unconsumed_opportunity_even_if_first_seen_was_saved(
    monkeypatch,
):
    from plugins.life_engine.service import opportunity_runtime as module

    runtime = SimpleNamespace(
        restore=AsyncMock(),
        health=AsyncMock(return_value={"status": "healthy"}),
    )
    monkeypatch.setattr(
        module, "attach_opportunity_runtime", AsyncMock(return_value=runtime)
    )
    service = LifeEngineService.__new__(LifeEngineService)
    service._opportunity_cutover_marker = object()
    service._opportunity_wake_event = asyncio.Event()
    service._event_history = [
        SimpleNamespace(
            content_type="opportunity.available",
            heartbeat_context_consumed=False,
        )
    ]
    service._pending_events = []
    await service._initialize_opportunity_runtime()
    assert service._opportunity_wake_event.is_set()
    runtime.restore.assert_awaited_once()
    service._opportunity_wake_event.clear()
    service._event_history[0].heartbeat_context_consumed = True
    assert not service._has_pending_opportunity_context()


def test_delivery_retry_is_bounded_and_does_not_create_or_complete_work():
    service = LifeEngineService.__new__(LifeEngineService)
    service._opportunity_wake_event = asyncio.Event()
    service._opportunity_health = {"scheduler_owner": True}
    assert service._request_opportunity_delivery_retry(1) == 5.0
    assert service._request_opportunity_delivery_retry(2) == 10.0
    assert service._request_opportunity_delivery_retry(10000) == 60.0
    assert service._opportunity_wake_event.is_set()
    assert service._opportunity_health["scheduler_owner"]
    assert service._opportunity_health["delivery_retry_pending"]
    assert "completed" not in service._opportunity_health


async def test_managed_heartbeat_retry_counts_each_round_once():
    service = LifeEngineService.__new__(LifeEngineService)
    service._state = SimpleNamespace(running=True, self_pause_until="")
    service._cfg = lambda: SimpleNamespace(
        settings=SimpleNamespace(log_heartbeat=False)
    )
    service._effective_heartbeat_interval = lambda: 1
    service._opportunity_runtime = object()
    service._opportunity_health = {}
    service._opportunity_wake_event = asyncio.Event()
    service._opportunity_wake_event.set()
    service._stop_event = asyncio.Event()
    service._sleep_state_active = False
    service._self_pause_skip_logged = False
    service._in_sleep_window_now = lambda: (False, "")
    service._self_pause_status = lambda: (False, None, "", "")
    service._has_pending_opportunity_context = lambda: True
    retries, delays = [], []

    def retry(failure_count):
        retries.append(failure_count)
        delays.append(
            LifeEngineService._request_opportunity_delivery_retry(
                service, failure_count
            )
        )
        return 0.0  # No real sleep in this isolated loop test.

    async def run_round(**_kwargs):
        if len(retries) == 2:
            service._state.running = False
        return "", SimpleNamespace(content="")

    service._request_opportunity_delivery_retry = retry
    service._run_heartbeat_round = run_round
    await service._heartbeat_loop()
    assert retries == [1, 2, 3]
    assert delays == [5.0, 10.0, 20.0]


async def test_managed_prompt_uses_real_capability_entry_without_loading_defaults():
    from plugins.life_engine.prompts.sections import (
        HeartbeatCapabilityCatalogSection,
        SectionContext,
    )

    service = LifeEngineService.__new__(LifeEngineService)
    service._opportunity_cutover_marker = object()
    header = "\n".join(service._build_prompt_header())
    assert "nucleus_capability_call" in header
    assert "operation_schema" in header
    assert "nucleus_learn action=help" not in header
    assert "skills/learning/SKILL.md" not in header
    ctx = SectionContext(service=service, config=object(), today_str="2026-09-05")
    catalog = await HeartbeatCapabilityCatalogSection().render(ctx)
    assert catalog and len(catalog.encode("utf-8")) <= 1024
    assert "nucleus_opportunity_query" in catalog
    assert "nucleus_capability_call" in catalog
    assert "nucleus_write_narrative" not in catalog


async def test_managed_mode_never_renders_legacy_invitation_page(monkeypatch) -> None:
    from plugins.life_engine.opportunity import bus as module

    collect = AsyncMock(side_effect=AssertionError("old semantic producers called"))
    monkeypatch.setattr(module, "collect_all_offers", collect)
    bus = module.OpportunityBus(SimpleNamespace(opportunity_managed=True))
    assert await bus.collect_and_render() is None
    collect.assert_not_awaited()


async def test_initiative_reference_replays_without_body_or_reopening() -> None:
    from plugins.life_engine.opportunity.source_bridge import (
        offer_initiative_reencounter,
    )

    service = _service()
    authority = service._opportunity_runtime.stores.authority
    authority.get_provider.return_value = SimpleNamespace(
        status=ProviderStatus.ENABLED,
        workflow_id="workflow:initiative",
        workflow_revision=1,
        workflow_sha256="a" * 64,
    )
    authority.get_opportunity = AsyncMock(return_value=None)
    authority.propose_opportunity = AsyncMock()
    seed = SimpleNamespace(
        seed_id="initiative:1",
        status="open",
        reencounter_at="2026-09-05T00:00:00+00:00",
        reencounter_revision=2,
        reencounter_event_id="initiative:event:2",
        reencounter_delivered_at="",
        current_statement="private" * 300000,
    )
    assert await offer_initiative_reencounter(service._opportunity_runtime, seed)
    proposal = authority.propose_opportunity.await_args.args[0]
    assert not hasattr(proposal, "current_statement")
    assert proposal.source_occurrence_ids == ("initiative:event:2",)
    assert proposal.referent_kind == "initiative.reencounter_reference.v1"
    assert len(repr(proposal).encode("utf-8")) < 2048
    authority.get_opportunity.return_value = SimpleNamespace(
        referent_sha256=proposal.referent_sha256, status="closed"
    )
    assert not await offer_initiative_reencounter(service._opportunity_runtime, seed)
    authority.propose_opportunity.assert_awaited_once()
    authority.get_opportunity.return_value.referent_sha256 = "f" * 64
    with pytest.raises(RuntimeError, match="SourceReferenceConflict"):
        await offer_initiative_reencounter(service._opportunity_runtime, seed)


@pytest.mark.parametrize("status", [ProviderStatus.PAUSED, ProviderStatus.UNINSTALLED])
async def test_unavailable_initiative_provider_cannot_publish(status) -> None:
    from plugins.life_engine.opportunity.source_bridge import (
        offer_initiative_reencounter,
    )

    service = _service()
    authority = service._opportunity_runtime.stores.authority
    authority.get_provider.return_value = SimpleNamespace(status=status)
    authority.propose_opportunity = AsyncMock()
    seed = SimpleNamespace(
        seed_id="initiative:1",
        status="open",
        reencounter_at="2026-09-05T00:00:00+00:00",
        reencounter_revision=2,
        reencounter_event_id="initiative:event:2",
        reencounter_delivered_at="",
    )
    assert not await offer_initiative_reencounter(service._opportunity_runtime, seed)
    authority.propose_opportunity.assert_not_awaited()


async def test_source_ack_recovers_from_durable_publication_after_uninstall() -> None:
    from plugins.life_engine.opportunity.source_bridge import (
        offer_initiative_reencounter,
    )

    service = _service()
    runtime = service._opportunity_runtime
    authority = runtime.stores.authority
    authority.get_provider.return_value = SimpleNamespace(
        status=ProviderStatus.ENABLED,
        workflow_id="workflow:1",
        workflow_revision=1,
        workflow_sha256="a" * 64,
    )
    authority.propose_opportunity = AsyncMock()
    seed = SimpleNamespace(
        seed_id="initiative:recovery",
        status="open",
        reencounter_at="2026-09-05T00:00:00+00:00",
        reencounter_revision=2,
        reencounter_event_id="initiative:event:2",
        reencounter_delivered_at="",
    )
    assert await offer_initiative_reencounter(runtime, seed)
    proposal = authority.propose_opportunity.await_args.args[0]
    authority.get_opportunity.return_value = SimpleNamespace(
        referent_sha256=proposal.referent_sha256,
        status="closed",
    )
    authority.get_provider.return_value.status = ProviderStatus.UNINSTALLED
    record = AsyncMock(side_effect=[OSError("source unavailable"), None, None])
    # The append has not been durably verified yet: no source acknowledgement.
    assert not await offer_initiative_reencounter(
        runtime,
        seed,
        record_source_publication=record,
    )
    record.assert_not_awaited()
    publication = SimpleNamespace(
        life_event_occurrence_id="life-event:exact-publication",
        updated_at="2026-09-05T00:00:03+00:00",
    )
    runtime.stores.delivery.latest_publication.return_value = publication
    with pytest.raises(OSError, match="source unavailable"):
        await offer_initiative_reencounter(
            runtime,
            seed,
            record_source_publication=record,
        )
    # A new scanner/runtime can read the same publication without reopening
    # the offer. Retry uses the same immutable event id and stable receipt time.
    for _ in range(2):
        assert not await offer_initiative_reencounter(
            runtime,
            seed,
            record_source_publication=record,
        )
    assert record.await_count == 3
    assert all(
        call.kwargs
        == {
            "seed_id": seed.seed_id,
            "seed_revision": 2,
            "life_event_id": publication.life_event_occurrence_id,
            "occurred_at": publication.updated_at,
        }
        for call in record.await_args_list
    )
    authority.propose_opportunity.assert_awaited_once()
    assert seed.reencounter_delivered_at == ""


def test_closed_offer_does_not_starve_newer_initiative_source() -> None:
    from plugins.life_engine.opportunity.source_bridge import next_initiative_scan_batch

    due = tuple(SimpleNamespace(seed_id=f"seed:{i:04d}") for i in range(90))
    after, seen = "", set()
    for _ in range(3):
        batch = next_initiative_scan_batch(due, after_id=after, limit=32)
        seen.update(item.seed_id for item in batch)
        after = batch[-1].seed_id
    assert len(seen) == 90
    assert next_initiative_scan_batch(due, after_id=after)[0] is due[0]
    assert next_initiative_scan_batch(due, after_id="")[0] is due[0]


async def test_owned_task_cancellation_is_not_parent_cancellation(monkeypatch) -> None:
    from plugins.life_engine.service import core

    ready = asyncio.Event()

    async def child():
        ready.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(child())
    await ready.wait()
    monkeypatch.setattr(
        core,
        "get_task_manager",
        lambda: SimpleNamespace(get_task=lambda _: SimpleNamespace(task=task)),
    )
    service = LifeEngineService.__new__(LifeEngineService)
    task.cancel()
    await service._await_managed_task("owned", timeout=0.01, strict=True)
    assert task.cancelled()
    assert not asyncio.current_task().cancelling()


async def test_uncooperative_owned_task_reports_bounded_failure(monkeypatch) -> None:
    from plugins.life_engine.service import core

    ready, release = asyncio.Event(), asyncio.Event()

    async def stubborn():
        ready.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    task = asyncio.create_task(stubborn())
    await ready.wait()
    monkeypatch.setattr(
        core,
        "get_task_manager",
        lambda: SimpleNamespace(get_task=lambda _: SimpleNamespace(task=task)),
    )
    service = LifeEngineService.__new__(LifeEngineService)
    try:
        with pytest.raises(RuntimeError, match="ManagedTaskQuiescenceTimeout"):
            await service._await_managed_task("owned", timeout=0.01, strict=True)
        assert not task.done()
    finally:
        release.set()
        await task


async def test_caller_cancel_during_stop_propagates_without_cancelling_child(
    monkeypatch,
) -> None:
    from plugins.life_engine.service import core

    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    monkeypatch.setattr(
        core,
        "get_task_manager",
        lambda: SimpleNamespace(get_task=lambda _: SimpleNamespace(task=task)),
    )
    service = LifeEngineService.__new__(LifeEngineService)
    waiter = asyncio.create_task(
        service._await_managed_task("owned", timeout=10, strict=True)
    )
    await asyncio.sleep(0)
    waiter.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not task.cancelled()
    finally:
        release.set()
        await task
