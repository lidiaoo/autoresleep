# AutoReSleep v2 —— 跨平台版（Windows / Linux / macOS）

v1（`PowerShell`，见上级目录的 `00-README-sources.md`）只能跑 Windows，因为它把
「注册计划任务、读 input idle、解析 powercfg」这些**管道**做成了工具本身。
v2 把**判定顺序**（真正的产品）和**平台管道**分开：顺序一行不改地搬过来，管道塞进
`autoresleep/system/`，三套实现背后是同一个接口。

纯标准库，零依赖，不需要 pip / venv / 网络。这是刻意的：这东西要在你半年才碰一次、
可能连 pip 都没有的机器上跑起来。

```
v2/
  autoresleep.py            直接跑：python autoresleep.py doctor（不需要安装）
  pyproject.toml            想装成命令：pip install -e .  → 然后 autoresleep doctor
  src/autoresleep/
    decide.py               判定引擎（纯逻辑，零 I/O，全部单测覆盖）
    pkt.py                  魔法包/标记包：三平台同一套 UDP socket；网卡 MAC 每平台自己枚举
    resume.py               睡眠恢复信号：wall clock 与 monotonic 的差值（三平台通用）
    config.py paths.py      阈值与状态目录（每平台一个去向，单测里跨平台验证）
    journal.py              日志（用标准库轮转，替掉 v1 手写轮转的坑）
    cli.py                  doctor / judge / listen / collect / install / uninstall / config
    system/{base,windows,linux,macos}.py    平台层：idle / suspend / wake-source / guards / firewall
    scheduling/{windows,linux,macos}.py    计划任务产物（文本），按平台生成
  tests/                    74 个单测（下面有跑法）
```

## 1. 三个平台各自怎么实现（同一件事）

| 事项 | Windows | Linux | macOS |
|---|---|---|---|
| 用户空闲 | `GetLastInputInfo`（ctypes） | Mutter IdleMonitor → XScreenSaver → `/dev/input/*` mtime | `ioreg` 的 `HIDIdleTime` |
| 睡眠 | `SetSuspendState(False,True,False)`，失败退 `rundll32` | `systemctl suspend`（先查 `systemd-inhibit`） | `pmset sleepnow`（先看 `pmset -g assertions`） |
| 唤醒源 | Power-Troubleshooter 事件1 + Kernel-Power 107 + `powercfg /devicequery wake_armed` | `journalctl`/`dmesg` 的 `Wakeup source` + `/sys/power/pm_wakeup_irq` | `pmset -g log` 的 `Wake from` + `Reason:` |
| 忙/占用 | `powercfg /requests`（按段落位置解析）+ `qwinsta` | ALSA `/proc/asound/.../status` + `who`/`ss` | `pmset -g assertions` |
| 防火墙 | `netsh advfirewall` 规则 `AutoReSleep-WoL-UDP-*` | `ufw allow ... proto udp`（没有 ufw 就只打印命令） | `socketfilterfw --unblockapp`（按程序，不是按端口） |
| 计划任务 | Task Scheduler XML + `schtasks` | systemd **用户**单元（service+timer，`systemctl --user`） | launchd **LaunchAgent**（`launchctl bootstrap gui/$UID`） |
| 睡眠恢复触发 | 事件触发器（Power-Troubleshooter 1） | 不需要：timer 一个周期内补上 + 守护进程自己发现 | 同 Linux |

两个刻意的设计选择：

1. **恢复信号用时钟差**。v1 要分别接 Windows 事件触发器、Linux `systemd-sleep` 钩子、
   macOS launchd——三套集成、三个失败点，Windows 那个还是最弱环（arm 任务在入睡瞬间被冻结，
   追踪往往在恢复之后才真正开起来）。而「刚睡醒」这件事本身是跨平台的：**墙钟走了几小时、
   monotonic 几乎没动**。任何进程、任何平台、零权限都能看到。于是三套钩子变成
   `resume.py` 里一个检测器，每平台只剩下「怎么睡」和「怎么取证」。
2. **计划任务产物是文本**。所以「Windows 的 judge 真的会在恢复时被触发吗？」这个问题
   可以在 Linux 上被单测回答（`tests/test_scheduling_artifacts.py` 在本机 Windows 上
   同时验证了 Linux 和 macOS 的产物）。

## 2. 谁来定时跑它？（三个平台各自的「计划任务」）

三个岗位在三平台是同一套分工，接线方式不同：

