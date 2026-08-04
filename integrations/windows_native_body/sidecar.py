"""Authenticated Windows capture and SendInput body for Minecraft."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import signal
import sys
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit
from uuid import uuid4

import dxcam
from command_ledger import CommandLedger, DecisionKind
from PIL import Image
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

PROTOCOL = "elysium.minecraft.bridge/1"
BRIDGE_VERSION = "0.2.0"
CONFIG_PATH = Path(r"G:\Game\Minecraft\.minecraft\config\elysium_native_bridge.json")

USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
ULONG_PTR = wintypes.WPARAM
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

USER32.IsWindowVisible.argtypes = [wintypes.HWND]
USER32.IsWindowVisible.restype = wintypes.BOOL
USER32.IsWindow.argtypes = [wintypes.HWND]
USER32.IsWindow.restype = wintypes.BOOL
USER32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
USER32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
USER32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
USER32.GetWindowTextLengthW.restype = ctypes.c_int
USER32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER32.GetWindowTextW.restype = ctypes.c_int
USER32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
USER32.GetWindowThreadProcessId.restype = wintypes.DWORD
USER32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
USER32.GetClientRect.restype = wintypes.BOOL
USER32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
USER32.ClientToScreen.restype = wintypes.BOOL
USER32.GetForegroundWindow.restype = wintypes.HWND
USER32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
USER32.ShowWindow.restype = wintypes.BOOL
USER32.SetForegroundWindow.argtypes = [wintypes.HWND]
USER32.SetForegroundWindow.restype = wintypes.BOOL
USER32.SetFocus.argtypes = [wintypes.HWND]
USER32.SetFocus.restype = wintypes.HWND
USER32.BringWindowToTop.argtypes = [wintypes.HWND]
USER32.BringWindowToTop.restype = wintypes.BOOL
USER32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
USER32.AttachThreadInput.restype = wintypes.BOOL
USER32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
USER32.MapVirtualKeyW.restype = wintypes.UINT
KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL32.OpenProcess.restype = wintypes.HANDLE
KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL32.CloseHandle.restype = wintypes.BOOL
KERNEL32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL
KERNEL32.GetCurrentThreadId.restype = wintypes.DWORD
KERNEL32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL32.CreateMutexW.restype = wintypes.HANDLE
KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL32.CloseHandle.restype = wintypes.BOOL

ERROR_ALREADY_EXISTS = 183
NATIVE_BODY_MUTEX = r"Local\ElysiumMinecraftNativeBody"

if not USER32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
    error_code = ctypes.get_last_error()
    if error_code != 5:
        raise ctypes.WinError(error_code)


class MOUSEINPUT(ctypes.Structure):
    """Win32 mouse input payload."""

    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    """Win32 keyboard input payload."""

    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    """Win32 hardware input payload."""

    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    """Win32 tagged input union."""

    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    """Win32 SendInput item."""

    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


USER32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
USER32.SendInput.restype = wintypes.UINT


@dataclass(slots=True)
class NativeBodyConfig:
    """Persistent native-body capture, endpoint, and target configuration."""

    controller_uri: str = "ws://127.0.0.1:18766/elysium"
    authentication_token: str = ""
    window_title_contains: str = "Minecraft"
    process_executable_names: tuple[str, ...] = ("javaw.exe", "java.exe")
    capture_backend: str = "dxgi"
    capture_fps: float = 6.0
    jpeg_quality: int = 92
    reconnect_seconds: float = 1.0
    open_timeout_seconds: float = 5.0
    capture_directory: str = r"G:\Game\Minecraft\.minecraft\elysium_capture\biomimetic"
    wsl_capture_directory: str = (
        "/mnt/g/Game/Minecraft/.minecraft/elysium_capture/biomimetic"
    )

    def validate(self) -> None:
        """Validate explicit operational bounds without replacing values."""

        endpoint = urlsplit(self.controller_uri)
        if endpoint.scheme != "ws" or not endpoint.hostname or endpoint.port is None:
            raise ValueError("controller_uri must be an explicit ws://host:port URI")
        if not self.window_title_contains:
            raise ValueError("window_title_contains must not be empty")
        if not self.process_executable_names:
            raise ValueError("process_executable_names must not be empty")
        if self.capture_fps <= 0:
            raise ValueError("capture_fps must be positive")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")
        if self.capture_backend not in {"dxgi", "winrt"}:
            raise ValueError("capture_backend must be dxgi or winrt")
        if self.reconnect_seconds <= 0:
            raise ValueError("reconnect_seconds must be positive")
        if self.open_timeout_seconds <= 0:
            raise ValueError("open_timeout_seconds must be positive")

    @classmethod
    def load(cls, path: Path) -> NativeBodyConfig:
        """Load or create configuration with a random 256-bit token."""

        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if "process_executable_names" in raw:
                raw["process_executable_names"] = tuple(raw["process_executable_names"])
            config = cls(**raw)
        else:
            config = cls()
        config.validate()
        if not config.authentication_token:
            config.authentication_token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return config


@dataclass(frozen=True, slots=True)
class WindowTarget:
    """Exact Minecraft window resolved for one action or frame."""

    hwnd: int
    pid: int
    title: str
    process_path: str
    visible: bool
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        """Return client-area width."""

        return self.right - self.left

    @property
    def height(self) -> int:
        """Return client-area height."""

        return self.bottom - self.top


class WindowResolver:
    """Resolve exactly one visible Minecraft client window."""

    def __init__(self, config: NativeBodyConfig) -> None:
        """Bind exact title and executable constraints."""

        self._config = config

    def resolve(self) -> WindowTarget:
        """Return one exact target and reject absent or ambiguous matches."""

        matches: list[WindowTarget] = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        @callback_type
        def callback(hwnd: int, _: int) -> bool:
            if not USER32.IsWindow(hwnd):
                return True
            title_length = USER32.GetWindowTextLengthW(hwnd)
            if title_length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(title_length + 1)
            USER32.GetWindowTextW(hwnd, buffer, len(buffer))
            title = buffer.value
            if self._config.window_title_contains.casefold() not in title.casefold():
                return True
            pid = wintypes.DWORD()
            USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process_path = self._process_path(pid.value)
            executable = Path(process_path).name.casefold()
            allowed = {
                item.casefold() for item in self._config.process_executable_names
            }
            if executable not in allowed:
                return True
            rect = wintypes.RECT()
            if not USER32.GetClientRect(hwnd, ctypes.byref(rect)):
                return True
            origin = wintypes.POINT(rect.left, rect.top)
            corner = wintypes.POINT(rect.right, rect.bottom)
            if not USER32.ClientToScreen(hwnd, ctypes.byref(origin)):
                return True
            if not USER32.ClientToScreen(hwnd, ctypes.byref(corner)):
                return True
            if corner.x <= origin.x or corner.y <= origin.y:
                USER32.ShowWindow(hwnd, SW_RESTORE)
                if not USER32.GetClientRect(hwnd, ctypes.byref(rect)):
                    return True
                origin = wintypes.POINT(rect.left, rect.top)
                corner = wintypes.POINT(rect.right, rect.bottom)
                if not USER32.ClientToScreen(hwnd, ctypes.byref(origin)):
                    return True
                if not USER32.ClientToScreen(hwnd, ctypes.byref(corner)):
                    return True
                if corner.x <= origin.x or corner.y <= origin.y:
                    return True
            matches.append(
                WindowTarget(
                    hwnd=int(hwnd),
                    pid=int(pid.value),
                    title=title,
                    process_path=process_path,
                    visible=bool(USER32.IsWindowVisible(hwnd)),
                    left=origin.x,
                    top=origin.y,
                    right=corner.x,
                    bottom=corner.y,
                )
            )
            return True

        if not USER32.EnumWindows(callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if not matches:
            raise RuntimeError("no exact Minecraft window matched configuration")
        if len(matches) != 1:
            identities = [(item.pid, item.title) for item in matches]
            raise RuntimeError(f"multiple Minecraft windows matched: {identities}")
        return matches[0]

    @staticmethod
    def _process_path(pid: int) -> str:
        """Query the executable path for one window process."""

        handle = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not KERNEL32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return buffer.value
        finally:
            KERNEL32.CloseHandle(handle)


class NativeMotor:
    """Maintain physical keyboard and mouse state through batched SendInput."""

    VK: ClassVar[dict[str, int]] = {
        "forward": 0x57,
        "back": 0x53,
        "left": 0x41,
        "right": 0x44,
        "jump": 0x20,
        "sneak": 0x10,
        "sprint": 0x11,
        "drop": 0x51,
        "inventory": 0x45,
        "chat": 0x54,
        "escape": 0x1B,
        "toggle_hud": 0x70,
        "toggle_debug": 0x72,
        "toggle_camera": 0x74,
        "toggle_fullscreen": 0x7A,
        "player_list": 0x09,
    }
    MOUSE: ClassVar[dict[str, tuple[int, int]]] = {
        "attack": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "use": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }

    def __init__(self, resolver: WindowResolver) -> None:
        """Create a motor with no held controls."""

        self._resolver = resolver
        self._held_keys: set[str] = set()
        self._held_mouse: set[str] = set()

    def execute(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Execute one exact low-level operation against the foreground game."""

        if operation == "control.release_all":
            return self.release_all("command")
        if operation != "native.input_batch":
            raise ValueError(f"unsupported operation: {operation}")
        target = self._resolver.resolve()
        self._focus(target.hwnd)
        holds_raw = parameters.get("holds", {})
        if not isinstance(holds_raw, dict):
            raise TypeError("holds must be an object")
        desired = {str(name) for name, down in holds_raw.items() if bool(down)}
        unknown = desired - set(self.VK) - set(self.MOUSE)
        if unknown:
            raise ValueError(f"unknown held controls: {sorted(unknown)}")

        inputs: list[INPUT] = []
        for name in sorted(self._held_keys - desired):
            inputs.append(self._key(self.VK[name], down=False))
        for name in sorted(self._held_mouse - desired):
            inputs.append(self._mouse_button(self.MOUSE[name][1]))
        desired_keys = desired & set(self.VK)
        desired_mouse = desired & set(self.MOUSE)
        for name in sorted(desired_keys - self._held_keys):
            inputs.append(self._key(self.VK[name], down=True))
        for name in sorted(desired_mouse - self._held_mouse):
            inputs.append(self._mouse_button(self.MOUSE[name][0]))

        mouse_delta = parameters.get("mouse_delta")
        if mouse_delta is not None:
            if not isinstance(mouse_delta, dict):
                raise ValueError("mouse_delta must be an object")
            inputs.append(
                self._mouse_move(
                    int(mouse_delta.get("x", 0)),
                    int(mouse_delta.get("y", 0)),
                )
            )

        pulses = parameters.get("pulses", [])
        if not isinstance(pulses, list):
            raise TypeError("pulses must be an array")
        for raw_name in pulses:
            name = str(raw_name)
            if name in self.VK:
                inputs.extend(
                    (
                        self._key(self.VK[name], down=True),
                        self._key(self.VK[name], down=False),
                    )
                )
            elif name in self.MOUSE:
                down_flag, up_flag = self.MOUSE[name]
                inputs.extend(
                    (self._mouse_button(down_flag), self._mouse_button(up_flag))
                )
            else:
                raise ValueError(f"unknown pulse control: {name}")

        if "hotbar_slot" in parameters:
            slot = int(parameters["hotbar_slot"])
            if not 0 <= slot <= 8:
                raise ValueError("hotbar_slot must be between 0 and 8")
            virtual_key = 0x31 + slot
            inputs.extend(
                (self._key(virtual_key, down=True), self._key(virtual_key, down=False))
            )

        sent = self._send(inputs)
        self._held_keys = desired_keys
        self._held_mouse = desired_mouse
        if "chat" in parameters:
            self._send_chat(str(parameters["chat"]))
        return {
            "send_input_events": sent,
            "held_keys": sorted(self._held_keys),
            "held_mouse_buttons": sorted(self._held_mouse),
            "window_handle": target.hwnd,
            "window_pid": target.pid,
            "window_title": target.title,
        }

    def release_all(self, reason: str) -> dict[str, Any]:
        """Release every held physical key and mouse button in one batch."""

        inputs = [
            self._key(self.VK[name], down=False) for name in sorted(self._held_keys)
        ]
        inputs.extend(
            self._mouse_button(self.MOUSE[name][1]) for name in sorted(self._held_mouse)
        )
        sent = self._send(inputs)
        self._held_keys.clear()
        self._held_mouse.clear()
        return {"controls_released": True, "send_input_events": sent, "reason": reason}

    @property
    def held(self) -> dict[str, list[str]]:
        """Return exact current physical control state."""

        return {
            "keys": sorted(self._held_keys),
            "mouse_buttons": sorted(self._held_mouse),
        }

    @staticmethod
    def _focus(hwnd: int) -> None:
        """Acquire and verify a foreground focus lease for physical input."""

        USER32.ShowWindow(hwnd, SW_RESTORE)
        foreground = USER32.GetForegroundWindow()
        current_thread = KERNEL32.GetCurrentThreadId()
        foreground_thread = USER32.GetWindowThreadProcessId(foreground, None)
        target_thread = USER32.GetWindowThreadProcessId(hwnd, None)
        attached_foreground = False
        attached_target = False
        try:
            if foreground_thread and foreground_thread != current_thread:
                attached_foreground = bool(
                    USER32.AttachThreadInput(current_thread, foreground_thread, True)
                )
            if target_thread and target_thread != current_thread:
                attached_target = bool(
                    USER32.AttachThreadInput(current_thread, target_thread, True)
                )
            USER32.BringWindowToTop(hwnd)
            USER32.SetForegroundWindow(hwnd)
            USER32.SetFocus(hwnd)
        finally:
            if attached_target:
                USER32.AttachThreadInput(current_thread, target_thread, False)
            if attached_foreground:
                USER32.AttachThreadInput(current_thread, foreground_thread, False)
        if USER32.GetForegroundWindow() != hwnd:
            raise RuntimeError("unable to acquire Minecraft foreground focus")

    @staticmethod
    def _key(virtual_key: int, *, down: bool) -> INPUT:
        """Build one scan-code keyboard event."""

        scan = USER32.MapVirtualKeyW(virtual_key, 0)
        flags = KEYEVENTF_SCANCODE | (0 if down else KEYEVENTF_KEYUP)
        return INPUT(
            type=INPUT_KEYBOARD,
            ki=KEYBDINPUT(0, scan, flags, 0, 0),
        )

    @staticmethod
    def _unicode(character: str, *, down: bool) -> INPUT:
        """Build one UTF-16 text input event."""

        flags = KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP)
        return INPUT(
            type=INPUT_KEYBOARD,
            ki=KEYBDINPUT(0, ord(character), flags, 0, 0),
        )

    @staticmethod
    def _mouse_button(flag: int) -> INPUT:
        """Build one mouse button transition."""

        return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, flag, 0, 0))

    @staticmethod
    def _mouse_move(dx: int, dy: int) -> INPUT:
        """Build one relative physical mouse movement."""

        return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0))

    @staticmethod
    def _send(inputs: list[INPUT]) -> int:
        """Send one atomic input array and require complete acceptance."""

        if not inputs:
            return 0
        array_type = INPUT * len(inputs)
        array = array_type(*inputs)
        sent = USER32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(sent)

    def _send_chat(self, text: str) -> None:
        """Open chat, send exact Unicode text, and submit it."""

        self._send(
            (
                self._key(self.VK["chat"], down=True),
                self._key(self.VK["chat"], down=False),
            )
        )
        time.sleep(0.08)
        inputs: list[INPUT] = []
        for character in text:
            inputs.extend(
                (
                    self._unicode(character, down=True),
                    self._unicode(character, down=False),
                )
            )
        self._send(inputs)
        self._send((self._key(0x0D, down=True), self._key(0x0D, down=False)))


