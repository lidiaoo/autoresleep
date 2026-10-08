# TODO —— 回睡窗口改造 + 三平台自提权

配套：[resleep-window-plan.md](resleep-window-plan.md)（设计）、[install-pitfalls.md](install-pitfalls.md)（提权规矩）。

用法：做完一项把 `[ ]` 改成 `[x]`，并在行末补上验收结果（命令 + 关键输出）；每阶段最后一条是
「本阶段单测全绿」，不绿不进下一阶段。

```bash
# 单测（每阶段收尾都跑）
cd v2 && python -m unittest discover -s tests -v
```

## 实施记录（2026-10-08/09，本机 Windows）

- 单测：**369 个全绿**（`OK (skipped=9)`；9 个 skip 是 Windows 上的 POSIX-pipe 用例，见 S5.7）。
  起点是红的：HEAD 在本机 Windows 上 18 errors + 3 failures（`os.geteuid` 与 evdev-pipe 用例），
  已在 S0.9/S5.7 修好 —— 「每阶段全绿」这条标准得从这个基线开始才有意义。
- 真机已验（只读 / dry-run，全部用重定向的临时数据目录，没动正在跑的那份安装）：
  S1（`config --set` 四个新键）、S2（`doctor` 把 `wake_nics` 解析成本机 9 张网卡中的 2 张）、
  S3+S4（`judge` 在窗口内给出 `reason=window-open`，判定行新增 `win=`）。
- **未做**：S7 的「唤醒→回睡」全链路（要真的睡眠这台机器）与 S6/S7 的真提权往返（要弹一次 UAC）——
  这两件都要你定时机；S8 的 README 还没改（等你过一遍再说）。
- 顺带发现并已修：`--grace` 默认值盖配置（S5.1）、`config --set` 配不了**带空格的网卡名**（S1.9）、
  `_write_root_artifact` 死代码（S6.6）、防火墙规则开给了错的二进制（S6.14）、判定的「陈旧窗口」风险（S3.11）。
- 风险提示：本机那份安装的计划任务指向**这个 checkout**，所以改动一落地，本机的 judge/listener 就在跑
  新代码了（判定行已经带 `win=`）。S7 的实测会受影响，做之前先说一声。

---

## S0 需求定稿与文档

- [x] S0.1 需求定稿（Q1–Q8）写进 `docs/resleep-window-plan.md` 第 1 节
- [x] S0.2 配置项定义（4 个新键 + 退役表）写进第 2 节
- [x] S0.3 判定流程与判定链新顺序写进第 3 节
- [x] S0.4 三平台落地要点（含「现在只认无线 MAC」这条）写进第 4 节
- [x] S0.5 提权分工与 pitfalls 逐条落地表写进第 5 节
- [x] S0.6 本 TODO 清单
- [x] S0.7 开放项按建议值实现：O.1 默认 `false`/`true`；O.2 口径 **(i) 只收网卡类**（timer / other-device 仍豁免）；
      O.3 **分工版**提权；O.4 做 `--no-elevate` + `AUTORESLEEP_NO_ELEVATE`；O.5 暂不补 install-design.md
- [ ] S0.8 处理 S1 之前的「未提交工作」——**未做**（见 S5.6，提交策略等你定）
- [x] S0.9 修基线红灯：`scheduling/linux.py` 的 `os.geteuid()` 在 Windows 上不存在（16–17 个 error）
      → 新增 `_is_root()`；验收：`test_scheduling_artifacts` / `test_wake_listeners` 在本机转绿

---

## S1 配置面（`config.py` / `cli.py`）

- [x] S1.1 `Config` 四个新字段（`wake_nics` / `magic_only` / `resleep_delay_seconds` / `resleep_break_on_input`）
- [x] S1.2 校验：delay ≥ 0、`wake_nics` 元素非空字符串、MAC 归一化、布尔量强制转换
      （不让手改的 config 崩掉一个只想决定睡不睡的 judge）
- [x] S1.3 `config --set` 支持字符串列表（`LIST_OF_STR_KEYS`）；验收：
      `--set wake_nics=AA-BB-CC-DD-EE-FF,wlp5s0` → `["aa:bb:cc:dd:ee:ff","wlp5s0"]`（归一化生效）
