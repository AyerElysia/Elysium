"""life_engine 文件下载工具。

提供从 URL 下载文件到 workspace 的能力。
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import stat
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote, urlparse
from weakref import WeakKeyDictionary

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..storage.subject_workspace import subject_path_from_workspace_relative
from ._utils import _get_workspace
from .file_tools import (
    _guard_workspace_mutation,
    _plugin_life_service,
    _standing_prompt_structural_error,
)

logger = log_api.get_logger("life_engine.download_tools")

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_SIZE_MB = 200
_CHUNK_SIZE = 65536  # 64 KB
_IO_BUDGETS: WeakKeyDictionary = WeakKeyDictionary()


class DownloadSafetyError(RuntimeError):
    """A mechanical download boundary failed; no subject semantics are inferred."""


def _safe_url(url: str) -> str:
    """Keep endpoint identity without credentials, query strings, or fragments."""
    parsed = urlparse(url)
    authority = parsed.netloc.rsplit("@", 1)[-1]
    return f"{parsed.scheme}://{authority}{parsed.path}"


async def _owned_io(callback: Any, *args: Any) -> Any:
    """Bound disk workers and drain each owned worker before cancellation escapes."""
    loop = asyncio.get_running_loop()
    budget = _IO_BUDGETS.setdefault(loop, asyncio.Semaphore(4))
    async with budget:
        task = asyncio.create_task(
            asyncio.to_thread(callback, *args), name="nucleus-download-owned-io"
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 - cancellation must drain arbitrary disk errors
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise


def _check_path_components(workspace: Path, target: Path) -> None:
    """Reject symlinks (including dangling ones) without resolving through them."""
    current = workspace
    for part in target.relative_to(workspace).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise DownloadSafetyError("DownloadSymlinkPathRejected")
        if current != target and not stat.S_ISDIR(info.st_mode):
            raise DownloadSafetyError("DownloadParentNotDirectory")


async def _check_download_target(plugin: Any, workspace: Path, target: Path) -> None:
    """Downloads are external bytes, never an authority-document write path."""
    _check_path_components(workspace, target)
    parts = target.relative_to(workspace).parts
    candidates = [
        workspace.joinpath(*parts[:index]) for index in range(1, len(parts) + 1)
    ]
    for candidate in candidates:
        candidate_relative = candidate.relative_to(workspace).as_posix()
        ok, guarded = _guard_workspace_mutation(plugin, candidate_relative)
        if not ok:
            raise DownloadSafetyError(str(guarded))
        if Path(guarded) != candidate:
            raise DownloadSafetyError("DownloadPathChanged")
        structural = _standing_prompt_structural_error(plugin, candidate)
        if structural:
            raise DownloadSafetyError(structural)
        if candidate_relative in {
            "notes",
            "diaries",
        } or subject_path_from_workspace_relative(candidate_relative):
            raise DownloadSafetyError("ExternalDownloadCannotWriteSubjectDocument")
    service = _plugin_life_service(plugin)
    if service is None:
        raise DownloadSafetyError("SubjectDocumentRegistryUnavailable")
    store = getattr(service, "_subject_document_store", None)
    getter = getattr(store, "get_head", None)
    if store is not None or bool(
        getattr(service, "_selectable_storage_enabled", False)
    ):
        if not callable(getter):
            raise DownloadSafetyError("SubjectDocumentRegistryUnavailable")
        try:
            for candidate in candidates:
                candidate_relative = candidate.relative_to(workspace).as_posix()
                head = await getter(f"life_engine_workspace/{candidate_relative}")
                if head is not None:
                    raise DownloadSafetyError(
                        "ExternalDownloadCannotWriteRegisteredDocument"
                    )
            binding_getter = getattr(store, "get_path_binding", None)
            if not callable(binding_getter):
                raise DownloadSafetyError("SubjectDocumentBindingRegistryUnavailable")
            relative = target.relative_to(workspace).as_posix()
            if await binding_getter(f"life_engine_workspace/{relative}") is not None:
                raise DownloadSafetyError(
                    "ExternalDownloadCannotWriteHistoricalManagedPath"
                )
        except DownloadSafetyError:
            raise
        except Exception as exc:
            raise DownloadSafetyError("SubjectDocumentRegistryReadFailed") from exc
    if os.path.lexists(target):
        raise DownloadSafetyError("DownloadTargetAlreadyExists")


async def _publish_download(
    plugin: Any, workspace: Path, target: Path, staging: _DownloadStaging
) -> None:
    """Keep the final registry check and filesystem publication under one fence."""
    service = _plugin_life_service(plugin)
    store = getattr(service, "_subject_document_store", None)
    if store is not None or bool(
        getattr(service, "_selectable_storage_enabled", False)
    ):
        fence = getattr(store, "workspace_namespace_fence", None)
        if not callable(fence):
            raise DownloadSafetyError("SubjectDocumentNamespaceFenceUnavailable")
        entered = False
        try:
            async with fence():
                entered = True
                await _check_download_target(plugin, workspace, target)
                await _owned_io(staging.publish)
        except Exception as exc:
            if not entered:
                raise DownloadSafetyError(
                    "SubjectDocumentNamespaceFenceRejected"
                ) from exc
            raise
    else:
        # Explicit legacy-disabled mode has no selected registry writer to fence.
        await _check_download_target(plugin, workspace, target)
        await _owned_io(staging.publish)


class _DownloadStaging:
    """Own one unnamed temporary inode; publish atomically without clobbering.

    Linux O_TMPFILE gives cleanup no directory name to race or accidentally delete.
    linkat through /proc/self/fd links only this owned inode. Unsupported platforms
    and filesystems fail closed. Directory identities are
    rechecked before writes/publication; untrusted symlink components are never
    followed. This is not a sandbox against a privileged filesystem administrator.
    """

    def __init__(self, workspace: Path, target: Path) -> None:
        self.workspace = workspace
        self.target = target
        self.directories: list[int] = []
        self.edges: list[tuple[int, str, int]] = []
        self.fd: int | None = None
        self.published = False
        if (
            os.name != "posix"
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_TMPFILE")
            or os.open not in os.supports_dir_fd
            or os.link not in os.supports_dir_fd
            or not Path("/proc/self/fd").is_dir()
        ):
            raise DownloadSafetyError("SecureDownloadPublishUnavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            parent_fd = os.open(workspace, flags)
            self.directories.append(parent_fd)
            for part in target.relative_to(workspace).parts[:-1]:
                try:
                    child_fd = os.open(part, flags, dir_fd=parent_fd)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                    child_fd = os.open(part, flags, dir_fd=parent_fd)
                self.directories.append(child_fd)
                self.edges.append((parent_fd, part, child_fd))
                parent_fd = child_fd
            self.parent_fd = parent_fd
            self.assert_attached()
            self.fd = os.open(
                ".",
                os.O_TMPFILE | os.O_RDWR,
                0o600,
                dir_fd=parent_fd,
            )
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    def assert_attached(self) -> None:
        if self._identity(self.workspace.lstat()) != self._identity(
            os.fstat(self.directories[0])
        ):
            raise DownloadSafetyError("DownloadWorkspaceChanged")
        for parent_fd, name, child_fd in self.edges:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or self._identity(info) != self._identity(
                os.fstat(child_fd)
            ):
                raise DownloadSafetyError("DownloadParentChanged")

    def write(self, chunk: bytes) -> None:
        self.assert_attached()
        assert self.fd is not None
        view = memoryview(chunk)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise OSError("DownloadWriteMadeNoProgress")
            view = view[written:]

    def publish(self) -> None:
        assert self.fd is not None
        os.fsync(self.fd)
        self.assert_attached()
        os.link(
            f"/proc/self/fd/{self.fd}",
            self.target.name,
            dst_dir_fd=self.parent_fd,
            follow_symlinks=True,
        )
        self.published = True
        os.fsync(self.parent_fd)

    def close(self) -> None:
        """Closing our unnamed inode never unlinks a caller-visible filename."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        for descriptor in reversed(self.directories):
            os.close(descriptor)
        self.directories.clear()


