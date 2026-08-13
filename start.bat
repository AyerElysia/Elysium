@echo off
setlocal
rem Elysium 的 Windows 兼容入口；生命周期契约由 deploy.ps1 统一执行。
cd /d "%~dp0" || exit /b 1
powershell.exe -NoLogo -NoProfile -File "%~dp0deploy.ps1" run %*
set "_ELYSIUM_START_EXIT=%ERRORLEVEL%"
endlocal & exit /b %_ELYSIUM_START_EXIT%
