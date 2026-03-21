@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "webui_server.py"
) else (
  py -3 "webui_server.py"
)

endlocal
