"""Checks for the committed compact model-registry baseline."""

from __future__ import annotations

from pathlib import Path

from src.kernel.config.models_loader import ModelsConfig


EXPECTED_TASK_BUDGETS = {
    "core": 32000,
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


def test_router_tasks_are_cloud_first_and_keep_reasoning_budget() -> None:
    registry_path = Path(__file__).parents[2] / "config" / "models.toml.example"
    config = ModelsConfig(registry_path)

    for task_name in ("router", "router_context_projection"):
        entries = config.get_task(task_name)
        assert len(entries) >= 3
        assert all(entry["api_provider"] == "NexusAI" for entry in entries)
        assert all(entry["max_tokens"] >= 8192 for entry in entries)

    assert "qwen3-0.6b-router" not in config.tasks["router"]["models"]


def test_unverified_gpt_models_are_registered_but_not_automatic() -> None:
    registry_path = Path(__file__).parents[2] / "config" / "models.toml.example"
    config = ModelsConfig(registry_path)

    assert {"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"}.issubset(
        config.models
    )
    automatic_models = {
        model_name
        for task in config.tasks.values()
        for model_name in task["models"]
    }
    assert not any(name.startswith("gpt-5.6-") for name in automatic_models)
