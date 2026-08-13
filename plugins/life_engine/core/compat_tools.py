"""life_engine 对话兼容工具层。

把默认聊天器/思考插件里的常用工具适配到 life_chatter，
让同一主体的不同运行模式可以直接复用这些能力。
"""

from __future__ import annotations

from typing import Annotated, Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction, BaseTool
from src.core.managers import get_plugin_manager

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
        from ..service.core import LifeEngineService

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



class LifeReportStateAction(BaseAction):
    """主意识向潜意识报告场景状态变化。"""

    action_name = "report_state"
    action_description = (
        "向潜意识的世界模型报告当前场景或关系状态变化。"
        "成功后写入不可变生命事件和 World assertion；它不会修改主体文档 MEMORY.md。"
        "当你完成一轮重要互动、观察到关系变化、或场景状态发生转变时使用。"
        "若要改变 SOUL.md、USER.md 或 MEMORY.md，必须改走主体候选复盘与明确接受流程。"
        "这不是给用户发消息，是向自己的内在世界更新一条有来源的观察。"
    )

    chatter_allow: list[str] = ["life_chatter"]
    primary_action: bool = False

    async def execute(
        self,
        report: Annotated[str, "状态变化的描述（必填）。例如：'小星星的胃线已闭合，不再追问'。"],
        kind: Annotated[
            str,
            "由你自由描述的观察领域；它只作为来源标签，不会触发代码分类或自动裁决。",
        ] = "",
        entity_id: Annotated[str, "关联的实体 ID（如人物），可留空。"] = "",
        thread_id: Annotated[str, "关联的话题 ID（如要闭合某个话题），可留空。"] = "",
        scene_id: Annotated[
            str,
            "当前场景 ID。通常留空，由可信运行时上下文自动注入。",
        ] = "",
    ) -> tuple[bool, str]:
        report_text = str(report or "").strip()
        if not report_text:
            return False, "report 不能为空"

        life_plugin = get_plugin_manager().get_plugin("life_engine")
        if life_plugin is None:
            return False, "life_engine 未加载"

        service = getattr(life_plugin, "service", None)
        if service is None or not hasattr(service, "report_world_observation"):
            return False, "life_engine 服务不可用"

        try:
            trusted_stream_id = str(
                getattr(getattr(self, "chat_stream", None), "stream_id", "")
                or ""
            ).strip()
            instance_id = service.resolve_consciousness_instance(
                trusted_stream_id
            )
            explicit_subject = str(
                entity_id or thread_id or scene_id or trusted_stream_id or ""
            ).strip()
            subject = explicit_subject or f"consciousness:{instance_id}"
            receipt = await service.report_world_observation(
                report_text,
                source_instance_id=instance_id,
                subject=subject,
                predicate="state_report",
                domain=str(kind or "").strip(),
                stream_id=trusted_stream_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"报告状态失败: {exc}")
            return False, f"报告状态失败: {exc}"

        return True, (
            "观察已写入不可变生命事件和 World assertion；"
            "这不是 MEMORY.md 主体文档提交"
            f"（assertion_id={receipt['assertion_id']}, "
            f"source_instance_id={receipt['source_instance_id']}）。"
        )


class LifeInnerQueryTool(BaseTool):
    """主意识向潜意识查询已知事实。"""

    tool_name = "inner_query"
    tool_description = (
        "向自己的内在世界查询已知事实。"
        "当你不确定某个人、某个话题、某个承诺的当前状态时使用。"
        "它会从潜意识的结构化世界模型中检索，比翻聊天记录更快。"
        "返回的是你自己已经知道的东西，不是新信息。"
    )
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        query: Annotated[str, "想查询的内容（必填）。例如：'小星星的身体状况'、'我和妹妹的关系'、'当前未闭合的话题'。"],
    ) -> tuple[bool, str]:
        query_text = str(query or "").strip()
        if not query_text:
            return False, "query 不能为空"

        life_plugin = get_plugin_manager().get_plugin("life_engine")
        if life_plugin is None:
            return False, "life_engine 未加载"

        service = getattr(life_plugin, "service", None)
        if service is None or not hasattr(service, "query_world"):
            return False, "life_engine 服务不可用"

        try:
            trusted_stream_id = str(
                getattr(getattr(self, "chat_stream", None), "stream_id", "")
                or ""
            ).strip()
            instance_id = service.resolve_consciousness_instance(
                trusted_stream_id
            )
            return True, await service.query_world(instance_id, query_text)
        except Exception as exc:  # noqa: BLE001
            return False, f"查询内在世界失败: {exc}"
