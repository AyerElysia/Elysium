"""对话式运动控制器。

她通过自然语言指挥身体，系统理解意图并执行动作。
这不是"AI 完成任务"，而是"她的意识控制她的身体"。

核心理念：
- 她说什么，系统就执行什么
- 动作忠实反映她的意图
- 她能随时改变想法、中断、调整
- 系统提供感觉反馈（"我正在走...我停下了"）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from PIL import Image as PILImage

from .input_control import Action, ActionType, InputController

logger = logging.getLogger("life_engine.minecraft.conversational_motor")


@dataclass(slots=True)
class IntentParse:
    """解析后的意图结构"""

    action: str  # walk, turn, jump, mine, place, attack, use, look, chat, noop
    direction: str = ""  # forward, back, left, right, up, down
    target: str = ""  # 目标描述
    duration: float = 1.0  # 持续时间（秒）
    distance: str = "medium"  # small, medium, large
    raw_intent: str = ""  # 原始意图文本


@dataclass(slots=True)
class ExecutionFeedback:
    """执行反馈（她感受到的）"""

    success: bool
    actions_taken: list[Action]
    feeling: str  # 第一人称感受："我正在向前走..."
    duration_seconds: float
    interrupted: bool = False


class ConversationalMotorController:
    """对话式运动控制器。
    
    把她的自然语言意图转换为键鼠动作，让她感受到在控制自己的身体。
    """

    def __init__(self, input_controller: InputController) -> None:
        self._input = input_controller
        self._executing = False
        self._should_stop = False

    async def execute_intent(
        self, intent: str, llm_helper: Any = None
    ) -> ExecutionFeedback:
        """执行自然语言意图。
        
        Args:
            intent: 她的自然语言意图，如"我想往左边走几步"
            llm_helper: 可选的 LLM 辅助解析（如果不提供，使用规则解析）
        
        Returns:
            ExecutionFeedback 包含执行结果和她的感受
        """
        if self._executing:
            return ExecutionFeedback(
                success=False,
                actions_taken=[],
                feeling="我还在执行上一个动作",
                duration_seconds=0.0,
            )

        self._executing = True
        self._should_stop = False

        try:
            # 1. 解析意图
            parsed = await self._parse_intent(intent, llm_helper)
            logger.info(f"意图解析: {parsed.action} {parsed.direction} {parsed.duration}s")

            # 2. 生成动作序列
            actions = self._intent_to_actions(parsed)
            if not actions:
                return ExecutionFeedback(
                    success=False,
                    actions_taken=[],
                    feeling="我不太明白要做什么",
                    duration_seconds=0.0,
                )

            # 3. 执行动作并反馈
            result = await self._execute_with_feeling(actions, parsed)
            return result

        except Exception as exc:
            logger.warning(f"执行意图失败: {exc}")
            return ExecutionFeedback(
                success=False,
                actions_taken=[],
                feeling=f"执行时出错了: {exc}",
                duration_seconds=0.0,
            )
        finally:
            self._executing = False

    def interrupt(self) -> None:
        """中断当前执行（她改变主意了）。"""
        self._should_stop = True
        logger.info("运动控制被中断")

    # ═══ 意图解析 ═══════════════════════════════════════════════

    async def _parse_intent(
        self, intent: str, llm_helper: Any = None
    ) -> IntentParse:
        """解析自然语言意图为结构化格式。
        
        优先使用 LLM，降级到规则解析。
        """
        if llm_helper:
            try:
                return await self._parse_with_llm(intent, llm_helper)
            except Exception as exc:
                logger.debug(f"LLM 解析失败，降级到规则: {exc}")

        # 降级：规则解析
        return self._parse_with_rules(intent)

    async def _parse_with_llm(self, intent: str, llm_helper: Any) -> IntentParse:
        """用 LLM 解析意图（更准确）。"""
        prompt = f"""
解析 Minecraft 运动意图为 JSON 格式。

示例：
"我想往右转一下" → {{"action": "turn", "direction": "right", "distance": "small"}}
"向前走几步" → {{"action": "walk", "direction": "forward", "duration": 2.0}}
"跳起来" → {{"action": "jump"}}
"挖眼前的方块" → {{"action": "mine", "duration": 1.5}}
"往左看" → {{"action": "look", "direction": "left"}}
"说你好" → {{"action": "chat", "target": "你好"}}

意图：{intent}

