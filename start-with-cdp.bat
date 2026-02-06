@echo off
echo Starting FlareSolverr with CDP enabled (headless=false)...
echo.
echo CDP ports will be dynamically allocated per session
echo API will be available at: http://localhost:8191
echo.

cd /d "%~dp0src"

set HEADLESS=false
set LOG_LEVEL=info

python flaresolverr.py

pause