| 岗位 | 什么时候跑 | Windows | Linux | macOS |
|---|---|---|---|---|
| judge（判定） | 每 N 分钟 **＋ 从睡眠恢复时** | Task Scheduler 任务 `\AutoReSleep\AutoReSleep-judge`：事件触发器(Power-Troubleshooter 1) + 日曆触发器带 `PT{N}M` 重复 | `autoresleep-judge.timer`（`OnBootSec=1min` + `OnUnitActiveSec={N}min`）→ 拉起 `.service` | LaunchAgent `com.autoresleep.judge`，`StartInterval={N*60}` |
| listener（常驻抓包） | 登录后一直在 | Task Scheduler `\AutoReSleep\AutoReSleep-listener`：登录触发器 + 失败重启 3 次 | `autoresleep-listener.service`（`WantedBy=default.target`，`Restart=always`） | LaunchAgent `com.autoresleep.listener`（`RunAtLoad` + `KeepAlive`） |
| collector（恢复取证） | 恢复时 | Task Scheduler `\AutoReSleep\AutoReSleep-collector`：事件触发器，以 **SYSTEM** 运行 | **没有独立定时单元**：listener 自己发现恢复后直接调 `collect` | 同 Linux |
| 运行身份 | — | judge/listener 用**登录用户 + 最高权限**（`GetLastInputInfo` 读的是调用者所在会话；SYSTEM/会话0 永远"空闲很久"，会把在用的机器睡掉）；collector 用 SYSTEM | 用户单元（`systemctl --user`），**不需要 root**，登录即有；`loginctl enable-linger $USER` 可让它在你没登录时也在 | LaunchAgent（`launchctl bootstrap gui/$UID`），**不需要 sudo** |

两个与 v1 不同的地方，都是刻意的：

- **Linux/macOS 没有"恢复时触发"这种原生触发器**（那是 Windows 事件触发器独有的）。所以那边
  不硬造一个：listener 用时钟差自己发现恢复并立刻取证，judge 的 timer 在一个周期内补上判定。
