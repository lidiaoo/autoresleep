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
- [x] S0.8 处理 S1 之前的「未提交工作」——**已提交**（`04bdde2`），见 S5.6
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
- [x] S6.9 「已存在先删再建」：**代码已改**（`Artifact.prepare_cmd` + `_apply_artifact`）。三个平台的
      「就地覆盖」都会静默留下旧东西：`schtasks /Create /XML /F` 留旧主体/触发器/ACL、`launchctl bootstrap`
      对已加载的 label 直接失败、`systemctl enable --now` 不会重启已在跑的单元（改了 unit 主体也不生效）。
      现在每个产物先 best-effort 拆除再重建，「没有可删的」记 `[note]` 而不是 `[failed]`（第一次装本来就是这样，
      把正常情况喊成失败会训练人忽略这个词）。实测（本机）：三个任务都是
      `schtasks /Delete /TN \AutoReSleep\... /F` → `schtasks /Create ... /XML ...`。
      顺带修两处真 bug：macOS 的 `launchctl` 目标原来写成 `gui/$(id -u)`（无 shell 的 argv 里永远不会展开，
      现在是真实 uid）；Linux/macOS 的 `prepare_cmd` 差点写成扁平 argv（照那样会去执行 `s`、`u`、`d`），
      单测抓到并加了防护 + 断言。**真机那半（重复 install 无残留、ACL 干净）仍在 S7.6**
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
- [ ] S7.3 真键鼠 —— **交给用户测**（他测）：窗口内动一次鼠标/键盘，两种开关各看一遍
      `decisions.log`：`break=true` → `skip wake-by-input who=input-in-window`；`break=false` →
      `SLEEP … who=…input-ignored-by-strict-mode`（明知有人仍睡，日志必须写明）
- [ ] S7.4 真网卡类唤醒 → 窗口过后回睡、记录实测耗时 —— **交给用户测**（他测，要真的睡这台机器）
- [x] S7.5 提权（**真机实测 7 轮**）：普通用户跑 `install` → **一次** UAC，子进程在
      `elevated-install.progress` 里逐条带时间戳记录（`start install as=elevated` / `step …` / `done rc=0`），
      父进程边等边念；任务先删再建 + 防火墙 + 监听器重启全在这一手里。收尾那轮：3 个任务的
      `[ok] delete`→`[ok] create`、防火墙 5 条规则、退出码 0。
- [x] S7.6 重复 `install` 无残留（**真机实测**）：judge 任务定义重复安装前后**逐字一致**（只有
      `StartBoundary` 变），触发器仍是 1×TimeTrigger + 1×EventTrigger；防火墙每条规则从**堆到 4 条
      回到 1 条**。install 收尾还会问 OS「9/7/45678 归谁」：`port 7/9/45678 held by pid 31824`、
      `67/68 by pid 4368 (the DHCP client - expected)`。
      这一轮顺手挖出并修掉四个真 bug：① `netsh add rule` 对同名规则不报错、会再叠一条，而
      `if _run([...show...]).strip()` 永远为真（没匹配也打印非空提示）→ 加失败也报成功；改成先删再建 +
      按**分隔行**数数核对（`uninstall` 侧的「只删一条再查名字」会永远认为没删掉、规则留在原地）。
      ② 任务带 `<RunLevel>HighestAvailable</RunLevel>` → 监听器跑在高完整性级别，普通用户
      `taskkill /F` 只会得到「原因: 拒绝访问。」，而旧代码丢掉返回码照样打「已停止上一个监听器」→
      孤儿监听器攒到**三个进程同时绑着 UDP 9/7/67/68**、lock 指向的 pid 却早已退出。改成把 pid 交给
      **提权子进程**（`--stop-pids`，答案走参数；子进程逐条记进度）。
      ③ `SO_REUSEADDR` 在 Windows 上语义相反（允许别人一起绑、谁收到包不由我们定）→ 换
      `SO_EXCLUSIVEADDRUSE`（POSIX 侧不设 reuse），第二个进程绑失败并由 `advice()` 讲清原因；
      顺带修了 bind 失败时 socket 不关闭的 fd 泄漏。
      ④ install 现在按 pid 归属核对端口（`netstat -ano -p UDP` / `ss -lunp`），不是新监听器就
      `[failed]` + 非 0 退出——期间还抓到自己的一个错：查询时忘了把 marker 端口算进去，导致每轮都
      误报 `nothing holds UDP 45678`（已加回归测试）。
