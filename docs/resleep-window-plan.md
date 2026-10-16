# 回睡窗口改造 + 三平台自提权 —— 详细计划

状态：**需求已定稿，代码未动**。本文件是设计与阶段划分；逐项可勾选的执行清单在
[resleep-window-todo.md](resleep-window-todo.md)。真机踩过的坑在
[install-pitfalls.md](install-pitfalls.md)（提权部分的验收标准直接引用它）。

---

## 0. 这份计划要解决什么

两件事，可以分开做但一起做更省事：

- **A. 回睡窗口与配置面**：用户能按网卡配置要管教的设备；能要求「只允许 WoL 魔法包唤醒」；
  唤醒后固定 N 秒（默认 10）回睡；这段时间里如果有人动键鼠，可以（按配置）打断回睡。
- **B. 三平台自提权**：`install` / `uninstall` 在三个平台都能由**普通用户**直接执行，
  需要特权的部分由脚本自己提权去完成，不再要求用户事先开管理员终端。

---

## 1. 需求定稿（逐条对应问答结论）

| # | 问题 | 结论 |
|---|---|---|
| Q1 | `wake_nics` 的语义 | **(a)** 只有列表里的网卡进入回睡判定；列表外的网卡唤醒当「有意唤醒」，不回睡。`[]` = 所有有线 + 所有无线 |
| Q2 | `magic_only` 打开时是否还过空闲门槛 | **仍要**。不是「非魔法包立刻睡」，而是「等窗口过完、确认没人」 |
| Q3 | 「唤醒后 N 秒」从哪算 | **从唤醒时刻算**（不是「最后一次输入后 N 秒」） |
| Q4 | 窗口与既有 `idle_minutes` 的关系 | **(a) 窗口就是唯一等待时钟**：网卡类唤醒的回睡等待 = `resleep_delay_seconds`；`idle_minutes` 不再决定等待时长 |
| Q5 | 打断开关关掉时窗口内按键怎么办 | **严格模式：照样回睡**（无视窗口内的键鼠） |
| Q6 | 包/键鼠豁免的范围 | **整个会话**（锁存到这次清醒时间结束，不是只豁免一次判定） |
| Q7 | `resleep_delay_seconds` 默认值 | **10** |
| Q8 | 平台范围 | **三平台一起**（判定引擎本来就平台无关） |

### 1.1 由 Q4(a) + Q5 推出的一个必然取舍（必须写清）

「确认没人在用」这个门槛**仍然存在**（Q2），但它的判据变了：

- 旧：距最后一次用户输入 ≥ `idle_minutes`（默认 2 分钟）；
- 新：**窗口内一次输入事件都没有**（默认 10 秒）。

代价是明确的：**有人坐在机器前读东西、10 秒里没碰键鼠，机器会被睡掉**。
这是 Q4(a)（窗口是唯一等待）加上 Q5（严格模式无视键鼠）两件事叠加的必然结果，不是实现缺陷。
想要「人在也不睡」的保护就必然与严格模式矛盾——那是方案 (c)（`max(窗口, idle_minutes)`）要解决的问题，
本次按 (a) 实现。

---

## 2. A：配置项定义

新增 4 个键（`config.json`，`Config` 数据类）。`Config.load` 本来就会忽略未知键，因此旧配置不炸、
新配置在旧版本里被忽略，双向兼容。

| 键 | 类型 | 默认 | 含义 | 校验 |
|---|---|---|---|---|
| `wake_nics` | list[str] | `[]` | 要管教的网卡。元素可写 **MAC** 或 **网卡名**（名字大小写不敏感；MAC 去分隔符后比较）。`[]` = 所有有线 + 所有无线 | 元素非空；MAC 合法则归一化，否则按名字处理；解析不到任何网卡 → 日志 + `doctor` 均 warn，并按空列表（全部）处理 |
| `magic_only` | bool | `false` | 只允许 WoL 魔法包唤醒。打开后网卡类唤醒不再依赖 OS/驱动归因 | — |
| `resleep_delay_seconds` | float | `10` | 唤醒后的观察窗口，**从唤醒时刻算** | ≥ 0 |
| `resleep_break_on_input` | bool | `true` | 窗口内有人动键鼠 → 打断本次回睡。关掉 = 严格模式（照样睡） | — |
| `input_settle_seconds` | float | `5` | 唤醒后这几秒内的输入变化算 resume 的 HID 回放、不算人（**本节之后追加**，理由见 §11） | ≥ 0 |

改名/退役：

| 旧键 | 处置 |
|---|---|
| `idle_minutes` | **不再决定网卡类唤醒的等待时长**。字段保留（老配置、老测试、`grace_verdict` 的 `needed_s` 都引用它），标注 deprecated，`doctor` 里说明它现在只作为「非窗口类」判定的兜底 |
| `unknown_idle_minutes` | 不变：归因不明时仍要求 15 分钟长空闲 |
| `pkt_window_s` | 保留：判断「刚听到的包是不是这次唤醒的原因」。与窗口分工见 3.4 |
| `fast_path_seconds` | 保留，但快路径的门槛从 `idle_minutes*60` 换成 `resleep_delay_seconds` |
| `grace_seconds` | 保留（睡前最后一次复查，只对「猜出来」的判定）。**顺手修 bug**：`judge` 的 `--grace` argparse 默认值写死 60，会盖掉 config 里的值 |

命令行：

```bash
autoresleep config --set wake_nics=00:11:22:33:44:55,wlp5s0 \
                          magic_only=true resleep_delay_seconds=10 resleep_break_on_input=true
```

`config --set` 现在把 list 一律按 `[int(v) ...]` 解析（那是给 `ports` 用的），`wake_nics` 必须走字符串列表
——按「配置项当前值的元素类型」解析，或给 `wake_nics` 显式分支。

---

## 3. A：判定流程

### 3.1 时间线

```
t0            唤醒（恢复检测：SuspendLedger / wall-vs-monotonic 兜底 / Windows 恢复事件触发器）
t0 … t0+delay 窗口（默认 10s）。listener 每秒一轮；特权传感器常驻听：
                 · 魔法包：UDP 9/7 + 裸帧 ethertype 0x0842（MAC 命中 wake_nics 解析出的集合）
                 · 键鼠：真实硬件输入（Linux evdev 优先，见 4.2）
t0+delay      判定（listener 快路径立刻判一次，不等 2 分钟粒度的定时任务）
                 ├ 有包                     → skip wake-requested      + 锁存整个会话
                 ├ 有键鼠 且 break=true      → skip wake-by-input       + 锁存整个会话
                 ├ 有键鼠 且 break=false     → 严格模式：无视键鼠，继续往下（不锁存）
                 └ 都没有                    → 回睡候选
回睡候选       仍要按原顺序过：guards → 去抖(min_gap) → 归因不明时 unknown_idle_minutes
               → 睡前 grace（只对猜出来的判定） → suspend
```

