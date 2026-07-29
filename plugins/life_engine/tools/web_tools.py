"""life_engine 网络搜索与浏览工具。

提供两类能力（均基于 Tavily API）：
1. nucleus_web_search：联网检索最新信息
2. nucleus_browser_fetch：像"浏览器打开页面"一样提取网页正文
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Annotated, Any, Literal

import requests

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..core.config import LifeEngineConfig
from ._utils import _get_workspace
from .results import truncate_text as _truncate_text

logger = log_api.get_logger("life_engine.web_tools")

_DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"
_DEFAULT_SEARCH_TIMEOUT_SECONDS = 30
_DEFAULT_EXTRACT_TIMEOUT_SECONDS = 60
_DEFAULT_SEARCH_MAX_RESULTS = 5
_DEFAULT_FETCH_MAX_CHARS = 12000
_MAX_RESULTS = 20
_MAX_FETCH_CHARS = 50000
_MAX_RAW_CONTENT_CHARS = 4000

_BLOCKED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
}


class TavilyTargetSelector:
    """Tavily API 目标选择器（线程安全）。"""

    def __init__(self) -> None:
        """初始化选择器。"""
        self._lock = threading.Lock()
        self._cursor = 0

    def next_target(
        self, keys: list[str], base_urls: list[str]
    ) -> tuple[str, str]:
        """选择下一个 API key 和 base URL。

        Args:
            keys: API key 列表
            base_urls: Base URL 列表

        Returns:
            (api_key, base_url) 元组
        """
        with self._lock:
            cursor = self._cursor
            self._cursor += 1
            api_key = keys[cursor % len(keys)]
            base_url = base_urls[cursor % len(base_urls)]
            return api_key, base_url


_tavily_selector = TavilyTargetSelector()


def _get_life_config(plugin: Any) -> LifeEngineConfig | None:
    cfg = getattr(plugin, "config", None)
    if isinstance(cfg, LifeEngineConfig):
        return cfg
    return None


def _clean_string_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """清洗字符串列表，移除空白项。"""
    if not values:
        return []
    cleaned: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            cleaned.append(text)
    return cleaned


def _resolve_tavily_api_keys(plugin: Any) -> list[str]:
    cfg = _get_life_config(plugin)
    if cfg is not None:
        keys = _clean_string_list(getattr(cfg.web, "tavily_api_keys", []))
        if keys:
            return keys
        key = str(cfg.web.tavily_api_key or "").strip()
        if key:
            return [key]
    return []


def _resolve_tavily_base_urls(plugin: Any) -> list[str]:
    cfg = _get_life_config(plugin)
    if cfg is not None:
        base_urls = _clean_string_list(getattr(cfg.web, "tavily_base_urls", []))
        if base_urls:
            return base_urls
        base = str(cfg.web.tavily_base_url or "").strip()
        if base:
            return [base]
    env_bases = _clean_string_list(
        [part.strip() for part in str(os.getenv("TAVILY_BASE_URLS") or "").split(",")]
    )
    if env_bases:
        return env_bases
    env_base = str(os.getenv("TAVILY_BASE_URL") or "").strip()
    if env_base:
        return [env_base]
    return [_DEFAULT_TAVILY_BASE_URL]


def _pick_tavily_target(plugin: Any) -> tuple[str, str]:
    """选择本次 Tavily 请求要使用的 key/base_url。

    兼容旧配置（单 key / 单 base_url），也支持多 key / 多 base_url 轮询。
    当 key 与 base_url 数量不同，二者分别按自己的长度循环。
    """
    keys = _resolve_tavily_api_keys(plugin)
    if not keys:
        raise RuntimeError(
            "未配置 Tavily API Key。请在 config/plugins/life_engine/config.toml "
            "中设置 [web].tavily_api_key，或 [web].tavily_api_keys。"
        )

    base_urls = _resolve_tavily_base_urls(plugin)
    if not base_urls:
        base_urls = [_DEFAULT_TAVILY_BASE_URL]

    return _tavily_selector.next_target(keys, base_urls)


def _resolve_search_timeout(plugin: Any) -> int:
    cfg = _get_life_config(plugin)
    if cfg is not None:
        return max(1, min(120, int(cfg.web.search_timeout_seconds)))
    return _DEFAULT_SEARCH_TIMEOUT_SECONDS


def _resolve_extract_timeout(plugin: Any) -> int:
    cfg = _get_life_config(plugin)
    if cfg is not None:
        return max(1, min(180, int(cfg.web.extract_timeout_seconds)))
    return _DEFAULT_EXTRACT_TIMEOUT_SECONDS


def _resolve_default_search_max_results(plugin: Any) -> int:
    cfg = _get_life_config(plugin)
    if cfg is not None:
        return max(1, min(_MAX_RESULTS, int(cfg.web.default_search_max_results)))
    return _DEFAULT_SEARCH_MAX_RESULTS


def _resolve_tavily_trust_env(plugin: Any) -> bool:
    cfg = _get_life_config(plugin)
    if cfg is not None:
        return bool(getattr(cfg.web, "trust_env", True))
    return True


def _resolve_default_fetch_max_chars(plugin: Any) -> int:
    cfg = _get_life_config(plugin)
    if cfg is not None:
        return max(500, min(_MAX_FETCH_CHARS, int(cfg.web.default_fetch_max_chars)))
    return _DEFAULT_FETCH_MAX_CHARS


def _resolve_endpoint(base_url: str, path: str) -> str:
    base = base_url.strip() if base_url else _DEFAULT_TAVILY_BASE_URL
    if not base:
        base = _DEFAULT_TAVILY_BASE_URL
    try:
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in ("http", "https"):
            logger.warning(f"Invalid URL scheme in base: {base}, using default")
            base = _DEFAULT_TAVILY_BASE_URL
    except ValueError as e:
        logger.warning(f"URL parse failed for base '{base}': {e}, using default")
        base = _DEFAULT_TAVILY_BASE_URL
    return base.rstrip("/") + "/" + path.lstrip("/")


def _resolve_local_path(plugin: Any, raw_path: str) -> tuple[bool, Path | str]:
    """解析本地文件路径，只允许 workspace 内的文件。"""
    workspace = _get_workspace(plugin)
    candidate = str(raw_path or "").strip()
    if not candidate:
        return False, "路径不能为空"

    if candidate.startswith("file://"):
        parsed = urllib.parse.urlparse(candidate)
        if parsed.netloc and parsed.path:
            candidate = f"{parsed.netloc}{parsed.path}"
        elif parsed.netloc:
            candidate = parsed.netloc
        else:
            candidate = parsed.path

    try:
        target = Path(candidate)
        if not target.is_absolute():
            target = (workspace / candidate).resolve()
        else:
            target = target.resolve()
    except Exception as exc:  # noqa: BLE001
        return False, f"本地路径解析失败: {exc}"

    try:
        target.relative_to(workspace)
    except ValueError:
        return False, f"本地路径超出工作空间范围。工作空间: {workspace}"

    return True, target


def _is_blocked_host(hostname: str) -> bool:
    host = hostname.strip().strip("[]").lower()
    if not host:
        return True
    if host in _BLOCKED_HOSTS or host.endswith(".local"):
        return True

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False

    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_public_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as e:
        logger.debug(f"URL parse failed: {e}")
        return False, "URL 格式无效"

    if parsed.scheme not in ("http", "https"):
        return False, "仅支持 http/https URL"
    if not parsed.netloc:
        return False, "URL 缺少主机名"
    if not parsed.hostname:
        return False, "URL 主机名无效"
    if _is_blocked_host(parsed.hostname):
        return False, "出于安全原因，禁止访问本地或内网地址"
    return True, ""


def _has_proxy_env() -> bool:
    return any(
        os.getenv(name)
        for name in (
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
    )


def _is_retryable_proxy_tls_error(exc: requests.exceptions.RequestException) -> bool:
    if not _has_proxy_env():
        return False
    message = str(exc)
    return (
        isinstance(exc, requests.exceptions.ProxyError)
        or "UNEXPECTED_EOF_WHILE_READING" in message
        or "EOF occurred in violation of protocol" in message
        or "SSL_ERROR_SYSCALL" in message
    )


def _requests_post_json(
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    *,
    trust_env: bool = True,
) -> requests.Response:
    kwargs = {
        "url": url,
        "json": payload,
        "headers": {
            "Accept": "application/json",
            "User-Agent": "life_engine/3.3.0",
        },
        "timeout": timeout_seconds,
    }
    if trust_env:
        return requests.post(**kwargs)

    session = requests.Session()
    session.trust_env = False
    try:
        return session.post(**kwargs)
    finally:
        session.close()


def _sync_post_json(
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    trust_env: bool = True,
) -> dict[str, Any]:
    # 使用 requests 替代 urllib 以获得更稳健的代理 TLS 处理能力
    if not trust_env:
        try:
            resp = _requests_post_json(url, payload, timeout_seconds, trust_env=False)
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Tavily 网络请求失败: {exc}") from exc
    else:
        try:
            # 默认沿用系统代理；如果本地代理对 Tavily TLS 握手提前断开，再直连重试一次。
            resp = _requests_post_json(url, payload, timeout_seconds, trust_env=True)
        except requests.exceptions.RequestException as exc:
            if not _is_retryable_proxy_tls_error(exc):
                raise RuntimeError(f"Tavily 网络请求失败: {exc}") from exc
            logger.warning(
                "Tavily 经系统代理请求失败，尝试绕过代理直连重试: "
                f"{type(exc).__name__}: {exc}"
            )
            try:
                resp = _requests_post_json(url, payload, timeout_seconds, trust_env=False)
            except requests.exceptions.RequestException as direct_exc:
                raise RuntimeError(
                    f"Tavily 网络请求失败: proxy={exc}; direct={direct_exc}"
                ) from direct_exc

    try:
        status = resp.status_code
        raw = resp.text
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Tavily 请求异常: {exc}") from exc

    if status >= 400:
        raise RuntimeError(f"Tavily 请求失败（HTTP {status}）: {raw[:500]}")
    if not isinstance(data, dict):
        raise RuntimeError("Tavily 返回格式异常：顶层不是对象")
    return data


async def _tavily_post_json(
    plugin: Any,
    endpoint: str,
    payload: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    api_key, base_url = _pick_tavily_target(plugin)
    body = dict(payload)
    body["api_key"] = api_key
    url = _resolve_endpoint(base_url, endpoint)
    return await asyncio.to_thread(
        _sync_post_json,
        url,
        body,
        timeout_seconds,
        _resolve_tavily_trust_env(plugin),
    )


class LifeEngineWebSearchTool(BaseTool):
    """网络搜索工具（Tavily）。"""

    tool_name: str = "nucleus_web_search"
    tool_description: str = (
        "联网搜索最新信息（基于 Tavily Search API）。\n\n"
        "**何时使用：**\n"
        "- ✓ 需要最新信息（新闻、近期动态、实时变化）\n"
        "- ✓ 需要跨站点快速收集多个来源\n"
        "- ✓ 需要按时间范围或域名过滤结果\n\n"
        "**何时不用：**\n"
        "- ✗ 已经有明确 URL，想直接读网页正文 → 用 nucleus_browser_fetch\n"
        "- ✗ 想回忆自己写过的内容 → 用 nucleus_search_memory / nucleus_grep_file\n\n"
        "**注意：** 这是外部网络信息，可能有偏差，关键事实请交叉核验。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "default_chatter", "life_chatter"]

    async def execute(
        self,
        query: Annotated[str, "搜索查询语句"],
        search_depth: Annotated[Literal["basic", "advanced"], "搜索深度：basic/advanced"] = "basic",
        topic: Annotated[Literal["general", "news", "finance"], "主题类型"] = "general",
        max_results: Annotated[int, "返回数量（1-20）"] = 0,
        include_answer: Annotated[bool, "是否包含 Tavily 生成的答案摘要"] = False,
        time_range: Annotated[Literal["day", "week", "month", "year"] | None, "时间范围过滤（None 表示不限）"] = None,
        include_domains: Annotated[list[str] | None, "仅包含这些域名"] = None,
        exclude_domains: Annotated[list[str] | None, "排除这些域名"] = None,
        include_raw_content: Annotated[bool, "是否附带较长原文片段（会截断）"] = False,
    ) -> tuple[bool, dict[str, Any]]:
        q = str(query or "").strip()
        if not q:
            return False, {"error": "query 不能为空"}

        if search_depth not in ("basic", "advanced"):
            return False, {"error": "search_depth 必须是 basic 或 advanced"}
        if topic not in ("general", "news", "finance"):
            return False, {"error": "topic 必须是 general/news/finance"}
        normalized_time_range = (
            None
            if time_range is None or not str(time_range).strip()
            else str(time_range).strip()
        )
        if normalized_time_range not in (None, "day", "week", "month", "year"):
            return False, {"error": "time_range 必须是 day/week/month/year 或不传"}
        if include_domains and exclude_domains:
            return False, {"error": "include_domains 和 exclude_domains 不能同时设置"}

        resolved_max_results = (
            _resolve_default_search_max_results(self.plugin)
            if max_results <= 0
            else max(1, min(_MAX_RESULTS, int(max_results)))
        )

        payload: dict[str, Any] = {
            "query": q,
            "max_results": resolved_max_results,
            "search_depth": search_depth,
            "topic": topic,
            "include_answer": bool(include_answer),
        }
        if normalized_time_range is not None:
            payload["time_range"] = normalized_time_range
        if include_domains:
            payload["include_domains"] = [d.strip() for d in include_domains if str(d).strip()]
        if exclude_domains:
            payload["exclude_domains"] = [d.strip() for d in exclude_domains if str(d).strip()]

        try:
            response = await _tavily_post_json(
                self.plugin,
                "/search",
                payload,
                _resolve_search_timeout(self.plugin),
            )
        except RuntimeError as exc:
            logger.error(f"nucleus_web_search 执行失败: {exc}")
            return False, {"error": str(exc)}
        except asyncio.TimeoutError:
            logger.error("nucleus_web_search 请求超时")
            return False, {"error": "搜索请求超时"}
        except OSError as exc:
            logger.error(f"nucleus_web_search 网络错误: {exc}")
            return False, {"error": f"网络错误: {exc}"}

        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raw_results = []

        results: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            result_item: dict[str, Any] = {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or ""),
            }
            score = item.get("score")
            if isinstance(score, (int, float)):
                result_item["score"] = round(float(score), 4)
            published = item.get("published_date")
            if isinstance(published, str) and published.strip():
                result_item["published"] = published.strip()
            if include_raw_content and isinstance(item.get("raw_content"), str):
                raw_content, _ = _truncate_text(str(item.get("raw_content")), _MAX_RAW_CONTENT_CHARS)
                result_item["raw_content"] = raw_content
            results.append(result_item)

        output: dict[str, Any] = {
            "action": "web_search",
            "provider": "tavily",
            "query": q,
            "search_depth": search_depth,
            "topic": topic,
            "total_results": len(results),
            "results": results,
        }

        answer = response.get("answer")
        if isinstance(answer, str) and answer.strip():
            output["answer"] = answer.strip()

        return True, output


class LifeEngineBrowserFetchTool(BaseTool):
    """网页浏览/提取工具（Tavily Extract）。"""

    tool_name: str = "nucleus_browser_fetch"
    tool_description: str = (
        "打开网页并提取可读正文（基于 Tavily Extract API）。\n\n"
        "**何时使用：**\n"
        "- ✓ 手上已经有 URL，想读取网页正文\n"
        "- ✓ 普通抓取拿不到内容，需要更稳的网页提取能力\n"
        "- ✓ 需要从页面中提炼信息用于后续思考或记录\n\n"
        "**何时不用：**\n"
        "- ✗ 还没有 URL，只是想先找资料 → 用 nucleus_web_search\n"
        "- ✗ 想处理本地文件内容 → 优先用 read/grep/memory 工具\n"
        "- ✗ 本地文件不是网页，但本工具也兼容 workspace 内的本地路径读取\n\n"
        "**安全约束：** 公开网页仅允许 http/https；本地路径仅允许 workspace 内文件。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "default_chatter", "life_chatter"]

    async def execute(
        self,
        url: Annotated[str, "目标网页 URL（http/https）"],
        extract_depth: Annotated[Literal["basic", "advanced"], "提取深度：basic/advanced"] = "basic",
        query: Annotated[str, "可选：按该问题重排提取内容"] = "",
        chunks_per_source: Annotated[int, "每个来源的分块数（1-5，需配合 query）"] = 0,
        include_images: Annotated[bool, "是否返回图片 URL"] = False,
        max_chars: Annotated[int, "正文最大返回字符数（500-50000）"] = 0,
    ) -> tuple[bool, dict[str, Any]]:
        target_url = str(url or "").strip()
        if not target_url:
            return False, {"error": "url 不能为空"}

        resolved_max_chars = (
            _resolve_default_fetch_max_chars(self.plugin)
            if max_chars <= 0
            else max(1, min(_MAX_FETCH_CHARS, int(max_chars)))
        )

        parsed = urllib.parse.urlparse(target_url)
        local_like = parsed.scheme != "http" and parsed.scheme != "https"

        if local_like:
            ok, resolved = _resolve_local_path(self.plugin, target_url)
            if not ok:
                return False, {"error": str(resolved)}
            target_path = resolved
            try:
                raw_content = target_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return False, {"error": "文件编码错误，请尝试其他编码"}
            except Exception as exc:  # noqa: BLE001
                logger.error(f"nucleus_browser_fetch 读取本地文件失败: {exc}")
                return False, {"error": f"读取本地文件失败: {exc}"}

            truncated_content, truncated = _truncate_text(raw_content, resolved_max_chars)
            return True, {
                "action": "browser_fetch",
                "provider": "local_file",
                "url": target_url,
                "result_url": str(target_path),
                "extract_depth": extract_depth,
                "content": truncated_content,
                "content_length": len(truncated_content),
                "truncated": truncated,
                "title": target_path.name,
            }

        ok, err = _validate_public_url(target_url)
        if not ok:
            return False, {"error": err}

        if extract_depth not in ("basic", "advanced"):
            return False, {"error": "extract_depth 必须是 basic 或 advanced"}

        q = str(query or "").strip()
        if chunks_per_source > 0 and not q:
            return False, {"error": "设置 chunks_per_source 时必须提供 query"}
        if chunks_per_source < 0:
            return False, {"error": "chunks_per_source 不能为负数"}
        if chunks_per_source > 5:
            return False, {"error": "chunks_per_source 不能超过 5"}

        resolved_max_chars = (
            _resolve_default_fetch_max_chars(self.plugin)
            if max_chars <= 0
            else max(1, min(_MAX_FETCH_CHARS, int(max_chars)))
        )

        payload: dict[str, Any] = {
            "urls": [target_url],
            "extract_depth": extract_depth,
            "include_images": bool(include_images),
        }
        if q:
            payload["query"] = q
        if chunks_per_source > 0:
            payload["chunks_per_source"] = int(chunks_per_source)

        try:
            response = await _tavily_post_json(
                self.plugin,
                "/extract",
                payload,
                _resolve_extract_timeout(self.plugin),
            )
        except RuntimeError as exc:
            logger.error(f"nucleus_browser_fetch 执行失败: {exc}")
            return False, {"error": str(exc)}
        except asyncio.TimeoutError:
            logger.error("nucleus_browser_fetch 请求超时")
            return False, {"error": "网页提取请求超时"}
        except OSError as exc:
            logger.error(f"nucleus_browser_fetch 网络错误: {exc}")
            return False, {"error": f"网络错误: {exc}"}

        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raw_results = []

        selected: dict[str, Any] | None = None
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            if str(item.get("url") or "").strip() == target_url:
                selected = item
                break
            if selected is None:
                selected = item

        if selected is None:
            failed_results = response.get("failed_results")
            return False, {
                "error": "未提取到可用网页内容",
                "failed_results": failed_results if isinstance(failed_results, list) else [],
            }

        title = str(selected.get("title") or "")
        content = str(selected.get("content") or "")
        raw_content = str(selected.get("raw_content") or "")
        chosen_content = content if content.strip() else raw_content
        truncated_content, truncated = _truncate_text(chosen_content, resolved_max_chars)

        result: dict[str, Any] = {
            "action": "browser_fetch",
            "provider": "tavily",
            "url": target_url,
            "result_url": str(selected.get("url") or target_url),
            "extract_depth": extract_depth,
            "content": truncated_content,
            "content_length": len(truncated_content),
            "truncated": truncated,
        }
        if title.strip():
            result["title"] = title.strip()
        if isinstance(selected.get("images"), list):
            result["images"] = [str(v) for v in selected["images"] if str(v).strip()]

        failed_results = response.get("failed_results")
        if isinstance(failed_results, list) and failed_results:
            result["failed_results"] = failed_results

        return True, result


WEB_TOOLS = [
    LifeEngineWebSearchTool,
    LifeEngineBrowserFetchTool,
]


class LifeEngineBatchFetchTool(BaseTool):
    """批量网页提取工具——一次提取多个 URL 的正文。"""

    tool_name: str = "nucleus_batch_fetch"
    tool_description: str = (
        "批量提取多个网页的可读正文（基于 Tavily Extract API，单次最多 10 个 URL）。\n\n"
        "**适用场景：**\n"
        "- 搜索后需要读取多个结果的详细内容\n"
        "- 对比多个来源的信息\n"
        "- 研究型任务需要广泛阅读\n\n"
        "**注意：** 每个 URL 的正文会被截断到 max_chars_per_url。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "default_chatter", "life_chatter"]

    async def execute(
        self,
        urls: Annotated[list[str], "要提取的网页 URL 列表（最多 10 个）"],
        extract_depth: Annotated[Literal["basic", "advanced"], "提取深度"] = "basic",
        query: Annotated[str, "可选：按该问题重排提取内容"] = "",
        max_chars_per_url: Annotated[int, "每个 URL 正文最大字符数（500-20000）"] = 0,
    ) -> tuple[bool, dict[str, Any]]:
        url_list = [str(u or "").strip() for u in (urls or []) if str(u or "").strip()]
        if not url_list:
            return False, {"error": "urls 不能为空"}
        if len(url_list) > 10:
            return False, {"error": f"单次最多 10 个 URL，收到 {len(url_list)} 个"}

        # 验证所有 URL
        valid_urls: list[str] = []
        errors: list[str] = []
        for u in url_list:
            ok, err = _validate_public_url(u)
            if ok:
                valid_urls.append(u)
            else:
                errors.append(f"{u}: {err}")

        if not valid_urls:
            return False, {"error": "无有效 URL", "details": errors}

        resolved_max_chars = (
            min(20000, max(500, int(max_chars_per_url)))
            if max_chars_per_url > 0
            else 8000
        )

        payload: dict[str, Any] = {
            "urls": valid_urls,
            "extract_depth": extract_depth,
            "include_images": False,
        }
        q = str(query or "").strip()
        if q:
            payload["query"] = q

        try:
            response = await _tavily_post_json(
                self.plugin,
                "/extract",
                payload,
                _resolve_extract_timeout(self.plugin),
            )
        except (RuntimeError, asyncio.TimeoutError, OSError) as exc:
            return False, {"error": f"批量提取失败: {exc}"}

        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raw_results = []

        pages: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or item.get("raw_content") or "")
            truncated, was_truncated = _truncate_text(content, resolved_max_chars)
            pages.append({
                "url": str(item.get("url") or ""),
                "title": str(item.get("title") or ""),
                "content": truncated,
                "content_length": len(truncated),
                "truncated": was_truncated,
            })

        failed = response.get("failed_results")
        return True, {
            "action": "batch_fetch",
            "provider": "tavily",
            "requested": len(valid_urls),
            "succeeded": len(pages),
            "pages": pages,
            "failed_results": failed if isinstance(failed, list) else [],
            "url_errors": errors,
        }


class LifeEngineDeepResearchTool(BaseTool):
    """深度研究工具——多查询搜索 + 批量提取 + 结构化汇总。"""

    tool_name: str = "nucleus_deep_research"
    tool_description: str = (
        "深度研究工具：对一个主题执行多角度搜索，提取关键页面正文，"
        "返回结构化的研究材料包。\n\n"
        "**工作流程：**\n"
        "1. 用你提供的多个子查询并行搜索\n"
        "2. 从搜索结果中选取最相关的页面\n"
        "3. 批量提取这些页面的正文\n"
        "4. 返回：搜索结果汇总 + 页面正文 + 来源列表\n\n"
        "**适用场景：**\n"
        "- 调研一个复杂主题（技术选型、市场分析、学术综述）\n"
        "- 需要多源交叉验证的事实核查\n"
        "- 需要广泛阅读后综合的研究报告\n\n"
        "**注意：** 本工具只收集和结构化原始材料，不做 LLM 综合。"
        "综合结论由你（调用者）基于返回的材料自行判断。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "default_chatter", "life_chatter"]

    async def execute(
        self,
        queries: Annotated[
            list[str],
            "搜索子查询列表（2-6 个）。每个查询覆盖主题的一个角度。",
        ],
        search_depth: Annotated[Literal["basic", "advanced"], "搜索深度"] = "advanced",
        max_results_per_query: Annotated[int, "每个查询返回的搜索结果数（1-10）"] = 5,
        extract_top_n: Annotated[int, "从所有结果中选取 top N 个页面提取正文（0=不提取）"] = 3,
        max_chars_per_page: Annotated[int, "每个提取页面的最大字符数"] = 6000,
        time_range: Annotated[Literal["day", "week", "month", "year"] | None, "时间范围"] = None,
    ) -> tuple[bool, dict[str, Any]]:
        query_list = [str(q or "").strip() for q in (queries or []) if str(q or "").strip()]
        if not query_list:
            return False, {"error": "queries 不能为空"}
        if len(query_list) > 6:
            return False, {"error": f"最多 6 个子查询，收到 {len(query_list)} 个"}

        resolved_max_results = max(1, min(10, int(max_results_per_query)))
        resolved_extract_top = max(0, min(10, int(extract_top_n)))
        resolved_max_chars = max(1000, min(20000, int(max_chars_per_page))) if max_chars_per_page > 0 else 6000

        # Phase 1: 并行搜索所有子查询
        search_tasks = []
        for q in query_list:
            payload: dict[str, Any] = {
                "query": q,
                "max_results": resolved_max_results,
                "search_depth": search_depth,
                "topic": "general",
                "include_answer": False,
            }
            if time_range and str(time_range).strip() in ("day", "week", "month", "year"):
                payload["time_range"] = str(time_range).strip()
            search_tasks.append(
                _tavily_post_json(self.plugin, "/search", payload, _resolve_search_timeout(self.plugin))
            )

        try:
            search_responses = await asyncio.gather(*search_tasks, return_exceptions=True)
        except Exception as exc:
            return False, {"error": f"搜索阶段异常: {exc}"}

        # 汇总搜索结果
        all_results: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        search_summaries: list[dict[str, Any]] = []

        for i, resp in enumerate(search_responses):
            if isinstance(resp, Exception):
                search_summaries.append({"query": query_list[i], "error": str(resp)})
                continue

            raw_results = resp.get("results") if isinstance(resp, dict) else []
            if not isinstance(raw_results, list):
                raw_results = []

            query_results: list[dict[str, Any]] = []
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                entry = {
                    "title": str(item.get("title") or ""),
                    "url": url,
                    "snippet": str(item.get("content") or "")[:500],
                    "score": float(item.get("score", 0) or 0),
                    "source_query": query_list[i],
                }
                all_results.append(entry)
                query_results.append(entry)

            search_summaries.append({
                "query": query_list[i],
                "result_count": len(query_results),
            })

        # 按相关性排序
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Phase 2: 提取 top N 页面正文
        pages: list[dict[str, Any]] = []
        if resolved_extract_top > 0 and all_results:
            top_urls = [r["url"] for r in all_results[:resolved_extract_top]]
            extract_payload: dict[str, Any] = {
                "urls": top_urls,
                "extract_depth": "basic",
                "include_images": False,
            }
            try:
                extract_resp = await _tavily_post_json(
                    self.plugin, "/extract", extract_payload,
                    _resolve_extract_timeout(self.plugin),
                )
                raw_pages = extract_resp.get("results") if isinstance(extract_resp, dict) else []
                if isinstance(raw_pages, list):
                    for item in raw_pages:
                        if not isinstance(item, dict):
                            continue
                        content = str(item.get("content") or item.get("raw_content") or "")
                        truncated, _ = _truncate_text(content, resolved_max_chars)
                        pages.append({
                            "url": str(item.get("url") or ""),
                            "title": str(item.get("title") or ""),
                            "content": truncated,
                            "content_length": len(truncated),
                        })
            except Exception as exc:
                logger.warning(f"deep_research 提取阶段失败: {exc}")

        return True, {
            "action": "deep_research",
            "provider": "tavily",
            "queries": query_list,
            "search_summaries": search_summaries,
            "total_unique_results": len(all_results),
            "search_results": all_results[:20],  # 最多返回 20 条
            "extracted_pages": pages,
            "extracted_count": len(pages),
        }


WEB_TOOLS = [
    LifeEngineWebSearchTool,
    LifeEngineBrowserFetchTool,
    LifeEngineBatchFetchTool,
    LifeEngineDeepResearchTool,
]
