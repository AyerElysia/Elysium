"""Project unified subject context into one bounded realtime voice episode."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.models.message import Message, MessageType

from .life_binding import get_running_life_service
from .runtime_store import VoiceEpisodeStore

_SUBJECT_SOURCE_ORDER = ("SOUL.md", "USER.md", "MEMORY.md")
_SUBJECT_SOURCE_NAMES = frozenset(_SUBJECT_SOURCE_ORDER)
_SUBJECT_PREFIX = "<subject_context_projection>\n"
_SUBJECT_SUFFIX = "\n</subject_context_projection>"
_EPISODE_PREFIX = "<episode_continuation_projection>\n"
_EPISODE_SUFFIX = "\n</episode_continuation_projection>"
_OVERLAY_PREFIX = "<voice_interaction_overlay>\n"
_OVERLAY_SUFFIX = "\n</voice_interaction_overlay>"
_PERCEPTION_PREFIX = "<transient_world_perception>\n"
_PERCEPTION_SUFFIX = "\n</transient_world_perception>"
_VOICE_PROMPT_ALGORITHM = "voice-live-layered-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_VOICE_RUNTIME_CONTRACT = """<voice_runtime_contract>
这是同一持续主体在实时语音场景中的局部意识窗口，不是另一套人格。
subject_context_projection 是从 SOUL.md、USER.md、MEMORY.md 权威原文派生的有界只读投影；权威仍是对应主体文件。
episode、语音覆盖层和瞬态世界感知都不是身份权威，不能覆盖或重写主体投影。
缺失或被省略的信息应通过已授权工具按需回想；不要猜测、补写或朗读内部标签、协议、哈希和工具名称。
实时语音允许自然地倾听、思考、打断、沉默或表达；是否表达和如何表达仍由当前意识自行判断。
</voice_runtime_contract>"""


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trim_utf8_suffix(value: str, max_bytes: int) -> tuple[str, int]:
    """Keep a valid UTF-8 suffix and report omitted source bytes."""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, 0
    start = len(encoded) - max(0, max_bytes)
    while start < len(encoded) and encoded[start] & 0xC0 == 0x80:
        start += 1
    return encoded[start:].decode("utf-8"), start


def _trim_utf8_prefix(value: str, max_bytes: int) -> tuple[str, int]:
    """Keep a valid UTF-8 prefix and report omitted source bytes."""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, 0
    end = max(0, max_bytes)
    while end > 0 and encoded[end] & 0xC0 == 0x80:
        end -= 1
    return encoded[:end].decode("utf-8"), len(encoded) - end


def _compact_context_lines(content: str, max_bytes: int) -> tuple[str, dict[str, Any]]:
    """Build a bounded head/tail view while keeping its durable source intact."""

    if max_bytes < 1024:
        raise ValueError("context projection budget must be at least 1024 bytes")
    original_bytes = _utf8_size(content)
    if original_bytes <= max_bytes:
        return content, {
            "algorithm": "head-tail-lines-v1",
            "compacted": False,
            "original_bytes": original_bytes,
            "delivered_bytes": original_bytes,
            "omitted_lines": 0,
            "max_bytes": max_bytes,
            "projection_sha256": _sha256_text(content),
        }

    lines = content.splitlines()
    marker_template = (
        "[有界投影已省略中间内容；完整来源仍保留，可通过已授权工具继续读取。"
        " original_bytes={original_bytes}; omitted_lines={omitted_lines}]"
    )
    provisional_marker = marker_template.format(
        original_bytes=original_bytes,
        omitted_lines=len(lines),
    )
    marker_bytes = _utf8_size(provisional_marker) + 2
    available = max(0, max_bytes - marker_bytes)
    head_budget = available // 3
    tail_budget = available - head_budget

    head: list[str] = []
    head_used = 0
    head_end = 0
    for index, line in enumerate(lines):
        cost = _utf8_size(line) + 1
        if head_used + cost > head_budget:
            break
        head.append(line)
        head_used += cost
        head_end = index + 1

    tail_reversed: list[str] = []
    tail_used = 0
    tail_start = len(lines)
    for index in range(len(lines) - 1, head_end - 1, -1):
        line = lines[index]
        cost = _utf8_size(line) + 1
        if tail_used + cost > tail_budget:
            break
        tail_reversed.append(line)
        tail_used += cost
        tail_start = index
    tail = list(reversed(tail_reversed))
    omitted_lines = max(0, tail_start - head_end)
    marker = marker_template.format(
        original_bytes=original_bytes,
        omitted_lines=omitted_lines,
    )
    if not head and not tail:
        available = max(0, max_bytes - _utf8_size(marker) - 2)
        head_text, _ = _trim_utf8_prefix(content, available // 3)
        tail_text, _ = _trim_utf8_suffix(content, available - _utf8_size(head_text))
        head = [head_text] if head_text else []
        tail = [tail_text] if tail_text else []
    compacted = "\n".join([*head, marker, *tail])
    delivered_bytes = _utf8_size(compacted)
    if delivered_bytes > max_bytes:
        raise RuntimeError("bounded projection exceeded its configured byte budget")
    return compacted, {
        "algorithm": "head-tail-lines-v1",
        "compacted": True,
        "original_bytes": original_bytes,
        "delivered_bytes": delivered_bytes,
        "omitted_lines": omitted_lines,
        "max_bytes": max_bytes,
        "projection_sha256": _sha256_text(compacted),
    }


def _project_episode_transcript(
    transcript: list[dict[str, Any]],
    max_bytes: int,
) -> tuple[str, dict[str, Any]]:
    """Keep the newest complete turns; the append-only episode remains complete."""

    normalized = [
        {
            "role": str(item.get("role") or ""),
            "text": str(item.get("text") or ""),
        }
        for item in transcript
        if str(item.get("role") or "") in {"user", "assistant"}
        and str(item.get("text") or "")
    ]
    lines = [
        json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        for item in normalized
    ]
    original = "\n".join(lines)
    original_bytes = _utf8_size(original)
    if original_bytes <= max_bytes:
        return original, {
            "algorithm": "recent-complete-turns-v1",
            "compacted": False,
            "original_bytes": original_bytes,
            "delivered_bytes": original_bytes,
            "source_turns": len(lines),
            "delivered_turns": len(lines),
            "omitted_turns": 0,
            "partial_latest_turn": False,
            "max_bytes": max_bytes,
            "projection_sha256": _sha256_text(original),
        }

    marker_template = (
        "{\"projection_notice\":\"更早的语音轮次或当前超长轮次前缀保留在完整 episode/Life Event 历史中；"
        "需要时使用已授权历史工具继续读取\",\"omitted_turns\":%d}"
    )
    selected_reversed: list[str] = []
    selected_bytes = 0
    partial_latest_turn = False
    for line in reversed(lines):
        prospective_omitted = len(lines) - len(selected_reversed) - 1
        marker = marker_template % max(0, prospective_omitted)
        cost = _utf8_size(line) + (1 if selected_reversed else 0)
        if selected_bytes + cost + _utf8_size(marker) + 1 > max_bytes:
            break
        selected_reversed.append(line)
        selected_bytes += cost
    selected = list(reversed(selected_reversed))
    omitted_turns = len(lines) - len(selected)
    if not selected and normalized:
        marker = marker_template % max(0, len(lines) - 1)
        role = normalized[-1]["role"]
        text_bytes = _utf8_size(normalized[-1]["text"])
        fixed = json.dumps(
            {
                "role": role,
                "text_suffix": "",
                "prefix_omitted_bytes": text_bytes,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        suffix_budget = max(0, max_bytes - _utf8_size(marker) - _utf8_size(fixed) - 2)
        while True:
            suffix, omitted_bytes = _trim_utf8_suffix(
                normalized[-1]["text"], suffix_budget
            )
            candidate = json.dumps(
                {
                    "role": role,
                    "text_suffix": suffix,
                    "prefix_omitted_bytes": omitted_bytes,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            overflow = _utf8_size(marker + "\n" + candidate) - max_bytes
            if overflow <= 0:
                selected = [candidate]
                break
            if suffix_budget <= 0:
                raise RuntimeError(
                    "episode continuation metadata exceeded its byte budget"
                )
            suffix_budget = max(0, suffix_budget - overflow - 8)
        omitted_turns = len(lines) - 1
        partial_latest_turn = True
    marker = marker_template % omitted_turns
    projected = "\n".join([marker, *selected])
    delivered_bytes = _utf8_size(projected)
    if delivered_bytes > max_bytes:
        raise RuntimeError("episode continuation projection exceeded its byte budget")
    return projected, {
        "algorithm": "recent-complete-turns-v1",
        "compacted": True,
        "original_bytes": original_bytes,
        "delivered_bytes": delivered_bytes,
        "source_turns": len(lines),
        "delivered_turns": len(selected),
        "omitted_turns": omitted_turns,
        "partial_latest_turn": partial_latest_turn,
        "max_bytes": max_bytes,
        "projection_sha256": _sha256_text(projected),
    }


def _snapshot_value(snapshot: Any, name: str, default: Any = None) -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(name, default)
    return getattr(snapshot, name, default)


def _normalize_subject_snapshot(snapshot: Any) -> tuple[str, dict[str, Any]]:
    text = str(_snapshot_value(snapshot, "text", "") or "")
    raw_metadata = _snapshot_value(snapshot, "metadata", {})
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    for name in (
        "schema_version",
        "kind",
        "authority",
        "source_digest",
        "revision",
        "projection_kind",
        "projection_profile",
        "projection_algorithm",
        "projection_version",
        "projection_sha256",
        "original_bytes",
        "delivered_bytes",
        "max_bytes",
        "sources",
        "budget",
    ):
        value = _snapshot_value(snapshot, name)
        if value is not None and name not in metadata:
            metadata[name] = value
    budget = metadata.get("budget")
    budget = dict(budget) if isinstance(budget, Mapping) else {}
    coverage = budget.get("sources")
    coverage = dict(coverage) if isinstance(coverage, Mapping) else {}
    raw_sources = metadata.get("sources")
    normalized_sources: list[dict[str, Any]] = []
    source_metadata_has_content = False
    if isinstance(raw_sources, Mapping):
        raw_sources = [
            {"path": path, **(dict(value) if isinstance(value, Mapping) else {})}
            for path, value in raw_sources.items()
        ]
    if isinstance(raw_sources, list):
        for raw_source in raw_sources:
            if isinstance(raw_source, Mapping):
                source = dict(raw_source)
            else:
                source = {"path": raw_source}
            if {"text", "content", "projection_text", "raw"} & set(source):
                source_metadata_has_content = True
            path = str(source.get("path") or "")
            source_coverage = coverage.get(path)
            if isinstance(source_coverage, Mapping):
                if {"text", "content", "projection_text", "raw"} & set(
                    source_coverage
                ):
                    source_metadata_has_content = True
                source.update(dict(source_coverage))
            if "original_bytes" not in source and "size_bytes" in source:
                source["original_bytes"] = source["size_bytes"]
            normalized_sources.append(
                {
                    name: source[name]
                    for name in (
                        "path",
                        "sha256",
                        "size_bytes",
                        "original_bytes",
                        "delivered_bytes",
                        "max_delivered_bytes",
                    )
                    if name in source
                }
            )
    metadata["sources"] = normalized_sources
    metadata["source_metadata_has_content"] = source_metadata_has_content
    metadata["artifact_kind"] = str(metadata.get("kind") or "")
    metadata["projection_kind"] = str(
        metadata.get("projection_profile")
        or metadata.get("projection_kind")
        or ""
    )
    metadata["projection_profile"] = metadata["projection_kind"]
    metadata["revision"] = str(
        metadata.get("revision") or metadata.get("source_digest") or ""
    )
    for name in ("original_bytes", "delivered_bytes", "max_bytes"):
        if name in budget:
            metadata[name] = budget[name]
    return text, metadata


def _source_names(metadata: Mapping[str, Any]) -> set[str]:
    sources = metadata.get("sources")
    if isinstance(sources, Mapping):
        candidates = list(sources)
    elif isinstance(sources, list):
        candidates = [
            item.get("path") if isinstance(item, Mapping) else item for item in sources
        ]
    else:
        candidates = []
    return {Path(str(item or "")).name for item in candidates if item}


def _validate_subject_sources(text: str, metadata: Mapping[str, Any]) -> None:
    if metadata.get("source_metadata_has_content"):
        raise RuntimeError("主体投影逐源 manifest 禁止携带私密正文")
    sources = metadata.get("sources")
    if not isinstance(sources, list):
        raise TypeError("统一主体上下文投影缺少逐源 manifest")
    paths = [Path(str(item.get("path") or "")).name for item in sources]
    if tuple(paths) != _SUBJECT_SOURCE_ORDER:
        raise RuntimeError(
            "统一主体上下文投影来源顺序或范围不兼容"
        )
    block_positions: list[int] = []
    for source, path in zip(sources, _SUBJECT_SOURCE_ORDER, strict=True):
        digest = str(source.get("sha256") or "")
        if not _SHA256_RE.fullmatch(digest):
            raise RuntimeError(f"主体投影来源 {path} 缺少有效 sha256")
        try:
            original_bytes = int(source.get("original_bytes"))
            delivered_bytes = int(source.get("delivered_bytes"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"主体投影来源 {path} 缺少覆盖统计") from exc
        if original_bytes < 0 or delivered_bytes < 0:
            raise RuntimeError(f"主体投影来源 {path} 覆盖统计非法")
        marker = f'<subject-source path="{path}">'
        if text.count(marker) != 1:
            raise RuntimeError(f"主体投影未唯一覆盖 {path}")
        block_positions.append(text.index(marker))
    if block_positions != sorted(block_positions):
        raise RuntimeError("主体投影三个权威源顺序错误")


def _subject_audit_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Keep provenance and sizes while never persisting projected subject text."""

    allowed = {
        "schema_version",
        "artifact_kind",
        "authority",
        "source_digest",
        "revision",
        "projection_kind",
        "projection_profile",
        "projection_algorithm",
        "projection_version",
        "projection_sha256",
        "original_bytes",
        "delivered_bytes",
        "max_bytes",
        "sources",
    }
    return {key: metadata[key] for key in allowed if key in metadata}


