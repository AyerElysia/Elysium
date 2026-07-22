"""做梦报告与余韵数据结构。

包含 NREM/REM 报告、完整梦报告、梦后余韵等数据类，
以及序列化/反序列化辅助函数。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .scenes import DreamTrace
    from .seeds import DreamSeed

from ..memory.eligibility import assess_workspace_document

logger = logging.getLogger("life_engine.dream")

# 常量
_MAX_SCENES = 5
_REMINDER_MAX_HISTORY = 20
_RESIDUE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class NREMReport:
    """NREM 回放阶段报告。"""

    episodes_replayed: int = 0
    total_steps: int = 0
    weight_before: dict[str, Any] = field(default_factory=dict)
    weight_after: dict[str, Any] = field(default_factory=dict)
    homeostatic_applied: bool = False


@dataclass
class REMReport:
    """REM 联想阶段报告。"""

    walk_rounds: int = 0
    nodes_activated: int = 0
    new_edges_created: int = 0
    edges_pruned: int = 0
    seed_node_ids: list[str] = field(default_factory=list)


@dataclass
class DreamResidue:
    """醒后余韵。"""

    summary: str = ""
    life_payload: str = ""
    dfc_payload: str = ""
    dominant_affect: str = ""
    strength: str = "light"
    tags: list[str] = field(default_factory=list)
    expires_at: float = 0.0


@dataclass
class DreamReport:
    """完整做梦周期报告。"""

    dream_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_seconds: float = 0.0
    nrem: NREMReport = field(default_factory=NREMReport)
    rem: REMReport = field(default_factory=REMReport)
    narrative: str = ""
    phase_sequence: list[str] = field(default_factory=list)
    seed_report: list["DreamSeed"] = field(default_factory=list)
    dream_trace: Any = None  # 延迟初始化为 DreamTrace
    dream_text: str = ""
    dream_residue: DreamResidue | None = None
    archive_path: str = ""
    memory_effects: dict[str, Any] = field(default_factory=dict)

    def get_trace(self) -> "DreamTrace":
        """获取或初始化 dream_trace。"""
        from .scenes import DreamTrace

        if self.dream_trace is None:
            self.dream_trace = DreamTrace()
        return self.dream_trace


# ============================================================
# 序列化辅助函数
# ============================================================


def _clean_text(value: Any) -> str:
    """清洗字符串。"""
    text = " ".join(str(value or "").split())
    return text.strip()


def _unique_preserve(items: Any) -> list[Any]:
    """按出现顺序去重。"""
    seen: set[Any] = set()
    result: list[Any] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _seed_to_dict(seed: "DreamSeed") -> dict[str, Any]:
    """将 DreamSeed 转成可序列化字典。"""
    return asdict(seed)


def _trace_to_dict(trace: Any) -> dict[str, Any]:
    """将 DreamTrace 转成可序列化字典。"""
    if trace is None:
        return {"scenes": [], "motifs": [], "transitions": []}
    return {
        "scenes": [
            {
                "title": scene.title,
                "summary": scene.summary,
                "imagery": list(scene.imagery),
                "emotion_shift": scene.emotion_shift,
                "refs": list(scene.refs),
            }
            for scene in trace.scenes
        ],
        "motifs": list(trace.motifs),
        "transitions": list(trace.transitions),
    }


def _residue_to_dict(residue: DreamResidue | None) -> dict[str, Any] | None:
    """将 DreamResidue 转为可序列化字典。"""
    if residue is None:
        return None
    return asdict(residue)


def _trace_from_payload(raw: Any) -> "DreamTrace":
    """从 payload 转换为 DreamTrace。"""
    from .scenes import DreamScene, DreamTrace

    if not isinstance(raw, dict):
        return DreamTrace()
    scenes_raw = raw.get("scenes")
    scenes: list[DreamScene] = []
    if isinstance(scenes_raw, list):
        for item in scenes_raw[:_MAX_SCENES]:
            if not isinstance(item, dict):
                continue
            scenes.append(
                DreamScene(
                    title=_clean_text(item.get("title", "")),
                    summary=_clean_text(item.get("summary", "")),
                    imagery=[_clean_text(v) for v in item.get("imagery", []) if _clean_text(v)],
                    emotion_shift=_clean_text(item.get("emotion_shift", "")),
                    refs=[_clean_text(v) for v in item.get("refs", []) if _clean_text(v)],
                )
            )
    return DreamTrace(
        scenes=scenes,
        motifs=[_clean_text(v) for v in raw.get("motifs", []) if _clean_text(v)],
        transitions=[_clean_text(v) for v in raw.get("transitions", []) if _clean_text(v)],
    )


def _residue_from_payload(raw: Any) -> DreamResidue:
    """从 payload 转换为 DreamResidue。"""
    if not isinstance(raw, dict):
        return DreamResidue()
    return DreamResidue(
        summary=_clean_text(raw.get("summary", "")),
        life_payload=_clean_text(raw.get("life_payload", "")),
        dfc_payload=_clean_text(raw.get("dfc_payload", "")),
        dominant_affect=_clean_text(raw.get("dominant_affect", "")),
        strength=_clean_text(raw.get("strength", "light")) or "light",
        tags=[_clean_text(tag) for tag in raw.get("tags", []) if _clean_text(tag)],
        expires_at=float(raw.get("expires_at", 0.0) or 0.0),
    )


def _compute_weight_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float]:
    """计算权重变化摘要。"""
    if not before or not after:
        return {}
    delta: dict[str, float] = {}
    for layer in before:
        if layer not in after:
            continue
        b = before[layer]
        a = after[layer]
        delta[f"{layer}_mean_delta"] = round(a.get("w_mean", 0) - b.get("w_mean", 0), 6)
        delta[f"{layer}_norm_delta"] = round(a.get("w_norm", 0) - b.get("w_norm", 0), 6)
    return delta


def _report_to_dict(report: DreamReport) -> dict[str, Any]:
    """将 DreamReport 转为可序列化字典。"""
    return {
        "dream_id": report.dream_id,
        "started_at": report.started_at,
        "ended_at": report.ended_at,
        "duration_seconds": round(report.duration_seconds, 1),
        "nrem": {
            "episodes_replayed": report.nrem.episodes_replayed,
            "total_steps": report.nrem.total_steps,
            "homeostatic_applied": report.nrem.homeostatic_applied,
            "weight_delta": _compute_weight_delta(report.nrem.weight_before, report.nrem.weight_after),
        },
        "rem": {
            "walk_rounds": report.rem.walk_rounds,
            "nodes_activated": report.rem.nodes_activated,
            "new_edges_created": report.rem.new_edges_created,
            "edges_pruned": report.rem.edges_pruned,
            "seed_node_ids": list(report.rem.seed_node_ids),
        },
        "narrative": report.narrative,
        "dream_text": report.dream_text,
        "phases": list(report.phase_sequence),
        "seed_report": [_seed_to_dict(seed) for seed in report.seed_report],
        "dream_trace": _trace_to_dict(report.dream_trace),
        "dream_residue": _residue_to_dict(report.dream_residue),
        "archive_path": report.archive_path,
        "memory_effects": dict(report.memory_effects),
    }


def _report_from_dict(raw: dict[str, Any]) -> DreamReport:
    """从字典恢复 DreamReport。"""
    from .seeds import DreamSeed

    seed_report = []
    seeds_raw = raw.get("seed_report")
    if isinstance(seeds_raw, list):
        for item in seeds_raw:
            if isinstance(item, dict):
                seed_report.append(
                    DreamSeed(
                        seed_id=str(item.get("seed_id") or ""),
                        seed_type=str(item.get("seed_type") or ""),
                        title=str(item.get("title") or ""),
                        summary=str(item.get("summary") or ""),
                        core_refs=list(item.get("core_refs") or []),
                        supporting_refs=list(item.get("supporting_refs") or []),
                        core_node_ids=list(item.get("core_node_ids") or []),
                        source_events=list(item.get("source_events") or []),
                        affect_valence=float(item.get("affect_valence") or 0.0),
                        affect_arousal=float(item.get("affect_arousal") or 0.0),
                        importance=float(item.get("importance") or 0.0),
                        novelty=float(item.get("novelty") or 0.0),
                        recurrence=float(item.get("recurrence") or 0.0),
                        unfinished_score=float(item.get("unfinished_score") or 0.0),
                        dreamability=float(item.get("dreamability") or 0.0),
                        score=float(item.get("score") or 0.0),
                        tension_reason=str(item.get("tension_reason") or ""),
                    )
                )

    rem_raw = raw.get("rem") or {}
    nrem_raw = raw.get("nrem") or {}
    return DreamReport(
        dream_id=str(raw.get("dream_id") or ""),
        started_at=float(raw.get("started_at") or 0.0),
        ended_at=float(raw.get("ended_at") or 0.0),
        duration_seconds=float(raw.get("duration_seconds") or 0.0),
        nrem=NREMReport(
            episodes_replayed=int(nrem_raw.get("episodes_replayed") or 0),
            total_steps=int(nrem_raw.get("total_steps") or 0),
            homeostatic_applied=bool(nrem_raw.get("homeostatic_applied", False)),
        ),
        rem=REMReport(
            walk_rounds=int(rem_raw.get("walk_rounds") or 0),
            nodes_activated=int(rem_raw.get("nodes_activated") or 0),
            new_edges_created=int(rem_raw.get("new_edges_created") or 0),
            edges_pruned=int(rem_raw.get("edges_pruned") or 0),
            seed_node_ids=list(rem_raw.get("seed_node_ids") or []),
        ),
        narrative=str(raw.get("narrative") or ""),
        phase_sequence=list(raw.get("phases") or []),
        seed_report=seed_report,
        dream_trace=_trace_from_payload(raw.get("dream_trace")),
        dream_text=str(raw.get("dream_text") or ""),
        dream_residue=_residue_from_payload(raw.get("dream_residue") or {}),
        archive_path=str(raw.get("archive_path") or ""),
        memory_effects=dict(raw.get("memory_effects") or {}),
    )


def _parse_json_payload(text: str) -> dict[str, Any]:
    """从模型返回中提取 JSON 对象。"""
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if "\n" in stripped:
            stripped = stripped.split("\n", 1)[1]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except Exception:  # noqa: BLE001
        pass

    match = _JSON_BLOCK_RE.search(stripped)
    if not match:
        raise ValueError("未找到 JSON 块")

    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("JSON 顶层不是对象")
    return parsed


# ============================================================
# 梦札归档函数
# ============================================================


async def archive_dream(
    report: DreamReport,
    workspace: Path,
    dream_trace: "DreamTrace",
) -> str:
    """将梦写入 Markdown 梦札。

    Args:
        report: 做梦报告
        workspace: 工作空间路径
        dream_trace: 梦迹对象

    Returns:
        归档文件相对路径
    """
    _DREAM_ARCHIVE_DIR = "dreams"

    started = datetime.fromtimestamp(report.started_at or time.time(), tz=timezone.utc).astimezone()
    archive_dir = workspace / _DREAM_ARCHIVE_DIR / started.strftime("%Y-%m-%d")
    archive_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{started.strftime('%H%M')}_{report.dream_id}.md"
    archive_path = archive_dir / file_name

    all_refs = _unique_preserve(
        ref
        for seed in report.seed_report
        for ref in (list(seed.core_refs) + list(seed.supporting_refs))
        if str(ref or "").strip()
    )
    motifs = dream_trace.motifs[:8]
    tags = report.dream_residue.tags[:8] if report.dream_residue else []

    frontmatter_lines = [
        "---",
        f'dream_id: "{report.dream_id}"',
        f'created_at: "{started.isoformat()}"',
        f'seed_types: [{", ".join(seed.seed_type for seed in report.seed_report)}]',
        "core_refs:",
    ]
    if all_refs:
        frontmatter_lines.extend([f"  - {ref}" for ref in all_refs[:12]])
    else:
        frontmatter_lines.append("  - ")
    frontmatter_lines.append("trace_tags:")
    if tags:
        frontmatter_lines.extend([f"  - {tag}" for tag in tags])
    else:
        frontmatter_lines.append("  - ")
    frontmatter_lines.append(
        f'dominant_affect: "{(report.dream_residue.dominant_affect if report.dream_residue else "")}"'
    )
    frontmatter_lines.append(
        f'residue_strength: "{(report.dream_residue.strength if report.dream_residue else "light")}"'
    )
    frontmatter_lines.append("motifs:")
    if motifs:
        frontmatter_lines.extend([f"  - {motif}" for motif in motifs])
    else:
        frontmatter_lines.append("  - ")
    frontmatter_lines.append("---")

    body_lines = [
        "# 梦札",
        "",
        report.dream_text or report.narrative or "今夜只留下了很淡的影子。",
        "",
        "## 梦核",
    ]
    if report.seed_report:
        for seed in report.seed_report:
            body_lines.append(
                f"- {seed.title}（{seed.seed_type}，score={seed.score:.2f}）："
                f"{seed.tension_reason or seed.summary}"
            )
    else:
        body_lines.append("- 今夜的梦更接近一种无名的整理。")

    body_lines.extend(["", "## 场景流"])
    if dream_trace.scenes:
        for index, scene in enumerate(dream_trace.scenes[:_MAX_SCENES], start=1):
            imagery = "、".join(scene.imagery[:4]) if scene.imagery else "无"
            body_lines.append(
                f"{index}. **{scene.title or f'场景{index}'}**：{scene.summary}（意象：{imagery}）"
            )
    else:
        body_lines.append("1. 今夜的场景没有留下足够清晰的片段。")

    if report.dream_residue is not None:
        body_lines.extend(
            [
                "",
                "## 醒后余韵",
                "",
                report.dream_residue.summary,
                "",
                report.dream_residue.life_payload,
            ]
        )

    if report.memory_effects:
        body_lines.extend(
            [
                "",
                "## 记忆变化",
                "",
                f"- 关联节点：{report.memory_effects.get('linked_refs', 0)}",
                f"- 梦后写入：{report.memory_effects.get('archive_written', False)}",
            ]
        )

    archive_path.write_text(
        "\n".join(frontmatter_lines + [""] + body_lines).strip() + "\n",
        encoding="utf-8",
    )
    return archive_path.relative_to(workspace).as_posix()


async def _index_memory_document(
    memory_service: Any,
    path: str,
    content: str,
    *,
    title: str,
    source_mtime: float,
) -> Any:
    """Write a document through the current index API, with legacy fallback."""
    upsert_document = getattr(memory_service, "upsert_document", None)
    if callable(upsert_document):
        result = upsert_document(
            path,
            content,
            title=title,
            source_mtime=source_mtime,
        )
        if inspect.isawaitable(result):
            result = await result
        node_id = getattr(result, "node_id", None)
        if node_id:
            return result
        if isinstance(result, dict) and result.get("node_id"):
            return SimpleNamespace(node_id=str(result["node_id"]))
        return None

    get_or_create_file_node = getattr(memory_service, "get_or_create_file_node", None)
    if not callable(get_or_create_file_node):
        return None
    result = get_or_create_file_node(path, title=title, content=content)
    if inspect.isawaitable(result):
        result = await result
    return result if getattr(result, "node_id", None) else None


async def integrate_archive_into_memory(
    report: DreamReport,
    workspace: Path,
    memory_service: Any,
    seed_report: list["DreamSeed"],
) -> dict[str, Any]:
    """将梦札接入文件系统记忆。"""
    if memory_service is None or workspace is None or not report.archive_path:
        return {}

    from ..memory.edges import EdgeType

    archive_eligibility = assess_workspace_document(workspace, report.archive_path)
    if not archive_eligibility.eligible:
        return {"archive_written": False, "linked_refs": 0}
    archive_path = archive_eligibility.path
    abs_archive_path = workspace / archive_path
    try:
        dream_content = abs_archive_path.read_text(encoding="utf-8", errors="replace")
        archive_mtime = abs_archive_path.stat().st_mtime
    except OSError:
        return {"archive_written": False, "linked_refs": 0}

    dream_node = await _index_memory_document(
        memory_service,
        archive_path,
        dream_content,
        title=f"梦札 {Path(archive_path).stem}",
        source_mtime=archive_mtime,
    )
    if dream_node is None:
        return {"archive_written": False, "linked_refs": 0}

    linked_refs = 0
    linked_paths: list[str] = []
    for ref in _iter_seed_file_refs(seed_report, workspace):
        ref_eligibility = assess_workspace_document(workspace, ref)
        if not ref_eligibility.eligible:
            continue
        ref = ref_eligibility.path
        ref_path = workspace / ref
        try:
            ref_content = ref_path.read_text(encoding="utf-8", errors="replace")
            ref_mtime = ref_path.stat().st_mtime
        except OSError:
            continue
        ref_node = await _index_memory_document(
            memory_service,
            ref,
            ref_content,
            title=ref_path.stem,
            source_mtime=ref_mtime,
        )
        if ref_node is None:
            continue
        await memory_service.create_or_update_edge(
            dream_node.node_id,
            ref_node.node_id,
            EdgeType.RELATES,
            reason="梦境来源与回响",
            strength=0.58,
            bidirectional=True,
        )
        linked_refs += 1
        linked_paths.append(ref)

    return {
        "archive_written": True,
        "archive_path": archive_path,
        "archive_node_id": dream_node.node_id,
        "linked_refs": linked_refs,
        "linked_paths": linked_paths,
    }


def _iter_seed_file_refs(seeds: list["DreamSeed"], workspace: Path) -> list[str]:
    """返回可映射到文件系统的 refs。"""
    refs: list[str] = []
    for seed in seeds:
        for ref in list(seed.core_refs) + list(seed.supporting_refs):
            raw = str(ref or "").strip()
            if not raw:
                continue
            path_part = raw.split("#", 1)[0].strip()
            normalized = _normalize_ref(path_part, workspace)
            if normalized:
                refs.append(normalized)
    return _unique_preserve(refs)


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