### 3.2 判定链的新顺序（对照 `decide.py` 现在的 1–8）

| 新顺序 | 内容 | 与现在的关系 |
|---|---|---|
| 0 | 会话锁存（`session-cause.json` = asked-for / input）→ skip | 不变，但只在**整个会话**范围内有效（3.4） |
| 1 | 窗口内听到 magic/marker 包 → skip + 锁存 asked-for | 不变，窗口判据沿用 `pkt_window_s` + 会话龄 |
| 2 | guards（远程会话 / 全屏 / 放音 / Windows Update）→ skip | 不变 |
| 3 | 窗口内键鼠 且 `resleep_break_on_input=true` → skip + 锁存 input | **改**：从「唤醒那一刻的键鼠」放宽到「窗口内的键鼠」；关掉开关则不锁存（否则永远睡不着） |
| 4 | **网卡类唤醒**（命中 `wake_nics`）→ 回睡候选：<br>· `magic_only=true`：不看归因，直接候选<br>· 否则沿用现有链 4（`LINK_LOST`）/ 链 6 的 `wifi-nic` | **改**：`eth-nic` 从「有意唤醒 skip」改为**进判定**（有线也要管）；等待时钟换成窗口 |
| 5 | 非网卡类归因（`timer` / `other-device` / USB 设备）→ skip | 不变（`magic_only` 的不收范围见第 8 节待确认项） |
| 6 | `fixed-feature-nic-suspect`（Windows 叫不出名字的网卡）→ 按 `unattributed_wake_policy`，默认保守 skip | 不变；若它命中 `wake_nics` 则按网卡类处理 |
| 7 | 什么都没拿到 → `unknown_idle_minutes` 长空闲 | 不变 |
| 8 | 去抖：距上次强制睡眠 < `min_gap_minutes`(5)；无人 + 网卡归因时 `min_gap_unattended_minutes`(1) | 不变，接线要在窗口判定之后 |

### 3.3 一句话总结

**窗口 = 网卡类唤醒唯一的等待时钟**；「确认没人在用」的门槛仍在，只是判据从「空闲满 N 分钟」
换成「窗口内零输入事件」（见 1.1 的取舍）。

### 3.4 锁存（Q6「整个会话」）的确切语义

- 一次「会话」= 一次唤醒到下一次入睡之间的整段清醒时间。标识是恢复采集器写下的 `session` 时间戳；
  锁存同时记 `slept_total`（机器累计睡眠时间），**机器自己又睡过一次 → 旧锁存自动失效**，
  不需要任何进程记得去清。
- 包 → 永远锁存 `asked-for`（有人明确要它醒着）
- 键鼠 → `break=true` 时锁存 `input`（人来了，整段清醒时间都不主动睡它）
- `break=false`（严格模式）→ **不锁存**，否则一次按键会让机器整晚不睡，和严格模式自相矛盾
- 网卡异常唤醒（链路掉线/重连那类）**不享受锁存**，照旧按窗口 + 门槛回睡

**`pkt_window_s` 与窗口的分工**：`pkt_window_s`(180s) 回答「刚听到的包是不是**这次唤醒的原因**」
（包龄 vs 会话龄），窗口回答「我们盯了多久」。两者都保留：窗口内听到包 → 立即锁存；窗口过后
才来的包仍是「要它醒着」的锁存，但不再算作「这次唤醒的原因」。

### 3.5 日志与原因名（沿用本项目「取证式日志」风格）

| 场景 | 日志 |
|---|---|
| 窗口内没包没键鼠，网卡类 | `SLEEP reason=NON_MAGIC_WLAN_LINK_MOVED / NON_MAGIC_WIFI_NIC / NON_MAGIC_ETHERNET_NIC(新)` |
| `magic_only` 收紧命中 | 沿用具体链名；归因拿不到时 `NON_MAGIC_UNATTRIBUTED`（见第 8 节口径二选一） |
| 窗口内键鼠打断 | `skip reason=window-input-break  who=<设备>@<窗口内第 N 秒>` |
| 严格模式明知有人按键仍睡 | 正常 `SLEEP`，但判定行必须带 `who=input-ignored-by-strict-mode`——「我们明知有人还是睡了」要能从日志一眼看出来 |

---

## 4. A：三平台落地要点

### 4.1 窗口计时落在哪

- 只有 **listener 是每秒醒着的**，所以窗口判定必须落在 listener 快路径（恢复后立刻重判的机制已有，
  门槛从 `idle_minutes*60` 换成 `resleep_delay_seconds`）。
- Windows 另有恢复事件触发器；Linux/macOS 靠 listener 的恢复检测（`SuspendLedger`）。
- **已知限制（写进 README 限制清单）**：Linux 上若 `Linger=no` 且没人登录，listener 不在跑，
  窗口判定就只能退回 2 分钟粒度的定时 judge，回睡延迟上界变成 2 分钟。

### 4.2 窗口内「有没有人动键鼠」的新判据（三平台）

现在判「键鼠唤醒」用的是「输入时钟是否落在唤醒那一刻附近」，**绝对读数不可信**：桌面空闲计数会被
恢复清零（Linux X11 实测如此）。窗口内改用**差值判据**：

- 采样规则：t0 采一次，窗口内周期采（listener 每秒本来就在跑）；
  **时钟 tick / 读数发生变化 = 有人操作**。清零只污染绝对读数，不污染两次采样的差。
- Linux：优先 `evdev` 真实事件（特权传感器已有）
- Windows：`GetLastInputInfo` 的 `dwTime` tick 变化
- macOS：`HIDIdleTime` 两次采样的变化（**需真机校准，我没有 macOS**）
- 落地方式与包证据同一套路：特权传感器 / listener 写一份小状态文件，judge 和快路径都读它
  （建议 `window-input.json`：窗口起点、最近采样值、是否观察到变化、采样者、时间戳）

### 4.3 魔法包 MAC 集合（不改就是睡反）

`pkt.wireless_macs()` 现在**优先只取无线网卡的 MAC**（没有无线才用全部）。有无线网卡的机器上，
有线网卡收到的魔法包不会被认成 magic → 会被当成「非魔法」→ 触发回睡，正好和需求相反。

改法：magic 匹配集合 = `wake_nics` 解析出的 MAC 集合（`[]` → 所有有线 + 所有无线），
listener 与特权传感器共用同一份。

### 4.4 平台命名差异（`wake_nics` 怎么匹配）

README 已有的规矩：**绝不写死网卡型号/接口名**（换卡、重装驱动都会变）。因此：

- 匹配优先级：**MAC 优先**（归一化：去分隔符、小写），名字兜底（大小写不敏感精确匹配）；
- 名字来源各平台不同：Windows `FriendlyName`/`Description`、Linux ifname、macOS `label`；
- `doctor` 必须打印解析结果（「配置解析成 N 个网卡：…」），解析不到就 warn——**静默失效正是本项目最怕的失败模式**。

