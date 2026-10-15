"""Configuration: a YAML file in the platform data dir, with v1's shipped defaults.

Deliberately dependency-free (no pydantic, no PyYAML) - v1's whole value was that it ran on a
machine with nothing installed, and a cross-platform tool that needs a package manager first
is a cross-platform tool nobody installs on the machine they only touch twice a year. YAML is
therefore read and written by `miniyaml`, a small subset that covers this file exactly.

The file is generated with comments (see `to_yaml`) because the knobs that matter here are not
self-explanatory: "guard_display_in_use: false" needs the sentence that says why it ships off.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, List, Optional

from . import miniyaml, paths
from .decide import Params

VALID_POLICIES = ("skip", "resleep-if-unattended")

#: Keys whose value is a list of STRINGS. `ports` is a list of ints, so `config --set` cannot
#: infer the element type from "it is a list" - coercing a NIC name with int() would raise.
#: Kept next to the fields it describes rather than in the CLI.
LIST_OF_STR_KEYS = frozenset(("wake_nics",))

#: The order the generated file is written in: the knobs an operator actually touches first
#: (wake policy, the re-sleep window, the watchdogs), then the internals.
YAML_ORDER = [
    "wake_nics", "magic_only", "resleep_delay_seconds", "resleep_break_on_input",
    "input_settle_seconds",
    "guard_remote_in_use", "guard_display_in_use", "guard_media_playing", "guard_update_running",
    "idle_minutes", "unknown_idle_minutes", "unattributed_wake_policy",
    "repeat_minutes", "min_gap_minutes", "min_gap_unattended_minutes",
    "ports", "marker_port", "pkt_window_s", "resume_threshold_s",
    "fast_path_seconds", "link_sample_seconds", "grace_seconds", "grace_only_for_guesses",
    "arm_idle_seconds", "disarm_idle_seconds",
    "log_max_bytes", "log_backups", "firewall_rule",
]

_YAML_HEADER = """\
AutoReSleep 配置文件（YAML）
改完不用重装：judge 每次运行都会重新读这个文件；`autoresleep config` 会把它打印出来。
最上面这几项是日常要动的，下面的是内部参数，默认值基本不用改。

  wake_nics               要"管教"的网卡：[] = 所有有线网卡 + 所有无线网卡。
                          写 MAC（00:11:22:33:44:55）最稳，写网卡名也行（带空格没关系）。
                          不在这个列表里的网卡把它唤醒 → 当成"你要的唤醒"，不回睡。
  magic_only              true = 只认 WoL 魔法包/标记包，其余唤醒一律按回睡处理。
  resleep_delay_seconds   唤醒后等多久回睡，**从唤醒那一刻起算**（默认 10 秒）。
  resleep_break_on_input  true = 窗口内有人动键盘/鼠标就不回睡；
                          false = 严格模式（明知有人也睡，但判定行会写明 input-ignored-by-strict-mode）。
  input_settle_seconds    唤醒后这几秒内的"输入变化"算机器自己醒来的副作用（HID 回放），不算人。
                          Windows/macOS 没有 evdev，只能看平台输入时钟的差值，而 resume 会让 HID
                          设备回放自己的状态、把时钟推动 —— 实测就是这样一次变化把整个会话锁成
                          "有人动过"。Linux 的 evdev 有设备名与自己的 15 秒 settle，不受这个影响。
                          判定行里的 (N-settled) 就是它吸收掉的次数；吸收不到的（回放来得更晚）
                          可以把这里调大，上限就是 resleep_delay_seconds（等于严格模式）。
  guard_*                 看门狗：命中就认为"有人在用这台机器"，本轮不回睡。可以逐个关掉。"""

_YAML_COMMENTS = {
    "wake_nics": "# [] = 所有有线 + 所有无线；MAC 优先，网卡名兜底",
    "magic_only": "# 只允许 WoL 魔法包唤醒这台机器",
    "resleep_delay_seconds": "# 观察窗口长度，从唤醒时刻算起（秒）",
    "resleep_break_on_input": "# 窗口内有键鼠操作 → 不回睡；false = 严格模式",
    "input_settle_seconds": ("# 唤醒后这几秒内的输入变化算 resume 的 HID 回放（不算人）；\n"
                             "# Linux 走 evdev，本项只影响 Windows/macOS"),
    "guard_remote_in_use": "# 远程会话正在使用 → 不回睡（默认开）",
    "guard_display_in_use": ("# 【默认关】远控/串流工具（实测 CrossDesk.exe）会一直占着\n"
                             "# 显示器电源请求，开着它会导致永远不回睡；要旧行为填 true"),
    "guard_media_playing": "# 有媒体在播放 → 不回睡（Windows 上暂无法归因，见 README）",
    "guard_update_running": "# Windows Update 在运行时 → 不回睡",
    "idle_minutes": "# 不再决定回睡等待（那是 resleep_delay_seconds）；仍用于'确定没人'的门槛",
    "unknown_idle_minutes": "# 归因不明时要求的长空闲",
    "unattributed_wake_policy": "# 'skip' | 'resleep-if-unattended'",
    "repeat_minutes": "# judge 多久跑一次（分钟）",
    "min_gap_minutes": "# 去抖：两次强制回睡之间至少隔这么久",
    "min_gap_unattended_minutes": "# ……但当确定没人在时，放松到这个值",
    "ports": "# 监听的 UDP 端口（9/7 = 魔法包）",
    "marker_port": "# 标记包端口：无特权时靠它分辨'有人叫醒'",
    "pkt_window_s": "# 多新的包才算'这次唤醒的包'",
    "resume_threshold_s": "# 墙钟与单调钟差多少算'睡过一觉'",
    "fast_path_seconds": "# 监听器重新判定的间隔（加快唤醒后的反应）",
    "link_sample_seconds": "# 监听器采样链路的间隔（不依赖驱动）",
    "grace_seconds": "# 回睡前最后一瞥的空闲门槛",
    "grace_only_for_guesses": "# 只对'猜出来的'唤醒做宽限",
    "arm_idle_seconds": "# 空闲到这个值就启用唤醒原因追踪",
    "disarm_idle_seconds": "# 空闲掉回这个值就停用",
    "log_max_bytes": "# 单个日志文件上限",
    "log_backups": "# 日志轮转保留份数",
    "firewall_rule": "# install 时打开监听端口的防火墙规则",
}

#: A MAC however a human writes it: 00:11:22:33:44:55, 00-11-22-33-44-55, 0011.2233.4455,
#: 001122334455. Adapter NAMES are never matched here - they are whatever the platform calls them.
_MAC_RE = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$"
                     r"|^[0-9a-f]{12}$"
                     r"|^(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}$", re.I)


def normalize_mac(text: str) -> Optional[str]:
    """'00-11-22-33-44-55' -> '00:11:22:33:44:55'; None when it is not a MAC at all.

    One canonical form, because the same card is written differently by every platform tool
    (netsh lowercases with dashes, `ip link` colon-separates, ifconfig does not pad) and a
    comparison that silently misses is the failure this project keeps guarding against.
    """
    raw = str(text).strip()
    if not _MAC_RE.match(raw):
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    if len(digits) != 12:
        return None
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


@dataclass
class Config:
    #: DEPRECATED as a *wait*: the re-sleep wait is now `resleep_delay_seconds`, measured from the
    #: wake moment (docs/resleep-window-plan.md). The field stays because old config files, the
    #: pre-suspend grace check and several tests still speak of it - but it no longer decides how
    #: long a NIC-caused wake waits before it is slept again.
    idle_minutes: float = 2.0
    unknown_idle_minutes: float = 15.0      # unattributable wake: demand a long idle
    unattributed_wake_policy: str = "skip"  # 'skip' | 'resleep-if-unattended'
    repeat_minutes: int = 2                 # how often the judge runs
    min_gap_minutes: float = 5.0            # debounce between two forced sleeps ...
    min_gap_unattended_minutes: float = 1.0  # ... but only when a human might be there
    pkt_window_s: float = 180.0             # how recent a packet must be to count
    resume_threshold_s: float = 90.0        # wall/monotonic skew that means "we slept"
    fast_path_seconds: int = 30             # listener re-judges this often once idle >= threshold
    link_sample_seconds: int = 15            # listener samples the link this often (driver-free)
    grace_seconds: int = 60                 # last look before suspending (v1's tier-A grace)
    grace_only_for_guesses: bool = True     # grace protects guesses, not evidence-backed wakes
    marker_port: int = 45678
    ports: List[int] = field(default_factory=lambda: [9, 7, 67, 68])
    arm_idle_seconds: int = 45              # arm the wake-reason trace once this idle
    disarm_idle_seconds: int = 20           # disarm it when the user is definitely back
    log_max_bytes: int = 512 * 1024
    log_backups: int = 3
    firewall_rule: bool = True              # open the listener's UDP ports at install
    # --- the re-sleep window (docs/resleep-window-plan.md) ---------------------------------
    wake_nics: List[str] = field(default_factory=list)  # [] = every wired + every wireless NIC
    magic_only: bool = False                # only a WoL magic packet may wake this machine
    resleep_delay_seconds: float = 10.0     # observation window, measured from the WAKE moment
    resleep_break_on_input: bool = True     # hands on the machine inside the window cancel it
    #: Input-clock changes within this many seconds of the wake are the resume's own HID replay,
    #: not a person (`windowwatch`). It is the portable twin of the 15-second motion settle window
    #: Linux has had all along (`inputwatch.MOTION_SETTLE_S`), applied to the one signal Windows and
    #: macOS have left - the platform input clock's delta. Measured on this box (2026-10-15): the
    #: replay moved the clock 604.6 s at wake+1.6 s, and that single change latched the whole
    #: session as "wake-by-input" so the machine never re-slept. Anything WAITING for a hand is
    #: deliberately NOT changed by this: the window still lasts `resleep_delay_seconds`, only the
    #: "whose change was it" question moves. Raise it towards `resleep_delay_seconds` if a box
    #: replays later than this one (the `(N-settled)` token in the decision line is how you tell).
    input_settle_seconds: float = 5.0
    # --- the watchdogs, switchable (docs/resleep-window-plan.md) ---------------------------
    #: These four say "somebody/something is clearly using this machine, do not sleep it".
    #: `guard_display_in_use` ships OFF: a remote-desktop / game-streaming tool holds a DISPLAY
    #: power request for as long as it runs (measured here: CrossDesk.exe, plus GameViewer's audio
    #: stream), so the guard fired on every single judgement and silently disabled every re-sleep -
    #: which is exactly what the operator reported. A genuine remote session is still covered by
    #: `guard_remote_in_use`, so turning the display one off does not mean "sleep while somebody is
    #: watching".
    guard_remote_in_use: bool = True
    guard_display_in_use: bool = False
    guard_media_playing: bool = True
    guard_update_running: bool = True
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ validation
    def validate(self) -> "Config":
        if self.unattributed_wake_policy not in VALID_POLICIES:
            raise ValueError("unattributed_wake_policy must be one of %r" % (VALID_POLICIES,))
        for name in ("idle_minutes", "unknown_idle_minutes", "min_gap_minutes",
                     "pkt_window_s", "resume_threshold_s", "resleep_delay_seconds",
                     "input_settle_seconds"):
            if float(getattr(self, name)) < 0:
                raise ValueError("%s must not be negative" % name)
        if int(self.repeat_minutes) <= 0:
            raise ValueError("repeat_minutes must be positive")
        if not self.ports or any(int(p) <= 0 or int(p) > 65535 for p in self.ports):
            raise ValueError("ports must be valid UDP port numbers")
        if not 0 < int(self.marker_port) <= 65535:
            raise ValueError("marker_port must be a valid UDP port number")
        # A typo in wake_nics must be loud: an entry that matches no adapter is a NIC nobody is
        # policing, and the failure mode is a machine that keeps waking itself (plan section 4.4).
        if not isinstance(self.wake_nics, (list, tuple)):
            raise ValueError("wake_nics must be a list")
        cleaned: List[str] = []
        for item in self.wake_nics:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("wake_nics entries must be non-empty strings "
                                 "(a MAC or an adapter name)")
            cleaned.append(normalize_mac(item) or item.strip())
        self.wake_nics = cleaned
        # Coerced rather than rejected: a hand-edited config.json that says 1 instead of true must
        # not crash a judge whose only job is to decide whether to sleep the machine.
        self.magic_only = bool(self.magic_only)
        self.resleep_break_on_input = bool(self.resleep_break_on_input)
        for name in ("guard_remote_in_use", "guard_display_in_use",
                     "guard_media_playing", "guard_update_running"):
            setattr(self, name, bool(getattr(self, name)))
        return self

    # ------------------------------------------------------------------ i/o
    def to_yaml(self, header: str = "") -> str:
        """The file as it is written - also what `autoresleep config` prints.

        `extra` is dropped on purpose: it is the catch-all for keys a future version might add, and
        writing it back would cement somebody else's typo into the operator's file.
        """
        data = {k: v for k, v in asdict(self).items() if k != "extra"}
        return miniyaml.dump(data, header=header, comments=_YAML_COMMENTS, order=YAML_ORDER)

    def save(self, path: Optional[Path] = None) -> Path:
        self.validate()
        target = Path(path) if path else paths.config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(self.to_yaml(_YAML_HEADER), encoding="utf-8")
        # atomic replace: a judge run must never read a half-written config
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        target = Path(path) if path else paths.config_file()
        raw = _read_mapping(target)
        if raw is None and path is None:
            # a machine installed before the config became YAML: read its JSON once, so nobody's
            # settings silently revert to defaults (install rewrites it as YAML, see migrate_legacy)
            raw = _read_mapping(paths.legacy_config_file())
        if raw is None:
            return cls()
        known = {f.name for f in fields(cls)}
        # unknown keys are ignored on purpose: a v1 or future-version file must not crash
        # a judge that is only trying to decide whether to sleep the machine
        data: dict[str, Any] = {k: v for k, v in raw.items() if k in known}
        return cls(**data)

    @classmethod
    def migrate_legacy(cls) -> Optional[Path]:
        """Write an old `config.json` out as `config.yaml`, once. Returns the path, or None.

        The JSON is RENAMED (`config.json.migrated`), not deleted: nothing the operator set is lost,
        and a file that is no longer read is visibly set aside instead of lurking next to the one
        that is - the same class of confusion as a stale lock file.
        """
        legacy = paths.legacy_config_file()
        if not legacy.exists():
            return None
        written = cls.load(legacy).save(paths.config_file())
        try:
            legacy.replace(legacy.with_suffix(".json.migrated"))
        except Exception:
            pass
        return written

    # ------------------------------------------------------------------ bridge to the engine
    def params(self) -> Params:
        return Params(pkt_window_s=float(self.pkt_window_s),
                      idle_minutes=float(self.idle_minutes),
                      unknown_idle_minutes=float(self.unknown_idle_minutes),
                      min_gap_minutes=float(self.min_gap_minutes),
                      min_gap_unattended_minutes=float(self.min_gap_unattended_minutes),
                      unattributed_wake_policy=self.unattributed_wake_policy)


def _read_mapping(target: Path) -> Optional[dict]:
    """Read the config file as a mapping - YAML, or the JSON this tool used to write.

    Returns None when there is nothing readable, which the caller treats as "use the defaults". The
    suffix decides the format, not a sniffed byte: guessing wrong on a JSON file (which starts with
    `{`) would hand back a single nonsense key and silently reset everything.
    """
    if not target.exists():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except Exception:
        return None
    if target.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except Exception:
            return None
        return data if isinstance(data, dict) else None
    data = miniyaml.load(text)
    return data or None