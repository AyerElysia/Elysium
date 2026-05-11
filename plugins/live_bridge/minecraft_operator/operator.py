"""Touhou Little Maid operation-side coordinator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .decision import (
    MinecraftDecisionRequest,
    MinecraftDecisionResult,
    build_decision_prompt,
    build_fallback_decision,
    extract_decision_result,
)


AskElysia = Callable[[MinecraftDecisionRequest, str], Awaitable[str]]


class MinecraftOperatorError(RuntimeError):
    """Raised when the Minecraft operator cannot produce a valid decision."""


class MinecraftOperator:
    """Bridge between Touhou Little Maid and Elysia's main consciousness."""

    async def decide(
        self,
        request: MinecraftDecisionRequest,
        ask_elysia: AskElysia,
    ) -> MinecraftDecisionResult:
        if not request.tool_names:
            raise MinecraftOperatorError("Minecraft decision request has no tools")

        prompt = build_decision_prompt(request)
        try:
            reply_text = await ask_elysia(request, prompt)
        except Exception as exc:  # noqa: BLE001
            return build_fallback_decision(request, f"elysia request failed: {exc}")

        parsed = extract_decision_result(reply_text, request)
        if parsed is None:
            return build_fallback_decision(request, "elysia reply did not contain a valid decision")
        return parsed

    async def record_life_event(
        self,
        message: str,
        *,
        stream_id: str,
        sender_name: str = "Minecraft操作AI",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Best-effort sync into life_engine without exposing Minecraft tools to Elysia."""

        del metadata  # Reserved for future raw-event metadata.
        try:
            from plugins.life_engine.service.registry import get_life_engine_service

            service = get_life_engine_service()
            if service is None:
                return
            await service.enqueue_direct_message(
                message,
                stream_id=stream_id,
                platform="game.minecraft",
                chat_type="private",
                sender_name=sender_name,
                sender_id="minecraft_operator",
            )
        except Exception:
            return