### 4.5 特权端口（Linux 9/7）

不变：`install` 注册 root 拥有的 `autoresleep-wol-sensor.service`（进程仍是登录用户，
只有 `CAP_NET_BIND_SERVICE` + `CAP_NET_RAW`），由它绑 9/7 和裸帧 0x0842。
提权改造后这一步不再需要用户手动 `sudo`（见第 5 节）。

---

## 5. B：三平台自提权

### 5.1 现状（今天每个平台卡在哪）

| 平台 | 今天怎么提权 | 卡在哪 |
|---|---|---|
| Windows | 只有 `.cmd` 双击时会提权（`net session` 检测 + `Start-Process -Verb RunAs`）；直接跑 `python autoresleep.py install` 会 `rc=2` 拒绝并打印命令 | 命令行用户必须自己开管理员终端；`.cmd` 是整只脚本提权重跑，没有进度转述、也不符合 pitfalls §0/§2 的分工 |
| Linux | 用户单元免 root；唯一的 root system 单元由 `cli._write_root_artifact()` 调 `sudo install -m 644` 写；`ufw allow` 直接调 `ufw`（无 sudo） | 非交互环境 `sudo` 直接失败；终端里会停在密码提示上；`ufw` 那步没提权会静默失败 |
| macOS | LaunchAgent 免 sudo；`socketfilterfw` 直接调（要 root） | 防火墙这步静默失败；`install` 里没有任何自提权路径 |

### 5.2 目标与分工（父进程 / 提权子进程）

**目标**：普通用户执行 `install` / `uninstall`，脚本自己把需要特权的动作提权完成，
过程中用户看得见进度、看得懂在等什么。

**分工（建议：只把特权动作交给提权子进程，而不是整只脚本提权重跑）**：

| 角色 | 干什么 |
|---|---|
| 父进程（普通用户） | 写 `config.json`；写用户级产物（用户计划任务 XML 落盘、systemd user 单元、LaunchAgent）；启动/重启用户 listener；读并转述子进程进度；汇总结果 |
| 提权子进程（管理员/root） | **只做特权动作**：Windows 注册 3 个计划任务（`schtasks /Create /XML /F`）+ 防火墙规则；Linux 写 `/etc/systemd/system/autoresleep-wol-sensor.service` + `systemctl daemon-reload/enable --now` + `ufw` 规则；macOS `socketfilterfw --add/--unblockapp`。卸载侧对称：删任务/单元/规则 |
| `--dry-run` | 两级都不提权，只打印 |

**为什么不做「整只脚本提权重跑」**：提权进程会以管理员身份启动 listener/用户单元（Windows 上
「最高权限」的任务本身没问题，但 `install` 里其它用户级动作会以管理员身份执行）；
而且 pitfalls §2 要求升权子进程**永不提问**，整只 install 提权重跑就必须先把所有问题在父进程问完，
复杂度反而更高。分工版天然满足 §2：子进程只吃内部参数。

### 5.3 三平台的提权手段

| 平台 | 手段 | 要点 |
|---|---|---|
| Windows | `powershell -NoProfile -Command "Start-Process -FilePath <exe> -Verb RunAs -PassThru -WindowStyle Hidden -ArgumentList ..."` | pitfalls §4：隐藏窗口；§0：子进程写行式进度文件，父进程念；§1：子进程内调原生命令一律 `$null \| & $Exe @argv 2>&1`；§5：`$ErrorActionPreference='Stop'` 下的三类地雷 |
| Linux | `pkexec`（桌面会话：图形认证框）→ 无则 `sudo`（终端）→ 都没有则**打印完整命令**让用户自己跑 | `pkexec` 会清洗环境 → 必须显式传全部参数；兼容「没有 pkexec 的纯 tty 机器」 |
| macOS | `osascript -e 'do shell script "..." with administrator privileges'` | 标准认证框；同样要显式传参 |

**统一入口**：新增一个**内部子命令**（例如 `elevate`），只接受内部参数（动作清单、显式
`--config`/数据目录、非交互标志），三平台各有一个启动器实现；`install`/`uninstall` 在检测到
需要特权时调用它，并且**不把它的输出直接喷给用户，而是念进度文件**。

### 5.4 必须遵守的规矩（逐条对应 install-pitfalls.md）

| pitfalls 条目 | 落地要求 |
|---|---|
| §0 先拿证据再猜原因 | 提权子进程每走一步追加一行**带时间戳**的 `elevated-<动作>.progress`；父进程边等边念；连续 N 秒没有新行 → 给出明确指引（「UAC 框可能被主窗口挡住/在任务栏闪烁」）。`Start-Transcript` 不用（它自己卡过） |
| §1 原生命令喂空 stdin | 子进程内所有 `schtasks`/`netsh` 调用统一走 `$null \| & $Exe @argv 2>&1` 封装；判据 `$null \| cmd /c pause` ~20ms 返回 |
| §2 升权子进程不许提问 | 子进程内任何 `Ask-*` 一律取默认值并说明；答案由父进程问好，经内部参数带入（**不用环境变量**——过 UAC 不保证继承） |
| §3 不用 WMI 探进程 | 停旧 listener 用 `Get-Process`（现有 `_pid_alive` 已经是 `tasklist`，保持），不要 `Get-CimInstance Win32_Process` |
| §4 升权不露窗口 | `-WindowStyle Hidden` + `-PassThru` |
| §5 `$ErrorActionPreference='Stop'` 地雷 | 不 `Test-Path C:\Windows\System32\Tasks\<名字>`（改走列任务）；不写机器级 PATH；裸调 `schtasks` 的 stderr 统一封装 |
| §6 SYSTEM 任务非管理员看不见 | 措辞：未提权时说「读不到内容，要管理员才看得到」；「不存在」只在管理员下才允许说 |
| §7 任务归安装脚本管 | 安装时若任务/单元已存在 → **先删再按定义重建**（`.cmd` 路径下 `schtasks /Create /XML /F` 是否等价于「先删再建」，要在真机上验一次 ACL/触发器没有残留）；卸载必删 |
| §8 架构不匹配说人话 | 本项目不涉及二进制包，暂不适用（将来打包时再补） |
| §9 其他 | `.ps1` 必须带 UTF-8 BOM；macOS 自带 bash 3.2 的变量花括号坑（写 `.sh`/`.command` 启动器时一律 `${VAR}`）；升权前把话说清楚（会弹 UAC、窗口可能在哪、怎么退出） |

### 5.5 提权后的身份与路径问题

- 提权进程必须**显式收到** `--config` 和状态目录（不能依赖 `%LOCALAPPDATA%`/`$HOME`/`$XDG_STATE_HOME`：
  同用户提权通常一致，但**换成另一个管理员账户提权就会变成那个账户的 profile**，
  于是「配置写到别处、任务指向别处」这种静默错位）。
- 提权进程执行完要有一行明确的「完成/失败」终点记录，父进程**读那份记录**再报成功或失败
  （不能靠「没看到输出」推断「没做」）。

