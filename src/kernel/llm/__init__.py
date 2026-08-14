"""LLM request framework.

对齐 Elysium 的 kernel/llm 架构契约：
- `LLMRequest`：构建 payloads 并发起请求
- `LLMResponse`：同时支持 `await`（收集全量）与 `async for`（流式）
- `LLMPayload`：`role + content` 的标准消息单元

本模块不依赖 core/config 的实现细节；上层需要传入 `model_set`：
- `list[dict]`，每个元素表示一个“模型实例”的完整配置（api_provider/base_url/model_identifier/api_key/...）。

负载均衡与重试策略由 `kernel.llm.policy` 承担。
"""

from .context import LLMContextManager
from .context_delivery import EffectiveContextReceipt
from .embedding_request import EmbeddingRequest
from .embedding_response import EmbeddingResponse
from .exceptions import (
	LLMAPIError,
	LLMAuthenticationError,
	LLMConfigurationError,
	LLMContentFilterError,
	LLMContextError,
	LLMError,
	LLMRateLimitError,
	LLMResponseConsumedError,
	LLMTimeoutError,
	LLMTokenLimitError,
	MediaLimitError,
	MediaValidationError,
	UnsupportedModalityError,
	classify_exception,
)
from .monitor import (
	MetricsCollector,
	ModelStats,
	RequestMetrics,
	RequestTimer,
	get_global_collector,
)
from .payload import (
	Audio,
	Content,
	File,
	Image,
	LLMPayload,
	LLMUsable,
	LLMUsableExecution,
	LLMUsableExecutionStatus,
	MediaKind,
	MediaPart,
	MediaRef,
	ReasoningText,
	Text,
	ToolCall,
	ToolRegistry,
	ToolResult,
	Video,
	normalize_media_mime_type,
)
from .request import LLMRequest
from .rerank_request import RerankRequest
from .rerank_response import RerankItem, RerankResponse
from .response import LLMResponse
from .roles import ROLE
from .types import ModelEntry, ModelSet, RequestType

__all__ = [
	# 核心类
	"ROLE",
	"LLMRequest",
	"EmbeddingRequest",
	"RerankRequest",
	"LLMContextManager",
	"EffectiveContextReceipt",
	"LLMResponse",
	"EmbeddingResponse",
	"RerankResponse",
	"RerankItem",
	"LLMPayload",
	# 类型定义
	"RequestType",
	"ModelEntry",
	"ModelSet",
	# 内容类型
	"Content",
	"ReasoningText",
	"Text",
	"Image",
	"Audio",
	"Video",
	"File",
	"MediaKind",
	"MediaPart",
	"MediaRef",
	"normalize_media_mime_type",
	# 工具相关
	"ToolResult",
	"ToolCall",
	"LLMUsable",
	"LLMUsableExecution",
	"LLMUsableExecutionStatus",
	"ToolRegistry",
	# 监控相关
	"RequestMetrics",
	"ModelStats",
	"MetricsCollector",
	"RequestTimer",
	"get_global_collector",
	# 异常相关
	"LLMError",
	"LLMContextError",
	"LLMConfigurationError",
	"MediaLimitError",
	"MediaValidationError",
	"UnsupportedModalityError",
	"LLMResponseConsumedError",
	"LLMRateLimitError",
	"LLMTimeoutError",
	"LLMContentFilterError",
	"LLMTokenLimitError",
	"LLMAuthenticationError",
	"LLMAPIError",
	"classify_exception",
]
