"""Bounded, read-only projections for the Elysium data console.

The console consumes Life Engine authority ports.  It never opens a database
connection, mutates authority data, or treats a bounded projection as the
underlying record.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import inspect
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from plugins.life_engine.attention_threads.contracts import AttentionThreadPageQuery
from plugins.life_engine.memory.experience import ExperienceOccurrenceCursor
from plugins.life_engine.service.registry import get_life_engine_service

CONSOLE_SCHEMA = "elysium-data-console.v1"
EVENT_CONTENT_MAX_BYTES = 8 * 1024
EXPERIENCE_CONTENT_MAX_BYTES = 8 * 1024
WORLD_INLINE_MAX_BYTES = 4 * 1024
TEXT_CHUNK_MAX_BYTES = 64 * 1024
WORKSPACE_PAGE_MAX_ITEMS = 500
WORKSPACE_SCAN_MAX_ITEMS = 50_000

WORKSPACE_ROOT_FILES = frozenset(
    {
        "AyerElysia_preferences.txt",
        "MEMORY.md",
        "MEMORY_GUIDE.md",
        "SOUL.md",
        "SUBCONSCIOUS.md",
        "TOOL.md",
        "TOOLS.md",
        "USER.md",
    }
)
WORKSPACE_ROOT_DIRECTORIES = frozenset(
    {
        "diaries",
        "dreams",
        "minecraft",
        "narrative",
        "notes",
        "received",
        "skills",
        "thoughts",
    }
)
TEXT_SUFFIXES = frozenset(
    {
        ".csv",
        ".json",
        ".jsonl",
        ".log",
        ".md",
        ".rst",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
SECRET_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


class ConsoleDataError(RuntimeError):
    """Base class for content-neutral console read failures."""


class ConsoleDataUnavailable(ConsoleDataError):
    """Raised when Life Engine is not ready."""


class ConsoleDataNotFound(ConsoleDataError):
    """Raised when a stable reference does not exist."""


class ConsoleDataInvalid(ConsoleDataError):
    """Raised when a caller supplies an invalid bounded query."""


def _utf8_prefix(value: str, max_bytes: int) -> tuple[str, int]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, len(encoded)
    end = max(0, int(max_bytes))
    while end > 0:
        try:
            return encoded[:end].decode("utf-8"), end
        except UnicodeDecodeError:
            end -= 1
    return "", 0


def project_text(value: str, max_bytes: int) -> dict[str, Any]:
    """Return a UTF-8-safe bounded projection with exact size metadata."""

    text = str(value or "")
    original = text.encode("utf-8")
    content, delivered = _utf8_prefix(text, max(0, int(max_bytes)))
    return {
        "content": content,
        "content_sha256": hashlib.sha256(original).hexdigest(),
        "original_bytes": len(original),
        "delivered_bytes": delivered,
        "omitted_bytes": len(original) - delivered,
        "complete": delivered == len(original),
    }


def _is_secret_key(key: str) -> bool:
    lowered = key.casefold()
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def safe_value(value: Any, *, max_depth: int = 5) -> Any:
    """Serialize public contract objects while redacting credential-like keys."""

    if max_depth < 0:
        return "<depth-limited>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return safe_value(value.value, max_depth=max_depth - 1)
    if isinstance(value, Path):
        return value.as_posix()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: safe_value(
                getattr(value, item.name),
                max_depth=max_depth - 1,
            )
            for item in fields(value)
            if not _is_secret_key(item.name)
        }
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>"
                if _is_secret_key(str(key))
                else safe_value(item, max_depth=max_depth - 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [safe_value(item, max_depth=max_depth - 1) for item in value]
    return str(value)


def content_free_health(value: Any) -> Any:
    """Keep health metadata without accidental message or prompt bodies."""

    blocked = {
        "content",
        "message",
        "model_reply",
        "prompt",
        "public_statement",
        "reasoning",
        "text",
        "thought",
        "tool_args",
        "value",
    }
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>"
                if str(key).casefold() in blocked or _is_secret_key(str(key))
                else content_free_health(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [content_free_health(item) for item in value]
    return safe_value(value, max_depth=4)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class ElysiumDataCatalog:
    """Bounded facade plus the explicit local Minecraft session controls."""

    def __init__(self, service_provider: Callable[[], Any] | None = None) -> None:
        self._service_provider = service_provider or get_life_engine_service

    def _service(self) -> Any:
        service = self._service_provider()
        if service is None:
            raise ConsoleDataUnavailable("life_engine is not ready")
        return service

    @staticmethod
    def _envelope(kind: str, **payload: Any) -> dict[str, Any]:
        return {"schema": CONSOLE_SCHEMA, "kind": kind, **payload}

    async def overview(self) -> dict[str, Any]:
        service = self._service()
        health = content_free_health(service.health())
        event_health = await _maybe_await(
            service._get_life_event_store().health_snapshot()
        )
        memory_health = await service.memory_service.health_snapshot()
        workspace = await asyncio.to_thread(self._workspace_summary_sync, service)
        presence = [
            safe_value(item, max_depth=3)
            for item in service.consciousness_registry.get_all()
        ]
        return self._envelope(
            "overview",
            read_only=True,
            life_engine_status=str(health.get("status") or "unknown"),
            health=health,
            event_ledger=content_free_health(event_health),
            memory=content_free_health(memory_health),
            presence=presence,
            workspace=workspace,
        )

    async def minecraft_status(self) -> dict[str, Any]:
        """Return a bounded operational view without exposing full observations."""

        session = getattr(self._service(), "minecraft_session", None)
        if session is None:
            return self._envelope(
                "minecraft_status",
                read_only=True,
                available=False,
                active=False,
                readiness="disabled",
                readiness_detail="Minecraft is not enabled or initialized",
            )
        raw = dict(await session.get_status())
        observation = raw.pop("latest_observation", None)
        if isinstance(observation, Mapping):
            facts = observation.get("facts")
            fact_map = dict(facts) if isinstance(facts, Mapping) else {}
            raw["latest_observation"] = {
                "observation_id": observation.get("observation_id"),
                "sequence": observation.get("sequence"),
                "observed_at": observation.get("observed_at"),
                "world": safe_value(fact_map.get("world"), max_depth=2),
                "player": safe_value(fact_map.get("player"), max_depth=2),
                "bot_tasks": safe_value(fact_map.get("bot_tasks"), max_depth=4),
            }
        return self._envelope(
            "minecraft_status",
            read_only=True,
            available=True,
            **safe_value(raw, max_depth=5),
        )

    async def minecraft_preflight(self) -> dict[str, Any]:
        """Check the companion body and configured LAN world without starting it."""

        session = getattr(self._service(), "minecraft_session", None)
        if session is None:
            raise ConsoleDataUnavailable("Minecraft is not enabled or initialized")
        result = await session.preflight(body_name="bot")
        return self._envelope(
            "minecraft_preflight",
            read_only=True,
            body_name="bot",
            result=safe_value(result, max_depth=5),
        )

    async def minecraft_start(self, *, goal: str = "") -> dict[str, Any]:
        """Start the explicitly selected independent companion body."""

        normalized_goal = " ".join(str(goal or "").split())
        if len(normalized_goal) > 500:
            raise ConsoleDataInvalid("Minecraft session goal is too long")
        session = getattr(self._service(), "minecraft_session", None)
        if session is None:
            raise ConsoleDataUnavailable("Minecraft is not enabled or initialized")
        result = await session.start(goal=normalized_goal, body_name="bot")
        return self._envelope(
            "minecraft_start",
            read_only=False,
            body_name="bot",
            result=safe_value(result, max_depth=6),
        )

    async def minecraft_stop(self) -> dict[str, Any]:
        """Stop only the Elysium-owned companion session and bot process."""

        session = getattr(self._service(), "minecraft_session", None)
        if session is None:
            raise ConsoleDataUnavailable("Minecraft is not enabled or initialized")
        result = await session.stop()
        return self._envelope(
            "minecraft_stop",
            read_only=False,
            body_name="bot",
            result=safe_value(result, max_depth=6),
        )

    async def timeline(self, *, limit: int = 80) -> dict[str, Any]:
        page_limit = max(1, min(int(limit), 200))
        events = await self._service()._get_life_event_store().read_tail(page_limit)
        items: list[dict[str, Any]] = []
        for event in events:
            items.append(
                {
                    "event_id": event.event_id,
                    "occurrence_id": event.occurrence_id,
                    "sequence": event.sequence,
                    "timestamp": event.timestamp,
                    "recorded_at": event.recorded_at,
                    "source": event.source,
                    "source_instance_id": event.source_instance_id,
                    "channel": event.channel,
                    "event_type": event.event_type,
                    "stream_id": event.stream_id,
                    "causation_id": event.causation_id,
                    "correlation_id": event.correlation_id,
                    "priority": event.priority,
                    "content": project_text(event.content, EVENT_CONTENT_MAX_BYTES),
                    "metadata": safe_value(event.metadata, max_depth=4),
                }
            )
        return self._envelope(
            "timeline",
            read_only=True,
            order="sequence_ascending_within_tail",
            requested_limit=page_limit,
            delivered_items=len(items),
            items=items,
        )

    async def subject_documents(self) -> dict[str, Any]:
        documents = await self._service().read_subject_authority_texts()
        items = []
        for path in ("SOUL.md", "USER.md", "MEMORY.md"):
            text = str(documents.get(path) or "")
            encoded = text.encode("utf-8")
            items.append(
                {
                    "path": path,
                    "content": text,
                    "bytes": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "complete": True,
                }
            )
        return self._envelope(
            "subject_documents",
            read_only=True,
            authority="LifeEngineService.read_subject_authority_texts",
            items=items,
        )

    async def memory_summary(self) -> dict[str, Any]:
        memory = self._service().memory_service
        return self._envelope(
            "memory_summary",
            read_only=True,
            health=content_free_health(await memory.health_snapshot()),
            statistics=safe_value(await memory.get_stats(), max_depth=5),
        )

    async def memory_experiences(
        self,
        *,
        limit: int = 40,
        after_position: int = 0,
        after_occurrence_id: str = "",
        through_position: int | None = None,
        through_occurrence_id: str = "",
    ) -> dict[str, Any]:
        page_limit = max(1, min(int(limit), 100))
        after = self._experience_cursor(after_position, after_occurrence_id)
        through = self._experience_cursor(through_position, through_occurrence_id)
        page = await self._service().memory_service.list_experience_occurrence_page(
            position_after=0 if after is not None else max(0, int(after_position)),
            after=after,
            through=through,
            limit=page_limit,
        )
        items: list[dict[str, Any]] = []
        for ref in page.items:
            experience = ref.experience
            items.append(
                {
                    "occurrence_id": ref.occurrence_id,
                    "source_event_id": ref.source_event_id,
                    "ingest_position": ref.ingest_position,
                    "canonical_event_id": ref.canonical_event_id,
                    "canonical_payload_sha256": ref.canonical_payload_sha256,
                    "recorded_at": ref.recorded_at,
                    "is_alias": ref.is_alias,
                    "experience": {
                        "event_id": experience.event_id,
                        "sequence": experience.sequence,
                        "occurred_at": experience.occurred_at,
                        "source": experience.source,
                        "channel": experience.channel,
                        "event_type": experience.event_type,
                        "stream_id": experience.stream_id,
                        "consciousness_instance_id": (
                            experience.consciousness_instance_id
                        ),
                        "actor": experience.actor,
                        "visibility": experience.visibility,
                        "content": project_text(
                            experience.content,
                            EXPERIENCE_CONTENT_MAX_BYTES,
                        ),
                        "metadata": safe_value(experience.metadata, max_depth=4),
                    },
                }
            )
        return self._envelope(
            "memory_experiences",
            read_only=True,
            order="ingest_position_then_occurrence_id",
            items=items,
            next_cursor=safe_value(page.next_cursor),
            frontier=safe_value(page.frontier),
            has_more=bool(page.has_more),
        )

    @staticmethod
    def _experience_cursor(
        position: int | None,
        occurrence_id: str,
    ) -> ExperienceOccurrenceCursor | None:
        identity = str(occurrence_id or "").strip()
        if position is None and not identity:
            return None
        if position is None or not identity:
            if int(position or 0) == 0 and not identity:
                return None
            raise ConsoleDataInvalid(
                "experience cursor requires both position and occurrence_id"
            )
        try:
            return ExperienceOccurrenceCursor(int(position), identity)
        except (TypeError, ValueError) as exc:
            raise ConsoleDataInvalid("invalid experience cursor") from exc

    async def world_page(
        self,
        *,
        limit: int = 50,
        after_observed_at: str = "",
        after_assertion_id: str = "",
        include_retracted: bool = False,
    ) -> dict[str, Any]:
        page = await _maybe_await(
            self._service().world_projection.list_assertion_references_page(
                include_retracted=bool(include_retracted),
                after_observed_at=str(after_observed_at or ""),
                after_assertion_id=str(after_assertion_id or ""),
                limit=max(1, min(int(limit), 100)),
                inline_max_bytes=WORLD_INLINE_MAX_BYTES,
            )
        )
        return self._envelope(
            "world_assertions",
            read_only=True,
            order="observed_at_then_assertion_id",
            items=safe_value(page.items, max_depth=5),
            total_items=page.total_items,
            total_value_bytes=page.total_value_bytes,
            next_after_observed_at=page.next_after_observed_at,
            next_after_assertion_id=page.next_after_assertion_id,
            has_more=bool(page.next_after_assertion_id),
        )

    async def world_assertion_value(
        self,
        assertion_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = TEXT_CHUNK_MAX_BYTES,
    ) -> dict[str, Any]:
        identity = str(assertion_id or "").strip()
        if not identity:
            raise ConsoleDataInvalid("assertion_id is required")
        try:
            chunk = await _maybe_await(
                self._service().world_projection.read_assertion_value_chunk(
                    identity,
                    offset_bytes=max(0, int(offset_bytes)),
                    max_bytes=max(1, min(int(max_bytes), TEXT_CHUNK_MAX_BYTES)),
                )
            )
        except KeyError as exc:
            raise ConsoleDataNotFound("world assertion does not exist") from exc
        return self._envelope(
            "world_assertion_value",
            read_only=True,
            chunk=safe_value(chunk, max_depth=3),
        )

    async def attention_page(
        self,
        *,
        statuses: Sequence[str] = (),
        continuation: str = "",
        limit: int = 32,
    ) -> dict[str, Any]:
        try:
            query = AttentionThreadPageQuery(
                statuses=tuple(str(item) for item in statuses),
                continuation=str(continuation or ""),
                limit=max(1, min(int(limit), 100)),
                max_bytes=64 * 1024,
                projection_kind="elysium_console",
            )
        except ValueError as exc:
            raise ConsoleDataInvalid("invalid attention query") from exc
        page = await self._service().page_attention_threads(query)
        return self._envelope(
            "attention_threads",
            read_only=True,
            authority="subject_attention_authority",
            page=safe_value(page, max_depth=5),
        )

    async def workspace_summary(self) -> dict[str, Any]:
        summary = await asyncio.to_thread(
            self._workspace_summary_sync,
            self._service(),
        )
        return self._envelope("workspace_summary", read_only=True, **summary)

    async def workspace_page(
        self,
        *,
        path: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        result = await asyncio.to_thread(
            self._workspace_page_sync,
            self._service(),
            path,
            max(0, int(offset)),
            max(1, min(int(limit), WORKSPACE_PAGE_MAX_ITEMS)),
        )
        return self._envelope("workspace_page", read_only=True, **result)

    async def workspace_text(
        self,
        *,
        path: str,
        offset_bytes: int = 0,
        max_bytes: int = TEXT_CHUNK_MAX_BYTES,
    ) -> dict[str, Any]:
        result = await asyncio.to_thread(
            self._workspace_text_sync,
            self._service(),
            path,
            max(0, int(offset_bytes)),
            max(4, min(int(max_bytes), TEXT_CHUNK_MAX_BYTES)),
        )
        return self._envelope("workspace_text", read_only=True, **result)

    async def data_map(self) -> dict[str, Any]:
        service = self._service()
        health = content_free_health(service.health())
        workspace = await asyncio.to_thread(self._workspace_summary_sync, service)
        domains = [
            ("life_event", "不可变生命事件", "append-only ledger", "timeline"),
            ("memory", "经历与见证", "selected memory authority", "memory"),
            ("subject_document", "主体自我解释", "SOUL / USER / MEMORY", "subject"),
            ("presence", "意识实例在场", "presence projection", "overview"),
            ("world", "有来源世界断言", "world projection", "world"),
            ("attention", "主体持续关注", "attention authority", "attention"),
            ("initiative", "主动行动证据", "initiative authority", "timeline"),
            ("learning", "学习证据与投影", "selected learning store", "catalog"),
            ("runtime_state", "技术运行状态", "selected runtime store", "catalog"),
            ("workspace", "爱莉写下的文件", "bounded workspace", "workspace"),
            ("vector_index", "记忆检索索引", "derived index", "catalog"),
            ("training_lake", "后训练证据湖", "append-only artifacts", "catalog"),
        ]
        components = health.get("storage_runtime", {}).get("components", {})
        items = []
        for key, title, authority, view in domains:
            component = components.get(key, health.get(key, {}))
            items.append(
                {
                    "domain": key,
                    "title": title,
                    "authority": authority,
                    "view": view,
                    "health": content_free_health(component),
                }
            )
        return self._envelope(
            "data_map",
            read_only=True,
            interpretation=("本页展示证据、连续性与完整性，不对人格成长或重要性打分。"),
            domains=items,
            workspace=workspace,
        )

    @staticmethod
    def _workspace_root(service: Any) -> Path:
        root = Path(service._workspace_dir()).resolve()
        if not root.is_dir():
            raise ConsoleDataUnavailable("life workspace is not available")
        return root

    @classmethod
    def _resolve_workspace_path(
        cls,
        service: Any,
        raw_path: str,
    ) -> tuple[Path, str]:
        root = cls._workspace_root(service)
        normalized = str(raw_path or "").replace("\\", "/").strip("/")
        relative = PurePosixPath(normalized or ".")
        if relative.is_absolute() or ".." in relative.parts:
            raise ConsoleDataInvalid("workspace path is outside the visible boundary")
        if any(part.startswith(".") for part in relative.parts if part != "."):
            raise ConsoleDataInvalid("hidden workspace paths are not visible")
        if normalized:
            top = relative.parts[0]
            if (
                top not in WORKSPACE_ROOT_DIRECTORIES
                and normalized not in WORKSPACE_ROOT_FILES
            ):
                raise ConsoleDataInvalid(
                    "workspace path is outside the visible boundary"
                )
        parts = () if relative == PurePosixPath(".") else relative.parts
        candidate = root.joinpath(*parts)
        try:
            current = root
            for part in parts:
                current = current / part
                if current.is_symlink():
                    raise ConsoleDataInvalid("workspace symlinks are not visible")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except FileNotFoundError as exc:
            raise ConsoleDataNotFound("workspace path does not exist") from exc
        except ValueError as exc:
            raise ConsoleDataInvalid(
                "workspace path is outside the visible boundary"
            ) from exc
        return resolved, normalized

    @classmethod
    def _workspace_summary_sync(cls, service: Any) -> dict[str, Any]:
        root = cls._workspace_root(service)
        counts = {"files": 0, "directories": 0, "bytes": 0, "hidden": 0}
        scanned = 0
        stack = [root / name for name in sorted(WORKSPACE_ROOT_DIRECTORIES)]
        for name in sorted(WORKSPACE_ROOT_FILES):
            path = root / name
            if path.is_file() and not path.is_symlink():
                counts["files"] += 1
                counts["bytes"] += path.stat().st_size
        while stack and scanned < WORKSPACE_SCAN_MAX_ITEMS:
            directory = stack.pop()
            if not directory.is_dir() or directory.is_symlink():
                continue
            counts["directories"] += 1
            try:
                entries = list(os.scandir(directory))
            except OSError:
                counts["hidden"] += 1
                continue
            for entry in entries:
                scanned += 1
                if entry.name.startswith(".") or entry.is_symlink():
                    counts["hidden"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    counts["files"] += 1
                    try:
                        counts["bytes"] += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        counts["hidden"] += 1
                if scanned >= WORKSPACE_SCAN_MAX_ITEMS:
                    break
        return {
            **counts,
            "scan_complete": not stack and scanned < WORKSPACE_SCAN_MAX_ITEMS,
            "visible_roots": sorted(WORKSPACE_ROOT_DIRECTORIES),
            "visible_root_files": sorted(WORKSPACE_ROOT_FILES),
        }

    @classmethod
    def _workspace_page_sync(
        cls,
        service: Any,
        raw_path: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        directory, normalized = cls._resolve_workspace_path(service, raw_path)
        if not directory.is_dir():
            raise ConsoleDataInvalid("workspace page path must be a directory")
        root = cls._workspace_root(service)
        visible: list[dict[str, Any]] = []
        for entry in os.scandir(directory):
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if not normalized and (
                entry.name not in WORKSPACE_ROOT_DIRECTORIES
                and entry.name not in WORKSPACE_ROOT_FILES
            ):
                continue
            stat = entry.stat(follow_symlinks=False)
            kind = "directory" if entry.is_dir(follow_symlinks=False) else "file"
            suffix = path.suffix.casefold()
            visible.append(
                {
                    "name": entry.name,
                    "path": relative,
                    "kind": kind,
                    "bytes": stat.st_size if kind == "file" else None,
                    "modified_at_ns": stat.st_mtime_ns,
                    "text_readable": kind == "file" and suffix in TEXT_SUFFIXES,
                    "media_kind": "image" if suffix in IMAGE_SUFFIXES else "",
                }
            )
        visible.sort(
            key=lambda item: (
                item["kind"] != "directory",
                item["name"].casefold(),
            )
        )
        items = visible[offset : offset + limit]
        next_offset = offset + len(items)
        return {
            "path": normalized,
            "offset": offset,
            "limit": limit,
            "total_visible_items": len(visible),
            "items": items,
            "next_offset": next_offset,
            "has_more": next_offset < len(visible),
        }

    @classmethod
    def _workspace_text_sync(
        cls,
        service: Any,
        raw_path: str,
        offset_bytes: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        path, normalized = cls._resolve_workspace_path(service, raw_path)
        if not path.is_file() or path.suffix.casefold() not in TEXT_SUFFIXES:
            raise ConsoleDataInvalid("workspace path is not a readable text file")
        total_bytes = path.stat().st_size
        if offset_bytes < 0 or offset_bytes > total_bytes:
            raise ConsoleDataInvalid("workspace offset is outside the file")
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        try:
            with path.open("rb") as handle:
                while block := handle.read(64 * 1024):
                    digest.update(block)
                    decoder.decode(block, final=False)
                decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ConsoleDataInvalid("workspace text is not strict UTF-8") from exc

        with path.open("rb") as handle:
            if offset_bytes < total_bytes:
                handle.seek(offset_bytes)
                first = handle.read(1)
                if first and first[0] & 0xC0 == 0x80:
                    raise ConsoleDataInvalid("workspace offset is not a UTF-8 boundary")
            handle.seek(offset_bytes)
            chunk = handle.read(max_bytes + 3)
        end = min(len(chunk), max_bytes)
        while end > 0:
            try:
                content = chunk[:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            content = ""
        return {
            "path": normalized,
            "offset_bytes": offset_bytes,
            "next_offset_bytes": offset_bytes + end,
            "total_bytes": total_bytes,
            "content": content,
            "sha256": digest.hexdigest(),
            "complete": offset_bytes + end == total_bytes,
        }


__all__ = [
    "CONSOLE_SCHEMA",
    "ConsoleDataError",
    "ConsoleDataInvalid",
    "ConsoleDataNotFound",
    "ConsoleDataUnavailable",
    "ElysiumDataCatalog",
    "content_free_health",
    "project_text",
    "safe_value",
]
