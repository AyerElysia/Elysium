from __future__ import annotations

import textwrap

import pytest

from src.kernel.config.models_loader import (
    ModelsConfig,
    _reset_task_model_overrides_for_tests,
    clear_task_model_override,
    get_task_model_state,
    switch_task_model,
)


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "models.toml"
    path.write_text(
        textwrap.dedent(
            """
            [providers.local]
            base_url = "http://127.0.0.1:8000/v1"
            api_key = "sk-test"
            client_type = "openai"

            [models.fast]
            provider = "local"
            id = "fast"

            [models.safe]
            provider = "local"
            id = "safe"

            [models.unrelated]
            provider = "local"
            id = "unrelated"

[tasks.core]
models = ["fast", "safe"]
context_tokens = 2048
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    config = ModelsConfig(path)
    config.require_tasks({"core"})
    config.require_runtime_readiness()
    _reset_task_model_overrides_for_tests()
    try:
        yield config
    finally:
        _reset_task_model_overrides_for_tests()


def test_switch_promotes_candidate_for_new_model_sets(registry):
    in_flight_model_set = registry.get_task("core")
    assert in_flight_model_set[0]["routing_model_alias"] == "fast"

    override = switch_task_model("core", "safe", registry=registry)
    model_set = registry.get_task("core")

    assert in_flight_model_set[0]["routing_model_alias"] == "fast"
    assert override.task == "core"
    assert override.model == "safe"
    assert model_set[0]["routing_model_alias"] == "safe"
    assert [item["routing_model_alias"] for item in model_set] == ["safe", "fast"]
    assert model_set[0]["routing_snapshot"].endswith(f":hot-{override.generation}")


def test_switch_rejects_model_outside_task_candidates(registry):
    with pytest.raises(ValueError, match="不是任务 'core' 的候选模型"):
        switch_task_model("core", "unrelated", registry=registry)


def test_clear_restores_configured_priority(registry):
    switch_task_model("core", "safe", registry=registry)
    assert get_task_model_state("core", registry=registry)["active_model"] == "safe"

    assert clear_task_model_override("core") is True
    state = get_task_model_state("core", registry=registry)
    assert state["active_model"] == "fast"
    assert state["override_model"] is None
    assert registry.get_task("core")[0]["routing_model_alias"] == "fast"
