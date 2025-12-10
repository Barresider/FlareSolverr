@echo off
echo Starting FlareSolverr with CDP enabled (headless=false)...
echo.
echo CDP will be available at: http://localhost:9222
echo API will be available at: http://localhost:8191
echo.

cd /d "%~dp0src"

set HEADLESS=false
set CDP_PORT=9222
set LOG_LEVEL=info

python flaresolverr.py

pause