### 5.6 卸载对称性

- `uninstall` 同样自提权；特权侧动作：删 3 个计划任务 / 删 system 单元 + `daemon-reload` / 删防火墙规则。
- 用户级动作（停 listener、删用户单元、删 LaunchAgent、`--purge` 删状态文件）留在父进程。
- 验收：`install` → `uninstall` → 再 `install` 必须幂等，且第二次 install 不出现残留触发器/ACL。

---

## 6. 验证计划

### 6.1 单测清单（纯逻辑，三平台同跑；命令 `python -m unittest discover -s tests -v`）

1. **配置**：新键默认值 / 校验 / `config --set` 传字符串列表 / 旧配置（无新键）兼容 / 未知键忽略
2. **网卡**：`[]`=全部、MAC 与名字混合、平台命名差异、解析不到时的 warn 行为
3. **窗口判定矩阵**：`{有包|无包} × {有键鼠|无键鼠} × {break 开|关} × {网卡在|不在列表} × {magic_only 开|关}`
   → 期望 `action` / `reason` / `who` / 是否锁存
4. **时间线**：窗口到期立刻判定（不等定时任务）；锁存跨窗口保持；换会话失效；机器再睡一次（`slept_total` 变化）失效；`break=false` 不锁存
5. **窗口内输入检测**：三次采样（无人 / 有人按键 / 只有运动被 settle 窗口忽略）→ 期望布尔值
6. **回归**：现有 162 个测试全绿（受影响的：`idle_minutes` 语义、`--grace` 默认值那几条，会一并改）
7. **源码级规则**（本项目已有先例：裸 `subprocess.run([...])` 会失败）：提权子进程里出现 `Ask-*` /
   `Read-Host` 就失败；`.ps1` 缺 UTF-8 BOM 就失败；出现裸 `schtasks` 调用就失败

### 6.2 真机验证清单

Windows（本机）
1. `judge --dry-run`：新配置项生效（`doctor` 打印解析后的网卡集合、窗口值、两个开关）
2. 真魔法包（本机 `socket.sendto` 到 127.0.0.1:9）→ `packets-seen.log` + `skip wake-requested`
3. 真键鼠（`break=true`）→ `skip wake-by-input` + 锁存；`break=false` → 判定行带 `input-ignored-by-strict-mode`
4. 提权四连：普通用户跑 `install` → 数 UAC 弹几次、进度有没有被念出来、失败时能不能看出死在哪；
   `uninstall` 对称；「任务已存在」重复 install 检查残留；未提权时 SYSTEM collector 任务的措辞

Linux
1–3 同上（键鼠走 evdev，需要一个能读 `/dev/input` 的特权传感器）
4. 提权：有桌面会话时 `pkexec` 图形框；纯 tty（`pkexec` 不可用）时退 `sudo`/打印命令；`Linger=no` 的提示仍在

macOS（我没有这台机器，只能靠单测 + `doctor`，真机确认需要你）
1. `doctor` / `judge` 的输出与 Linux 一致
2. 提权：`osascript` 认证框；`socketfilterfw` 那步真的成功
3. 窗口内键鼠检测（`HIDIdleTime` 两次采样）校准

---

## 7. 阶段划分与交付物

| 阶段 | 内容 | 交付物 |
|---|---|---|
| **S0** | 本文件 + TODO；需求定稿 | `docs/resleep-window-plan.md`、`docs/resleep-window-todo.md` |
| **S1** | 配置面：新 4 键 + 校验 + `config --set` 字符串列表 + `doctor` 显示解析结果 | `config.py`、`cli.py`、单测 |
| **S2** | 网卡解析：`wake_nics` → MAC/名字集合；magic 匹配集合改用配置 | `pkt.py`、单测、`doctor` 输出 |
| **S3** | 判定引擎：窗口判定 + nic 过滤 + `magic_only` + 锁存语义 | `decide.py`、单测矩阵 |
| **S4** | 窗口执行器：唤醒锚点 + 窗口内输入检测（三平台）+ 快路径改按窗口 | `cli.py`、`system/*`、单测 |
| **S5** | 清理与修 bug：`--grace` 默认值、`idle_minutes` 退役标注、去抖接线 | 单测回归全绿 |
| **S6** | 三平台自提权：内部提权子命令 + 进度文件 + 三平台实现 + 启动器 + 卸载对称 | `cli.py`、`install.cmd/uninstall.cmd`、`install.sh`、单测（源码级规则） |
| **S7** | 真机验证：上面 6.2 的清单 | 逐条日志对账记录 |
| **S8** | 文档：README 配置表 + 示例 `config.json` + 限制清单（4.1 那条）+ pitfalls 落地说明 | README、QUICKSTART |

每个阶段收尾：`python -m unittest discover -s tests -v` 全绿再进下一阶段。

---

## 8. 待你点头的开放项（我先按括号里的默认实现）

1. `magic_only` 默认 **false**；`resleep_break_on_input` 默认 **true**（更安全：人在机器前就不睡）
2. `magic_only` 的收紧口径，两种：
   - **(i)（建议）只收网卡类**：命中 `wake_nics` 的唤醒被收紧；`timer` / `other-device` 仍豁免
     （定时器唤醒是系统自己的维护计划，不是网卡异常唤醒）
   - **(ii) 字面收紧**：除了包和键鼠，任何唤醒都回睡（含 `timer` / `other-device` / 归因不明）
   - 注意 Linux 现实：拿不到归因时 `kind=unknown`（README 现状矩阵），(i) 在 Linux 上抓不住
     「网卡唤醒」，会落到「归因不明 → 15 分钟长空闲」；(ii) 能抓住，代价是定时器唤醒也会被睡
3. 提权形态：**只把特权动作交给提权子进程**（5.2 的分工版），还是整只 `install` 提权重跑
4. Linux 提权链：`pkexec` → `sudo` → 打印命令；要不要额外提供 `--no-elevate`（只打印命令，不动系统）
5. 是否现在就把 `docs/install-design.md`（pitfalls 里引用的设计契约）补出来——它不在这个 checkout 里

## 9. 顺带发现（要不要一起做随你）

- `v2/` 下有三个 0 字节文件 `cd`、`p`、`python`，**已被提交进仓库**（看着像误建的），建议清掉。
- `judge` 的 `--grace` argparse 默认值 60 会盖掉 `config.json` 里的 `grace_seconds`（S5 修）。
- 上一轮的工作还没提交：README §3f 洪泛修复、`inputwatch.py` 改动、`tests/test_input_flood_cost.py`、
  `tools/flood_cpu_proof.py`。

---

## 10. 实施记录（2026-10-08/09）—— 与第 1–9 节的差异

落地进度与逐项验收见 [resleep-window-todo.md](resleep-window-todo.md)（S0–S8 已勾选）。这里只记
**计划之外**的决定与发现，设计本身没有改：

