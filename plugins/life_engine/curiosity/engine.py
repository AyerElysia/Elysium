"""同主体异步好奇层。

这个模块只维护“值得靠近的疑点/刺点”，不直接行动、不发消息、不调用工具。
它的输出作为 life_chatter 的 transient suffix 被主体看到，由表达层自行决定
是否追问、观察、搜索或放下。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json_repair

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import LLMPayload, ROLE, Text


CURIOUSNESS_GUIDE = """## 什么是值得好奇
- 好奇不是“未知信息”的待办清单，而是主体看到某处没有完全闭合时产生的靠近倾向。
- 值得好奇的东西通常有这些特征：表层说法和深层意味不一致、预期和实际反应不一致、反复出现却没有被理解、用户在暗示但没有说透、对象有可被再次观察的细节。
- 不值得好奇的东西：已经足够明确的问候、纯命令执行、无关噪声、只会把主体拖进机械服务状态的细枝末节。
- 你只负责判断“这里有没有一个值得主体注意的刺点”。不要替表达层写回复，不要命令使用工具。
- 如果有刺点，要写成轻量牵引：它为什么让你想再看一眼、还有哪里没闭合、可以如何靠近。"""


@dataclass(slots=True)
class CuriositySignal:
    """当前好奇牵引。"""

    active: bool = False
    anchor: str = ""
    why: str = ""
    unknown: str = ""
    approach: str = ""
    updated_at: str = ""
    source_event_id: str = ""
    source_stream_id: str = ""
    confidence: float = 0.0
    tags: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "CuriositySignal":
        return cls(active=False, updated_at=_now_iso())

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "CuriositySignal":
        tags_raw = data.get("tags")
        tags = [str(item).strip() for item in tags_raw if str(item).strip()] if isinstance(tags_raw, list) else []
        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            active=bool(data.get("active", False)),
            anchor=str(data.get("anchor", "") or "").strip(),
            why=str(data.get("why", "") or "").strip(),
            unknown=str(data.get("unknown", "") or "").strip(),
            approach=str(data.get("approach", "") or "").strip(),
            updated_at=str(data.get("updated_at", "") or "").strip() or _now_iso(),
            source_event_id=str(data.get("source_event_id", "") or "").strip(),
            source_stream_id=str(data.get("source_stream_id", "") or "").strip(),
            confidence=max(0.0, min(confidence, 1.0)),
            tags=tags[:6],
        )

    def normalized(self) -> "CuriositySignal":
        if not self.active:
            return CuriositySignal.empty()
        if not (self.anchor or self.why or self.unknown):
            return CuriositySignal.empty()
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CuriosityEngine:
    """异步好奇代理。

    代理使用与 life_chatter 同源的身份前缀和统一历史，但只写入一个短暂的
    好奇牵引缓存。主流程读取缓存，不等待代理完成。
    """

    def __init__(
        self,
        *,
        workspace_path: str,
        model_task_name: str = "life",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.workspace_path = str(workspace_path or "")
        self.model_task_name = str(model_task_name or "life")
        self.timeout_seconds = max(3.0, float(timeout_seconds or 30.0))
        self._lock = asyncio.Lock()

    @property
    def state_path(self) -> Path:
        workspace = Path(self.workspace_path).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace / "curiosity_state.json"

    async def load_signal(self) -> CuriositySignal:
        async with self._lock:
            return self._load_signal_unlocked()

    async def save_signal(self, signal: CuriositySignal) -> None:
        async with self._lock:
            self._save_signal_unlocked(signal.normalized())

    async def clear(self) -> None:
        await self.save_signal(CuriositySignal.empty())

    async def review(
        self,
        *,
        prefix_prompt: str,
        history_text: str,
        new_event_text: str,
        source_event_id: str = "",
        source_stream_id: str = "",
    ) -> CuriositySignal:
        """运行一次异步好奇判断，并持久化结果。"""

        previous = await self.load_signal()
        system_prompt = self._build_system_prompt(prefix_prompt)
        user_prompt = self._build_user_prompt(
            history_text=history_text,
            new_event_text=new_event_text,
            previous=previous,
        )

        request = create_llm_request(
            get_model_set_by_task(self.model_task_name),
            request_name="life_curiosity",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        response = await asyncio.wait_for(
            request.send(auto_append_response=False, stream=False),
            timeout=self.timeout_seconds,
        )
        raw_text = await asyncio.wait_for(response, timeout=self.timeout_seconds)
        signal = self._parse_signal(
            raw_text,
            source_event_id=source_event_id,
            source_stream_id=source_stream_id,
        )
        await self.save_signal(signal)
        return signal

    async def format_for_prompt(self, *, max_chars: int = 1200) -> str:
        signal = await self.load_signal()
        text = format_curiosity_signal(signal)
        if max_chars > 0 and len(text) > max_chars:
            return text[: max_chars - 1].rstrip() + "…"
        return text

    def _load_signal_unlocked(self) -> CuriositySignal:
        path = self.state_path
        if not path.exists():
            return CuriositySignal.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return CuriositySignal.empty()
        if not isinstance(raw, dict):
            return CuriositySignal.empty()
        return CuriositySignal.from_mapping(raw).normalized()

    def _save_signal_unlocked(self, signal: CuriositySignal) -> None:
        path = self.state_path
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(signal.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    @staticmethod
    def _build_system_prompt(prefix_prompt: str) -> str:
        base = str(prefix_prompt or "").strip()
        parts = []
        if base:
            parts.append(base)
        parts.append(
            "## 异步好奇过程\n"
            "你仍然是同一个爱莉希雅，不是外部评审、不是监督器、不是任务规划器。"
            "你此刻只负责在后台轻轻判断：最近的统一事件流里，是否有一个值得自己好奇的刺点。"
        )
        parts.append(CURIOUSNESS_GUIDE)
        parts.append(
            "## 输出格式\n"
            "只输出 JSON，不要输出解释性正文：\n"
            "{\n"
            '  "active": true/false,\n'
            '  "anchor": "刺点锚点，20字以内",\n'
            '  "why": "为什么这个地方让主体想再看一眼",\n'
            '  "unknown": "还没有闭合的地方",\n'
            '  "approach": "如果主体愿意，怎样轻轻靠近；不要写命令",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "tags": ["可选标签"]\n'
            "}\n"
            "如果没有值得好奇的刺点，active=false，其余字段留空。"
        )
        return "\n\n".join(parts)

    @staticmethod
    def _build_user_prompt(
        *,
        history_text: str,
        new_event_text: str,
        previous: CuriositySignal,
    ) -> str:
        previous_text = format_curiosity_signal(previous) or "（暂无未闭合好奇牵引）"
        history = str(history_text or "").strip() or "（暂无最近聊天历史）"
        event_text = str(new_event_text or "").strip() or "（暂无新增事件）"
        return (
            "<previous_curiosity>\n"
            f"{previous_text}\n"
            "</previous_curiosity>\n\n"
            "<chat_history>\n"
            f"{history}\n"
            "</chat_history>\n\n"
            "<new_event>\n"
            f"{event_text}\n"
            "</new_event>\n\n"
            "请判断现在是否有一个值得同一个主体保留的好奇刺点。"
        )

    @staticmethod
    def _parse_signal(
        raw_text: str,
        *,
        source_event_id: str,
        source_stream_id: str,
    ) -> CuriositySignal:
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except Exception:
            parsed = json_repair.repair_json(raw_text, return_objects=True)
        if not isinstance(parsed, dict):
            return CuriositySignal.empty()
        parsed["updated_at"] = _now_iso()
        parsed["source_event_id"] = source_event_id
        parsed["source_stream_id"] = source_stream_id
        return CuriositySignal.from_mapping(parsed).normalized()


def format_curiosity_signal(signal: CuriositySignal) -> str:
    """格式化为可注入 suffix 的短文本。"""

    signal = signal.normalized()
    if not signal.active:
        return ""
    lines = [
        "### 好奇牵引",
        "这是同一主体的异步好奇过程留下的轻量观察，不是命令；是否靠近由你自己决定。",
    ]
    if signal.anchor:
        lines.append(f"- 刺点：{signal.anchor}")
    if signal.why:
        lines.append(f"- 牵引：{signal.why}")
    if signal.unknown:
        lines.append(f"- 未闭合：{signal.unknown}")
    if signal.approach:
        lines.append(f"- 可轻轻靠近：{signal.approach}")
    if signal.tags:
        lines.append(f"- 标签：{'、'.join(signal.tags)}")
    return "\n".join(lines).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
