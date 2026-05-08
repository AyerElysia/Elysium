"""梦境种子数据结构与收集函数。

包含 DreamSeedType 枚举、DreamSeed 数据类，
以及从多种来源收集入梦种子的函数。
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .residue import _clean_text, _unique_preserve

if TYPE_CHECKING:
    from ..memory.service import LifeMemoryService


logger = logging.getLogger("life_engine.dream")

# 常量
_MAX_DREAM_SEEDS = 3
_SEED_SCORE_TEMPERATURE = 0.15
_DREAM_HISTORY_WINDOW = 5
_DATE_STEM_RE = __import__("re").compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:$|[_-])")
_MONTH_STEM_RE = __import__("re").compile(r"^(?P<month>\d{4}-\d{2})(?:$|[_-])")


class DreamSeedType(str, Enum):
    """入梦材料类型。"""

    DAY_RESIDUE = "day_residue"
    DREAM_LAG = "dream_lag"
    UNFINISHED_TENSION = "unfinished_tension"
    SELF_THEME = "self_theme"


@dataclass
class DreamSeed:
    """一簇可入梦的心理张力。"""

    seed_id: str
    seed_type: str
    title: str
    summary: str
    core_refs: list[str] = field(default_factory=list)
    supporting_refs: list[str] = field(default_factory=list)
    core_node_ids: list[str] = field(default_factory=list)
    source_events: list[str] = field(default_factory=list)
    affect_valence: float = 0.0
    affect_arousal: float = 0.0
    importance: float = 0.0
    novelty: float = 0.0
    recurrence: float = 0.0
    unfinished_score: float = 0.0
    dreamability: float = 0.0
    score: float = 0.0
    tension_reason: str = ""


# ============================================================
# 事件辅助函数
# ============================================================


def _event_value(event: Any, key: str, default: Any = None) -> Any:
    """统一读取 dict/object 事件字段。"""
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def _event_type_value(event: Any) -> str:
    """获取事件类型值。"""
    event_type = _event_value(event, "event_type", None)
    if event_type is None:
        event_type = _event_value(event, "type", None)
    return str(getattr(event_type, "value", event_type) or "").strip().lower()


def _event_timestamp(event: Any) -> float:
    """提取事件时间戳。"""
    raw = _event_value(event, "timestamp", None)
    if raw is None:
        return time.time()
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except Exception:  # noqa: BLE001
        return time.time()


# ============================================================
# 种子收集函数
# ============================================================


async def load_memory_candidates(memory_service: LifeMemoryService | None) -> list[dict[str, Any]]:
    """从整个记忆图谱中采样候选节点。

    策略：
    - 保留 top-5 高重要性节点（核心记忆更容易被激活）
    - 从全图谱随机采样 15 个节点（任何记忆都可能闪回）
    - 合并去重，总共 ~20 个候选
    """
    if memory_service is None:
        return []

    candidates: list[dict[str, Any]] = []

    # 1. 高重要性核心节点
    getter = getattr(memory_service, "list_dream_candidate_nodes", None)
    if callable(getter):
        try:
            top_nodes = await getter(limit=5)
            if isinstance(top_nodes, list):
                candidates.extend(item for item in top_nodes if isinstance(item, dict))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取 top memory candidates 失败：{exc}")

    # 2. 全图谱随机采样
    random_getter = getattr(memory_service, "list_random_file_nodes", None)
    if callable(random_getter):
        try:
            random_nodes = await random_getter(limit=15)
            if isinstance(random_nodes, list):
                existing_ids = {c.get("node_id") for c in candidates}
                candidates.extend(
                    item
                    for item in random_nodes
                    if isinstance(item, dict) and item.get("node_id") not in existing_ids
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"随机采样 memory candidates 失败：{exc}")

    random.shuffle(candidates)
    return candidates


async def collect_day_residue(
    event_history: list[Any],
    memory_candidates: list[dict[str, Any]],
    workspace: Path | None,
) -> list[DreamSeed]:
    """收集近期残留。"""
    now = time.time()
    recent_events = [
        event
        for event in event_history[-40:]
        if now - _event_timestamp(event) <= 24 * 60 * 60
    ]
    if not recent_events:
        return []

    ref_scores: dict[str, float] = {}
    snippets: list[str] = []
    for event in recent_events:
        etype = _event_type_value(event)
        content = _clean_text(_event_value(event, "content", ""))
        if etype in {"message", "heartbeat"} and content:
            snippets.append(content[:90])
        if etype == "tool_call":
            args = _event_value(event, "tool_args", {}) or {}
            if isinstance(args, dict):
                for key in ("path", "file_path", "source_path", "target_path"):
                    value = str(args.get(key) or "").strip()
                    normalized = _normalize_ref(value, workspace)
                    if normalized:
                        ref_scores[normalized] = ref_scores.get(normalized, 0.0) + 1.0

    top_refs = [ref for ref, _ in sorted(ref_scores.items(), key=lambda item: item[1], reverse=True)[:3]]
    memory_map = {str(item.get("file_path") or ""): item for item in memory_candidates}
    core_node_ids = [
        str(memory_map[ref].get("node_id") or "")
        for ref in top_refs
        if ref in memory_map and str(memory_map[ref].get("node_id") or "")
    ]
    recurrence = min(len(snippets) / 6.0, 1.0)
    score = 0.42 + recurrence * 0.18 + min(len(top_refs), 3) * 0.06
    return [
        DreamSeed(
            seed_id=f"seed_day_{uuid.uuid4().hex[:6]}",
            seed_type=DreamSeedType.DAY_RESIDUE.value,
            title="白天尚未散去的余波",
            summary="；".join(snippets[:2]) if snippets else "最近的消息、心跳和文件动作还挂在心里。",
            core_refs=top_refs,
            core_node_ids=core_node_ids,
            source_events=snippets[:3],
            affect_valence=0.05,
            affect_arousal=0.55,
            importance=0.45,
            novelty=0.2,
            recurrence=recurrence,
            unfinished_score=0.3,
            dreamability=0.68 if top_refs else 0.52,
            score=score,
            tension_reason="最近 24 小时内多次回流的内容，还没有完全沉下去。",
        )
    ]


async def collect_unfinished_tension(
    memory_candidates: list[dict[str, Any]],
    workspace: Path | None,
) -> list[DreamSeed]:
    """收集未完成张力。"""
    if workspace is None:
        return []

    from ..tools.todo_tools import TodoStatus, TodoStorage

    storage = TodoStorage(workspace)
    active_todos = [
        todo
        for todo in storage.load()
        if todo.status
        not in {
            TodoStatus.COMPLETED.value,
            TodoStatus.CANCELLED.value,
            TodoStatus.ARCHIVED.value,
        }
    ]
    if not active_todos:
        return []

    priority_weight = {
        "low": 0.25,
        "normal": 0.45,
        "high": 0.75,
        "urgent": 1.0,
    }
    status_weight = {
        "pending": 0.62,
        "in_progress": 0.48,
        "blocked": 0.8,
    }
    scored = sorted(
        active_todos,
        key=lambda todo: (
            priority_weight.get(str(getattr(todo, "priority", "") or ""), 0.45)
            + status_weight.get(str(todo.status or ""), 0.2)
        ),
        reverse=True,
    )
    chosen = scored[:2]
    if not chosen:
        return []

    notes = [f"{todo.title}（{todo.status}/{getattr(todo, 'priority', 'normal')}）" for todo in chosen]
    score = min(
        0.45
        + sum(priority_weight.get(str(getattr(todo, "priority", "") or ""), 0.45) for todo in chosen[:1]) * 0.35,
        0.96,
    )
    todo_ref = "todos.json"
    memory_map = {str(item.get("file_path") or ""): item for item in memory_candidates}
    core_node_ids = []
    if todo_ref in memory_map and str(memory_map[todo_ref].get("node_id") or ""):
        core_node_ids.append(str(memory_map[todo_ref].get("node_id") or ""))

    return [
        DreamSeed(
            seed_id=f"seed_todo_{uuid.uuid4().hex[:6]}",
            seed_type=DreamSeedType.UNFINISHED_TENSION.value,
            title="还没有真正推进完的承诺行动",
            summary="、".join(notes),
            core_refs=[f"todos.json#{todo.id}" for todo in chosen] + [todo_ref],
            core_node_ids=core_node_ids,
            source_events=notes,
            affect_valence=0.0,
            affect_arousal=0.72,
            importance=0.68,
            novelty=0.15,
            recurrence=min(len(chosen) / 2.0, 1.0),
            unfinished_score=0.92,
            dreamability=0.78,
            score=score,
            tension_reason="这些承诺行动还没有被真正推进、完成或放下。",
        )
    ]


async def collect_dream_lag(
    memory_candidates: list[dict[str, Any]],
    workspace: Path | None,
) -> list[DreamSeed]:
    """收集延迟记忆 — 经典梦滞后 + 远期闪回。"""
    if workspace is None:
        return []

    # 70% 经典梦滞后（4-8天），30% 远期闪回（14-90天）
    if random.random() < 0.3:
        min_age, max_age, optimal_age = 14, 90, 30
        seed_title = "很久以前的记忆碎片在深夜浮起"
        seed_reason = "远期记忆闪回——大脑在深层整合中偶尔翻出被遗忘的旧事。"
    else:
        min_age, max_age, optimal_age = 4, 8, 6
        seed_title = "几天前的材料悄悄回返"
        seed_reason = "这批材料不算最新，却带着延迟后的个人意义，适合在梦里回返。"

    candidates: list[tuple[float, Path]] = []
    for path in _iter_workspace_markdown_files(workspace):
        age_days = _file_age_days(path)
        if age_days is None or age_days < min_age or age_days > max_age:
            continue
        distance_penalty = abs(age_days - optimal_age)
        score = max(0.0, 1.0 - distance_penalty * 0.2)
        candidates.append((score, path))

    if not candidates:
        return []

    top_files = [path for _, path in sorted(candidates, key=lambda item: item[0], reverse=True)[:2]]
    memory_map = {str(item.get("file_path") or ""): item for item in memory_candidates}
    refs = [_normalize_ref(path.relative_to(workspace).as_posix(), workspace) for path in top_files]
    previews = [_read_preview(path, max_chars=90) for path in top_files]
    core_node_ids = [
        str(memory_map[ref].get("node_id") or "")
        for ref in refs
        if ref in memory_map and str(memory_map[ref].get("node_id") or "")
    ]
    return [
        DreamSeed(
            seed_id=f"seed_lag_{uuid.uuid4().hex[:6]}",
            seed_type=DreamSeedType.DREAM_LAG.value,
            title=seed_title,
            summary="；".join(preview for preview in previews if preview) or "前几天的内容在今晚绕了一圈回来。",
            core_refs=refs,
            core_node_ids=core_node_ids,
            source_events=[path.stem for path in top_files],
            affect_valence=0.02,
            affect_arousal=0.48,
            importance=0.55,
            novelty=0.5,
            recurrence=0.34,
            unfinished_score=0.4,
            dreamability=0.74,
            score=0.64,
            tension_reason=seed_reason,
        )
    ]


async def collect_self_theme(
    memory_candidates: list[dict[str, Any]],
) -> list[DreamSeed]:
    """收集长期自我主题。"""
    if not memory_candidates:
        return []

    sample_size = min(3, len(memory_candidates))
    top_nodes = random.sample(memory_candidates, sample_size)
    refs = [
        str(item.get("file_path") or "").strip()
        for item in top_nodes
        if str(item.get("file_path") or "").strip()
    ]
    titles = [
        str(item.get("title") or "").strip() or Path(ref).stem
        for item, ref in zip(top_nodes, refs, strict=False)
    ]
    core_node_ids = [
        str(item.get("node_id") or "")
        for item in top_nodes
        if str(item.get("node_id") or "")
    ]
    recurrence = min(sum(float(item.get("access_count") or 0) for item in top_nodes) / 10.0, 1.0)
    importance = min(sum(float(item.get("importance") or 0.0) for item in top_nodes[:2]) / 2.0, 1.0)
    summary = "、".join(title for title in titles if title) or "一些反复出现的主题仍在定义她是谁。"
    return [
        DreamSeed(
            seed_id=f"seed_theme_{uuid.uuid4().hex[:6]}",
            seed_type=DreamSeedType.SELF_THEME.value,
            title="那些总会回来的长期主题",
            summary=summary,
            core_refs=refs[:3],
            core_node_ids=core_node_ids[:3],
            source_events=titles[:3],
            affect_valence=0.08,
            affect_arousal=0.4,
            importance=importance,
            novelty=0.12,
            recurrence=recurrence,
            unfinished_score=0.28,
            dreamability=0.62,
            score=0.5 + importance * 0.28 + recurrence * 0.1,
            tension_reason="这些主题不是一时冲动，而是会反复构成自我感的底纹。",
        )
    ]


def select_seed_candidates(
    candidates: list[DreamSeed],
    recent_seed_titles: set[str] | None = None,
    repetition_decay: float = 0.3,
) -> list[DreamSeed]:
    """按类型优先 + 神经噪声选择最终种子。"""
    if not candidates:
        return []

    # 海马体重复抑制
    recent_titles = recent_seed_titles or set()
    for seed in candidates:
        if seed.title in recent_titles:
            seed.score = max(0.05, seed.score - repetition_decay)

    # 神经噪声
    scored: list[tuple[float, DreamSeed]] = []
    for seed in candidates:
        noise = random.gauss(0, _SEED_SCORE_TEMPERATURE)
        effective = max(0.01, seed.score + noise)
        scored.append((effective, seed))

    # 按类型分组
    by_type: dict[str, tuple[float, DreamSeed]] = {}
    for eff_score, seed in sorted(scored, key=lambda x: x[0], reverse=True):
        by_type.setdefault(seed.seed_type, (eff_score, seed))

    selected = [seed for _, seed in sorted(by_type.values(), key=lambda x: x[0], reverse=True)]

    if len(selected) >= _MAX_DREAM_SEEDS:
        return selected[:_MAX_DREAM_SEEDS]

    # 剩余名额：加权随机采样
    existing_ids = {s.seed_id for s in selected}
    remaining = [(eff, s) for eff, s in scored if s.seed_id not in existing_ids]
    if remaining:
        weights = [max(0.01, eff) for eff, _ in remaining]
        total = sum(weights)
        if total > 0:
            extra_count = min(_MAX_DREAM_SEEDS - len(selected), len(remaining))
            extras = random.choices(
                [s for _, s in remaining],
                weights=[w / total for w in weights],
                k=extra_count,
            )
            selected.extend(extras)

    return selected[:_MAX_DREAM_SEEDS]


async def collect_thought_stream_seeds(
    thought_manager: Any,
) -> list[DreamSeed]:
    """从活跃的思考流中收集入梦种子。

    思考流是爱莉持续在意的兴趣线索——它们带有未完成的张力，
    是天然的入梦材料。做梦可以巩固白天的思考，
    或在看似无关的记忆间发现新关联。
    """
    if thought_manager is None:
        return []

    try:
        active_streams = thought_manager.list_active()
    except Exception:  # noqa: BLE001
        return []

    if not active_streams:
        return []

    seeds: list[DreamSeed] = []
    for ts in active_streams[:3]:
        # 思考流的好奇心越高、推进次数越多 → 梦境价值越高
        curiosity = getattr(ts, "curiosity_score", 0.5)
        advance_count = getattr(ts, "advance_count", 0)
        last_thought = getattr(ts, "last_thought", "")
        title = getattr(ts, "title", "未命名的思考")
        ts_id = getattr(ts, "id", "unknown")

        # 未完成张力：好奇心高且推进次数少 = 正在琢磨但还没想通
        unfinished = curiosity * max(0.3, 1.0 - advance_count / 10.0)

        # 梦境价值：好奇心 + 未完成度 + 情感唤起
        dreamability = min(0.4 + curiosity * 0.3 + unfinished * 0.2, 1.0)

        seeds.append(DreamSeed(
            seed_id=f"seed_ts_{ts_id}_{uuid.uuid4().hex[:4]}",
            seed_type=DreamSeedType.UNFINISHED_TENSION.value,
            title=f"思考流: {title}",
            summary=last_thought[:200] if last_thought else f"关于「{title}」的持续思考",
            source_events=[f"thought_stream:{ts_id}"],
            affect_valence=curiosity * 0.2 - 0.1,
            affect_arousal=min(curiosity * 0.6, 0.8),
            importance=curiosity,
            novelty=max(0.1, 1.0 - advance_count / 5.0),
            recurrence=min(advance_count / 8.0, 1.0),
            unfinished_score=unfinished,
            dreamability=dreamability,
            score=dreamability * 0.7 + unfinished * 0.3,
            tension_reason=f"关于「{title}」的思考尚未完成，好奇心仍在。",
        ))

    return seeds


def collect_seed_node_ids(seeds: list[DreamSeed]) -> list[str]:
    """收集 REM 的主种子节点 ID。"""
    return _unique_preserve(
        node_id
        for seed in seeds
        for node_id in seed.core_node_ids
        if str(node_id or "").strip()
    )


# ============================================================
# 辅助函数
# ============================================================


def _normalize_ref(value: str, workspace: Path | None = None) -> str:
    """把 ref 规整为 workspace 相对路径。"""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if workspace is None:
        return raw.lstrip("/")
    path = Path(raw)
    try:
        if path.is_absolute():
            return path.resolve().relative_to(workspace).as_posix()
    except Exception:  # noqa: BLE001
        pass
    return raw.lstrip("/")


def _iter_workspace_markdown_files(workspace: Path) -> list[Path]:
    """遍历可作为梦材料的 markdown 文件。"""
    if workspace is None or not workspace.exists():
        return []

    roots = [
        workspace / "diary",
        workspace / "diaries",
        workspace / "notes",
    ]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(
            path
            for path in root.rglob("*.md")
            if path.is_file() and "dreams" not in path.parts
        )
    return files


def _file_age_days(path: Path) -> int | None:
    """推断文件距离现在多少天。"""
    now = datetime.now().astimezone().date()
    stem = path.stem
    match = _DATE_STEM_RE.match(stem)
    if match:
        try:
            target = datetime.strptime(match.group("date"), "%Y-%m-%d").date()
            return abs((now - target).days)
        except ValueError:
            pass

    month_match = _MONTH_STEM_RE.match(stem)
    if month_match:
        try:
            target = datetime.strptime(month_match.group("month"), "%Y-%m").date()
            return abs((now - target).days)
        except ValueError:
            pass

    try:
        file_date = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).astimezone().date()
        return abs((now - file_date).days)
    except Exception:  # noqa: BLE001
        return None


def _read_preview(path: Path, *, max_chars: int = 120) -> str:
    """读取文件简短预览。"""
    if not path.exists() or not path.is_file():
        return ""
    try:
        content = " ".join(path.read_text(encoding="utf-8").split())
    except Exception:  # noqa: BLE001
        return ""
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 1] + "…"
