"""Authoritative compact model-registry loader.

``config/models.toml`` defines providers, registered models, and ordered task
routes.  A registry generation is parsed and validated completely before it is
published, so callers can never observe a partial or silently degraded route.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlparse

from src.kernel.logger import get_logger

logger = get_logger("models_loader", display="Models", enable_event_broadcast=False)


class ModelRegistryError(ValueError):
    """The authoritative compact model registry is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ModelRoutingSnapshot:
    """Secret-free immutable identity of one validated routing generation."""

    source_path: str
    digest: str
    task_routes: Mapping[str, tuple[str, ...]]
    active_providers: tuple[str, ...]
    provider_count: int
    model_count: int


PRODUCTION_MODEL_TASKS = frozenset(
    {
        "core",
        "expression",
        "witness",
        "agent",
        "utility",
        "vision",
        "voice",
        "embedding",
        "router",
        "router_context_projection",
        "live",
    }
)

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

_TASK_ALIASES: dict[str, str] = {
    "life": "core",
    "actor": "expression",
    "sub_actor": "agent",
    "diary": "witness",
    "vlm": "vision",
    "video": "vision",
    "utils": "utility",
    "utils_small": "utility",
    "media_observer": "vision",
    "tool_use": "agent",
}

_PROVIDER_FIELDS = frozenset(
    {
        "base_url",
        "api_key",
        "client_type",
        "max_retry",
        "timeout",
        "retry_interval",
    }
)
_MODEL_FIELDS = frozenset(
    {
        "provider",
        "id",
        "ctx",
        "stream",
        "vision",
        "audio",
        "video",
        "tool_call_compat",
        "price_in",
        "price_out",
        "extra",
    }
)
_TASK_FIELDS = frozenset({"models", "tokens", "temp"})
_READY_CLIENT_TYPES = frozenset({"openai", "anthropic"})
_NON_GENERATIVE_TASKS = frozenset({"voice", "embedding"})


