# Minecraft Window Helper - Windows side operations
# Usage: powershell.exe -ExecutionPolicy Bypass -File mc_helper.ps1 <command> [args...]
# Commands: find, capture, key, keydown, keyup, mousemove, mouseclick, mousedown, mouseup, scroll, focus, is_running

param(
    [Parameter(Position=0, Mandatory=$true)]
    [string]$Command,
    [Parameter(Position=1)]
    [string]$Arg1 = "",
    [Parameter(Position=2)]
    [string]$Arg2 = "",
    [Parameter(Position=3)]
    [string]$Arg3 = "",
    [Parameter(Position=4)]
    [string]$Arg4 = ""
)

$ErrorActionPreference = "Stop"

# === P/Invoke definitions ===
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Drawing;
using System.Drawing.Imaging;

public class WinAPI {
    [DllImport("user32.dll")]
    public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern bool IsWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll", SetLastError=true)]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [DllImport("user32.dll", SetLastError=true)]
    public static extern void mouse_event(uint dwFlags, int dx, int dy, uint dwData, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);

    [DllImport("user32.dll")]
    public static extern bool GetCursorPos(out POINT lpPoint);

    [DllImport("user32.dll")]
    public static extern IntPtr WindowFromPoint(POINT point);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {
        public int Left, Top, Right, Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT {
        public int X, Y;
    }

    // Virtual Key Codes
    public static byte VkKeyScan(char c) {
        return (byte)Char.ToUpper(c);
    }

    // mouse_event flags
    public const uint MOUSEEVENTF_MOVE = 0x0001;
    public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    public const uint MOUSEEVENTF_LEFTUP = 0x0004;
    public const uint MOUSEEVENTF_RIGHTDOWN = 0x0008;
    public const uint MOUSEEVENTF_RIGHTUP = 0x0010;
    public const uint MOUSEEVENTF_MIDDLEDOWN = 0x0020;
    public const uint MOUSEEVENTF_MIDDLEUP = 0x0040;
    public const uint MOUSEEVENTF_WHEEL = 0x0800;

    // keybd_event flags
    public const uint KEYEVENTF_KEYDOWN = 0x0000;
    public const uint KEYEVENTF_KEYUP = 0x0002;

    // VK codes
    public const byte VK_SPACE = 0x20;
    public const byte VK_SHIFT = 0x10;
    public const byte VK_CONTROL = 0x11;
    public const byte VK_ESCAPE = 0x1B;
    public const byte VK_TAB = 0x09;
    public const byte VK_RETURN = 0x0D;

    public static byte GetVkCode(string key) {
        switch(key.ToLower()) {
            case "space": return VK_SPACE;
            case "shift": return VK_SHIFT;
            case "ctrl": case "control": return VK_CONTROL;
            case "escape": case "esc": return VK_ESCAPE;
            case "tab": return VK_TAB;
            case "enter": case "return": return VK_RETURN;
            default:
                if (key.Length == 1) return (byte)Char.ToUpper(key[0]);
                // F1-F12
                if (key.StartsWith("f") && key.Length <= 3) {
                    int fn;
                    if (int.TryParse(key.Substring(1), out fn) && fn >= 1 && fn <= 12)
                        return (byte)(0x6F + fn);
                }
                return (byte)Char.ToUpper(key[0]);
        }
    }
}
"@ -ReferencedAssemblies System.Drawing

function Find-MinecraftWindow {
    # Try exact title first, then partial match
    $hwnd = [WinAPI]::FindWindow($null, "Minecraft* 1.21.1")
    if ($hwnd -eq [IntPtr]::Zero) {
        $hwnd = [WinAPI]::FindWindow($null, "Minecraft 1.21.1")
    }
    if ($hwnd -eq [IntPtr]::Zero) {
        # Search by process
        $proc = Get-Process -Name "java","javaw" -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero }
        if ($proc) {
            $hwnd = $proc.MainWindowHandle
        }
    }
    if ($hwnd -eq [IntPtr]::Zero) {
        # Try any window with "Minecraft" in title via EnumWindows
        $procs = Get-Process | Where-Object { $_.MainWindowTitle -like "*Minecraft*" -and $_.MainWindowHandle -ne [IntPtr]::Zero }
        if ($procs) {
            $hwnd = $procs[0].MainWindowHandle
        }
    }
    return $hwnd
}

function Get-WindowRect($hwnd) {
    $rect = New-Object WinAPI+RECT
    [WinAPI]::GetWindowRect($hwnd, [ref]$rect) | Out-Null
    return $rect
}