- [x] S1.4 旧配置兼容：只含 `idle_minutes` 的 config 能加载、取默认值、`judge` 不炸（有单测）
- [x] S1.5 `idle_minutes` 标注 deprecated（字段 docstring + `doctor` 里说明它不再决定等待时长）
- [x] S1.6 `doctor` 打印窗口段：delay / 两个开关 / `wake_nics` 原始值 + 解析结果 + 当前窗口状态
- [x] S1.7 单测：默认值 / 校验 / `--set` 字符串列表 / 旧配置兼容 / 多对 `--set` / 非法值不写文件
- [x] S1.8 **本阶段单测全绿**（当时 297 个）
- [x] S1.9 附带修：`--set` 支持 `--set a=1 b=2`（README 一直这么写，argparse 原本会退出 2），
      且 `wake_nics` 只用逗号分隔（Windows 网卡名带空格：`WLAN 4`、`以太网 2`）
      验收：`config --set "wake_nics=WLAN 4,以太网 2"` → `["WLAN 4","以太网 2"]`

---

## S2 网卡解析与 magic 匹配集合（`pkt.py`）

- [x] S2.1 `select_wake_nics()`：`[]` = 所有有线 + 所有无线；MAC 优先（归一化）、名字兜底（大小写不敏感，
      含平台的人类可读 label）；返回 `unmatched`（匹配不到本机的条目）
- [x] S2.2 magic 匹配集合改为配置解析结果（`wake_macs()` 取代 `wireless_macs()`，`Listener` 默认值同步）；
      验收：`doctor` 的 `magic MACs` 里出现**有线**网卡的 MAC
- [x] S2.3 解析不到任何网卡时：日志 + `doctor` 均 warn 并按「全部」继续（禁止静默失效）
- [x] S2.4 `doctor` 打印 `configured / resolved / magic MACs` 三段
- [x] S2.5 单测（`tests/test_wake_nics.py`，13 个）：`[]`=全部、MAC 花式写法、名字/label、未匹配上报、
      重复条目去重、本机真实网卡
- [x] S2.6 **本阶段单测全绿**（当时 310 个）

---

## S3 判定引擎（`decide.py`）

- [x] S3.1 `Evidence` 增 `window_input` / `wake_nic_policed`；`Params` 增三个新键；模块头判定顺序表同步更新
- [x] S3.2 链 3：窗口内键鼠 + break=true → `skip wake-by-input who=input-in-window`（锁存在 cli 侧）；
      `break=false` 不产生该判定（因此不锁存）
- [x] S3.3 链 4/6：网卡类走「窗口 + 门槛」；`eth-nic` 进判定（新增 `NON_MAGIC_ETHERNET_NIC`）；
      列表外网卡 → `wake-source-<kind>-not-policed`
- [x] S3.4 `magic_only=true`：网卡类不看 OS/驱动归因，直接候选；`fixed-feature-nic-suspect`
      在 `magic_only` 下也进候选（口径 (i)）
- [x] S3.5 归因不明仍用 `unknown_idle_minutes`；timer / other-device 仍 `skip`
- [x] S3.6 严格模式留痕：判定行 `who=...+input-ignored-by-strict-mode`
- [x] S3.7 锁存语义未动（`slept_total` / `session` 失效规则回归通过）
- [x] S3.8 单测矩阵（`tests/test_resleep_window.py`，22 个）：包 × 键鼠 × break 开关 × 网卡在不在列表 × magic_only
- [x] S3.9 单测：窗口到期、窗口内包/键鼠取消、无 watcher 退回空闲门槛、无会话戳退回旧时钟
- [x] S3.10 **本阶段单测全绿**（当时 330 个；`test_decide_attribution` 里 eth-nic 那条按新语义改写）
- [x] S3.11 附带修：**陈旧窗口**。窗口的结论只描述唤醒后那 N 秒；若机器在唤醒之后又被人用过
      （`idle_s < age`），旧结论失效、回到空闲门槛 —— 否则一段几小时前的会话会让机器睡在正在打字的人身上
      （这条是在本机那份实时安装上想出来的，不是理论风险）

---

## S4 窗口执行器与三平台输入检测

- [x] S4.1 唤醒锚点 = `evidence.observed_at`（会话戳），窗口 = 锚点 + delay
- [x] S4.2 窗口内输入检测：Linux 优先 evdev（特权传感器）；Windows/macOS 用平台时钟的**差值**
      （`windowwatch.observe`，只认变化）；单测覆盖 tick 变化 → `input=True`；macOS `HIDIdleTime` 仍需真机校准
- [x] S4.3 状态文件 `window-input.json`（锚点 / 截止 / ended / input / 首末采样 / 采样数）；
      listener 采样、judge 读（同一份文件，同一套路）
- [x] S4.4 listener 快路径门槛换成窗口（`_fast_path_ready`：有唤醒戳用窗口，没有退回旧门槛）
- [x] S4.5 evdev 的 `MOTION_SETTLE_S` 未动（窗口内照旧忽略 USB 回放的运动）
- [x] S4.6 单测（`tests/test_windowwatch.py`，20 个）：首次采样、tick 变化、稳定 tick、窗口后输入不算、
      换唤醒重开、坏文件、跨进程累积、CLI 黏合（含「没采样 ≠ 没人」）