- **Windows 侧不再用 PowerShell 注册**（v1 用 `Register-ScheduledTask`）：v2 把任务定义写成
  Task Scheduler XML，用 `schtasks /Create /XML /F` 落地——任务定义因此变成可被单测断言的数据
  （本机 Windows 上就能验证 Linux/macOS 的产物，反之亦然）。任务的**位置**和 v1 一样规整：
  都在任务计划程序库下的一个自己的文件夹里（v1 是 `\Hermes\`，v2 是 `\AutoReSleep\`），
  紧急情况下 `schtasks /Delete /TN "\AutoReSleep\AutoReSleep-judge" /F` 三条就能拆掉。

**提权**：Windows 上注册「最高权限」任务 + 改防火墙都要管理员。v2 不自己弹 UAC，而是
**提前拒绝并打印该执行的完整命令**（`rc=2`，不会改到一半失败留下半个安装）；双击
`install.cmd` / `uninstall.cmd` 会自动提权（内部走 `Start-Process -Verb RunAs`，和 v1 的
`install-tasks.cmd` 同一套路）。Linux/macOS 的用户单元和 LaunchAgent 都不需要提权。

### Windows 任务 XML 的三个坑（都在这台机器上真踩过）

1. **`<Subscription>` 里必须是转义文本，不是子元素**。Task Scheduler 把事件订阅序列化成
   `&lt;QueryList&gt;…&lt;/QueryList&gt;`；直接内联 `<QueryList>` 元素树会被拒，报
   `unexpected node: Subscription` 并只给一个列号——原因不在消息里。
   （权威参照不是文档，而是**让 Task Scheduler 自己导出一份**：注册一个探针任务再 `Export-ScheduledTask`。）
2. **元素顺序被强制检查**，而且不是 SDK 文档里那种顺序。`<Settings>`、`<RegistrationInfo>`
   都是严格序列；顺序错了报的是"无法读取 XML 文件的内容"，同样不说哪个元素错。
   现在的顺序取自 Windows 自己导出/重新导入的那份，并且有单测盯着。
3. **动作不经过 cmd.exe**，所以 `>> file 2>&1` 不是重定向而是多出来的三个 argv；另外
   `-m autoresleep` 只对 pip 安装过的包有效，从源码目录跑必须用 `autoresleep.py` 启动器。

验证方式（不需要管理员）：把生成文档的主体等级降成 `LeastPrivilege` 后让 schtasks 真注册一次，
看它是否接受，再删掉。这条已经做成可选测试：

```bash
AR_LIVE_TASK_TESTS=1 python -m unittest tests.test_scheduling_artifacts -v
#   实测结果：create rc=0 成功创建计划任务 "\AutoReSleep\AutoReSleep-selftest"
```

**不要同时跑 v1 和 v2**：两套都会每 2 分钟判一次，而且监听器的 UDP 端口相同（先绑定的才收得到包）。
先 `uninstall-tasks.cmd` 卸掉 v1，再装 v2。

### 从唤醒到回睡要多久（响应预算）

| 段 | 耗时 | 说明 |
|---|---|---|
| 发现唤醒 | 秒级 | 监听器每秒采样时钟差（三平台通用）；Windows 另有恢复事件触发器 |
| 空闲门槛 | `idle_minutes`（默认 2 分钟） | 唯一硬性的等待：必须确认没人在用。`config --set idle_minutes=1` 可缩短 |
| 等下一次判定 | **≤30 秒** | 监听器快路径（v1 也有）：空闲够了就自己重判，不必等 2 分钟的定时任务 |
| 宽限期 | 0 或 `grace_seconds` | 只对**猜出来**的唤醒（窗口未归因 / 疑似网卡）等 60 秒；证据明确的唤醒不等 |
| 去抖 | 0–5 分钟 | `min_gap_minutes`：距上次强制睡眠不足这个时间就跳过，防止"睡-醒"死循环。**但仅当可能有人在**：自我们上次强制睡眠以来一个输入事件都没有、且唤醒归因给网卡时，改用 `min_gap_unattended_minutes`（默认 1 分钟）——空机器被网卡反复捅醒时，5 分钟的"醒着"不是安全特性，是永远睡不着 |

**实测（本机 2026-09-28 夜）**：判定本身是 0–30 秒级；有两次看起来是 5 分钟，日志里就是
`debounce(278s<300s)`。根因在更下面一层：无线网卡每隔 1 秒–1.4 分钟就把机器捅醒一次
（`link=LINK_LOST`，即 AP 关联丢失）。这属于网卡/AP 层面：
`*WakeOnPattern=0` 与 `*WakeOnMagicPacket=1` 已经是对的，`*DeviceSleepOnDisconnect=1` 与驱动默认
（0）不同、且 Intel 未文档化其语义——是"关联丢失就唤醒"的首要嫌疑，值得改回默认观察一夜。

没有快路径时，延迟 = 空闲门槛 + **最多 2 分钟等定时器** + 宽限期，实测就是 5–8 分钟。
有快路径之后，常见情形是"空闲早够了 → 立刻回睡"，上界 ≈ `idle_minutes` + 30 秒。

## 3. 判定顺序（与 v1 完全一致，这是不能改的部分）

1. **要来的唤醒**（抓到 magic 或 marker 包，且在 `pkt_window_s` 内）→ 永不回睡
2. **看门狗**：入站远程会话 / 全屏占用 / 正在放音频 / Windows Update 在跑 → 不回睡
3. **有人在用**（入睡之后有过输入）→ 不回睡
4. **驱动自己报的唤醒原因**（`AP_ASSOC_LOST` / `ROAM` / `GTK_REKEY` / `4-Way` 等无线专属）
   → **压过操作系统的归因**，按 `idle_minutes` 判断是否回睡
   （有线网卡只会报 media/网线变化，所以这个推断安全；`MEDIA_*` 一律视为有意唤醒）
5. **操作系统归因**：`eth-nic` / `other-device` / `timer` → 有意唤醒，不回睡；
   `fixed-feature-nic-suspect`（Windows 叫不出设备名、没有唤醒定时器、武装设备只有网卡）
   → 默认 `skip`，只有显式 `resleep-if-unattended` 才回睡；`wifi-nic` → 本工具存在的意义
6. **什么都没拿到** → 不猜：要求 `unknown_idle_minutes` 的长空闲 + 无远程会话
7. **去抖**：距上次强制睡眠不足 `min_gap_minutes` → 跳过（对付路由器把机器反复捅醒）

`os_link=LINK_LOST` **永远只是线索**（`link=` 字段），只有 `wake=wifi-nic` 时才升级成
`NON_MAGIC_WLAN_LINK_LOST`。这条是 v1 用一整晚的错误结论换来的：每次恢复都会重新关联/漫游，
所以它同样出现在「确定是被叫醒」的那次上。

## 4. 现状矩阵（哪些真跑过、哪些没有）

| 能力 | Windows | Linux | macOS |
|---|---|---|---|
| 判定引擎（纯逻辑） | ✅ 74 个单测 | ✅ 同样单测 | ✅ 同样单测 |
| 调度产物生成 | ✅ 单测 | ✅ 单测 | ✅ 单测 |
| 空闲探测 | ✅ **真机实测**（本机 6.9 min） | ⚠️ 写了，未在 Linux 上跑 | ⚠️ 写了，未在 macOS 上跑 |
| 网卡枚举 | ✅ **真机实测**（10 张网卡，含虚拟） | ⚠️ 写了，未跑 | ⚠️ 写了，未跑 |
| 唤醒源解析 | ✅ **真机实测**（结果与 v1 当天的判定一致） | ⚠️ 写了，未跑（解析逻辑各发行版差异最大） | ⚠️ 写了，未跑 |
| 睡眠调用 | ⚠️ 未实跑（会真的睡掉这台机器） | ⚠️ 未跑 | ⚠️ 未跑 |
| 计划任务注册 | ⚠️ 只跑过 `install --dry-run` | ⚠️ 未跑 | ⚠️ 未跑 |
| 防火墙规则 | ⚠️ 只打印过计划 | ⚠️ 未跑 | ⚠️ 未跑 |

「未跑」都标在这里而不是含糊过去：本机是 Windows，另外两个平台只能靠
**同一套单测 + `doctor` 的自检输出**来保证接口一致，真正的确认要在目标机上做（第 6 节）。

## 5. 跑起来

```bash
# 不用安装（任何平台，Python 3.9+）
python autoresleep.py doctor          # 先看这台机器支持什么，输出里每一项都带来源
python autoresleep.py judge           # 只判断、写日志（默认不睡！）
python autoresleep.py judge --apply   # 真判断 + 真睡
python autoresleep.py listen          # 常驻：抓包 + 发现恢复
python autoresleep.py collect         # 恢复时取证（调度器会调它）
python autoresleep.py install         # 写配置 + 注册计划任务/单元/LaunchAgent（+ 防火墙）
python autoresleep.py uninstall       # 全部撤销，逐条打印动过什么
python autoresleep.py config --set idle_minutes=4 unknown_idle_minutes=20
```
装成命令：`pip install -e .`（dev 用 `pip install -e ".[dev]"`）。跑测试：
```bash
python -m unittest discover -s tests -v        # 74 tests，无第三方依赖
```

**安全默认与 v1 不同（刻意的）**：`judge` 只判断并写日志，**只有加 `--apply` 才真的睡**。
v1 是「默认干、要加 `-DryRun` 才不干」；v2 会被从没读过脚本的人跑，所以破坏性的那一半
改成显式打开。

## 6. 状态目录与文件（保留 v1 的来源前缀）

| 平台 | 目录 |
|---|---|
| Windows | `%LOCALAPPDATA%\AutoReSleep` |
| Linux | `$XDG_STATE_HOME/autoresleep`，缺省 `~/.local/state/autoresleep` |
| macOS | `~/Library/Application Support/AutoReSleep` |

| 文件 | 来源链 | 内容 |
|---|---|---|
| `decisions.log` | `dec-` | 每次判定一行：`action= reason= src= link= who=` + `idle=` / `pkt=` / `drv=` / `guards=` |
| `heartbeat.txt` | `dec-` | 每行覆盖写：时间 + 判定 + 空闲秒数（看它一眼就知道还活着没） |
| `packets-seen.log` | `pkt-` | 每次抓到：`DETECTED kind=magic|marker src=IP:port len=N` |
| `packet-last.txt` | `pkt-` | 最新证据：`magic|epoch=..|src=..|len=..`（judge 读它） |
| `evidence-last.json` | `drv-`/`os-` | 最近一次恢复的唤醒源：`kind` / `who` / `raw` / `chain`（`chain=drv` 表示驱动自己报的，判定里压过 `os`） |
| `journal.log` | — | 运行日志（标准库轮转，`log_max_bytes`/`log_backups`） |
| `config.json` | — | 全部阈值 |

`raw` 字段永远保留操作系统原话（Windows 的事件字段、Linux 的 dmesg 行、macOS 的 pmset 行）——
归类只是方便，证据本体不加工。

## 7. 在 Linux / macOS 上确认（我需要你在这两台机器上跑）

```bash
# 1) 先看自检：每一项能力、来源、当前空闲、网卡 MAC、防火墙状态都会打印
python autoresleep.py doctor