switch ($Command) {
    "find" {
        $hwnd = Find-MinecraftWindow
        if ($hwnd -ne [IntPtr]::Zero) {
            $rect = Get-WindowRect $hwnd
            $w = $rect.Right - $rect.Left
            $h = $rect.Bottom - $rect.Top
            Write-Output "OK|$($hwnd.ToInt64())|$($rect.Left)|$($rect.Top)|$w|$h"
        } else {
            Write-Output "NOTFOUND"
        }
    }

    "is_running" {
        $procs = Get-Process -Name "java","javaw" -ErrorAction SilentlyContinue
        if ($procs) {
            Write-Output "RUNNING|$($procs[0].Id)"
        } else {
            Write-Output "STOPPED"
        }
    }

    "capture" {
        # Arg1 = output file path
        $outPath = if ($Arg1) { $Arg1 } else { "$env:TEMP\mc_capture.png" }
        $hwnd = Find-MinecraftWindow
        if ($hwnd -eq [IntPtr]::Zero) {
            Write-Output "NOTFOUND"
            exit 1
        }

        # Restore if minimized
        if ([WinAPI]::IsIconic($hwnd)) {
            [WinAPI]::ShowWindow($hwnd, 9) | Out-Null  # SW_RESTORE
            Start-Sleep -Milliseconds 500
        }

        $rect = Get-WindowRect $hwnd
        $w = $rect.Right - $rect.Left
        $h = $rect.Bottom - $rect.Top

        if ($w -le 0 -or $h -le 0) {
            Write-Output "INVALID_SIZE|$w|$h"
            exit 1
        }

        # Capture using BitBlt
        Add-Type -AssemblyName System.Drawing
        $bmp = New-Object System.Drawing.Bitmap($w, $h)
        $graphics = [System.Drawing.Graphics]::FromImage($bmp)
        $hdc = $graphics.GetHdc()

        # Use PrintWindow for better compatibility with games
        Add-Type @"
using System;
using System.Runtime.InteropServices;
public class PrintWin {
    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
}
"@
        $success = [PrintWin]::PrintWindow($hwnd, $hdc, 0)
        $graphics.ReleaseHdc($hdc)
        $graphics.Dispose()

        if (-not $success) {
            # Fallback: copy from screen
            $graphics2 = [System.Drawing.Graphics]::FromImage($bmp)
            $graphics2.CopyFromScreen($rect.Left, $rect.Top, 0, 0, (New-Object System.Drawing.Size($w, $h)))
            $graphics2.Dispose()
        }

        $bmp.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        Write-Output "OK|$outPath|$w|$h"
    }

    "save_focus" {
        $fg = [WinAPI]::GetForegroundWindow()
        Write-Output "OK|$($fg.ToInt64())"
    }

    "restore_focus" {
        # Arg1 = hwnd to restore
        $hwnd = [IntPtr]([long]$Arg1)
        if ($hwnd -ne [IntPtr]::Zero) {
            # Alt 键技巧绕过 SetForegroundWindow 限制
            [WinAPI]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
            [WinAPI]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
            [WinAPI]::SetForegroundWindow($hwnd) | Out-Null
            Write-Output "OK"
        } else {
            Write-Output "INVALID"
        }
    }

    "position_window" {
        # Arg1=x, Arg2=y, Arg3=w, Arg4=h
        $hwnd = Find-MinecraftWindow
        if ($hwnd -eq [IntPtr]::Zero) {
            Write-Output "NOTFOUND"
            exit 1
        }
        Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinPos {
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@
        [WinPos]::ShowWindow($hwnd, 9) | Out-Null  # SW_RESTORE
        Start-Sleep -Milliseconds 100
        $x = [int]$Arg1; $y = [int]$Arg2; $w = [int]$Arg3; $h = [int]$Arg4
        if ($w -le 0) { $w = 854 }
        if ($h -le 0) { $h = 480 }
        [WinPos]::MoveWindow($hwnd, $x, $y, $w, $h, $true) | Out-Null
        Write-Output "OK|$x|$y|$w|$h"
    }

    "focus" {
        $hwnd = Find-MinecraftWindow
        if ($hwnd -eq [IntPtr]::Zero) {
            Write-Output "NOTFOUND"
            exit 1
        }
        [WinAPI]::SetForegroundWindow($hwnd) | Out-Null
        Start-Sleep -Milliseconds 100
        $fg = [WinAPI]::GetForegroundWindow()
        if ($fg -eq $hwnd) {
            Write-Output "OK"
        } else {
            Write-Output "FAILED"
        }
    }

    "key" {
        # Arg1 = key name
        $vk = [WinAPI]::GetVkCode($Arg1)
        [WinAPI]::keybd_event($vk, 0, [WinAPI]::KEYEVENTF_KEYDOWN, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 30
        [WinAPI]::keybd_event($vk, 0, [WinAPI]::KEYEVENTF_KEYUP, [UIntPtr]::Zero)
        Write-Output "OK|$Arg1"
    }

    "keydown" {
        $vk = [WinAPI]::GetVkCode($Arg1)
        [WinAPI]::keybd_event($vk, 0, [WinAPI]::KEYEVENTF_KEYDOWN, [UIntPtr]::Zero)
        Write-Output "OK|$Arg1"
    }

    "keyup" {
        $vk = [WinAPI]::GetVkCode($Arg1)
        [WinAPI]::keybd_event($vk, 0, [WinAPI]::KEYEVENTF_KEYUP, [UIntPtr]::Zero)
        Write-Output "OK|$Arg1"
    }

    "mousemove" {
        # Arg1 = dx, Arg2 = dy (relative movement)
        $dx = [int]$Arg1
        $dy = [int]$Arg2
        [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_MOVE, $dx, $dy, 0, [UIntPtr]::Zero)
        Write-Output "OK|$dx|$dy"
    }

    "mouseclick" {
        # Arg1 = button (left/right/middle)
        switch ($Arg1.ToLower()) {
            "left" {
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
                Start-Sleep -Milliseconds 30
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
            }
            "right" {
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, [UIntPtr]::Zero)
                Start-Sleep -Milliseconds 30
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_RIGHTUP, 0, 0, 0, [UIntPtr]::Zero)
            }
            "middle" {
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, [UIntPtr]::Zero)
                Start-Sleep -Milliseconds 30
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_MIDDLEUP, 0, 0, 0, [UIntPtr]::Zero)
            }
        }
        Write-Output "OK|$Arg1"
    }

    "mousedown" {
        switch ($Arg1.ToLower()) {
            "left" { [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero) }
            "right" { [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, [UIntPtr]::Zero) }
            "middle" { [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, [UIntPtr]::Zero) }
        }
        Write-Output "OK|$Arg1"
    }

    "mouseup" {
        switch ($Arg1.ToLower()) {
            "left" { [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero) }
            "right" { [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_RIGHTUP, 0, 0, 0, [UIntPtr]::Zero) }
            "middle" { [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_MIDDLEUP, 0, 0, 0, [UIntPtr]::Zero) }
        }
        Write-Output "OK|$Arg1"
    }

    "scroll" {
        # Arg1 = amount (positive=up, negative=down), 120 = 1 notch
        $amount = [int]$Arg1
        $delta = $amount * 120
        [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_WHEEL, 0, 0, [uint32]$delta, [UIntPtr]::Zero)
        Write-Output "OK|$amount"
    }

    "holdmouse" {
        # Arg1 = button (left/right), Arg2 = duration ms
        $button = if ($Arg1) { $Arg1.ToLower() } else { "left" }
        $durationMs = if ($Arg2) { [int]$Arg2 } else { 1000 }
        switch ($button) {
            "left" {
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
                Start-Sleep -Milliseconds $durationMs
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
            }
            "right" {
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, [UIntPtr]::Zero)
                Start-Sleep -Milliseconds $durationMs
                [WinAPI]::mouse_event([WinAPI]::MOUSEEVENTF_RIGHTUP, 0, 0, 0, [UIntPtr]::Zero)
            }
        }
        Write-Output "OK|$button|${durationMs}ms"
    }

    "typetext" {
        # Arg1 = text to type (via clipboard paste, supports Chinese/Unicode)
        $text = $Arg1
        if (-not $text) {
            Write-Output "OK|empty"
            break
        }
        # Use clipboard to reliably paste any Unicode text
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.Clipboard]::SetText($text)
        Start-Sleep -Milliseconds 80
        # Ctrl+V to paste
        [WinAPI]::keybd_event(0x11, 0, [WinAPI]::KEYEVENTF_KEYDOWN, [UIntPtr]::Zero)  # Ctrl down
        Start-Sleep -Milliseconds 30
        [WinAPI]::keybd_event(0x56, 0, [WinAPI]::KEYEVENTF_KEYDOWN, [UIntPtr]::Zero)  # V down
        Start-Sleep -Milliseconds 30
        [WinAPI]::keybd_event(0x56, 0, [WinAPI]::KEYEVENTF_KEYUP, [UIntPtr]::Zero)    # V up
        Start-Sleep -Milliseconds 30
        [WinAPI]::keybd_event(0x11, 0, [WinAPI]::KEYEVENTF_KEYUP, [UIntPtr]::Zero)    # Ctrl up
        Start-Sleep -Milliseconds 50
        Write-Output "OK|$($text.Length)chars"
    }

    default {
        Write-Output "UNKNOWN_COMMAND|$Command"
        exit 1
    }
}