- [x] S4.7 **本阶段单测全绿**（当时 347 个）
- [x] S4.8 `judge` 判定行新增 `win=`（`in` / `none` / `-` + 来源），`doctor` 打印窗口当前状态

---

## S5 清理与修 bug

- [x] S5.1 修 `--grace` argparse 默认值 60 覆盖 `config.json`（改 `default=None`，落到 `cfg.grace_seconds`）
- [x] S5.2 去抖接线：窗口判定在去抖之前；无人 + 网卡归因仍走 `min_gap_unattended_minutes`
- [x] S5.3 清理 3 个 0 字节文件 `cd` / `p` / `python`（已 `git rm`，删除已暂存）
- [x] S5.4 README 现状矩阵 / 限制清单更新 —— 并到 S8 做完了（矩阵补 4 行、限制清单补 4 条）
- [x] S5.5 全量单测绿（受影响断言已按新语义改写：eth-nic 进判定、`--grace`）
- [x] S5.6 提交上一轮未提交的工作 —— **已提交**：`04bdde2 watcher: bounded cost when a device floods`
      （README §3f / `inputwatch.py` / `test_input_flood_cost.py` / `tools/flood_cpu_proof.py`）；
      本轮改造在 `106154c`、文档在 `3a6cf76`。全部只提交到本地，没有 push
- [x] S5.7 附带修：日志 handler 每次写完即关闭（Windows 上句柄不放，临时数据目录删不掉，
      测试尾部会喷一段 traceback —— 绿色的运行里出现崩溃痕迹最容易盖住真失败）；
      同时把 9 个「用 `os.pipe()` 驱动 `poll()`」的用例在 Windows 上显式 skip（Windows 的 select
      不支持管道句柄，原来会「以错误的理由通过」）

---

## S6 三平台自提权

- [x] S6.1 内部子命令形态：`install --elevated --progress <file>` / `uninstall --elevated`（同一份代码，
      只用内部参数区分），**永不提问**
- [x] S6.2 行式带时间戳的 `elevated-<command>.progress`；父进程边等边念；连续 20s 无新行给指引
      （「UAC 提示可能在任务栏」/「被拒绝就重跑并选是」）
- [x] S6.3 成功/失败以**读记录**为准（`done rc=`）；读不到即判失败，并给出可手工执行的完整命令
- [x] S6.4 子进程显式收 `--config`（以及 `--no-firewall` / `--no-sensor`），不依赖环境变量
- [x] S6.5 Windows：`ShellExecuteW(None,"runas",exe,params,None,SW_HIDE)`（避开 PowerShell 三层引号），
      子进程 stdout/stderr 直接写进进度文件（pythonw 没有控制台）
- [x] S6.6 Linux：`pkexec` → `sudo` → 打印命令；子进程输出进进度文件，root 侧只做
      写 system 单元（直接写，不再 `sudo install` 中转）+ `daemon-reload/enable --now` + `ufw`
- [x] S6.7 macOS：`osascript ... with administrator privileges`（真机待验）
- [x] S6.8 卸载对称：删任务 / 删 system 单元 + reload / 删防火墙规则；用户级动作留在父进程
- [ ] S6.9 「已存在先删再建」+ 重复 install 幂等（无残留触发器/ACL）—— **待真机验证**（S7.6）
- [x] S6.10 未提权读 SYSTEM 任务时按 pitfalls §6 措辞（本机实测 `schtasks /Query` 返回「拒绝访问」，
      正是那条规矩描述的现象）
- [x] S6.11 `--no-elevate` + `AUTORESLEEP_NO_ELEVATE=1`：只打印命令、不动系统；并在 `elevate.delegate()`
      这个咽喉处兜底 —— **测试 / CI / 沙箱绝不允许在别人桌面上弹 UAC**
- [x] S6.12 源码级单测（`tests/test_elevate.py`，15 个）：提权模块 AST 里不得出现 `input/getpass`、
      两个 elevated 分支不得提问且必须逐步记进度、父进程必须靠读记录报失败
- [x] S6.13 **本阶段单测全绿**（366 个）
- [x] S6.14 附带修：防火墙规则的程序从 `sys.executable` 改成 **listener 真正用的解释器**
      （Windows 上是 `pythonw.exe`）—— 给一个永远不绑端口的二进制开洞等于没开，而且没有任何日志会说
- [x] S6.15 `install.cmd` / `uninstall.cmd` 删掉自带的 RunAs 块（否则会弹两次 UAC）；双击即用，
      一次提权，进度就在这个窗口里念

---

## S7 真机验证

Windows 本机（**等你说时机**：会真的睡眠 / 会弹 UAC / 会碰你正在跑的那份安装）

