"""VLA 运动皮层引擎。

加载 VLA 模型（UI-TARS-7B 或 JARVIS-VLA），提供视觉闭环推理接口。
输入：当前截图 + 子目标描述
输出：键鼠动作
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from PIL import Image as PILImage

from .input_control import Action

logger = logging.getLogger("life_engine.minecraft.vla")

# VLA 推理分辨率
VLA_INPUT_SIZE = (640, 360)

# 默认模型路径（HuggingFace 缓存或本地路径）
DEFAULT_VLA_MODEL = "bytedance-research/UI-TARS-7B-SFT"


@dataclass(slots=True)
class VLAConfig:
    """VLA 配置。"""

    model_name_or_path: str = DEFAULT_VLA_MODEL
    device: str = "cuda"
    dtype: str = "float16"
    max_new_tokens: int = 128
    temperature: float = 0.1
    # 动作空间配置
    mouse_sensitivity: float = 2.0
    action_repeat: int = 1


@dataclass(slots=True)
class VLAOutput:
    """VLA 推理输出。"""

    action: Action
    raw_text: str = ""
    confidence: float = 0.0
    latency_ms: float = 0.0


class VLAEngine:
    """VLA 运动皮层引擎。

    负责加载模型并将 (截图, 意图) 转换为键鼠动作。
    """

    def __init__(self, config: VLAConfig | None = None) -> None:
        self._cfg = config or VLAConfig()
        self._model: Any = None
        self._processor: Any = None
        self._loaded = False
        self._lock = asyncio.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def load_model(self) -> bool:
        """加载 VLA 模型到 GPU。"""
        if self._loaded:
            return True

        try:
            logger.info(f"正在加载 VLA 模型: {self._cfg.model_name_or_path}")
            # 在线程池中加载，避免阻塞事件循环
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(None, self._load_sync)
            if success:
                self._loaded = True
                logger.info("VLA 模型加载完成")
            return success
        except Exception as exc:
            logger.error(f"VLA 模型加载失败: {exc}")
            return False

    def _load_sync(self) -> bool:
        """同步加载模型（在线程池中执行）。"""
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor

            dtype_map = {
                "float16": torch.float16,
                "bfloat16": torch.bfloat16,
                "float32": torch.float32,
            }
            torch_dtype = dtype_map.get(self._cfg.dtype, torch.float16)

            self._processor = AutoProcessor.from_pretrained(
                self._cfg.model_name_or_path,
                trust_remote_code=True,
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                self._cfg.model_name_or_path,
                torch_dtype=torch_dtype,
                device_map=self._cfg.device,
                trust_remote_code=True,
            )
            self._model.eval()
            return True
        except ImportError as exc:
            logger.error(f"缺少依赖: {exc}")
            return False
        except Exception as exc:
            logger.error(f"模型加载异常: {exc}")
            return False

    async def infer(self, frame: PILImage.Image, intent: str) -> VLAOutput:
        """推理：给定截图和意图，输出动作。

        Args:
            frame: 当前游戏画面（已缩放到 VLA 尺寸）
            intent: 自然语言子目标，如 "走到那棵树前并砍它"

        Returns:
            VLAOutput 包含解析后的 Action
        """
        if not self._loaded:
            return VLAOutput(
                action=Action.noop(),
                raw_text="VLA not loaded",
            )

        import time
        t0 = time.perf_counter()

        try:
            loop = asyncio.get_event_loop()
            raw_text = await loop.run_in_executor(
                None, self._infer_sync, frame, intent
            )
            latency = (time.perf_counter() - t0) * 1000

            action = self._parse_action(raw_text)
            return VLAOutput(
                action=action,
                raw_text=raw_text,
                latency_ms=latency,
            )
        except Exception as exc:
            logger.debug(f"VLA 推理异常: {exc}")
            return VLAOutput(
                action=Action.noop(),
                raw_text=f"error: {exc}",
            )

    def _infer_sync(self, frame: PILImage.Image, intent: str) -> str:
        """同步推理（在线程池中执行）。"""
        import torch

        # 构建 prompt（UI-TARS 格式）
        prompt = self._build_prompt(intent)

        # 处理输入
        inputs = self._processor(
            text=prompt,
            images=[frame],
            return_tensors="pt",
        ).to(self._cfg.device)

        # 生成
        with torch.no_grad():
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=self._cfg.max_new_tokens,
                temperature=self._cfg.temperature,
                do_sample=self._cfg.temperature > 0,
            )

        # 解码
        generated = outputs[0][inputs["input_ids"].shape[1]:]
        text = self._processor.decode(generated, skip_special_tokens=True)
        return text.strip()

    def _build_prompt(self, intent: str) -> str:
        """构建 VLA prompt。

        UI-TARS 风格：描述任务 + 请求下一步动作。
        """
        return (
            f"You are playing Minecraft. Your current goal: {intent}\n"
            f"Based on the screenshot, output the next action.\n"
            f"Available actions:\n"
            f"- move(dx, dy): move mouse to adjust view\n"
            f"- click(button): left/right click\n"
            f"- key(name): press w/a/s/d/space/shift/e/q/1-9\n"
            f"- key_hold(name, seconds): hold a key\n"
            f"- scroll(amount): scroll hotbar\n"
            f"- noop: wait\n"
            f"- done: goal completed\n"
            f"Output ONLY the action in format: action(param1, param2)\n"
        )

    def _parse_action(self, raw_text: str) -> Action:
        """解析 VLA 输出文本为 Action。"""
        text = raw_text.strip().lower()

        # done
        if "done" in text:
            return Action.done(info=raw_text)

        # noop
        if "noop" in text or "wait" in text:
            return Action.noop()

        # move(dx, dy)
        if "move" in text:
            dx, dy = self._extract_two_ints(text)
            if dx is not None and dy is not None:
                # 应用鼠标灵敏度
                dx = int(dx * self._cfg.mouse_sensitivity)
                dy = int(dy * self._cfg.mouse_sensitivity)
                return Action.move(dx, dy)

        # click(button)
        if "click" in text:
            if "right" in text:
                return Action.click("right")
            return Action.click("left")

        # key_hold(name, seconds)
        if "key_hold" in text or "hold" in text:
            key = self._extract_key(text)
            duration = self._extract_float(text, default=1.0)
            if key:
                return Action.key_hold(key, duration)

        # key(name)
        if "key" in text:
            key = self._extract_key(text)
            if key:
                return Action.press(key)

        # scroll(amount)
        if "scroll" in text:
            amount = self._extract_one_int(text)
            if amount is not None:
                return Action.scroll(amount)

        # 无法解析，返回 noop
        logger.debug(f"无法解析 VLA 输出: {raw_text}")
        return Action.noop()

    def _extract_two_ints(self, text: str) -> tuple[int | None, int | None]:
        """提取两个整数。"""
        import re
        nums = re.findall(r"-?\d+", text)
        if len(nums) >= 2:
            return int(nums[0]), int(nums[1])
        return None, None

    def _extract_one_int(self, text: str) -> int | None:
        """提取一个整数。"""
        import re
        nums = re.findall(r"-?\d+", text)
        if nums:
            return int(nums[0])
        return None

    def _extract_float(self, text: str, default: float = 1.0) -> float:
        """提取浮点数。"""
        import re
        nums = re.findall(r"\d+\.?\d*", text)
        if nums:
            try:
                return float(nums[-1])
            except ValueError:
                pass
        return default

    def _extract_key(self, text: str) -> str:
        """提取键名。"""
        import re
        # 查找括号内的键名
        match = re.search(r"[\(\s](w|a|s|d|space|shift|ctrl|e|q|f|r|[1-9])[\)\s,]", text)
        if match:
            return match.group(1)
        # 直接查找关键词
        for key in ["space", "shift", "ctrl", "w", "a", "s", "d", "e", "q"]:
            if key in text:
                return key
        return ""

    async def unload(self) -> None:
        """卸载模型释放 VRAM。"""
        if self._model is not None:
            import torch
            del self._model
            self._model = None
            self._processor = None
            self._loaded = False
            torch.cuda.empty_cache()
            logger.info("VLA 模型已卸载")


class FallbackEngine:
    """降级引擎：VLA 不可用时使用结构化动作。

    意识层直接输出 JSON 动作，此引擎翻译为 xdotool 序列。
    """

    def __init__(self) -> None:
        self._loaded = True

    @property
    def is_loaded(self) -> bool:
        return True

    async def infer(self, frame: PILImage.Image, intent: str) -> VLAOutput:
        """降级模式：解析结构化 JSON 意图。"""
        import json

        # 尝试从 intent 中解析 JSON
        try:
            # 查找 JSON 块
            start = intent.find("{")
            end = intent.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(intent[start:end])
                action = self._json_to_action(data)
                return VLAOutput(action=action, raw_text=intent)
        except (json.JSONDecodeError, KeyError):
            pass

        # 简单关键词匹配
        action = self._keyword_to_action(intent)
        return VLAOutput(action=action, raw_text=intent)

    def _json_to_action(self, data: dict) -> Action:
        """JSON 转 Action。"""
        act_type = data.get("action", "noop")
        match act_type:
            case "move_forward":
                return Action.key_hold("w", data.get("duration", 2.0))
            case "move_backward":
                return Action.key_hold("s", data.get("duration", 1.5))
            case "turn_left":
                return Action.move(-data.get("amount", 30), 0)
            case "turn_right":
                return Action.move(data.get("amount", 30), 0)
            case "mine":
                return Action.key_hold("left", data.get("hold", 1.5))
            case "place":
                return Action.click("right")
            case "attack":
                return Action.click("left")
            case "jump":
                return Action.press("space")
            case "open_inventory":
                return Action.press("e")
            case "select_slot":
                return Action.press(str(data.get("slot", 1)))
            case _:
                return Action.noop()

    def _keyword_to_action(self, intent: str) -> Action:
        """关键词匹配转 Action。"""
        text = intent.lower()
        if "前" in text or "forward" in text:
            return Action.key_hold("w", 2.0)
        if "后" in text or "back" in text:
            return Action.key_hold("s", 1.5)
        if "左" in text or "left" in text:
            return Action.move(-30, 0)
        if "右" in text or "right" in text:
            return Action.move(30, 0)
        if "挖" in text or "mine" in text or "dig" in text:
            return Action.key_hold("left", 1.5)
        if "砍" in text or "chop" in text:
            return Action.key_hold("left", 2.0)
        if "跳" in text or "jump" in text:
            return Action.press("space")
        if "放" in text or "place" in text:
            return Action.click("right")
        if "打" in text or "attack" in text:
            return Action.click("left")
        return Action.noop()