class FrameSensor:
    """Capture immutable first-person client-area frames through DXGI or WGC."""

    def __init__(
        self, config: NativeBodyConfig, resolver: WindowResolver, instance_id: str
    ) -> None:
        """Initialize one hardware capture backend and session directory."""

        self._config = config
        self._resolver = resolver
        self._instance_id = instance_id
        self._camera = dxcam.create(
            backend=config.capture_backend,
            output_color="RGB",
            processor_backend="numpy",
        )
        self._camera.start(
            target_fps=max(1, round(config.capture_fps)),
            video_mode=True,
        )
        self._directory = Path(config.capture_directory) / instance_id
        self._directory.mkdir(parents=True, exist_ok=True)

    def capture(self, sequence: int, held: dict[str, list[str]]) -> dict[str, Any]:
        """Capture, durably save, and describe one client-area frame."""

        target = self._resolver.resolve()
        desktop = self._camera.get_latest_frame()
        if desktop is None:
            raise RuntimeError("capture backend has not produced its first frame")
        desktop_height, desktop_width = desktop.shape[:2]
        if (
            target.left < 0
            or target.top < 0
            or target.right > desktop_width
            or target.bottom > desktop_height
        ):
            raise RuntimeError(
                "Minecraft client area is outside the configured DXGI output: "
                f"client=({target.left},{target.top},{target.right},{target.bottom}), "
                f"output=({desktop_width},{desktop_height})"
            )
        frame = desktop[target.top : target.bottom, target.left : target.right].copy()
        if frame is None:
            raise RuntimeError("capture backend returned no new frame")
        filename = f"frame_{sequence:012d}.jpg"
        path = self._directory / filename
        temporary = path.with_suffix(".jpg.tmp")
        image = Image.fromarray(frame)
        image.save(temporary, format="JPEG", quality=self._config.jpeg_quality)
        os.replace(temporary, path)
        wsl_path = (
            Path(self._config.wsl_capture_directory) / self._instance_id / filename
        ).as_posix()
        return {
            "frame_path": wsl_path,
            "facts": {
                "window": {
                    "handle": target.hwnd,
                    "pid": target.pid,
                    "title": target.title,
                    "process_path": target.process_path,
                    "visible": target.visible,
                    "client_left": target.left,
                    "client_top": target.top,
                    "client_width": target.width,
                    "client_height": target.height,
                    "foreground": USER32.GetForegroundWindow() == target.hwnd,
                },
                "capture": {
                    "backend": self._config.capture_backend,
                    "width": int(frame.shape[1]),
                    "height": int(frame.shape[0]),
                    "jpeg_quality": self._config.jpeg_quality,
                },
                "controls": held,
            },
        }

    def close(self) -> None:
        """Stop the hardware capture thread."""

        self._camera.stop()