# 2) 空闲探测必须是一个合理的小数字（而不是 UNAVAILABLE 或 0）
#    Linux：wayland 会话、X11 会话、纯 tty 三种情况都想试一下
#    macOS：应显示 HIDIdleTime (ns -> s)

# 3) 只看判定，不动系统
python autoresleep.py judge

# 4) 注册 + 验证
python autoresleep.py install --dry-run     # 先看会写什么、会跑哪些命令
python autoresleep.py install
#    Linux: systemctl --user list-timers autoresleep-judge.timer
#           systemctl --user status autoresleep-listener.service
#    macOS: launchctl print gui/$(id -u)/com.autoresleep.judge | head -30

# 5) 真发一个魔法包（从另一台机器），看 pkt 链是否抓到
#    Linux:  python autoresleep.py listen   （前台跑，然后发包）
#    macOS:  同上
#    期望：packets-seen.log 出现 DETECTED kind=magic；没出现就看防火墙那一步
```

**防火墙**（三平台都是同一个坑）：监听器把 UDP 9/7/67/68/45678 绑在 Python 进程上，
防火墙开着时入站单播默认被丢弃，`pkt-` 链会**安静地**永远什么都抓不到。
`install` 会处理 Windows（`netsh`）、Linux（有 `ufw` 时自动加，否则只打印该执行的命令）、
macOS（按程序放行解释器）。`--no-firewall` 明确跳过。

## 8. 已知限制（照抄 v1 的诚实清单，加上 v2 新增的）

- 睡眠中唤醒主机的那一个包在网卡固件里就被消费掉了，**任何平台的主机侧抓包都看不到它**；
  `pkt-` 抓到的是回醒之后到达的重复包/标记包。这是硬件限制。
- 驱动是否上报唤醒原因由厂商决定（同一台机器上都会时有时无），所以才有 `os-`/`pkt-` 备用链。
- **绝不在脚本里写死网卡型号**：换卡 / 重装驱动会改掉唤醒源字符串（v1 实测 `AX1650i` →
  `AX210`，接口 `WLAN` → `WLAN 5`，描述还会多一个 ` #2`）。v2 每次都从本机重建名字，
  并且额外把 `WakeSourceText` 里的名字和 `Get-NetAdapter` 的描述一起比对。
