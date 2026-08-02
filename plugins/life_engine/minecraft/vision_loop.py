"""视觉闭环执行引擎。

真正的仿生控制：看 → 做 → 看 → 调整 → 看 → 做...
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from PIL import Image as PILImage

from .capture import WindowCapture
from .conversational_motor import ConversationalMotorController

logger = logging.getLogger("life_engine.minecraft.vision_loop")


@dataclass
class VisionUnderstanding:
    """视觉理解结果"""

    scene_description: str  # 我看到什么
    target_visible: bool  # 目标在画面里吗
    target_direction: str  # 目标在哪个方向（left/right/center/not_visible）
    target_distance: str  # 距离（near/medium/far/unknown）
    obstacles: list[str]  # 障碍物
    next_action_suggestion: str  # 建议下一步做什么
    goal_reached: bool  # 是否达成目标
    confidence: float  # 理解的置信度


class VisionLoopController:
    """视觉闭环控制器。
    
    人类模式：看 → 判断 → 行动 → 看 → 调整...
    """

    def __init__(
        self,
        capture: WindowCapture,
        motor: ConversationalMotorController,
        vision_model: Any,  # VLM (Qwen2-VL, LLaVA, etc.)
    ) -> None:
        self._capture = capture
        self._motor = motor
        self._vlm = vision_model
        self._max_steps = 50  # 最多闭环 50 步
        self._step_interval = 0.3  # 每步间隔（秒）

    async def execute_intent_with_vision(
        self, intent: str, timeout: float = 30.0
    ) -> dict[str, Any]:
        """视觉闭环执行意图。
        
        Args:
            intent: 她的意图，如"走到那棵树前"
            timeout: 超时时间
        
        Returns:
            执行报告 + 她的感受
        """
        import time
        
        t0 = time.time()
        steps_taken = 0
        feelings = []  # 记录她每一步的感受
        
        logger.info(f"开始视觉闭环执行: {intent}")
        
        for step in range(self._max_steps):
            if time.time() - t0 > timeout:
                return {
                    "success": False,
                    "reason": "timeout",
                    "steps": steps_taken,
                    "feeling": f"我尝试了 {steps_taken} 步，但时间太久了",
                }
            
            # 1. 👀 看当前画面
            frame = await self._capture.grab_consciousness_frame()
            if not frame:
                logger.warning("无法获取画面")
                break
            
            # 2. 🧠 理解画面
            understanding = await self._understand_scene(frame.image, intent)
            
            # 记录她的感受
            feeling = self._generate_feeling(understanding, step)
            feelings.append(feeling)
            logger.info(f"步骤 {step}: {feeling}")
            
            # 3. ✅ 目标达成？
            if understanding.goal_reached:
                return {
                    "success": True,
                    "steps": steps_taken,
                    "feeling": f"我做到了！{feeling}",
                    "journey": feelings,
                }
            
            # 4. 👣 执行建议的动作
            if understanding.next_action_suggestion:
                feedback = await self._motor.execute_intent(
                    understanding.next_action_suggestion, llm_helper=None
                )
                steps_taken += 1
                
                if not feedback.success:
                    logger.warning(f"动作执行失败: {feedback.feeling}")
            
            # 5. 等待画面稳定
            await asyncio.sleep(self._step_interval)
        
        # 超过最大步数
        return {
            "success": False,
            "reason": "max_steps_exceeded",
            "steps": steps_taken,
            "feeling": "我尝试了很多次，但还没完全做到",
            "journey": feelings,
        }

    async def _understand_scene(
        self, frame: PILImage.Image, intent: str
    ) -> VisionUnderstanding:
        """用 VLM 理解画面（相对于意图）。"""
        
        prompt = f"""
你正在 Minecraft 中游玩，你的意图是：{intent}

看这个画面，回答：
1. 我现在看到什么？（简要描述）
2. 我的目标（{intent}）在画面里吗？在哪个方向？
3. 距离目标有多远？
4. 前面有障碍物吗？
5. 我下一步应该做什么？（用简短的动作描述，如"往右转一点"）
6. 我的目标达成了吗？

用JSON格式回答：
{{
  "scene": "我在平原上，前方有棵橡树和一些草",
  "target_visible": true,
  "target_direction": "center",  // left, center, right, not_visible
  "target_distance": "medium",   // near, medium, far, unknown
  "obstacles": ["草"],
  "next_action": "往前走几步",
  "goal_reached": false
}}
"""
        
        try:
            # 调用 VLM
            response = await self._vlm.chat(frame, prompt)
            
            # 解析 JSON
            import json
            import re
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return VisionUnderstanding(
                    scene_description=data.get("scene", ""),
                    target_visible=data.get("target_visible", False),
                    target_direction=data.get("target_direction", "unknown"),
                    target_distance=data.get("target_distance", "unknown"),
                    obstacles=data.get("obstacles", []),
                    next_action_suggestion=data.get("next_action", ""),
                    goal_reached=data.get("goal_reached", False),
                    confidence=0.8,
                )
        except Exception as exc:
            logger.warning(f"视觉理解失败: {exc}")
        
        # 降级：返回空理解
        return VisionUnderstanding(
            scene_description="我看不清",
            target_visible=False,
            target_direction="unknown",
            target_distance="unknown",
            obstacles=[],
            next_action_suggestion="往前走一点",  # 盲走
            goal_reached=False,
            confidence=0.0,
        )

    def _generate_feeling(
        self, understanding: VisionUnderstanding, step: int
    ) -> str:
        """生成她的第一人称感受（基于视觉理解）。"""
        
        feeling_parts = []
        
        # 场景感受
        if understanding.scene_description:
            feeling_parts.append(f"我看到{understanding.scene_description}")
        
        # 目标感受
        if understanding.target_visible:
            direction_feeling = {
                "left": "在我左边",
                "right": "在我右边",
                "center": "在我前方",
            }.get(understanding.target_direction, "")
            
            distance_feeling = {
                "near": "很近了",
                "medium": "",
                "far": "还有点远",
            }.get(understanding.target_distance, "")
            
            if direction_feeling or distance_feeling:
                feeling_parts.append(
                    f"目标{direction_feeling}{distance_feeling}".strip()
                )
        else:
            feeling_parts.append("我还看不到目标")
        
        # 障碍感受
        if understanding.obstacles:
            feeling_parts.append(f"前面有{'/'.join(understanding.obstacles[:2])}")
        
        return "，".join(feeling_parts) if feeling_parts else f"我在尝试第 {step} 步"


def create_vision_loop(
    capture: WindowCapture,
    motor: ConversationalMotorController,
    vision_model: Any,
) -> VisionLoopController:
    """创建视觉闭环控制器。"""
    return VisionLoopController(capture, motor, vision_model)
