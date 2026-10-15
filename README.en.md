# AutoReSleep v2 — Cross-platform (Windows / Linux / macOS)

v1 (`PowerShell`, see the parent directory's `00-README-sources.md`) only runs Windows because it baked the **pipes** — registering scheduled tasks, reading input idle, parsing powercfg — into the tool itself.
v2 separates the **decision chain** (the actual product) from the **platform pipes**: the chain comes over unchanged; the pipes go into `autoresleep/system/`. Three implementations behind one interface.

Zero dependencies — stdlib only. No pip, no venv, no network. That is deliberate: this runs on machines you touch once every six months and might not even have pip on.

```
v2/
  autoresleep.py            run directly: python autoresleep.py doctor (no install needed)
  pyproject.toml            install as a command: pip install -e .  → then `autoresleep doctor`
  src/autoresleep/
    decide.py               decision engine (pure logic, zero I/O, fully covered by tests)
    pkt.py                  magic packets & markers: one UDP socket across three platforms; NIC MAC enumeration per platform
    resume.py               sleep-resume signal: wall-clock vs monotonic delta (cross-platform)
    config.py paths.py      thresholds & state directory (one destination per platform, cross-platform verified in tests)
    journal.py              logging (stdlib rotating, replacing v1's hand-rolled rotation)
    cli.py                  doctor / judge / listen / collect / install / uninstall / config
    system/{base,windows,linux,macos}.py    platform layer: idle / suspend / wake-source / guards / firewall
    scheduling/{windows,linux,macos}.py     scheduled-task artifacts (text), generated per platform
  tests/                    521 unit tests (run command below)
```

## 1. How each platform implements the same thing

| Concern | Windows | Linux | macOS |
|---|---|---|---|
| User idle | `GetLastInputInfo` (ctypes) | Mutter IdleMonitor → XScreenSaver → `/dev/input/*` mtime | `ioreg` `HIDIdleTime` |
| Suspend | `SetSuspendState(False,True,False)`, falls back to `rundll32` | `systemctl suspend` (after checking `systemd-inhibit`) | `pmset sleepnow` (check `pmset -g assertions` first) |
| Wake source | Power-Troubleshooter event 1 + Kernel-Power 107 + `powercfg /devicequery wake_armed` | `journalctl`/`dmesg` `Wakeup source` + `/sys/power/pm_wakeup_irq` | `pmset -g log` `Wake from` + `Reason:` |
| Busy / in use | `powercfg /requests` (parse by section) + `qwinsta` | ALSA `/proc/asound/.../status` + `who`/`ss` | `pmset -g assertions` |
| Firewall | `netsh advfirewall` rules `AutoReSleep-WoL-UDP-*` | `ufw allow ... proto udp` (prints the command if ufw is absent) | `socketfilterfw --unblockapp` (per-program, not per-port) |
| Scheduled task | Task Scheduler XML + `schtasks` | systemd **user** units (service+timer, `systemctl --user`) | launchd **LaunchAgent** (`launchctl bootstrap gui/$UID`) |
| Sleep-resume trigger | Event trigger (Power-Troubleshooter 1) | Not needed: timer fires once per cycle + listener self-detects | Same as Linux |

Two deliberate design choices:

1. **Resume signal uses clock delta.** v1 had to wire up three separate hooks — Windows event trigger, Linux `systemd-sleep` hook, macOS launchd. Three integrations, three failure points, and the Windows one was the weakest link (armed tasks get frozen at suspend and only really start after resume). But "just woke up" is cross-platform: **wall clock advanced hours, monotonic barely moved**. Any process, any platform, zero privileges can see that. So three hooks collapse into one detector in `resume.py`; each platform is left with only "how to sleep" and "how to collect evidence".

2. **Scheduled-task artifacts are text.** So the question "does the Windows judge really fire on resume?" can be answered by a unit test on Linux (`tests/test_scheduling_artifacts.py` verifies Linux and macOS artifacts on this Windows machine).

## 2. Who runs it on a schedule? (per-platform "scheduled task")

Three roles, three platforms, same division of labour, different wiring:

| Role | When it runs | Windows | Linux | macOS |
|---|---|---|---|---|
| judge (decide) | Every N min **+ on resume from sleep** | Task Scheduler task `\AutoReSleep\AutoReSleep-judge`: event trigger (Power-Troubleshooter 1) + calendar trigger with `PT{N}M` repeat | `autoresleep-judge.timer` (`OnBootSec=1min` + `OnUnitActiveSec={N}min`) → starts `.service` | LaunchAgent `com.autoresleep.judge`, `StartInterval={N*60}` |
| listener (packet capture) | Resident after login | Task Scheduler `\AutoReSleep\AutoReSleep-listener`: logon trigger + 3 failed restarts | `autoresleep-listener.service` (`WantedBy=default.target`, `Restart=always`) | LaunchAgent `com.autoresleep.listener` (`RunAtLoad` + `KeepAlive`) |
| collector (post-resume evidence) | On resume | Task Scheduler `\AutoReSleep\AutoReSleep-collector`: event trigger, runs as **SYSTEM** | **No standalone unit**: listener detects resume and calls `collect` directly | Same as Linux |
| Run-as | — | judge/listener as **logged-in user + HighestAvailable** (`GetLastInputInfo` reads the caller's session; SYSTEM/session 0 always looks "idle for a long time" and would sleep an in-use machine); collector as SYSTEM | User unit (`systemctl --user`), **no root needed**, available on login; `loginctl enable-linger $USER` keeps it running when you're logged out | LaunchAgent (`launchctl bootstrap gui/$UID`), **no sudo needed** |

Two deliberate differences from v1:

- **Linux/macOS have no "resume-time trigger"** (that's a Windows event-triggerer exclusive). So they don't fake one: the listener uses clock delta to self-detect resume and collect immediately; the judge's timer makes up the decision within one cycle.
- **Windows side no longer uses PowerShell to register** (v1 used `Register-ScheduledTask`): v2 writes the task definition as Task Scheduler XML, registers via `schtasks /Create /XML /F` — the definition becomes testable data (unit tests can verify Linux/macOS artifacts on this Windows machine and vice versa). **Reinstall is "delete-then-create"**: delete before register with `schtasks /Delete /TN <name> /F` (best-effort, there's nothing on first install), because in-place overwrite leaves old principals, old triggers, old ACLs behind — the same class of pitfall exists on the other two platforms (`launchctl bootstrap` fails directly on an already-loaded label; `systemctl enable --now` doesn't restart a running unit). So all three platform artifacts carry a rebuild step. The **location** of the tasks matches v1:
  all under one folder in the Task Scheduler library (v1 was `\Hermes\`, v2 is `\AutoReSleep\`);
  in an emergency, three `schtasks /Delete /TN "\AutoReSleep\AutoReSleep-judge" /F` lines take them all down.
- **Relaunching the listener borrows elevation** (confirmed on Windows): the task carries `<RunLevel>HighestAvailable</RunLevel>`, the listener runs at **high integrity**, and a normal-user `taskkill /PID n /F` only gets
  "Error: Cannot terminate process with PID n. Reason: Access is denied." — and the old code threw that return code away and unconditionally printed "stopped the previous listener", so **orphan listeners accumulated** (confirmed on this machine: three processes all bound to
  UDP 9/7/67/68, and the lock file pointed at a pid that had long since exited). Now install/uninstall passes the pid of "the python process still holding the WoL ports" to the **elevated child** to kill (`--stop-pids`, answer travels as argument); the child records each kill in the progress file.
- **Port binding must be exclusive, not `SO_REUSEADDR`** (Windows semantics vs Unix): `SO_REUSEADDR` on Windows means "allow others to bind the same address too"; after binding succeeds, which socket actually receives the packet is out of our control — all three of the above processes bind, and the packet chain goes quietly half-dead. Now `pkt._exclusive()` uses `SO_EXCLUSIVEADDRUSE` (on POSIX it's just not setting reuse): the second process **fails to bind**, and `unavailable()`/`advice()` tells the operator why.
  install then also **asks the OS** "who actually owns 9/7/45678 now" (`netstat -ano -p UDP` / `ss -lunp`);
  if it's not the pid we just started, it reports `[failed]` and exits non-zero — the lock file only proves "a listener is alive", not which one.

- **The resident listener cannot have an ExecutionTimeLimit** (confirmed on Windows): the task XML by default carries
  `<ExecutionTimeLimit>PT10M</ExecutionTimeLimit>`, and Task Scheduler **kills the task and its process the moment it has run for 10 minutes** (confirmed: `Last Result: 267014` = `0x41306` "the task was killed"; pid 31824 started at 08:53:12, last heartbeat 09:02:49). The symptom is the quietest kind: ten minutes after install/login, the packet chain goes **silent**, and `doctor` can still bind 9/7/45678 (because nobody is watching), only reviving on the next login. Now the listener writes `PT0S` (Task Scheduler's "no limit"); judge/collector, the one-shot tasks, keep the limit as a runaway guard.

**Elevation**: on Windows, registering "HighestAvailable" tasks and touching the firewall both need admin; on Linux the `wol-sensor` unit and firewall do too. install/uninstall runs as a **normal user**, and the tool **elevates itself once** (Windows `Start-Process -Verb RunAs` → one UAC; Linux `pkexec` → `sudo` → prints the exact command if neither is available; macOS `osascript`). The elevated child **only writes a timestamped progress file**; the parent reads it and narrates it — the elevated child **never asks anything**. All answers travel as arguments (environment variables do not cross UAC). Unit tests / CI / sandboxes **never pop a UAC**.

Four entry scripts, identical behaviour across the three platforms (banner, interpreter version check, elevation method, exit code, failure hint, whether to keep the window open):

| Platform | Install | Uninstall | Note |
|---|---|---|---|
| Windows | Double-click `install.cmd` | Double-click `uninstall.cmd` | Needs Python 3.9+ on PATH (`python` or `py -3`); UNC/network paths are rejected up front |
| Linux | `./install.sh` | `./uninstall.sh` | Finds `python3` → `python`; in MSYS/Cygwin it will tell you to use `.cmd` instead |
| macOS | `./install.sh` | `./uninstall.sh` | Same; the start-kit stub `python3` gets rejected by the version check |
| (all three) | `python autoresleep.py install` | `python autoresleep.py uninstall` | The scripts are wrappers; the real logic lives in these two commands |

The four scripts share these conventions: `--dry-run` prints the plan and ends with "nothing was changed"; `--yes` skips the "config was generated, continue?" prompt; `AUTORESLEEP_NO_PAUSE=1` stops the script from waiting for a keypress at the end (cron/CI); `AUTORESLEEP_NO_ELEVATE=1` refuses to elevate and only prints the exact command to run. Double-clicking (or `cmd /c`) keeps the window open so you can read the result; running inside an already-open terminal does not. You don't need to memorise where the config lives: `python autoresleep.py config --path` (the scripts ask it exactly that way — no path is hardcoded per platform).

### What the installer does (same across the three platforms)

The first time you run `install`: it generates a config → registers the scheduled task / service → opens the firewall → starts the listener. Each step prints as one line (`1) Config … 2) Jobs … 3) Firewall … 4) Elevate … 5) Listener …`). Add `-v` to see the details (each command, artifact path, hint); **failure always expands fully** and does not need `-v`.

**The menu appears every time** (including the first install): it first states what THIS machine currently has (on a fresh machine that is "not yet installed"), then offers a choice set —
every menu option has an equivalent CLI flag, so you never have to drive the menu:

| Menu option | What it does | Equivalent CLI |
|---|---|---|
| 1) Install / Overwrite (Enter to confirm) | Writes config, registers tasks and service, opens firewall, starts listener; if already installed, rebuilds and leaves config untouched | `install` |
| 2) Uninstall | Removes jobs, revokes firewall rules, stops listener; keeps config and logs by default | `uninstall` |
| 3) Regenerate config | Overwrites with the project's `config.default.yaml` (existing file is backed up as `config.yaml.bak`) | `install --regen-config` |
| 4) Show state | Prints the doctor decision chain | `doctor` |
| 5) Quit | Does nothing | — |

| Platform | Install / Uninstall entry | Note |
|---|---|---|
| Windows | Double-click `install.cmd` / `uninstall.cmd` | Pre-checks (Python version, UNC path) then hands off to the tool |
| Linux | `./install.sh` / `./uninstall.sh` | Same; MSYS/Cygwin will remind you to use `.cmd` |
| macOS | `./install.sh` / `./uninstall.sh` | Same; the start-kit stub `python3` is rejected by the version check |
| All three | `python autoresleep.py install [-v] [--regen-config] [--lang en]` | Dry run `--dry-run`; CI/sandbox `--yes` or `AUTORESLEEP_NO_PROMPT=1` |

**Internationalisation**: `install` / `uninstall` default to Chinese. Pass `--lang en` to get English output on every string in the install / uninstall flow.

`uninstall` asks for confirmation in an interactive terminal (`--yes` skips). The golden rule about prompts: **the elevated child never asks**; **a dry run never asks**; **a non-interactive terminal never asks** (defaults to continuing, never blocks).

## 3. Decision order: driver-agnostic (the NIC driver does not explain itself)

1. **Requested wake** (a magic or marker packet is captured) → never resleep; the **whole wake session is excluded from the sleep logic**
2. **Watchdogs**: inbound remote session / display power request / audio playing / Windows Update running → no resleep.
   All four can be switched off individually (`guard_remote_in_use` / `guard_display_in_use` / `guard_media_playing` /
   `guard_update_running`); **`guard_display_in_use` is off by default**: remote-control / streaming tools hold the
   display power request all day (measured `CrossDesk.exe`), so leaving it on means the machine never resleeps; a
   real remote operator is still covered by `guard_remote_in_use`. A disabled watchdog does not vanish: the decision
   line records it as `guards=ignored:display-in-use[CrossDesk.exe]`.
   A hit also names the holder (`guards=display-in-use[CrossDesk.exe]`) instead of making you guess
3. **Keyboard/mouse/USB wake**: two clocks answer the same question — **hardware input clock** (a real evdev event
   at the wake instant) or **in-window** (any input within `resleep_delay_seconds` after the wake) → no resleep for
   the whole session. The window side only looks at **deltas** (the platform clock is sampled twice at intervals; a
   change means somebody is there), never absolute readings: the desktop idle counter is reset to zero on resume,
   which is exactly the root cause of "every wake looks like a keypress". The delta has a second layer too: a change
   inside `input_settle_seconds` (default 5 s) of the wake is the **machine's own resume side effect** (a HID device
   replaying its state), not a person — the settle window Linux has always had, now on the other two platforms as
   well, and the decision line says so (`win=none@windows-window-tick(1-settled)`)
4. **The network moved during sleep** (four independent pieces of evidence; any one condemns) → **wait for the
   window to finish** before resleeping: **nl80211 events recorded by `iw event` (primary evidence)**, the
   pre-sleep snapshot diff (association lost / BSSID changed / kernel `carrier_changes` rose / system log shows a
   re-association after sleep)
5. **The wake reason the driver reports itself**: **recorded only, never judged on**. It is each NIC's private
   string (some drivers don't print one at all, and the field names differ), so sleeping on it = sleeping on the
   vendor's wording. This chain no longer has a `return` in the code
6. **OS attribution**: a NIC in `wake_nics` → resleep candidate (**wired and wireless treated equally**, so is
   `eth-nic`); **a NIC not in the list** → intentional wake, no resleep; `timer` / `other-device` /
   `fixed-feature` → intentional wake; `fixed-feature-nic-suspect` (Windows cannot name the device, no wake
   timers, the only armed devices are NICs) → defaults to `skip`, resleeps only with an explicit
   `resleep-if-unattended` or `magic_only`
7. **Nothing was obtained** → do not guess: require the long idle of `unknown_idle_minutes` + no remote session
8. **Debouncing**: less than `min_gap_minutes` since the last forced sleep → skip (to survive a router poking the
   machine awake repeatedly); but if "not a single input event since our last forced sleep" can be confirmed AND the
   wake came from network hardware (chain 4), use `min_gap_unattended_minutes` (default 1 minute) instead

**The window IS the wait (chains 4 and 6)**: the wait is `resleep_delay_seconds` (default 10 s, counted from the
wake instant), no longer the 2-minute idle. **The moment the window expires, one decision is made immediately
inside the listener process** (not waiting for the 30-second `fast_path_seconds` tick) — otherwise the resleep
decision for every NIC wake would be dragged out to "10 s window + up to 30 s"; the same window is decided only
once (the window state stays on disk; `age >= delay` would be true forever, and without deduplication it becomes a
decision every second). The "confirm nobody is using it" threshold is still there; only the criterion changed to
"zero input events inside the window".
`idle_minutes` comes back as a backstop in only two cases: the evidence has no session stamp (the collector never
ran), or the machine was used again after the wake (which means the window's conclusion is stale — it cannot
authorise sleeping a machine that is being used). Configuration is in section 6b.

### Three listeners: they judge the wake cause, the driver does not

| Listener | Runs where | Requires | Judges | Records |
|---|---|---|---|---|
| **nl80211 events** (`iw event -t`) | Privileged sensor (resident child process) | root reads nl80211 | wifi disconnect/roam/reconnect wakes | `wlan-events.log` |
| **Magic packet** UDP 9/7 | Privileged sensor | `CAP_NET_BIND_SERVICE` | magic-packet wakes | `packet-last.txt` |
| **Magic-packet raw frame** ethertype `0x0842` | Privileged sensor | `CAP_NET_RAW` | `etherwake`-style senders (**no UDP**) | same |
| **Hardware input** `/dev/input/event*` | Privileged sensor (thread) | `SupplementaryGroups=input` | keyboard/mouse/USB wakes | `input-last.json` |

The judge only reads these files: zero coupling between processes, no sudo process, no driver field touched. The
sensor's privileges are **exactly three**:
`CAP_NET_BIND_SERVICE` + `CAP_NET_RAW` + the `input` group; the process identity is still the logged-in user
(`Uid=1000`).

**Why not `tcpdump`**: it is a general-purpose packet capture tool; a resident root tcpdump just to wait for WoL
frames is too heavy. `AF_PACKET` bound to ethertype `0x0842` delivers **only WoL frames** from the kernel —
equivalent but near-zero overhead, and our socket is frozen (not closed) during suspend, so it is still there when
the machine wakes: strictly more coverage than "start a tcpdump before suspending".
The `iw event` path is the same idea: no script polling `sudo iw`; the unit takes its privilege once and listens to
nl80211 for the long haul.

**Why the X11 idle clock must not judge keyboard/mouse** (a pitfall that was actually hit): resume resets the X
idle counter **to 0**, so **every** wake showed `idle=0s`, was judged "keyboard/mouse wake", and got latched by the
session lock — a machine woken by a wifi disconnect then never reslept for the whole day (measured: 34 of 41
decisions in one day were `wake-by-input idle=0s`). Now only real evdev hardware events judge keyboard/mouse, and
**only they may create a session exemption**; the X11 fallback can only "skip this one", never write a latch.

### Why the exemption must "latch the whole session" instead of one time window

Packets and input are facts **near the wake instant** and can only be recognised inside a window; but the
conclusion ("this machine was woken on purpose, do not sleep") must live until the session ends. The first
version did only the window, and the measured consequence is what you would see in the log:

```
09:51:53  skip   wake-by-input          idle=50s     <- someone tapped the keyboard
09:53:04  SLEEP  NON_MAGIC_INPUT_WAKE   idle=121s    <- two minutes later (idle passed 120s), person still there, machine slept
          PM: suspend entry (deep) 09:53:04
