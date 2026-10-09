"""Windows scheduling: Task Scheduler XML applied with schtasks.exe.

Deliberate difference from v1: v1 called `Register-ScheduledTask` through PowerShell. schtasks
takes an XML file, which means the task definition is *data we can test* - and the tests for it
run on any OS. What must not change (v1 lesson): the judge runs as the logged-on user with the
highest available level, because an idle probe must read the interactive session's input; a
SYSTEM/session-0 process sees "idle forever" and would suspend a machine someone is using.

Three things here were learned the hard way on a real Windows 10 box. All three fail with a
message that does not name the culprit:

1. `<Subscription>` is ESCAPED TEXT, not child nodes. Task Scheduler serializes an event
   subscription as `&lt;QueryList&gt;...&lt;/QueryList&gt;` inside that element; embedding raw
   `<QueryList>` elements makes schtasks answer "unexpected node: Subscription" and point at a
   column number - the cause is nowhere in the message. (Reference: the XML Task Scheduler itself
   exports for the same construct, obtained by registering a probe task and exporting it.)
2. ELEMENT ORDER IS ENFORCED, and not in the order the SDK docs suggest. `<Settings>` is a strict
   sequence: `<RegistrationInfo>` and `<Principals>` likewise. The order emitted below is the one
   Windows itself produces and re-imports; the wrong order reads as "unable to read the contents
   of the XML file".
3. A scheduled action is launched DIRECTLY, with no cmd.exe, so `>> file 2>&1` is not a
   redirection but three extra argv entries - and `-m autoresleep` only resolves for an installed
   package, so a checkout runs the launcher script instead.

Run as a non-elevated user, the schema can still be verified end to end: register the generated
document with `RunLevel=LeastPrivilege` and see whether schtasks accepts it (that is what
tests/test_scheduling_artifacts.py does when AR_LIVE_TASK_TESTS=1).
"""
from __future__ import annotations

import os
import time
from typing import List, Optional
from xml.sax.saxutils import escape

from .artifact import Artifact, launcher_parts

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK_VERSION = "1.2"
#: The scheduled judge must actually suspend; the CLI stays decide-only by default because a
#: human at a keyboard wants to look first. A task action with a bare `judge` never sleeps
#: anything and reports success every two minutes.
JUDGE_MODE = "judge --apply"
PREFIX = "AutoReSleep"
#: tasks live in their own folder, like v1's \Hermes\ - one folder to look at, and
#: `schtasks /Delete /TN \AutoReSleep\...` is a usable emergency uninstall
TASK_FOLDER = "\\AutoReSleep\\"

#: The order Windows itself emits. A test asserts the generated document keeps it, because the
#: only other way to find out is to read the XSD again.
SETTINGS_ORDER = (
    "DisallowStartIfOnBatteries",
    "StopIfGoingOnBatteries",
    "ExecutionTimeLimit",
    "MultipleInstancesPolicy",
    "StartWhenAvailable",
    "RestartOnFailure",
    "IdleSettings",
)
REGISTRATION_ORDER = ("URI", "Author", "Version", "Description", "Documentation")


def task_path(name: str) -> str:
    """The full Task Scheduler path (schtasks wants the folder INCLUDED in /TN)."""
    return TASK_FOLDER + name


def subscription(query: str) -> str:
    """An event subscription as ESCAPED TEXT - the only shape Task Scheduler accepts."""
    return "<Subscription>%s</Subscription>" % escape(query)


RESUME_QUERY = (
    "<QueryList><Query Id='0' Path='System'><Select Path='System'>"
    "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and (EventID=1)]]"
    "</Select></Query></QueryList>"
)
SLEEP_QUERY = (
    "<QueryList><Query Id='0' Path='System'><Select Path='System'>"
    "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=42)]]"
    "</Select></Query></QueryList>"
)

RESUME_SUBSCRIPTION = subscription(RESUME_QUERY)
SLEEP_SUBSCRIPTION = subscription(SLEEP_QUERY)


def _xml(name: str, description: str, command: str, arguments: str, user: str,
         logon_type: str, run_level: str, triggers: str,
         execution_limit: str = "PT10M", restart: bool = False) -> str:
    restart_xml = ("<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count>"
                   "</RestartOnFailure>") if restart else ""
    # A service-account principal (SYSTEM) must OMIT <LogonType>: the XML enumeration has no
    # 'ServiceAccount' value (that spelling belongs to the PowerShell cmdlet), and Task Scheduler
    # itself writes SYSTEM principals with UserId + RunLevel only. (Measured: schtasks answers
    # "(7,33):LogonType:ServiceAccount" - "value out of range" - and refuses the whole document.)
    logon_xml = "<LogonType>%s</LogonType>" % logon_type if logon_type else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="{TASK_VERSION}" xmlns="{TASK_NS}">
  <RegistrationInfo><URI>{escape(task_path(name))}</URI><Author>AutoReSleep</Author><Description>{escape(description)}</Description></RegistrationInfo>
  <Triggers>{triggers}</Triggers>
  <Principals><Principal id="Author">
    <UserId>{escape(user)}</UserId>
    {logon_xml}<RunLevel>{run_level}</RunLevel>
  </Principal></Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>{execution_limit}</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    {restart_xml}
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(command)}</Command><Arguments>{escape(arguments)}</Arguments></Exec>
  </Actions>