- Linux 的唤醒源解析是三个平台里最不统一的（内核版本 / 发行版 / 是否有 journald 都影响输出），
  `raw` 一定留着，分类可能偏保守 → 这正是要靠 `doctor` 在真机上校准的地方。
- 时钟差检测会把「超过 `resume_threshold_s`（默认 90 s）的 NTP 校正」误判成一次恢复；
  比阈值还短的睡眠看不见——tier B 不受影响，因为墙钟空闲判定不依赖它。
- macOS 的应用防火墙是按**程序**放行的，不是按端口，而且只拦未签名的入站连接。
- **`pythonw.exe` + 子进程 = 闪黑窗口**：没有控制台的进程每启动一个控制台程序，Windows 就给它新建一个控制台窗口。
  judge 每次判定都要调 `powercfg`，所以每隔 30 秒会闪一下。v2 给所有 `subprocess` 都加了
  `CREATE_NO_WINDOW`（+ 隐藏的 `STARTUPINFO`），并且有一条源码级单测：出现裸的 `subprocess.run([...])` 就失败。
- **同一台机器上不要同时跑 v1 和 v2 的监听器**：两边的 UDP 端口一样（9/7/67/68/45678），
  Windows 上先绑定的那个才会收到单播包——实测：v2 监听器后来绑定成功、却一个包都没看到，
  同一个包全被 v1 的监听器记下了。想并存就先改其中一边的 `ports` / `marker_port`。

## 9. 与 v1 的对应关系（想对照着看的时候）

| v1 | v2 | 说明 |
|---|---|---|
| `resleep.ps1` | `decide.py` + `cli.py judge` | 判定顺序一字未改；平台调用换到 `system/` |
| `wol-listener.ps1` | `cli.py listen` + `pkt.py` | 抓包 + 恢复检测；`NativeIdle2.dll` 不再需要（ctypes 直连） |
| `wake-reason-capture.ps1` / `wake-reason-arm.ps1` | `cli.py collect` + `system.*.wake_source()` | ETW 追踪那一套只在 Windows 有意义，v2 改用系统各自的唤醒源接口 |
| `wlan-wake-evidence.ps1` | `system.*` 的 guards/link 证据 | `LINK_LOST` 仍然只是线索 |
| `install-tasks.ps1` / `uninstall-tasks.ps1` | `cli.py install` / `uninstall` + `scheduling/` | 三平台同一套命令，产物按平台生成 |
| `run-hidden.vbs` | 不需要 | `pythonw.exe`（Windows）本身就无窗口 |
| `drv-` / `pkt-` / `os-` / `dec-` 前缀 | 原样保留 | `evidence-last.json` 里的 `chain` 就是它 |