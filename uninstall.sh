#!/bin/sh
# =====================================================================
#  AutoReSleep v2 - UNINSTALL                                (Linux/macOS)
#
#  WHAT THIS REMOVES
#    * the jobs it installed
#        Linux : systemctl --user stop/disable autoresleep-judge.timer/.service,
#                autoresleep-listener.service
#                + systemctl disable/stop autoresleep-wol-sensor.service   (root)
#        macOS : launchctl bootout the two LaunchAgents (com.autoresleep.judge / .listener)
#    * the firewall rules it opened (UDP 9/7/67/68/45678) when a known firewall is running
#    * the running listener, so nothing keeps holding UDP 9/7/67/68/45678
#  WHAT IT KEEPS
#    * config.yaml and the logs. --purge also drops the logs.
#      The config is kept either way, on purpose: it is the only record of what you set.
#
#  ONE ELEVATION PROMPT, for the privileged half only (the root-owned sensor unit / firewall).
#  This script never calls sudo itself.
#
#  LOOK FIRST, CHANGE NOTHING:  ./uninstall.sh --dry-run
#  REMOVE THE LOGS TOO:          ./uninstall.sh --purge
#  SCRIPTED / CI RUN:           ./uninstall.sh       (with AUTORESLEEP_NO_PAUSE=1)
# =====================================================================
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE" || exit 1

#  Is this a dry run? (so the last line can say "nothing was changed" instead of claiming success)
DRY=""
case " $* " in *" --dry-run "*) DRY=1 ;; esac

keep_open() {
  [ -n "${AUTORESLEEP_NO_PAUSE:-}" ] && return 0
  [ -t 0 ] || return 0
  printf '\n Press Enter to close . . . '
  read -r _ 2>/dev/null || true
  return 0
}

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

#  MSYS/Cygwin: a Windows shell, not Linux or macOS - see install.sh for the full reason.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "[x] This is a Windows shell (MSYS/Cygwin), not Linux or macOS."
    echo "    Use uninstall.cmd instead."
    keep_open
    exit 1
    ;;
esac

PY=$(find_python) || PY=""
if [ -z "$PY" ]; then
  echo "[x] Python 3.9+ was not found."
  echo "    Run the tool with an interpreter you have, e.g.:"
  echo "      python3 \"$HERE/autoresleep.py\" uninstall"
  keep_open
  exit 1
fi

case "$(uname -s)" in
  Darwin) JOBS="launchd  com.autoresleep.judge / com.autoresleep.listener" ;;
  *)      JOBS="systemd  autoresleep-judge.timer/.service, autoresleep-listener.service,
             autoresleep-wol-sensor.service  (root)" ;;
esac

CFG=$("$PY" "$HERE/autoresleep.py" config --path 2>/dev/null) || CFG=""
if [ -n "$CFG" ]; then
  DATADIR=$(dirname -- "$CFG")
else
  CFG="(ask: $PY $HERE/autoresleep.py config --path)"
  DATADIR='<data dir>'
fi

#  The banner, the plan and the closing lines come from the tool itself.
"$PY" "$HERE/autoresleep.py" uninstall "$@"
RC=$?

echo
if [ "$RC" != "0" ]; then
  echo "[x] uninstall exited with code $RC."
  echo "    The failing step is printed above, and the record it wrote is"
  echo "      $DATADIR/elevated-uninstall.progress"
  echo "    To remove things by hand, as root where needed:"
  echo "      systemctl --user disable --now autoresleep-judge.timer autoresleep-judge.service"
  echo "      systemctl --user disable --now autoresleep-listener.service"
  echo "      systemctl disable --now autoresleep-wol-sensor.service"
  echo "      launchctl bootout gui/\$(id -u)/com.autoresleep.judge     # macOS instead"
  echo "      launchctl bootout gui/\$(id -u)/com.autoresleep.listener  # macOS instead"
elif [ -n "$DRY" ]; then
  echo " Dry run finished - nothing was changed. Run ./uninstall.sh without --dry-run to apply it."
else
  echo " Removed. Nothing of AutoReSleep is left scheduled or running."
  echo "   install again: ./install.sh"
fi
keep_open
exit "$RC"
