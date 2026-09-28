"""Per-OS scheduling artifacts, generated as text.

Why text: the interesting question - "does the Windows judge actually trigger on a resume?" -
must be answerable on a Linux box and vice versa. Each platform module returns `Artifact`
objects; `apply()` runs the OS's own tool, and everything is reversible with `remove()`.

Task inventory (same three jobs on every OS, because the jobs are the product, not the OS):

  judge      every `repeat_minutes` AND on resume   decide whether to go back to sleep
  listener   at login, stays up                     magic-packet/marker detection + trace arming
  collector  on resume                              wake-source evidence (per-OS)

Resume triggers only exist on Windows (a Task Scheduler event trigger). On Linux and macOS the
judge's timer catches it within one interval, and the listener daemon notices the wall/monotonic
skew (`autoresleep.resume`) within seconds - so no per-OS resume hook is required.
"""
from __future__ import annotations

from typing import List, Optional

from .artifact import Artifact

__all__ = ["Artifact", "artifacts_for"]


def artifacts_for(platform: str, python_exe: str, workdir: str, interval_minutes: int,
                  user: Optional[str] = None, config_path: Optional[str] = None) -> List[Artifact]:
    """Build the install artifacts for one platform. Pure: no side effects, no OS calls."""
    plat = (platform or "").lower()
    if plat.startswith("win"):
        from .windows import windows_artifacts
        return windows_artifacts(python_exe, workdir, interval_minutes, user, config_path)
    if plat == "darwin":
        from .macos import macos_artifacts
        return macos_artifacts(python_exe, workdir, interval_minutes, config_path)
    if plat.startswith("linux") or plat.startswith("freebsd"):
        from .linux import linux_artifacts
        return linux_artifacts(python_exe, workdir, interval_minutes, config_path)
    raise ValueError("unsupported platform: %r (win32 / linux / darwin are the supported set)"
                     % platform)