- [x] S7.6b `uninstall` 的对称那一半（**真机实测**）：一次 UAC → 子进程先停监听器（`stale listener
      pid 1760: stopped`）→ 三个任务 `[ok] schtasks /Delete`（含 SYSTEM 的 collector）→
      `removed: AutoReSleep-WoL-UDP-9, -7, -67, -68, -45678` → `done rc=0`，退出码 0。
      独立复核：`AutoReSleep` 任务 **0 个**、`schtasks /Query /TN '\AutoReSleep\'` 空、
      每条规则 **0 条**、无 pythonw 进程、UDP 9/7/45678 无人占、lock 已删；
      **config.json 与日志保留**（没加 `--purge`）。随后 `install` 装回：退出码 0、
      新监听器 12208 独占 7/9/45678、每条规则回到 1 条。
- [x] S7.6c **常驻监听器被 ExecutionTimeLimit 杀死**（真机实测，本轮最严重的一个）：
  任务 XML 默认写 `<ExecutionTimeLimit>PT10M</ExecutionTimeLimit>`，Task Scheduler 在**启动满
  10 分钟时把任务连同进程终止**（`上次结果: 267014` = `0x41306`「任务被终止」；pid 31824 于
  08:53:12 启动、最后一次心跳 09:02:49、10 分钟整；随后 `doctor` 能绑上 9/7/45678 才暴露它）。
  即「装完/登录后十分钟，包链静默死掉，直到下次登录」。
  已改成监听器 `PT0S`（无限制），judge/collector 保留上限；`test_the_resident_listener_has_no_time_limit`
  与 `test_the_oneshot_tasks_keep_a_runaway_limit` 两个断言钉住；真机重装后 XML 已是 `PT0S`，
  **并盯着新监听器跨过门槛**：pid 12208 于 09:23:56 启动，09:35:44（11.8 分钟后）仍存活、
  心跳持续（最后 09:35:35）、UDP 9/7/45678 仍归它 —— 修之前那个进程正好死在 10 分钟整。
- [x] S7.6d 未提权时的措辞（**真机实测**）：`install --no-elevate` 逐项打印
      `apply: deferred to the elevated step`（清单里点了 SYSTEM 的 collector），并给出可手敲的完整命令
      （`… install --elevated --progress …--stop-pids 12208`），最后 `[error] the privileged step
      did not finish; the resident jobs were not started` —— 没有假装成功。
      同一次输出还顺带印证了 pkt 的 errno 修复：防火墙那段提示是
      `marker port 45678 is held by another process (another listener?) … this is expected while the
      daemon runs`（健康状态不再被喊成「包链分辨不出唤醒来源」）。
      剩下的「非管理员查 SYSTEM 任务」是 Windows 自己的「拒绝访问」，不是我们的措辞问题。

Linux（另一台机器）

- [ ] S7.7 `doctor` / `judge` / 窗口判定同上（键鼠走 evdev，需要特权传感器在跑）
- [x] S7.8 `Linger=no` 的限制写进 README 限制清单 —— 并到 S8.3 做完了
- [ ] S7.9 提权：有桌面 → `pkexec` 图形框；纯 tty → `sudo` / 打印命令；system 单元真的起来且 9/7 被绑

macOS（这台机器我没有，只能靠你）

- [ ] S7.10 `doctor` / `judge` 输出与 Linux 一致
- [ ] S7.11 提权：`osascript` 认证框；`socketfilterfw` 那步成功
- [ ] S7.12 窗口内键鼠检测（`HIDIdleTime` 两次采样）校准；USB 回放会不会被误判成人（见 S4.2 的已知代价）

---

## S7 现场命令（Windows 真机上的都跑完了；下面只剩「要真的睡一次」的两项交给用户测）