```

The magic-packet side is the same: once `pkt_window_s=180` is past, the evidence is stale → resleep.

So now **recognition still happens inside the window, and the recognition result is latched** into
`session-cause.json`:

- written the moment the packet is heard (listener/privileged sensor), without waiting for the next decision;
- the judge writes it too (keyboard/mouse wakes, and cases where the immediate write was missed);
- the record carries a **session identifier** — the `session` timestamp that recovery writes into
  `evidence-last.json`. The next recovery writes a new identifier, and the old latch stops matching on its own —
  **no process has to remember to clear it**, and it cannot leak across sessions.
- when the judge rewrites evidence to backfill link probing it keeps this identifier (a change would equal a new
  session, and the latch would silently die).

Both exemptions in the decision therefore become "return skip directly" and never enter the idle threshold or the
debounce sleep logic at all; while **wakes caused by the wireless NIC itself (link drop/reconnect, chain 4) still
resleep by `idle_minutes`** — the latch only applies to packets and input, and only to its own session. To verify:

```
Measured (this machine 2026-09-29 10:2x, same real evidence + idle=600s):
  magic-packet wake, one hour later (packet long since stale)      skip   wake-requested     who=asked-for-this-session
  keyboard/mouse wake, person left an hour ago                    skip   wake-by-input       who=input-this-session
  packet and wake at the same time (just heard)                  skip   wake-requested     who=magic@+2s
  wireless NIC drop/reconnect (no latch)                          sleep  NON_MAGIC_WLAN_LINK_MOVED
  latch from the previous session (must not apply)                sleep  NON_MAGIC_WLAN_LINK_MOVED
