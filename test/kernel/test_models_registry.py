"""Checks for the committed compact model-registry baseline."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from src.core.config.model_config import ModelConfig
from src.kernel.config import models_loader
from src.kernel.config.models_loader import (
    PRODUCTION_MODEL_TASKS,
    ModelRegistryError,
    ModelsConfig,
)
from src.kernel.llm.api import _resolve_model_set

EXPECTED_TASK_BUDGETS = {
    "core": 32000,
    "learning": 32000,
    "expression": 32000,
    "witness": 16000,
    "agent": 32000,
    "utility": 16000,
    "vision": 16000,
    "voice": 8192,
    "embedding": 8192,
    "router": 8192,
    "router_context_projection": 16000,
    "live": 32000,
}
EXPECTED_CONTEXT_BUDGETS = {
    "core": 100000,
    "learning": 100000,
    "expression": 200000,
    "witness": 100000,
    "agent": 200000,
    "utility": 64000,
    "vision": 100000,
    "router": 32000,
    "router_context_projection": 100000,
    "live": 100000,
}
EXPECTED_ATTEMPT_TIMEOUTS = {
    "core": 180,
    "learning": 300,
    "witness": 300,
    "agent": 300,
    "utility": 180,
}
GENERATIVE_TASKS = set(EXPECTED_TASK_BUDGETS) - {"voice", "embedding"}


def test_models_example_is_complete_and_budgeted() -> None:
    registry_path = Path(__file__).parents[2] / "config" / "models.toml.example"
    config = ModelsConfig(registry_path)

    assert set(config.tasks) == set(EXPECTED_TASK_BUDGETS)
    assert {
        task_name: task["tokens"] for task_name, task in config.tasks.items()
    } == EXPECTED_TASK_BUDGETS

    for task_name, task in config.tasks.items():
        entries = config.get_task(task_name)
        assert len(entries) == len(task["models"])
        assert all(entry["max_tokens"] == task["tokens"] for entry in entries)
        if task_name in GENERATIVE_TASKS:
            assert task["context_tokens"] == EXPECTED_CONTEXT_BUDGETS[task_name]
            assert all(
                entry["context_tokens"] == task["context_tokens"] for entry in entries
            )
        else:
            assert "context_tokens" not in task
            assert all("context_tokens" not in entry for entry in entries)

        expected_timeout = EXPECTED_ATTEMPT_TIMEOUTS.get(task_name, 120)
        assert all(entry["timeout"] == expected_timeout for entry in entries)


def test_models_example_uses_environment_credentials_but_remains_structural(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = Path(__file__).parents[2] / "config" / "models.toml.example"
    monkeypatch.delenv("ELYSIUM_NEXUS_API_KEY", raising=False)

    config = ModelsConfig(registry_path)

    assert config.providers["NexusAI"]["api_key"] == "${ELYSIUM_NEXUS_API_KEY}"


def test_example_defines_background_attempt_timeout_policy() -> None:
    # 仅依赖入库的 models.toml.example（CI 自包含、不含真实密钥）；
    # 生产 config/models.toml 被 .gitignore 忽略，绝不能作为 CI 测试输入。
    config_dir = Path(__file__).parents[2] / "config"
    example = ModelsConfig(config_dir / "models.toml.example")

    assert "learning" in PRODUCTION_MODEL_TASKS
    assert {
        task_name: example.tasks[task_name].get("attempt_timeout_seconds")
        for task_name in EXPECTED_ATTEMPT_TIMEOUTS
    } == EXPECTED_ATTEMPT_TIMEOUTS


def test_task_routes_preserve_toml_order() -> None:
    registry_path = Path(__file__).parents[2] / "config" / "models.toml.example"
    config = ModelsConfig(registry_path)

    for task_name, task in config.tasks.items():
        entries = config.get_task(task_name)
        assert [entry["routing_model_alias"] for entry in entries] == list(
            task["models"]
        )


def test_expression_keeps_vision_capable_fallback() -> None:
    """表达层文本优先 DeepSeek，但列表必须保留至少一个 vision 模型兜底识图。

    media-aware routing (LLMRequest.filter_model_set_for_media) 会在 payload
    含图片/表情时把 model_set 过滤到支持 image 的成员，因此文本优先 + 视觉
    兜底的结构不会丢失原生识图能力。
    """
    registry_path = Path(__file__).parents[2] / "config" / "models.toml.example"
    config = ModelsConfig(registry_path)

    expression_models = config.tasks["expression"]["models"]

    # 首选模型可文本优先；列表内必须至少有一个 vision-capable 模型承接识图。
    assert any(config.models[name].get("vision", False) for name in expression_models)
    assert all(
        config.models[name].get("vision", False)
        or config.models[name].get("ctx", 0) >= config.tasks["expression"]["context_tokens"]
        for name in expression_models
    )


def test_task_routes_only_reference_unique_registered_models() -> None:
    registry_path = Path(__file__).parents[2] / "config" / "models.toml.example"
    config = ModelsConfig(registry_path)

    for task in config.tasks.values():
        model_names = list(task["models"])
        assert len(model_names) == len(set(model_names))
        assert set(model_names).issubset(config.models)


def test_task_context_budgets_replace_per_model_triggers() -> None:
    registry_path = Path(__file__).parents[2] / "config" / "models.toml.example"
    raw = tomllib.loads(registry_path.read_text(encoding="utf-8"))

    for model in raw["models"].values():
        assert "context_compression_trigger_tokens" not in model.get("extra", {})

    for task_name in GENERATIVE_TASKS:
        task = raw["tasks"][task_name]
        assert task["context_tokens"] == EXPECTED_CONTEXT_BUDGETS[task_name]
        for model_name in task["models"]:
            assert (
                task["context_tokens"] + task["tokens"]
                <= raw["models"][model_name]["ctx"]
            )


def test_example_task_inference_policy_is_task_specific() -> None:
    registry_path = Path(__file__).parents[2] / "config" / "models.toml.example"
    config = ModelsConfig(registry_path)

    core = {
        entry["routing_model_alias"]: entry["extra_params"]
        for entry in config.get_task("core")
    }
    expression = {
        entry["routing_model_alias"]: entry["extra_params"]
        for entry in config.get_task("expression")
    }
    router = {
        entry["routing_model_alias"]: entry["extra_params"]
        for entry in config.get_task("router")
    }

    assert core["deepseek-v4-flash"]["thinking"]["type"] == "enabled"
    assert core["gpt-5.6-luna"]["reasoning_effort"] == "high"
    assert expression["gpt-5.6-luna"]["reasoning_effort"] == "low"
    assert expression["gpt-5.6-terra"]["reasoning_effort"] == "low"
    assert expression["gpt-5.6-sol"]["reasoning_effort"] == "low"
    assert all(params["parallel_tool_calls"] is True for params in expression.values())
    assert router["deepseek-v4-flash"]["thinking"]["type"] == "disabled"
    assert router["gpt-5.6-luna"]["reasoning_effort"] == "low"
    assert router["MiMo-V2.5"]["enable_thinking"] is False


def _write_minimal_registry(
    path: Path,
    *,
    api_key: str = "secret-a",
    task_models: str = '["primary", "backup"]',
) -> None:
    path.write_text(
        f"""
