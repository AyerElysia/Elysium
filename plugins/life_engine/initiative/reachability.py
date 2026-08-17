"""Read-only audience/surface resolution for initiative embodiment.

This module never infers identity from names, content, recency, embeddings, or
the consciousness instance that observed an occurrence.  Cross-platform person
identity is accepted only from the existing explicit ``canonical_person_key``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import InitiativeSurfaceUnavailable, ReachableSurface


@dataclass(frozen=True, slots=True)
class ReachabilityRow:
    """Content-neutral database row used by the pure projector."""

    stream_id: str
    platform: str
    chat_type: str
    person_id: str = ""
    canonical_person_key: str = ""
    user_label: str = ""
    group_id: str = ""
    group_name: str = ""


def _opaque_ref(prefix: str, *parts: str) -> str:
    material = "\0".join(str(part or "").strip() for part in parts)
    return f"{prefix}:" + hashlib.sha256(material.encode()).hexdigest()


def project_reachable_surfaces(
    rows: Iterable[ReachabilityRow],
) -> tuple[ReachableSurface, ...]:
    """Project stable surfaces without salience or recent-stream ordering."""

    surfaces: list[ReachableSurface] = []
    seen: set[str] = set()
    for row in rows:
        stream_id = str(row.stream_id or "").strip()
        platform = str(row.platform or "").strip()
        chat_type = str(row.chat_type or "").strip().lower()
        if not stream_id or not platform or chat_type not in {"private", "group"}:
            continue
        if chat_type == "private":
            canonical = str(row.canonical_person_key or "").strip()
            person_id = str(row.person_id or "").strip()
            if canonical:
                audience_ref = f"person:{canonical}"
            elif person_id:
                # Provider account ids are only unique inside one platform.
                # Keep the fallback opaque and platform-scoped so identical
                # raw ids can never collapse into one audience accidentally.
                audience_ref = _opaque_ref("account", platform, person_id)
            else:
                continue
            display_name = str(row.user_label or "").strip() or "已登记私聊账号"
        else:
            group_id = str(row.group_id or "").strip()
            if not group_id:
                continue
            audience_ref = _opaque_ref("place", platform, "group", group_id)
            display_name = str(row.group_name or "").strip() or "已登记群聊"
        surface_ref = _opaque_ref("surface", platform, chat_type, stream_id)
        if surface_ref in seen:
            continue
        seen.add(surface_ref)
        surfaces.append(
            ReachableSurface(
                surface_ref=surface_ref,
                audience_ref=audience_ref,
                platform=platform,
                chat_type=chat_type,  # type: ignore[arg-type]
                display_name=display_name,
                stream_id=stream_id,
            )
        )
    return tuple(
        sorted(
            surfaces,
            key=lambda item: (
                item.audience_ref,
                item.platform,
                item.chat_type,
                item.surface_ref,
            ),
        )
    )


def _row_value(row: Any, name: str) -> Any:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    if isinstance(mapping, Mapping):
        return mapping.get(name)
    return getattr(mapping, name, None)


async def load_reachable_surfaces() -> tuple[ReachableSurface, ...]:
    """Load all registered routes; recency is intentionally not consulted."""

    from sqlalchemy import select

    from src.core.models.sql_alchemy import ChatStreams, PersonInfo
    from src.kernel.db import get_db_session

    async with get_db_session() as session:
        result = await session.execute(
            select(
                ChatStreams.stream_id.label("stream_id"),
                ChatStreams.platform.label("platform"),
                ChatStreams.chat_type.label("chat_type"),
                ChatStreams.person_id.label("person_id"),
                ChatStreams.group_id.label("group_id"),
                ChatStreams.group_name.label("group_name"),
                PersonInfo.canonical_person_key.label("canonical_person_key"),
                PersonInfo.nickname.label("nickname"),
                PersonInfo.cardname.label("cardname"),
            ).outerjoin(PersonInfo, ChatStreams.person_id == PersonInfo.person_id)
        )
        rows = result.all()
    projected = []
    for row in rows:
        projected.append(
            ReachabilityRow(
                stream_id=str(_row_value(row, "stream_id") or ""),
                platform=str(_row_value(row, "platform") or ""),
                chat_type=str(_row_value(row, "chat_type") or ""),
                person_id=str(_row_value(row, "person_id") or ""),
                canonical_person_key=str(
                    _row_value(row, "canonical_person_key") or ""
                ),
                user_label=str(
                    _row_value(row, "cardname")
                    or _row_value(row, "nickname")
                    or ""
                ),
                group_id=str(_row_value(row, "group_id") or ""),
                group_name=str(_row_value(row, "group_name") or ""),
            )
        )
    return project_reachable_surfaces(projected)


async def resolve_reachable_surface(
    *,
    audience_ref: str,
    surface_ref: str,
) -> ReachableSurface:
    """Resolve an exact current surface without aliases or fallback."""

    audience = str(audience_ref or "").strip()
    surface = str(surface_ref or "").strip()
    for candidate in await load_reachable_surfaces():
        if candidate.surface_ref != surface:
            continue
        if candidate.audience_ref != audience:
            raise InitiativeSurfaceUnavailable(
                "surface does not belong to the explicitly selected audience"
            )
        return candidate
    raise InitiativeSurfaceUnavailable("selected delivery surface is unavailable")


__all__ = [
    "ReachabilityRow",
    "load_reachable_surfaces",
    "project_reachable_surfaces",
    "resolve_reachable_surface",
]
