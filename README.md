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
  （本机 Windows 上就能验证 Linux/macOS 的产物，反之亦然）。**重装是「先删再建」**：注册前先
  `schtasks /Delete /TN <名字> /F`（best-effort，第一次装时本来就没有），因为就地覆盖会留下旧主体、
  旧触发器、旧 ACL —— 同一类坑在另外两个平台也存在（`launchctl bootstrap` 对已加载的 label 直接失败、
  `systemctl enable --now` 不会重启已在跑的单元），所以三个平台的产物都带一个 rebuild 步骤。
  任务的**位置**和 v1 一样规整：
  都在任务计划程序库下的一个自己的文件夹里（v1 是 `\Hermes\`，v2 是 `\AutoReSleep\`），
  紧急情况下 `schtasks /Delete /TN "\AutoReSleep\AutoReSleep-judge" /F` 三条就能拆掉。
- **重启监听器要借提权的那一手**（Windows 实测）：任务带 `<RunLevel>HighestAvailable</RunLevel>`，
  监听器进程跑在**高完整性级别**，普通用户的 `taskkill /PID n /F` 只会得到
  「错误: 无法终止 PID 为 n 的进程。原因: 拒绝访问。」——而旧代码把 taskkill 的返回码丢掉、
  无条件打印「已停止上一个监听器」，于是**孤儿监听器越攒越多**（本机实测：三个进程同时绑着
  UDP 9/7/67/68，而 lock 文件指向的 pid 早已退出）。现在 install/uninstall 把「还占着 WoL 端口的
  python 进程」的 pid 交给**提权子进程**去杀（`--stop-pids`，答案走参数），子进程杀掉后
  在进度文件里逐条记录。
- **绑端口要独占，不能 `SO_REUSEADDR`**（Windows 语义和 Unix 相反）：`SO_REUSEADDR` 在 Windows 上
  是「允许别人也绑同一个地址」，绑成功之后到底哪个 socket 收到数据包不由我们决定——上面那三个
  进程因此都能绑上，包链静默半死。现在 `pkt._exclusive()` 用 `SO_EXCLUSIVEADDRUSE`（POSIX 侧
  就是不设 reuse）：第二个进程会**绑失败**，`unavailable()`/`advice()` 把原因讲给操作者。
  install 随后还会**问 OS**「9/7/45678 现在到底归谁」（`netstat -ano -p UDP` / `ss -lunp`），
  只要不是刚起来的那个 pid 就报 `[failed]` 并以非 0 退出——lock 文件只能证明「有个监听器活着」，
  证明不了它是哪一个。

- **常驻监听器不能有 ExecutionTimeLimit**（Windows 实测）：任务 XML 默认写了
  `<ExecutionTimeLimit>PT10M</ExecutionTimeLimit>`，Task Scheduler 会在**启动满 10 分钟时把任务连同
  进程一起终止**（实测 `上次结果: 267014` = `0x41306`「任务被终止」；pid 31824 于 08:53:12 启动、
  最后一次心跳 09:02:49）。症状是最阴的那种：装完/登录后十分钟，包链**静默**死掉，`doctor` 反而
  能绑上 9/7/45678（因为已经没人在看了），直到下次登录才复活。现在监听器写 `PT0S`（Task Scheduler
  的「无限制」），judge/collector 这种一次性任务保留上限当跑飞的保险。

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
| 去抖 | 0–5 分钟 | `min_gap_minutes`：距上次强制睡眠不足这个时间就跳过，防止"睡-醒"死循环。**但仅当可能有人在**：自我们上次强制睡眠以来一个输入事件都没有、且唤醒来自网络硬件（网卡归因、或第 4 条链判定链路掉过线）时，改用 `min_gap_unattended_minutes`（默认 1 分钟）——空机器被网卡反复捅醒时，5 分钟的"醒着"不是安全特性，是永远睡不着 |

**实测（本机 2026-09-28 夜）**：判定本身是 0–30 秒级；有两次看起来是 5 分钟，日志里就是
`debounce(278s<300s)`。根因在更下面一层：无线网卡每隔 1 秒–1.4 分钟就把机器捅醒一次
（`link=LINK_LOST`，即 AP 关联丢失）。这属于网卡/AP 层面：
`*WakeOnPattern=0` 与 `*WakeOnMagicPacket=1` 已经是对的，`*DeviceSleepOnDisconnect=1` 与驱动默认
（0）不同、且 Intel 未文档化其语义——是"关联丢失就唤醒"的首要嫌疑，值得改回默认观察一夜。

没有快路径时，延迟 = 空闲门槛 + **最多 2 分钟等定时器** + 宽限期，实测就是 5–8 分钟。
有快路径之后，常见情形是"空闲早够了 → 立刻回睡"，上界 ≈ `idle_minutes` + 30 秒。

**实测（本机 2026-09-29 晨，Linux，驱动无关的三条链各跑通一次）**：

| 判定时刻 | 判决 | 依据 | 内核对应记录 |
|---|---|---|---|
| 08:19:13 | `sleep` `NON_MAGIC_WLAN_LINK_MOVED` | 快照 BSSID 变了，`idle=538s` | `PM: suspend entry (deep)` 08:19:13 |
| 08:24:15 | `sleep` `NON_MAGIC_INPUT_WAKE` | 输入时钟落在唤醒时刻，`idle=210s` | `PM: suspend entry (deep)` 08:24:15 — **旧行为**，现已改：键鼠唤醒豁免整个会话 |
| 08:32:09 | `sleep` `NON_MAGIC_WLAN_LINK_MOVED` | `carrier-changed-2x-since-sleep`（**BSSID 没变**），`idle=279s`，监听器快路径 | `PM: suspend entry (deep)` 08:32:09 |
| 08:56:50 | `skip` `wake-requested` | 本机自发自收的 marker 包（`pkt=marker`），**当时 `link=LINK_LOST` 也被压住** | —（该睡的时候不睡，所以没有记录） |

三次决定的 `pkt=- drv=- guards=-`：**没有包、没有驱动信息、没有看门狗**，全靠可观测事实。
另外这台机器的无线确实是坏的：`carrier_changes` 在 30 分钟里从 10 涨到 14（掉了 4 次），
而 `iw dev wlan0 link` 每问一次都显示"已连接"——瞬时状态看不出掉过线，这就是第 4 条链要用
计数器而不是状态的原因。

## 3. 判定顺序：驱动无关（不靠网卡驱动解释它自己）

1. **要来的唤醒**（抓到 magic 或 marker 包）→ 永不回睡，**整个唤醒会话都不进睡眠逻辑**
2. **看门狗**：入站远程会话 / 显示器电源请求 / 正在放音频 / Windows Update 在跑 → 不回睡。
   四个可以逐个关掉（`guard_remote_in_use` / `guard_display_in_use` / `guard_media_playing` /
   `guard_update_running`），**`guard_display_in_use` 默认关**：远控/串流工具会一直占着显示器
   电源请求（实测 `CrossDesk.exe`），开着它等于永远不回睡；真有人在远端操作时仍由
   `guard_remote_in_use` 兜住。关掉的看门狗不会消失：判定行写 `guards=ignored:display-in-use[CrossDesk.exe]`。
   命中时判定行也会点名持有者（`guards=display-in-use[CrossDesk.exe]`），不再让人去猜
3. **键鼠/USB 唤醒**：两个时钟回答同一个问题——**硬件输入时钟**（evdev 真实事件落在唤醒时刻）
   或**窗口内**（唤醒后 `resleep_delay_seconds` 之内）出现过输入 → 整个会话不回睡。窗口那一侧
   只看**差值**（平台时钟隔一会儿采样两次，变了就是有人），绝不用绝对读数：桌面空闲计数会被
   resume 清零，那正是「每次唤醒都像按了键」的根因
4. **网络在睡眠期间动过**（四路独立证据，任一命中即定罪）→ **等窗口过完**再回睡：
   **`iw event` 记录的 nl80211 事件（主证据）**、睡前快照对比（关联丢了 / BSSID 变了 /
   内核 `carrier_changes` 涨了 / 系统日志里睡后有重新关联）
5. **驱动自己报的唤醒原因**：**只记录，绝不判决**。它是每张网卡各自的私有字符串（有的驱动根本
   不打，字段名还各不相同），拿它睡觉＝拿厂商的措辞睡觉。代码里这条链已经没有 `return` 了
6. **操作系统归因**：`wake_nics` 里的网卡 → 回睡候选（**有线与无线同等对待**，`eth-nic` 也一样）；
   **不在列表里的网卡** → 有意唤醒，不回睡；`timer` / `other-device` / `fixed-feature` → 有意唤醒；
   `fixed-feature-nic-suspect`（Windows 叫不出设备名、没有唤醒定时器、武装设备只有网卡）
   → 默认 `skip`，只有显式 `resleep-if-unattended` 或 `magic_only` 才回睡
7. **什么都没拿到** → 不猜：要求 `unknown_idle_minutes` 的长空闲 + 无远程会话
8. **去抖**：距上次强制睡眠不足 `min_gap_minutes` → 跳过（对付路由器把机器反复捅醒）；
   但若能确认「自我们上次强制睡眠以来一个输入都没有」而且唤醒来自网络硬件（第 4 条链），
   改用 `min_gap_unattended_minutes`（默认 1 分钟）

**窗口就是等待（第 4、6 条链）**：等待时长是 `resleep_delay_seconds`（默认 10 秒，从唤醒时刻
算起），不再是 2 分钟空闲。**窗口一到期就在监听器进程内立刻判一次**（不等 `fast_path_seconds`
那个 30 秒节拍）——否则每个网卡唤醒的回睡决定会被硬拖成"窗口 10 秒 + 最多 30 秒"；
同一个窗口只判一次（窗口状态留在磁盘上，`age >= delay` 会永远为真，不去重就会变成每秒判一次）。「确认没人在用」这个门槛仍然在，只是判据换成了「窗口内零输入事件」。
`idle_minutes` 只在两种情况下回来兜底：证据没有会话戳（采集器从没跑过），或机器在唤醒之后又被人
用过（那说明窗口的结论已经过期，不能拿它授权睡一台正在被用的机器）。配置见第 6b 节。

### 三个监听器：唤醒原因由它们判，不靠驱动

| 监听器 | 跑在哪 | 需要什么 | 判什么 | 记录 |
|---|---|---|---|---|
| **nl80211 事件** (`iw event -t`) | 特权传感器（长驻子进程） | root 读 nl80211 | wifi 断连/漫游/重连唤醒 | `wlan-events.log` |
| **魔法包** UDP 9/7 | 特权传感器 | `CAP_NET_BIND_SERVICE` | 魔法包唤醒 | `packet-last.txt` |
| **魔法包裸帧** ethertype `0x0842` | 特权传感器 | `CAP_NET_RAW` | `etherwake` 类发送端（**不发 UDP**） | 同上 |
| **硬件输入** `/dev/input/event*` | 特权传感器（线程） | `SupplementaryGroups=input` | 键鼠/USB 唤醒 | `input-last.json` |

判定器只读这些文件，进程间零耦合、不需要 sudo 进程、不碰任何驱动字段。传感器的权限**正好三个**：
`CAP_NET_BIND_SERVICE` + `CAP_NET_RAW` + `input` 组，进程身份仍是登录用户（`Uid=1000`）。

**为什么不用 `tcpdump`**：它是通用抓包器，常驻一个 root tcpdump 只为等 WoL 帧太重。`AF_PACKET`
绑到 ethertype `0x0842` 时**内核只把 WoL 帧送上来**，等价但几乎零开销，而且我们的 socket 在挂起
期间是冻结而不是关闭——机器醒来时它还在，比"挂起前起一个 tcpdump"看得更全。
`iw event` 那条也一样：不用脚本轮询 `sudo iw`，而是让单元一次性拿到权限、长驻听 nl80211。

**为什么不用 X11 空闲时钟判键鼠**（这是真实踩过的坑）：resume 会把 X 的空闲计数**重置为 0**，
于是**每一次**唤醒都显示 `idle=0s`、被判成"键鼠唤醒"，再被会话锁存锁住 → 一台被 wifi 断连唤醒的
机器整天不再回睡（实测某天 41 条判定里 34 条是 `wake-by-input idle=0s`）。现在只有 evdev 硬件事件
能判键鼠，且**只有它允许创建会话豁免**；X11 兜底只能"跳过当次"，永不落锁存。

### 为什么豁免要「锁存整个会话」，而不是一个时间窗口

包和输入都是**唤醒那一刻附近**的事实，只能在窗口内认定；但结论（"这台机器是被人叫醒的，别睡"）
必须活到会话结束。第一版只做了窗口，实测后果就是你会在日志里看到的：

```
09:51:53  skip   wake-by-input          idle=50s     ← 人按了下键盘
09:53:04  SLEEP  NON_MAGIC_INPUT_WAKE   idle=121s    ← 两分钟后（idle 过 120s），人还在，机器睡了
          PM: suspend entry (deep) 09:53:04
