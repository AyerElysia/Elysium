"""Explicitly isolated native-client identity and Windows process lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path, PureWindowsPath
from uuid import UUID


def offline_uuid(username: str) -> str:
    """Return Minecraft's exact offline account UUID, not the human account UUID."""

    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", username):
        raise ValueError("invalid native Minecraft account name")
    return str(UUID(bytes=hashlib.md5(
        f"OfflinePlayer:{username}".encode(), usedforsecurity=False
    ).digest(), version=3))


def mount_path(windows_path: str) -> Path:
    """Map only an absolute drive path to its WSL mount."""

    path = PureWindowsPath(windows_path)
    if not path.is_absolute() or len(path.drive) != 2 or not path.drive[0].isalpha():
        raise ValueError("native client paths must be absolute Windows drive paths")
    return Path("/mnt", path.drive[0].lower(), *path.parts[1:])


def _argument(line: str, name: str) -> str:
    matches = list(re.finditer(
        rf"(?<!\S)--{re.escape(name)}(?:=|\s+)(\"[^\"]*\"|'[^']*'|[^\s]+)", line
    ))
    if len(matches) != 1:
        raise ValueError(f"native launch script must contain exactly one --{name}")
    return matches[0].group(1).strip("\"'")


def _replace_argument(line: str, name: str, value: str) -> str:
    _argument(line, name)
    return re.sub(
        rf"(?<!\S)--{re.escape(name)}(?:=|\s+)(\"[^\"]*\"|'[^']*'|[^\s]+)",
        lambda _: f'--{name} "{value}"', line, count=1,
    )


def render_launch_script(
    template: str, *, game_directory: str, username: str, address: str,
    width: int = 854, height: int = 480,
) -> str:
    """Retain installed Java/libraries/assets but replace every account/session field."""

    target = PureWindowsPath(game_directory)
    mount_path(game_directory)
    if any(character in game_directory for character in '"&|<>^%!'):
        raise ValueError("native game directory contains shell metacharacters")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+:\d{1,5}", address):
        raise ValueError("native multiplayer address is invalid")
    candidates = [line.strip() for line in template.splitlines() if "--gameDir" in line]
    if len(candidates) != 1:
        raise ValueError("launch template must have exactly one Java launch line")
    line = candidates[0]
    source = PureWindowsPath(_argument(line, "gameDir"))
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("native client must be isolated from the human game directory")
    if username == _argument(line, "username"):
        raise ValueError("native client must not reuse the human account name")
    if "-javaagent:" in line.casefold() or any(c in line for c in "&|<>^%!"):
        raise ValueError("launch template needs manual review of shell or JVM agent arguments")
    if not re.match(r'^"[^"]*java(?:w)?\.exe"\s', line, flags=re.IGNORECASE):
        raise ValueError("launch template must directly invoke the installed Java executable")
    for name, value in (
        ("gameDir", str(target)), ("username", username),
        ("uuid", offline_uuid(username)), ("accessToken", "0"),
        ("clientId", ""), ("xuid", ""), ("userType", "legacy"),
        ("width", str(width)), ("height", str(height)),
    ):
        line = _replace_argument(line, name, value)
    if "--quickPlayMultiplayer" in line:
        line = _replace_argument(line, "quickPlayMultiplayer", address)
    else:
        _argument(line, "quickPlaySingleplayer")
        line = re.sub(
            r'(?<!\S)--quickPlaySingleplayer(?:=|\s+)("[^"]*"|\'[^\']*\'|[^\s]+)',
            lambda _: f'--quickPlayMultiplayer "{address}"', line, count=1,
        )
    line = re.sub(r"(?<!\S)-Xmx\S+", "-Xmx4G", line)
    line = re.sub(r"(?<!\S)-Xms\S+", "-Xms1G", line)
    return f'@echo off\r\nchcp 65001>nul\r\ncd /D "{target}"\r\n{line}\r\nexit /b %errorlevel%\r\n'


