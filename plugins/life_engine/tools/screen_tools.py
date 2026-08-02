"""life_engine 屏幕观察工具。

让 life_chatter / life heartbeat 可以按需截取当前桌面，并通过视觉模型生成
一段可进入 tool result 的观察摘要。截图本身默认只在临时目录短暂存在。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from PIL import Image as PILImage
from PIL import ImageGrab

from src.app.plugin_system.api import log_api
from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.base import BaseTool
from src.kernel.llm import Image, LLMContextManager, LLMPayload, ROLE, Text

from ..core.multimodal import _is_supported_image_data
from ._utils import _get_workspace


logger = log_api.get_logger("life_engine.screen_tools")

_DEFAULT_WIDTH = 2560
_DEFAULT_HEIGHT = 1440
_DEFAULT_TIMEOUT_SECONDS = 20
_SUPPORTED_OUTPUT_FORMATS = {"png", "jpeg", "jpg", "webp"}

_SCREEN_SYSTEM_PROMPT = (
    "你正在帮爱莉观察 Ayer 当前电脑屏幕。"
    "请只基于截图中看得见的内容回答，不要编造。"
    "优先描述当前正在做什么、打开了哪些主要窗口、屏幕上显眼的文字/错误/代码/按钮。"
    "如果有密码、token、cookie、私钥、验证码等敏感信息，不要逐字复述，只说明看到了敏感内容。"
    "如果画面模糊或内容太多，请明确说看不清哪些部分。"
)

# 判断当前是否运行在 WSL（Windows Subsystem for Linux）环境中。
_WSL_DETECTED: bool | None = None


def _is_wsl() -> bool:
    """检测是否运行在 WSL 环境中（通过 /proc/version 中是否含 microsoft 关键字判断）。"""
    global _WSL_DETECTED
    if _WSL_DETECTED is None:
        try:
            version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
            _WSL_DETECTED = "microsoft" in version
        except Exception:
            _WSL_DETECTED = False
    return _WSL_DETECTED


# PowerShell 截图的临时文件落点（Windows Temp 目录通过 /mnt/c 访问）。
_POWERSHELL_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
_WINDOWS_TEMP_MOUNT = "/mnt/c/Windows/Temp"

# 黑图平均像素亮度阈值：低于此值视为全黑截图（深色主题仍有内容时通常远高于 5）。
_BLANK_IMAGE_MEAN_THRESHOLD = 5.0


@dataclass(slots=True)
class CapturedScreen:
    """一次屏幕截图结果。"""

    base64_data: str
    width: int
    height: int
    image_format: str
    captured_at: str
    method: str
    saved_path: str = ""


def _get_config(plugin: Any) -> Any:
    return getattr(plugin, "config", None)


def _get_screen_cfg(plugin: Any) -> Any:
    return getattr(_get_config(plugin), "screen", None)


def _get_multimodal_cfg(plugin: Any) -> Any:
    return getattr(_get_config(plugin), "multimodal", None)


def _get_life_model_task(plugin: Any) -> str:
    cfg = _get_config(plugin)
    model_cfg = getattr(cfg, "model", None)
    return str(getattr(model_cfg, "task_name", "") or "life").strip() or "life"


def _normalize_mode(mode: str) -> str:
    value = str(mode or "auto").strip().lower()
    return value if value in {"auto", "native", "fallback"} else "auto"


def _normalize_detail(detail: str) -> str:
    value = str(detail or "normal").strip().lower()
    return value if value in {"brief", "normal", "detailed"} else "normal"


def _normalize_output_format(value: str) -> str:
    fmt = str(value or "png").strip().lower()
    if fmt not in _SUPPORTED_OUTPUT_FORMATS:
        fmt = "png"
    return "jpeg" if fmt == "jpg" else fmt


def _display_value(screen_cfg: Any) -> str:
    configured = str(getattr(screen_cfg, "display", "") or "").strip()
    return configured or os.environ.get("DISPLAY") or ":0"


async def _run_command(args: list[str], timeout_seconds: int) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, f"未找到命令: {args[0]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"启动命令失败: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return False, f"命令超时: {' '.join(args[:3])}"

    if proc.returncode != 0:
        text = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
        return False, text or f"命令退出码 {proc.returncode}"
    return True, (stdout or b"").decode("utf-8", errors="replace")


async def _detect_screen_size(screen_cfg: Any) -> tuple[int, int]:
    try:
        configured_width = int(getattr(screen_cfg, "screen_width", 0) or 0)
        configured_height = int(getattr(screen_cfg, "screen_height", 0) or 0)
        if configured_width > 0 and configured_height > 0:
            return configured_width, configured_height
    except Exception:
        pass

    display = _display_value(screen_cfg)
    if shutil.which("xdpyinfo"):
        ok, output = await _run_command(
            ["xdpyinfo", "-display", display],
            timeout_seconds=5,
        )
        if ok:
            match = re.search(r"dimensions:\s+(\d+)x(\d+)\s+pixels", output)
            if match:
                return int(match.group(1)), int(match.group(2))

    return _DEFAULT_WIDTH, _DEFAULT_HEIGHT


def _resize_and_resave(path: Path, screen_cfg: Any) -> tuple[int, int, str]:
    output_format = _normalize_output_format(getattr(screen_cfg, "output_format", "png"))
    max_width = int(getattr(screen_cfg, "max_width", 2560) or 0)
    max_height = int(getattr(screen_cfg, "max_height", 1600) or 0)
    jpeg_quality = int(getattr(screen_cfg, "jpeg_quality", 92) or 92)
    jpeg_quality = max(40, min(100, jpeg_quality))

    with PILImage.open(path) as image:
        image.load()
        if max_width > 0 and max_height > 0:
            image.thumbnail((max_width, max_height), PILImage.Resampling.LANCZOS)

        if output_format == "jpeg" and image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")

        save_kwargs: dict[str, Any] = {}
        if output_format == "png":
            save_kwargs["optimize"] = True
        elif output_format == "jpeg":
            save_kwargs["quality"] = jpeg_quality
            save_kwargs["optimize"] = True
        elif output_format == "webp":
            save_kwargs["quality"] = jpeg_quality

        image.save(path, format=output_format.upper(), **save_kwargs)
        return image.width, image.height, output_format


def _is_blank_image(path: Path) -> bool:
    """检测截图是否为全黑/空白图（平均像素亮度低于阈值）。

    用于过滤 ffmpeg x11grab 在 WSLg 环境下返回全黑截图但 exit code 0 的假成功情况。
    """
    try:
        with PILImage.open(path) as img:
            img.load()
            # 转灰度后取平均值，速度快且跨格式
            gray = img.convert("L")
            pixels = list(gray.get_flattened_data())
            if not pixels:
                return True
            mean = sum(pixels) / len(pixels)
            return mean < _BLANK_IMAGE_MEAN_THRESHOLD
    except Exception:
        return True


async def _capture_with_powershell(path: Path, screen_cfg: Any) -> tuple[bool, str]:
    """使用 PowerShell + .NET System.Drawing 截取 Windows 桌面（仅 WSL 环境有效）。

    通过 SetProcessDPIAware() 确保截取物理像素分辨率（2K 等高分屏不被缩放）。
    截图先写入 Windows Temp 目录，再通过 /mnt/c 路径复制到 Linux 临时文件。
    """
    if not shutil.which(_POWERSHELL_EXE) and not Path(_POWERSHELL_EXE).exists():
        return False, f"未找到 PowerShell: {_POWERSHELL_EXE}"

    win_temp_dir = Path(_WINDOWS_TEMP_MOUNT)
    if not win_temp_dir.is_dir():
        return False, f"Windows Temp 目录不可访问: {_WINDOWS_TEMP_MOUNT}"

    # 使用唯一文件名避免并发冲突
    unique_name = f"elysia_screen_{uuid.uuid4().hex}.png"
    win_save_path = win_temp_dir / unique_name
    # PowerShell 看到的路径（反斜杠，Windows 格式）
    win_path_ps = f"C:\\Windows\\Temp\\{unique_name}"

    timeout_seconds = int(
        getattr(screen_cfg, "capture_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
        or _DEFAULT_TIMEOUT_SECONDS
    )

    ps_script = (
        "Add-Type @'\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class DpiHelper {\n"
        "    [DllImport(\"user32.dll\")]\n"
        "    public static extern bool SetProcessDPIAware();\n"
        "}\n"
        "'@\n"
        "[DpiHelper]::SetProcessDPIAware() | Out-Null\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "Add-Type -AssemblyName System.Drawing\n"
        "$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds\n"
        "$bitmap = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height\n"
        "$graphics = [System.Drawing.Graphics]::FromImage($bitmap)\n"
        "$graphics.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)\n"
        f"$bitmap.Save('{win_path_ps}', [System.Drawing.Imaging.ImageFormat]::Png)\n"
        "$graphics.Dispose()\n"
        "$bitmap.Dispose()\n"
        "Write-Output 'OK'\n"
    )

    try:
        ok, result = await _run_command(
            [_POWERSHELL_EXE, "-NonInteractive", "-Command", ps_script],
            timeout_seconds=timeout_seconds,
        )
        if not ok:
            return False, f"PowerShell 截图失败: {result}"

        if not win_save_path.exists() or win_save_path.stat().st_size == 0:
            return False, "PowerShell 截图文件未生成或为空"

        # 复制到 Linux 目标路径
        shutil.copyfile(win_save_path, path)
        return True, ""
    finally:
        win_save_path.unlink(missing_ok=True)


async def _capture_with_ffmpeg(path: Path, screen_cfg: Any) -> tuple[bool, str]:
    if not shutil.which("ffmpeg"):
        return False, "未安装 ffmpeg"

    width, height = await _detect_screen_size(screen_cfg)
    display = _display_value(screen_cfg)
    timeout_seconds = int(getattr(screen_cfg, "capture_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS) or _DEFAULT_TIMEOUT_SECONDS)
    capture_cursor = bool(getattr(screen_cfg, "capture_cursor", True))

    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "x11grab",
        "-draw_mouse",
        "1" if capture_cursor else "0",
        "-video_size",
        f"{width}x{height}",
        "-i",
        display,
        "-frames:v",
        "1",
        str(path),
    ]
    ok, result = await _run_command(args, timeout_seconds=timeout_seconds)
    return ok, result


async def _capture_with_grim(path: Path, screen_cfg: Any) -> tuple[bool, str]:
    if not shutil.which("grim"):
        return False, "未安装 grim"
    timeout_seconds = int(getattr(screen_cfg, "capture_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS) or _DEFAULT_TIMEOUT_SECONDS)
    return await _run_command(["grim", str(path)], timeout_seconds=timeout_seconds)


async def _capture_with_pil(path: Path, screen_cfg: Any) -> tuple[bool, str]:
    def _grab() -> None:
        image = ImageGrab.grab()
        image.save(path, format="PNG")

    try:
        await asyncio.to_thread(_grab)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"PIL ImageGrab 失败: {exc}"


async def _capture_screen(plugin: Any) -> CapturedScreen:
    screen_cfg = _get_screen_cfg(plugin)
    method = str(getattr(screen_cfg, "capture_method", "auto") or "auto").strip().lower()
    if method not in {"auto", "ffmpeg", "grim", "pil", "powershell"}:
        method = "auto"

    fd, temp_name = tempfile.mkstemp(prefix="life_screen_", suffix=".png")
    os.close(fd)
    temp_path = Path(temp_name)

    methods: list[tuple[str, Any]]
    if method == "auto":
        if _is_wsl():
            # WSL 环境：优先用 PowerShell 截取真实 Windows 桌面，x11grab 在 WSLg 下返回黑屏
            methods = [
                ("powershell", _capture_with_powershell),
                ("grim", _capture_with_grim),
                ("ffmpeg", _capture_with_ffmpeg),
                ("pil", _capture_with_pil),
            ]
        else:
            methods = [
                ("ffmpeg", _capture_with_ffmpeg),
                ("grim", _capture_with_grim),
                ("pil", _capture_with_pil),
            ]
    elif method == "powershell":
        methods = [("powershell", _capture_with_powershell)]
    elif method == "ffmpeg":
        methods = [("ffmpeg", _capture_with_ffmpeg)]
    elif method == "grim":
        methods = [("grim", _capture_with_grim)]
    else:
        methods = [("pil", _capture_with_pil)]

    errors: list[str] = []
    used_method = ""
    try:
        for method_name, capture_func in methods:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            ok, detail = await capture_func(temp_path, screen_cfg)
            if ok and temp_path.exists() and temp_path.stat().st_size > 0:
                if _is_blank_image(temp_path):
                    errors.append(f"{method_name}: 截图为全黑/空白，已跳过")
                    logger.warning(f"截图方法 {method_name} 返回全黑图片，跳过（常见于 WSLg x11grab）")
                    continue
                used_method = method_name
                break
            errors.append(f"{method_name}: {detail}")

        if not used_method:
            raise RuntimeError("无法截屏；" + " | ".join(errors))

        width, height, image_format = await asyncio.to_thread(_resize_and_resave, temp_path, screen_cfg)
        b64 = base64.b64encode(temp_path.read_bytes()).decode("ascii")
        if not _is_supported_image_data(f"base64|{b64}"):
            raise RuntimeError("截图生成后未通过图片格式校验")

        saved_path = ""
        if bool(getattr(screen_cfg, "save_latest", False)):
            workspace = _get_workspace(plugin)
            relative = str(getattr(screen_cfg, "latest_path", "screenshots/latest_screen.png") or "").strip()
            relative = relative.lstrip("/\\") or "screenshots/latest_screen.png"
            latest_path = (workspace / relative).resolve()
            latest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(temp_path, latest_path)
            saved_path = str(latest_path)

        return CapturedScreen(
            base64_data=b64,
            width=width,
            height=height,
            image_format=image_format,
            captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            method=used_method,
            saved_path=saved_path,
        )
    finally:
        temp_path.unlink(missing_ok=True)


def _native_image_enabled(plugin: Any) -> bool:
    screen_cfg = _get_screen_cfg(plugin)
    return bool(getattr(screen_cfg, "native_when_available", True))


def _build_observation_prompt(focus: str, detail: str) -> str:
    detail = _normalize_detail(detail)
    detail_hint = {
        "brief": "用 3-5 句话概括。",
        "normal": "先给整体判断，再列出关键可见内容和你认为值得注意的地方。",
        "detailed": "尽量细致地描述窗口、文字、错误、代码、任务状态和可操作线索。",
    }[detail]
    focus_text = str(focus or "").strip()
    if focus_text:
        return (
            "请观察这张刚刚截取的电脑屏幕。\n"
            f"关注点：{focus_text}\n"
            f"详细程度：{detail_hint}"
        )
    return (
        "请观察这张刚刚截取的电脑屏幕。\n"
        "目标：帮助爱莉理解 Ayer 此刻正在做什么、屏幕上有什么重要上下文。\n"
        f"详细程度：{detail_hint}"
    )


async def _analyze_screenshot_with_model(
    *,
    model_task_name: str,
    image_data_url: str,
    prompt: str,
    request_name: str,
) -> str:
    model_set = get_model_set_by_task(model_task_name)
    request = create_llm_request(
        model_set,
        request_name=request_name,
        context_manager=LLMContextManager(),
    )
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_SCREEN_SYSTEM_PROMPT)))
    request.add_payload(LLMPayload(ROLE.USER, [Text(prompt), Image(image_data_url)]))
    response = await request.send(stream=False)
    await response
    return str(getattr(response, "message", "") or "").strip()


async def _observe_screen(
    plugin: Any,
    captured: CapturedScreen,
    *,
    focus: str,
    detail: str,
    mode: str,
) -> tuple[str, str]:
    screen_cfg = _get_screen_cfg(plugin)
    image_data_url = f"data:image/{captured.image_format};base64,{captured.base64_data}"
    prompt = _build_observation_prompt(focus, detail)

    normalized_mode = _normalize_mode(mode)
    native_allowed = _native_image_enabled(plugin)
    if normalized_mode == "native":
        use_native = True
    elif normalized_mode == "fallback":
        use_native = False
    else:
        use_native = native_allowed

    fallback_error = ""
    if use_native:
        native_task = str(getattr(screen_cfg, "native_task_name", "") or "").strip()
        native_task = native_task or _get_life_model_task(plugin)
        try:
            observation = await _analyze_screenshot_with_model(
                model_task_name=native_task,
                image_data_url=image_data_url,
                prompt=prompt,
                request_name="life_screen_native",
            )
            if observation:
                return "native_image", observation
        except Exception as exc:  # noqa: BLE001
            fallback_error = str(exc)
            logger.warning(f"原生屏幕视觉请求失败，准备走降级路径: {exc}")

    fallback_task = str(getattr(screen_cfg, "fallback_task_name", "vision") or "vision").strip() or "vision"
    try:
        observation = await _analyze_screenshot_with_model(
            model_task_name=fallback_task,
            image_data_url=image_data_url,
            prompt=prompt,
            request_name="life_screen_fallback",
        )
        if observation:
            if fallback_error:
                observation = f"（原生视觉失败，已走 VLM 降级：{fallback_error}）\n{observation}"
            return "vlm_fallback", observation
    except Exception as exc:  # noqa: BLE001
        logger.error(f"屏幕 VLM 降级识别失败: {exc}", exc_info=True)
        if fallback_error:
            raise RuntimeError(f"原生视觉失败: {fallback_error}; VLM 降级也失败: {exc}") from exc
        raise

    raise RuntimeError("视觉模型没有返回可用观察结果")


def _truncate_observation(text: str, plugin: Any) -> tuple[str, bool]:
    screen_cfg = _get_screen_cfg(plugin)
    try:
        limit = int(getattr(screen_cfg, "max_observation_chars", 2400) or 2400)
    except Exception:
        limit = 2400
    limit = max(200, min(12000, limit))
    if len(text) <= limit:
        return text, False
    return text[: limit - 20].rstrip() + "\n...[已截断]", True


class LifeEngineViewScreenTool(BaseTool):
    """截取并观察当前电脑屏幕。"""

    tool_name: str = "nucleus_view_screen"
    tool_description: str = (
        "截取 Ayer 当前电脑屏幕，并让视觉模型读图后返回观察结果。"
        "\n\n"
        "**何时使用：**\n"
        "- 你需要知道 Ayer 当前电脑上正在做什么、屏幕上有什么错误/代码/窗口\n"
        "- 用户让你“看看屏幕”“帮我盯一下”“你自己看电脑现在是什么情况”\n"
        "- 你想结合屏幕内容陪伴、提醒、解释当前状态\n"
        "\n"
        "**何时不用：**\n"
        "- 只靠聊天上下文就能回答\n"
        "- 没有必要看屏幕却频繁窥探；屏幕可能包含隐私信息\n"
        "\n"
        "**路径策略：** auto 模式下，screen.native_when_available=true 时走原生图片视觉；"
        "否则走 VLM 降级路径。屏幕视觉与 life_chatter 原生媒体注入是独立开关。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        focus: Annotated[str, "本次看屏幕的关注点。留空表示整体观察当前屏幕。"] = "",
        detail: Annotated[str, "详细程度：brief / normal / detailed。默认 normal。"] = "normal",
        mode: Annotated[str, "视觉路径：auto / native / fallback。默认 auto。"] = "auto",
    ) -> tuple[bool, str | dict]:
        screen_cfg = _get_screen_cfg(self.plugin)
        if screen_cfg is None:
            return False, "life_engine 缺少 screen 配置区段，无法截屏。"
        if not bool(getattr(screen_cfg, "enabled", False)):
            return False, "屏幕观察工具未启用。请在 config/plugins/life_engine/config.toml 的 [screen] 中设置 enabled = true。"

        try:
            captured = await _capture_screen(self.plugin)
            used_mode, observation = await _observe_screen(
                self.plugin,
                captured,
                focus=focus,
                detail=detail,
                mode=mode,
            )
            observation, truncated = _truncate_observation(observation, self.plugin)
            result: dict[str, Any] = {
                "observation": observation,
                "mode": used_mode,
                "captured_at": captured.captured_at,
                "screen_size": f"{captured.width}x{captured.height}",
                "image_format": captured.image_format,
                "capture_method": captured.method,
                "truncated": truncated,
            }
            if captured.saved_path:
                result["saved_path"] = captured.saved_path
            return True, result
        except Exception as exc:  # noqa: BLE001
            logger.error(f"观察屏幕失败: {exc}", exc_info=True)
            return False, f"观察屏幕失败: {exc}"


SCREEN_TOOLS = [LifeEngineViewScreenTool]


__all__ = [
    "CapturedScreen",
    "LifeEngineViewScreenTool",
    "SCREEN_TOOLS",
    "_capture_screen",
    "_observe_screen",
    "_is_wsl",
    "_is_blank_image",
    "_capture_with_powershell",
]