- [x] S7.1 `doctor`：新配置项、解析后的网卡集合（本机 9 张里解析出 2 张）、窗口值、窗口状态都打印
- [x] S7.2 真 magic 包：**已验**（`tools/verify_packet_chain.py`，私有端口 19090 + 临时数据目录，
      没起常驻守护——它的快路径会真的睡机器，也会抢你那份安装的 9/7）。结果：
      监听器 `kind=magic`（发往**有线** MAC 的包认出来了）、证据 `magic|epoch=..|src=127.0.0.1:50200|len=54`、
      判定 `action=skip reason=wake-requested src=pkt link=LINK_LOST who=magic@+0s`（链路证据在场也被包链压住）、
      锁存写入 `{"cause":"asked-for", ...}`
- [ ] S7.3 真键鼠（`break=true`）→ `skip wake-by-input`；`break=false` → `SLEEP who=input-ignored-by-strict-mode`
      —— **待做**：要真的按键唤醒 + 常驻守护在跑，等 S7.4 一起
- [ ] S7.4 非魔法、非键鼠的网卡类唤醒 → 窗口过后回睡；记录「唤醒 → 回睡」实测耗时 —— **待做（要真的睡这台机器）**
- [ ] S7.5 提权：普通用户跑 `install` → UAC 弹几次、进度是否被念出来、故意让某步失败能否看出死在哪
- [ ] S7.6 提权：`uninstall` 对称；重复 `install` 无残留；未提权时 SYSTEM 任务措辞正确

Linux（另一台机器）

- [ ] S7.7 `doctor` / `judge` / 窗口判定同上（键鼠走 evdev，需要特权传感器在跑）
- [ ] S7.8 `Linger=no` 的限制写进 README 限制清单（窗口退化为 2 分钟定时判定）
- [ ] S7.9 提权：有桌面 → `pkexec` 图形框；纯 tty → `sudo` / 打印命令；system 单元真的起来且 9/7 被绑

macOS（这台机器我没有，只能靠你）

- [ ] S7.10 `doctor` / `judge` 输出与 Linux 一致
- [ ] S7.11 提权：`osascript` 认证框；`socketfilterfw` 那步成功
- [ ] S7.12 窗口内键鼠检测（`HIDIdleTime` 两次采样）校准；USB 回放会不会被误判成人（见 S4.2 的已知代价）

---

## S8 文档

- [x] S8.1 README 新增「6b. 配置项」表（4 个新键 + 与 `idle_minutes` 的关系 + `config --set` 的写法）
- [x] S8.2 README 第 3 节判定顺序按新链更新：链 3 两个时钟、链 4/6「等窗口过完」、链 6 按 `wake_nics`
      管辖（有线与无线同等）、`fixed-feature-nic-suspect` 在 `magic_only` 下也进候选，并补「窗口就是等待」段
- [x] S8.3 README 限制清单补 4 条：窗口判定依赖 listener 在跑（Linux `Linger=no` 退化）、
      Windows/macOS 的 tick 差值分不清按键与 HID 回放（代价方向是「不睡」，出口是严格模式）、
      窗口结论只描述唤醒后那 N 秒、提权会弹一次 UAC（`--no-elevate` 可关）
- [x] S8.4 README 第 5 节新增提权段落：三平台各弹什么、父进程念子进程进度、子进程不提问、
      失败先读 `elevated-*.progress`、`--no-elevate` / `AUTORESLEEP_NO_ELEVATE`
- [x] S8.5 **原计划写错了**：`QUICKSTART.txt` 是 v1 的（在上级目录），v2 没有这个文件；
      v2 的入门就是 README 第 5 节（已更新：新命令示例、`--set` 写法、提权说明）——要不要给 v2 单独补一份随你
- [x] S8.6 落地结果已回填：本条 TODO 的实施记录 + plan 第 10 节（8 条计划外的决定/发现）；
      现状矩阵（README 第 4 节）补齐 `wake_nics` / 回睡窗口 / 窗口内输入 / 一键自提权四行

---

## 开放项（结论见 S0.7）

- [x] O.1 默认值：`magic_only=false`、`resleep_break_on_input=true`、`resleep_delay_seconds=10`
- [x] O.2 `magic_only` 口径 **(i) 只收网卡类**（timer / other-device 仍豁免）
- [x] O.3 提权形态：**只把特权动作交给提权子进程**（父进程做用户级那一半）
- [x] O.4 做了 `--no-elevate` + `AUTORESLEEP_NO_ELEVATE`（Linux 提权链 pkexec → sudo → 打印命令）
- [ ] O.5 `docs/install-design.md` 暂不补（pitfalls 里引用、本 checkout 里没有）—— 要的话说一声