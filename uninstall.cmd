@echo off
rem =====================================================================
rem  AutoReSleep v2 - UNINSTALL                                      (Windows)
rem
rem  WHAT THIS REMOVES
rem    * the three Task Scheduler jobs in \AutoReSleep\  (judge / listener / collector)
rem    * the AutoReSleep-WoL-UDP-* firewall rules
rem    * the running listener (so nothing keeps holding UDP 9/7/67/68/45678)
rem  WHAT IT KEEPS
rem    * config.yaml and the logs. --purge also drops the logs.
rem      The config is kept either way, on purpose: it is the only record of what you set.
rem
rem  ONE UAC PROMPT, for the privileged half only (the jobs and the firewall). This file never
rem  elevates anything by itself; the hidden child reports every step into THIS window.
rem
rem  LOOK FIRST, CHANGE NOTHING:  uninstall.cmd --dry-run
rem  REMOVE THE LOGS TOO:          uninstall.cmd --purge
rem  SCRIPTED / CI RUN:           uninstall.cmd        (with AUTORESLEEP_NO_PAUSE=1)
rem =====================================================================
setlocal EnableExtensions
set "HERE=%~dp0"
cd /d "%HERE%" 2>nul

rem  Is this a dry run? (so the last line can say "nothing was changed" and not lie about removing)
set "DRY="
for %%a in (%*) do if /i "%%a"=="--dry-run" set "DRY=1"

rem  Never call bare `find` / `where`: launched from a POSIX shell (MSYS/Cygwin) the PATH picks up
rem  THEIR find.exe, which scans the filesystem and hangs on permission errors. System32, by name.
set "SYS=%SystemRoot%"
if not defined SYS set "SYS=C:\Windows"
set "SYS=%SYS%\System32"

set "PY="
"%SYS%\where.exe" python >nul 2>&1 && set "PY=python"
if not defined PY ( "%SYS%\where.exe" py >nul 2>&1 && set "PY=py -3" )
if defined PY (
  %PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
  if errorlevel 1 (
    echo [x] Found %PY%, but it is older than Python 3.9.
    set "PY="
  )
)
if not defined PY (
  echo [x] Python 3.9+ was not found on PATH.
  echo     Run the tool with a full path instead:
  echo       "C:\Path\to\python.exe" "%HERE%autoresleep.py" uninstall
  call :keep_open
  exit /b 1
)

set "CFG=%LOCALAPPDATA%\AutoReSleep\config.yaml"
for /f "usebackq delims=" %%p in (`%PY% "%HERE%autoresleep.py" config --path 2^>nul`) do set "CFG=%%p"

echo.
echo  AutoReSleep - uninstall
echo  =======================
echo    removes: \AutoReSleep\ tasks, AutoReSleep-WoL-UDP-* firewall rules, the listener
echo    keeps  : "%CFG%" and the logs   ^(add --purge to drop the logs too^)
echo.
echo  One UAC prompt appears for the privileged half; its progress is printed below.
echo.

%PY% "%HERE%autoresleep.py" uninstall %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" goto :uninstall_failed
if defined DRY goto :uninstall_dried
echo  Removed. Nothing of AutoReSleep is left scheduled or running.
echo    install again: install.cmd
goto :uninstall_end

:uninstall_dried
echo  Dry run finished - nothing was changed. Run uninstall.cmd without --dry-run to apply it.
goto :uninstall_end

:uninstall_failed
echo [x] uninstall exited with code %RC%.
echo     The failing step is printed above, and the record it wrote is
echo       %LOCALAPPDATA%\AutoReSleep\elevated-uninstall.progress
echo     To remove things by hand, in an ADMINISTRATOR shell:
echo       schtasks /Delete /TN "\AutoReSleep\AutoReSleep-judge" /F
echo       schtasks /Delete /TN "\AutoReSleep\AutoReSleep-listener" /F
echo       schtasks /Delete /TN "\AutoReSleep\AutoReSleep-collector" /F
echo       netsh advfirewall firewall delete rule name="AutoReSleep-WoL-UDP-9"
echo     ^(repeat that netsh line for ports 7, 67, 68 and 45678^)

:uninstall_end
call :keep_open
endlocal & exit /b %RC%

rem ---------------------------------------------------------------------
:keep_open
rem  Double-clicked (console closes when we return) vs typed in an open console (console stays).
rem  System32's findstr BY ABSOLUTE PATH - a bare one under an MSYS PATH is THEIR binary.
if defined AUTORESLEEP_NO_PAUSE goto :eof
set "KEEP="
echo %CMDCMDLINE% | "%SYS%\findstr.exe" /i /c:"%~nx0" >nul 2>&1 && set "KEEP=1"
if not defined KEEP goto :eof
echo.
echo  Press any key to close this window . . .
pause >nul
goto :eof