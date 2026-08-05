"""LLM 模块类型定义

提供 LLM 模块使用的类型别名和 TypedDict 定义。
"""

from enum import Enum
from typing import Any, NotRequired, TypeAlias, TypedDict


class RedactedSecret(str):
    """A string that remains usable by clients but is masked in containers/logs."""

    def __repr__(self) -> str:
        return "'<redacted>'"


def redact_secret(value: object) -> RedactedSecret:
    """Wrap a secret without changing its string value or equality semantics."""
    if isinstance(value, RedactedSecret):
        return value
    return RedactedSecret(str(value or ""))


class RequestType(str, Enum):
    """LLM 请求类型。"""

    COMPLETIONS = "completions"
    EMBEDDINGS = "embeddings"
    RERANK = "rerank"


class ModelEntry(TypedDict, total=True):
    """模型配置条目

    定义单个 LLM 模型的完整配置信息。
    """

    api_provider: str
    base_url: str
    model_identifier: str
    api_key: str
    client_type: str
    max_retry: int
    timeout: float
    retry_interval: float
    price_in: float
    price_out: float
    temperature: float
    max_tokens: int
    max_context: int
    tool_call_compat: bool
    extra_params: dict[str, Any]
    media_capabilities: dict[str, Any]
    force_stream_mode: NotRequired[bool]
    routing_task: NotRequired[str]
    routing_model_alias: NotRequired[str]
    routing_priority: NotRequired[int]
    routing_snapshot: NotRequired[str]
    context_tokens: NotRequired[int]


# 模型集合类型：一组可用的模型配置
ModelSet: TypeAlias = list[ModelEntry]


__all__ = [
    "ModelEntry",
    "ModelSet",
    "RedactedSecret",
    "RequestType",
    "redact_secret",
]
