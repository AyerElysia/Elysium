"""游戏窗口截图模块。

双模式截图：
- 意识层截图（高清，给她的 LLM 看）
- VLA 截图（640x360 低分辨率，给 VLA 快速推理）

WSL2 环境：通过 WinBridge 调用 Windows PrintWindow API。
"""

from __future__ import annotations

import base64
import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image as PILImage

from .win_bridge import WinBridge, get_bridge

logger = logging.getLogger("life_engine.minecraft.capture")

# VLA 推理分辨率
VLA_WIDTH = 640
VLA_HEIGHT = 360

# 截图临时文件路径（Windows 端）
_CAPTURE_PATH = "/mnt/c/Users/26652/AppData/Local/Temp/mc_capture.png"


@dataclass(slots=True)
class Frame:
    """一帧截图。"""

    image: PILImage.Image
    width: int
    height: int
    timestamp: float = 0.0
    window_hwnd: int | None = None

    def to_base64(self, fmt: str = "jpeg", quality: int = 85) -> str:
        """转为 base64 字符串（用于注入 prompt）。"""
        buf = io.BytesIO()
        img = self.image
        # JPEG 不支持 RGBA，需要转换
        if fmt.lower() in ("jpeg", "jpg") and img.mode == "RGBA":
            img = img.convert("RGB")
        img.save(buf, format=fmt.upper(), quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def resize_for_vla(self) -> "Frame":
        """缩放为 VLA 推理尺寸。"""
        resized = self.image.resize((VLA_WIDTH, VLA_HEIGHT), PILImage.LANCZOS)
        return Frame(
            image=resized,
            width=VLA_WIDTH,
            height=VLA_HEIGHT,
            timestamp=self.timestamp,
            window_hwnd=self.window_hwnd,
        )

    def save(self, path: Path | str, fmt: str = "png") -> Path:
        """保存到文件。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.image.save(p, format=fmt.upper())
        return p


class WindowCapture:
    """游戏窗口截图器（WSL2 + Windows PrintWindow）。"""

    def __init__(self, bridge: WinBridge | None = None) -> None:
        self._bridge = bridge or get_bridge()
        self._window_info: dict[str, Any] | None = None

    @property
    def window_info(self) -> dict[str, Any] | None:
        return self._window_info

    async def find_window(self) -> dict[str, Any] | None:
        """查找游戏窗口。"""
        self._window_info = await self._bridge.find_window()
        return self._window_info

    async def grab_frame(self, high_res: bool = True) -> Frame | None:
        """截取当前窗口画面。

        Args:
            high_res: True=意识层高清截图，False=VLA 低分辨率截图
        """
        img = await self._bridge.capture(_CAPTURE_PATH)
        if img is None:
            return None

        w, h = img.size
        hwnd = self._window_info.get("hwnd") if self._window_info else None

        frame = Frame(
            image=img,
            width=w,
            height=h,
            timestamp=time.time(),
            window_hwnd=hwnd,
        )

        if not high_res:
            frame = frame.resize_for_vla()

        return frame

    async def grab_consciousness_frame(self) -> Frame | None:
        """意识层截图（高清，给她的 LLM 看）。"""
        return await self.grab_frame(high_res=True)

    async def grab_vla_frame(self) -> Frame | None:
        """VLA 截图（640x360，给 VLA 快速推理）。"""
        return await self.grab_frame(high_res=False)

    async def is_window_focused(self) -> bool:
        """检查 MC 窗口是否获得焦点（简化实现）。"""
        return await self._bridge.is_running()

    async def focus_window(self) -> bool:
        """将 MC 窗口置于前台。"""
        return await self._bridge.focus_window()
