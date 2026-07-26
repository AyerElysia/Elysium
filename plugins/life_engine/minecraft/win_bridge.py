"""Windows API 桥接模块。

在 WSL2 环境中通过 powershell.exe 调用 Windows API，
实现窗口查找、截图、键鼠输入等操作。

所有 Windows 交互都通过此模块统一管理。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image as PILImage

logger = logging.getLogger("life_engine.minecraft.win_bridge")

# PowerShell 脚本在 Windows 端的缓存路径
_WIN_TEMP = Path("/mnt/c/Users/26652/AppData/Local/Temp")
_HELPER_SCRIPT = _WIN_TEMP / "mc_helper.ps1"
_CLICK_SCRIPT = _WIN_TEMP / "mc_click3.ps1"
_HOLDKEY_SCRIPT = _WIN_TEMP / "mc_holdkey.ps1"
_MOUSEMOVE_SCRIPT = _WIN_TEMP / "mc_mousemove.ps1"
_SENDKEY_SCRIPT = _WIN_TEMP / "mc_sendkey.ps1"

# 本模块源码目录（用于部署脚本到 Windows 端）
_MODULE_DIR = Path(__file__).parent


class WinBridge:
    """WSL2 → Windows API 桥接。

    通过 powershell.exe 调用 Windows 端的 .ps1 脚本，
    实现窗口管理、截图、键鼠输入。
    """

    def __init__(self, window_title: str = "Minecraft* 1.21.1") -> None:
        self._window_title = window_title
        self._hwnd: int | None = None
        self._lock = asyncio.Lock()
        self._scripts_deployed = False

    async def ensure_scripts(self) -> None:
        """确保 PowerShell 脚本已部署到 Windows 端。"""
        if self._scripts_deployed:
            return

        # 部署主 helper 脚本
        src = _MODULE_DIR / "win_helper.ps1"
        if src.exists():
            shutil.copy2(src, _HELPER_SCRIPT)
            logger.info(f"已部署 win_helper.ps1 → {_HELPER_SCRIPT}")

        # 生成点击脚本
        self._deploy_click_script()
        # 生成按键脚本
        self._deploy_holdkey_script()
        # 生成鼠标移动脚本
        self._deploy_mousemove_script()
        # 生成 SendInput 按键脚本
        self._deploy_sendkey_script()

        self._scripts_deployed = True
        logger.info("Windows 辅助脚本部署完成")

    def _deploy_click_script(self) -> None:
        """部署鼠标点击脚本（含礼貌焦点）。"""
        script = '''param([int]$ScreenX, [int]$ScreenY, [string]$Button = "left")
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class MouseClick3 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string n);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, uint d, UIntPtr e);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    public const uint MOUSEEVENTF_LEFTUP = 0x0004;
    public const uint MOUSEEVENTF_RIGHTDOWN = 0x0008;
    public const uint MOUSEEVENTF_RIGHTUP = 0x0010;
    public static void SimpleClick(int x, int y, bool right) {
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(80);
        uint down = right ? MOUSEEVENTF_RIGHTDOWN : MOUSEEVENTF_LEFTDOWN;
        uint up = right ? MOUSEEVENTF_RIGHTUP : MOUSEEVENTF_LEFTUP;
        mouse_event(down, 0, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(40);
        mouse_event(up, 0, 0, 0, UIntPtr.Zero);
    }
}
"@
# 礼貌焦点：保存用户前台窗口
$savedFg = [MouseClick3]::GetForegroundWindow()
$hwnd = [MouseClick3]::FindWindow($null, "''' + self._window_title + '''")
if ($hwnd -eq [IntPtr]::Zero) {
    $procs = Get-Process | Where-Object { $_.MainWindowTitle -like "*Minecraft*" -and $_.MainWindowHandle -ne [IntPtr]::Zero }
    if ($procs) { $hwnd = $procs[0].MainWindowHandle }
}
if ($hwnd -ne [IntPtr]::Zero) {
    [MouseClick3]::SetForegroundWindow($hwnd) | Out-Null
    Start-Sleep -Milliseconds 200
}
$right = ($Button -eq "right")
[MouseClick3]::SimpleClick($ScreenX, $ScreenY, $right)
# 恢复用户前台窗口
if ($savedFg -ne [IntPtr]::Zero -and $savedFg -ne $hwnd) {
    Start-Sleep -Milliseconds 50
    [MouseClick3]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    [MouseClick3]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
    [MouseClick3]::SetForegroundWindow($savedFg) | Out-Null
}
Write-Output "OK|$ScreenX|$ScreenY|$Button"
'''
        _CLICK_SCRIPT.write_text(script, encoding="utf-8")

    def _deploy_holdkey_script(self) -> None:
        """部署按住按键脚本（含礼貌焦点）。"""
        script = '''param([string]$Key = "w", [int]$DurationMs = 2000)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class HoldKey {
    [DllImport("user32.dll", SetLastError=true)]
    public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    [DllImport("user32.dll")] public static extern IntPtr GetMessageExtraInfo();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string n);
    [DllImport("user32.dll")] public static extern uint MapVirtualKey(uint uCode, uint uMapType);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT { public uint type; public INPUTUNION u; }
    [StructLayout(LayoutKind.Explicit)]
    public struct INPUTUNION {
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public MOUSEINPUT mi;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }
    public const uint INPUT_KEYBOARD = 1;
    public const uint KEYEVENTF_KEYUP = 0x0002;
    public static ushort GetVk(string key) {
        switch(key.ToLower()) {
            case "w": return 0x57; case "a": return 0x41;
            case "s": return 0x53; case "d": return 0x44;
            case "space": return 0x20; case "shift": return 0x10;
            case "e": return 0x45; case "q": return 0x51;
            case "enter": return 0x0D; case "escape": return 0x1B;
            case "tab": return 0x09; case "ctrl": return 0x11;
            case "1": return 0x31; case "2": return 0x32;
            case "3": return 0x33; case "4": return 0x34;
            case "5": return 0x35; case "6": return 0x36;
            case "7": return 0x37; case "8": return 0x38;
            case "9": return 0x39; case "f": return 0x46;
            case "r": return 0x52; case "t": return 0x54;
            default: return (ushort)Char.ToUpper(key[0]);
        }
    }
    public static void PressAndHold(ushort vk, int durationMs) {
        ushort scan = (ushort)MapVirtualKey(vk, 0);
        INPUT[] down = new INPUT[1];
        down[0].type = INPUT_KEYBOARD;
        down[0].u.ki.wVk = vk; down[0].u.ki.wScan = scan;
        down[0].u.ki.dwFlags = 0; down[0].u.ki.dwExtraInfo = GetMessageExtraInfo();
        SendInput(1, down, Marshal.SizeOf(typeof(INPUT)));
        System.Threading.Thread.Sleep(durationMs);
        INPUT[] up = new INPUT[1];
        up[0].type = INPUT_KEYBOARD;
        up[0].u.ki.wVk = vk; up[0].u.ki.wScan = scan;
        up[0].u.ki.dwFlags = KEYEVENTF_KEYUP; up[0].u.ki.dwExtraInfo = GetMessageExtraInfo();
        SendInput(1, up, Marshal.SizeOf(typeof(INPUT)));
    }
}
"@
# 礼貌焦点：保存用户前台窗口
$savedFg = [HoldKey]::GetForegroundWindow()
$hwnd = [HoldKey]::FindWindow($null, "''' + self._window_title + '''")
if ($hwnd -eq [IntPtr]::Zero) {
    $procs = Get-Process | Where-Object { $_.MainWindowTitle -like "*Minecraft*" -and $_.MainWindowHandle -ne [IntPtr]::Zero }
    if ($procs) { $hwnd = $procs[0].MainWindowHandle }
}
if ($hwnd -ne [IntPtr]::Zero) {
    [HoldKey]::SetForegroundWindow($hwnd) | Out-Null
    Start-Sleep -Milliseconds 150
}
$vk = [HoldKey]::GetVk($Key)
[HoldKey]::PressAndHold($vk, $DurationMs)
# 恢复用户前台窗口
if ($savedFg -ne [IntPtr]::Zero -and $savedFg -ne $hwnd) {
    [HoldKey]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    [HoldKey]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
    [HoldKey]::SetForegroundWindow($savedFg) | Out-Null
}
Write-Output "OK|$Key|${DurationMs}ms"
'''
        _HOLDKEY_SCRIPT.write_text(script, encoding="utf-8")

    def _deploy_mousemove_script(self) -> None:
        """部署鼠标相对移动脚本（含礼貌焦点）。"""
        script = '''param([int]$DX = 0, [int]$DY = 0)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class MouseMoveRel {
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, uint d, UIntPtr e);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string n);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    public const uint MOUSEEVENTF_MOVE = 0x0001;
    public static void Move(int dx, int dy) { mouse_event(MOUSEEVENTF_MOVE, dx, dy, 0, UIntPtr.Zero); }
}
"@
# 礼貌焦点
$savedFg = [MouseMoveRel]::GetForegroundWindow()
$hwnd = [MouseMoveRel]::FindWindow($null, "''' + self._window_title + '''")
if ($hwnd -eq [IntPtr]::Zero) {
    $procs = Get-Process | Where-Object { $_.MainWindowTitle -like "*Minecraft*" -and $_.MainWindowHandle -ne [IntPtr]::Zero }
    if ($procs) { $hwnd = $procs[0].MainWindowHandle }
}
if ($hwnd -ne [IntPtr]::Zero) {
    [MouseMoveRel]::SetForegroundWindow($hwnd) | Out-Null
    Start-Sleep -Milliseconds 100
}
[MouseMoveRel]::Move($DX, $DY)
# 恢复
if ($savedFg -ne [IntPtr]::Zero -and $savedFg -ne $hwnd) {
    Start-Sleep -Milliseconds 50
    [MouseMoveRel]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    [MouseMoveRel]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
    [MouseMoveRel]::SetForegroundWindow($savedFg) | Out-Null
}
Write-Output "OK|$DX|$DY"
'''
        _MOUSEMOVE_SCRIPT.write_text(script, encoding="utf-8")

    def _deploy_sendkey_script(self) -> None:
        """部署 SendInput 按键脚本（单次按键，含礼貌焦点）。"""
        script = '''param([string]$Key = "enter")
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SendKeyInput {
    [DllImport("user32.dll", SetLastError=true)]
    public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    [DllImport("user32.dll")] public static extern IntPtr GetMessageExtraInfo();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string n);
    [DllImport("user32.dll")] public static extern uint MapVirtualKey(uint uCode, uint uMapType);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT { public uint type; public INPUTUNION u; }
    [StructLayout(LayoutKind.Explicit)]
    public struct INPUTUNION {
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public MOUSEINPUT mi;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }
    public const uint INPUT_KEYBOARD = 1;
    public const uint KEYEVENTF_KEYUP = 0x0002;
    public static ushort GetVk(string key) {
        switch(key.ToLower()) {
            case "w": return 0x57; case "a": return 0x41;
            case "s": return 0x53; case "d": return 0x44;
            case "space": return 0x20; case "shift": return 0x10;
            case "e": return 0x45; case "q": return 0x51;
            case "enter": return 0x0D; case "escape": return 0x1B;
            case "tab": return 0x09; case "ctrl": return 0x11;
            case "1": return 0x31; case "2": return 0x32;
            case "3": return 0x33; case "4": return 0x34;
            case "5": return 0x35; case "6": return 0x36;
            case "7": return 0x37; case "8": return 0x38;
            case "9": return 0x39; case "f": return 0x46;
            case "r": return 0x52; case "t": return 0x54;
            default: return (ushort)Char.ToUpper(key[0]);
        }
    }
    public static uint SendKeyPress(ushort vk) {
        ushort scan = (ushort)MapVirtualKey(vk, 0);
        INPUT[] inputs = new INPUT[2];
        inputs[0].type = INPUT_KEYBOARD;
        inputs[0].u.ki.wVk = vk; inputs[0].u.ki.wScan = scan;
        inputs[0].u.ki.dwFlags = 0; inputs[0].u.ki.dwExtraInfo = GetMessageExtraInfo();
        inputs[1].type = INPUT_KEYBOARD;
        inputs[1].u.ki.wVk = vk; inputs[1].u.ki.wScan = scan;
        inputs[1].u.ki.dwFlags = KEYEVENTF_KEYUP; inputs[1].u.ki.dwExtraInfo = GetMessageExtraInfo();
        return SendInput(2, inputs, Marshal.SizeOf(typeof(INPUT)));
    }
}
"@
# 礼貌焦点
$savedFg = [SendKeyInput]::GetForegroundWindow()
$hwnd = [SendKeyInput]::FindWindow($null, "''' + self._window_title + '''")
if ($hwnd -eq [IntPtr]::Zero) {
    $procs = Get-Process | Where-Object { $_.MainWindowTitle -like "*Minecraft*" -and $_.MainWindowHandle -ne [IntPtr]::Zero }
    if ($procs) { $hwnd = $procs[0].MainWindowHandle }
}
if ($hwnd -ne [IntPtr]::Zero) {
    [SendKeyInput]::SetForegroundWindow($hwnd) | Out-Null
    Start-Sleep -Milliseconds 150
}
$vk = [SendKeyInput]::GetVk($Key)
$result = [SendKeyInput]::SendKeyPress($vk)
# 恢复
if ($savedFg -ne [IntPtr]::Zero -and $savedFg -ne $hwnd) {
    Start-Sleep -Milliseconds 50
    [SendKeyInput]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    [SendKeyInput]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
    [SendKeyInput]::SetForegroundWindow($savedFg) | Out-Null
}
Write-Output "OK|$Key|$result"
'''
        _SENDKEY_SCRIPT.write_text(script, encoding="utf-8")

    # === 核心调用方法 ===

    async def _run_ps(self, script_path: Path, *args: str, timeout: float = 15.0) -> str:
        """执行 PowerShell 脚本并返回输出。"""
        cmd = [
            "powershell.exe", "-ExecutionPolicy", "Bypass",
            "-File", str(script_path).replace("/mnt/c", "C:").replace("/", "\\"),
        ] + list(args)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="/mnt/c",  # 避免 UNC 路径问题
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode(errors="ignore").strip()
            if proc.returncode != 0 and not output:
                err = stderr.decode(errors="ignore")[:300]
                logger.warning(f"PowerShell 错误: {err}")
            return output
        except asyncio.TimeoutError:
            logger.warning(f"PowerShell 超时: {script_path.name}")
            return ""
        except Exception as exc:
            logger.error(f"PowerShell 执行失败: {exc}")
            return ""

    async def _run_ps_command(self, command: str, *args: str, timeout: float = 15.0) -> str:
        """执行 mc_helper.ps1 的命令。"""
        await self.ensure_scripts()
        return await self._run_ps(_HELPER_SCRIPT, command, *args, timeout=timeout)

    # === 窗口管理 ===

    async def find_window(self) -> dict[str, Any] | None:
        """查找 MC 窗口，返回 {hwnd, x, y, w, h}。"""
        output = await self._run_ps_command("find")
        if output.startswith("OK|"):
            parts = output.split("|")
            if len(parts) >= 6:
                self._hwnd = int(parts[1])
                return {
                    "hwnd": int(parts[1]),
                    "x": int(parts[2]),
                    "y": int(parts[3]),
                    "w": int(parts[4]),
                    "h": int(parts[5]),
                }
        return None

    async def is_running(self) -> bool:
        """检查 MC 进程是否在运行。"""
        output = await self._run_ps_command("is_running")
        return output.startswith("RUNNING")

    async def focus_window(self) -> bool:
        """将 MC 窗口置于前台。"""
        output = await self._run_ps_command("focus")
        return output == "OK"

    async def save_focus(self) -> int | None:
        """保存当前前台窗口 HWND。"""
        output = await self._run_ps_command("save_focus")
        if output.startswith("OK|"):
            return int(output.split("|")[1])
        return None

    async def restore_focus(self, hwnd: int) -> bool:
        """恢复指定 HWND 为前台窗口。"""
        output = await self._run_ps_command("restore_focus", str(hwnd))
        return output == "OK"

    async def position_window(self, x: int, y: int, w: int, h: int) -> bool:
        """移动/调整 MC 窗口位置和大小。"""
        output = await self._run_ps_command(
            "position_window", str(x), str(y), str(w), str(h)
        )
        return output.startswith("OK")

    # === 截图 ===

    async def capture(self, output_path: str | None = None) -> PILImage.Image | None:
        """截取 MC 窗口画面。"""
        if not output_path:
            output_path = str(_WIN_TEMP / "mc_capture.png")

        # 转换为 Windows 路径
        win_path = output_path.replace("/mnt/c", "C:").replace("/", "\\")
        output = await self._run_ps_command("capture", win_path, timeout=10.0)

        if output.startswith("OK|"):
            # 读取截图文件
            img_path = Path(output_path)
            if img_path.exists():
                try:
                    img = PILImage.open(img_path)
                    img.load()
                    return img
                except Exception as exc:
                    logger.debug(f"读取截图失败: {exc}")
        return None

    # === 键盘输入 ===

    async def press_key(self, key: str) -> bool:
        """按下并释放一个键（SendInput）。"""
        await self.ensure_scripts()
        output = await self._run_ps(_SENDKEY_SCRIPT, "-Key", key)
        return output.startswith("OK")

    async def hold_key(self, key: str, duration_ms: int = 2000) -> bool:
        """按住一个键指定时间。"""
        await self.ensure_scripts()
        output = await self._run_ps(
            _HOLDKEY_SCRIPT, "-Key", key, "-DurationMs", str(duration_ms),
            timeout=duration_ms / 1000 + 10,
        )
        return output.startswith("OK")

    # === 鼠标输入 ===

    async def click_at(self, screen_x: int, screen_y: int, button: str = "left") -> bool:
        """在屏幕绝对坐标点击。"""
        await self.ensure_scripts()
        output = await self._run_ps(
            _CLICK_SCRIPT, "-ScreenX", str(screen_x), "-ScreenY", str(screen_y), "-Button", button
        )
        return output.startswith("OK")

    async def mouse_move(self, dx: int, dy: int) -> bool:
        """鼠标相对移动（视角旋转）。"""
        await self.ensure_scripts()
        output = await self._run_ps(_MOUSEMOVE_SCRIPT, "-DX", str(dx), "-DY", str(dy))
        return output.startswith("OK")

    async def scroll(self, amount: int) -> bool:
        """滚轮操作。正数=向上/前，负数=向下/后（切换快捷栏）。"""
        output = await self._run_ps_command("scroll", str(amount))
        return output.startswith("OK")

    async def hold_mouse(self, button: str = "left", duration_ms: int = 1500) -> bool:
        """按住鼠标按键指定时长（挖掘/攻击/放置）。"""
        output = await self._run_ps_command(
            "holdmouse", button, str(duration_ms),
            timeout=duration_ms / 1000 + 10,
        )
        return output.startswith("OK")

    async def type_text(self, text: str) -> bool:
        """输入文本（通过剪贴板粘贴，支持中文/Unicode）。"""
        output = await self._run_ps_command("typetext", text, timeout=8.0)
        return output.startswith("OK")

    async def click_relative(
        self, win_info: dict[str, Any], rel_x: float, rel_y: float, button: str = "left"
    ) -> bool:
        """在窗口内相对位置点击 (0.0~1.0)。"""
        screen_x = win_info["x"] + int(win_info["w"] * rel_x)
        screen_y = win_info["y"] + int(win_info["h"] * rel_y)
        return await self.click_at(screen_x, screen_y, button)


# 全局单例
_bridge: WinBridge | None = None


def get_bridge(window_title: str = "Minecraft* 1.21.1") -> WinBridge:
    """获取全局 WinBridge 实例。"""
    global _bridge
    if _bridge is None:
        _bridge = WinBridge(window_title)
    return _bridge