</Task>
"""


def _data_dir() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "AutoReSleep")


def repeating_trigger(interval_minutes: int) -> str:
    """A one-shot TimeTrigger with an unbounded repetition: the shape Windows itself produces
    when asked for 'every N minutes, for ever' (a CalendarTrigger/ScheduleByDay also works)."""
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60))
    return ("<TimeTrigger><StartBoundary>%s</StartBoundary>"
            "<Repetition><Interval>PT%dM</Interval>"
            "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>"
            % (start, int(interval_minutes)))


def windows_artifacts(python_exe: str, workdir: str, interval_minutes: int,
                      user: Optional[str] = None,
                      config_path: Optional[str] = None) -> List[Artifact]:
    def task(name: str, description: str, command: str, arguments: str, principal: str,
             logon: str, level: str, triggers: str,
             restart: bool = False, execution_limit: str = "PT10M") -> Artifact:
        xml = _xml(name, description, command, arguments, principal, logon, level, triggers,
                   execution_limit=execution_limit, restart=restart)
        xml_path = os.path.join(_data_dir(), name + ".xml")
        tn = task_path(name)
        return Artifact(name=name,
                        path=tn,
                        content=xml,
                        kind="task",
                        # delete-then-create, NOT an in-place overwrite (pitfalls 7): `/F` replaces
                        # the definition but leaves whatever the old task carried - a stale
                        # principal, old triggers, an ACL somebody set by hand - and the symptom is
                        # a task that looks right in the XML and behaves like the one before it.
                        prepare_cmd=[["schtasks", "/Delete", "/TN", tn, "/F"]],
                        apply_cmd=["schtasks", "/Create", "/TN", tn, "/XML", xml_path, "/F"],
                        remove_cmd=["schtasks", "/Delete", "/TN", tn, "/F"],
                        start_cmd=["schtasks", "/Run", "/TN", tn],
                        note="xml written to " + xml_path)

    triggers_judge = ("<EventTrigger>%s</EventTrigger>" % RESUME_SUBSCRIPTION
                      + repeating_trigger(interval_minutes))

    judge_user = user or ("%s\\%s" % (os.environ.get("USERDOMAIN", "."), os.environ.get("USERNAME", "")))
    artifacts = [
        task(PREFIX + "-judge",
             "Decide whether to go back to sleep: every %d min AND on resume" % interval_minutes,
             *launcher_parts(python_exe, workdir, JUDGE_MODE, config_path, windowless=True),
             judge_user, "InteractiveToken", "HighestAvailable", triggers_judge),
        task(PREFIX + "-listener",
             "Resident listener: magic packet / marker detection",
             *launcher_parts(python_exe, workdir, "listen", config_path, windowless=True),
             judge_user, "InteractiveToken", "HighestAvailable",
             "<LogonTrigger><UserId>%s</UserId></LogonTrigger>" % escape(judge_user),
             restart=True,
             # NO TIME LIMIT (`PT0S` is Task Scheduler's "unlimited"; the default here was PT10M).
             # A resident listener that Task Scheduler kills ten minutes after it starts is a packet
             # chain that silently dies ten minutes after every logon and stays dead until the next
             # one - measured on this machine: pid 31824 started 08:53:12, last heartbeat 09:02:49,
             # task's last result 0x41306 ("the task was terminated"), and `doctor` right afterwards
             # could bind UDP 9/7/45678, i.e. nothing was watching them at all.
             execution_limit="PT0S"),
        task(PREFIX + "-collector",
             "On resume: collect the OS wake-source evidence",
             # pythonw for the collector too: a SYSTEM console can still flash on the desktop, and
             # the job logs to journal.log anyway
             *launcher_parts(python_exe, workdir, "collect", config_path, windowless=True),
             "S-1-5-18", "", "HighestAvailable",      # SYSTEM: no <LogonType> element, see _xml
             "<EventTrigger>%s</EventTrigger>" % RESUME_SUBSCRIPTION,
             execution_limit="PT5M"),
    ]
    return artifacts


def write_xml_files(artifacts: List[Artifact]) -> List[str]:
    """Persist the XML next to the other state files; schtasks needs a real path.

    UTF-16 with BOM, matching the encoding declared in the document - schtasks is strict about
    that pair, and a mismatch reads as an unparsable file.
    """
    written = []
    for a in artifacts:
        if a.kind != "task" or not a.content:
            continue
        target = os.path.join(_data_dir(), a.name + ".xml")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-16") as fh:
            fh.write(a.content)
        written.append(target)
    return written