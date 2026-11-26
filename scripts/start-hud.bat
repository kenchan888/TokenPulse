@echo off
rem TokenPulse launcher. ASCII only: cmd.exe mis-parses multi-byte batch files.
cd /d "%~dp0"

rem ---- locate Python: prefer the one bundled with WorkBuddy ----
set "PYW="
for %%R in ("%USERPROFILE%\.workbuddy" "D:\.workbuddy" "C:\.workbuddy") do (
  if not defined PYW if exist "%%~R\binaries\python\versions" (
    for /d %%d in ("%%~R\binaries\python\versions\*") do (
      if not defined PYW if exist "%%d\pythonw.exe" set "PYW=%%d\pythonw.exe"
    )
  )
)
if not defined PYW for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('where python.exe 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
  echo [FAIL] Python not found ^(neither bundled nor on PATH^).
  echo        Install Python 3.8+ or run WorkBuddy once, then retry.
  pause
  exit /b 1
)
set "PYEXE=%PYW:pythonw.exe=python.exe%"
if not exist "%PYEXE%" set "PYEXE=%PYW%"

rem ---- geometry (DIP; leave empty to use defaults) ----
rem set "TP_LEFT=300"
rem set "TP_RIGHT=6"
rem set "TP_BOTTOM=7.3"

rem ---- appearance ----
rem set "TP_INK=0.70"
rem set "TP_GAMMA=0.90"
rem set "TP_OPACITY=255"

rem ---- scope ----
rem set "TP_ONLY_CHAT=1"

rem ---- pin one session ----
rem set "TP_SESSION="

rem ---- stop leftovers, then start ----
powershell -NoProfile -WindowStyle Hidden -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and ($_.CommandLine -like '*hud.py*' -or $_.CommandLine -like '*guard.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 3 >nul
if exist "_guard.log" del /q "_guard.log" >nul 2>&1
if exist "_hud.log" del /q "_hud.log" >nul 2>&1

start "" "%PYW%" "%~dp0guard.py"
timeout /t 4 >nul

echo.
echo ======== self check ========
"%PYEXE%" "%~dp0check_hud.py"
echo.
if exist "_hud.log" (
  echo -------- runtime log --------
  type "_hud.log"
) else (
  echo [ok] no error recorded.
)
if exist "_guard.log" (
  echo -------- guard log --------
  type "_guard.log"
) else (
  echo [warn] no guard log: guard.py may not have started.
)
echo ============================
echo.
echo If anything above says [FAIL], screenshot this window.
echo Closing in 12 seconds.
timeout /t 12 >nul