1. **陈旧窗口（新规则，第 3.5 节没写）**。窗口的 `input` 结论只描述唤醒后那 N 秒。若机器在唤醒之后
   又被人用过（最后一次输入比唤醒时刻更新，即 `idle_s < age`），窗口的旧结论失效、回到
   `idle_minutes` 门槛。没有这条，一段几小时前的会话里的 `window_input=False` 会让机器睡在正在
   打字的人身上 —— 这不是理论风险：本机那份安装的计划任务指向这个 checkout，改动一落地就在跑。
2. **窗口内输入的判据是差值，不是读数**。`windowwatch.observe()` 只认平台输入时钟的**变化**；
   绝对值（`idle=0s`）被 resume 清零，正是 v1 的老坑。代价要记住：Windows/macOS 分不清「按键」与
   「resume 之后的 HID 回放」，方向是保守的（机器不睡），出口是 `resleep_break_on_input=false`。
3. **`wake_nics` 的 `--set` 只用逗号分隔**。Windows 网卡名带空格（`WLAN 4`、`以太网 2`、
   `vEthernet (Default Switch)`），按空白切分会把名字切碎 —— 与第 4.4 节「配了就生效，不生效就吵」
   同一类要求。
4. **提权形态按第 5.2 节的分工版实现**：子命令 `install --elevated --progress <file>` 做特权动作，
   父进程做用户级那一半并念进度。Windows 用 `ShellExecuteW("runas", SW_HIDE)` 而不是 PowerShell 的
   `Start-Process`（少三层引号）；子进程的 stdout/stderr 直接写进进度文件（pythonw 没有控制台）。
5. **新增 `--no-elevate` / `AUTORESLEEP_NO_ELEVATE`（第 8 节 O.4 的答案）**：单测、CI、沙箱里绝不允许
   在别人桌面上弹 UAC，所以这个开关在 `elevate.delegate()` 这个咽喉处兜底。
   （教训：这次真有一个测试在跑 `install` 时弹了 UAC —— 就是被它抓出来的。）
6. **防火墙规则的程序** 从 `sys.executable` 改成 listener 真正使用的解释器（Windows 上
   `pythonw.exe`）：给一个永不绑端口的二进制开洞等于没开。
7. **基线本来就红**：HEAD 在本机 Windows 上 18 errors（`os.geteuid` 不存在）+ 3 failures（用
   `os.pipe()` 驱动 evdev 的用例，Windows 的 select 不支持管道句柄）。已修，且那 9 个用例在 Windows
   上显式 skip 而不是「以错误的理由通过」。
8. **`--grace` 默认值**（第 2 节表的最后一行）确实盖住了配置，已修。

---

## 11. 实施记录（2026-10-15）—— 一次真实的「非键鼠、非魔法包唤醒没有回睡」

现场（本机 `%LOCALAPPDATA%\AutoReSleep`，日志原样）：21:25:07 因 WiFi 掉线被强制回睡
（`NON_MAGIC_WLAN_LINK_MOVED`），21:34:22 被 **Killer Wi-Fi 6 AX1650i** 唤醒
（`evidence-last.json`：`chain=os kind=wifi-nic WakeSourceType=5`，不是键鼠、不是魔法包），
此后一路 `action=skip reason=wake-by-input src=input who=input-this-session`，整晚没再睡。
按第 3/6 节这次唤醒本该是回睡候选，拦住它的是链 3 —— 而链 3 依赖的那条事实是错的，
两个时钟自相矛盾：

| 时刻 | 事实 | 出处 |
|---|---|---|
| 21:24:21 | 最后一次真实输入（tick `129287953`） | 由 21:34:24 那行 `idle=603s` 反推 |
| 21:34:22 | 唤醒，OS 归因＝网卡 | `evidence-last.json` |
| 21:34:24 | 窗口打开（锚点）；**首个采样读到的还是睡前那个 tick，`idle=603s`** | `window-input.json` 的 `first_tick` |
| ≈21:34:26 | tick 跳到 `129892562`（**+604.6 秒**）→ 窗口判为「有输入」 | `window-input.json` 的 `last_tick`、判定行 `win=in@win32-window-tick` |
| 21:34:34 | 窗口关闭 → `wake-by-input(@win32-window-tick)` → `_may_latch_input` 放行 → 锁进 `session-cause.json` | `decisions.log` |

即：**没人碰过这台机器（`idle=603s` 就是证据），但 resume 之后的 HID 回放把平台时钟推了一次，那一次被
当成了人**；而 `*-window-tick` 是「硬件级」来源、允许产生会话级豁免（第 3 节 Q6），于是一次误判 = 整晚不回睡。

两条修复：

1. **`input_settle_seconds`（默认 5 秒）：把 Linux 早就有、另两个平台一直缺的那道 settle 窗口补上**。
   `inputwatch.MOTION_SETTLE_S`（15 秒）忽略的是 MOTION 事件——它带设备名，还能区分「按键」与「移动」；
   Windows/macOS 没有设备名，只剩时钟，所以只能按时间划线：唤醒后 `settle_s` 内的 tick 变化只进
   `settled` 计数、不算人。**变化仍然折进 `last_tick`**——否则同一个回放会在 settle 结束后的下一秒被重新
   发现，等于只把误判推迟。判定行写 `win=none@windows-window-tick(1-settled)`，`doctor` 也打印。
   上限就是 `resleep_delay_seconds`（等于严格模式），不会偷偷越过它。残留风险已写进 README 限制清单：
   晚于 settle 的回放仍会被读成输入（实测这台是唤醒后 ~1.6 秒，Linux 上是 +7/+11/+12 秒）。
2. **强制睡眠标记改成在 `suspend()` 之前写**（`_suspend_with_marker`）。Windows 的 `SetSuspendState`
   **要等机器被唤醒才返回**，而 `_mark_forced_sleep()` 写在它之后——实测 21:25:07 那次睡眠的标记是
   `1792071262.26` = **21:34:22，也就是唤醒时刻**。三个后果：`last_forced_sleep_at` 变成「0 秒前睡过」，
   去抖基线失真（会白吃掉一次合法回睡）；`decisions.log` 在唤醒瞬间多出一条
   `SLEEP ... (listener fast path)`，读起来像「唤醒瞬间又想睡」；`input_since_sleep` 的 tick 基准是苏醒后
   才读的，已经被 resume 自己推动过。现在标记在调用前写、`suspend()` 抛异常就回滚，时钟读不到时降级成
   「不知道」而不是丢掉这次睡眠。

## 12. 实施记录（2026-10-16）—— Linux 归因链补上「有线网卡」与「每设备唤醒事件」

现象（Linux 机，root 安装）：唤醒既不是键鼠也不是魔法包，机器不回睡。静态分析 + 真机读数定位：
**不是判定链的问题，是 Linux 的 `wake_source()` 根本认不出有线网卡，而内核又不给唤醒源字符串**。
`decide.py` 一行没动，改动全部收在 `system/linux.py` 的归因层（判定顺序不变）。