class NativeBodySession:
    """Authenticated outbound control session for the native body."""

    CAPABILITIES = ("control.release_all", "native.input_batch")

    def __init__(self, config: NativeBodyConfig) -> None:
        """Create server resources for one sidecar process identity."""

        self._config = config
        self._instance_id = f"windows_native_{uuid4().hex}"
        self._resolver = WindowResolver(config)
        self._motor = NativeMotor(self._resolver)
        self._sensor = FrameSensor(config, self._resolver, self._instance_id)
        self._command_ledger = CommandLedger()

    async def run_connection(self, socket: ClientConnection) -> None:
        """Authenticate and serve one outbound controller connection."""

        nonce = secrets.token_urlsafe(32)
        await socket.send(
            self._encode(
                {
                    "type": "hello",
                    "protocol": PROTOCOL,
                    "body_type": "windows-native",
                    "bridge_version": BRIDGE_VERSION,
                    "nonce": nonce,
                    "instance_id": self._instance_id,
                    "capabilities": list(self.CAPABILITIES),
                }
            )
        )
        try:
            async with asyncio.timeout(self._config.open_timeout_seconds):
                authentication = self._decode(await socket.recv())
            expected = hmac.new(
                self._config.authentication_token.encode(),
                nonce.encode(),
                hashlib.sha256,
            ).hexdigest()
            accepted = (
                authentication.get("type") == "authenticate"
                and authentication.get("protocol") == PROTOCOL
                and hmac.compare_digest(
                    str(authentication.get("digest") or ""), expected
                )
            )
            await socket.send(
                self._encode({"type": "authentication", "accepted": accepted})
            )
            if not accepted:
                await socket.close(code=1008, reason="authentication rejected")
                return
            stop = asyncio.Event()
            observations = asyncio.create_task(
                self._observation_loop(socket, stop),
                name="native_body_observations",
            )
            try:
                async for raw in socket:
                    await self._handle_message(socket, self._decode(raw))
            finally:
                stop.set()
                observations.cancel()
                try:
                    await observations
                except asyncio.CancelledError:
                    pass
        finally:
            await asyncio.to_thread(self._motor.release_all, "controller disconnected")

    async def shutdown(self) -> None:
        """Release every physical control during process shutdown."""

        await asyncio.to_thread(self._motor.release_all, "sidecar shutting down")
        await asyncio.to_thread(self._sensor.close)

    async def _observation_loop(
        self,
        socket: ClientConnection,
        stop: asyncio.Event,
    ) -> None:
        """Capture and send contiguous immutable first-person observations."""

        sequence = 0
        interval = 1.0 / self._config.capture_fps
        while not stop.is_set():
            started = time.monotonic()
            candidate = sequence + 1
            try:
                captured = await asyncio.to_thread(
                    self._sensor.capture,
                    candidate,
                    self._motor.held,
                )
            except Exception as exception:  # noqa: BLE001 - report sensor evidence
                captured = {
                    "frame_path": None,
                    "facts": {
                        "capture_error": {
                            "type": exception.__class__.__name__,
                            "message": str(exception),
                        }
                    },
                }
            sequence = candidate
            observation = {
                "observation_id": f"observation_{uuid4().hex}",
                "instance_id": self._instance_id,
                "sequence": sequence,
                "observed_at": datetime.now(UTC).isoformat(),
                "source": "windows-native-sidecar",
                "facts": captured["facts"],
                "frame_path": captured["frame_path"],
            }
            await socket.send(
                self._encode({"type": "observation", "observation": observation})
            )
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _handle_message(
        self,
        socket: ClientConnection,
        message: dict[str, Any],
    ) -> None:
        """Handle one command, interruption, or release request."""

        message_type = message.get("type")
        if message_type == "command":
            await self._command(socket, message)
        elif message_type == "interrupt":
            await asyncio.to_thread(
                self._motor.release_all,
                str(message.get("reason") or "interrupt"),
            )
        elif message_type == "release_all":
            await asyncio.to_thread(
                self._motor.release_all,
                str(message.get("reason") or "release_all"),
            )
        else:
            raise ValueError(f"unsupported message type: {message_type!r}")

    async def _command(
        self,
        socket: ClientConnection,
        message: dict[str, Any],
    ) -> None:
        """Acknowledge quickly, execute off-loop, and return terminal facts."""

        command = message.get("command")
        if not isinstance(command, dict):
            raise TypeError("command must be an object")
        command_id = self._required(command, "command_id")
        intent_id = self._required(command, "intent_id")
        operation = self._required(command, "operation")
        parameters = command.get("parameters")
        if not isinstance(parameters, dict):
            raise TypeError("parameters must be an object")
        decision = self._command_ledger.begin(command_id, command)
        if decision.kind is DecisionKind.CONFLICT:
            await self._send_receipt(
                socket,
                self._receipt(
                    command_id,
                    intent_id,
                    False,
                    True,
                    {},
                    "command_id was already used for another payload",
                ),
            )
            return
        if decision.kind is DecisionKind.PENDING_REPLAY:
            await self._send_receipt(
                socket,
                self._receipt(command_id, intent_id, True, False, {}, None),
            )
            completion = decision.pending_completion
            if completion is None:
                raise RuntimeError("pending replay has no completion handle")
            terminal = await asyncio.shield(asyncio.wrap_future(completion))
            await self._send_receipt(socket, terminal)
            return
        if decision.kind is DecisionKind.TERMINAL_REPLAY:
            if decision.terminal_receipt is None:
                raise RuntimeError("terminal replay has no receipt")
            await self._send_receipt(socket, decision.terminal_receipt)
            return
        if operation not in self.CAPABILITIES:
            terminal = self._receipt(
                command_id,
                intent_id,
                False,
                True,
                {},
                f"unsupported operation: {operation}",
            )
            self._command_ledger.complete(command_id, terminal)
            await self._send_receipt(socket, terminal)
            return
        await self._send_receipt(
            socket,
            self._receipt(command_id, intent_id, True, False, {}, None),
        )
        try:
            facts = await asyncio.to_thread(self._motor.execute, operation, parameters)
            terminal = self._receipt(command_id, intent_id, True, True, facts, None)
        except Exception as exception:  # noqa: BLE001 - return exact motor failure
            terminal = self._receipt(
                command_id,
                intent_id,
                True,
                True,
                {},
                str(exception),
            )
        self._command_ledger.complete(command_id, terminal)
        await self._send_receipt(socket, terminal)

    def _receipt(
        self,
        command_id: str,
        intent_id: str,
        accepted: bool,
        completed: bool,
        facts: dict[str, Any],
        error: str | None,
    ) -> dict[str, Any]:
        """Construct one correlated factual receipt envelope."""

        receipt: dict[str, Any] = {
            "receipt_id": f"receipt_{uuid4().hex}",
            "command_id": command_id,
            "intent_id": intent_id,
            "accepted": accepted,
            "completed": completed,
            "interrupted": False,
            "recorded_at": datetime.now(UTC).isoformat(),
            "facts": facts,
        }
        if error:
            receipt["error"] = error
        return {"type": "receipt", "receipt": receipt}

    async def _send_receipt(
        self,
        socket: ClientConnection,
        receipt: dict[str, Any],
    ) -> None:
        """Send an acknowledgement or a ledger-backed terminal receipt."""

        await socket.send(self._encode(receipt))

    @staticmethod
    def _required(payload: dict[str, Any], name: str) -> str:
        """Read one non-empty protocol string."""

        value = str(payload.get(name) or "")
        if not value:
            raise ValueError(f"missing non-empty field: {name}")
        return value

    @staticmethod
    def _encode(payload: dict[str, Any]) -> str:
        """Encode one compact JSON frame."""

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode(raw: str | bytes) -> dict[str, Any]:
        """Decode one JSON object and reject other root values."""

        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("message must be a JSON object")
        return value


