@echo off
rem Stop TokenPulse HUD and its guard process. ASCII only.
cd /d "%~dp0"
powershell -NoProfile -WindowStyle Hidden -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and ($_.CommandLine -like '*hud.py*' -or $_.CommandLine -like '*guard.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo TokenPulse stopped.
timeout /t 2 >nul