内核给的三条事实（本机 CachyOS 实测）：

| 事实 | 实测数据 | 后果 |
|---|---|---|
| journal 常常没有 "Wakeup source" 行 | `journalctl -b -k` 无命中（ACPI 固件不给） | 走 IRQ 号 |
| `pm_wakeup_irq` 只给 IRQ 号；x86 上 PCI 设备的 PME 唤醒走 ACPI SCI | IRQ 9 → `/sys/kernel/irq/9/actions` = `acpi` | ** IRQ 号认不出任何设备**，旧代码归 `other-device` 直接 skip |
| 有线卡的中断名是**网卡名**不是驱动名 | `180: IR-PCI-MSIX-0000:2d:00.0 0-edge enp45s0`（驱动 r8169）；无线是 `iwlwifi:queue_N` | 旧代码只匹配无线驱动名，有线唤醒永远归因不了 |

补的两层（顺序固定，只填空、不重排）：

1. **IRQ → 设备**：`/proc/interrupts` 该 IRQ 行里的 action 名 + PCI 地址，后者对到
   `/sys/class/net/*/device`（`phy80211` 判 wifi/有线，不靠驱动表）；驱动名表
   （新增 `WIRED_DRIVERS`）只是兜底。
2. **每设备唤醒事件计数**：`/sys/.../power/wakeup_last_time_ms` 是 CLOCK_MONOTONIC 毫秒
   （内核 ABI 文档，suspend 不计时，事件在 resume 后落账——实测与 Python `time.monotonic()*1000`
   同基准：`now=8378323ms`，wlan0 事件 `last_ms=4078414`，age 4300s，窗口外正确不认）。
   取 `WAKEUP_EVENT_WINDOW_S=300s` 内**最新**者为归因（judge 每分钟跑一次，5 倍裕量；旧一次睡眠的
   事件被窗口挡住）。USB 网卡的计数器在父 USB 设备上（`4-2:1.0` 没有、`4-2` 有），只往上一层——
   再往上就是 xHCI 控制器，会把别的 USB 设备的唤醒错记到网卡头上。这条链只在 journal/IRQ
   **没认出设备**时才说话（`unknown` 或 `other-device(irq:N)`），journal 明确点名 usb/hid 时不覆盖。

边界仍然存在（写进 README 限制清单）：USB 网卡没启用唤醒（`power/wakeup` 不存在）时依旧归因不了；
计数器把「这台机器上次被这块卡唤醒」记到当前会话头上（窗口内），方向保守。测试
`tests/test_linux_wakesource.py` 24 项，样例全部取自本机 `/proc/interrupts` 原文。

**追加（同日 09:15，真机 `judge -v` 实锤）**：归因链还没轮到说话，先被 `guarded:media-playing`
拦住了。`_sound_server_streams` 的 pactl 分支里 `or "Corked: no"` 是错的：桌面应用（浏览器标签、
通知服务）常年挂着一个 `State: IDLE, Corked: no` 的空闲流，按 PipeWire 自己的语义这不是「在放」，
该子句却把它当成了「在放」——正是第 8 节 ALSA `RUNNING-forever` 的同款症状换了个入口复发。
最小修法：只认 `State: RUNNING`（RUNNING 流照常命中，方向不变），补
`test_an_idle_stream_with_an_open_sink_is_not_playing`。

## 13. 配置单位统一成秒 + 出厂默认改为「除键鼠/魔法包外都回睡」（2026-10-16）

**需求原话**：任何情况下都回睡，除了键鼠唤醒和魔法包唤醒；不管有没有键鼠输入、音频、远程控制，
也不管归因不明的唤醒，一律 10 秒回睡。

**改动一：分钟制键改名成秒**（5 个）：`idle_minutes`→`idle_seconds`(120)、`unknown_idle_minutes`→
`unknown_idle_seconds`(10)、`repeat_minutes`→`repeat_seconds`(120)、`min_gap_minutes`→
`min_gap_seconds`(30，2026-10-16 下午应用户要求由 300 改 30：去抖档不再按人在的保守假设，链路类唤醒 30 秒后即可再睡；测试里的保守档改为显式 Params(min_gap_seconds=300) 钉住)、`min_gap_unattended_minutes`→`min_gap_unattended_seconds`(60)。
同步点：config.py 字段/YAML_ORDER/注释/validate/params 桥、decide.Params 字段与全部
`*60` 换算点（_sleep_blocked 的 `needed_seconds`、_finish 的 debounce）、cli.py 的
doctor 打印/install 传参（`_timer_interval` 新助手，注册定时器仍是 1 分钟粒度）、
doctor/judge/install 的 `--idle-seconds`/`--unknown-idle-seconds` 旗标、两份 README。
**兼容**：`Config.load` 的 `_LEGACY_KEY_ALIASES` 把旧键 ×60 换算读入（新键优先于旧键），
老配置文件不用手改。

**改动二：出厂默认改成新策略**：四个看门狗全关、`resleep_break_on_input: false`（严格模式；
键鼠"唤醒本身"的会话豁免不受影响，豁免在 strict 检查之前）、`unattributed_wake_policy:
resleep-if-unattended`、`unknown_idle_seconds: 10`。config.default.yaml 改为**由
`Config().to_yaml(_YAML_HEADER)` 直接生成**（模板与代码逐字一致，由
`test_the_template_keeps_the_generated_comments_and_order` 锁定）。

**边界（不改）**：`other-device`/`timer`（RTC、USB 设备）唤醒仍设计为永不回睡
（decide.py 第 6 层，`magic_only` 也不改变它）；判定的「10 秒回睡」实际落点是唤醒后
10 秒 ~ 10 秒+repeat 之间的第一次 judge。

**教训**：本机 shell 可能以 root 跑测试，而测试套件不是 root-safe（sensor 产物在 euid==0 时
正确地不带 sudo、文件属主判定不同）——那不是回归。判定回归必须固定 uid 对照；本机用
`os.geteuid` 模拟 1000 的 runner 复现非 root 环境，diff/HEAD 双向跑平。

**验证**：非 root 模拟环境 581 项全绿（diff）vs 554 项（HEAD，差的 27 项是本轮新增测试）。


## 14. force_suspend：桌面睡眠锁让机器永远睡不下去（2026-10-16）

**实测（Mint 桌面）**：judge `--apply` 判定 `action=sleep` 后机器纹丝不动。两层原因：
① `LinuxSystem.suspend()` 的 inhibitor 预检发现 csd-power / workbuddy / NetworkManager 持着
sleep 阻塞锁就拒绝执行——而**正常桌面这些锁永远在**，与 guard_display_in_use 的
CrossDesk 教训同构（环境噪音被当成"有人在用"）；② 即使绕过预检，logind 也会因
block 锁拒绝 `systemctl suspend`，而旧代码 `_run` **不查返回码**，静默报告成功。