def _acquire_process_lease() -> int:
    """Acquire one Windows-session-wide native body lease."""

    ctypes.set_last_error(0)
    handle = KERNEL32.CreateMutexW(None, True, NATIVE_BODY_MUTEX)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        KERNEL32.CloseHandle(handle)
        raise RuntimeError("another Elysium Minecraft native body is already running")
    return int(handle)


async def main() -> None:
    """Reconnect outward to the WSL controller until an operating-system stop signal."""

    if sys.platform != "win32":
        raise RuntimeError("the native body must run on Windows")
    process_lease = _acquire_process_lease()
    try:
        config = NativeBodyConfig.load(CONFIG_PATH)
        session = NativeBodySession(config)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_name, stop.set)
            except NotImplementedError:
                pass
        try:
            while not stop.is_set():
                try:
                    async with connect(
                        config.controller_uri,
                        max_size=16 * 1024 * 1024,
                        open_timeout=config.open_timeout_seconds,
                        ping_interval=10,
                        ping_timeout=10,
                    ) as socket:
                        print(
                            f"Elysium native body connected to {config.controller_uri}; "
                            f"token file: {CONFIG_PATH}",
                            flush=True,
                        )
                        await session.run_connection(socket)
                except (OSError, TimeoutError, ConnectionClosed):
                    pass
                if not stop.is_set():
                    try:
                        await asyncio.wait_for(
                            stop.wait(),
                            timeout=config.reconnect_seconds,
                        )
                    except TimeoutError:
                        pass
        finally:
            await session.shutdown()
    finally:
        KERNEL32.CloseHandle(process_lease)


if __name__ == "__main__":
    asyncio.run(main())
