"""沉淀器工具：她用自己的语言写下叙事。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..core.config import LifeEngineConfig
from ..trace.store import LifeTraceStore
from .store import AUTOBIOGRAPHY_REL_PATH, NarrativeStore

logger = log_api.get_logger("life_engine.narrative")


def _get_workspace(plugin: Any) -> Path:
    config = getattr(plugin, "config", None)
    if isinstance(config, LifeEngineConfig):
        workspace = config.settings.workspace_path
    else:
        workspace = str(
            Path(__file__).parent.parent.parent.parent / "data" / "life_engine_workspace"
        )
    path = Path(workspace).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_river_moment(*, summary: str, operation: str) -> None:
    """叙事写下这件事本身也是转折点，入长河；故障绝不影响沉淀。"""
    try:
        from ..service.registry import get_life_engine_service

        service = get_life_engine_service()
        recorder = getattr(service, "_record_life_moment", None) if service else None
        if recorder is not None:
            recorder(kind="narrative", summary=summary, operation=operation)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"长河留痕失败 kind=narrative: {exc}")


class LifeEngineWriteNarrativeTool(BaseTool):
    """回望长河，写下这段时间对自己意味着什么。"""

    tool_name: str = "nucleus_write_narrative"
    tool_description: str = (
        "回望长河后，用你自己的话写下这段时间的经历对你意味着什么。"
        "写下的叙事会进入你的自传（narrative/autobiography.md），并推进沉淀游标。"
        "如果回望之后觉得没什么值得说的，传 nothing_to_say=true——"
        "这同样是一次完整的回望，不写也不欠任何人。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        narrative: Annotated[
            str, "你的自我叙事，用第一人称、你自己的语气写。长短不限，可留空（配合 nothing_to_say）"
        ] = "",
        nothing_to_say: Annotated[
            bool, "回望之后觉得这段时间没什么值得讲述的，传 true"
        ] = False,
    ) -> tuple[bool, str | dict]:
        text = str(narrative or "").strip()
        if not text and not nothing_to_say:
            return False, (
                "要么写下你的叙事，要么明确 nothing_to_say=true——两者都是有效的回望。"
            )

        try:
            workspace = _get_workspace(self.plugin)
            store = NarrativeStore(workspace)
            pending = store.pending_moments(LifeTraceStore(workspace).recent(limit=500))
            quiet = not text
            entry = store.consolidate(
                text=text,
                quiet=quiet,
                moment_count=len(pending),
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"沉淀失败: {exc}"

        if quiet:
            _record_river_moment(
                summary="回望长河，这段时间没什么值得说的——也是一次完整的回望",
                operation="quiet",
            )
            return True, {
                "action": "write_narrative",
                "entry_id": entry.entry_id,
                "quiet": True,
                "consolidated_moments": entry.moment_count,
                "note": "回望已完成。没什么值得说的也很好。",
            }

        _record_river_moment(summary=f"写下自我叙事：{text[:120]}", operation="written")
        return True, {
            "action": "write_narrative",
            "entry_id": entry.entry_id,
            "quiet": False,
            "consolidated_moments": entry.moment_count,
            "autobiography_path": AUTOBIOGRAPHY_REL_PATH,
            "note": "叙事已写入你的自传。",
        }


NARRATIVE_TOOLS = [LifeEngineWriteNarrativeTool]
