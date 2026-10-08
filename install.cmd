@echo off
rem ===================================================================
rem  AutoReSleep v2 - one-click install (registers the scheduled tasks)
rem  Double-click this file. No Python required on PATH? see the check below.
rem
rem  Everything it changes, and how to undo it:
rem      python autoresleep.py install --dry-run     (print the plan, touch nothing)
rem      python autoresleep.py uninstall             (undo all of it)
rem  Windows tasks land in Task Scheduler -> \AutoReSleep\ :
rem      AutoReSleep-judge      every N min AND on resume   (logged-on user, highest level)
rem      AutoReSleep-listener   at logon, stays resident    (logged-on user, highest level)
rem      AutoReSleep-collector  on resume                   (SYSTEM)
rem ===================================================================
setlocal
cd /d "%~dp0"

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py -3" )
if not defined PY (
  echo Python 3.9+ was not found on PATH. Install it, or run the following with a full path:
  echo     "C:\Path\to\python.exe" "%~dp0autoresleep.py" install
  pause
  exit /b 1
)

rem ---- elevation: the tool asks for it itself, once, and only for the part that needs it.
rem      The user-level half runs here first; then ONE UAC prompt covers the scheduled jobs and the
rem      firewall, and the privileged child's progress is printed in THIS window (it is hidden, and
rem      a hidden window cannot tell you why it is stuck).
echo Installing AutoReSleep ...
echo.
%PY% "%~dp0autoresleep.py" install %*
echo.
echo Done. Press any key to close.
pause >nul
endlocal