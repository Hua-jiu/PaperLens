@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PAPER_LAUNCH_ROOT=%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PAPER_LAUNCH_ROOT%app\launch.ps1"
set "PAPER_LAUNCH_EXIT=%ERRORLEVEL%"
if "%PAPER_LAUNCH_EXIT%"=="0" exit /b 0
echo.
echo PaperLens could not start. See the message above.
if not "%PAPERLENS_DRYRUN%"=="1" pause
exit /b %PAPER_LAUNCH_EXIT%
