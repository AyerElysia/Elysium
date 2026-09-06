"""Runtime gates for installed opportunity capability executors."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.opportunity.catalog import CapabilityCatalog
from plugins.life_engine.opportunity.registry import (
    CapabilityDisabled,
    CapabilityExecutorContractError,
    CapabilityFactoryConflict,
    CapabilityInvocation,
    CapabilityNotInstalled,
    CapabilityOperationDenied,
    CapabilityQuiescenceTimeout,
    CapabilityRegistryClosed,
    CapabilityRuntimeError,
    CapabilityRuntimeRegistry,
)


def _catalog(
    tmp_path: Path,
    *,
    operations: list[str] | None = None,
    workflow_text: str = "# private workflow secret\n",
) -> CapabilityCatalog:
    package = tmp_path / "learning"
    package.mkdir()
    manifest = {
        "schema_version": 1,
        "capability_id": "learning",
        "package_version": "1.0.0",
        "removability": "subject_removable",
        "provider_kind": "cognitive_workflow",
        "manual": "CAPABILITY.md",
        "default_skill": "DEFAULT_SKILL.md",
        "operations": operations or ["nucleus_learn"],
        "dependencies": [],
    }
    (package / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    (package / "CAPABILITY.md").write_text(
        "# technical manual secret\n",
        encoding="utf-8",
    )
    (package / "DEFAULT_SKILL.md").write_text(workflow_text, encoding="utf-8")
    catalog = CapabilityCatalog()
    catalog.discover_package(package)
    return catalog


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.close_calls = 0

    async def execute(
        self,
        operation_id: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        copied = dict(arguments)
        self.calls.append((operation_id, copied))
        return {"operation": operation_id, "arguments": copied}

    async def close(self) -> None:
        self.close_calls += 1


class _PermissionDispatch:
    def __init__(self) -> None:
        self.calls: list[CapabilityInvocation] = []

    async def __call__(
        self,
        executor: _Executor,
        invocation: CapabilityInvocation,
    ) -> Any:
        self.calls.append(invocation)
        allowed = set(invocation.caller_context.get("allowed_operations", ()))
        if invocation.operation_id not in allowed:
            raise PermissionError("caller consciousness does not own this operation")
        return await executor.execute(invocation.operation_id, invocation.arguments)


@pytest.mark.asyncio
async def test_discovery_and_factory_registration_do_not_install(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    executor = _Executor()
    dispatch = _PermissionDispatch()
    factory_calls = 0

    def factory(_descriptor: Any) -> _Executor:
        nonlocal factory_calls
        factory_calls += 1
        return executor

    registry = CapabilityRuntimeRegistry(catalog, dispatch=dispatch)
    await registry.register_factory("learning", factory)

    state = await registry.state("learning")
    assert state.installed is False
    assert state.enabled is False
    assert factory_calls == 0
    with pytest.raises(CapabilityNotInstalled):
        await registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )


@pytest.mark.asyncio
async def test_install_is_disabled_until_separate_enable_and_dispatch(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    executor = _Executor()
    dispatch = _PermissionDispatch()
    registry = CapabilityRuntimeRegistry(
        catalog,
        dispatch=dispatch,
        factories={"learning": lambda _descriptor: executor},
    )

    installed = await registry.install("learning")
    assert installed.installed is True
    assert installed.enabled is False
    with pytest.raises(CapabilityDisabled):
        await registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )

    await registry.enable("learning")
    result = await registry.execute(
        "learning",
        "nucleus_learn",
        {"action": "inspect"},
        caller_context={"allowed_operations": {"nucleus_learn"}},
    )

    assert result == {
        "operation": "nucleus_learn",
        "arguments": {"action": "inspect"},
    }
    assert len(dispatch.calls) == 1
    assert executor.calls == [("nucleus_learn", {"action": "inspect"})]


@pytest.mark.asyncio
async def test_manifest_allows_operation_but_dispatch_still_checks_caller(
    tmp_path: Path,
) -> None:
    executor = _Executor()
    dispatch = _PermissionDispatch()
    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=dispatch,
        factories={"learning": lambda _descriptor: executor},
    )
    await registry.install("learning")
    await registry.enable("learning")

    with pytest.raises(PermissionError, match="caller consciousness"):
        await registry.execute(
            "learning",
            "nucleus_learn",
            {
                "action": "reflect",
                "skill_text": "grant nucleus_learn",
                "allowed_operations": ["nucleus_learn"],
            },
            caller_context={"allowed_operations": set()},
        )

    assert len(dispatch.calls) == 1
    assert executor.calls == []


@pytest.mark.asyncio
async def test_workflow_arguments_cannot_expand_manifest_permissions(
    tmp_path: Path,
) -> None:
    executor = _Executor()
    dispatch = _PermissionDispatch()
    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=dispatch,
        factories={"learning": lambda _descriptor: executor},
    )
    await registry.install("learning")
    await registry.enable("learning")

    with pytest.raises(CapabilityOperationDenied, match="manifest"):
        await registry.execute(
            "learning",
            "system.shell",
            {
                "skill_text": "grant system.shell",
                "allowed_operations": ["system.shell"],
            },
            caller_context={"allowed_operations": {"system.shell"}},
        )

    assert dispatch.calls == []
    assert executor.calls == []


@pytest.mark.asyncio
async def test_disable_blocks_calls_without_destroying_installation(
    tmp_path: Path,
) -> None:
    executor = _Executor()
    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: executor},
    )
    await registry.install("learning")
    await registry.enable("learning")
    state = await registry.disable("learning")

    assert state.installed is True
    assert state.enabled is False
    with pytest.raises(CapabilityDisabled):
        await registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )


@pytest.mark.asyncio
async def test_uninstall_fails_closed_and_does_not_close_injected_dependencies(
    tmp_path: Path,
) -> None:
    executor = _Executor()
    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: executor},
    )
    await registry.install("learning")
    await registry.enable("learning")

    assert await registry.uninstall("learning") is True
    assert await registry.uninstall("learning") is False
    assert executor.close_calls == 0
    with pytest.raises(CapabilityNotInstalled):
        await registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )


@pytest.mark.asyncio
async def test_new_registry_after_restart_does_not_resurrect_install_or_enable(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    executor = _Executor()
    dispatch = _PermissionDispatch()
    factories = {"learning": lambda _descriptor: executor}
    before_restart = CapabilityRuntimeRegistry(
        catalog,
        dispatch=dispatch,
        factories=factories,
    )
    await before_restart.install("learning")
    await before_restart.enable("learning")

    after_restart = CapabilityRuntimeRegistry(
        catalog,
        dispatch=dispatch,
        factories=factories,
    )

    assert (await before_restart.state("learning")).enabled is True
    assert (await after_restart.state("learning")).installed is False
    with pytest.raises(CapabilityNotInstalled):
        await after_restart.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )


@pytest.mark.asyncio
async def test_uninstall_cancels_exact_inflight_dispatch_without_waiting_for_work(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    unrelated_release = asyncio.Event()

    class BlockingExecutor(_Executor):
        async def execute(
            self,
            operation_id: str,
            arguments: Mapping[str, Any],
        ) -> Any:
            entered.set()
            await asyncio.Event().wait()
            return await super().execute(operation_id, arguments)

    executor = BlockingExecutor()
    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: executor},
    )
    await registry.install("learning")
    await registry.enable("learning")
    call = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    await entered.wait()
    unrelated = asyncio.create_task(unrelated_release.wait())

    assert await registry.uninstall("learning") is True
    with pytest.raises(asyncio.CancelledError):
        await call
    assert unrelated.done() is False
    with pytest.raises(CapabilityNotInstalled):
        await registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    unrelated_release.set()
    await unrelated


@pytest.mark.asyncio
async def test_disable_stops_admission_and_cancels_an_existing_call(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    class BlockingExecutor(_Executor):
        async def execute(
            self,
            operation_id: str,
            arguments: Mapping[str, Any],
        ) -> Any:
            entered.set()
            await asyncio.Event().wait()
            return await super().execute(operation_id, arguments)

    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: BlockingExecutor()},
    )
    await registry.install("learning")
    await registry.enable("learning")
    call = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    await entered.wait()

    state = await registry.disable("learning")

    assert state.enabled is False
    assert state.in_flight == 0
    with pytest.raises(asyncio.CancelledError):
        await call
    with pytest.raises(CapabilityDisabled):
        await registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )


@pytest.mark.asyncio
async def test_quiescence_timeout_is_degraded_and_never_claims_stopped(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    ignored_cancel = asyncio.Event()
    release = asyncio.Event()

    class CancellationDefiantExecutor(_Executor):
        async def execute(
            self,
            operation_id: str,
            arguments: Mapping[str, Any],
        ) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                ignored_cancel.set()
                await release.wait()
            return await super().execute(operation_id, arguments)

    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: CancellationDefiantExecutor()},
        quiescence_timeout_seconds=0.01,
    )
    await registry.install("learning")
    await registry.enable("learning")
    call = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    await entered.wait()

    try:
        with pytest.raises(CapabilityQuiescenceTimeout) as failure:
            await registry.disable("learning")
        assert failure.value.capability_id == "learning"
        assert failure.value.in_flight == 1
        assert ignored_cancel.is_set()
        state = await registry.state("learning")
        assert state.installed is True
        assert state.enabled is False
        assert state.in_flight == 1
        assert state.quiescence_error == "CapabilityQuiescenceTimeout"
        assert (await registry.health_snapshot())["status"] == "degraded"
    finally:
        release.set()
        await call

    # A later exact retry clears the degraded marker only after the old call
    # really has left the registry.
    state = await registry.disable("learning")
    assert state.in_flight == 0
    assert state.quiescence_error == ""


@pytest.mark.asyncio
async def test_uninstall_timeout_keeps_disabled_entry_until_exact_retry(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class CancellationDefiantExecutor(_Executor):
        async def execute(
            self,
            operation_id: str,
            arguments: Mapping[str, Any],
        ) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return await super().execute(operation_id, arguments)

    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: CancellationDefiantExecutor()},
        quiescence_timeout_seconds=0.01,
    )
    await registry.install("learning")
    await registry.enable("learning")
    call = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    await entered.wait()

    try:
        with pytest.raises(CapabilityQuiescenceTimeout):
            await registry.uninstall("learning")
        state = await registry.state("learning")
        assert state.installed is True
        assert state.enabled is False
        assert state.in_flight == 1
    finally:
        release.set()
        await call

    assert await registry.uninstall("learning") is True
    assert (await registry.state("learning")).installed is False


@pytest.mark.asyncio
async def test_self_pause_exempts_only_the_current_management_call(
    tmp_path: Path,
) -> None:
    observed_state: list[Any] = []
    self_manage_entered = asyncio.Event()
    allow_self_manage = asyncio.Event()
    registry: CapabilityRuntimeRegistry

    async def self_managing_dispatch(
        executor: _Executor,
        invocation: CapabilityInvocation,
    ) -> Any:
        plan = await executor.execute(invocation.operation_id, invocation.arguments)
        self_manage_entered.set()
        await allow_self_manage.wait()
        observed_state.append(await registry.disable(invocation.capability_id))
        return plan

    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=self_managing_dispatch,
        factories={"learning": lambda _descriptor: _Executor()},
    )
    await registry.install("learning")
    await registry.enable("learning")
    current_management = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    await self_manage_entered.wait()
    stale_waiter = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    for _ in range(100):
        if (await registry.state("learning")).in_flight == 2:
            break
        await asyncio.sleep(0)
    assert (await registry.state("learning")).in_flight == 2
    allow_self_manage.set()

    result = await current_management
    with pytest.raises(asyncio.CancelledError):
        await stale_waiter

    assert result["operation"] == "nucleus_learn"
    assert observed_state[0].enabled is False
    assert observed_state[0].in_flight == 1
    assert observed_state[0].completion_exempt_in_flight == 1
    final = await registry.state("learning")
    assert final.enabled is False
    assert final.in_flight == 0
    assert final.completion_exempt_in_flight == 0


@pytest.mark.asyncio
async def test_external_call_cancellation_propagates_and_cleans_admission(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    class BlockingExecutor(_Executor):
        async def execute(
            self,
            operation_id: str,
            arguments: Mapping[str, Any],
        ) -> Any:
            entered.set()
            await asyncio.Event().wait()
            return await super().execute(operation_id, arguments)

    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: BlockingExecutor()},
    )
    await registry.install("learning")
    await registry.enable("learning")
    call = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    await entered.wait()

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    state = await registry.state("learning")
    assert state.enabled is True
    assert state.in_flight == 0


@pytest.mark.asyncio
async def test_close_cancels_calls_is_idempotent_and_never_closes_executor(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    class BlockingExecutor(_Executor):
        async def execute(
            self,
            operation_id: str,
            arguments: Mapping[str, Any],
        ) -> Any:
            entered.set()
            await asyncio.Event().wait()
            return await super().execute(operation_id, arguments)

    executor = BlockingExecutor()
    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: executor},
    )
    await registry.install("learning")
    await registry.enable("learning")
    call = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    await entered.wait()

    await registry.close()
    await registry.close()

    with pytest.raises(asyncio.CancelledError):
        await call
    assert executor.close_calls == 0
    assert (await registry.state("learning")).installed is False
    assert (await registry.health_snapshot())["status"] == "closed"
    with pytest.raises(CapabilityRegistryClosed):
        await registry.install("learning")


@pytest.mark.asyncio
async def test_close_timeout_is_explicitly_degraded_until_retry(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class CancellationDefiantExecutor(_Executor):
        async def execute(
            self,
            operation_id: str,
            arguments: Mapping[str, Any],
        ) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return await super().execute(operation_id, arguments)

    executor = CancellationDefiantExecutor()
    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: executor},
        quiescence_timeout_seconds=0.01,
    )
    await registry.install("learning")
    await registry.enable("learning")
    call = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    await entered.wait()

    try:
        with pytest.raises(ExceptionGroup) as failure:
            await registry.close()
        assert failure.value.subgroup(CapabilityQuiescenceTimeout) is not None
        state = await registry.state("learning")
        assert state.installed is True
        assert state.enabled is False
        assert state.in_flight == 1
        assert (await registry.health_snapshot())["status"] == "degraded"
        with pytest.raises(CapabilityRegistryClosed):
            await registry.execute(
                "learning",
                "nucleus_learn",
                {},
                caller_context={"allowed_operations": {"nucleus_learn"}},
            )
    finally:
        release.set()
        await call

    await registry.close()
    await registry.close()
    assert (await registry.state("learning")).installed is False
    assert (await registry.health_snapshot())["status"] == "closed"


@pytest.mark.asyncio
async def test_disable_cancels_serial_owner_and_admitted_waiter(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    class BlockingExecutor(_Executor):
        async def execute(
            self,
            operation_id: str,
            arguments: Mapping[str, Any],
        ) -> Any:
            entered.set()
            await asyncio.Event().wait()
            return await super().execute(operation_id, arguments)

    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: BlockingExecutor()},
    )
    await registry.install("learning")
    await registry.enable("learning")
    calls = [
        asyncio.create_task(
            registry.execute(
                "learning",
                "nucleus_learn",
                {},
                caller_context={"allowed_operations": {"nucleus_learn"}},
            )
        )
        for _ in range(2)
    ]
    await entered.wait()
    for _ in range(100):
        if (await registry.state("learning")).in_flight == 2:
            break
        await asyncio.sleep(0)
    assert (await registry.state("learning")).in_flight == 2

    state = await registry.disable("learning")
    outcomes = await asyncio.gather(*calls, return_exceptions=True)

    assert state.in_flight == 0
    assert all(isinstance(item, asyncio.CancelledError) for item in outcomes)


@pytest.mark.asyncio
async def test_cancelling_lifecycle_wait_propagates_and_records_unknown_stop(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    ignored_cancel = asyncio.Event()
    release = asyncio.Event()

    class CancellationDefiantExecutor(_Executor):
        async def execute(
            self,
            operation_id: str,
            arguments: Mapping[str, Any],
        ) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                ignored_cancel.set()
                await release.wait()
            return await super().execute(operation_id, arguments)

    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: CancellationDefiantExecutor()},
        quiescence_timeout_seconds=5.0,
    )
    await registry.install("learning")
    await registry.enable("learning")
    call = asyncio.create_task(
        registry.execute(
            "learning",
            "nucleus_learn",
            {},
            caller_context={"allowed_operations": {"nucleus_learn"}},
        )
    )
    await entered.wait()
    stopping = asyncio.create_task(registry.disable("learning"))
    await ignored_cancel.wait()

    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    state = await registry.state("learning")
    assert state.enabled is False
    assert state.in_flight == 1
    assert state.quiescence_error == "CapabilityQuiescenceCancelled"

    release.set()
    await call
    assert (await registry.disable("learning")).quiescence_error == ""


@pytest.mark.asyncio
async def test_uninstall_during_factory_creation_wins_after_install(
    tmp_path: Path,
) -> None:
    factory_entered = asyncio.Event()
    release_factory = asyncio.Event()

    async def factory(_descriptor: Any) -> _Executor:
        factory_entered.set()
        await release_factory.wait()
        return _Executor()

    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": factory},
    )
    install = asyncio.create_task(registry.install("learning"))
    await factory_entered.wait()
    uninstall = asyncio.create_task(registry.uninstall("learning"))
    await asyncio.sleep(0)
    assert uninstall.done() is False

    release_factory.set()
    assert (await install).installed is True
    assert await uninstall is True
    assert (await registry.state("learning")).installed is False


@pytest.mark.asyncio
async def test_async_factory_is_supported_but_failed_contract_is_not_installed(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    executor = _Executor()

    async def async_factory(_descriptor: Any) -> _Executor:
        await asyncio.sleep(0)
        return executor

    registry = CapabilityRuntimeRegistry(
        catalog,
        dispatch=_PermissionDispatch(),
        factories={"learning": async_factory},
    )
    assert (await registry.install("learning")).installed is True

    invalid_registry = CapabilityRuntimeRegistry(
        catalog,
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: object()},
    )
    with pytest.raises(CapabilityExecutorContractError):
        await invalid_registry.install("learning")
    assert (await invalid_registry.state("learning")).installed is False


@pytest.mark.asyncio
async def test_factory_registration_is_identity_idempotent_and_conflict_safe(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    factory = lambda _descriptor: _Executor()
    registry = CapabilityRuntimeRegistry(catalog, dispatch=_PermissionDispatch())

    await registry.register_factory("learning", factory)
    await registry.register_factory("learning", factory)
    with pytest.raises(CapabilityFactoryConflict):
        await registry.register_factory("learning", lambda _descriptor: _Executor())


def test_dispatch_must_be_async_before_any_side_effect(tmp_path: Path) -> None:
    calls: list[str] = []

    def unsafe_dispatch(_executor: Any, _invocation: Any) -> None:
        calls.append("ran")

    with pytest.raises(TypeError, match="async"):
        CapabilityRuntimeRegistry(_catalog(tmp_path), dispatch=unsafe_dispatch)

    assert calls == []


@pytest.mark.asyncio
async def test_health_is_content_free(tmp_path: Path) -> None:
    secret = "workflow private body 1a83d"
    catalog = _catalog(tmp_path, workflow_text=secret)
    registry = CapabilityRuntimeRegistry(
        catalog,
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: _Executor()},
    )
    await registry.install("learning")
    snapshot = await registry.health_snapshot()

    encoded = json.dumps(snapshot, sort_keys=True)
    assert snapshot["catalogued"] == 1
    assert snapshot["installed"] == 1
    assert snapshot["enabled"] == 0
    assert secret not in encoded
    assert "technical manual secret" not in encoded


@pytest.mark.asyncio
async def test_installed_factory_cannot_be_removed(tmp_path: Path) -> None:
    registry = CapabilityRuntimeRegistry(
        _catalog(tmp_path),
        dispatch=_PermissionDispatch(),
        factories={"learning": lambda _descriptor: _Executor()},
    )
    await registry.install("learning")

    with pytest.raises(CapabilityRuntimeError, match="installed"):
        await registry.unregister_factory("learning")