```bash
# ===== 已经做完、留档备查的部分 =====
# 0) 状态对照
python autoresleep.py doctor
schtasks /Query /TN '\AutoReSleep\' /FO LIST | head -20

# 1) S7.2 包链（随时可跑，动不了任何东西）：私有端口 + 临时数据目录
python tools/verify_packet_chain.py

# 2) S7.5/S7.6 提权往返（已实测 7 轮）：一次 UAC、子进程逐条记进度、任务先删再建、
#    防火墙每条规则 1 条、监听器独占 7/9/45678、退出码 0
python autoresleep.py install
type "%LOCALAPPDATA%\AutoReSleep\elevated-install.progress"
python autoresleep.py uninstall     # 已实测对称：任务 0 个、规则 0 条、无 pythonw、lock 已删

# ===== 交给用户测的两项（都要真的睡这台机器）=====
# 第 0 步：把窗口调大一点，好看清「窗口内」这件事（默认 10 秒，改成 30 秒）
python autoresleep.py config --set resleep_delay_seconds=30 --set magic_only=false
python autoresleep.py config          # 确认 resleep_delay_seconds / resleep_break_on_input 的值

# 第 1 步（S7.4）：让机器自然睡一次（你自己按电源键 / 合盖 / 等它睡），然后让【网卡】把它捅醒
#   （WiFi 重连、插网线、或从另一台机器发个魔法包以外的包——只要不是键鼠唤醒的就行）
type "%LOCALAPPDATA%\AutoReSleep\decisions.log" | tail -8
#   期望看到：window-open …；随后要么 wake-requested（魔法包/标记包 → 不回睡），
#   要么 NON_MAGIC_* + action=sleep（非魔法包 → 窗口过后回睡），行尾的 win= 就是实测窗口耗时

# 第 2 步（S7.3，默认开关允许打断）：再睡一次，这次用【键盘或鼠标】唤醒，唤醒后 30 秒内动一下鼠标
python autoresleep.py config --set resleep_break_on_input=true
type "%LOCALAPPDATA%\AutoReSleep\decisions.log" | tail -5
#   期望：action=skip reason=wake-by-input who=input-in-window（有人动了 → 不回睡）

# 第 3 步（S7.3 严格模式对照）：把打断关掉，再睡一次、再用键鼠唤醒、再动鼠标
python autoresleep.py config --set resleep_break_on_input=false
type "%LOCALAPPDATA%\AutoReSleep\decisions.log" | tail -5
#   期望：action=sleep … who=…input-ignored-by-strict-mode（明知有人仍睡，日志必须写明）
#   注意：这一档是「严格模式」，不要在日常用；测完记得 set 回 true

# 看完的状态副证：%LOCALAPPDATA%\AutoReSleep\window-input.json（窗口内输入状态）、journal.log（心跳/恢复）

# ===== 还没有机器的平台 =====
# Linux（那台机器）：S7.7 / S7.9 —— doctor 对照、pkexec/sudo 提权、system 单元真的绑上 9/7
# macOS（那台机器）：S7.10–S7.12 —— doctor 对照、osascript 提权、HIDIdleTime 校准
```

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
---

## S9 配置面：YAML + 看门狗开关 + 首次运行提示（**已完成，真机验证**）

起因：用户真机测「网卡类唤醒 → 应当回睡」没触发。现场证据（decisions.log + journal.log）指向
`guards=display-in-use`：远控/串流工具 `CrossDesk.exe` 一直占着显示器电源请求，看门狗链因此每次
都跳过；而看门狗排在网卡归因链**前面**，所以永远走不到回睡。

- [x] S9.1 看门狗可开关：`guard_remote_in_use`(true) / `guard_display_in_use`(**false**) /
      `guard_media_playing`(true) / `guard_update_running`(true)。**关掉的不静默**：判定行写
      `guards=ignored:display-in-use[CrossDesk.exe]`；命中的写 `guards=display-in-use[CrossDesk.exe]`。
      真机验证：迁移完配置后 judge 下一轮即输出 `guards=ignored:display-in-use[CrossDesk.exe]`。