**改动**：新增 `force_suspend`（默认 true，符合"任何情况都回睡"）——跳过预检；
`systemctl suspend` 失败时回退写内核 `/sys/power/state`（logind/inhibitor 都拦不住，
需要 root，judge 有）。`_suspend_with_marker` 传递该开关（`_inhibitors` 存在性判定，
Windows/macOS 不受影响）；SLEEP-FAILED 现在同时进 journal.log（以前只上 stdout，
这正是这次排查绕弯的原因）。测试 5 项（tests/test_platform_layer.py）。

**注意**：内核直写绕过 systemd-sleep 钩子（不锁屏、无 NetworkManager pre-sleep 动作），
无人值守场景可接受。

## 15. strict 模式吞掉了键鼠唤醒豁免（2026-10-16）

**现象**：操作者键鼠唤醒后机器仍回睡，且每约 2 分钟一次。decisions.log 11:42:30 实录：
`de+input-ignored-by-strict-mode via=AT Translated Set 2 keyboard@116s-before idle=26s win=in@evdev`
——窗口内确有输入（`win=in@evdev`），但 strict（`resleep_break_on_input=false`）直接落空，
键鼠唤醒豁免（wake-by-input → 锁 `session_cause="input"`）从未触发，链路看门狗随后按
NM 陈旧日志噪音（`reconnect-event-after-sleep`，wlan0 实为 not-associated）反复回睡。

**根因**：`decide()` 第 3 步把「窗口内输入打断」和「唤醒本身由输入引起」两个事实捆在一起处理。
strict 模式本应只忽略前者（Params 注释 148-149 行写的就是这个语义：「only the WAKE being caused
by a keypress/mouse exempts a session」），但实现里 strict 时整步短路，连唤醒成因豁免也一并吞掉，
导致 cli 的 `write_session_cause("input", ...)` 永远走不到。

**修法（最小改动，只动 `decide.py` 第 3 步）**：strict 时仅当 `wake_by_input` 为假
（输入只在窗口内、不在唤醒瞬间）才落 strict_note；`wake_by_input=True`（evdev 硬件时钟说
按键落在唤醒瞬间，且窗口时钟未作否定）照常返回 `wake-by-input` → cli 照常锁会话。
窗口时钟优先语义不变：`window_input` 显式为 False 时仍以窗口为准（45 秒预唤醒宽限内的
按键属于 NIC 唤醒前的输入，不豁免）。

**测试**：`TestStrictMode` 增 2 项（at-wake 豁免保持 / 窗口时钟权威），套件 24 项全绿；
全量 588 项，余 6 失败为 root 沙箱既有假失败（sudo/属主敏感的安装与 unit 测试），与本次无关。

**遗留**：`who=reconnect-event-after-sleep` 读到 NM 陈旧日志（wlan0 未关联仍报重连）的归因噪音
未修——本修复后键鼠唤醒在第 3 步即豁免、不再走到链路看门狗，故不再致命；列第 9 节顺带发现。

## 16. 魔法包唤醒的第二个证据源：nl80211 的 WoWLAN 报告（2026-10-16）

**背景**：第 14 节把"魔法包链路"验收为"能收到包就豁免"，但留下一个物理缺口——睡眠中唤醒主机的
那一个包被网卡固件消费掉，主机侧 socket 永远看不到它（只有回醒之后到达的重发包/证据波能被记到）。
操作者的手机 App 只允许一次性发包，所以"单发一个魔法包 → 保持醒"这条最真实的用法一直失败
（实测 13:31:05 醒、13:31:16 被快速路径睡回去）。后续讨论中他先否掉了"只武装 magic-packet 过滤器 ⇒
计数器命中即魔法包"的方案——**理由是这块网卡即使只开魔法包也会异常唤醒（这个项目存在的起因本身）**，
所以计数器命中不等于魔法包。这个否决是对的，且他自己的日志就证明得了：
`journal.log` 里 10:27–12:31 多次 `collect kind=wifi-nic who=wakeup-event:wlan0@1s` 发生在还没开始发
魔法包的时段；13:32:19 那次明明是键盘唤醒，计数器仍报 `wlan0@40s`。

**转机**：`sudo iw event -t -f` 抓到了两条 verbatim 记录（2026-10-16 14:04:51 / 14:05:20）：

```
1792130691.782796: wlan0 (phy #0): WoWLAN wakeup
        * magic packet received
        * packet (might be truncated): ff:ff:ff:ff:ff:ff 96:56:06:9b:a8:24 ... c0:a8:00:8c c0:a8:00:ff
          a4:07 00:09 00:6e ... ff:ff:ff:ff:ff:ff 28:d0:ea:27:40:0c 28:d0:ea:27:40:0c ...
1792130720.783661: wlan0 (phy #0): WoWLAN wakeup
        wakeup not due to WoWLAN
```

第一条是操作者手机（192.168.0.140）发来的真魔法包（截断帧里能看到广播目的、UDP 9、6×FF + 本机 MAC 重复）；
第二条是机器被**非无线原因**唤醒——固件自己说的。内核依据：无线驱动把唤醒原因从固件问出来后经
`ieee80211_report_wowlan_wakeup()` 上报，归一化成 `struct cfg80211_wowlan_wakeup`
（`magic_pkt` / `disconnect` / `gtk_rekey_failure` / `rfkill_release` …）；`iwlwifi` 的 mvm 与 dvm 都实现了
（`iwl_mvm_report_wakeup_reasons()`，`IWL_WOWLAN_WAKEUP_BY_NON_WIRELESS` → 报告为空，`iw` 就打印
"wakeup not due to WoWLAN"）。nl80211 头文件原文即此语义：*"If it is not present in the wakeup notification,
the wireless device didn't cause the wakeup but reports that it was woken up."*

**这是"通用方案"的正确落点**：不解析任何厂商私有字符串，读的是内核结构体经 `iw` 统一渲染的那一行，
和 `wlan-events.log` 里已有的链路事件同一个协议层。**覆盖范围仍受驱动实现影响**（Intel 报得全，
部分驱动只报一部分；有线网卡没有这套接口），所以文档按"可插拔 + 读不到就降级"来写，不宣称全平台通用。

**实现（最小改动，不动判定顺序）**：
- `paths.wowlan_event_log()`：独立文件 `wowlan-events.log`（与链路日志分开，避免污染
  `wifi_moves_around_resume` 的读者）。
- `iwevent.py`：`IwEventWatcher` 增加 `wowlan_file`；读取线程改为**直接读 fd、自己缓冲字节**，
  遇 header（`WoWLAN wakeup`）就开一份"待收报告"，其后所有不带时间戳的行都归它，直到出现带时间戳的行
  或 0.5s 内不再来数据为止（绝不吞掉那个带时间戳的事件）；分类为 `magic` / `not-wowlan` / `other:<word>`，
  写一行 `kind=… epoch=… detail=…` 并 `take_wowlan()` 交给传感器。纯函数 `classify_wowlan` /
  `parse_wowlan_row` / `read_wowlan_log` / `last_wowlan` 可单测。
  *踩坑（第一版就是这么错的，记下来）*：先用 `readline()` 读、再用 `select` 查 fd 是否还有数据——
  `printf` 的整块输出早已被 Python 文本包装器读进用户态缓冲，`select` 只会说"没有数据"，
  于是每条报告都掉成 `kind=other`：日志看着像在干活，魔法包却仍会被回睡。必须绕过包装器读 fd。
