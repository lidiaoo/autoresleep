#!/bin/sh
# =====================================================================
#  AutoReSleep v2 - INSTALL                                  (Linux/macOS)
#
#  WHAT THIS DOES
#    * config  : copies config.default.yaml -> the data dir's config.yaml
#                (only when it is not there yet - and then it STOPS to ask, so you can edit it
#                 first: answer n, edit, run this again, or pass --yes)
#    * jobs    : installs and starts
#                  Linux : systemd --user  autoresleep-judge.timer/.service,
#                          autoresleep-listener.service
#                          + /etc/systemd/system/autoresleep-wol-sensor.service
#                            (root: it is the one that sniffs UDP 9/7/67/68)
#                  macOS : launchd         ~/Library/LaunchAgents/com.autoresleep.{judge,listener}.plist
#    * firewall: opens inbound UDP 9/7/67/68/45678 when a known firewall is running
#                (ufw / firewalld on Linux; on macOS the tool says what to do if pf gets in the way)
#    * starts the listener right away
#
#  WHAT THIS DOES NOT DO
#    * no power/ACPI setting, no NIC/device setting, no driver install, nothing else
#
#  ELEVATION, the same story as the other two platforms: the tool elevates itself ONCE for the
#  privileged half (pkexec, else sudo, else it prints the exact command to run). This script never
#  calls sudo itself. Run it as your NORMAL user - the jobs must belong to the account that logs
#  in, and the root-only parts are exactly what that one prompt is for.
#
#  LOOK FIRST, CHANGE NOTHING:  ./install.sh --dry-run
#  UNDO EVERYTHING:             ./uninstall.sh
#  SCRIPTED / CI RUN:           ./install.sh --yes     (set AUTORESLEEP_NO_PAUSE=1 too)
# =====================================================================
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE" || exit 1

#  Is this a dry run? (so the last line can say "nothing was changed" instead of claiming success)
DRY=""
case " $* " in *" --dry-run "*) DRY=1 ;; esac

#  A terminal the user launched this from survives the script ending; a pipe or cron does not.
#  Stop for a keypress only when there is a terminal to stop in, and honour AUTORESLEEP_NO_PAUSE=1
#  (same idea as AUTORESLEEP_NO_ELEVATE) so cron/CI/scripted runs never block waiting for input.
keep_open() {
  [ -n "${AUTORESLEEP_NO_PAUSE:-}" ] && return 0
  [ -t 0 ] || return 0
  printf '\n Press Enter to close . . . '
  read -r _ 2>/dev/null || true
  return 0
}

#  python3 first, then python - and 3.9+ (pyproject: requires-python). On macOS a bare python3 can
#  be the developer-tools stub that only exists to nag you into installing Xcode; the version check
#  rejects it instead of handing you a confusing ImportError later.
find_python() {
  for cand in python3 python; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
    then
      printf '%s' "$cand"
      return 0
    fi
  done
  return 1
}

#  MSYS/Cygwin are Windows shells wearing a POSIX hat: the job names printed below would be a lie,
#  and a native Windows python cannot even open an MSYS path (it turns /e/... into E:\e\...). Send
#  the user to the launcher that is actually for Windows instead of half-failing later.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "[x] This is a Windows shell (MSYS/Cygwin), not Linux or macOS."
    echo "    Use install.cmd instead: it finds Windows Python and talks to Task Scheduler."
    keep_open
    exit 1
    ;;
esac

PY=$(find_python) || PY=""
if [ -z "$PY" ]; then
  echo "[x] Python 3.9+ was not found."
  echo
  echo "    Debian/Ubuntu : sudo apt install python3"
  echo "    Fedora        : sudo dnf install python3"
  echo "    Arch          : sudo pacman -S python"
  echo "    macOS         : brew install python3     (or: xcode-select --install)"
  echo
  echo "    Then run this again, or point at an interpreter directly:"
  echo "      /path/to/python3 \"$HERE/autoresleep.py\" install"
  keep_open
  exit 1
fi

case "$(uname -s)" in
  Darwin) JOBS="launchd  ~/Library/LaunchAgents/com.autoresleep.{judge,listener}.plist" ;;
  *)      JOBS="systemd --user  autoresleep-judge.timer/.service, autoresleep-listener.service
             + /etc/systemd/system/autoresleep-wol-sensor.service  (root: it sniffs UDP)" ;;
esac

#  Where THIS machine reads its config: ask the tool (on Linux it lives under XDG_STATE_HOME, on
#  macOS under ~/Library/Application Support) instead of hard-coding one platform's answer.
CFG=$("$PY" "$HERE/autoresleep.py" config --path 2>/dev/null) || CFG=""
if [ -n "$CFG" ]; then
  DATADIR=$(dirname -- "$CFG")
else
  CFG="(ask: $PY $HERE/autoresleep.py config --path)"
  DATADIR='<data dir>'
fi

if [ "$(id -u)" = "0" ]; then
  echo "[!] You are running this as root. AutoReSleep's jobs must belong to the account that logs"
  echo "    in, so run it as that user - the single elevation prompt covers the root-only half."
  echo
fi

printf '\n AutoReSleep - install\n'
printf ' =====================\n'
printf '   config : %s\n' "$CFG"
printf '   jobs   : %s\n' "$JOBS"
printf '   undo   : ./uninstall.sh\n'
printf '\n One elevation prompt appears for the privileged half; its progress is printed below.\n\n'

"$PY" "$HERE/autoresleep.py" install "$@"
RC=$?

echo
if [ "$RC" != "0" ]; then
  echo "[x] install exited with code $RC."
  echo "    The privileged half is all-or-nothing: the failing step is printed above, and the"
  echo "    record it wrote is $DATADIR/elevated-install.progress. To redo just that half, as root:"
  echo "      $PY \"$HERE/autoresleep.py\" install --elevated --progress \"$DATADIR/elevated-install.progress\""
elif [ -n "$DRY" ]; then
  echo " Dry run finished - nothing was changed. Run ./install.sh without --dry-run to apply it."
else
  echo " Installed. Next:"
  echo "   check it now   : $PY \"$HERE/autoresleep.py\" doctor"
  echo "   edit the config: $CFG"
  echo "   undo it        : ./uninstall.sh"
fi
keep_open
exit "$RC"