```

魔法包那边同样：`pkt_window_s=180` 一过，证据过期 → 回睡。

所以现在**认定照旧在窗口内做，认定结果锁存**到 `session-cause.json`：

- 听到包的那一刻就写（监听器/特权传感器），不用等下一次判定；
- 判定器也会写（键鼠唤醒、以及没赶上即时写入的情形）；
- 记录里带**会话标识**，就是恢复时 `evidence-last.json` 里写下的 `session` 时间戳。下一次恢复会
  写新的标识，旧锁存自动不再匹配——**不需要任何进程记得去清它**，也不会跨会话泄漏。
- 判定器因为要补链路探测而重写证据时会保持这个标识（换了就等于换了会话，锁存会无声失效）。

判定里两条豁免都因此变成"直接 return skip"，完全不进入空闲门槛、去抖等睡眠逻辑；
而**无线网卡自己引起的唤醒（链路掉线/重连，第 4 条链）依旧按 `idle_minutes` 回睡**——
锁存只对包和输入生效，且只对它自己那个会话生效。想验证：

```
实测（本机 2026-09-29 10:2x，同一份真证据 + idle=600s）：
  魔法包唤醒，一小时后（包里已过期）      skip   wake-requested     who=asked-for-this-session
  键鼠唤醒，人已走开一小时              skip   wake-by-input       who=input-this-session
  包和唤醒同时发生（刚听到）             skip   wake-requested     who=magic@+2s
  无线网卡掉线/重连引起（无锁存）         sleep  NON_MAGIC_WLAN_LINK_MOVED
  上一会话的锁存（不该生效）             sleep  NON_MAGIC_WLAN_LINK_MOVED