- `cli.py`：传感器主循环在 resume 之后 drain 报告，`_record_wowlan()`：`kind=magic` 走**与听到包完全相同**
  的两个文件（`packet-last.txt` + 立刻锁 `session-cause=asked-for`），于是引擎不需要任何新规则、新 kind，
  第 1 层原样生效；`not-wowlan` / `other` 只写一行 journal，绝不豁免。
- `system/linux.py`：`wake_source()` 先问 `_wowlan_says_not_wireless()`（读 `wowlan-events.log` 最新一条，
  窗口 `WAKEUP_EVENT_WINDOW_S=300s`），为真则 `_wakeup_event_nic(ignore_wifi=True)`——**否掉计数器对无线
  网卡的指认**（有线候选仍可用，仍归因不了就 `unknown`，走原策略回睡）。

**测试**：`test_wake_listeners.py` +8（含 verbatim 两条记录的分类、续行块读取、块后面那行仍是事件、
只读行不碰链路口志、日志行 round-trip 与窗口）；`test_linux_wakesource.py` +7（`ignore_wifi` 只掉无线候选、
`not-wowlan` 才生效且过期作废、缺文件/读不到都不算否认、flow 里 `ignore_wifi=True` 且 `who` 不指网卡）；
`test_session_exemption.py` +4（magic 报告锁会话、`not-wowlan`/`other` 什么都不做、报告经 packet 文件端到端走到第 1 层）。全量 607 项，
余 6 失败是 root 沙箱既有假失败（安装/unit/端口），与本次无关。

**生效方式**：传感器是常驻 system unit，必须重启才吃到新代码：
`sudo systemctl restart autoresleep-wol-sensor`。judge 每次新进程，无需重启。

**待实机确认**：`iw event -t`（无 `-f`）是否也打印 `* magic packet received` 那一行——`-f` 按 `iw --help`
只影响 auth/assoc 的整帧打印，理论上无关；传感器用的是 `-t`，所以要等一次真魔法包唤醒后看
`~/.local/state/autoresleep/wowlan-events.log` 是否出现 `kind=magic`。若为空则给 tap 加 `-f`。

## 17. 唤醒原因通道：配置开关、验证脚本、平台边界（2026-10-16）

第 16 节把 Linux 的 WoWLAN 通道做完之后，本节补三件事：**可关**、**可自证**、**写清边界**。

**配置开关 `wowlan_wake_reason`（默认 true，Linux）**：`paths.wowlan_event_log()` 是这条通道唯一的
落点，所以"关掉"必须让**写**和**读**同时停：

- `iwevent.wowlan_tap_target(enabled, log_file)` 一处决定 tap 目标；`cli.py` 的传感器用它
  → 关掉时 `IwEventWatcher(wowlan_file=None)` 既不写 `wowlan-events.log`、也不把报告交给主循环；
- `LinuxSystem._wowlan_says_not_wireless(enabled=...)` 收一个显式开关（保持 staticmethod：既有的
  直接调用测试不受影响），`wake_source()` 传 `enabled=self.wowlan_wake_reason`；
- 平台对象通过 `System.__init__(config=None)` / `get_system(platform, config=...)` 拿到配置，
  `cli._system()` 在构造处统一挂上（读不到配置就当出厂默认，绝不让一个坏配置文件挡住一次判定）。
- 关掉后行为与第 16 节之前逐字一致：不再有报告落盘、不再有 `not-wowlan` 否认、魔法包仍需补发证据波。
  **它只会少一个豁免来源，不会让本来不回睡的会话变成回睡。**

**验证脚本 `tools/verify_wowlan_chain.py`（七段，5 秒，不睡眠、不绑 9/7、不动真的 `iw`）**：
① 用逐字的真实 `iw` 输出验分类（`magic` / `not-wowlan` / `other:disconnect`）；② 假 `iw` 子进程 →
真读线程 → `wowlan-events.log`；③ `kind=magic` → `packet-last.txt` → 引擎第 1 层 → `skip wake-requested`
+ 会话锁；③b 反面（`other` 什么都不做）；④ 否认时无线计数器失去指认权（假计数器：wlan0 wifi +
enp3s0 eth，`ignore_wifi=True` 只剩 eth）；⑤ 开关关掉后报告不被读、tap 目标为 None、线上有魔法包也
不落文件；⑥ 读**已安装**状态目录的最新报告（只读）；⑦ 打印真机那一轮与 Windows/macOS 对应命令。
本机实跑：`ALL CHECKS PASSED`。

**教程 `docs/verify-wowlan.md`**：三层验证（L1 脚本 / L2 真机抓原始报告 / L3 真机全链）、判读表
（含"驱动不上报""`iw event` 不带 `-f` 只打印 header""机器压根没醒"三种失败各怎么办）、
开关的操作方式、以及 Windows/macOS 的替代做法。

**平台边界（写进 README §3g 与 §8，别再让人以为三个平台都有）**：设备级归因（谁）三平台都有；
原因级归因（为什么）**只有 Linux** —— Windows 的 Power-Troubleshooter 事件 1 只有设备名（没有
"魔法包 vs 模式匹配包"这个字段，官方事件结构如此），macOS 的 `pmset` 只有硬件码
（`ARPT`=无线节点 / `GLAN`=有线节点 / `EHC`=USB / `RTC`=定时）+ 粗类别 `Network`/`USB`/`Alarm`。
两平台能做到的是在驱动/OS 侧收紧（关 `Wake on Pattern Match` / `sudo pmset -a womp 1`）。

**顺手修的一处 macOS 归因缺口**：`MacSystem.wake_source()` 原先只认 `SOCKS` 里的名字，
`due to GLAN/Network`（Apple 有线节点）落到 `other-device`，而 `other-device` 设计上永不回睡
→ 有线 WoL 唤醒的 Mac 会一直醒着。现在 `WIRED = ("GLAN", "Ethernet", "以太网")` 判成 `eth-nic`
（无线集合先匹配，`en0` 这种在 MacBook 上是 Wi-Fi、在 Mac mini 上是网线的名字保持不变），
另加 `RTC (Alarm)` 的 `alarm` 一起判 timer。5 项新单测（fixture 驱动，未在真 Mac 上跑）。

**测试**：全量 616 项；本沙箱（HOME=/root）下 6 项 sudo/属主敏感的既有假失败，其余全绿；
`tools/verify_wowlan_chain.py` 七段全过。
