"""AutoReSleep v2 - cross-platform wake / re-sleep control.

One tool, three operating systems, standard library only:

  Windows   GetLastInputInfo + SetSuspendState + Task Scheduler XML/schtasks
  Linux     Mutter/XScreenSaver//dev/input idle + systemctl suspend + systemd user units
  macOS     ioreg HIDIdleTime + pmset sleepnow + launchd LaunchAgents

What it does: when a machine wakes for a reason nobody asked for (the Wi-Fi NIC losing its
association, a roam, an unrelated pattern match), and nobody is using it, put it back to sleep.
A wake that WAS asked for - a magic packet, or the sender's marker packet - is never re-slept.

Why v2 exists: v1 was PowerShell, so it was Windows-only. The chain ORDER (the actual product)
turned out to be portable; the plumbing was not. v2 keeps the order, moves the plumbing behind
`autoresleep.system`, and makes the portable parts portable:

  * the packet chain is plain UDP sockets on all three OSes;
  * the resume signal is a wall-clock-vs-monotonic skew, so no per-OS resume hook is needed;
  * the scheduler artifacts are text, so "does the Windows judge trigger on resume?" is
    answerable by a unit test running on Linux;
  * every threshold lives in one JSON config, and every decision is one log line.
"""
__version__ = "2.0.0.dev0"
__all__ = ["__version__"]