```

One exception to know: if **the machine was already awake** and somebody sends a magic packet, it does not count
as "the cause of this wake" (packet age 3 s vs session age 1 h — too far apart), chain 1 does not fire; but the
listener still latches when it hears the packet — meaning "somebody is explicitly asking this machine to stay
awake".

### Why chains 3 and 4 need no driver

The wake reason a driver gives (Windows `WakeSourceText`, the string a driver prints in Linux dmesg) is not
guaranteed to exist on any of the three platforms: after a fixed-feature NIC wakes the machine, the driver simply
does not say who poked it. So the decision uses only **facts the OS itself publishes**:

| To judge | Observable fact | Windows | Linux | macOS |
|---|---|---|---|---|
| Keyboard/mouse wake | Input clock: did the last input fall at the wake instant | `GetLastInputInfo` | XScreenSaver / `/dev/input` timestamp | `HIDIdleTime` of `ioreg -c IOHIDSystem` |
| Wake-packet wake | The UDP the listener itself received | same | same | same |
| WiFi disconnect/reconnect | Pre-sleep snapshot vs current association | `netsh wlan show interfaces` + WLAN-AutoConfig event | `iw dev <if> link` + `/proc/net/wireless` + `carrier_changes` | `airport -I` |

The Linux "media playing" watchdog asks the **sound server** (`pactl`, answers on both pipewire and pulse), not
`state: RUNNING` in `/proc/asound`: PipeWire keeps analog outputs idling (`hw_ptr` advancing, zero streams), and a
machine that only reads the kernel line is judged "playing audio" forever and never resleeps (measured on this
machine, where `pactl list sinks` clearly says `State: IDLE`). Only when there is no sound server to ask (pure tty +
`aplay` style) does it fall back to the kernel read — where `RUNNING` really does mean a client is playing.

`/sys/class/net/<if>/carrier_changes` is a pure kernel counter, and it **sees reconnection to the same AP too**:
SSID unchanged, BSSID unchanged, but the link layer really did drop. That is the class of evidence an
instantaneous state can never show.

So `link=` is no longer "a hint" but the actual conclusion of chain 4: the pre-sleep snapshot is written to
`link-presleep.json` (judge and listener fast path each write one before every suspend), and the listener
additionally looks at the link once a second, appending changes to `link-events.log`. The v1 lesson still holds —
"after recovery the machine will certainly re-associate, so `LINK_LOST` alone cannot condemn a requested
wake" — but it is now structurally guaranteed by chain 1 (packet evidence): magic/marker packets `return` before the
link chain ever runs, so link inference never gets a chance to override them.

## 3b. Resume detection and latch invalidation (short sleeps must be visible)

Two clocks: one precise, one a backstop.

- **Precise: the suspend ledger.** `CLOCK_BOOTTIME` (includes suspend) minus `CLOCK_MONOTONIC` (excludes it) is the
  total seconds this machine has slept, to the millisecond, monotonically increasing, no threshold needed. **No
  sleep can escape it**, not even a 1-second one. Windows uses the equivalent delta of `GetTickCount64` minus
  `QueryUnbiasedInterruptTime`.
- **Backstop: wall-clock skew.** The original criterion (wall vs monotonic delta > `resume_threshold_s=90`) **does
  not see short sleeps**: measured, a `10:50:06` suspend, `10:50:23` resume (**17 seconds**) was not recognised as
  a resume, the collector did not run, the session stamp did not move, an old "keyboard/mouse wake" latch kept
  matching, and the machine did not resleep two minutes later. Use this only when the ledger is unavailable.

`CLOCK_BOOTTIME` and `CLOCK_MONOTONIC` are two independent reads, possibly straddling a clock tick, so the delta
jitters (`LEDGER_EPSILON_S = 0.5`). Measured, this jitter once "detected 4 resumes" while the machine was fully
awake, each rewriting evidence and invalidating a legitimate latch — hence the tolerance.

**Latch invalidation has two independent paths**, both must hold:

1. the `session` stamp differs (the collector wrote a new resume);
2. `slept_total` differs (**the machine itself slept**, whether or not any process noticed).

With only path 1, an unnoticed short sleep would pin an old latch forever. Path 2 lets the machine's own history
invalidate the latch. The judge also self-heals: it finds the ledger moved but the evidence is stale, and runs a
make-up collection (new session stamp).

## 3c. Which "inputs" are not human (ACPI keys itself)

Measured: `rtcwake -m mem -s 20` — pure RTC wake, nobody in the room — the **`Power Button` device emitted
`KEY_POWER` two seconds before resume**; the input clock treated it as "a key was pressed" and wrote an `input`
latch, exempting a wifi-disconnect wake from resleep. So:

- the device denylist (`DENY`) extends to ACPI/control-class devices: `power button`, `sleep button`, `lid switch`,
  `wmi hotkeys`, `acpi`, `video bus`, `pc speaker`, `rfkill`;
- key-code filtering (`SYSTEM_KEY_CODES`): `KEY_POWER(116)`, `KEY_SLEEP(142)`, `KEY_WAKEUP(143)`,
  `KEY_SUSPEND(205)` are never human input no matter which device they come from.

## 3f. A flooding device must not take the machine down (CPU 100% / disk filled / input stalled)

Measured: after resume the mouse **keeps** reporting (`Logitech G502 HERO`, over a thousand `value=±1` events
per second). The first listener version paid for that in three ways, and they all had **the same root cause**:

1. **CPU 100%**: `select` returns **immediately** when there is data to read, and only 64 events were read per
   pass — as long as the device is still flooding, the loop spins at full speed.
2. **Two file operations per event**: `_audit()` does `open/write/close` once per event; `_write()` rewrites
   `input-last.json` per event (read old file + write temp + `rename`). A thousand events a second = several thousand
   filesystem calls a second.
3. **Disk filled**: the audit log is about 80 bytes per event, about 80 KB/s ≈ **6.9 GB/day** (measured: 71 MB /
   910,000 lines within a few hours). Once `/home` filled up, writes started failing — that is "disk not
   accessible"; meanwhile saturated IO starves the USB input path, and the feel is "the keyboard and mouse lost
   power".

The fixes:

- **Audit aggregation**: count by `(device, kind, note)`, write at most once per `AUDIT_FLUSH_S` (0.5 s), one line
  carrying `n=` and the count per `code`; **the same flooding device costs two lines/second, not a thousand**.
- **Audit rotation**: over `AUDIT_MAX_BYTES` (2 MB) rolls to `.1`, keeping only one generation.
- **State-file throttling**: `input-last.json` records only the **last** event, so it is written once per
  `STATE_WRITE_S` (1 s) with the content cached in memory (no more re-reading the file before every write); the
  moment the stream goes quiet it flushes, so the file is never "stale".
- **Flood pause**: reading a **full buffer** means more data is waiting, so `sleep(FLOOD_PAUSE_S)` (2 ms) caps the
  loop rate instead of letting it own a core.

Measured (real event structs piped in): **30,394 events/s** (30× heavier than the real mouse) sustained for 10 s;
the watcher's own CPU was **3.4% of one core** and the audit log held **21 lines** — the old implementation would
have produced 300,000 lines and thousands of file operations per second.

## 3e. Wake source ≠ wake cause (USB acts out on itself at resume)

Measured (kernel log, identical across three wakes):

```
xhci_hcd 0000:02:00.0: xHC error in resume, USBSTS 0x401, Reinit
usb usb1: root hub lost power or was reset
usb 1-6: reset full-speed USB device number 3 using xhci_hcd
```

Every resume the kernel **resets the entire USB bus**, and HID devices **replay their own state** on re-init: with
nobody in the room and a pure RTC wake (`rtcwake -m mem -s 20`), the mouse burst **80 REL events with `value=-1`**
at +7 s after resume, 13 at +11 s, 16 at +12 s, wheel codes `8/11` alternating — that is not a hand. Counting them
as human input latches the whole session with an `input` exemption and the machine never resleeps again (that is
the `12:12:09 action=skip reason=wake-by-input` line).

The fixes:

- after resume, **ignore motion events for `MOTION_SETTLE_S` (15 s, the longest measured replay)**; key-press
  events are **never** affected by this window — a real keypress at the wake instant is exactly what the exemption
  exists for, and a hand still moving after the window counts as before;
- every accepted or dropped input event is written to `input-events.log` (device, kind, code, value; dropped ones
  tagged `settling:ignored`); the decision line names the device and time:
  `via=Logitech G502 HERO Gaming Mouse@41s-before`;
- **never print a stale packet as `pkt=magic`**: a magic packet from 40 minutes ago used to appear on every
  decision line, reading like "this was a requested wake" and hiding the real decision basis. Now a packet is only
  shown inside `pkt_window_s`; otherwise its age is shown with `(203m-old:ignored)`.

## 3d. Two `iw event` pitfalls (both hit)

1. **`readline()` blocks the sensor's main thread.** When the link is quiet `iw event` prints **nothing at all**,
   and `readline()` blocks forever on a silent pipe. Measured with `/proc/<pid>/stack`: the main thread was stuck in
   `anon_pipe_read` — so the main loop stopped polling the UDP 9/7 and raw-frame `0x0842` sockets, and **a real
   magic packet was completely missed**. Fix: read in a dedicated thread; `drain()` only reports counts and never
   blocks. The regression test asserts "when the child is silent, drain returns immediately".
2. **The event format must be copied from the kernel**, not from imagination. Measured format:
   ```
   1790738340.015097: wlp5s0 (phy #0): mgmt TX status (cookie 135): acked
   1790738412.501000: wlp5s0 (phy #0): disconnected (by AP) reason: 3 (DEAUTH_LEAVING)
   1790738412.777000: wlp5s0 (phy #0): new 44:df:65:cd:00:00 station became authorized
   ```
   The MAC sits **between** `new` and `station`, so the literal `new station` never matches a real association;
   `\bdeauth\b` also fails to match `DEAUTH_LEAVING` (the underscore is a word character). Both once caused real
   events to be dropped.

Also: **the listener writes a `# iw-event tap started ...` heartbeat line at startup**. A machine whose link never
drops produces zero events, and without a heartbeat there is no way to tell "listener healthy and quiet" from
"listener broken".


## 4. Status matrix (what has really run, what has not)

| Capability | Windows | Linux | macOS |
|---|---|---|---|
| Decision engine (pure logic) | ✅ 369 unit tests (run on this machine, all three platforms share the same source) | ✅ same unit tests | ✅ same unit tests |
| Scheduling artifact generation | ✅ unit tests | ✅ unit tests | ✅ unit tests |
| Idle probe | ✅ **measured on real hardware** (6.9 min on this machine) | ⚠️ written, not yet run on Linux | ⚠️ written, not yet run on macOS |
| NIC enumeration | ✅ **measured on real hardware** (10 NICs, including virtual) | ✅ **measured on real hardware** (`wlan0`) | ⚠️ written, not run |
| NIC jurisdiction (`wake_nics`) | ✅ **measured on real hardware** (2 of 9 configured on this machine, including Chinese names with spaces) | ⚠️ unit tests | ⚠️ unit tests |
| Wake-source parsing (driver) | ✅ **measured on real hardware** (matches v1's decision from the same day) | ⚠️ unavailable on this machine (`kind=unknown`) — **this is exactly why it was changed to be driver-agnostic** | ⚠️ written, not run |
| Link probing (driver-agnostic) | ⚠️ unit tests (`netsh` / WLAN-AutoConfig parsing) | ✅ **measured on real hardware** (14 consecutive reconciliations with `iw` agreed; `carrier_changes` rose 4 times in 30 min on this machine) | ⚠️ written, not run |
| Input clock (keyboard/mouse wake) | ✅ **measured on real hardware** | ✅ **measured on real hardware** (all three levels of X11 idle 0s / 210s / 538s read on this machine) | ⚠️ written, not run |
| Resleep window (`resleep_delay_seconds`) | ⚠️ unit tests + `judge` on real hardware gives `window-open`; **full "wake→resleep" chain to be done** | ⚠️ unit tests (input side reuses the evdev verified on real hardware) | ⚠️ unit tests |
| In-window input (delta criterion) | ⚠️ unit tests + `input_settle_seconds` calibrated once against real hardware (2026-10-15: replay landed ~1.6 s after the wake); a later replay is still indistinguishable | ✅ evdev verified on real hardware + settle window | ⚠️ not run, calibration needed |
| Session-level exemption latch | ⚠️ unit tests | ✅ **measured on real hardware** (after the packet goes stale for an hour it still says `skip wake-requested who=asked-for-this-session`; a keyboard/mouse wake with `idle=600s` still says `skip wake-by-input`) | ⚠️ unit tests |
| Privileged WoL ports (9/7) | — | ✅ **measured on real hardware** (system unit + CAP_NET_BIND_SERVICE only, Uid=1000; after binding, a magic packet judges as `wake-requested`) | — |
| Watchdogs (remote session / audio / updates) | ✅ **measured on real hardware** | ✅ **measured on real hardware** (audio watchdog fixed for the PipeWire idle misjudgement; `guards=-` reproduced) | ⚠️ written, not run |
| Sleep invocation | ✅ **measured on real hardware** (full "wake→resleep" timeline) | ✅ **measured on real hardware** (three verdicts match the kernel `PM: suspend entry` line by line) | ⚠️ not run |
| Scheduled-task registration | ✅ **measured on real hardware** (`schtasks` + `pythonw.exe`) | ✅ **measured on real hardware** (3 systemd user units, 4 `[ok]` lines) | ⚠️ not run |
| One-shot self-elevation (install/uninstall) | ✅ **measured on real hardware** (normal user runs install → one UAC → the child records each step in the progress file while the parent reads it aloud; delete-then-create of tasks, firewall, listener restart all handled in one go) | ⚠️ unit tests (pkexec/sudo on real hardware to be done) | ⚠️ unit tests (osascript to be done) |
| Firewall rules | ✅ **measured on real hardware** (rule points at `pythonw.exe`; after reinstall each rule has exactly 1 entry, no longer 4 stacked) | ⚠️ status has only been printed (no user-level interface) | ⚠️ not run |
| Single listener instance / port ownership | ✅ **measured on real hardware** (`SO_EXCLUSIVEADDRUSE` + elevated child kills the old listener + install asks the OS who owns 9/7/45678) | ⚠️ not run (POSIX side relies on the default exclusive binding) | ⚠️ not run |

Every "not run" is marked here instead of being glossed over. This machine is now **Linux** (the machine from the
timeline above); Windows is the original dev machine and its results also come from real hardware; macOS is only
guaranteed by **the same unit tests + `doctor` self-check output** for interface consistency — real confirmation has
to be done on the target machine (section 6).

## 5. Run it

```bash
# No install needed (any platform, Python 3.9+)
python autoresleep.py doctor          # first see what this machine supports; every line of output carries its source
python autoresleep.py judge           # decide only, write the log (does NOT sleep by default!)
python autoresleep.py judge --apply   # real decision + real sleep
python autoresleep.py listen          # resident: capture packets + detect resume
python autoresleep.py wol-sensor      # only bind 9/7 and record evidence (what the root system unit runs)
python autoresleep.py collect         # collect evidence on resume (the scheduler calls it)
python autoresleep.py install         # write config + register scheduled task/unit/LaunchAgent (+ firewall)
python autoresleep.py uninstall       # undo everything, printing what was touched, line by line
python autoresleep.py config --set idle_minutes=4 --set unknown_idle_minutes=20
autoresleep config --set "wake_nics=WLAN 4,Ethernet 2" magic_only=true   # multiple pairs at once also work
```
Install as a command: `pip install -e .` (for dev, `pip install -e ".[dev]"`). Run the tests:
```bash
python -m unittest discover -s tests -v        # 369 tests, no third-party dependencies (9 POSIX-pipe cases skip on Windows)
```

**The safe default differs from v1 (deliberately)**: `judge` only decides and writes the log; **only with
`--apply` does it actually sleep**. v1 was "do it by default, add `-DryRun` to stop it"; v2 will be run by people
who have never read a script, so the destructive half was flipped to opt-in.

**Elevation (a single command as a normal user)**: `install` / `uninstall` elevate themselves once; no admin
terminal needed first. The parent finishes the user-level half in your identity (write config, user units, start
the resident task); the privileged actions go to a **hidden child process**: Windows shows one UAC
(`ShellExecuteW("runas")` + `SW_HIDE`), Linux uses `pkexec` (graphical auth box) falling back to `sudo`, macOS uses
`osascript ... with administrator privileges`. The child writes each step to
`elevated-<command>.progress` (line-based, timestamped), and the parent **reads it aloud while waiting** — because
you cannot see that window, a silent wait forces the operator to guess. It **never asks** (a question inside an
invisible window equals a hang), so every answer is decided by the parent and carried through internal arguments.
On failure, read that record first, then the manual command it printed.

Do not want the prompt at all (CI, sandbox, just want to see what would run): `--no-elevate`, or set
`AUTORESLEEP_NO_ELEVATE=1`; it prints the exact command to run as administrator.

**Language**: `install` and `uninstall` default to Chinese. Add `--lang en` to get the entire flow (menu, plan
lines, confirmation, tail) in English.

## 6. State directory and files (v1 source prefixes kept)

| Platform | Directory |
|---|---|
| Windows | `%LOCALAPPDATA%\AutoReSleep` |
| Linux | `$XDG_STATE_HOME/autoresleep`, default `~/.local/state/autoresleep` |
| macOS | `~/Library/Application Support/AutoReSleep` |

| File | Source chain | Content |
|---|---|---|
| `session-cause.json` | `session-` | why this session was asked to wake (`asked-for` / `input`) + session identifier. **The whole session's** exemption rests on it; it stays valid after the window expires; it dies automatically on a new session |
| `decisions.log` | `dec-` | one line per decision: `action= reason= src= link= who=` + `idle=` / `win=` / `pkt=` / `drv=` / `guards=` (`win=` says whether someone was in the window: `in` / `none` / `-`, followed by the clock that decided it) |
| `heartbeat.txt` | `dec-` | overwrite each line: time + decision + idle seconds (a glance tells you it is still alive) |
| `packets-seen.log` | `pkt-` | every capture: `DETECTED kind=magic\|marker src=IP:port len=N` |
| `packet-last.txt` | `pkt-` | newest evidence: `magic\|epoch=..\|src=..\|len=..` (the judge reads it) |
| `evidence-last.json` | `drv-`/`os-` | the wake source of the last resume: `kind` / `who` / `raw` / `chain` (`chain=drv` means the driver itself reported it; in the decision it outranks `os`) |
| `link-presleep.json` | `link-` | pre-sleep link snapshot (`connected`/`ssid`/`bssid`/`changes`/`at`): after resume it judges whether the WiFi actually dropped, no driver needed |
| `link-events.log` | `link-` | link changes the listener sampled (drop/AP change/reconnect); over 256 KB only the last 500 lines are kept |
| `window-input.json` | `win-` | the "was anyone in the resleep window" conclusion: the anchor (which wake), the deadline, whether it ended, `input`, the clock readings of the first and last samples. The listener samples every second, the judge reads it directly — the same pattern as the packet evidence |
| `elevated-<command>.progress` | — | line-based progress record of the elevated child (timestamped; the parent reads it aloud while waiting); read it first on failure |
| `journal.log` | — | run log (stdlib rotation, `log_max_bytes`/`log_backups`) |
| `config.yaml` | — | all thresholds (YAML with comments; `autoresleep config` prints it, `config --set key=value` changes it) |
| `config.json.migrated` | — | the old JSON config: renamed, not deleted, when install migrated it to `config.yaml` on its first run |

The `raw` field always preserves the OS's own words (Windows event fields, Linux dmesg lines, macOS pmset lines) —
categorisation is convenience; the evidence itself is not altered.

## 6b. Configuration

The config file is **YAML**. The project keeps a **canonical default** `config.default.yaml` (with Chinese
comments, at the repo root); on a fresh machine `install` copies it **byte for byte** into the data directory,
and from then on all reads and writes only touch the data directory copy
(`%LOCALAPPDATA%\AutoReSleep\config.yaml` / `~/.local/state/autoresleep/config.yaml`):
`autoresleep config` prints **the installed one**, `config --set key=value` edits that one; the project template
stays put. To set a starting point for a batch deployment, edit the project one; when the data directory copy
already exists `install` **leaves it alone** (hand-written comments and edits are kept) and only rewrites it when a
value really differs (e.g. a command-line override). Zero dependencies: reading and writing go through the
bundled `miniyaml` (only the subset this file needs); it must run on machines without pip.
**The first `install` generates a default config automatically and stops to ask** "config generated — continue?";
to edit it first, press Enter or answer `n`, then run `install` again; add `--yes` to the script (or set
`AUTORESLEEP_NO_PROMPT=1`) and it will not ask. An old machine's `config.json` is migrated to `config.yaml`
automatically (the old file is renamed to `config.json.migrated`, not deleted).
The 8 keys added in this round answer everything about "who is being managed, how long to wait, what can cancel":

| Key | Default | Meaning |
|---|---|---|
| `wake_nics` | `[]` | **NICs to discipline**. `[]` = all wired + all wireless. An element may be a MAC (any spelling; normalised) or a NIC name (case-insensitive; platform human-readable names are also recognised, e.g. Windows `WLAN 4` / `Ethernet 2`). A wake from a NIC outside the list → no resleep, treated as intentional. **A configured name that matches nothing on this machine makes `doctor` and the log warn** — silent failure is this project's most feared failure mode |
| `magic_only` | `false` | "only WoL magic packets may wake": NIC-class wakes no longer look at how the OS/driver explains them and all become resleep candidates (the packet and keyboard/mouse exemptions still apply). **The window and threshold still apply** — it is not "non-magic packets sleep immediately" |
| `resleep_delay_seconds` | `10` | the observation window after a wake, **counted from the wake instant**. A magic packet or keyboard/mouse input inside it cancels the resleep; it never sleeps before the window ends |
| `resleep_break_on_input` | `true` | keyboard/mouse inside the window → breaks the resleep. Turning it off = strict mode: "keypless inside the window still sleep", but the decision line says so explicitly (`who=input-ignored-by-strict-mode`), never silently |
| `input_settle_seconds` | `5` | an input change within this many seconds of the wake is the **resume's own HID replay** (a device replayed its state while the bus came back up), not a person, so that judgement does not count as "somebody is there". Linux goes through evdev, which carries device names and its own 15-second settle, and is unaffected; Windows/macOS only have the platform clock, so time is the only thing to draw the line with. The `(N-settled)` token in the decision line is how many changes it absorbed; if a replay lands later than that (you see `win=in@...-tick` while nobody was at the machine), raise it — up to `resleep_delay_seconds`, which equals strict mode |
| `guard_display_in_use` | **`false`** | the display is held (someone `SetThreadExecutionState` in the DISPLAY section of `powercfg /requests`) → no resleep. **Off by default**: remote-control/streaming tools hold it all day — measured, this is what kept this machine from ever resleeping |
| `guard_remote_in_use` | `true` | an active remote session (`qwinsta` rdp/tcp + active) → no resleep |
| `guard_media_playing` | `true` | media playing → no resleep (Windows cannot attribute audio to a specific request yet, see the limits list) |
| `guard_update_running` | `true` | Windows Update running (a request in the EXECUTION section + `mousocoreworker`/`tiworker` etc. present) → no resleep |

Two values are written into the task XML **only at task registration time**; changing the config does not change
the registered tasks: `repeat_minutes` (the judge's repeat interval) and the execution time limit. To make them
take effect, run `install` again (delete-then-create, one UAC);
`doctor` tells you directly whether the two sides agree (e.g. `judge repeats every PT2M, config repeat_minutes=1`
→ suggests running install to rebuild).

`--set` for `wake_nics` splits only on **commas** (Windows NIC names contain spaces); for multiple pairs at once
either `--set a=1 b=2` or `--set a=1 --set b=2` works.

The window replaced the old field `idle_minutes` as "how long to wait for a NIC-class wake"; `idle_minutes` is
kept, but only as a backstop threshold when the **evidence has no session stamp** (the collector never ran) or
**the machine was used again after the wake**. Unclear attribution (`unknown_idle_minutes`) and debouncing
(`min_gap_minutes` / `min_gap_unattended_minutes`) are unchanged.

## 7. Confirming on Linux / macOS (to be run on those two machines)

```bash
# 1) first the self-check: every capability, source, current idle, NIC MACs and firewall state are printed
python autoresleep.py doctor

# 2) the idle probe must be a sensible small number (not UNAVAILABLE or 0)
#    Linux: try all three cases — wayland session, X11 session, pure tty
#    macOS: should show HIDIdleTime (ns -> s)

# 3) decisions only, do not touch the system
python autoresleep.py judge

# 4) register + verify
python autoresleep.py install --dry-run     # first see what would be written and which commands would run
python autoresleep.py install
#    Linux: systemctl --user list-timers autoresleep-judge.timer
#           systemctl --user status autoresleep-listener.service
#    macOS: launchctl print gui/$(id -u)/com.autoresleep.judge | head -30

# 5) send a real packet and see whether the pkt chain captures it (verifiable on this machine alone, no second machine needed)
python - <<'EOF'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(b"HERMES-WOL-MARKER" + b"hello", ("127.0.0.1", 45678))
EOF
#    expected: packets-seen.log gets DETECTED kind=marker, and judge immediately says reason=wake-requested
#    measured (this machine 2026-09-29 08:56:50):
#      packets-seen.log: DETECTED kind=marker src=127.0.0.1:60922 len=41
#      judge           : action=skip reason=wake-requested src=pkt who=marker@+2s  (link=LINK_LOST at the time)
#    magic packets (UDP 9) have an additional privilege issue on Linux, see the two sections below
```

**Firewall** (the same pitfall on all three platforms): the listener binds UDP 9/7/67/68/45678 to the Python
process; with a firewall up, inbound unicast is dropped by default and the `pkt-` chain **silently** captures
nothing forever.
`install` handles Windows (`netsh`), Linux (adds automatically when `ufw` is present, otherwise prints the
command to run), macOS (allow the interpreter per program). `--no-firewall` skips explicitly.

**Rules are "delete-then-create", not "just add"** (measured on Windows): `netsh add rule` does not error on an
existing **same-named** rule — it quietly adds another one; after several installs on this machine, each rule name
had **4** rules stacked under it, and every install stacked one more; meanwhile the old success test
`if _run(["netsh",...,"show",...]).strip()` was always true (when no rule is found, netsh still prints a non-empty
"no matching rule"), so **a failed add was reported as success**.
`uninstall` was worse: it deleted only one rule, then checked "does the name still exist" — of course it did,
so the rules stayed in place, the WoL ports stayed open, and the report said "nothing was removed".
Now `_drop_rules()` deletes by name until clean (a 50-iteration cap against an infinite loop), adds again, and
verifies by counting **separator lines**;
`uninstall` loops until nothing is left.

### Linux: WoL ports (9/7) are privileged — install solves it with one minimal system unit

The listener is a normal user's `--user` unit, and Linux defaults to
`net.ipv4.ip_unprivileged_port_start=1024`,
so none of 9/7/67/68 can be bound — only the marker port 45678 is alive (measured on this machine: `refused:
7/9/67/68 (Permission denied)`). The symptom is not an error but "reslept two minutes after a magic-packet
wake": the packet is never seen, so the decision cannot know this wake was requested.

So `install` additionally registers **one** root-owned system unit `autoresleep-wol-sensor.service` that does
only "bind 9/7 + record one evidence line":

```ini
User=jianghh                                   # the process still runs as you, not root
Group=jianghh
Environment=HOME=/home/jianghh
AmbientCapabilities=CAP_NET_BIND_SERVICE        # exactly this one extra capability
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=no                              # ambient capabilities can only be granted while this is off
ExecStart=.../autoresleep.py wol-sensor
```

Measured (this machine 2026-09-29 09:20): `Uid: 1000`, `CapEff: 0000000000000400` (**only**
CAP_NET_BIND_SERVICE, all other capability bits 0), `net.ipv4.ip_unprivileged_port_start` still 1024
(no machine-wide relaxation); at the same moment a normal user binding 9 by hand is still `Permission denied`.
Send a real magic packet to UDP 9:

```
packets-seen.log: 09:23:04  DETECTED kind=magic src=127.0.0.1:49244 len=102
judge           : action=skip reason=wake-requested src=pkt link=LINK_LOST who=magic@+2s
```

**Why not move the whole listener into the system unit**: the polkit policy for
`org.freedesktop.login1.suspend` is `allow_inactive=auth_admin_keep` — a process without a session calling
`systemctl suspend` is asked for interactive authentication, which becomes a "never sleeps" silent failure (the
failure mode this project fears most). Keeping the decision and resleep in the user unit means no polkit rule has
to change; the privileged process is as small as "bind 9/7 + write one line".

To keep install from writing this system unit: add `--no-sensor`, or afterwards run `sudo systemctl disable --now
autoresleep-wol-sensor && sudo rm /etc/systemd/system/autoresleep-wol-sensor.service`
(`uninstall` removes it too). There are then two root-free paths: point the sender at the marker port 45678, or
raise the kernel boundary `sudo sysctl -w net.ipv4.ip_unprivileged_port_start=9` (at the cost of letting every
unprivileged process on the machine bind ports ≥ 9).

`doctor` prints this line: `sensor: active - the privileged half watches UDP 9/7`; when the user listener finds
the port already watched by someone else it no longer alarms — it says "already watched by the privileged unit,
which is the designed division of labour".

### Linux: `Linger=no` means nobody watches after a reboot

`--user` units live and die with the login session: with `Linger=no` (the default) logging out stops everything,
and after a reboot, with nobody logged in, nothing runs at all
(this machine is currently `Linger=no`). To make it watch even when nobody is logged in:

```bash
sudo loginctl enable-linger $USER     # once, needs root
```
`doctor` prints the current state, and `install` also warns when `Linger=no`.

## 8. Known limits (v1's honest list, plus the new ones from v2)

- The single packet that wakes the host from sleep is consumed by the NIC firmware; **no host-side capture on any
  platform can see it**;
  what `pkt-` captures is the duplicate/marker packets that arrive after the machine is back awake. A hardware
  limit.
- Whether a driver reports the wake reason is up to the vendor (on the same machine it comes and goes), so the
  decision does not depend on it: keyboard/mouse look at the input clock,
  WiFi disconnects look at the pre-sleep snapshot vs the kernel `carrier_changes`; driver information only
  supplements `who=`.
- **Linux WoL ports (9/7) need privilege**: unless `net.ipv4.ip_unprivileged_port_start` is raised, magic packets
  sent to those two ports are invisible to the listener and only the marker port hears them — and that chain is
  precisely the guarantee that "a requested wake never resleeps". See section 7.
- **Never hard-code a NIC model in a script**: swapping cards / reinstalling drivers changes the wake-source string
  (v1 measured `AX1650i` →
  `AX210`, interface `WLAN` → `WLAN 5`, and the description gains a ` #2`). v2 rebuilds the names from this
  machine every time,
  and additionally cross-checks the name in `WakeSourceText` against `Get-NetAdapter` descriptions.
- Linux wake-source parsing is the least uniform of the three platforms (kernel version / distro / whether
  journald is present all affect the output),
  `raw` is always kept, categorisation may be conservative → that is exactly where `doctor` on real hardware has to
  calibrate.
- The clock-delta detection mistakes "an NTP correction exceeding `resume_threshold_s` (default 90 s)" for a
  resume;
  sleeps shorter than the threshold are invisible — tier B is unaffected, because the wall-clock idle decision does
  not depend on it.
- macOS's application firewall allows by **program**, not by port, and only blocks unsigned inbound connections.
- **`pythonw.exe` + subprocess = a black window flash**: a process without a console gets a fresh console window
  from Windows every time it starts a console program.
  judge calls `powercfg` for every decision, so it flashes every 30 seconds. v2 adds
  `CREATE_NO_WINDOW` (plus a hidden `STARTUPINFO`) to every `subprocess`, and a source-level unit test fails on a
  bare `subprocess.run([...])`.
- **Do not run v1 and v2 listeners on the same machine at once**: both bind the same UDP ports
  (9/7/67/68/45678),
  and on Windows only the first binder receives unicast packets — measured: the v2 listener bound later but saw no
  packets at all,
  and every one of them was recorded by v1's listener. To coexist, change one side's `ports` / `marker_port` first.
- **The resleep window decision depends on the listener running**: the window is "N seconds after the wake", and
  only the listener is awake every second,
  so the fast path can decide the moment it expires. On Linux with `Linger=no` and nobody logged in the listener is
  not there, and the decision falls back to the 2-minute-granularity
  scheduled task (the decision itself is still correct, just slower).
- **In-window input detection on Windows / macOS cannot distinguish "a keypress" from "HID replay after
  resume"**: those two platforms have no
  evdev and can only use **deltas** of the platform input clock, and HID replay moves those deltas too.
  `input_settle_seconds` (default 5 s) exists for exactly that: a change within those seconds of the wake is
  treated as the machine's own resume side effect, counted in `settled` and not as a person. It cannot cover a
  later replay (measured on this box: ~1.6 s after the wake; on Linux: +7/+11/+12 s), and that one is still read
  as input — the cost direction is conservative (the machine does not sleep rather than sleeps wrongly), and the
  exit is to raise this value or set `resleep_break_on_input=false`. Linux has no such problem: evdev carries the
  device name, plus a 15-second motion settle window.
- **The window's conclusion only describes those N seconds after the wake**: if the machine was used again after
  the wake (the last input is newer than the wake instant),
  the window's old conclusion expires and it falls back to the `idle_minutes` threshold. Otherwise a session from
  hours ago would put the machine to sleep on top of a person typing.
- **Elevation uses one UAC / auth box**: `install` / `uninstall` do it themselves; no admin terminal needed; to
  avoid the prompt use
  `--no-elevate` (just prints the command to run). The elevated child is hidden and its progress is relayed through
  `elevated-*.progress` —
  if it hangs, read that record first.

## 9. Mapping to v1 (for when you want to compare)

| v1 | v2 | Note |
|---|---|---|
| `resleep.ps1` | `decide.py` + `cli.py judge` | the decision order is unchanged word for word; platform calls moved to `system/` |
| `wol-listener.ps1` | `cli.py listen` + `pkt.py` | capture + resume detection; `NativeIdle2.dll` no longer needed (ctypes direct) |
| `wake-reason-capture.ps1` / `wake-reason-arm.ps1` | `cli.py collect` + `system.*.wake_source()` | the ETW tracing machinery only makes sense on Windows; v2 uses each OS's own wake-source interface |
| `wlan-wake-evidence.ps1` | `system.*` guards/link evidence | `LINK_LOST` is still only a hint |
| `install-tasks.ps1` / `uninstall-tasks.ps1` | `cli.py install` / `uninstall` + `scheduling/` | one set of commands across the three platforms; artifacts generated per platform |
| `run-hidden.vbs` | not needed | `pythonw.exe` (Windows) is windowless by itself |
| `drv-` / `pkt-` / `os-` / `dec-` prefixes | kept as-is | the `chain` field in `evidence-last.json` is exactly that |
