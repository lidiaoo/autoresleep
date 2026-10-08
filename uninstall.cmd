@echo off
rem ===================================================================
rem  AutoReSleep v2 - one-click uninstall
rem  Removes: the \AutoReSleep\ tasks, the AutoReSleep-WoL-UDP-* firewall
rem           rules, the resident listener, and (with --purge) the logs.
rem  Keeps:   config.json and the logs unless you ask for --purge.
rem  Double-click this file; it asks for elevation itself.
rem ===================================================================
setlocal
cd /d "%~dp0"

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py -3" )
if not defined PY (
  echo Python 3.9+ was not found on PATH. Run manually:
  echo     "C:\Path\to\python.exe" "%~dp0autoresleep.py" uninstall
  pause
  exit /b 1
)

rem ---- elevation: the tool asks for it itself (one UAC prompt), and prints the hidden child's
rem      progress here. Nothing to do in this file.
echo Uninstalling AutoReSleep ...
echo.
%PY% "%~dp0autoresleep.py" uninstall %*
if errorlevel 1 (
  echo   hint: schtasks /Delete /TN "\AutoReSleep\AutoReSleep-judge" /F
  echo         schtasks /Delete /TN "\AutoReSleep\AutoReSleep-listener" /F
  echo         schtasks /Delete /TN "\AutoReSleep\AutoReSleep-collector" /F
)
echo.
echo Done. Press any key to close.
pause >nul
endlocal