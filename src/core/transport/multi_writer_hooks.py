"""Optional multi-writer coordination hooks for core transport paths.

The multi-writer protocol lives in the ``life_engine`` plugin, but inbound
message facts and outbound send intents pass through core transport code
(``MessageReceiver``/``Distributor`` and ``MessageSender``).  To keep core
independent of the plugin, core exposes no-op hook slots here; the Life Engine
service registers its bridge when ``multi_writer_enabled`` is true and
unregisters it on shutdown.

Default behavior (no hook registered) is a strict no-op: the transport paths
behave exactly as before multi-writer support existed.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import Any

InboundFactHook = Callable[[Any], Awaitable[bool]]
"""Returns True when the fact was durably recorded and the message may proceed,
False when the message must be skipped (fact conflict), and raises are treated
as a recorded failure by callers."""

OutboxIntentHook = Callable[[Any], Awaitable[bool]]
"""Returns True when the send intent was durably recorded (send may proceed),
False when the intent could not be recorded (fail closed before platform call).
A missing hook means the multi-writer outbox is not active."""

OutboxSettleHook = Callable[[Any, dict[str, Any]], Awaitable[bool]]
"""Finalizes one outbox action after the platform call.  The dict carries
``provider_receipt`` (content-free receipt fields), ``error_type`` and
``delivery_unknown``; a missing hook means the outbox is not active."""

OutboundDeliveryProofHook = Callable[[Any, dict[str, Any]], Awaitable[bool]]
"""Persists an exact transport acknowledgement for its domain owner.

This hook is independent of multi-writer mode.  A missing hook means no
delivery-proof owner is active.
"""

_inbound_fact_hook: InboundFactHook | None = None
_outbox_intent_hook: OutboxIntentHook | None = None
_outbox_settle_hook: OutboxSettleHook | None = None
_outbound_delivery_proof_hook: OutboundDeliveryProofHook | None = None
_hooks_lock = threading.Lock()


def register_inbound_fact_hook(hook: InboundFactHook) -> None:
    """Register the active inbound message fact hook (single slot)."""
    global _inbound_fact_hook
    with _hooks_lock:
        _inbound_fact_hook = hook


def unregister_inbound_fact_hook(hook: InboundFactHook) -> None:
    """Unregister the hook only if it is the exact registered callable."""
    global _inbound_fact_hook
    with _hooks_lock:
        if _inbound_fact_hook is hook:
            _inbound_fact_hook = None


def register_outbox_intent_hook(hook: OutboxIntentHook) -> None:
    """Register the active outbox send-intent hook (single slot)."""
    global _outbox_intent_hook
    with _hooks_lock:
        _outbox_intent_hook = hook


def unregister_outbox_intent_hook(hook: OutboxIntentHook) -> None:
    """Unregister the hook only if it is the exact registered callable."""
    global _outbox_intent_hook
    with _hooks_lock:
        if _outbox_intent_hook is hook:
            _outbox_intent_hook = None


def register_outbox_settle_hook(hook: OutboxSettleHook) -> None:
    """Register the active outbox settle hook (single slot)."""
    global _outbox_settle_hook
    with _hooks_lock:
        _outbox_settle_hook = hook


def unregister_outbox_settle_hook(hook: OutboxSettleHook) -> None:
    """Unregister the hook only if it is the exact registered callable."""
    global _outbox_settle_hook
    with _hooks_lock:
        if _outbox_settle_hook is hook:
            _outbox_settle_hook = None


def register_outbound_delivery_proof_hook(
    hook: OutboundDeliveryProofHook,
) -> None:
    """Register the single active durable delivery-proof owner."""

    global _outbound_delivery_proof_hook
    with _hooks_lock:
        if (
            _outbound_delivery_proof_hook is not None
            and _outbound_delivery_proof_hook is not hook
        ):
            raise RuntimeError("OutboundDeliveryProofHookAlreadyRegistered")
        _outbound_delivery_proof_hook = hook


def unregister_outbound_delivery_proof_hook(
    hook: OutboundDeliveryProofHook,
) -> None:
    """Unregister only the exact delivery-proof owner."""

    global _outbound_delivery_proof_hook
    with _hooks_lock:
        if _outbound_delivery_proof_hook is hook:
            _outbound_delivery_proof_hook = None


async def invoke_inbound_fact_hook(message: Any) -> bool | None:
    """Record an inbound message fact when a hook is active.

    Returns:
        True when recorded and processing should continue; False when the
        message must be skipped; None when no hook is registered (legacy path).
    """
    with _hooks_lock:
        hook = _inbound_fact_hook
    if hook is None:
        return None
    return await hook(message)


async def invoke_outbox_intent_hook(message: Any) -> bool | None:
    """Persist a send intent before the platform call when a hook is active.

    Returns:
        True when the intent was durably recorded and sending may proceed;
        False when recording failed and the send must fail closed; None when
        no hook is registered (legacy path).
    """
    with _hooks_lock:
        hook = _outbox_intent_hook
    if hook is None:
        return None
    return await hook(message)


async def invoke_outbox_settle_hook(
    message: Any,
    *,
    provider_receipt: dict[str, Any] | None = None,
    error_type: str = "",
    delivery_unknown: bool = False,
) -> bool | None:
    """Finalize one outbox action after the platform call when a hook is active.

    Returns:
        True when the action was settled (or no hook is registered); False when
        settlement failed and recovery information may be lost; None when no
        hook is registered (legacy path).
    """
    with _hooks_lock:
        hook = _outbox_settle_hook
    if hook is None:
        return None
    outcome: dict[str, Any] = {
        "provider_receipt": dict(provider_receipt or {}),
        "error_type": str(error_type or ""),
        "delivery_unknown": bool(delivery_unknown),
    }
    return await hook(message, outcome)


async def invoke_outbound_delivery_proof_hook(
    message: Any,
    receipt: dict[str, Any],
) -> bool | None:
    """Persist one exact receipt when a domain proof owner is active."""

    with _hooks_lock:
        hook = _outbound_delivery_proof_hook
    if hook is None:
        return None
    return await hook(message, dict(receipt))


def multi_writer_hooks_active() -> bool:
    """Report whether any core hot-path hook is currently registered."""
    with _hooks_lock:
        return (
            _inbound_fact_hook is not None
            or _outbox_intent_hook is not None
            or _outbox_settle_hook is not None
            or _outbound_delivery_proof_hook is not None
        )


__all__ = [
    "InboundFactHook",
    "OutboxIntentHook",
    "OutboxSettleHook",
    "OutboundDeliveryProofHook",
    "invoke_inbound_fact_hook",
    "invoke_outbound_delivery_proof_hook",
    "invoke_outbox_intent_hook",
    "invoke_outbox_settle_hook",
    "multi_writer_hooks_active",
    "register_inbound_fact_hook",
    "register_outbound_delivery_proof_hook",
    "register_outbox_intent_hook",
    "register_outbox_settle_hook",
    "unregister_inbound_fact_hook",
    "unregister_outbound_delivery_proof_hook",
    "unregister_outbox_intent_hook",
    "unregister_outbox_settle_hook",
]
