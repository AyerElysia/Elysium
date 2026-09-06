"""Keep exclusive legacy tools from bypassing installed capability control."""

from __future__ import annotations

from typing import Any

from .execution_context import current_capability_execution


async def require_optional_capability(service: Any, capability_id: str) -> None:
    """Check managed admission; preserve an explicitly unmigrated runtime.

    File I/O, event reads, memory reads and other shared primitives do not use
    this gate: uninstalling a recipe cannot delete foundational capabilities.
    """
    config = service._cfg() if callable(getattr(service, "_cfg", None)) else None
    configured = bool(getattr(getattr(config, "opportunity", None), "enabled", False))
    managed = bool(getattr(service, "opportunity_managed", configured))
    if not managed:
        return
    if current_capability_execution() != capability_id:
        raise PermissionError("OpportunityManagedToolRequiresCapabilityCall")
    runtime = getattr(service, "_opportunity_runtime", None)
    if runtime is None:
        raise RuntimeError("OpportunityRuntimeNotReady")
    from ..storage.opportunity_contracts import ProviderStatus

    binding = await runtime.stores.authority.get_provider(capability_id)
    state = await runtime.registry.state(capability_id)
    if (
        binding is None
        or binding.status != ProviderStatus.ENABLED
        or not state.installed
        or not state.enabled
    ):
        raise PermissionError("OpportunityCapabilityNotEnabled")
