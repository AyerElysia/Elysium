"""新格式模型配置加载器。

读取 config/models.toml（LiteLLM 风格），转换为老 ModelConfig 兼容的 model_set 格式。

新格式优势：
- 66KB → 3KB
- Provider 定义一次
- Model 只写非默认值
- Task 路由一行搞定

兼容策略：
- 输出与老 model_config.get_task() 完全相同的 list[dict] 格式
- 下游代码（LLMRequest、policy、inspector）零修改
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from src.kernel.logger import get_logger

logger = get_logger("models_loader", display="Models", enable_event_broadcast=False)

# 默认值（配置中省略时使用）
_DEFAULTS = {
    "ctx": 32768,
    "stream": False,
    "vision": False,
    "audio": False,
    "tool_call_compat": False,
    "timeout": 120,
    "max_retry": 3,
    "retry_interval": 5,
    "client_type": "openai",
    "tokens": 800,
    "temp": 0.7,
}


class ModelsConfig:
    """新格式模型配置。"""

    def __init__(self, path: str | Path = "config/models.toml") -> None:
        self._path = Path(path)
        self._providers: dict[str, dict[str, Any]] = {}
        self._models: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning(f"模型配置不存在: {self._path}")
            return
        with open(self._path, "rb") as f:
            data = tomllib.load(f)
        self._providers = data.get("providers", {})
        self._models = data.get("models", {})
        self._tasks = data.get("tasks", {})
        logger.info(
            f"模型配置已加载: {len(self._providers)} providers, "
            f"{len(self._models)} models, {len(self._tasks)} tasks"
        )

    # ─── 查询接口 ─────────────────────────────

    @property
    def providers(self) -> dict[str, dict[str, Any]]:
        return self._providers

    @property
    def models(self) -> dict[str, dict[str, Any]]:
        return self._models

    @property
    def tasks(self) -> dict[str, dict[str, Any]]:
        return self._tasks

    def get_task(self, task_name: str) -> list[dict[str, Any]]:
        """获取任务的 model_set（兼容老 LLMRequest 格式）。

        Returns:
            list[dict]，每个 dict 包含 api_provider, base_url, model_identifier,
            api_key, max_tokens, temperature 等字段。

        Raises:
            ValueError: 任务不存在
        """
        task = self._tasks.get(task_name)
        if task is None:
            raise ValueError(f"任务 '{task_name}' 未找到。可用: {list(self._tasks.keys())}")

        model_names = task.get("models", [])
        max_tokens = task.get("tokens", _DEFAULTS["tokens"])
        temperature = task.get("temp", _DEFAULTS["temp"])

        model_set: list[dict[str, Any]] = []
        for name in model_names:
            entry = self._build_model_entry(name, max_tokens, temperature)
            if entry:
                model_set.append(entry)

        if not model_set:
            raise ValueError(f"任务 '{task_name}' 无可用模型")
        return model_set

    def get_model_entry(self, model_name: str) -> dict[str, Any] | None:
        """获取单个模型的完整配置。"""
        return self._build_model_entry(model_name, _DEFAULTS["tokens"], _DEFAULTS["temp"])

    def list_model_names(self) -> list[str]:
        return list(self._models.keys())

    def list_task_names(self) -> list[str]:
        return list(self._tasks.keys())

    # ─── 内部 ────────────────────────────────

    def _build_model_entry(
        self, model_name: str, max_tokens: int, temperature: float
    ) -> dict[str, Any] | None:
        """将新格式 model 转为老 model_set entry。"""
        model = self._models.get(model_name)
        if model is None:
            logger.warning(f"模型 '{model_name}' 未定义，跳过")
            return None

        provider_name = model.get("provider", "")
        provider = self._providers.get(provider_name, {})

        # 模型标识符（API 调用用）
        model_id = model.get("id", model_name)

        # 媒体能力
        modalities = ["text"]
        if model.get("vision", False):
            modalities.append("image")
        if model.get("audio", False):
            modalities.append("audio")

        entry: dict[str, Any] = {
            "api_provider": provider_name,
            "base_url": provider.get("base_url", ""),
            "model_identifier": model_id,
            "api_key": provider.get("api_key", ""),
            "client_type": provider.get("client_type", _DEFAULTS["client_type"]),
            "max_retry": provider.get("max_retry", _DEFAULTS["max_retry"]),
            "timeout": provider.get("timeout", _DEFAULTS["timeout"]),
            "retry_interval": provider.get("retry_interval", _DEFAULTS["retry_interval"]),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "max_context": model.get("ctx", _DEFAULTS["ctx"]),
            "force_stream_mode": model.get("stream", _DEFAULTS["stream"]),
            "tool_call_compat": model.get("tool_call_compat", _DEFAULTS["tool_call_compat"]),
            "media_capabilities": {
                "modalities": modalities,
            },
            "extra_params": model.get("extra", {}),
        }
        return entry


# ─────────────────────────────────────────────
# 全局实例
# ─────────────────────────────────────────────

_models_config: ModelsConfig | None = None


def init_models_config(path: str | Path = "config/models.toml") -> ModelsConfig:
    """初始化新格式模型配置。"""
    global _models_config
    _models_config = ModelsConfig(path)
    return _models_config


def get_models_config() -> ModelsConfig:
    """获取全局模型配置。"""
    global _models_config
    if _models_config is None:
        _models_config = ModelsConfig()
    return _models_config
