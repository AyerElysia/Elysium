"""统一配置加载器。

职责：
- 从 config/elysium.toml 加载配置（若不存在则回退到老文件）
- 环境变量插值：${VAR_NAME} 语法
- 分层覆盖：默认值 → TOML → 环境变量（ELYSIUM_*）→ 运行时 set
- 全局访问：get_config()

用法：
    from src.kernel.config.unified import get_config, init_config

    # 启动时
    init_config("config/elysium.toml")

    # 任何地方
    cfg = get_config()
    model = cfg.llm.routing["default"]
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .schema import ElysiumSchema

# ─────────────────────────────────────────────
# 全局状态
# ─────────────────────────────────────────────

_config: ElysiumSchema | None = None
_config_path: Path | None = None

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_PREFIX = "ELYSIUM_"


# ─────────────────────────────────────────────
# 环境变量插值
# ─────────────────────────────────────────────


def _interpolate_env(value: Any) -> Any:
    """递归替换字符串中的 ${VAR} 为环境变量值。"""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            var_name = m.group(1)
            return os.environ.get(var_name, m.group(0))  # 未设置则保留原文
        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(item) for item in value]
    return value


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """应用 ELYSIUM_ 前缀的环境变量覆盖。

    规则：ELYSIUM_{SECTION}_{KEY} → data[section][key]
    例如：ELYSIUM_RUNTIME_LOG_LEVEL=DEBUG → data["runtime"]["log_level"] = "DEBUG"
    """
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        parts = env_key[len(_ENV_PREFIX):].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, key = parts
        if section in data and isinstance(data[section], dict):
            # 尝试类型转换
            data[section][key] = _coerce(env_val)
    return data


def _coerce(val: str) -> Any:
    """尝试将字符串转为 bool/int/float。"""
    if val.lower() in ("true", "false"):
        return val.lower() == "true"
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


# ─────────────────────────────────────────────
# 老配置兼容桥接
# ─────────────────────────────────────────────


def _load_legacy_configs(config_dir: Path) -> dict[str, Any]:
    """从老的 core.toml + model.toml 构建统一格式（兼容模式）。"""
    data: dict[str, Any] = {}

    # core.toml → runtime + http + permission
    core_path = config_dir / "core.toml"
    if core_path.exists():
        with open(core_path, "rb") as f:
            core = tomllib.load(f)
        bot = core.get("bot", {})
        data["runtime"] = {
            "log_level": bot.get("log_level", "INFO"),
            "data_dir": bot.get("data_dir", "data"),
            "plugins_dir": bot.get("plugins_dir", "plugins"),
            "shutdown_timeout": bot.get("shutdown_timeout", 15.0),
            "force_shutdown_after": bot.get("force_shutdown_after", 5.0),
            "process_workers": core.get("advanced", {}).get("process_workers", 4),
            "trust_env": core.get("advanced", {}).get("trust_env", True),
        }
        http = core.get("http_router", {})
        data["http"] = {
            "enabled": http.get("enable_http_router", True),
            "host": http.get("http_router_host", "127.0.0.1"),
            "port": http.get("http_router_port", 18000),
            "api_keys": http.get("api_keys", []),
        }
        perm = core.get("permission", {})
        data["permission"] = {
            "owner_list": perm.get("owner_list", []),
            "default_level": perm.get("default_permission_level", "user"),
            "strict_mode": perm.get("strict_mode", True),
            "cache_ttl": perm.get("permission_cache_ttl", 300),
        }

    # model.toml → llm
    model_path = config_dir / "model.toml"
    if model_path.exists():
        with open(model_path, "rb") as f:
            model_data = tomllib.load(f)
        providers: dict[str, Any] = {}
        for p in model_data.get("api_providers", []):
            name = p.get("name", "").lower().replace(" ", "_")
            if name:
                providers[name] = {
                    "base_url": p.get("base_url", ""),
                    "api_key": p.get("api_key", ""),
                    "client_type": p.get("client_type", "openai"),
                    "max_retry": p.get("max_retry", 3),
                    "timeout": p.get("timeout", 60),
                    "retry_interval": p.get("retry_interval", 5),
                }
        models: dict[str, Any] = {}
        for m in model_data.get("models", []):
            name = m.get("name", "")
            if name:
                models[name] = {
                    "provider": m.get("provider", ""),
                    "model": m.get("model_identifier", name),
                    "max_tokens": m.get("max_tokens", 4096),
                    "temperature": m.get("temperature", 0.7),
                }
        data["llm"] = {"providers": providers, "models": models, "routing": {"default": "main"}}

    return data


# ─────────────────────────────────────────────
# 公共 API
# ─────────────────────────────────────────────


def init_config(
    config_path: str | Path = "config/elysium.toml",
    *,
    env_override: bool = True,
) -> ElysiumSchema:
    """初始化统一配置。

    加载顺序：
    1. 若 config_path 存在 → 读取统一 TOML
    2. 否则 → 从同目录的 core.toml + model.toml 兼容加载
    3. 环境变量插值（${VAR}）
    4. ELYSIUM_* 环境变量覆盖

    Args:
        config_path: 统一配置文件路径
        env_override: 是否启用 ELYSIUM_* 环境变量覆盖

    Returns:
        解析后的 ElysiumSchema 实例
    """
    global _config, _config_path

    path = Path(config_path)
    _config_path = path

    if path.exists():
        with open(path, "rb") as f:
            raw: dict[str, Any] = tomllib.load(f)
    else:
        # 兼容模式：从老文件构建
        raw = _load_legacy_configs(path.parent)

    # 环境变量插值
    raw = _interpolate_env(raw)

    # ELYSIUM_* 覆盖
    if env_override:
        raw = _apply_env_overrides(raw)

    _config = ElysiumSchema.model_validate(raw)
    return _config


def get_config() -> ElysiumSchema:
    """获取全局配置实例。

    若尚未初始化，自动以默认路径初始化。
    """
    global _config
    if _config is None:
        return init_config()
    return _config


def reload_config() -> ElysiumSchema:
    """重新从文件加载配置（热重载）。"""
    if _config_path is None:
        return init_config()
    return init_config(_config_path)


def set_runtime(key_path: str, value: Any) -> None:
    """运行时动态修改配置（不持久化）。

    Args:
        key_path: 点分路径，如 "runtime.log_level"
        value: 新值
    """
    cfg = get_config()
    parts = key_path.split(".")
    obj: Any = cfg
    for part in parts[:-1]:
        obj = getattr(obj, part, None)
        if obj is None:
            return
    setattr(obj, parts[-1], value)