@dataclass(frozen=True, slots=True)
class VoicePromptBundle:
    """One immutable provider prompt and its content-free audit metadata."""

    text: str
    subject_context: dict[str, Any]
    layers: dict[str, dict[str, Any]]


class ContextBridge:
    """Build bounded context and publish final voice turns to LifeEngine."""

    def __init__(
        self, config: Any, consciousness: Any, store: VoiceEpisodeStore
    ) -> None:
        self._config = config
        self._consciousness = consciousness
        self._store = store
        self._last_perception_stats: dict[str, Any] = {}
        self._prompt_bundle: VoicePromptBundle | None = None

    def _bound_subject_context(self) -> dict[str, Any]:
        bound: dict[str, Any] = {}
        for record in self._store.read_all():
            if record.event == "subject_context.bound":
                bound = dict(record.payload)
        return bound

    async def _load_subject_projection(self) -> tuple[str, dict[str, Any], bool]:
        service = get_running_life_service()
        if service is None:
            raise RuntimeError(
                "LifeEngine 未运行，Voice Live 无法取得统一主体上下文投影"
            )
        getter = getattr(service, "get_subject_context_projection_snapshot", None)
        if not callable(getter):
            raise TypeError("LifeEngine 未提供统一主体上下文投影 API")
        bound = await asyncio.to_thread(self._bound_subject_context)
        configured_budget = int(self._config.session.subject_context_max_bytes)
        if bound:
            bound_kind = str(bound.get("projection_kind") or "")
            bound_budget = int(bound.get("max_bytes") or 0)
            if bound_kind != "voice_live" or bound_budget != configured_budget:
                raise RuntimeError(
                    "episode 绑定的主体投影 profile/预算与当前配置不同；"
                    "为避免重连时身份漂移，请建立新 episode"
                )
        kwargs: dict[str, Any] = {
            "projection_kind": "voice_live",
            "max_bytes": configured_budget,
        }
        if bound:
            kwargs["source_digest"] = str(bound.get("source_digest") or "")
            kwargs["projection_version"] = bound.get("projection_version")
        snapshot = getter(**kwargs)
        if inspect.isawaitable(snapshot):
            snapshot = await snapshot
        if snapshot is None:
            raise RuntimeError("统一主体上下文投影不可用；禁止回退到旧 personality")
        text, metadata = _normalize_subject_snapshot(snapshot)
        if not text:
            raise RuntimeError("统一主体上下文投影为空；禁止构造无身份语音实例")
        delivered_bytes = _utf8_size(text)
        if delivered_bytes > configured_budget:
            raise RuntimeError("统一主体上下文投影超过 Voice Live 身份预算")
        if metadata.get("authority") != "derived_non_authoritative":
            raise RuntimeError("Voice Live 只接受明确标记为非权威的主体投影")
        if metadata.get("projection_kind") != "voice_live":
            raise RuntimeError("主体投影 profile 不是 voice_live")
        try:
            manifest_budget = int(metadata.get("max_bytes"))
            manifest_delivered = int(metadata.get("delivered_bytes"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("主体投影缺少可验证的字节统计") from exc
        if manifest_budget != configured_budget:
            raise RuntimeError("主体投影 manifest 预算与 Voice Live 请求不一致")
        if manifest_delivered != delivered_bytes:
            raise RuntimeError("主体投影 manifest delivered_bytes 与正文不一致")
        missing_sources = _SUBJECT_SOURCE_NAMES - _source_names(metadata)
        if missing_sources:
            raise RuntimeError(
                "统一主体上下文投影缺少权威来源: "
                + ", ".join(sorted(missing_sources))
            )
        _validate_subject_sources(text, metadata)
        projection_sha256 = str(metadata.get("projection_sha256") or "")
        actual_sha256 = _sha256_text(text)
        if projection_sha256 != actual_sha256:
            raise RuntimeError("统一主体上下文投影哈希校验失败")
        source_digest = str(metadata.get("source_digest") or "")
        if not _SHA256_RE.fullmatch(source_digest):
            raise RuntimeError("统一主体上下文投影缺少 source_digest")
        if str(metadata.get("revision") or "") == "":
            raise RuntimeError("统一主体上下文投影缺少 revision")
        try:
            projection_version = int(metadata.get("projection_version"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("统一主体上下文投影缺少版本") from exc
        if projection_version <= 0 or not metadata.get("projection_algorithm"):
            raise RuntimeError("统一主体上下文投影版本契约非法")
        if bound:
            if metadata.get("source_digest") != bound.get("source_digest"):
                raise RuntimeError("episode 重连取得了不同的主体 source_digest")
            if metadata.get("revision") != bound.get("revision"):
                raise RuntimeError("episode 重连取得了不同的主体 revision")
            if metadata.get("projection_sha256") != bound.get("projection_sha256"):
                raise RuntimeError("episode 重连无法重现原主体投影")
            if metadata.get("projection_version") != bound.get("projection_version"):
                raise RuntimeError("episode 重连取得了不同的主体投影版本")
        metadata["delivered_bytes"] = delivered_bytes
        return text, _subject_audit_metadata(metadata), bool(bound)

    async def build_system_prompt(self) -> VoicePromptBundle:
        """Bind one subject revision and build a deterministic layered prompt."""

        if self._prompt_bundle is not None:
            return self._prompt_bundle
        subject_text, subject_metadata, resumed = await self._load_subject_projection()
        subject_wrapper = _SUBJECT_PREFIX + subject_text + _SUBJECT_SUFFIX
        layers: dict[str, dict[str, Any]] = {
            "runtime_contract": {
                "algorithm": "static-runtime-contract-v1",
                "delivered_bytes": _utf8_size(_VOICE_RUNTIME_CONTRACT),
                "projection_sha256": _sha256_text(_VOICE_RUNTIME_CONTRACT),
            },
            "subject_context": {
                **subject_metadata,
                "delivered_bytes": _utf8_size(subject_wrapper),
            },
        }
        parts = [_VOICE_RUNTIME_CONTRACT, subject_wrapper]

        transcript = await asyncio.to_thread(self._store.transcript)
        if transcript:
            wrapper_budget = _utf8_size(_EPISODE_PREFIX + _EPISODE_SUFFIX)
            episode_text, episode_stats = _project_episode_transcript(
                transcript,
                int(self._config.session.episode_context_max_bytes) - wrapper_budget,
            )
            episode_wrapper = _EPISODE_PREFIX + episode_text + _EPISODE_SUFFIX
            episode_stats["delivered_bytes"] = _utf8_size(episode_wrapper)
            layers["episode_continuation"] = episode_stats
            parts.append(episode_wrapper)

        instructions = str(self._config.full_duplex.instructions or "").strip()
        if instructions:
            wrapper_budget = _utf8_size(_OVERLAY_PREFIX + _OVERLAY_SUFFIX)
            overlay, overlay_stats = _compact_context_lines(
                instructions,
                int(self._config.session.voice_instructions_max_bytes)
                - wrapper_budget,
            )
            overlay_wrapper = _OVERLAY_PREFIX + overlay + _OVERLAY_SUFFIX
            overlay_stats["delivered_bytes"] = _utf8_size(overlay_wrapper)
            layers["voice_interaction_overlay"] = overlay_stats
            parts.append(overlay_wrapper)

        prompt = "\n\n".join(parts)
        total_bytes = _utf8_size(prompt)
        max_total = int(self._config.session.startup_context_max_bytes)
        if total_bytes > max_total:
            raise RuntimeError(
                "Voice Live 分层启动上下文超过总预算: "
                f"delivered={total_bytes}, max={max_total}"
            )
        layers["total"] = {
            "algorithm": _VOICE_PROMPT_ALGORITHM,
            "delivered_bytes": total_bytes,
            "max_bytes": max_total,
            "projection_sha256": _sha256_text(prompt),
        }
        event = "subject_context.resumed" if resumed else "subject_context.bound"
        await self._store.append_async(event, subject_metadata)
        self._prompt_bundle = VoicePromptBundle(prompt, subject_metadata, layers)
        return self._prompt_bundle

    def build_llm_context_prefix(self) -> tuple[str, Any | None]:
        """Build one transient world context and its uncommitted delivery."""

        prepared = self._consciousness.prepare_perception()
        if prepared is None:
            self._last_perception_stats = {}
            return "", None
        max_bytes = int(self._config.session.perception_context_max_bytes)
        wrapper_bytes = _utf8_size(_PERCEPTION_PREFIX + _PERCEPTION_SUFFIX)
        content, stats = _compact_context_lines(
            prepared.content,
            max_bytes=max_bytes - wrapper_bytes,
        )
        stats.update(
            {
                "projection_kind": "voice_live_perception",
                "from_position": getattr(prepared, "from_position", None),
                "through_position": getattr(prepared, "through_position", None),
                "cursor_revision": getattr(prepared, "cursor_revision", None),
                "assertion_count": len(getattr(prepared, "assertion_ids", ()) or ()),
                "change_count": len(getattr(prepared, "change_positions", ()) or ()),
            }
        )
        self._last_perception_stats = stats
        return (
            f"{_PERCEPTION_PREFIX}{content}{_PERCEPTION_SUFFIX}",
            prepared,
        )

    def perception_projection_stats(self) -> dict[str, Any]:
        """Return content-free metrics for the latest transient projection."""

        return dict(self._last_perception_stats)

    def project_tool_result(self, result: Any) -> tuple[str, dict[str, Any]]:
        """Create a bounded one-turn transport view of a tool result."""

        if isinstance(result, str):
            source = result
            source_type = "text"
        else:
            source = json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            source_type = type(result).__name__
        projected, stats = _compact_context_lines(
            source,
            int(self._config.session.tool_result_context_max_bytes),
        )
        stats.update(
            {
                "projection_kind": "voice_live_tool_result",
                "source_type": source_type,
                "retention": "provider_response_ttl",
            }
        )
        return projected, stats

    async def record_transcript(
        self, role: str, text: str, *, provider_event_id: str = ""
    ) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported transcript role: {role}")
        if not text:
            return
        payload = {
            "role": role,
            "text": text,
            "provider_event_id": provider_event_id,
        }
        await self._store.append_async("transcript.final", payload)
        if not self._config.session.record_to_life:
            return
        service = get_running_life_service()
        if service is None:
            if self._config.session.require_life_engine:
                raise RuntimeError("最终转写无法写入 LifeEngine")
            return
        is_user = role == "user"
        instance = getattr(self._consciousness, "instance", None)
        sender_name = (
            str(getattr(instance, "display_name", "") or "")
            or str(self._config.session.display_name)
        )
        message = Message(
            message_id=provider_event_id or f"voice-{uuid.uuid4().hex}",
            content=text,
            processed_plain_text=text,
            message_type=MessageType.VOICE,
            sender_id=self._config.session.user_id
            if is_user
            else self._consciousness.instance_id,
            sender_name=self._config.session.user_name if is_user else sender_name,
            platform="voice_live",
            chat_type="private",
            stream_id=self._consciousness.stream_id,
            extra={
                "episode_id": self._store.episode_id,
                "consciousness_instance_id": self._consciousness.instance_id,
                "provider_event_id": provider_event_id,
            },
        )
        await service.record_message(
            message, direction="received" if is_user else "sent"
        )


__all__ = [
    "ContextBridge",
    "VoicePromptBundle",
    "_compact_context_lines",
    "_project_episode_transcript",
]
