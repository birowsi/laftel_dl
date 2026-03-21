@echo off
setlocal
cd /d "%~dp0"
set "LAFTEL_VERBOSE_BINARIES=1"

if exist ".venv\Scripts\python.exe" goto run_venv

where py >nul 2>&1
if errorlevel 1 goto no_python
goto run_py

:run_venv
".venv\Scripts\python.exe" "main.py" %*
goto cleanup

:run_py
py -3 "main.py" %*
goto cleanup

:no_python
echo [ERROR] Python executable not found.
echo [HINT] Create venv or install Python launcher.
pause
exit /b 1

:cleanup
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$procs=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*\.chrome-profile*' }; foreach($p in $procs){ Stop-Process -Id $p.ProcessId -Force }" >nul 2>&1
pause
endlocal
