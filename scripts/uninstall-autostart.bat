@echo off
rem Remove TokenPulse autostart and stop all related processes. ASCII only.
cd /d "%~dp0"
set "PYEXE="
for %%R in ("%USERPROFILE%\.workbuddy" "D:\.workbuddy" "C:\.workbuddy") do (
  if not defined PYEXE if exist "%%~R\binaries\python\versions" (
    for /d %%d in ("%%~R\binaries\python\versions\*") do (
      if not defined PYEXE if exist "%%d\python.exe" set "PYEXE=%%d\python.exe"
    )
  )
)
if not defined PYEXE for /f "delims=" %%i in ('where python.exe 2^>nul') do if not defined PYEXE set "PYEXE=%%i"
if not defined PYEXE (
  echo [FAIL] Python not found ^(neither bundled nor on PATH^).
  echo        Install Python 3.8+ or run WorkBuddy once, then retry.
  pause
  exit /b 1
)

echo Removing autostart entry and stopping all related processes...
echo.
"%PYEXE%" "%~dp0tpctl.py" remove
echo.
pause