- [x] S9.2 「谁占着」要看得见：`powercfg /requests` 按位置解析 DISPLAY/SYSTEM/EXECUTION 段并取出持有者
      （`CrossDesk.exe` / `audio-stream`），写进判定行与 `doctor`。顺带修一个「假装干净」：非提权调用
      powercfg 只会得到「此命令需要管理员权限」，旧代码把「没有段落」当成「没有请求」——现在返回
      `?[powercfg needs administrator]`（judge 是提权跑的，doctor 通常不是）。
- [x] S9.3 配置格式改 **YAML**：自带 `miniyaml`（零依赖，只覆盖这个文件需要的子集：平坦映射、行内/块
      列表、注释、引号与转义），生成时带中文注释；旧 `config.json` 自动迁移为 `config.yaml`
      （旧文件改名 `config.json.migrated`，不删）。真机已迁移，原有设置原样保留。
- [x] S9.4 `install` 首次运行生成默认配置并**停下问**「已生成默认配置文件：… 是否继续执行 install？[Y/n]」；
      非交互（管道/EOF）按默认继续；`--yes` / `AUTORESLEEP_NO_PROMPT=1` 不问；**提权子进程永不提问**。
      真机验证（临时数据目录）：`config.yaml written   (defaults)` → 提问 → 读不到输入则继续。
- [x] S9.5 沙箱护栏：数据目录不是本机真实那份时（临时 `LOCALAPPDATA`、测试），**不**把机器上正在跑的
      监听器当成「待杀的旧监听器」。这是做 S9.4 时踩出来的：差一个 UAC 就会杀掉正在服务的 listener。
      默认目录用「空环境 + home」重算，避开沙箱自己设的那个变量。
- [x] S9.6 文档：README 判定顺序第 2 条改成「可开关的看门狗 + 点名持有者」；6b 配置节改成 YAML +
      4 个 guard 键 + 首次运行提示 + 迁移说明；状态表补 `config.yaml` / `config.json.migrated`。
- [x] S9.7 项目里留一份规范默认配置：`v2/config.default.yaml`（带中文注释，由 `Config().to_yaml()` 生成，
      单测断言两者逐字一致 → 不会各写各的）。`install` 在全新机器上**拷贝**它到数据目录
      （`paths.default_config_template()` → `data_dir()/config.yaml`），此后所有读写只认数据目录那份：
      真机验证 `cmp` 数据目录文件与项目模板**逐字节一致**（3212 字节），`config --set resleep_delay_seconds=42`
      只改数据目录那份（模板仍是 10.0）。数据目录那份已存在时 `install` 不覆盖（`_same_values()` 比较值，
      只有值真的不同才重写），手工注释得以保留；仓库缺模板时退回代码生成（装成包也能用）。
      `config.default.yaml` 已在提交里，真机那份也已换成模板拷贝。

## S10 反应速度（**已完成**）

- [x] S10.1 **窗口到期即判**：监听器在窗口（`resleep_delay_seconds`）到期后的**第一秒**就判一次，
      不再等 `fast_path_seconds`(30 s) 那个节拍 → 网卡类唤醒的回睡决定最坏延迟从「10 s + 最多 30 s」
      压到 ~10 s。**同一个窗口只判一次**：窗口状态留在磁盘上、`age >= delay` 永远为真，不去重就会
      变成每秒判一次（实测关心的成本：一次判定 ≈0.12 s，每秒一次就是 12% 一颗核心）。
      实现：`_fast_path_ready()` 返回 `(ready, via_window, stamp)`，新增
      `_fast_path_fire(cfg, idle_probe, now, fast_deadline, judged_window)` 记住"已判过的那个窗口"；
      测试 5 条（含"同一窗口不重复判""新窗口再判""窗口内不判"）。
- [x] S10.2 配置 vs 已注册任务的**漂移检查**：`repeat_minutes` 与执行时间上限只在注册任务时写进
      XML，改配置不会改任务。`doctor` 新增一段直接对照（真机实测：
      `judge repeats every PT2M, config repeat_minutes=2 -> in sync`；
      `judge execution limit PT10M (a one-shot; PT1M..PT10M is a sane cap)`），不一致时提示跑
      `install` 重建。测试 3 条（含"配置说 1 分钟 → 产物里就是 PT1M""产物带先删再建"）。
