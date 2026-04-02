@echo off
REM Dev launcher: runs the bot from the repo's local .venv (not the runtime venv).
REM For production use, install via service/install.py and register the service.
cd /d "%~dp0.."
.venv\Scripts\python.exe -m vibeaway
pause
