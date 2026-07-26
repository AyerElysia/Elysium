"""统一配置 Schema。

定义 Elysium 全系统的配置结构。所有配置项在此声明，
由 unified.py 负责加载、环境变量覆盖和全局访问。

设计原则：
- 配置即声明：一个 TOML 文件描述全部运行时行为
- 分层覆盖：默认值 → 配置文件 → 环境变量 → 运行时修改
- 扩展性：新模块通过 `register_section()` 动态注入配置节
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field as PydanticField


# ─────────────────────────────────────────────
# 子模型
# ─────────────────────────────────────────────


class ProviderConfig(BaseModel):
    """单个 LLM Provider 配置。"""

    base_url: str = ""
    api_key: str | list[str] = ""
    client_type: Literal["openai", "anthropic", "gemini", "bedrock"] = "openai"
    max_retry: int = 3
    timeout: int = 60
    retry_interval: int = 5
    extra: dict[str, Any] = PydanticField(default_factory=dict)


class ModelConfig(BaseModel):
    """单个模型配置。"""

    provider: str = ""  # 对应 providers 中的 key
    model: str = ""
    max_tokens: int = 4096
    temperature: float = 0.7
    top_p: float = 1.0
    extra: dict[str, Any] = PydanticField(default_factory=dict)


class LLMSection(BaseModel):
    """LLM 配置节。"""

    providers: dict[str, ProviderConfig] = PydanticField(default_factory=dict)
    models: dict[str, ModelConfig] = PydanticField(default_factory=dict)
    routing: dict[str, str] = PydanticField(
        default_factory=lambda: {"default": "main"}
    )


class RuntimeSection(BaseModel):
    """运行时配置节。"""

    log_level: str = "INFO"
    db_path: str = "data/elysium.db"
    data_dir: str = "data"
    plugins_dir: str = "plugins"
    shutdown_timeout: float = 15.0
    force_shutdown_after: float = 5.0
    process_workers: int = 4
    trust_env: bool = True


class HTTPSection(BaseModel):
    """HTTP 服务配置节。"""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 18000
    api_keys: list[str] = PydanticField(default_factory=list)


class PermissionSection(BaseModel):
    """权限配置节。"""

    owner_list: list[str] = PydanticField(default_factory=list)
    default_level: str = "user"
    strict_mode: bool = True
    cache_ttl: int = 300


# ─────────────────────────────────────────────
# 顶层 Schema
# ─────────────────────────────────────────────


class ElysiumSchema(BaseModel):
    """Elysium 统一配置顶层结构。

    对应 config/elysium.toml：

    ```toml
    [runtime]
    log_level = "INFO"

    [llm.providers.openai]
    base_url = "https://api.openai.com/v1"
    api_key = "${OPENAI_API_KEY}"

    [llm.models.main]
    provider = "openai"
    model = "gpt-4o"

    [llm.routing]
    default = "main"
    ```
    """

    runtime: RuntimeSection = PydanticField(default_factory=RuntimeSection)
    llm: LLMSection = PydanticField(default_factory=LLMSection)
    http: HTTPSection = PydanticField(default_factory=HTTPSection)
    permission: PermissionSection = PydanticField(default_factory=PermissionSection)

    # 动态扩展区：任何模块可以往这里塞自定义配置
    extra: dict[str, Any] = PydanticField(default_factory=dict)

    model_config = {"extra": "allow"}  # 允许未知 key，保证前向兼容