def validate_launch_script(
    content: str, *, game_directory: str, username: str, address: str,
) -> None:
    """Fail closed on a stale or human-bound prepared launch script."""

    lines = [line.strip() for line in content.splitlines() if "--gameDir" in line]
    if len(lines) != 1:
        raise ValueError("isolated launch script has no unique game command")
    line = lines[0]
    for name, expected in (
        ("username", username), ("uuid", offline_uuid(username)), ("accessToken", "0"),
        ("clientId", ""), ("xuid", ""), ("userType", "legacy"),
        ("quickPlayMultiplayer", address),
    ):
        if _argument(line, name) != expected:
            raise ValueError(f"isolated launch script has a different --{name}")
    if PureWindowsPath(_argument(line, "gameDir")) != PureWindowsPath(game_directory):
        raise ValueError("isolated launch script uses another game directory")
    if "--quickPlaySingleplayer" in line:
        raise ValueError("isolated client must not load the human singleplayer world")


async def _powershell(script: str) -> str:
    """Run a scoped process-management command without manipulating any windows."""

    process = await asyncio.create_subprocess_exec(
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference = 'Stop'\n" + script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(20):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        # Never expose process command lines or account material from diagnostics.
        raise RuntimeError(f"isolated client process check failed (exit {process.returncode})")
    return stdout.decode("utf-8-sig").strip()


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def find_process(game_directory: str, username: str) -> int | None:
    """Select Java only by its exact directory AND independently verified account."""

    script = r"""
$mcDirectory = __DIRECTORY__
$mcUsername = __USERNAME__
$mcUuid = __UUID__
$mcMatches = @()
foreach ($mcProcess in (Get-CimInstance Win32_Process -Filter "Name='java.exe' OR Name='javaw.exe'")) {
    $mcArgs = @{}
    foreach ($mcArg in [regex]::Matches($mcProcess.CommandLine, '(?<!\S)--(gameDir|username|uuid)(?:=|\s+)("[^"]*"|[^\s]+)')) {
        if ($mcArgs.ContainsKey($mcArg.Groups[1].Value)) { throw 'duplicate game identity argument' }
        $mcArgs[$mcArg.Groups[1].Value] = $mcArg.Groups[2].Value.Trim('"')
    }
    if ($mcArgs['gameDir'] -and $mcArgs['gameDir'].TrimEnd('\') -ieq $mcDirectory.TrimEnd('\')) {
        if ($mcArgs['username'] -cne $mcUsername -or $mcArgs['uuid'] -ine $mcUuid) {
            throw 'isolated directory is occupied by a different account'
        }
        $mcMatches += [int]$mcProcess.ProcessId
    }
}
ConvertTo-Json -InputObject @($mcMatches) -Compress
"""
    script = script.replace("__DIRECTORY__", _ps_literal(game_directory))
    script = script.replace("__USERNAME__", _ps_literal(username))
    script = script.replace("__UUID__", _ps_literal(offline_uuid(username)))
    result = json.loads(await _powershell(script))
    if not isinstance(result, list) or len(result) > 1:
        raise RuntimeError("isolated native profile has multiple active game processes")
    return int(result[0]) if result else None


async def launch_process(launch_script: str) -> int:
    """Launch the prepared Java command directly, avoiding cmd.exe's shorter line limit."""

    path = PureWindowsPath(launch_script)
    mount_path(launch_script)
    output = await _powershell(
        "$mcScript = " + _ps_literal(str(path)) + "\n"
        "$mcWorking = " + _ps_literal(str(path.parent)) + "\n"
        r"""
$mcCommands = @(Get-Content -LiteralPath $mcScript -Encoding UTF8 | Where-Object { $_ -match '--gameDir' })
if ($mcCommands.Count -ne 1) { throw 'prepared native script has no unique game command' }
$mcMatch = [regex]::Match($mcCommands[0], '^"([^"]*java(?:w)?\.exe)"\s+(.+)$')
if (-not $mcMatch.Success) { throw 'prepared script does not directly invoke Java' }
$mcProcess = Start-Process -FilePath $mcMatch.Groups[1].Value -ArgumentList $mcMatch.Groups[2].Value -WorkingDirectory $mcWorking -WindowStyle Hidden -PassThru
ConvertTo-Json -InputObject ([int]$mcProcess.Id) -Compress
"""
    )
    pid = json.loads(output)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("native Java launch did not return its owned process ID")
    return pid
