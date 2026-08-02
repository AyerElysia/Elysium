"""Structured world state: the subconscious shared inner world model.

The WorldState is a semantic-level snapshot of "what the subconscious currently
knows about its world". It sits above the event-level SubconsciousSummary and
provides a structured, renderable, filterable view that multiple consciousness
instances can perceive according to their individual PerceptionFilter.

Design principles:
- JSON-serializable (persisted to runtime/world_state.json)
- Best-effort updates: if a heartbeat fails, the previous version remains valid
- Renderable into compact text for transient suffix injection
- Filterable: each consciousness instance sees only its relevant slice
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WORLD_STATE_SCHEMA_VERSION = 1


class WorldStateMigrationError(RuntimeError):
    """Raised when the preserved legacy snapshot cannot be imported safely."""


# ---------------------------------------------------------------------------
# Sub-structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RelationshipState:
    """One person/entity in the subconscious world model."""

    entity_id: str
    display_name: str = ""
    # 当前状态摘要（如"肠胃炎恢复中"、"活跃在群里"）
    status_summary: str = ""
    # 情感基调（如"亲昵"、"轻松"、"需要关心"）
    emotional_tone: str = ""
    # 最近互动时间（ISO 格式）
    last_interaction_at: str = ""
    # 最近互动的场景/流 ID
    last_interaction_stream: str = ""
    # 关键事实标签（如 ["每天晚安约定", "喜欢表情包互动"]）
    key_facts: list[str] = field(default_factory=list)
    # 关联的未闭合话题 ID
    open_thread_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "display_name": self.display_name,
            "status_summary": self.status_summary,
            "emotional_tone": self.emotional_tone,
            "last_interaction_at": self.last_interaction_at,
            "last_interaction_stream": self.last_interaction_stream,
            "key_facts": list(self.key_facts),
            "open_thread_ids": list(self.open_thread_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelationshipState":
        return cls(
            entity_id=str(data.get("entity_id") or ""),
            display_name=str(data.get("display_name") or ""),
            status_summary=str(data.get("status_summary") or ""),
            emotional_tone=str(data.get("emotional_tone") or ""),
            last_interaction_at=str(data.get("last_interaction_at") or ""),
            last_interaction_stream=str(data.get("last_interaction_stream") or ""),
            key_facts=[str(v) for v in (data.get("key_facts") or []) if v],
            open_thread_ids=[str(v) for v in (data.get("open_thread_ids") or []) if v],
        )

    def render_line(self) -> str:
        """Render as a single compact line for prompt injection."""
        parts = [self.display_name or self.entity_id]
        if self.status_summary:
            parts.append(self.status_summary)
        if self.emotional_tone:
            parts.append(f"情感:{self.emotional_tone}")
        if self.key_facts:
            parts.append("；".join(self.key_facts))
        return "[关系] " + "，".join(parts)


@dataclass(slots=True)
class OpenThread:
    """An unresolved conversation thread, commitment, or topic."""

    thread_id: str
    kind: str = "topic"  # topic | commitment | promise | question
    title: str = ""
    summary: str = ""
    # 关联的实体 ID
    related_entity_ids: list[str] = field(default_factory=list)
    # 关联的场景/流 ID
    stream_id: str = ""
    # 状态：open | waiting | resolved
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "related_entity_ids": list(self.related_entity_ids),
            "stream_id": self.stream_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpenThread":
        return cls(
            thread_id=str(data.get("thread_id") or ""),
            kind=str(data.get("kind") or "topic"),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            related_entity_ids=[str(v) for v in (data.get("related_entity_ids") or []) if v],
            stream_id=str(data.get("stream_id") or ""),
            status=str(data.get("status") or "open"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def render_line(self) -> str:
        label = {"commitment": "承诺", "promise": "约定", "question": "待答"}.get(
            self.kind, "未闭合"
        )
        text = self.title or self.summary or self.thread_id
        return f"[{label}] {text}"


@dataclass(slots=True)
class EmbodiedState:
    """Body and emotional baseline state."""

    # 身体状态摘要（如"肠胃炎恢复期"）
    body_summary: str = ""
    # 情绪基调（如"温暖、满足"）
    mood: str = ""
    # 精力水平（如"充沛"、"有些困"）
    energy: str = ""
    # 需要注意的身体约束（如"不追问肠胃"）
    constraints: list[str] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "body_summary": self.body_summary,
            "mood": self.mood,
            "energy": self.energy,
            "constraints": list(self.constraints),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmbodiedState":
        return cls(
            body_summary=str(data.get("body_summary") or ""),
            mood=str(data.get("mood") or ""),
            energy=str(data.get("energy") or ""),
            constraints=[str(v) for v in (data.get("constraints") or []) if v],
            updated_at=str(data.get("updated_at") or ""),
        )

    def render_lines(self) -> list[str]:
        lines: list[str] = []
        if self.body_summary:
            lines.append(f"[身体] {self.body_summary}")
        if self.mood:
            lines.append(f"[情绪] {self.mood}")
        if self.constraints:
            lines.append(f"[约束] {'；'.join(self.constraints)}")
        return lines


@dataclass(slots=True)
class SceneState:
    """State of one active scene (chat stream, livestream, etc.)."""

    scene_id: str
    kind: str = "chat"  # chat | group | livestream | creation
    display_name: str = ""
    # 当前状态摘要（如"停在晚安闭环"、"午间活跃"）
    status_summary: str = ""
    # 最近活跃时间
    last_active_at: str = ""
    # 绑定的意识实例 ID（Phase 3）
    consciousness_instance_id: str = ""
    # 场景内的关键上下文标签
    context_tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "status_summary": self.status_summary,
            "last_active_at": self.last_active_at,
            "consciousness_instance_id": self.consciousness_instance_id,
            "context_tags": list(self.context_tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SceneState":
        return cls(
            scene_id=str(data.get("scene_id") or ""),
            kind=str(data.get("kind") or "chat"),
            display_name=str(data.get("display_name") or ""),
            status_summary=str(data.get("status_summary") or ""),
            last_active_at=str(data.get("last_active_at") or ""),
            consciousness_instance_id=str(data.get("consciousness_instance_id") or ""),
            context_tags=[str(v) for v in (data.get("context_tags") or []) if v],
        )

    def render_line(self) -> str:
        name = self.display_name or self.scene_id
        status = self.status_summary or "静默"
        return f"[场景] {name}：{status}"


# ---------------------------------------------------------------------------
# Perception Filter (for multi-consciousness, Phase 3)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PerceptionFilter:
    """Controls which slice of WorldState a consciousness instance perceives."""

    # 关注的关系实体 ID（空=全部）
    relationship_ids: list[str] = field(default_factory=list)
    # 关注的场景 ID（空=全部）
    scene_ids: list[str] = field(default_factory=list)
    # 关注的话题类型（空=全部）
    thread_kinds: list[str] = field(default_factory=list)
    # 是否需要身体状态
    include_body_state: bool = True
    # 是否需要承诺/约定
    include_commitments: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationship_ids": list(self.relationship_ids),
            "scene_ids": list(self.scene_ids),
            "thread_kinds": list(self.thread_kinds),
            "include_body_state": self.include_body_state,
            "include_commitments": self.include_commitments,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PerceptionFilter":
        return cls(
            relationship_ids=[str(v) for v in (data.get("relationship_ids") or [])],
            scene_ids=[str(v) for v in (data.get("scene_ids") or [])],
            thread_kinds=[str(v) for v in (data.get("thread_kinds") or [])],
            include_body_state=bool(data.get("include_body_state", True)),
            include_commitments=bool(data.get("include_commitments", True)),
        )

    @classmethod
    def full(cls) -> "PerceptionFilter":
        """A filter that perceives everything (default for chat_global)."""
        return cls()


# ---------------------------------------------------------------------------
# WorldState (top-level)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorldState:
    """The subconscious structured inner world model.

    Shared across all consciousness instances. Updated by the subconscious
    heartbeat cycle. Persisted to runtime/world_state.json.
    """

    schema_version: int = WORLD_STATE_SCHEMA_VERSION
    # 关系层
    relationships: dict[str, RelationshipState] = field(default_factory=dict)
    # 话题层
    open_threads: list[OpenThread] = field(default_factory=list)
    # 身体/情绪层
    embodied_state: EmbodiedState = field(default_factory=EmbodiedState)
    # 场景层
    active_scenes: dict[str, SceneState] = field(default_factory=dict)
    # 元数据
    last_updated_sequence: int = 0
    last_updated_at: str = ""
    revision: int = 0

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "relationships": {k: v.to_dict() for k, v in self.relationships.items()},
            "open_threads": [t.to_dict() for t in self.open_threads],
            "embodied_state": self.embodied_state.to_dict(),
            "active_scenes": {k: v.to_dict() for k, v in self.active_scenes.items()},
            "last_updated_sequence": self.last_updated_sequence,
            "last_updated_at": self.last_updated_at,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorldState":
        relationships = {}
        raw_rels = data.get("relationships")
        if isinstance(raw_rels, dict):
            for key, val in raw_rels.items():
                if isinstance(val, dict):
                    relationships[str(key)] = RelationshipState.from_dict(val)

        open_threads = []
        raw_threads = data.get("open_threads")
        if isinstance(raw_threads, list):
            for item in raw_threads:
                if isinstance(item, dict):
                    open_threads.append(OpenThread.from_dict(item))

        embodied = EmbodiedState()
        raw_embodied = data.get("embodied_state")
        if isinstance(raw_embodied, dict):
            embodied = EmbodiedState.from_dict(raw_embodied)

        scenes = {}
        raw_scenes = data.get("active_scenes")
        if isinstance(raw_scenes, dict):
            for key, val in raw_scenes.items():
                if isinstance(val, dict):
                    scenes[str(key)] = SceneState.from_dict(val)

        return cls(
            schema_version=int(data.get("schema_version") or WORLD_STATE_SCHEMA_VERSION),
            relationships=relationships,
            open_threads=open_threads,
            embodied_state=embodied,
            active_scenes=scenes,
            last_updated_sequence=int(data.get("last_updated_sequence") or 0),
            last_updated_at=str(data.get("last_updated_at") or ""),
            revision=int(data.get("revision") or 0),
        )

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=indent,
            sort_keys=False,
            separators=None if indent else (",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "WorldState":
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise ValueError("world state JSON must be an object")
        return cls.from_dict(raw)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "WorldState":
        """Load the legacy snapshot, refusing a lossy reset when it is corrupt."""

        if not path.exists():
            return cls()
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise WorldStateMigrationError(
                "legacy world_state could not be imported; source was preserved"
            ) from exc

    def save(self, path: Path) -> None:
        """Atomically persist to disk (write tmp + rename)."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(self.to_json(), encoding="utf-8")
            import os
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning(f"保存 world_state 失败: {exc}")

    # ------------------------------------------------------------------
    # Rendering (for consciousness perception)
    # ------------------------------------------------------------------

    def render_for_perception(
        self,
        perception_filter: PerceptionFilter | None = None,
        *,
        max_chars: int = 3000,
    ) -> str:
        """Render a compact text slice for injection into a consciousness context.

        The output is structured as labeled lines that the consciousness can
        clearly identify as its own inner knowledge (not external messages).
        """
        # Legacy filters are retained only for transfer compatibility.  They
        # must not enforce cognitive inclusion/exclusion in code.
        del perception_filter
        lines: list[str] = []

        # 关系层
        for rel in self.relationships.values():
            lines.append(rel.render_line())

        # 身体/情绪层
        lines.extend(self.embodied_state.render_lines())

        # 话题层
        for thread in self.open_threads:
            lines.append(thread.render_line())

        # 场景层
        for scene in self.active_scenes.values():
            lines.append(scene.render_line())

        if not lines:
            return ""

        del max_chars
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Mutation helpers (used by heartbeat update logic)
    # ------------------------------------------------------------------

    def bump_revision(self, sequence: int, timestamp: str) -> None:
        """Increment revision after a successful update."""
        self.revision += 1
        self.last_updated_sequence = max(self.last_updated_sequence, sequence)
        self.last_updated_at = timestamp

    def upsert_relationship(self, rel: RelationshipState) -> None:
        """Insert or update a relationship by entity_id."""
        self.relationships[rel.entity_id] = rel

    def upsert_scene(self, scene: SceneState) -> None:
        """Insert or update a scene by scene_id."""
        self.active_scenes[scene.scene_id] = scene

    def add_thread(self, thread: OpenThread) -> None:
        """Add a new open thread (deduplicates by thread_id)."""
        for i, existing in enumerate(self.open_threads):
            if existing.thread_id == thread.thread_id:
                self.open_threads[i] = thread
                return
        self.open_threads.append(thread)

    def resolve_thread(self, thread_id: str) -> None:
        """Mark a thread as resolved."""
        for thread in self.open_threads:
            if thread.thread_id == thread_id:
                thread.status = "resolved"
                return
