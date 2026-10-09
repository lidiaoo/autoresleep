@echo off
rem =====================================================================
rem  AutoReSleep v2 - INSTALL                                        (Windows)
rem
rem  WHAT THIS DOES
rem    * config  : copies config.default.yaml -> the data dir's config.yaml
rem                (only when it is not there yet - and then it STOPS to ask, so you can
rem                 edit it first: answer n, edit, run this again, or pass --yes)
rem    * jobs    : registers three Task Scheduler jobs in \AutoReSleep\
rem                  AutoReSleep-judge      every N min AND on resume   (you, highest level)
rem                  AutoReSleep-listener   at logon, stays resident    (you, highest level)
rem                  AutoReSleep-collector  on resume                   (SYSTEM)
rem    * firewall: opens inbound UDP 9/7/67/68/45678 for the listener's interpreter
rem    * starts the listener right away
rem
rem  WHAT THIS DOES NOT DO
rem    * no power-plan change, no NIC/device setting, no driver install, nothing else
rem
rem  ONE UAC PROMPT. The user-level half runs here; the jobs and the firewall are the
rem  privileged half, which the tool asks for itself - once. This file never elevates
rem  anything by itself, and the hidden privileged child reports every step back into
rem  THIS window (a hidden window cannot tell you why it is stuck).
rem
rem  LOOK FIRST, CHANGE NOTHING:  install.cmd --dry-run
rem  UNDO EVERYTHING:             uninstall.cmd
rem  SCRIPTED / CI RUN:           install.cmd --yes     (set AUTORESLEEP_NO_PAUSE=1 too)
rem =====================================================================
setlocal EnableExtensions
set "HERE=%~dp0"
cd /d "%HERE%" 2>nul

rem  Is this a dry run? (so the last line can say "nothing was changed" and not lie about installing)
set "DRY="
for %%a in (%*) do if /i "%%a"=="--dry-run" set "DRY=1"

rem  Never call bare `find` / `where`: launched from a POSIX shell (MSYS/Cygwin) the PATH picks up
rem  THEIR find.exe, which scans the filesystem and hangs on permission errors. System32, by name.
set "SYS=%SystemRoot%"
if not defined SYS set "SYS=C:\Windows"
set "SYS=%SYS%\System32"

rem  `cd /d \\server\share` cannot work: cmd.exe has no UNC current directory, and Task Scheduler
rem  needs a local path anyway. Pure-batch check - no external command involved.
if "%HERE:~0,2%"=="\\" (
  echo [x] This script is running from a network path ^(%HERE%^).
  echo     cmd.exe cannot use a UNC path as its working directory.
  echo     Copy this folder to a local disk and run it there.
  call :keep_open
  exit /b 1
)

rem ---- interpreter: `python` first, then the `py` launcher - and 3.9+ (pyproject:
rem      requires-python). A too-old interpreter is reported, never silently used.
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
  echo.
  echo     Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^),
  echo     or run the tool with a full path:
  echo       "C:\Path\to\python.exe" "%HERE%autoresleep.py" install
  call :keep_open
  exit /b 1
)

rem ---- where this machine reads its config: ask the tool, do not hard-code %LOCALAPPDATA%.
set "CFG=%LOCALAPPDATA%\AutoReSleep\config.yaml"
for /f "usebackq delims=" %%p in (`%PY% "%HERE%autoresleep.py" config --path 2^>nul`) do set "CFG=%%p"

echo.
echo  AutoReSleep - install
echo  =====================
echo    config : %CFG%
echo    jobs   : Task Scheduler -^> \AutoReSleep\  ^(judge / listener / collector^)
echo    undo   : uninstall.cmd
echo.
echo  One UAC prompt appears for the privileged half; its progress is printed below.
echo.

%PY% "%HERE%autoresleep.py" install %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" goto :install_failed
if defined DRY goto :install_dried
echo  Installed. Next:
echo    check it now   : %PY% "%HERE%autoresleep.py" doctor
echo    edit the config: "%CFG%"
echo    undo it        : uninstall.cmd
goto :install_end

:install_dried
echo  Dry run finished - nothing was changed. Run install.cmd without --dry-run to apply it.
goto :install_end

:install_failed
echo [x] install exited with code %RC%.
echo     The privileged half is all-or-nothing: the failing step is printed above and the
echo     record it wrote is
echo       %LOCALAPPDATA%\AutoReSleep\elevated-install.progress
echo     To redo just that half, open an ADMINISTRATOR shell and run:
echo       %PY% "%HERE%autoresleep.py" install --elevated --progress "%LOCALAPPDATA%\AutoReSleep\elevated-install.progress"

:install_end
call :keep_open
endlocal & exit /b %RC%

rem ---------------------------------------------------------------------
:keep_open
rem  A double-clicked .cmd is launched as `cmd /c "..\install.cmd"` and its console closes the
rem  moment we return; a .cmd typed inside an open console must NOT stop for a keypress.
rem  Checked with System32's findstr BY ABSOLUTE PATH: a bare `find`/`findstr` under an MSYS PATH is
rem  THEIR binary (it scans the filesystem instead of the pipe), and the `%CMDCMDLINE:%~nx0=%`
rem  substring trick does not expand `%~nx0` inside the modifier - measured, it pauses either way.
rem  AUTORESLEEP_NO_PAUSE=1 covers cron/CI/scripted runs (same idea as AUTORESLEEP_NO_ELEVATE).
if defined AUTORESLEEP_NO_PAUSE goto :eof
set "KEEP="
echo %CMDCMDLINE% | "%SYS%\findstr.exe" /i /c:"%~nx0" >nul 2>&1 && set "KEEP=1"
if not defined KEEP goto :eof
echo.
echo  Press any key to close this window . . .
pause >nul
goto :eof