```

一个例外要知道：如果**机器本来就醒着**，这时别人发来一个 magic 包，它不算"这次唤醒的原因"
（包龄 3 秒 vs 会话龄 1 小时，差得太远），第 1 条链不会命中；但监听器在听到包时仍会锁存——
即"有人明确在叫这台机器保持醒着"。

### 为什么第 3、4 条不需要驱动

驱动给的唤醒原因（Windows `WakeSourceText`、Linux dmesg 里驱动打的字符串）在三个平台上都不保证
存在：被固定特征网卡叫醒后，驱动根本不说谁捅的。所以判定只用**操作系统自己就会发布的事实**：

| 要判断 | 可观测事实 | Windows | Linux | macOS |
|---|---|---|---|---|
| 键鼠唤醒 | 输入时钟：最近一次输入是否落在唤醒时刻 | `GetLastInputInfo` | XScreenSaver / `/dev/input` 时间戳 | `ioreg -c IOHIDSystem` 的 `HIDIdleTime` |
| 唤醒包唤醒 | 监听器自己收到的 UDP | 同左 | 同左 | 同左 |
| WiFi 断开/重连 | 睡前快照 vs 现在的关联状态 | `netsh wlan show interfaces` + WLAN-AutoConfig 事件 | `iw dev <if> link` + `/proc/net/wireless` + `carrier_changes` | `airport -I` |

Linux 的「正在放音频」看门狗问的是**声音服务器**（`pactl`，pipewire/pulse 都答），不是
`/proc/asound` 里的 `state: RUNNING`：PipeWire 会让模拟输出口一直空转（`hw_ptr` 在推进、
一个流都没有），只看内核那一行的机器会永远被判「在放音」而永不回睡（本机实测就是这样，
`pactl list sinks` 明明 `State: IDLE`）。没有声音服务器可问时（纯 tty + `aplay` 那种）才退回
内核读法——那时 `RUNNING` 确实代表真有客户端在放音。

`/sys/class/net/<if>/carrier_changes` 是纯内核计数，**同一个 AP 的重连也看得出来**：SSID 没变、
BSSID 没变，但链路层确实掉过。这是只看瞬时状态永远看不到的那类证据。

所以 `link=` 不再是「线索」，而是第 4 条链的结论本体：睡前快照写在 `link-presleep.json`
（判定器与监听器快路径在每次挂起前各写一次），监听器另外每秒看一眼链路，把变化追加到
`link-events.log`。v1 的教训仍然成立——「恢复了就一定会重新关联，所以 `LINK_LOST` 单独不能
定罪要来的唤醒」——但这条现在由第 1 条链（包证据）结构性保证：magic/marker 包在链路链之前
就 return 了，链路推断永远没有机会覆盖它。

## 3b. 恢复检测与锁存失效（短睡眠必须看得见）

两个时钟，一个精确、一个兜底：

- **精确：挂起账本**。`CLOCK_BOOTTIME`（含挂起）减 `CLOCK_MONOTONIC`（不含挂起）就是这台机器
  累计睡了多少秒，精确到毫秒、单调递增、无需阈值。**任何睡眠都逃不掉**，1 秒也算。Windows 用
  `GetTickCount64` 减 `QueryUnbiasedInterruptTime` 的同类差值。
- **兜底：墙上时钟偏差**。原判据（wall 与 monotonic 的偏差 > `resume_threshold_s=90`）**看不见
  短睡眠**：实测 `10:50:06` 挂起、`10:50:23` 恢复（**17 秒**）没有被识别成恢复，采集器没跑，
  会话戳没动，一个旧的"键鼠唤醒"锁存继续命中，机器 2 分钟后没有回睡。只在拿不到账本时用它。

`CLOCK_BOOTTIME` 与 `CLOCK_MONOTONIC` 是两次独立读取，中间可能跨一个时钟 tick，所以差值会抖动
（`LEDGER_EPSILON_S = 0.5`）。实测这个抖动曾在机器完全清醒时"检测到 4 次恢复"，每次都重写证据、
作废一个合法锁存——所以必须有这个容差。

**锁存失效有两条独立路径**，都必须成立：

1. `session` 戳不同（采集器写了新的一次恢复）；
2. `slept_total` 不同（**机器自己睡过了**，无论有没有进程注意到）。

只有第 1 条时，一次没人察觉的短睡眠会让旧锁存永久粘住。第 2 条让锁存由机器自己的历史失效。
判定器自己也会自愈：发现账本动了但证据还是旧的，就补一次采集（换新会话戳）。

## 3c. 哪些"输入"不算人（ACPI 会自己按键）

实测：`rtcwake -m mem -s 20`——纯 RTC 唤醒、屋里没人——**`Power Button` 设备在恢复前 2 秒发出了
`KEY_POWER`**；输入时钟把它当成"有人按了键"，于是写下 `input` 锁存，一次 WiFi 断连唤醒因此被
豁免回睡。所以：

- 设备黑名单（`DENY`）扩到 ACPI/控制类设备：`power button`、`sleep button`、`lid switch`、
  `wmi hotkeys`、`acpi`、`video bus`、`pc speaker`、`rfkill`；
- 键码过滤（`SYSTEM_KEY_CODES`）：`KEY_POWER(116)`、`KEY_SLEEP(142)`、`KEY_WAKEUP(143)`、
  `KEY_SUSPEND(205)` 无论来自哪个设备都不算人类输入。

## 3f. 洪泛设备不能拖垮机器（CPU 100% / 磁盘写爆 / 输入卡死）

实测：鼠标在恢复后会**持续**上报（`Logitech G502 HERO`，每秒上千个 `value=±1` 的事件）。第一版
监听器为此付出了三样代价，而且它们是**同一个根因**：

1. **CPU 100%**：`select` 在有数据待读时**立即返回**，而每次只读 64 个事件——只要设备还在洪泛，
   循环就以极限速度空转。
2. **每事件两次文件操作**：`_audit()` 每个事件 `open/write/close` 一次；`_write()` 每个事件重写
   `input-last.json`（读旧文件 + 写临时文件 + `rename`）。每秒上千事件 = 每秒数千次文件系统调用。
3. **磁盘被写爆**：审计日志每事件一行约 80 字节，约 80 KB/s ≈ **6.9 GB/天**（实测几小时就到了
   71 MB / 91 万行）。`/home` 写满后写入开始失败——这就是"磁盘不可访问"；同时 IO 饱和会让 USB
   输入路径饿死，手感上就是"键盘鼠标供电断了"。

修法：

- **审计聚合**：按 `(设备, kind, note)` 计数，每 `AUDIT_FLUSH_S`（0.5 秒）最多写一次，一行里带
  `n=` 与各 `code` 的计数；**同一个洪泛设备只花两行/秒，而不是上千行**。
- **审计轮转**：超过 `AUDIT_MAX_BYTES`（2 MB）就滚成 `.1`，只留一代。
- **状态文件节流**：`input-last.json` 只记录**最后一次**事件，所以按 `STATE_WRITE_S`（1 秒）写一次，
  并把内容缓存在内存里（不再每次写前重读文件）；流一安静就立刻落盘，文件永远不会"旧"。
- **洪泛暂停**：读到**满缓冲**说明还有数据在等，此时 `sleep(FLOOD_PAUSE_S)`（2 ms），把循环速率
  限住，而不是让它占满一个核。

实测（管道灌真实 event 结构体）：**30,394 事件/秒**（比真实鼠标重 30 倍）持续 10 秒，watcher 自身
CPU **3.4% 单核**，审计日志 **21 行**——旧实现会是 30 万行、每秒数千次文件操作。

## 3e. 唤醒源 ≠ 唤醒原因（恢复时 USB 会自导自演）

实测（内核日志，三次唤醒都一样）：

```
xhci_hcd 0000:02:00.0: xHC error in resume, USBSTS 0x401, Reinit
usb usb1: root hub lost power or was reset
usb 1-6: reset full-speed USB device number 3 using xhci_hcd
```

每次恢复内核都会**复位整条 USB 总线**，HID 设备重新初始化时**回放自身状态**：屋里没人、纯 RTC 唤醒
（`rtcwake -m mem -s 20`）的情况下，鼠标在恢复后 +7 秒爆发 **80 个 `value=-1` 的 REL 事件**、+11 秒
13 个、+12 秒 16 个，滚轮码 `8/11` 交替——这不是手。把它们算成人类输入，就会给整个会话打上
`input` 豁免锁存，机器再也不会回睡（就是 `12:12:09 action=skip reason=wake-by-input` 那条）。

修法：

- 恢复后 **`MOTION_SETTLE_S`（15 秒，实测最长回放）内忽略运动事件**；按键事件**永不**受此窗口影响
  ——唤醒瞬间真按键正是豁免存在的意义，而且超过窗口后仍在动的手照样算数；
- 每个被接受或丢弃的输入事件都写 `input-events.log`（设备、kind、code、value，丢弃的标
  `settling:ignored`），判定行点名设备与时间：`via=Logitech G502 HERO Gaming Mouse@41s-before`；
- **不要把过期的包印成 `pkt=magic`**：40 分钟前的魔法包曾出现在每条判定行上，读起来像"这是一次被
  请求的唤醒"，反而盖住了真正的判定依据。现在只在 `pkt_window_s` 内才显示包，否则显示年龄与
  `(203m-old:ignored)`。

## 3d. `iw event` 的两个坑（都踩过）

1. **`readline()` 会阻塞传感器主线程**。`iw event` 在链路没动时**一个字都不打印**，而管道写入端
   沉默时 `readline()` 永久阻塞。实测用 `/proc/<pid>/stack` 抓到主线程停在 `anon_pipe_read`——
   于是主循环不再轮询 UDP 9/7 与裸帧 `0x0842` 套接字，**真实魔法包会被完全漏掉**。修法：在独立
   线程里读，`drain()` 只报告计数、绝不阻塞。回归测试直接断言"子进程沉默时 drain 立刻返回"。
2. **事件格式必须照抄内核**，不能照想象写。实测格式：
   ```
   1790738340.015097: wlp5s0 (phy #0): mgmt TX status (cookie 135): acked
   1790738412.501000: wlp5s0 (phy #0): disconnected (by AP) reason: 3 (DEAUTH_LEAVING)
   1790738412.777000: wlp5s0 (phy #0): new 44:df:65:cd:00:00 station became authorized
   ```
   MAC 夹在 `new` 和 `station` **之间**，所以字面量 `new station` 永远匹配不上真实关联；
   `\bdeauth\b` 也匹配不到 `DEAUTH_LEAVING`（下划线是词字符）。两个都曾让真实事件被丢弃。

另外：**监听器一启动就写一行 `# iw-event tap started ...` 心跳**。链路长期不断开的机器一条事件都
不产生，没有心跳就无法区分"监听器正常且安静"和"监听器坏了"。


## 4. 现状矩阵（哪些真跑过、哪些没有）

| 能力 | Windows | Linux | macOS |
|---|---|---|---|
| 判定引擎（纯逻辑） | ✅ 369 个单测（本机跑，三平台同源） | ✅ 同样单测 | ✅ 同样单测 |
| 调度产物生成 | ✅ 单测 | ✅ 单测 | ✅ 单测 |
| 空闲探测 | ✅ **真机实测**（本机 6.9 min） | ⚠️ 写了，未在 Linux 上跑 | ⚠️ 写了，未在 macOS 上跑 |
| 网卡枚举 | ✅ **真机实测**（10 张网卡，含虚拟） | ✅ **真机实测**（`wlan0`） | ⚠️ 写了，未跑 |
| 网卡管辖（`wake_nics`） | ✅ **真机实测**（本机 9 张里解析出配置的 2 张，含带空格的中文名） | ⚠️ 单测 | ⚠️ 单测 |
| 唤醒源解析（驱动） | ✅ **真机实测**（与 v1 当天的判定一致） | ⚠️ 本机拿不到（`kind=unknown`）——**这正是这次改成驱动无关的原因** | ⚠️ 写了，未跑 |
| 链路探测（驱动无关） | ⚠️ 单测（`netsh` / WLAN-AutoConfig 解析） | ✅ **真机实测**（与 `iw` 连续对账 14 次一致；本机 `carrier_changes` 30 分钟涨 4 次） | ⚠️ 写了，未跑 |
| 输入时钟（键鼠唤醒） | ✅ **真机实测** | ✅ **真机实测**（本机 X11 空闲 0s / 210s / 538s 三档都读到过） | ⚠️ 写了，未跑 |
| 回睡窗口（`resleep_delay_seconds`） | ⚠️ 单测 + `judge` 真机给出 `window-open`；**「唤醒→回睡」全链路待做** | ⚠️ 单测（输入侧复用真机验过的 evdev） | ⚠️ 单测 |
| 窗口内输入（差值判据） | ⚠️ 单测（Windows 上需真机校准：分不清按键与 HID 回放） | ✅ 走真机验过的 evdev + settle 窗口 | ⚠️ 未跑，需校准 |
| 会话级豁免锁存 | ⚠️ 单测 | ✅ **真机实测**（包过期一小时后仍 `skip wake-requested who=asked-for-this-session`；键鼠唤醒 `idle=600s` 仍 `skip wake-by-input`） | ⚠️ 单测 |
| 特权 WoL 端口（9/7） | — | ✅ **真机实测**（system 单元 + 仅 CAP_NET_BIND_SERVICE，Uid=1000；绑定后 magic 包判定为 `wake-requested`） | — |
| 看门狗（远程会话/音频/更新） | ✅ **真机实测** | ✅ **真机实测**（音频看门狗已修掉 PipeWire 空转误判，`guards=-` 复现） | ⚠️ 写了，未跑 |
| 睡眠调用 | ✅ **真机实测**（完整「唤醒→回睡」时间线） | ✅ **真机实测**（三次判决与内核 `PM: suspend entry` 逐条对上） | ⚠️ 未跑 |
| 计划任务注册 | ✅ **真机实测**（`schtasks` + `pythonw.exe`） | ✅ **真机实测**（3 个 systemd 用户单元，4 个 `[ok]`） | ⚠️ 未跑 |
| 一键自提权（install/uninstall） | ✅ **真机实测**（普通用户跑 install → 一次 UAC → 子进程在进度文件里逐条记录、父进程边等边念；任务先删再建、防火墙、监听器重启全在这一手里） | ⚠️ 单测（pkexec/sudo 真机待做） | ⚠️ 单测（osascript 待做） |
| 防火墙规则 | ✅ **真机实测**（规则指向 `pythonw.exe`；重装后每条规则只剩 1 条，不再是 4 条堆积） | ⚠️ 只打印过状态（无用户级接口） | ⚠️ 未跑 |
| 监听器单一实例 / 端口归属 | ✅ **真机实测**（`SO_EXCLUSIVEADDRUSE` + 提权子进程杀旧监听器 + install 问 OS 核对 9/7/45678 归属） | ⚠️ 未跑（POSIX 侧靠默认的独占绑定） | ⚠️ 未跑 |

「未跑」都标在这里而不是含糊过去。本机现在是 **Linux**（就是上面那张时间线的机器），
Windows 是原开发机、结果同样来自真机；macOS 只有**同一套单测 + `doctor` 自检输出**保证
接口一致，真正的确认要在目标机上做（第 6 节）。

## 5. 跑起来

```bash
# 不用安装（任何平台，Python 3.9+）
python autoresleep.py doctor          # 先看这台机器支持什么，输出里每一项都带来源
python autoresleep.py judge           # 只判断、写日志（默认不睡！）
python autoresleep.py judge --apply   # 真判断 + 真睡
python autoresleep.py listen          # 常驻：抓包 + 发现恢复
python autoresleep.py wol-sensor      # 只绑 9/7 并记证据（root system 单元跑的就是它）
python autoresleep.py collect         # 恢复时取证（调度器会调它）
python autoresleep.py install         # 写配置 + 注册计划任务/单元/LaunchAgent（+ 防火墙）
python autoresleep.py uninstall       # 全部撤销，逐条打印动过什么
python autoresleep.py config --set idle_minutes=4 --set unknown_idle_minutes=20
autoresleep config --set "wake_nics=WLAN 4,以太网 2" magic_only=true   # 也可以一次给多对
```
装成命令：`pip install -e .`（dev 用 `pip install -e ".[dev]"`）。跑测试：
```bash
python -m unittest discover -s tests -v        # 369 tests，无第三方依赖（Windows 上 9 个 POSIX-pipe 用例会 skip）
```

**安全默认与 v1 不同（刻意的）**：`judge` 只判断并写日志，**只有加 `--apply` 才真的睡**。
v1 是「默认干、要加 `-DryRun` 才不干」；v2 会被从没读过脚本的人跑，所以破坏性的那一半
改成显式打开。

**提权（普通用户一条命令即可）**：`install` / `uninstall` 自己提权一次，不需要你先开管理员终端。
父进程以你的身份做完用户级那一半（写配置、用户单元、启动常驻任务），需要特权的动作交给一个
**隐藏的子进程**：Windows 弹一次 UAC（`ShellExecuteW("runas")` + `SW_HIDE`），Linux 用 `pkexec`
（图形认证框）退 `sudo`，macOS 用 `osascript ... with administrator privileges`。子进程把每一步写成
`elevated-<命令>.progress`（行式、带时间戳），父进程**边等边念**——因为那个窗口你看不见，
沉默的等待等于让人猜。它**不会提问**（提问在看不见的窗口里等于卡死），所以答案都由父进程问好、
经内部参数带过去。失败时先读那份记录，再看它打印的手工命令。

不想让它弹提示（CI、沙箱、只想看会执行什么）：`--no-elevate`，或设 `AUTORESLEEP_NO_ELEVATE=1`，
它会打印那条该用管理员执行的确切命令。

## 6. 状态目录与文件（保留 v1 的来源前缀）

| 平台 | 目录 |
|---|---|
| Windows | `%LOCALAPPDATA%\AutoReSleep` |
| Linux | `$XDG_STATE_HOME/autoresleep`，缺省 `~/.local/state/autoresleep` |
| macOS | `~/Library/Application Support/AutoReSleep` |

| 文件 | 来源链 | 内容 |
|---|---|---|
| `session-cause.json` | `session-` | 本会话为什么被要醒（`asked-for` / `input`）＋会话标识。**整个会话**的豁免靠它，窗口过期后也管用；换会话自动失效 |
| `decisions.log` | `dec-` | 每次判定一行：`action= reason= src= link= who=` + `idle=` / `win=` / `pkt=` / `drv=` / `guards=`（`win=` 是窗口里有没有人：`in` / `none` / `-`，后跟判定它的时钟） |
| `heartbeat.txt` | `dec-` | 每行覆盖写：时间 + 判定 + 空闲秒数（看它一眼就知道还活着没） |
| `packets-seen.log` | `pkt-` | 每次抓到：`DETECTED kind=magic|marker src=IP:port len=N` |
| `packet-last.txt` | `pkt-` | 最新证据：`magic|epoch=..|src=..|len=..`（judge 读它） |
| `evidence-last.json` | `drv-`/`os-` | 最近一次恢复的唤醒源：`kind` / `who` / `raw` / `chain`（`chain=drv` 表示驱动自己报的，判定里压过 `os`） |
| `link-presleep.json` | `link-` | 睡前链路快照（`connected`/`ssid`/`bssid`/`changes`/`at`）：恢复后拿它判断 WiFi 是否掉过线，不需要驱动 |
| `link-events.log` | `link-` | 监听器采样到的链路变化（掉线/换 AP/重连），超过 256 KB 只保留最后 500 行 |
| `window-input.json` | `win-` | 回睡窗口里「有没有人」的结论：锚点（哪次唤醒）、截止时刻、是否已结束、`input`、首末两次采样的时钟值。listener 每秒采样、judge 直接读——和包证据同一套路 |
| `elevated-<命令>.progress` | — | 提权子进程的行式进度记录（带时间戳，父进程边等边念）；失败时先读它 |
| `journal.log` | — | 运行日志（标准库轮转，`log_max_bytes`/`log_backups`） |
| `config.yaml` | — | 全部阈值（YAML，带注释；`autoresleep config` 打印、`config --set 键=值` 修改） |
| `config.json.migrated` | — | 旧版 JSON 配置：install 首次跑时迁移成 `config.yaml` 后改名留下的 |

`raw` 字段永远保留操作系统原话（Windows 的事件字段、Linux 的 dmesg 行、macOS 的 pmset 行）——
归类只是方便，证据本体不加工。

## 6b. 配置项

配置文件是 **YAML**。项目里留一份**规范默认值** `config.default.yaml`（带中文注释，见仓库根），
`install` 在全新机器上把它**逐字节拷贝**到数据目录，之后一切读写只认数据目录那份
（`%LOCALAPPDATA%\AutoReSleep\config.yaml` / `~/.local/state/autoresleep/config.yaml`）：
`autoresleep config` 打印的是**装上去的那份**，`config --set 键=值` 改的也是它，项目里的模板不动。
想给一次批量部署定个起点，改项目里那份就行；数据目录那份已经存在时 `install` **不动它**
（手工加的注释和改动都留着），只有真的值不一样（比如命令行参数覆盖）才重写。
零依赖：读写走自带的 `miniyaml`（只覆盖这个文件需要的子集），要跑在没有 pip 的机器上。
**`install` 第一次跑会自动生成一份默认配置**，并停下来问「已生成配置文件，是否继续执行」——
想先改配置就回车/答 n，改完再跑一次 `install` 即可；脚本里加 `--yes`（或设 `AUTORESLEEP_NO_PROMPT=1`）就不问。
老机器的 `config.json` 会被自动迁移成 `config.yaml`（旧的改名成 `config.json.migrated`，不删）。
新一轮加的 4 个键是「谁在被管、等多久、什么能取消」的全部答案：

| 键 | 默认 | 含义 |
|---|---|---|
| `wake_nics` | `[]` | **要管教的网卡**。`[]` = 所有有线 + 所有无线。元素可写 MAC（随便哪种写法，会归一化）或网卡名（大小写不敏感，也认平台的人类可读名，如 Windows 的 `WLAN 4`、`以太网 2`）。列表外的网卡唤醒 → 不回睡，当有意唤醒。**配了匹配不到本机的名字，`doctor` 与日志都会 warn**——静默失效是这个项目最怕的失败模式 |
| `magic_only` | `false` | 「只允许 WoL 魔法包唤醒」：网卡类唤醒不再看 OS/驱动怎么解释它，一律成为回睡候选（包和键鼠两条豁免照旧）。**仍要过窗口与门槛**，不是「非魔法包立刻睡」 |
| `resleep_delay_seconds` | `10` | 唤醒后的观察窗口，**从唤醒时刻算**。里面收到魔法包或键鼠操作就取消回睡；窗口没结束绝不睡 |
| `resleep_break_on_input` | `true` | 窗口内有人动键鼠 → 打断回睡。关掉＝严格模式：形如「窗口内按键也照样睡」，但判定行会写明 `who=input-ignored-by-strict-mode`，不静默 |
| `guard_display_in_use` | **`false`** | 显示器被占用（`powercfg /requests` 里的 DISPLAY 段有人 `SetThreadExecutionState`）→ 不回睡。**默认关**：远控/串流工具会一整天占着它，实测就是它让这台机器永不回睡 |
| `guard_remote_in_use` | `true` | 有活动远程会话（`qwinsta` 里 rdp/tcp + active）→ 不回睡 |
| `guard_media_playing` | `true` | 有媒体在放 → 不回睡（Windows 上暂时无法把音频归因到某个请求，见限制清单） |
| `guard_update_running` | `true` | Windows Update 在跑（EXECUTION 段有请求 + `mousocoreworker`/`tiworker` 之类在场）→ 不回睡 |

两个值只在**注册任务时**写进任务 XML，改配置不会改已注册的任务：`repeat_minutes`（judge 的
重复间隔）与执行时间上限。想让它们生效就再跑一次 `install`（先删再建，一次 UAC）；
`doctor` 会直接告诉你两边是否一致（例：`judge repeats every PT2M, config repeat_minutes=1`
→ 提示跑 install 重建）。

`--set` 对 `wake_nics` 只按**逗号**切分（Windows 的网卡名里有空格），一次给多对时写成
`--set a=1 b=2` 或 `--set a=1 --set b=2` 都行。

窗口取代了旧字段 `idle_minutes` 作为「网卡类唤醒等多久」；`idle_minutes` 仍保留，但只在
**证据没有会话戳**（采集器从没跑过）或**机器在唤醒之后又被人用过**时作为兜底门槛。归因不明
（`unknown_idle_minutes`）与去抖（`min_gap_minutes` / `min_gap_unattended_minutes`）不变。

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

# 5) 真发一个包，看 pkt 链是否抓到（本机就能验，不需要第二台机器）
python - <<'EOF'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(b"HERMES-WOL-MARKER" + b"hello", ("127.0.0.1", 45678))
EOF
#    期望：packets-seen.log 出现 DETECTED kind=marker，紧接着 judge 给出 reason=wake-requested
#    实测（本机 2026-09-29 08:56:50）：
#      packets-seen.log: DETECTED kind=marker src=127.0.0.1:60922 len=41
#      judge           : action=skip reason=wake-requested src=pkt who=marker@+2s  （当时 link=LINK_LOST）
#    魔法包（UDP 9）在 Linux 上另有特权问题，见下面两节
```

**防火墙**（三平台都是同一个坑）：监听器把 UDP 9/7/67/68/45678 绑在 Python 进程上，
防火墙开着时入站单播默认被丢弃，`pkt-` 链会**安静地**永远什么都抓不到。
`install` 会处理 Windows（`netsh`）、Linux（有 `ufw` 时自动加，否则只打印该执行的命令）、
macOS（按程序放行解释器）。`--no-firewall` 明确跳过。

**规则是「先删再建」，不是「直接 add」**（Windows 实测）：`netsh add rule` 对**同名**已存在的
规则不报错，它会安静地再加一条——本机装过几次之后，每条规则名下面堆了 **4 条**，
每次 install 还会再叠一条；而当时的成功判据 `if _run(["netsh",...,"show",...]).strip()`
永远为真（没找到规则时 netsh 也会打印一句非空的「没有匹配的规则」），所以**加失败也报成功**。
`uninstall` 那边更糟：它只删一条，然后检查「名字还在不在」——名字当然还在，
于是规则留在原地、WoL 端口继续敞着，报告还说「什么都没删」。
现在 `_drop_rules()` 按名字删干净（上限 50 次防死循环）、再 add、再按**分隔行**数数核对；
`uninstall` 循环删到没有为止。

### Linux：WoL 端口（9/7）是特权端口——install 用一个最小 system 单元解决

监听器是普通用户的 `--user` 单元，而 Linux 默认 `net.ipv4.ip_unprivileged_port_start=1024`，
于是 9/7/67/68 一个都绑不上、只有 marker 口 45678 活着（本机实测 `refused: 7/9/67/68
(Permission denied)`）。症状不是报错，而是"魔法包唤醒之后两分钟被回睡"：包没被看见，判定就无从
知道这次唤醒是要来的。

所以 `install` 额外注册**一个** root 拥有的 system 单元 `autoresleep-wol-sensor.service`，只做
"绑 9/7 + 记一行证据"这件事：

```ini
User=jianghh                                   # 进程仍以你的身份跑，不是 root
Group=jianghh
Environment=HOME=/home/jianghh
AmbientCapabilities=CAP_NET_BIND_SERVICE        # 只多这一个能力
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=no                              # ambient 能力必须在它关闭时才能授予
ExecStart=.../autoresleep.py wol-sensor
```

实测（本机 2026-09-29 09:20）：`Uid: 1000`、`CapEff: 0000000000000400`（**只有**
CAP_NET_BIND_SERVICE，其余能力位全 0）、`net.ipv4.ip_unprivileged_port_start` 仍是 1024
（全机不放松）；同一时刻普通用户自己绑 9 仍然 `Permission denied`。发一个真 magic 包到 UDP 9：

```
packets-seen.log: 09:23:04  DETECTED kind=magic src=127.0.0.1:49244 len=102
judge           : action=skip reason=wake-requested src=pkt link=LINK_LOST who=magic@+2s
```

**为什么不把整个监听器搬进 system 单元**：`org.freedesktop.login1.suspend` 的 polkit 策略是
`allow_inactive=auth_admin_keep`——没有会话的进程调 `systemctl suspend` 会被要求交互认证，那会变成
"永远睡不着"的静默故障（正是本项目最怕的那类失败）。把判定与回睡留在用户单元，就不需要改任何
polkit 规则，特权进程小到只有"bind 9/7 + 写一行"。

不想让 install 写这个 system 单元：加 `--no-sensor`，或者事后 `sudo systemctl disable --now
autoresleep-wol-sensor && sudo rm /etc/systemd/system/autoresleep-wol-sensor.service`
（`uninstall` 也会删）。此时还有两条不用 root 的路：把发送端指向 marker 口 45678，或抬高内核边界
`sudo sysctl -w net.ipv4.ip_unprivileged_port_start=9`（代价是全机非特权进程都能绑 ≥9 的端口）。

`doctor` 会打印这条：`sensor: active - the privileged half watches UDP 9/7`；用户监听器发现端口被
别人看着时也不再报警，而是说"已被特权单元看着，这是设计好的分工"。

### Linux：`Linger=no` 等于重启后没人守

`--user` 单元随登录会话生灭：`Linger=no`（默认）时登出就全停，重启后没人登录就一直没在跑
（本机现在就是 `Linger=no`）。要让它在无人登录时也守着：

```bash
sudo loginctl enable-linger $USER     # 一次，需 root
```
`doctor` 会打印当前状态，`install` 在 `Linger=no` 时也会提示。

## 8. 已知限制（照抄 v1 的诚实清单，加上 v2 新增的）

- 睡眠中唤醒主机的那一个包在网卡固件里就被消费掉了，**任何平台的主机侧抓包都看不到它**；
  `pkt-` 抓到的是回醒之后到达的重复包/标记包。这是硬件限制。
- 驱动是否上报唤醒原因由厂商决定（同一台机器上都会时有时无），所以判定不依赖它：键鼠看输入时钟、
  WiFi 断开看睡前快照 vs 内核 `carrier_changes`，驱动信息只用来补充 `who=`。
- **Linux 的 WoL 端口（9/7）需要特权**：不动 `net.ipv4.ip_unprivileged_port_start` 的话，发往那两个口
  的魔法包监听器看不见，只有 marker 口收得到——而这条链正是"要来的唤醒绝不回睡"的保证。见第 7 节。
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
- **回睡窗口的判定依赖 listener 在跑**：窗口是「唤醒后 N 秒」，只有 listener 是每秒醒着的，
  快路径才能一到期就判。Linux 上 `Linger=no` 且没人登录时 listener 不在，判定退回 2 分钟粒度的
  定时任务（判定本身仍然正确，只是慢）。
- **Windows / macOS 的窗口内输入检测分不清「按键」与「resume 之后的 HID 回放」**：那两个平台没有
  evdev，只能用平台输入时钟的**差值**，而差值同样会被 HID 回放推动。代价方向是保守的（机器不睡
  而不是睡错），出口是 `resleep_break_on_input=false`。Linux 没这个问题：evdev 带设备名，还有
  15 秒的运动 settle 窗口。
- **窗口的结论只描述唤醒后那 N 秒**：如果机器在唤醒之后又被人用过（最后一次输入比唤醒时刻更新），
  窗口的旧结论失效、回到 `idle_minutes` 门槛。否则一段几小时前的会话会让机器睡在正在打字的人身上。
- **提权会用一次 UAC / 认证框**：`install` / `uninstall` 自己做，不需要管理员终端；不想要提示就
  `--no-elevate`（只打印该执行的命令）。提权子进程是隐藏的，进度靠 `elevated-*.progress` 转述——
  如果它卡住，先看那份记录。

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