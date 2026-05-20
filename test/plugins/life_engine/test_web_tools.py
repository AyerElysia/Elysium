"""life_engine web_tools 测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools.web_tools import (
    LifeEngineBrowserFetchTool,
    LifeEngineWebSearchTool,
    _pick_tavily_target,
    _resolve_tavily_trust_env,
    _sync_post_json,
    _tavily_selector,
)


@dataclass
class _DummyPlugin:
    config: LifeEngineConfig


def _make_plugin(tmp_path: Path) -> _DummyPlugin:
    cfg = LifeEngineConfig()
    cfg.settings.workspace_path = str(tmp_path)
    return _DummyPlugin(config=cfg)


class _FakeTavilyResponse:
    status_code = 200
    text = '{"results": []}'

    def json(self) -> dict:
        return {"results": [], "query": "test"}


def test_web_search_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """web_search 应返回 Tavily 结果并完成字段规范化。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"

    async def _fake_post(_plugin, endpoint: str, payload: dict, _timeout: int) -> dict:
        assert endpoint == "/search"
        assert payload["query"] == "最新 AI 新闻"
        return {
            "answer": "这里是摘要",
            "results": [
                {
                    "title": "T1",
                    "url": "https://example.com/a",
                    "content": "snippet-a",
                    "score": 0.91,
                    "published_date": "2026-04-09",
                }
            ],
        }

    monkeypatch.setattr("plugins.life_engine.tools.web_tools._tavily_post_json", _fake_post)

    tool = LifeEngineWebSearchTool(plugin=plugin)
    ok, data = asyncio.run(
        tool.execute(
            query="最新 AI 新闻",
            search_depth="advanced",
            topic="news",
            max_results=3,
            include_answer=True,
        )
    )

    assert ok is True
    assert data["action"] == "web_search"
    assert data["provider"] == "tavily"
    assert data["total_results"] == 1
    assert data["results"][0]["title"] == "T1"
    assert data["answer"] == "这里是摘要"


def test_web_search_requires_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 Tavily key 时应给出明确错误。"""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = ""

    tool = LifeEngineWebSearchTool(plugin=plugin)
    ok, data = asyncio.run(tool.execute(query="test query"))

    assert ok is False
    assert "Tavily API Key" in data["error"]


def test_web_search_does_not_use_env_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅设置环境变量时不应生效，必须从 toml 配置读取 key。"""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env-only")
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = ""

    tool = LifeEngineWebSearchTool(plugin=plugin)
    ok, data = asyncio.run(tool.execute(query="test query"))

    assert ok is False
    assert "config/plugins/life_engine/config.toml" in data["error"]


def test_sync_post_json_retries_direct_when_proxy_tls_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """系统代理 TLS EOF 时，应绕过代理直连重试一次。"""
    calls: list[bool] = []

    def _fake_post(*_args, trust_env: bool = True, **_kwargs) -> _FakeTavilyResponse:
        calls.append(trust_env)
        if trust_env:
            raise requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")
        return _FakeTavilyResponse()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr("plugins.life_engine.tools.web_tools._requests_post_json", _fake_post)

    data = _sync_post_json(
        "https://api.tavily.com/search",
        {"query": "test", "api_key": "tvly-test"},
        5,
    )

    assert data["query"] == "test"
    assert calls == [True, False]


def test_sync_post_json_skips_proxy_when_trust_env_disabled() -> None:
    """禁用 trust_env 后，应直接绕过系统代理。"""
    calls: list[bool] = []

    def _fake_post(*_args, trust_env: bool = True, **_kwargs) -> _FakeTavilyResponse:
        calls.append(trust_env)
        return _FakeTavilyResponse()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
        monkeypatch.setattr("plugins.life_engine.tools.web_tools._requests_post_json", _fake_post)

        data = _sync_post_json(
            "https://api.tavily.com/search",
            {"query": "test", "api_key": "tvly-test"},
            5,
            trust_env=False,
        )

    assert data["query"] == "test"
    assert calls == [False]


def test_resolve_tavily_trust_env_reads_config(tmp_path: Path) -> None:
    """应从 life_engine web 配置读取 trust_env。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.trust_env = False

    assert _resolve_tavily_trust_env(plugin) is False


def test_pick_tavily_target_supports_round_robin_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置多个 key/base_url 时应按轮询方式选择。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_keys = ["key-a", "key-b"]
    plugin.config.web.tavily_base_urls = [
        "https://api-1.example.com",
        "https://api-2.example.com",
        "https://api-3.example.com",
    ]
    monkeypatch.setattr(_tavily_selector, "_cursor", 0)

    picks = [_pick_tavily_target(plugin) for _ in range(5)]

    assert picks == [
        ("key-a", "https://api-1.example.com"),
        ("key-b", "https://api-2.example.com"),
        ("key-a", "https://api-3.example.com"),
        ("key-b", "https://api-1.example.com"),
        ("key-a", "https://api-2.example.com"),
    ]


def test_pick_tavily_target_keeps_legacy_single_value_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧的单 key / 单 base_url 配置仍应可正常工作。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "legacy-key"
    plugin.config.web.tavily_base_url = "https://legacy.example.com"
    monkeypatch.setattr(_tavily_selector, "_cursor", 0)

    first = _pick_tavily_target(plugin)
    second = _pick_tavily_target(plugin)

    assert first == ("legacy-key", "https://legacy.example.com")
    assert second == ("legacy-key", "https://legacy.example.com")


def test_web_tools_allow_default_and_life_chatter() -> None:
    """网络工具应允许 life_engine_internal、default_chatter、life_chatter 调用。"""
    assert "life_engine_internal" in LifeEngineWebSearchTool.chatter_allow
    assert "default_chatter" in LifeEngineWebSearchTool.chatter_allow
    assert "life_chatter" in LifeEngineWebSearchTool.chatter_allow
    assert "life_engine_internal" in LifeEngineBrowserFetchTool.chatter_allow
    assert "default_chatter" in LifeEngineBrowserFetchTool.chatter_allow
    assert "life_chatter" in LifeEngineBrowserFetchTool.chatter_allow


def test_web_search_rejects_conflicting_domains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_domains 与 exclude_domains 同时设置应被拒绝。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"

    tool = LifeEngineWebSearchTool(plugin=plugin)
    ok, data = asyncio.run(
        tool.execute(
            query="test",
            include_domains=["example.com"],
            exclude_domains=["foo.com"],
        )
    )

    assert ok is False
    assert "不能同时设置" in data["error"]


def test_browser_fetch_blocks_private_url(tmp_path: Path) -> None:
    """浏览工具应阻止访问本地/内网地址。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"

    tool = LifeEngineBrowserFetchTool(plugin=plugin)
    ok, data = asyncio.run(tool.execute(url="http://127.0.0.1:8080/admin"))

    assert ok is False
    assert "禁止访问本地或内网地址" in data["error"]