class ModelsConfig:
    """Validated, immutable routing snapshot loaded from ``models.toml``."""

    def __init__(self, path: str | Path = "config/models.toml") -> None:
        self._path = Path(path)
        self._providers: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
        self._models: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
        self._tasks: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
        self._snapshot = ModelRoutingSnapshot(
            source_path=str(self._path),
            digest="",
            task_routes=MappingProxyType({}),
            active_providers=(),
            provider_count=0,
            model_count=0,
        )
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            raise ModelRegistryError(f"模型配置不存在: {self._path}")
        try:
            with self._path.open("rb") as file:
                data = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ModelRegistryError(f"模型配置无法读取: {self._path}: {exc}") from exc

        unknown_sections = sorted(set(data) - {"providers", "models", "tasks"})
        if unknown_sections:
            raise ModelRegistryError(f"模型配置包含未知顶层节: {unknown_sections}")

        providers = self._require_section(data, "providers")
        models = self._require_section(data, "models")
        tasks = self._require_section(data, "tasks")
        self._validate_providers(providers)
        self._validate_models(models, providers)
        self._validate_tasks(tasks, models, providers)

        task_routes = {
            task_name: tuple(str(name) for name in task["models"])
            for task_name, task in tasks.items()
        }
        active_models: list[str] = []
        active_providers: list[str] = []
        for route in task_routes.values():
            for model_name in route:
                if model_name not in active_models:
                    active_models.append(model_name)
                provider_name = str(models[model_name]["provider"])
                if provider_name not in active_providers:
                    active_providers.append(provider_name)
        route_manifest = {
            "tasks": {
                name: {
                    "models": task_routes[name],
                    "tokens": task.get("tokens", _DEFAULTS["tokens"]),
                    "temp": task.get("temp", _DEFAULTS["temp"]),
                }
                for name, task in tasks.items()
            },
            "models": {
                name: {
                    "provider": model["provider"],
                    "id": model.get("id", name),
                    "ctx": model.get("ctx", _DEFAULTS["ctx"]),
                    "stream": model.get("stream", _DEFAULTS["stream"]),
                    "vision": model.get("vision", _DEFAULTS["vision"]),
                    "audio": model.get("audio", _DEFAULTS["audio"]),
                    "video": model.get("video", False),
                    "tool_call_compat": model.get(
                        "tool_call_compat", _DEFAULTS["tool_call_compat"]
                    ),
                }
                for name, model in models.items()
                if name in active_models
            },
            "providers": {
                name: {
                    "client_type": provider.get(
                        "client_type", _DEFAULTS["client_type"]
                    ),
                    "max_retry": provider.get("max_retry", _DEFAULTS["max_retry"]),
                    "timeout": provider.get("timeout", _DEFAULTS["timeout"]),
                    "retry_interval": provider.get(
                        "retry_interval", _DEFAULTS["retry_interval"]
                    ),
                }
                for name, provider in providers.items()
                if name in active_providers
            },
        }
        digest = hashlib.sha256(
            json.dumps(
                route_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]

        # Publish only after the entire file has passed validation.  No caller
        # can observe a partially loaded generation.
        self._providers = self._freeze_mapping(providers)
        self._models = self._freeze_mapping(models)
        self._tasks = self._freeze_mapping(tasks)
        self._snapshot = ModelRoutingSnapshot(
            source_path=str(self._path.resolve()),
            digest=digest,
            task_routes=MappingProxyType(task_routes),
            active_providers=tuple(active_providers),
            provider_count=len(providers),
            model_count=len(models),
        )
        logger.debug(
            f"模型配置已验证: digest={digest}, "
            f"{len(providers)} providers, {len(models)} models, "
            f"{len(tasks)} tasks"
        )

    @staticmethod
    def _require_section(
        data: Mapping[str, Any], section_name: str
    ) -> dict[str, dict[str, Any]]:
        section = data.get(section_name)
        if not isinstance(section, dict) or not section:
            raise ModelRegistryError(f"模型配置缺少非空 [{section_name}] 节")
        invalid = [
            name for name, value in section.items() if not isinstance(value, dict)
        ]
        if invalid:
            raise ModelRegistryError(f"[{section_name}] 条目必须是 table: {invalid}")
        return section

    @staticmethod
    def _require_non_empty_string(value: Any, *, location: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ModelRegistryError(f"{location} 必须是非空字符串")
        return value.strip()

    @staticmethod
    def _reject_unknown_fields(
        value: Mapping[str, Any],
        *,
        allowed: frozenset[str],
        location: str,
    ) -> None:
        unknown = sorted(set(value) - set(allowed))
        if unknown:
            raise ModelRegistryError(f"{location} 包含未知字段: {unknown}")

    @staticmethod
    def _validate_number(
        value: Any,
        *,
        location: str,
        minimum: float,
        strict: bool = False,
    ) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ModelRegistryError(f"{location} 必须是数字")
        parsed = float(value)
        invalid = parsed <= minimum if strict else parsed < minimum
        if not math.isfinite(parsed) or invalid:
            comparator = ">" if strict else ">="
            raise ModelRegistryError(
                f"{location} 必须是有限数且 {comparator} {minimum}"
            )

    @classmethod
    def _validate_providers(cls, providers: Mapping[str, Mapping[str, Any]]) -> None:
        for name, provider in providers.items():
            cls._require_non_empty_string(name, location="provider 名")
            cls._reject_unknown_fields(
                provider,
                allowed=_PROVIDER_FIELDS,
                location=f"providers.{name}",
            )
            base_url = cls._require_non_empty_string(
                provider.get("base_url"), location=f"providers.{name}.base_url"
            )
            parsed_url = urlparse(base_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ModelRegistryError(
                    f"providers.{name}.base_url 必须是完整的 http(s) URL"
                )
            client_type = cls._require_non_empty_string(
                provider.get("client_type", _DEFAULTS["client_type"]),
                location=f"providers.{name}.client_type",
            )
            if client_type not in _READY_CLIENT_TYPES:
                raise ModelRegistryError(
                    f"providers.{name}.client_type 当前无可用客户端: {client_type}"
                )
            api_key = provider.get("api_key", "")
            if not isinstance(api_key, str):
                raise ModelRegistryError(
                    f"providers.{name}.api_key 必须是字符串；"
                    "compact registry 不提供隐式密钥轮换"
                )
            cls._validate_number(
                provider.get("timeout", _DEFAULTS["timeout"]),
                location=f"providers.{name}.timeout",
                minimum=0.0,
                strict=True,
            )
            cls._validate_number(
                provider.get("retry_interval", _DEFAULTS["retry_interval"]),
                location=f"providers.{name}.retry_interval",
                minimum=0.0,
            )
            max_retry = provider.get("max_retry", _DEFAULTS["max_retry"])
            if (
                isinstance(max_retry, bool)
                or not isinstance(max_retry, int)
                or max_retry < 0
            ):
                raise ModelRegistryError(f"providers.{name}.max_retry 必须是非负整数")

    @classmethod
    def _validate_models(
        cls,
        models: Mapping[str, Mapping[str, Any]],
        providers: Mapping[str, Mapping[str, Any]],
    ) -> None:
        for name, model in models.items():
            cls._require_non_empty_string(name, location="model 名")
            cls._reject_unknown_fields(
                model,
                allowed=_MODEL_FIELDS,
                location=f"models.{name}",
            )
            provider_name = cls._require_non_empty_string(
                model.get("provider"), location=f"models.{name}.provider"
            )
            if provider_name not in providers:
                raise ModelRegistryError(
                    f"models.{name}.provider 引用未定义 provider: {provider_name}"
                )
            cls._require_non_empty_string(
                model.get("id", name), location=f"models.{name}.id"
            )
            context = model.get("ctx", _DEFAULTS["ctx"])
            if (
                isinstance(context, bool)
                or not isinstance(context, int)
                or context <= 0
            ):
                raise ModelRegistryError(f"models.{name}.ctx 必须是正整数")
            if not isinstance(model.get("extra", {}), dict):
                raise ModelRegistryError(f"models.{name}.extra 必须是 table")
            for boolean_field in (
                "stream",
                "vision",
                "audio",
                "video",
                "tool_call_compat",
            ):
                value = model.get(boolean_field, _DEFAULTS.get(boolean_field, False))
                if not isinstance(value, bool):
                    raise ModelRegistryError(
                        f"models.{name}.{boolean_field} 必须是布尔值"
                    )
            for price_field in ("price_in", "price_out"):
                cls._validate_number(
                    model.get(price_field, 0.0),
                    location=f"models.{name}.{price_field}",
                    minimum=0.0,
                )

    @classmethod
    def _validate_tasks(
        cls,
        tasks: Mapping[str, Mapping[str, Any]],
        models: Mapping[str, Mapping[str, Any]],
        providers: Mapping[str, Mapping[str, Any]],
    ) -> None:
        for name, task in tasks.items():
            cls._require_non_empty_string(name, location="task 名")
            if name in _TASK_ALIASES:
                raise ModelRegistryError(
                    f"tasks.{name} 与保留兼容别名冲突，请使用 {_TASK_ALIASES[name]}"
                )
            cls._reject_unknown_fields(
                task,
                allowed=_TASK_FIELDS,
                location=f"tasks.{name}",
            )
            model_names = task.get("models")
            if not isinstance(model_names, list) or not model_names:
                raise ModelRegistryError(f"tasks.{name}.models 必须是非空列表")
            if any(
                not isinstance(model_name, str) or not model_name
                for model_name in model_names
            ):
                raise ModelRegistryError(f"tasks.{name}.models 只能包含非空字符串")
            seen: set[str] = set()
            duplicates: set[str] = set()
            for model_name in model_names:
                if model_name in seen:
                    duplicates.add(model_name)
                seen.add(model_name)
            if duplicates:
                raise ModelRegistryError(
                    f"tasks.{name}.models 包含重复模型: {sorted(duplicates)}"
                )
            undefined = [
                model_name for model_name in model_names if model_name not in models
            ]
            if undefined:
                raise ModelRegistryError(
                    f"tasks.{name}.models 引用未定义模型: {undefined}"
                )
            transport_targets: dict[tuple[str, str, str], str] = {}
            duplicate_targets: list[tuple[str, str]] = []
            for model_name in model_names:
                model = models[model_name]
                provider_name = str(model["provider"])
                provider = providers[provider_name]
                target = (
                    provider_name.lower(),
                    str(provider["base_url"]).rstrip("/").lower(),
                    str(model.get("id", model_name)),
                )
                previous_alias = transport_targets.get(target)
                if previous_alias is not None:
                    duplicate_targets.append((previous_alias, model_name))
                else:
                    transport_targets[target] = model_name
            if duplicate_targets:
                raise ModelRegistryError(
                    f"tasks.{name}.models 包含重复传输目标别名: {duplicate_targets}"
                )
            tokens = task.get("tokens", _DEFAULTS["tokens"])
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
                raise ModelRegistryError(f"tasks.{name}.tokens 必须是正整数")
            too_small = (
                []
                if name in _NON_GENERATIVE_TASKS
                else [
                    model_name
                    for model_name in model_names
                    if tokens > int(models[model_name].get("ctx", _DEFAULTS["ctx"]))
                ]
            )
            if too_small:
                raise ModelRegistryError(
                    f"tasks.{name}.tokens 超过候选模型 ctx: {too_small}"
                )
            cls._validate_number(
                task.get("temp", _DEFAULTS["temp"]),
                location=f"tasks.{name}.temp",
                minimum=0.0,
            )

    @classmethod
    def _freeze_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return MappingProxyType(
                {key: cls._freeze_value(item) for key, item in value.items()}
            )
        if isinstance(value, list):
            return tuple(cls._freeze_value(item) for item in value)
        return value

    @classmethod
    def _freeze_mapping(
        cls, value: Mapping[str, Mapping[str, Any]]
    ) -> Mapping[str, Mapping[str, Any]]:
        return MappingProxyType(
            {key: cls._freeze_value(item) for key, item in value.items()}
        )

    @classmethod
    def _thaw_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: cls._thaw_value(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [cls._thaw_value(item) for item in value]
        return value

    def require_tasks(self, required_tasks: set[str] | frozenset[str]) -> None:
        """Fail explicitly when a production consumer task has no route."""

        missing = sorted(set(required_tasks) - set(self._tasks))
        if missing:
            raise ModelRegistryError(f"模型配置缺少必需生产任务: {missing}")

    def log_snapshot(self) -> None:
        """Log a complete secret-free task priority manifest once at startup."""

        routes = "; ".join(
            f"{task}={' -> '.join(models)}"
            for task, models in self._snapshot.task_routes.items()
        )
        logger.info(
            "模型路由快照已加载: "
            f"source={self._snapshot.source_path}, digest={self._snapshot.digest}, "
            f"providers={self._snapshot.provider_count}, "
            f"models={self._snapshot.model_count}, routes=[{routes}]"
        )

    @property
    def providers(self) -> Mapping[str, Mapping[str, Any]]:
        return self._providers

    @property
    def models(self) -> Mapping[str, Mapping[str, Any]]:
        return self._models

    @property
    def tasks(self) -> Mapping[str, Mapping[str, Any]]:
        return self._tasks

    @property
    def snapshot(self) -> ModelRoutingSnapshot:
        return self._snapshot

    def get_task(self, task_name: str) -> list[dict[str, Any]]:
        """Return an ordered model set for a canonical task or legacy alias."""

        resolved = _TASK_ALIASES.get(task_name, task_name)
        task = self._tasks.get(resolved)
        if task is None:
            raise ValueError(
                f"任务 '{task_name}' 未找到。可用: {list(self._tasks.keys())}"
            )

        model_names = task.get("models", ())
        max_tokens = int(task.get("tokens", _DEFAULTS["tokens"]))
        temperature = float(task.get("temp", _DEFAULTS["temp"]))
        model_set: list[dict[str, Any]] = []
        for priority, name in enumerate(model_names):
            entry = self._build_model_entry(
                str(name),
                max_tokens,
                temperature,
                routing_task=resolved,
                routing_priority=priority,
            )
            if entry is not None:
                model_set.append(entry)

        if not model_set:
            raise ValueError(f"任务 '{task_name}' 无可用模型")
        return model_set

    def get_model_entry(
        self,
        model_name: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any] | None:
        """Return one registered model without assigning it to a task route."""

        resolved_tokens = _DEFAULTS["tokens"] if max_tokens is None else max_tokens
        resolved_temperature = _DEFAULTS["temp"] if temperature is None else temperature
        if (
            isinstance(resolved_tokens, bool)
            or not isinstance(resolved_tokens, int)
            or resolved_tokens <= 0
        ):
            raise ValueError("max_tokens 必须是正整数")
        self._validate_number(
            resolved_temperature,
            location="temperature",
            minimum=0.0,
        )
        return self._build_model_entry(
            model_name,
            resolved_tokens,
            float(resolved_temperature),
        )

    def list_model_names(self) -> list[str]:
        return list(self._models.keys())

    def list_task_names(self) -> list[str]:
        return list(self._tasks.keys())

    def _build_model_entry(
        self,
        model_name: str,
        max_tokens: int,
        temperature: float,
        *,
        routing_task: str | None = None,
        routing_priority: int | None = None,
    ) -> dict[str, Any] | None:
        model = self._models.get(model_name)
        if model is None:
            return None

        provider_name = str(model["provider"])
        provider = self._providers[provider_name]
        model_id = str(model.get("id", model_name))
        modalities = ["text"]
        if model.get("vision", False):
            modalities.append("image")
        if model.get("audio", False):
            modalities.append("audio")
        if model.get("video", False):
            modalities.append("video")

        entry: dict[str, Any] = {
            "api_provider": provider_name,
            "base_url": provider.get("base_url", ""),
            "model_identifier": model_id,
            "api_key": self._thaw_value(provider.get("api_key", "")),
            "client_type": provider.get("client_type", _DEFAULTS["client_type"]),
            "max_retry": provider.get("max_retry", _DEFAULTS["max_retry"]),
            "timeout": provider.get("timeout", _DEFAULTS["timeout"]),
            "retry_interval": provider.get(
                "retry_interval", _DEFAULTS["retry_interval"]
            ),
            "price_in": model.get("price_in", 0.0),
            "price_out": model.get("price_out", 0.0),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "max_context": model.get("ctx", _DEFAULTS["ctx"]),
            "force_stream_mode": model.get("stream", _DEFAULTS["stream"]),
            "tool_call_compat": model.get(
                "tool_call_compat", _DEFAULTS["tool_call_compat"]
            ),
            "media_capabilities": {"modalities": modalities},
            "extra_params": self._thaw_value(model.get("extra", {})),
        }
        if routing_task is not None and routing_priority is not None:
            entry.update(
                {
                    "routing_task": routing_task,
                    "routing_model_alias": model_name,
                    "routing_priority": routing_priority,
                    "routing_snapshot": self._snapshot.digest,
                }
            )
        return entry


_models_config: ModelsConfig | None = None
_models_config_lock = threading.Lock()


def init_models_config(
    path: str | Path = "config/models.toml",
    *,
    required_tasks: set[str] | frozenset[str] = PRODUCTION_MODEL_TASKS,
) -> ModelsConfig:
    """Atomically install one validated production routing generation."""

    global _models_config
    with _models_config_lock:
        candidate = ModelsConfig(path)
        candidate.require_tasks(required_tasks)
        _models_config = candidate
    candidate.log_snapshot()
    return candidate


def get_models_config() -> ModelsConfig:
    """Return the authoritative registry, loading it exactly once if needed."""

    global _models_config
    existing = _models_config
    if existing is not None:
        return existing

    with _models_config_lock:
        existing = _models_config
        if existing is None:
            existing = ModelsConfig()
            existing.require_tasks(PRODUCTION_MODEL_TASKS)
            _models_config = existing
    existing.log_snapshot()
    return existing