[providers.gateway]
base_url = "http://127.0.0.1:3000/v1"
api_key = "{api_key}"
client_type = "openai"
timeout = 120
max_retry = 0
retry_interval = 0

[models.primary]
provider = "gateway"
id = "primary-id"
ctx = 32000

[models.backup]
provider = "gateway"
id = "backup-id"
ctx = 32000

[tasks.expression]
models = {task_models}
tokens = 8000
temp = 0.4
context_tokens = 16000
""".lstrip(),
        encoding="utf-8",
    )


def test_registry_recursively_expands_environment_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "models.toml"
    secret = "runtime-secret-must-not-appear-in-errors"
    monkeypatch.setenv("ELYSIUM_TEST_MODEL_API_KEY", secret)
    monkeypatch.setenv(
        "ELYSIUM_TEST_MODEL_BASE_URL",
        "https://gateway.example/v1",
    )
    monkeypatch.setenv("ELYSIUM_TEST_MODEL_ID", "expanded-primary-id")
    monkeypatch.setattr(models_loader, "_models_config", None)
    _write_minimal_registry(
        registry_path,
        api_key="${ELYSIUM_TEST_MODEL_API_KEY}",
    )
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8")
        .replace(
            'base_url = "http://127.0.0.1:3000/v1"',
            'base_url = "${ELYSIUM_TEST_MODEL_BASE_URL}"',
        )
        .replace('id = "primary-id"', 'id = "${ELYSIUM_TEST_MODEL_ID}"'),
        encoding="utf-8",
    )

    config = models_loader.init_models_config(
        registry_path,
        required_tasks=frozenset({"expression"}),
    )

    assert config.providers["gateway"]["api_key"] == secret
    assert config.providers["gateway"]["base_url"] == "https://gateway.example/v1"
    assert config.get_task("expression")[0]["api_key"] == secret
    assert config.get_task("expression")[0]["model_identifier"] == (
        "expanded-primary-id"
    )
    assert models_loader.get_models_config() is config


def test_unresolved_environment_credential_is_structurally_inspectable_but_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "models.toml"
    placeholder = "${ELYSIUM_TEST_UNSET_MODEL_API_KEY}"
    monkeypatch.delenv("ELYSIUM_TEST_UNSET_MODEL_API_KEY", raising=False)
    monkeypatch.setattr(models_loader, "_models_config", None)
    _write_minimal_registry(registry_path, api_key=placeholder)

    direct = ModelsConfig(registry_path)
    assert direct.providers["gateway"]["api_key"] == placeholder

    with pytest.raises(ModelRegistryError, match="未解析") as caught:
        models_loader.init_models_config(
            registry_path,
            required_tasks=frozenset({"expression"}),
        )

    message = str(caught.value)
    assert "providers.gateway.api_key" in message
    assert placeholder not in message
    assert models_loader._models_config is None

    monkeypatch.setattr(models_loader, "_models_config", direct)
    with pytest.raises(ModelRegistryError, match="未解析") as get_caught:
        models_loader.get_models_config()
    assert "providers.gateway.api_key" in str(get_caught.value)
    assert placeholder not in str(get_caught.value)


def test_runtime_rejects_unresolved_nested_placeholder_without_value_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "models.toml"
    placeholder = "${ELYSIUM_TEST_UNSET_TASK_VALUE}"
    monkeypatch.delenv("ELYSIUM_TEST_UNSET_TASK_VALUE", raising=False)
    monkeypatch.setattr(models_loader, "_models_config", None)
    _write_minimal_registry(registry_path)
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(
            "context_tokens = 16000",
            f'context_tokens = 16000\nextra = {{ marker = "{placeholder}" }}',
        ),
        encoding="utf-8",
    )

    direct = ModelsConfig(registry_path)
    assert direct.tasks["expression"]["extra"]["marker"] == placeholder

    with pytest.raises(ModelRegistryError, match="未解析") as caught:
        models_loader.init_models_config(
            registry_path,
            required_tasks=frozenset({"expression"}),
        )

    message = str(caught.value)
    assert "tasks.expression.extra.marker" in message
    assert placeholder not in message


@pytest.mark.parametrize(
    ("api_key", "message"),
    [
        ("", "不能为空"),
        ("   ", "不能为空"),
        ("replace-with-never-print-this-token", "示例占位符"),
        ("RePlAcE-WiTh-Never-Print-This-Token", "示例占位符"),
    ],
)
@pytest.mark.parametrize("access_path", ["init", "get"])
def test_runtime_paths_reject_empty_and_known_placeholders_without_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_key: str,
    message: str,
    access_path: str,
) -> None:
    registry_path = tmp_path / "models.toml"
    _write_minimal_registry(registry_path, api_key=api_key)
    direct = ModelsConfig(registry_path)
    monkeypatch.setattr(
        models_loader,
        "_models_config",
        None if access_path == "init" else direct,
    )

    with pytest.raises(ModelRegistryError, match=message) as caught:
        if access_path == "init":
            models_loader.init_models_config(
                registry_path,
                required_tasks=frozenset({"expression"}),
            )
        else:
            models_loader.get_models_config()

    error = str(caught.value)
    assert "providers.gateway.api_key" in error
    if api_key.strip():
        assert api_key not in error


def test_registry_preserves_priority_and_attaches_snapshot_identity(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "models.toml"
    _write_minimal_registry(registry_path)

    config = ModelsConfig(registry_path)
    entries = config.get_task("expression")

    assert [entry["model_identifier"] for entry in entries] == [
        "primary-id",
        "backup-id",
    ]
    assert [entry["routing_priority"] for entry in entries] == [0, 1]
    assert {entry["routing_task"] for entry in entries} == {"expression"}
    assert {entry["routing_snapshot"] for entry in entries} == {config.snapshot.digest}

    with pytest.raises(TypeError):
        config.tasks["expression"]["tokens"] = 1  # type: ignore[index]


def test_task_attempt_timeout_overrides_provider_for_every_candidate_and_alias(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "models.toml"
    _write_minimal_registry(registry_path)
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(
            "context_tokens = 16000",
            "context_tokens = 16000\nattempt_timeout_seconds = 240.5",
            1,
        ),
        encoding="utf-8",
    )

    config = ModelsConfig(registry_path)

    assert {entry["timeout"] for entry in config.get_task("expression")} == {240.5}
    assert {entry["timeout"] for entry in config.get_task("actor")} == {240.5}
    assert {entry["routing_task"] for entry in config.get_task("actor")} == {
        "expression"
    }
    assert all(
        "attempt_timeout_seconds" not in entry["extra_params"]
        for entry in config.get_task("expression")
    )


def test_task_without_attempt_timeout_inherits_provider_value(tmp_path: Path) -> None:
    registry_path = tmp_path / "models.toml"
    _write_minimal_registry(registry_path)

    config = ModelsConfig(registry_path)

    assert {entry["timeout"] for entry in config.get_task("expression")} == {120}


@pytest.mark.parametrize(
    "value", ["true", "0", "-1", '"120"', "nan", "inf", "-inf"]
)
def test_registry_rejects_invalid_task_attempt_timeout(
    tmp_path: Path,
    value: str,
) -> None:
    registry_path = tmp_path / "invalid-task-timeout.toml"
    _write_minimal_registry(registry_path)
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(
            "context_tokens = 16000",
            f"context_tokens = 16000\nattempt_timeout_seconds = {value}",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ModelRegistryError,
        match=r"tasks\.expression\.attempt_timeout_seconds",
    ):
        ModelsConfig(registry_path)


def test_task_extra_merges_over_model_defaults_without_shared_mutation(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "models.toml"
    _write_minimal_registry(registry_path)
    configured = registry_path.read_text(encoding="utf-8").replace(
        "[models.backup]",
        'extra = { reasoning_effort = "high", parallel_tool_calls = false }\n\n'
        "[models.backup]",
        1,
    )
    configured = configured.replace(
        "context_tokens = 16000",
        "context_tokens = 16000\n"
        "extra = { parallel_tool_calls = true, seed = 7 }\n"
        'model_extra = { primary = { reasoning_effort = "low" } }',
        1,
    )
    registry_path.write_text(configured, encoding="utf-8")

    config = ModelsConfig(registry_path)
    entries = config.get_task("expression")
    primary = entries[0]["extra_params"]
    backup = entries[1]["extra_params"]

    assert primary == {
        "reasoning_effort": "low",
        "parallel_tool_calls": True,
        "seed": 7,
    }
    assert backup == {"parallel_tool_calls": True, "seed": 7}

    primary["seed"] = 99
    assert config.get_task("expression")[0]["extra_params"]["seed"] == 7


def test_registry_digest_is_secret_free_and_route_sensitive(tmp_path: Path) -> None:
    first_path = tmp_path / "first.toml"
    second_path = tmp_path / "second.toml"
    changed_path = tmp_path / "changed.toml"
    budget_path = tmp_path / "budget.toml"
    context_budget_path = tmp_path / "context-budget.toml"
    attempt_timeout_path = tmp_path / "attempt-timeout.toml"
    endpoint_path = tmp_path / "endpoint.toml"
    inference_path = tmp_path / "inference.toml"
    _write_minimal_registry(first_path, api_key="secret-a")
    _write_minimal_registry(second_path, api_key="secret-b")
    _write_minimal_registry(
        changed_path,
        api_key="secret-a",
        task_models='["backup", "primary"]',
    )
    _write_minimal_registry(budget_path, api_key="secret-a")
    budget_path.write_text(
        budget_path.read_text(encoding="utf-8").replace(
            "tokens = 8000", "tokens = 9000"
        ),
        encoding="utf-8",
    )
    _write_minimal_registry(context_budget_path, api_key="secret-a")
    context_budget_path.write_text(
        context_budget_path.read_text(encoding="utf-8").replace(
            "context_tokens = 16000", "context_tokens = 15000"
        ),
        encoding="utf-8",
    )
    _write_minimal_registry(attempt_timeout_path, api_key="secret-a")
    attempt_timeout_path.write_text(
        attempt_timeout_path.read_text(encoding="utf-8").replace(
            "context_tokens = 16000",
            "context_tokens = 16000\nattempt_timeout_seconds = 240",
        ),
        encoding="utf-8",
    )
    _write_minimal_registry(endpoint_path, api_key="secret-a")
    endpoint_path.write_text(
        endpoint_path.read_text(encoding="utf-8").replace(
            "http://127.0.0.1:3000/v1", "https://gateway.example/v1"
        ),
        encoding="utf-8",
    )
    _write_minimal_registry(inference_path, api_key="secret-a")
    inference_path.write_text(
        inference_path.read_text(encoding="utf-8").replace(
            "context_tokens = 16000",
            "context_tokens = 16000\nextra = { parallel_tool_calls = true }",
        ),
        encoding="utf-8",
    )

    first = ModelsConfig(first_path)
    second = ModelsConfig(second_path)
    changed = ModelsConfig(changed_path)
    budget = ModelsConfig(budget_path)
    context_budget = ModelsConfig(context_budget_path)
    attempt_timeout = ModelsConfig(attempt_timeout_path)
    endpoint = ModelsConfig(endpoint_path)
    inference = ModelsConfig(inference_path)

    assert first.snapshot.digest == second.snapshot.digest
    assert first.snapshot.digest == endpoint.snapshot.digest
    assert changed.snapshot.digest != first.snapshot.digest
    assert budget.snapshot.digest != first.snapshot.digest
    assert context_budget.snapshot.digest != first.snapshot.digest
    assert attempt_timeout.snapshot.digest != first.snapshot.digest
    assert inference.snapshot.digest != first.snapshot.digest
    assert "secret-a" not in first.snapshot.digest


@pytest.mark.parametrize(
    ("task_override", "message"),
    [
        ('extra = "invalid"', r"tasks\.expression\.extra"),
        ('model_extra = "invalid"', r"tasks\.expression\.model_extra"),
        (
            "model_extra = { missing = { seed = 1 } }",
            r"tasks\.expression\.model_extra",
        ),
        (
            'model_extra = { primary = "invalid" }',
            r"tasks\.expression\.model_extra",
        ),
    ],
)
def test_registry_rejects_invalid_task_inference_overrides(
    tmp_path: Path,
    task_override: str,
    message: str,
) -> None:
    registry_path = tmp_path / "invalid-task-extra.toml"
    _write_minimal_registry(registry_path)
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(
            "context_tokens = 16000",
            f"context_tokens = 16000\n{task_override}",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModelRegistryError, match=message):
        ModelsConfig(registry_path)


@pytest.mark.parametrize(
    ("task_models", "message"),
    [
        ('["primary", "missing"]', "引用未定义模型"),
        ('["primary", "primary"]', "包含重复模型"),
        ("[]", "必须是非空列表"),
    ],
)
def test_registry_rejects_invalid_routes_without_leaking_secrets(
    tmp_path: Path,
    task_models: str,
    message: str,
) -> None:
    registry_path = tmp_path / "invalid.toml"
    _write_minimal_registry(
        registry_path,
        api_key="never-log-this-key",
        task_models=task_models,
    )

    with pytest.raises(ModelRegistryError, match=message) as caught:
        ModelsConfig(registry_path)

    assert "never-log-this-key" not in str(caught.value)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("ctx = 32000", "ctx = 32000\ncttx = 1", "包含未知字段"),
        (
            'id = "backup-id"',
            'id = "primary-id"',
            "包含重复传输目标别名",
        ),
        (
            'client_type = "openai"',
            'client_type = "not-installed"',
            "当前无可用客户端",
        ),
        (
            'api_key = "secret-a"',
            'api_key = ["secret-a", "secret-b"]',
            "不提供隐式密钥轮换",
        ),
        (
            'base_url = "http://127.0.0.1:3000/v1"',
            'base_url = "gateway-without-scheme"',
            "必须是完整的 http",
        ),
        ("tokens = 8000", "tokens = 64000", "超过候选模型 ctx"),
    ],
)
def test_registry_rejects_schema_typos_and_fake_failover_targets(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    registry_path = tmp_path / "invalid-schema.toml"
    _write_minimal_registry(registry_path)
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(old, new, 1),
        encoding="utf-8",
    )

    with pytest.raises(ModelRegistryError, match=message):
        ModelsConfig(registry_path)


def test_registry_rejects_task_context_plus_output_over_model_window(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "invalid-task-context.toml"
    _write_minimal_registry(registry_path)
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(
            "context_tokens = 16000", "context_tokens = 25000"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModelRegistryError, match=r"context_tokens \+ tokens"):
        ModelsConfig(registry_path)


def test_registry_rejects_unknown_top_level_sections(tmp_path: Path) -> None:
    registry_path = tmp_path / "invalid-top-level.toml"
    _write_minimal_registry(registry_path)
    registry_path.write_text(
        '[metadata]\nowner = "nobody"\n\n' + registry_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ModelRegistryError, match="未知顶层节"):
        ModelsConfig(registry_path)


def test_failed_registry_reload_preserves_last_valid_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "models.toml"
    _write_minimal_registry(registry_path)
    monkeypatch.setattr(models_loader, "_models_config", None)

    first = models_loader.init_models_config(
        registry_path,
        required_tasks=frozenset({"expression"}),
    )
    with pytest.raises(ModelRegistryError):
        models_loader.init_models_config(
            tmp_path / "missing.toml",
            required_tasks=frozenset({"expression"}),
        )

    assert models_loader.get_models_config() is first


def test_production_registry_requires_every_consumer_task(tmp_path: Path) -> None:
    registry_path = tmp_path / "models.toml"
    _write_minimal_registry(registry_path)
    config = ModelsConfig(registry_path)

    with pytest.raises(ModelRegistryError, match="缺少必需生产任务"):
        config.require_tasks(PRODUCTION_MODEL_TASKS)


def test_global_model_config_does_not_silently_fall_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.config.model_config as legacy_module

    legacy_config = ModelConfig()
    monkeypatch.setattr(legacy_module, "_global_model_config", legacy_config)

    class BrokenRegistry:
        def get_task(self, _task_name: str) -> list[dict[str, object]]:
            raise ModelRegistryError("authoritative registry is invalid")

    monkeypatch.setattr(
        models_loader,
        "get_models_config",
        lambda: BrokenRegistry(),
    )

    with pytest.raises(ModelRegistryError, match="authoritative registry"):
        legacy_config.get_task("expression")


def test_kernel_api_uses_only_authoritative_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "models.toml"
    _write_minimal_registry(registry_path)
    registry = ModelsConfig(registry_path)
    monkeypatch.setattr(models_loader, "_models_config", registry)

    assert [
        entry["model_identifier"] for entry in _resolve_model_set("expression")
    ] == ["primary-id", "backup-id"]
    assert _resolve_model_set("primary")[0]["model_identifier"] == "primary-id"
    with pytest.raises(ValueError, match=registry.snapshot.digest):
        _resolve_model_set("not-configured")