def test_browser_fetch_reads_file_url(tmp_path: Path) -> None:
    """浏览工具应兼容读取 workspace 内的 file:// 本地文件路径。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "name_promise.md").write_text("名字承诺内容", encoding="utf-8")

    tool = LifeEngineBrowserFetchTool(plugin=plugin)
    ok, data = asyncio.run(tool.execute(url="file://notes/name_promise.md"))

    assert ok is True
    assert data["provider"] == "local_file"
    assert data["action"] == "browser_fetch"
    assert "名字承诺内容" in data["content"]
    assert data["title"] == "name_promise.md"


def test_browser_fetch_rejects_outside_workspace_path(tmp_path: Path) -> None:
    """浏览工具仍应拒绝 workspace 外的本地路径。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"

    tool = LifeEngineBrowserFetchTool(plugin=plugin)
    ok, data = asyncio.run(tool.execute(url="file:///etc/passwd"))

    assert ok is False
    assert "超出工作空间范围" in data["error"]


def test_browser_fetch_success_and_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """browser_fetch 应提取内容并按 max_chars 截断。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"

    async def _fake_post(_plugin, endpoint: str, payload: dict, _timeout: int) -> dict:
        assert endpoint == "/extract"
        assert payload["urls"] == ["https://example.com/post"]
        return {
            "results": [
                {
                    "url": "https://example.com/post",
                    "title": "Example Post",
                    "content": "abcdefghijklmnopqrstuvwxyz",
                    "images": ["https://example.com/a.png"],
                }
            ]
        }

    monkeypatch.setattr("plugins.life_engine.tools.web_tools._tavily_post_json", _fake_post)

    tool = LifeEngineBrowserFetchTool(plugin=plugin)
    ok, data = asyncio.run(
        tool.execute(
            url="https://example.com/post",
            extract_depth="basic",
            max_chars=10,
            include_images=True,
        )
    )

    assert ok is True
    assert data["action"] == "browser_fetch"
    assert data["provider"] == "tavily"
    assert data["title"] == "Example Post"
    assert data["truncated"] is True
    assert len(data["content"]) == 10
    assert data["images"] == ["https://example.com/a.png"]


def test_web_search_handles_timeout_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """web_search 应正确处理 asyncio.TimeoutError 而非捕获裸 Exception。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"

    async def _fake_post_timeout(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(
        "plugins.life_engine.tools.web_tools._tavily_post_json", _fake_post_timeout
    )

    tool = LifeEngineWebSearchTool(plugin=plugin)
    ok, data = asyncio.run(tool.execute(query="测试超时"))

    assert ok is False
    assert "超时" in data["error"]


def test_web_search_handles_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """web_search 应正确处理 RuntimeError（Tavily API 错误）。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"

    async def _fake_post_runtime(*args, **kwargs):
        raise RuntimeError("API 限额已用完")

    monkeypatch.setattr(
        "plugins.life_engine.tools.web_tools._tavily_post_json", _fake_post_runtime
    )

    tool = LifeEngineWebSearchTool(plugin=plugin)
    ok, data = asyncio.run(tool.execute(query="测试 RuntimeError"))

    assert ok is False
    assert "API 限额已用完" in data["error"]


def test_browser_fetch_handles_timeout_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """browser_fetch 应正确处理 asyncio.TimeoutError。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"

    async def _fake_post_timeout(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(
        "plugins.life_engine.tools.web_tools._tavily_post_json", _fake_post_timeout
    )

    tool = LifeEngineBrowserFetchTool(plugin=plugin)
    ok, data = asyncio.run(tool.execute(url="https://example.com"))

    assert ok is False
    assert "超时" in data["error"]


def test_browser_fetch_handles_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """browser_fetch 应正确处理 OSError（网络错误）。"""
    plugin = _make_plugin(tmp_path)
    plugin.config.web.tavily_api_key = "tvly-test-key"

    async def _fake_post_oserror(*args, **kwargs):
        raise OSError("网络不可达")

    monkeypatch.setattr(
        "plugins.life_engine.tools.web_tools._tavily_post_json", _fake_post_oserror
    )

    tool = LifeEngineBrowserFetchTool(plugin=plugin)
    ok, data = asyncio.run(tool.execute(url="https://example.com"))

    assert ok is False
    assert "网络错误" in data["error"]
