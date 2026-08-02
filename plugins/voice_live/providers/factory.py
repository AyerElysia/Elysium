"""Explicit realtime provider construction."""

from __future__ import annotations

import os

from .base import BaseRealtimeProvider


def create_provider(config: object) -> BaseRealtimeProvider:
    provider = config.full_duplex
    provider_type = provider.provider_type
    if provider_type == "disabled":
        raise RuntimeError("Voice Live provider is explicitly disabled")
    if not provider.upstream_url:
        raise RuntimeError(f"{provider_type} upstream_url is required")
    if provider_type == "minicpm_omni":
        from .minicpm_omni import MiniCPMOmniProvider

        return MiniCPMOmniProvider(
            provider.upstream_url,
            mode=provider.mode,
            reference_audio_path=provider.reference_audio_path,
            tts_reference_audio_path=provider.tts_reference_audio_path,
            input_chunk_ms=provider.upstream_input_chunk_ms,
            connect_timeout=provider.connect_timeout_seconds,
            event_timeout=provider.event_timeout_seconds,
        )
    api_key = os.environ.get(provider.api_key_env, "")
    if provider_type == "qwen_realtime":
        from .qwen_realtime import QwenRealtimeProvider

        return QwenRealtimeProvider(
            provider.upstream_url,
            api_key,
            model=provider.model_name,
            voice=provider.voice,
            connect_timeout=provider.connect_timeout_seconds,
            event_timeout=provider.event_timeout_seconds,
        )
    if provider_type == "openai_realtime":
        from .openai_realtime import OpenAIRealtimeProvider

        return OpenAIRealtimeProvider(
            provider.upstream_url,
            api_key,
            model=provider.model_name,
            voice=provider.voice,
            connect_timeout=provider.connect_timeout_seconds,
            event_timeout=provider.event_timeout_seconds,
        )
    raise ValueError(f"unsupported Voice Live provider: {provider_type}")
