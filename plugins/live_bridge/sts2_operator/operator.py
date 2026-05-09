"""STS2 operation-side coordinator.

This module keeps low-level game details outside Elysia's tool list. It asks
the main consciousness for a structured choice, validates that choice, and
returns the strict JSON contract expected by the STS2 teammate mod.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from .decision import (
    Sts2DecisionRequest,
    Sts2DecisionResult,
    build_decision_prompt,
    build_fallback_decision,
    extract_decision_result,
)


AskElysia = Callable[[Sts2DecisionRequest, str], Awaitable[str]]


class Sts2OperatorError(RuntimeError):
    """Raised when STS2 operator cannot produce a valid decision."""


class Sts2Operator:
    """Bridge between the STS2 teammate mod and Elysia's main consciousness."""

    def __init__(self, *, cache_size: int = 128) -> None:
        self._cache_size = max(8, int(cache_size))
        self._decision_cache: OrderedDict[str, Sts2DecisionResult] = OrderedDict()

    async def decide(
        self,
        request: Sts2DecisionRequest,
        ask_elysia: AskElysia,
    ) -> Sts2DecisionResult:
        cache_key = self._cache_key(request)
        cached = self._decision_cache.get(cache_key)
        if cached is not None:
            return cached

        if not request.legal_action_ids:
            raise Sts2OperatorError("STS2 decision request has no legal actions")

        prompt = build_decision_prompt(request)
        try:
            reply_text = await ask_elysia(request, prompt)
        except Exception as exc:  # noqa: BLE001
            result = build_fallback_decision(request, f"elysia request failed: {exc}")
            self._remember(cache_key, result)
            return result

        parsed = extract_decision_result(reply_text, request)
        if parsed is None:
            result = build_fallback_decision(request, "elysia reply did not contain a legal action id")
        else:
            result = parsed

        self._remember(cache_key, result)
        return result

    async def record_life_event(
        self,
        message: str,
        *,
        stream_id: str,
        sender_name: str = "STS2操作AI",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Best-effort sync into life_engine without exposing tools to Elysia."""

        del metadata  # Reserved for a future raw-event path.
        try:
            from plugins.life_engine.service.registry import get_life_engine_service

            service = get_life_engine_service()
            if service is None:
                return
            await service.enqueue_direct_message(
                message,
                stream_id=stream_id,
                platform="game.sts2",
                chat_type="private",
                sender_name=sender_name,
                sender_id="sts2_operator",
            )
        except Exception:
            return

    def _remember(self, key: str, result: Sts2DecisionResult) -> None:
        self._decision_cache[key] = result
        self._decision_cache.move_to_end(key)
        while len(self._decision_cache) > self._cache_size:
            self._decision_cache.popitem(last=False)

    @staticmethod
    def _cache_key(request: Sts2DecisionRequest) -> str:
        return f"{request.request_id}:{request.snapshot_id}:{request.actor_id}"