只输出 JSON，不要解释。
"""
        response = await llm_helper(prompt)
        # 提取 JSON
        match = re.search(r'\{.*\}', response, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return IntentParse(
                action=data.get("action", "noop"),
                direction=data.get("direction", ""),
                target=data.get("target", ""),
                duration=float(data.get("duration", 1.0)),
                distance=data.get("distance", "medium"),
                raw_intent=intent,
            )
        raise ValueError("LLM 未返回有效 JSON")

    def _parse_with_rules(self, intent: str) -> IntentParse:
        """规则解析（降级方案）。"""
        intent_lower = intent.lower()
        
        # 聊天
        if any(kw in intent for kw in ["说", "讲", "回复", "告诉"]):
            # 提取要说的话
            for pattern in [r'说[「『"](.+?)[」』"]', r"说(.+)", r"回复(.+)"]:
                m = re.search(pattern, intent)
                if m:
                    return IntentParse(action="chat", target=m.group(1).strip(), raw_intent=intent)
        
        # 行走
        if any(kw in intent_lower for kw in ["走", "前进", "后退", "移动"]):
            direction = "forward"
            if any(kw in intent for kw in ["左", "left"]):
                direction = "left"
            elif any(kw in intent for kw in ["右", "right"]):
                direction = "right"
            elif any(kw in intent for kw in ["后", "back"]):
                direction = "back"
            
            # 估计时长
            duration = 1.5
            if any(kw in intent for kw in ["几步", "一点", "稍微"]):
                duration = 1.0
            elif any(kw in intent for kw in ["远", "多", "久"]):
                duration = 3.0
            
            return IntentParse(action="walk", direction=direction, duration=duration, raw_intent=intent)
        
        # 转身/看
        if any(kw in intent_lower for kw in ["转", "看", "turn", "look"]):
            direction = "forward"
            if any(kw in intent for kw in ["左", "left"]):
                direction = "left"
            elif any(kw in intent for kw in ["右", "right"]):
                direction = "right"
            elif any(kw in intent for kw in ["上", "up", "天"]):
                direction = "up"
            elif any(kw in intent for kw in ["下", "down", "地"]):
                direction = "down"
            elif any(kw in intent for kw in ["后", "back", "背后"]):
                direction = "back"
            
            distance = "medium"
            if any(kw in intent for kw in ["一点", "稍微", "little"]):
                distance = "small"
            elif any(kw in intent for kw in ["大", "很多", "转过来"]):
                distance = "large"
            
            action = "look" if any(kw in intent for kw in ["看", "look"]) else "turn"
            return IntentParse(action=action, direction=direction, distance=distance, raw_intent=intent)
        
        # 跳跃
        if any(kw in intent_lower for kw in ["跳", "jump"]):
            return IntentParse(action="jump", raw_intent=intent)
        
        # 挖掘
        if any(kw in intent_lower for kw in ["挖", "采", "砍", "mine", "dig"]):
            duration = 1.5
            if any(kw in intent for kw in ["久", "完全", "彻底"]):
                duration = 3.0
            return IntentParse(action="mine", duration=duration, raw_intent=intent)
        
        # 放置
        if any(kw in intent_lower for kw in ["放", "摆", "建", "place", "build"]):
            return IntentParse(action="place", raw_intent=intent)
        
        # 攻击
        if any(kw in intent_lower for kw in ["打", "攻击", "attack"]):
            return IntentParse(action="attack", raw_intent=intent)
        
        # 使用物品
        if any(kw in intent_lower for kw in ["用", "使用", "吃", "喝", "use"]):
            return IntentParse(action="use", duration=1.5, raw_intent=intent)
        
        # 默认：等待
        return IntentParse(action="noop", raw_intent=intent)

    # ═══ 动作生成 ═══════════════════════════════════════════════

    def _intent_to_actions(self, parsed: IntentParse) -> list[Action]:
        """把解析后的意图转换为动作序列。"""
        
        if parsed.action == "walk":
            return self._walk_actions(parsed)
        elif parsed.action == "turn":
            return self._turn_actions(parsed)
        elif parsed.action == "look":
            return self._look_actions(parsed)
        elif parsed.action == "jump":
            return [Action.press("space")]
        elif parsed.action == "mine":
            return [Action.key_hold("mouse_left", parsed.duration)]  # 注：需在 InputController 实现
        elif parsed.action == "place":
            return [Action.click("right")]
        elif parsed.action == "attack":
            return [Action.click("left")]
        elif parsed.action == "use":
            return [Action.key_hold("mouse_right", parsed.duration)]
        elif parsed.action == "chat":
            return []  # chat 需要特殊处理，在 execute_with_feeling 中
        elif parsed.action == "noop":
            return [Action.noop()]
        
        return []

    def _walk_actions(self, parsed: IntentParse) -> list[Action]:
        """生成行走动作。"""
        key_map = {
            "forward": "w",
            "back": "s",
            "left": "a",
            "right": "d",
        }
        key = key_map.get(parsed.direction, "w")
        return [Action.key_hold(key, parsed.duration)]

    def _turn_actions(self, parsed: IntentParse) -> list[Action]:
        """生成转身动作（视角旋转）。"""
        distance_map = {
            "small": 30,
            "medium": 90,
            "large": 180,
        }
        amount = distance_map.get(parsed.distance, 90)
        
        if parsed.direction == "left":
            return [Action.move(-amount, 0)]
        elif parsed.direction == "right":
            return [Action.move(amount, 0)]
        elif parsed.direction == "back":
            return [Action.move(180, 0)]
        else:
            return [Action.noop()]

    def _look_actions(self, parsed: IntentParse) -> list[Action]:
        """生成看（视角）动作。"""
        distance_map = {"small": 20, "medium": 45, "large": 90}
        amount = distance_map.get(parsed.distance, 45)
        
        if parsed.direction == "left":
            return [Action.move(-amount, 0)]
        elif parsed.direction == "right":
            return [Action.move(amount, 0)]
        elif parsed.direction == "up":
            return [Action.move(0, -amount)]
        elif parsed.direction == "down":
            return [Action.move(0, amount)]
        else:
            return []

    # ═══ 执行与反馈 ═══════════════════════════════════════════════

    async def _execute_with_feeling(
        self, actions: list[Action], parsed: IntentParse
    ) -> ExecutionFeedback:
        """执行动作并生成第一人称感受反馈。"""
        import time
        
        t0 = time.perf_counter()
        executed_actions = []
        
        # 特殊：聊天需要单独处理
        if parsed.action == "chat":
            success = await self._input.type_chat(parsed.target)
            duration = time.perf_counter() - t0
            return ExecutionFeedback(
                success=success,
                actions_taken=[],
                feeling=f"我说：{parsed.target}" if success else "我想说话但失败了",
                duration_seconds=duration,
            )
        
        # 执行动作序列
        for action in actions:
            if self._should_stop:
                break
            success = await self._input.execute(action)
            if success:
                executed_actions.append(action)
            await asyncio.sleep(0.05)
        
        duration = time.perf_counter() - t0
        
        # 生成第一人称感受
        feeling = self._generate_feeling(parsed, executed_actions, self._should_stop)
        
        return ExecutionFeedback(
            success=len(executed_actions) > 0,
            actions_taken=executed_actions,
            feeling=feeling,
            duration_seconds=duration,
            interrupted=self._should_stop,
        )

    def _generate_feeling(
        self, parsed: IntentParse, actions: list[Action], interrupted: bool
    ) -> str:
        """生成第一人称感受描述。"""
        if interrupted:
            return "我停下来了"
        
        if not actions:
            return "我没有动"
        
        # 根据动作类型生成感受
        action = parsed.action
        
        if action == "walk":
            direction_feeling = {
                "forward": "向前",
                "back": "向后",
                "left": "向左",
                "right": "向右",
            }.get(parsed.direction, "")
            return f"我{direction_feeling}走了几步"
        
        elif action == "turn":
            direction_feeling = {
                "left": "向左",
                "right": "向右",
                "back": "向后",
            }.get(parsed.direction, "")
            return f"我{direction_feeling}转了一下身"
        
        elif action == "look":
            direction_feeling = {
                "left": "向左",
                "right": "向右",
                "up": "向上",
                "down": "向下",
            }.get(parsed.direction, "")
            return f"我{direction_feeling}看了看"
        
        elif action == "jump":
            return "我跳了一下"
        
        elif action == "mine":
            return f"我挥动工具挖了 {parsed.duration:.1f} 秒"
        
        elif action == "place":
            return "我放置了一个方块"
        
        elif action == "attack":
            return "我挥出了一击"
        
        elif action == "use":
            return "我使用了手上的物品"
        
        return "我做了一个动作"


def create_conversational_motor(input_controller: InputController) -> ConversationalMotorController:
    """创建对话式运动控制器。"""
    return ConversationalMotorController(input_controller)
