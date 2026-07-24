"""life_engine 对话兼容工具层。

把默认聊天器/思考插件里的常用工具适配到 life_chatter，
让同一主体的不同运行模式可以直接复用这些能力。
"""

from __future__ import annotations
from typing import Annotated, Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.base import BaseAction, BaseTool
from src.core.managers import get_plugin_manager
from src.kernel.llm import LLMPayload, ROLE, Text, ToolRegistry, ToolResult


logger = get_logger("life_engine.compat_tools")


class LifeThinkAction(BaseAction):
    """生命对话器的思考动作。"""

    action_name = "think"
    action_description = (
        "在发送文本回复前，先记录一段内心思考动作。"
        "此 action 必须与 action-life_send_text 同时使用，且必须排在 action-life_send_text 之前；"
        "不要单独调用，也不要把它和查询型 tool 混在同一轮。"
        "thought 只写内心活动，不要把真正要发给用户的正文只写在 thought 里；"
        "最终回复必须单独写进 life_send_text.content。"
    )

    chatter_allow: list[str] = ["life_chatter"]
    primary_action: bool = False

    async def execute(
        self,
        mood: Annotated[str, "此刻的心情/情绪状态（必填）。"],
        decision: Annotated[str, "你决定的下一步行动（必填）。"],
        expected_response: Annotated[str, "你预期用户看到回复后的反应（必填）。"],
        thought: Annotated[str, "你的心理活动（必填）。"] = "",
        **extra_kwargs: object,
    ) -> tuple[bool, str]:
        legacy_content = extra_kwargs.pop("content", None)
        normalized_thought = (thought or "").strip()
        if not normalized_thought and isinstance(legacy_content, str):
            normalized_thought = legacy_content.strip()
            if normalized_thought:
                logger.warning("action-think 收到兼容字段 content，已映射到 thought")

        if not normalized_thought:
            logger.warning(
                "action-think 缺少 thought/content，已按 mood/decision/expected_response 降级记录"
            )

        if extra_kwargs:
            logger.warning(
                "action-think 收到未知参数，已忽略: %s",
                sorted(extra_kwargs.keys()),
            )

        chat_stream = getattr(self, "chat_stream", None)
        stream_id = str(getattr(chat_stream, "stream_id", "") or "").strip()
        service = getattr(getattr(self, "plugin", None), "service", None)
        if service is None:
            life_plugin = get_plugin_manager().get_plugin("life_engine")
            service = getattr(life_plugin, "service", None) if life_plugin is not None else None

        if service is not None and stream_id and hasattr(service, "record_chatter_think_snapshot"):
            try:
                await service.record_chatter_think_snapshot(
                    stream_id=stream_id,
                    thought=normalized_thought,
                    mood=str(mood or "").strip(),
                    decision=str(decision or "").strip(),
                    expected_response=str(expected_response or "").strip(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"记录 action-think 快照失败: {exc}")

        return True, "思考动作已记录。请在同一轮内继续调用 life_send_text 发送最终回复。"


class LifeRecordInnerMonologueAction(BaseAction):
    """把当前对话器生成的内心独白写回 life 运行态。"""

    action_name = "record_inner_monologue"
    action_description = (
        "记录一段当前对话器视角下的内心独白。"
        "适合在主动机会、延迟续话、犹豫要不要开口时先留下当前心理推进，"
        "让同一主体后续轮次仍能看到连续的内在状态。"
        "它不会直接给用户发消息。"
    )

    chatter_allow: list[str] = ["life_chatter"]
    primary_action: bool = False

    async def execute(
        self,
        thought: Annotated[str, "这一次新的内心独白正文，写你此刻真实在想什么。"],
        mood: Annotated[str, "当前情绪/氛围，例如想念、克制、轻松、犹豫。"] = "",
        intent: Annotated[str, "你此刻的下一步倾向，例如继续等待、想轻轻开口、先按住不说。"] = "",
        topic: Annotated[str, "这段独白围绕的主题，可留空。"] = "",
    ) -> tuple[bool, str]:
        thought_text = str(thought or "").strip()
        if not thought_text:
            return False, "thought 不能为空"

        chat_stream = getattr(self, "chat_stream", None)
        if chat_stream is None:
            return False, "缺少当前聊天流，无法记录内心独白"

        life_plugin = get_plugin_manager().get_plugin("life_engine")
        if life_plugin is None:
            return False, "life_engine 未加载，无法记录内心独白"

        service = getattr(life_plugin, "service", None)
        if service is None or not hasattr(service, "record_chatter_inner_monologue"):
            return False, "life_engine 服务不可用，无法记录内心独白"

        try:
            await service.record_chatter_inner_monologue(
                thought_text,
                stream_id=str(getattr(chat_stream, "stream_id", "") or ""),
                platform=str(getattr(chat_stream, "platform", "") or ""),
                chat_type=str(getattr(chat_stream, "chat_type", "") or ""),
                sender_name=str(getattr(chat_stream, "bot_nickname", "") or "当前对话器"),
                mood=str(mood or "").strip(),
                intent=str(intent or "").strip(),
                topic=str(topic or "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"记录内心独白失败: {exc}")
            return False, f"记录内心独白失败: {exc}"

        return True, "内心独白已记录。请继续决定是回复用户，还是 pass_and_wait。"


class LifeScheduleFollowupMessageAction(BaseAction):
    """登记一条延迟续话计划。"""

    action_name = "schedule_followup_message"
    action_description = (
        "当你刚刚已经发出一条回复，但觉得过一小会儿在对方还没回复时"
        "可能还想补一句时使用。它不会立刻发送消息，而是登记一条延迟续话计划。"
        "这个动作会复用主动续话运行层的调度能力。"
    )

    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        delay_seconds: Annotated[float, "过多久后再检查一次，单位秒。"],
        thought: Annotated[str, "你此刻为什么还想继续说。"],
        topic: Annotated[str, "这次续话围绕的话题。"],
        followup_type: Annotated[
            str,
            "续话类型，例如 add_detail / clarify / soft_emotion / share_new_thought。",
        ] = "share_new_thought",
    ) -> tuple[bool, str]:
        from plugins.life_engine.service.core import LifeEngineService

        service = LifeEngineService.get_instance()
        if service is None:
            return False, "life_engine 服务未就绪，无法登记延迟续话"

        chat_stream = getattr(self, "chat_stream", None)
        if chat_stream is None:
            return False, "缺少当前聊天流，无法登记延迟续话"

        try:
            ok, message = await service.schedule_followup_for_stream(
                chat_stream,
                delay_seconds=delay_seconds,
                thought=thought,
                topic=topic,
                followup_type=followup_type,
                source="life_engine",
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"登记延迟续话失败: {exc}"

        return ok, message


class LifeInnerDialogueTool(BaseTool):
    """主意识把念头沉进潜意识：异步内心对话，不即时返回答案。"""

    tool_name = "inner_dialogue"
    tool_description = (
        "把一句话沉进自己心里慢慢想——这是主意识对潜意识的内心对话，不是咨询另一个人。"
        "工具只负责投递，不会同步返回“想完了的答案”。"
        "适合：犹豫、惦记、补信息差、理清倾向；想通了会自己浮回表达层（若需要）。"
        "调用后继续场面，不要假装已经想清楚。"
    )
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        thought: Annotated[str, "此刻想沉下去的话。直接写内心内容，例如「其实我有点犹豫…」。"] = "",
        mode: Annotated[
            str,
            "notice=只记下；reflect=好好想想（默认）；gap=补信息差；decide=理清倾向。",
        ] = "reflect",
        expect_surface: Annotated[
            bool,
            "想完后是否允许浮回表达层。默认 true；false 则只在中枢内部沉淀。",
        ] = True,
        stream_id: Annotated[str, "当前对话流 ID。通常留空，由系统自动填充。"] = "",
        platform: Annotated[str, "当前平台名。通常留空，由系统自动填充。"] = "",
        chat_type: Annotated[str, "当前聊天类型。通常留空，由系统自动填充。"] = "",
        sender_name: Annotated[str, "当前说话身份展示名。通常留空，由系统自动填充。"] = "",
        # legacy aliases from message_nucleus / consult_nucleus
        content: Annotated[str, "兼容旧参数：等同 thought。"] = "",
        query: Annotated[str, "兼容旧参数：等同 thought。"] = "",
    ) -> tuple[bool, str]:
        text = str(thought or content or query or "").strip()
        if not text:
            return False, "thought 不能为空"

        life_plugin = get_plugin_manager().get_plugin("life_engine")
        if life_plugin is None:
            return False, "life_engine 未加载，无法进行内心对话"

        service = getattr(life_plugin, "service", None)
        if service is None or not hasattr(service, "enqueue_inner_dialogue"):
            return False, "life_engine 服务不可用，无法进行内心对话"

        chat_stream = getattr(self, "chat_stream", None)
        resolved_stream = str(stream_id or getattr(chat_stream, "stream_id", "") or "").strip()
        resolved_platform = str(platform or getattr(chat_stream, "platform", "") or "life_chatter").strip()
        resolved_chat_type = str(chat_type or getattr(chat_stream, "chat_type", "") or "").strip()
        resolved_sender = str(
            sender_name or getattr(chat_stream, "bot_nickname", "") or "主意识"
        ).strip()

        try:
            receipt: dict[str, Any] = await service.enqueue_inner_dialogue(
                text,
                mode=str(mode or "reflect"),
                expect_surface=bool(expect_surface),
                stream_id=resolved_stream,
                platform=resolved_platform,
                chat_type=resolved_chat_type,
                sender_name=resolved_sender,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"内心对话投递失败: {exc}"

        receipt_id = str(receipt.get("receipt_id") or receipt.get("event_id") or "unknown")
        mode_name = str(receipt.get("mode") or mode or "reflect")
        return True, (
            f"这句话已经沉进心里了（receipt={receipt_id}, mode={mode_name}）。"
            "先继续场面；想通了会自己浮上来。不要假装已经想清楚。"
        )


# ---- deprecated aliases (thin wrappers) ----


class LifeMessageNucleusTool(LifeInnerDialogueTool):
    """兼容旧名 message_nucleus → inner_dialogue。"""

    tool_name = "message_nucleus"
    tool_description = (
        "【已弃用，请改用 inner_dialogue】"
        "向自己的潜意识投递异步念头。不会同步返回答案。"
    )


class LifeConsultNucleusTool(LifeInnerDialogueTool):
    """兼容旧名 consult_nucleus → inner_dialogue（异步，不再扮演潜意识）。"""

    tool_name = "consult_nucleus"
    tool_description = (
        "【已弃用，请改用 inner_dialogue】"
        "把问题沉进心里慢慢想，不会同步扮演潜意识回答。"
    )


class LifeSearchLifeMemoryTool(BaseTool):
    """同步检索 life_engine 深层记忆。"""

    tool_name = "search_life_memory"
    tool_description = (
        "深度检索生命中枢的记忆文件与联想结果。"
        "适合查询过去聊过的事、旧计划、历史文件记录、被记住的线索。"
        "这是记忆检索，不是当前状态摘要。"
    )
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        query: Annotated[str, "要检索的记忆主题或关键词"],
        top_k: Annotated[int, "最多返回多少条主结果，默认 5"] = 5,
    ) -> tuple[bool, str]:
        query_text = str(query or "").strip()
        if not query_text:
            return False, "query 不能为空"

        resolved_top_k = max(1, min(int(top_k), 10))
        try:
            life_plugin = get_plugin_manager().get_plugin("life_engine")
            if life_plugin is None:
                return False, "life_engine 未加载"

            service = getattr(life_plugin, "service", None)
            if service is None:
                return False, "life_engine 服务不可用"

            if hasattr(service, "search_outer_memory"):
                result = await service.search_outer_memory(query_text, top_k=resolved_top_k)
            else:
                result = await service.search_actor_memory(query_text, top_k=resolved_top_k)

            if not result:
                return True, "暂时没有检索到相关记忆"
            return True, result
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"检索 life memory 失败: {exc}")
            return False, f"检索失败: {exc}"


class LifeRetrieveMemoryTool(BaseTool):
    """智能记忆检索工具。"""

    tool_name = "retrieve_memory"
    tool_description = (
        "智能检索生命中枢的记忆，自动决定检索策略和详细程度。"
        "适合查询过去聊过的事、旧计划、历史文件记录、被记住的线索。"
        "如果需要翻过去的记忆文件或旧计划，请直接使用 search_life_memory / fetch_life_memory。"
    )
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        query: Annotated[str, "要检索的记忆主题或关键词"],
        detail_level: Annotated[str, "详细程度：brief/normal/detailed/auto"] = "normal",
        max_results: Annotated[int, "最多返回几条记忆"] = 3,
    ) -> tuple[bool, str]:
        query_text = str(query or "").strip()
        if not query_text:
            return False, "query 不能为空"

        detail_level = str(detail_level or "normal").strip().lower()
        if detail_level not in {"brief", "normal", "detailed", "auto"}:
            detail_level = "normal"

        max_results = max(1, min(int(max_results), 5))

        try:
            life_plugin = get_plugin_manager().get_plugin("life_engine")
            if not life_plugin:
                return False, "life_engine 未加载"

            registry = ToolRegistry()
            search_tool_cls = None
            fetch_tool_cls = None

            for tool in getattr(life_plugin, "tools", []):
                if getattr(tool, "tool_name", "") == "search_life_memory":
                    search_tool_cls = tool.__class__
                    registry.register(search_tool_cls)
                elif getattr(tool, "tool_name", "") == "fetch_life_memory":
                    fetch_tool_cls = tool.__class__
                    registry.register(fetch_tool_cls)

            if not search_tool_cls:
                return False, "search_life_memory 工具未找到"

            agent_prompt = f"""你是生命中枢的记忆检索专家。你的任务是帮助当前运行模式检索相关记忆。

**检索需求**：
- 主题/关键词：{query_text}
- 详细程度：{detail_level}
- 最多返回：{max_results} 条

**可用工具**：
- search_life_memory: 检索记忆摘要
- fetch_life_memory: 获取完整文件内容

**你的任务**：
1. 使用 search_life_memory 检索相关记忆
2. 根据 detail_level 决定是否需要调用 fetch_life_memory：
   - brief: 只返回 search 的摘要
   - normal: 对最相关的 1-2 条记忆调用 fetch（限制 500 字符）
   - detailed: 对所有相关记忆调用 fetch（限制 1000 字符）
   - auto: 根据摘要质量自动判断
3. 整合结果，返回清晰的摘要

**返回格式**：
【记忆1】标题
相关度：0.XX
内容：...

【记忆2】标题
相关度：0.XX
内容：...

**注意**：
- 如果没有找到相关记忆，明确说明
- 控制返回长度，避免过长
- 优先返回最相关的记忆
- 直接返回整合后的结果，不要说“我已经检索完成”之类的元信息
"""

            model_set = get_model_set_by_task("sub_actor")
            request = create_llm_request(
                model_set=model_set,
                request_name="memory_retrieval_agent",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(agent_prompt)))
            request.add_payload(LLMPayload(ROLE.TOOL, list(registry.get_all())))
            request.add_payload(LLMPayload(ROLE.USER, Text(f"请检索关于「{query_text}」的记忆")))

            max_rounds = 5
            response = await request.send(stream=False)
            final_result = ""

            for _ in range(max_rounds):
                response_text = await response
                call_list = list(getattr(response, "call_list", []) or [])

                if not call_list:
                    final_result = str(response_text or "").strip()
                    break

                for call in call_list:
                    tool_name = getattr(call, "name", "") or ""
                    args = dict(getattr(call, "args", {}) or {})

                    usable_cls = registry.get(tool_name)
                    if usable_cls:
                        tool_instance = usable_cls(plugin=life_plugin)
                        success, result = await tool_instance.execute(**args)
                        result_text = str(result) if success else f"失败: {result}"
                    else:
                        result_text = f"未知工具: {tool_name}"

                    call_id = getattr(call, "id", None)
                    response.add_payload(
                        LLMPayload(
                            ROLE.TOOL_RESULT,
                            ToolResult(value=result_text, call_id=call_id, name=tool_name),
                        )
                    )

                response = await response.send(stream=False)
            else:
                final_result = str(response_text or "").strip() if response_text else f"子代理在 {max_rounds} 轮内未完成"

            if not final_result:
                final_result = "暂时没有检索到相关记忆"
            return True, final_result
        except Exception as exc:  # noqa: BLE001
            logger.error(f"执行智能记忆检索失败: {exc}", exc_info=True)
            return False, f"执行失败: {exc}"
