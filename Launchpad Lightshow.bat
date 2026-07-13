@echo off
title Launchpad Lightshow
cd /d "%~dp0"
".venv\Scripts\python.exe" -u lightshow.py --quiet %*
echo.
echo === Lightshow stopped. Press any key to close this window. ===
pause >nul