def _infer_filename(url: str, content_type: str | None) -> str:
    """从 URL 和 Content-Type 推断文件名。"""
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)

    # 清理非法字符
    name = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    if not name or name in {".", "..", "/"}:
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


def _resolve_save_path(
    workspace: Path, save_path: str, filename: str
) -> tuple[bool, Path | str]:
    """将用户指定的保存路径解析到 workspace 内的绝对路径。"""
    raw = str(save_path or "").strip().replace("\\", "/")
    if "\x00" in raw or ".." in Path(raw).parts:
        return False, "DownloadPathTraversalRejected"

    if not raw:
        target = workspace / "downloads" / filename
    else:
        candidate = Path(raw)
        if candidate.is_absolute():
            target = candidate
        else:
            target = workspace / candidate

    try:
        resolved = Path(os.path.abspath(target))
        resolved.relative_to(workspace)
        _check_path_components(workspace, resolved)
    except (OSError, ValueError, DownloadSafetyError) as exc:
        return False, f"路径解析失败: {type(exc).__name__}"

    # 如果路径以目录形式给出（已存在的目录，或者以 / 结尾），把文件名拼上去
    if resolved.is_dir() or str(raw).endswith("/"):
        resolved = resolved / filename

    # 安全检查：必须在 workspace 内
    try:
        resolved.relative_to(workspace)
        _check_path_components(workspace, resolved)
    except ValueError:
        return (
            False,
            f"保存路径超出 workspace 范围: {resolved}（workspace: {workspace}）",
        )

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
        "现有目标不会覆盖；主体文件、已登记文档与运行保留路径不接受外部下载。\n"
        "下载内容保持外部来源，不构成主体表达或记忆版本。\n\n"
        "**返回：** 保存路径、文件大小、MIME 类型"
    )

    async def execute(
        self,
        url: Annotated[str, "要下载的文件 URL"],
        save_path: Annotated[
            str,
            "workspace 内的保存路径（相对路径或目录），留空则自动放到 downloads/ 下",
        ] = "",
        timeout_seconds: Annotated[
            int, "下载超时（秒），默认 60"
        ] = _DEFAULT_TIMEOUT_SECONDS,
        max_size_mb: Annotated[
            int, "最大允许文件大小（MB），默认 200"
        ] = _DEFAULT_MAX_SIZE_MB,
    ) -> tuple[bool, str | dict]:
        """下载文件到 workspace。"""
        try:
            import aiohttp
        except ImportError:
            return False, {"error": "缺少 aiohttp 依赖，无法执行下载"}

        url = str(url or "").strip()
        if not url:
            return False, {"error": "url 不能为空"}
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return False, {"error": "不支持的 URL（仅支持有效 http/https 地址）"}
            public_url = _safe_url(url)
        except ValueError:
            return False, {"error": "URL 格式无效"}

        try:
            timeout_seconds = max(5, min(600, int(timeout_seconds)))
        except (TypeError, ValueError, OverflowError):
            timeout_seconds = _DEFAULT_TIMEOUT_SECONDS

        try:
            max_size_mb = max(1, min(2048, int(max_size_mb)))
        except (TypeError, ValueError, OverflowError):
            max_size_mb = _DEFAULT_MAX_SIZE_MB

        max_size_bytes = max_size_mb * 1024 * 1024
        workspace = _get_workspace(self.plugin)

        logger.info(f"[nucleus_download] timeout={timeout_seconds}s")

        started = time.perf_counter()

        staging: _DownloadStaging | None = None
        try:
            ok, resolved = _resolve_save_path(
                workspace, save_path, _infer_filename(url, None)
            )
            if not ok:
                return False, {"error": str(resolved), "url": public_url}
            await _check_download_target(self.plugin, workspace, Path(resolved))
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    allow_redirects=True,
                ) as resp,
            ):
                if resp.status != 200:
                    return False, {
                        "error": f"HTTP {resp.status}",
                        "url": public_url,
                    }

                content_type = resp.headers.get("Content-Type", "")
                content_length = resp.headers.get("Content-Length")
                declared_size: int | None = None
                if content_length is not None:
                    declared_size = int(content_length)
                    if declared_size < 0:
                        raise DownloadSafetyError("InvalidDownloadContentLength")
                    if declared_size > max_size_bytes:
                        return False, {
                            "error": (
                                f"文件大小 {_format_size(declared_size)} 超过限制 "
                                f"{max_size_mb} MB"
                            ),
                            "url": public_url,
                        }

                # 推断文件名
                filename = _infer_filename(str(resp.url), content_type or None)

                # 解析保存路径
                ok, resolved = _resolve_save_path(workspace, save_path, filename)
                if not ok:
                    return False, {"error": str(resolved), "url": public_url}

                save_file = Path(resolved)
                await _check_download_target(self.plugin, workspace, save_file)
                staging = _DownloadStaging(workspace, save_file)

                # 流式写入
                downloaded = 0
                digest = hashlib.sha256()
                async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                    downloaded += len(chunk)
                    if downloaded > max_size_bytes:
                        raise DownloadSafetyError("DownloadSizeLimitExceeded")
                    await _owned_io(staging.write, chunk)
                    digest.update(chunk)
                # aiohttp can decompress wire bytes; only compare like representations.
                if (
                    declared_size is not None
                    and not resp.headers.get("Content-Encoding")
                    and downloaded != declared_size
                ):
                    raise DownloadSafetyError("DownloadContentLengthMismatch")
                await _publish_download(self.plugin, workspace, save_file, staging)

        except TimeoutError:
            return False, {
                "error": f"下载超时（{timeout_seconds}秒）",
                "url": public_url,
            }
        except DownloadSafetyError as exc:
            return False, {"error": str(exc), "url": public_url}
        except FileExistsError:
            return False, {"error": "DownloadTargetAlreadyExists", "url": public_url}
        except Exception as exc:  # noqa: BLE001 - tool boundary must return sanitized failures
            result: dict[str, Any] = {
                "error": f"下载失败: {type(exc).__name__}",
                "url": public_url,
            }
            if staging is not None and staging.published:
                result.update(
                    {
                        "publication_status": "published_durability_unconfirmed",
                        "saved_to": str(staging.target),
                        "workspace_relative": staging.target.relative_to(
                            workspace
                        ).as_posix(),
                    }
                )
            return False, result
        finally:
            if staging is not None:
                staging.close()

        duration_ms = int((time.perf_counter() - started) * 1000)
        rel_path = str(save_file.relative_to(workspace))

        logger.info(
            f"[nucleus_download] 完成: {rel_path} "
            f"size={_format_size(downloaded)} duration={duration_ms}ms"
        )

        return True, {
            "url": public_url,
            "saved_to": str(save_file),
            "workspace_relative": rel_path,
            "size": _format_size(downloaded),
            "size_bytes": downloaded,
            "sha256": digest.hexdigest(),
            "source_origin": "external_download",
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
