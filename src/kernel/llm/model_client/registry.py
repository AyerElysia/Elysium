"""
模型客户端注册表，负责根据模型配置返回对应的 client 实例。
"""
from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass

from ..exceptions import LLMConfigurationError
from .base import (
    ASRModelClient,
    ChatModelClient,
    EmbeddingModelClient,
    RerankModelClient,
    SpeechModelClient,
)
from .anthropic_client import AnthropicChatClient
from .openai_client import OpenAIChatClient
from ..types import ModelEntry

@dataclass(slots=True)
class ModelClientRegistry:
    """provider -> client 的注册表。

    当前默认提供 openai client；gemini/bedrock 后续可注册。
    """

    openai: ChatModelClient | None = None
    anthropic: ChatModelClient | None = None
    gemini: ChatModelClient | None = None
    bedrock: ChatModelClient | None = None

    def __post_init__(self) -> None:
        if self.openai is None:
            self.openai = OpenAIChatClient()
        if self.anthropic is None:
            self.anthropic = AnthropicChatClient()

    def get_client_for_model(self, model: ModelEntry) -> ChatModelClient:
        """根据单个模型配置决定使用哪个 provider。

        当前阶段以 `client_type` 为准：openai/anthropic/gemini/bedrock。
        """

        client_type = model.get("client_type")
        if not isinstance(client_type, str) or not client_type.strip():
            raise LLMConfigurationError("model.client_type 必须是非空字符串")

        normalized_client_type = client_type.strip()
        clients: dict[str, ChatModelClient | None] = {
            "openai": self.openai,
            "anthropic": self.anthropic,
            "gemini": self.gemini,
            "aiohttp_gemini": self.gemini,
            "bedrock": self.bedrock,
        }
        if normalized_client_type not in clients:
            raise LLMConfigurationError(
                f"不支持的 model.client_type: {normalized_client_type!r}"
            )

        client = clients[normalized_client_type]
        if client is None:
            raise LLMConfigurationError(
                f"{normalized_client_type} client 未配置"
            )
        return client

    def get_embedding_client_for_model(self, model: ModelEntry) -> EmbeddingModelClient:
        """根据单个模型配置获取 embedding client。"""

        client = self.get_client_for_model(model)
        if not hasattr(client, "create_embedding"):
            raise LLMConfigurationError("当前 client 不支持 embeddings 请求")
        return client  # type: ignore[return-value]

    def get_rerank_client_for_model(self, model: ModelEntry) -> RerankModelClient:
        """根据单个模型配置获取 rerank client。"""

        client = self.get_client_for_model(model)
        if not hasattr(client, "create_rerank"):
            raise LLMConfigurationError("当前 client 不支持 rerank 请求")
        return client  # type: ignore[return-value]

    def get_speech_client_for_model(self, model: ModelEntry) -> SpeechModelClient:
        """根据单个模型配置获取 TTS client。"""
        client = self.get_client_for_model(model)
        if not hasattr(client, "create_speech"):
            raise LLMConfigurationError("当前 client 不支持 TTS 请求")
        return client  # type: ignore[return-value]

    def get_asr_client_for_model(self, model: ModelEntry) -> ASRModelClient:
        """根据单个模型配置获取 ASR client。"""

        client = self.get_client_for_model(model)
        if not hasattr(client, "create_transcription"):
            raise LLMConfigurationError("当前 client 不支持 ASR 请求")
        return client  # type: ignore[return-value]


_default_clients_lock = threading.Lock()
_default_openai_client: ChatModelClient | None = None
_default_anthropic_client: ChatModelClient | None = None


def get_default_model_client_registry() -> ModelClientRegistry:
    """返回独立注册表外壳，共享进程级 provider 连接池。"""
    global _default_openai_client, _default_anthropic_client
    with _default_clients_lock:
        if _default_openai_client is None:
            _default_openai_client = OpenAIChatClient()
        if _default_anthropic_client is None:
            _default_anthropic_client = AnthropicChatClient()
        openai_client = _default_openai_client
        anthropic_client = _default_anthropic_client

    return ModelClientRegistry(
        openai=openai_client,
        anthropic=anthropic_client,
    )


async def close_default_model_clients() -> None:
    """关闭默认 provider 连接池，并允许下次初始化创建干净实例。"""
    global _default_openai_client, _default_anthropic_client
    with _default_clients_lock:
        clients = {
            client
            for client in (_default_openai_client, _default_anthropic_client)
            if client is not None
        }
        _default_openai_client = None
        _default_anthropic_client = None

    for client in clients:
        close = getattr(client, "aclose", None)
        if close is None:
            close = getattr(client, "close", None)
        if close is None:
            continue
        result = close()
        if inspect.isawaitable(result):
            await result
