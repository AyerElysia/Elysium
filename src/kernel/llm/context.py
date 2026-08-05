"""LLM context management utilities."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from src.core.prompt import SystemReminderConsumeType, SystemReminderInsertType

from .exceptions import LLMContextError
from .payload import LLMPayload
from .payload.content import Content, Text
from .payload.tooling import LLMUsable, ToolCall, ToolResult
from .roles import ROLE

CompressionHook = Callable[[list[list[LLMPayload]], list[LLMPayload]], list[LLMPayload]]
TokenCounter = Callable[[list[LLMPayload]], int]
_TASK_CONTEXT_TRUNCATION_MARKER = (
    "\n\n[... context omitted to fit the task token budget ...]\n\n"
)


@dataclass(slots=True, frozen=True)
class RegisteredReminder:
    """登记到上下文管理器中的 reminder。"""

    text: str
    insert_type: SystemReminderInsertType
    consume_type: SystemReminderConsumeType = SystemReminderConsumeType.FOREVER
    source_key: tuple[str, str, str] | None = None


@dataclass(slots=True)
class RegisteredReminderSource:
    """登记到上下文管理器中的动态 reminder 源。"""

    bucket: str
    names: tuple[str, ...] | None
    wrap_with_system_tag: bool
    last_rendered_texts: tuple[str, ...] = ()


@dataclass(slots=True)
class LLMContextManager:
    """上下文管理器。

    默认职责：
    1. 接管 payload 列表写入（add_payload/system/tool）；
    2. 接管 reminder 的延迟登记；
    3. 在写入后执行结构校验（strict，不做自动修复）；
    4. 发送前按模型 token budget 执行裁剪。

    对于 reminder：固定注入为前置 SYSTEM 块（靠前且稳定），
    每次注入前会先清理旧 reminder，避免在上下文中重复堆积。
    """

    compression_hook: CompressionHook | None = None
    _reminders: list[RegisteredReminder] | None = None
    _reminder_sources: list[RegisteredReminderSource] | None = None
    _consumed_once_reminder_keys: set[tuple[str, str, str]] = field(default_factory=set)

    def validate_for_send(self, payloads: list[LLMPayload]) -> None:
        """在发起 LLM 请求前校验上下文结构。

        不允许任何未闭合的 tool 调用链（包括尾部）。
        """

        self._validate_payloads(payloads, allow_incomplete_tail=False)

    def add_payload(
        self,
        payloads: list[LLMPayload],
        payload: LLMPayload,
        position: int | None = None,
    ) -> list[LLMPayload]:
        """向上下文追加 payload，并进行规范化与裁剪。

        Args:
            payloads: 现有 payload 列表。
            payload: 待追加 payload。
            position: 可选插入位置。

        Returns:
            list[LLMPayload]: 规范化后的 payload 列表。
        """

        updated = list(payloads)

        if position is not None:
            updated.insert(int(position), payload)
        elif updated and updated[-1].role == payload.role:
            if payload.role == ROLE.TOOL_RESULT:
                # 合并相邻 TOOL_RESULT 时去重：跳过 call_id 已在末尾 payload 中存在的条目，
                # 防止浅拷贝导致同一 ToolResult 对象被 extend 两次引发 LLMContextError。
                existing_ids = {
                    str(p.call_id)
                    for p in updated[-1].content
                    if isinstance(p, ToolResult) and p.call_id
                }
                new_parts = [
                    p for p in payload.content
                    if not (isinstance(p, ToolResult) and str(p.call_id) in existing_ids)
                ]
                if new_parts:
                    updated[-1].content.extend(new_parts)
            else:
                updated[-1].content.extend(payload.content)
        else:
            updated.append(payload)

        updated = self._apply_reminders(updated)
        trimmed = self.maybe_trim(updated)

        # strict：不做自动修复，但要尽早暴露“非尾部”的链路错误。
        # 允许“尾部未闭合”的中间态（例如 assistant(tool_calls) 刚写入，tool_result 还没追加）。
        self._validate_payloads(trimmed, allow_incomplete_tail=True)

        return trimmed

    def _validate_payloads(self, payloads: list[LLMPayload], *, allow_incomplete_tail: bool) -> None:
        """校验 payloads 是否满足 OpenAI 兼容 messages 的基本结构约束。

        约束（对对话消息 USER/ASSISTANT/TOOL_RESULT 生效；SYSTEM/TOOL 作为 pinned 不参与链路判断）：
        - TOOL_RESULT 必须紧随带 tool_calls 的 ASSISTANT 之后；
        - 若 ASSISTANT 含 tool_calls，则必须补齐所有对应 call_id 的 TOOL_RESULT；
        - TOOL_RESULT 之后可以由 ASSISTANT 承接，也可以进入下一条 USER。

        Args:
            payloads: 待校验的 payload 列表。
            allow_incomplete_tail: 是否允许“尾部未闭合”的中间态。
                - True：允许末尾是 ASSISTANT(tool_calls) 或 TOOL_RESULT（等待后续补齐）。
                - False：必须完整闭合。
        """

        pinned_roles = {ROLE.SYSTEM, ROLE.TOOL}
        convo = [p for p in payloads if p.role not in pinned_roles]

        def _err(message: str) -> None:
            roles = [p.role.value for p in convo]
            raise LLMContextError(f"LLM 上下文不合法: {message}; roles={roles}")

        idx = 0
        while idx < len(convo):
            payload = convo[idx]

            if payload.role == ROLE.USER:
                idx += 1
                continue

            if payload.role == ROLE.ASSISTANT:
                if idx == 0:
                    _err("对话不能以 assistant 开始")
                prev_role = convo[idx - 1].role
                if prev_role not in {ROLE.USER, ROLE.TOOL_RESULT}:
                    _err("assistant 前必须是 user 或 tool_result")

                tool_calls = [part for part in payload.content if isinstance(part, ToolCall)]
                if not tool_calls:
                    idx += 1
                    continue

                expected_ids: set[str] = set()
                for part in tool_calls:
                    if not part.id:
                        # 自动补全缺失的 ID 并保持同步
                        object.__setattr__(part, "id", f"call_{uuid.uuid4().hex[:8]}")
                    expected_ids.add(str(part.id))

                j = idx + 1
                if j >= len(convo):
                    if allow_incomplete_tail:
                        return
                    _err("assistant(tool_calls) 后缺少 tool_result")

                seen: set[str] = set()
                while j < len(convo) and convo[j].role == ROLE.TOOL_RESULT:
                    results = [part for part in convo[j].content if isinstance(part, ToolResult)]
                    if not results:
                        _err("tool_result payload 中缺少 ToolResult 内容")
                    for result in results:
                        if not result.call_id:
                            _err("ToolResult 缺少 call_id")
                        call_id = str(result.call_id)
                        if call_id not in expected_ids:
                            _err(f"ToolResult.call_id={call_id} 不匹配任何 tool_call")
                        if call_id in seen:
                            _err(f"重复的 ToolResult.call_id={call_id}")
                        seen.add(call_id)
                    j += 1

                missing = expected_ids - seen
                if missing:
                    if allow_incomplete_tail and j >= len(convo):
                        return
                    _err(f"tool_result 未覆盖全部 tool_call: missing={sorted(missing)}")

                # tool_result 作为尾部是合法且常见的（下一条 assistant 将由本次请求生成）。
                # 如果工具执行期间插入了新用户消息，也允许直接进入 USER，让调用方及时处理新输入。
                if j < len(convo) and convo[j].role not in {ROLE.ASSISTANT, ROLE.USER}:
                    _err("tool_result 后只能是 assistant、user 或结束")

                idx = j
                continue

            if payload.role == ROLE.TOOL_RESULT:
                # 孤立 tool_result：一定非法（是否允许尾部取决于前面是否有 tool_calls）
                _err("孤立的 tool_result（未紧随 assistant.tool_calls）")

            _err(f"未知的对话角色: {payload.role}")

    def system(
        self,
        payloads: list[LLMPayload],
        content: Content | LLMUsable | list[Content | LLMUsable],
        position: int | None = None,
    ) -> list[LLMPayload]:
        """追加 SYSTEM payload，语义等同于 add_payload。"""

        return self.add_payload(
            payloads,
            LLMPayload(ROLE.SYSTEM, content),
            position=position,
        )

    def tool(
        self,
        payloads: list[LLMPayload],
        content: Content | LLMUsable | list[Content | LLMUsable],
        position: int | None = None,
    ) -> list[LLMPayload]:
        """追加 TOOL payload，语义等同于 add_payload。"""

        return self.add_payload(
            payloads,
            LLMPayload(ROLE.TOOL, content),
            position=position,
        )

    def reminder(
        self,
        content: str | Text | list[str | Text],
        *,
        insert_type: SystemReminderInsertType = SystemReminderInsertType.FIXED,
        wrap_with_system_tag: bool = False,
    ) -> None:
        """仅登记 reminder，不立即注入到 payload 列表。

        reminder 作为仅次于 system 的固有提示词，不单独作为 role 出现。
        真正的注入会在后续 add_payload/system/tool 时由 _apply_reminders 完成。
        """

        items = content if isinstance(content, list) else [content]
        if self._reminders is None:
            self._reminders = []

        for item in items:
            text = item.text if isinstance(item, Text) else str(item)
            if wrap_with_system_tag:
                text = (
                    "<system_reminder>\n"
                    f"{text}\n"
                    "</system_reminder>"
                )
            self._reminders.append(
                RegisteredReminder(text=text, insert_type=insert_type)
            )

    def update_reminder(
        self,
        content: str | Text | list[str | Text],
        *,
        wrap_with_system_tag: bool = True,
    ) -> None:
        """替换全部已登记的 reminder 内容，保持注入语义不变。

        用于在 enhanced runner 长生命周期内刷新来自 SystemReminderStore 的
        最新系统提醒，避免 runner 启动时的快照长期过期。
        """
        self._reminders = []
        self.reminder(content, wrap_with_system_tag=wrap_with_system_tag)

    def reminder_bucket(
        self,
        bucket: str,
        *,
        names: Sequence[str] | None = None,
        wrap_with_system_tag: bool = False,
    ) -> None:
        """登记一个从 system reminder store 动态读取的 reminder bucket。"""

        normalized_bucket = str(bucket).strip()
        if not normalized_bucket:
            raise ValueError("bucket 不能为空")

        normalized_names: tuple[str, ...] | None = None
        if names is not None:
            normalized_list: list[str] = []
            for name in names:
                normalized_name = str(name).strip()
                if not normalized_name:
                    raise ValueError("names 中包含空 name")
                normalized_list.append(normalized_name)
            normalized_names = tuple(normalized_list)

        if self._reminder_sources is None:
            self._reminder_sources = []

        self._reminder_sources.append(
            RegisteredReminderSource(
                bucket=normalized_bucket,
                names=normalized_names,
                wrap_with_system_tag=wrap_with_system_tag,
            )
        )

    def _apply_reminders(self, payloads: list[LLMPayload]) -> list[LLMPayload]:
        """根据插入类型将 reminder 注入目标 USER 消息首段。"""

        resolved_reminders, reminder_texts_for_stripping = self._resolve_reminders()
        if not resolved_reminders and not reminder_texts_for_stripping:
            return payloads

        updated = list(payloads)

        user_indices = [idx for idx, payload in enumerate(updated) if payload.role == ROLE.USER]
        if not user_indices:
            return updated

        first_user_index = user_indices[0]
        last_user_index = user_indices[-1]
        all_reminder_parts = [Text(text) for text in reminder_texts_for_stripping]
        target_parts: dict[int, list[Content | LLMUsable]] = {}
        seen_targets: set[tuple[int, str]] = set()
        consumed_now: set[tuple[str, str, str]] = set()

        for reminder in resolved_reminders:
            if (
                reminder.consume_type == SystemReminderConsumeType.ONCE
                and reminder.source_key in self._consumed_once_reminder_keys
            ):
                continue

            target_index = (
                first_user_index
                if reminder.insert_type == SystemReminderInsertType.FIXED
                else last_user_index
            )
            target_key = (target_index, reminder.text)
            if target_key not in seen_targets:
                seen_targets.add(target_key)
                target_parts.setdefault(target_index, []).append(Text(reminder.text))

            if (
                reminder.consume_type == SystemReminderConsumeType.ONCE
                and reminder.source_key is not None
            ):
                consumed_now.add(reminder.source_key)

        for user_index in user_indices:
            prefix_parts = target_parts.get(user_index, [])
            existing = self._strip_registered_reminders(updated[user_index].content, all_reminder_parts)
            rebuilt = prefix_parts + existing
            updated[user_index] = LLMPayload(ROLE.USER, rebuilt)

        self._consumed_once_reminder_keys.update(consumed_now)
        return updated

    def _resolve_reminders(self) -> tuple[list[RegisteredReminder], list[str]]:
        """将直接登记和 bucket-backed reminder 解析为当前可注入文本。"""

        resolved: list[RegisteredReminder] = []
        strip_texts: list[str] = []

        if self._reminders:
            resolved.extend(self._reminders)
            strip_texts.extend(item.text for item in self._reminders)

        if self._reminder_sources:
            from src.core.prompt import get_system_reminder_store

            store = get_system_reminder_store()
            for source in self._reminder_sources:
                strip_texts.extend(source.last_rendered_texts)
                items = store.get_items(source.bucket, names=source.names)
                current_texts: list[str] = []
                for item in items:
                    text = item.render()
                    if source.wrap_with_system_tag:
                        text = (
                            "<system_reminder>\n"
                            f"{text}\n"
                            "</system_reminder>"
                        )
                    current_texts.append(text)
                    resolved.append(
                        RegisteredReminder(
                            text=text,
                            insert_type=item.insert_type,
                            consume_type=item.consume_type,
                            source_key=(source.bucket, item.name, item.render()),
                        )
                    )
                source.last_rendered_texts = tuple(current_texts)
                strip_texts.extend(current_texts)

        return resolved, list(dict.fromkeys(strip_texts))

    def _strip_registered_reminders(
        self,
        content: Sequence[Content | LLMUsable],
        reminder_parts: Sequence[Content | LLMUsable],
    ) -> list[Content | LLMUsable]:
        """移除内容开头已登记的 reminder 文本，便于按最新目标位置重建前缀。"""

        offset = 0
        while offset < len(content):
            matched = any(
                self._is_same_text_part(content[offset], reminder_part)
                for reminder_part in reminder_parts
            )
            if not matched:
                break
            offset += 1

        return list(content[offset:])

    def _is_same_text_part(self, left: Content | LLMUsable, right: Content | LLMUsable) -> bool:
        """判断两个内容片段是否为同一文本片段。"""

        return isinstance(left, Text) and isinstance(right, Text) and left.text == right.text

    def maybe_trim(
        self,
        payloads: list[LLMPayload],
        *,
        max_token_budget: int | None = None,
        token_counter: TokenCounter | None = None,
    ) -> list[LLMPayload]:
        """
        根据 max_token_budget 对 payloads 进行裁剪。

        裁剪策略：
        1. 保留开头的系统/工具消息（pinned prefix）。
        2. 将剩余消息按用户/助手对话分组，整体裁剪掉较早的对话组。
        3. 如果提供了 compression_hook，则在裁剪掉一批对话组后，调用该 hook 生成压缩后的消息，并将其插入剩余消息的开头。
        4. 如果 max_token_budget 仍然超出，则继续裁剪剩余的对话组，直到满足预算。
        """

        if (
            max_token_budget is not None
            and max_token_budget > 0
            and token_counter is not None
            and token_counter(payloads) > max_token_budget
        ):
            return self._trim_by_tokens(payloads, max_token_budget, token_counter)

        return payloads

    def _trim_by_tokens(
        self,
        payloads: list[LLMPayload],
        token_budget: int,
        token_counter: TokenCounter,
    ) -> list[LLMPayload]:
        """
        根据 token_budget 对 payloads 进行裁剪
        """

        pinned, tail = self._split_pinned_prefix(payloads)
        groups = self._build_qa_groups(tail)
        if not groups:
            return payloads

        kept_groups = list(groups)
        dropped_groups: list[list[LLMPayload]] = []

        # 先尝试直接裁剪对话组，保留 pinned 和尽可能多的对话组，直到满足 token 预算
        while len(kept_groups) > 1:
            candidate = pinned + self._flatten_groups(kept_groups)
            if token_counter(candidate) <= token_budget:
                break
            dropped_groups.append(kept_groups.pop(0))

        remaining_payloads = self._flatten_groups(kept_groups)

        # 如果提供了 compression_hook，则在裁剪掉一批对话组后，调用该 hook 生成压缩后的消息，并将其插入剩余消息的开头
        hook_payloads = self._apply_compression_hook(dropped_groups, remaining_payloads)
        if hook_payloads:
            combined = pinned + hook_payloads + remaining_payloads
            while len(kept_groups) > 1 and token_counter(combined) > token_budget:
                kept_groups.pop(0)
                remaining_payloads = self._flatten_groups(kept_groups)
                combined = pinned + hook_payloads + remaining_payloads
            return self._fit_oversized_text_payloads(
                combined,
                token_budget=token_budget,
                token_counter=token_counter,
            )

        return self._fit_oversized_text_payloads(
            pinned + remaining_payloads,
            token_budget=token_budget,
            token_counter=token_counter,
        )

    def _fit_oversized_text_payloads(
        self,
        payloads: list[LLMPayload],
        *,
        token_budget: int,
        token_counter: TokenCounter,
    ) -> list[LLMPayload]:
        """Bound the last indivisible group without mutating caller payloads.

        Full historical groups are removed first.  If the remaining current
        group is itself too large, shrink only ordinary user/assistant ``Text``
        parts while preserving both their beginning and newest tail.  Pinned
        system/tool payloads and structured tool results are never silently cut.
        """

        if token_counter(payloads) <= token_budget:
            return payloads

        working = [LLMPayload(item.role, list(item.content)) for item in payloads]
        candidates: list[tuple[int, int, int, str]] = []
        for payload_index, payload in enumerate(working):
            if payload.role in {ROLE.SYSTEM, ROLE.TOOL}:
                continue
            for content_index, part in enumerate(payload.content):
                if isinstance(part, Text) and part.text:
                    candidates.append(
                        (len(part.text), payload_index, content_index, part.text)
                    )
        candidates.sort(reverse=True)

        for _, payload_index, content_index, original in candidates:
            if token_counter(working) <= token_budget:
                break

            working[payload_index].content[content_index] = Text(
                self._clip_context_text(original, 0)
            )
            if token_counter(working) > token_budget:
                continue

            low = 0
            high = len(original)
            best = 0
            while low <= high:
                keep_chars = (low + high) // 2
                working[payload_index].content[content_index] = Text(
                    self._clip_context_text(original, keep_chars)
                )
                if token_counter(working) <= token_budget:
                    best = keep_chars
                    low = keep_chars + 1
                else:
                    high = keep_chars - 1
            working[payload_index].content[content_index] = Text(
                self._clip_context_text(original, best)
            )

        final_tokens = token_counter(working)
        if final_tokens > token_budget:
            raise LLMContextError(
                "task context cannot fit without truncating pinned or structured "
                f"payloads: tokens={final_tokens}, budget={token_budget}"
            )
        return working

    @staticmethod
    def _clip_context_text(value: str, keep_chars: int) -> str:
        """Preserve instruction-bearing head and recent tail around one marker."""

        if keep_chars >= len(value):
            return value
        if keep_chars <= 0:
            return _TASK_CONTEXT_TRUNCATION_MARKER
        head_chars = max(1, keep_chars // 4)
        tail_chars = max(0, keep_chars - head_chars)
        tail = value[-tail_chars:] if tail_chars else ""
        return value[:head_chars] + _TASK_CONTEXT_TRUNCATION_MARKER + tail

    def _split_pinned_prefix(
        self, payloads: list[LLMPayload]
    ) -> tuple[list[LLMPayload], list[LLMPayload]]:
        """将 payloads 拆分为 pinned 消息和对话消息两部分。

        pinned 消息定义为：所有 SYSTEM 和 TOOL 角色的消息，无论其出现在列表的任何位置，
        均视为固定部分，始终被保留，不参与裁剪。
        对话消息为剩余的 USER 和 ASSISTANT 消息，按原始顺序保留。
        """
        pinned_roles = {ROLE.SYSTEM, ROLE.TOOL}
        pinned = [p for p in payloads if p.role in pinned_roles]
        tail = [p for p in payloads if p.role not in pinned_roles]
        return pinned, tail

    def _build_qa_groups(self, payloads: list[LLMPayload]) -> list[list[LLMPayload]]:
        """将消息分组。一个组作为一个不可分割的最小裁剪单位。

        分组策略：
        1. 每一个 USER 角色开始一个新组。
        2. 后续的 ASSISTANT 和 TOOL_RESULT 消息紧跟在该 USER 组内。
        3. 如果在第一个 USER 之前有孤立的消息（如历史遗留），它们会各自独立成组。
        """
        groups: list[list[LLMPayload]] = []
        current: list[LLMPayload] = []

        for payload in payloads:
            # 遇到 USER 角色，开启新组
            if payload.role == ROLE.USER:
                if current:
                    groups.append(current)
                current = [payload]
            elif not current:
                # 处理第一个 USER 之前的孤立消息
                groups.append([payload])
            else:
                # 归入当前组（确保 user-assistant-tool_result 连带关系）
                current.append(payload)

        if current:
            groups.append(current)

        return groups

    def _apply_compression_hook(
        self,
        dropped_groups: list[list[LLMPayload]],
        remaining_payloads: list[LLMPayload],
    ) -> list[LLMPayload]:
        """调用压缩 hook 生成压缩后的消息"""
        if not self.compression_hook or not dropped_groups:
            return []
        return self.compression_hook(dropped_groups, remaining_payloads)

    def _flatten_groups(self, groups: list[list[LLMPayload]]) -> list[LLMPayload]:
        """将分组扁平化为单一列表"""
        return [payload for group in groups for payload in group]
