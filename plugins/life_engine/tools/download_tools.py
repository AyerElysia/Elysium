"""life_engine 文件下载工具。

提供从 URL 下载文件到 workspace 的能力。
"""

from __future__ import annotations

import mimetypes
import os
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote, urlparse

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ._utils import _get_workspace

logger = log_api.get_logger("life_engine.download_tools")

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_SIZE_MB = 200
_CHUNK_SIZE = 65536  # 64 KB


def _infer_filename(url: str, content_type: str | None) -> str:
    """从 URL 和 Content-Type 推断文件名。"""
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)

    # 清理非法字符
    name = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    if not name or name == "/":
        name = f"download_{int(time.time())}"

    # 如果没有扩展名，尝试从 Content-Type 补一个
    if "." not in Path(name).suffix and content_type:
        mime = content_type.split(";")[0].strip()
        ext = mimetypes.guess_extension(mime)
        if ext and ext not in (".ksh", ".bat"):
            name += ext

    return name


def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size //= 1024
    return f"{size:.1f} GB"


def _resolve_save_path(workspace: Path, save_path: str, filename: str) -> tuple[bool, Path | str]:
    """将用户指定的保存路径解析到 workspace 内的绝对路径。"""
    raw = save_path.strip()

    if not raw:
        target = workspace / "downloads" / filename
    else:
        candidate = Path(raw)
        if candidate.is_absolute():
            target = candidate
        else:
            target = workspace / candidate

    try:
        resolved = target.resolve()
    except Exception as exc:
        return False, f"路径解析失败: {exc}"

    # 如果路径以目录形式给出（已存在的目录，或者以 / 结尾），把文件名拼上去
    if resolved.is_dir() or str(raw).endswith("/"):
        resolved = resolved / filename

    # 安全检查：必须在 workspace 内
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return False, f"保存路径超出 workspace 范围: {resolved}（workspace: {workspace}）"

    return True, resolved


class LifeEngineDownloadTool(BaseTool):
    """从 URL 下载文件到 workspace。"""

    tool_name: str = "nucleus_download"
    tool_description: str = (
        "从指定 URL 下载文件，保存到 workspace 目录内。\n\n"
        "**典型用途：**\n"
        "- 下载参考图片、素材、模型文件（如 .naiv4vibe）\n"
        "- 下载数据文件、配置文件、归档包\n"
        "- 下载任何需要持久化到 workspace 的网络资源\n\n"
        "**save_path 说明：**\n"
        "- 留空 → 自动保存到 workspace/downloads/<推断文件名>\n"
        "- 相对路径（如 'vibes/my.naiv4vibe'）→ workspace/<路径>\n"
        "- 目录路径（如 'images/'）→ workspace/images/<推断文件名>\n"
        "- 绝对路径也支持，但必须在 workspace 内\n\n"
        "**返回：** 保存路径、文件大小、MIME 类型"
    )

    async def execute(
        self,
        url: Annotated[str, "要下载的文件 URL"],
        save_path: Annotated[
            str,
            "workspace 内的保存路径（相对路径或目录），留空则自动放到 downloads/ 下",
        ] = "",
        timeout_seconds: Annotated[int, "下载超时（秒），默认 60"] = _DEFAULT_TIMEOUT_SECONDS,
        max_size_mb: Annotated[int, "最大允许文件大小（MB），默认 200"] = _DEFAULT_MAX_SIZE_MB,
    ) -> tuple[bool, str | dict]:
        """下载文件到 workspace。"""
        try:
            import aiohttp
        except ImportError:
            return False, {"error": "缺少 aiohttp 依赖，无法执行下载"}

        url = str(url or "").strip()
        if not url:
            return False, {"error": "url 不能为空"}
        if not url.startswith(("http://", "https://")):
            return False, {"error": f"不支持的 URL 协议（仅支持 http/https）: {url}"}

        try:
            timeout_seconds = max(5, min(600, int(timeout_seconds)))
        except Exception:
            timeout_seconds = _DEFAULT_TIMEOUT_SECONDS

        try:
            max_size_mb = max(1, min(2048, int(max_size_mb)))
        except Exception:
            max_size_mb = _DEFAULT_MAX_SIZE_MB

        max_size_bytes = max_size_mb * 1024 * 1024
        workspace = _get_workspace(self.plugin)

        logger.info(f"[nucleus_download] url={url} save_path={save_path!r} timeout={timeout_seconds}s")

        started = time.perf_counter()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    allow_redirects=True,
                ) as resp:
                    if resp.status not in (200, 206):
                        return False, {
                            "error": f"HTTP {resp.status}",
                            "url": url,
                        }

                    content_type = resp.headers.get("Content-Type", "")
                    content_length = resp.headers.get("Content-Length")
                    if content_length:
                        declared_size = int(content_length)
                        if declared_size > max_size_bytes:
                            return False, {
                                "error": (
                                    f"文件大小 {_format_size(declared_size)} 超过限制 "
                                    f"{max_size_mb} MB"
                                ),
                                "url": url,
                            }

                    # 推断文件名
                    filename = _infer_filename(str(resp.url), content_type or None)

                    # 解析保存路径
                    ok, resolved = _resolve_save_path(workspace, save_path, filename)
                    if not ok:
                        return False, {"error": str(resolved), "url": url}

                    save_file: Path = resolved  # type: ignore[assignment]
                    save_file.parent.mkdir(parents=True, exist_ok=True)

                    # 流式写入
                    downloaded = 0
                    with save_file.open("wb") as fh:
                        async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                            downloaded += len(chunk)
                            if downloaded > max_size_bytes:
                                fh.close()
                                try:
                                    save_file.unlink()
                                except Exception:
                                    pass
                                return False, {
                                    "error": (
                                        f"下载过程中文件超过限制 {max_size_mb} MB，已中止"
                                    ),
                                    "url": url,
                                }
                            fh.write(chunk)

        except TimeoutError:
            return False, {"error": f"下载超时（{timeout_seconds}秒）", "url": url}
        except Exception as exc:
            return False, {"error": f"下载失败: {exc}", "url": url}

        duration_ms = int((time.perf_counter() - started) * 1000)
        rel_path = str(save_file.relative_to(workspace))

        logger.info(
            f"[nucleus_download] 完成: {rel_path} "
            f"size={_format_size(downloaded)} duration={duration_ms}ms"
        )

        return True, {
            "url": url,
            "saved_to": str(save_file),
            "workspace_relative": rel_path,
            "size": _format_size(downloaded),
            "size_bytes": downloaded,
            "content_type": content_type or "unknown",
            "duration_ms": duration_ms,
        }


DOWNLOAD_TOOLS = [
    LifeEngineDownloadTool,
]

__all__ = [
    "DOWNLOAD_TOOLS",
    "LifeEngineDownloadTool",
]
