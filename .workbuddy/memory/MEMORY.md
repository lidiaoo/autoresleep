# MEMORY.md — autoresleep 项目长期记忆

## 项目定位
跨平台（Windows/Linux/macOS）的「非自愿唤醒 → 回睡」工具。核心判定链在 `src/autoresleep/decide.py`（纯逻辑、
可单测），OS 相关在 `src/autoresleep/system/*`，计划任务/unit 在 `src/autoresleep/scheduling/*`。

## 本机（Windows）运行事实
- 状态/日志目录：`C:\Users\jianghh\AppData\Local\AutoReSleep`（= `%LOCALAPPDATA%\AutoReSleep`）。
  日志文件：`journal.log`（过程）、`decisions.log`（判定 + heartbeat）、`evidence-last.json`（每次唤醒的证据）、
  `session-cause.json`（会话级豁免锁）、`window-input.json`（回睡窗口内输入）、`last-forced-sleep.txt`（我们发起的睡眠）、
  `link-presleep.json` / `link-events.log`（睡前链路快照与变化）、`heartbeat.txt`。
- 计划任务：`AutoReSleep-listener`（常驻，每秒醒）、`AutoReSleep-judge`（每 2 分钟 + Power-Troubleshooter 事件）、
  `AutoReSleep-collector`（需管理员）。任务指向仓库的 `autoresleep.py`。
- **改了 listener 相关代码必须重启任务才生效**：
  `schtasks /End /TN "AutoReSleep\AutoReSleep-listener"` → `schtasks /Run /TN ...`（用户态即可）。
  judge 每次是**新进程**，改完立即生效；`python autoresleep.py doctor`、`judge`（不加 `--apply` 即 dry-run）可随时排查。

## 测试约定
- 跑法：`python -m unittest discover -s tests`（无第三方依赖；项目用的解释器是 `E:\ENV\Python\Python311`）。
- 注意：**直接执行单个测试文件会漏测**——`tests/test_windowwatch.py` 中间就有一个 `unittest.main()`，
  发现式运行才会跑它后面的类。
- 本机 Windows 基线**不干净**：约 80 项既有失败（50 项是某个用例污染 HOME/USERPROFILE 后级联出的
  "Could not determine home directory"，22 项是 Linux 专有的 `pwd` 模块，其余是缺外部命令）。
  **对比「失败清单」而不是看总数**：`git stash` → 跑一遍存清单 → `git stash pop` → 再跑 → diff。

## 设计约定
- 判定顺序即产品（见 `decide.py` 顶部注释）：要来的唤醒 → 看门狗 → 键鼠 → 网络移动 → 驱动字符串（只记录）→
  OS 归因 → 无归因不猜。改动必须保序，并在 `docs/resleep-window-plan.md` 的「实施记录」里写明理由与实测数据。
- 输入时钟只有两种可信度：`evdev`（Linux，硬件真相，可锁会话）与 `*-window-tick`（平台时钟**差值**，可锁会话）；
  `x11-idle` 是唤醒那一刻的兜底，**永不允许**锁会话（resume 会把它清零）。
- 平台差异要写进 README 的「限制清单」（中英两份），不要只留在代码注释里。
- **原因级唤醒判据只有一条可用**（2026-10-16 定）：cfg80211 经 nl80211 上报的 WoWLAN 报告
  （`iw event` 渲染成 `WoWLAN wakeup` + bullets：`* magic packet received` / `wakeup not due to WoWLAN`）。
  厂商**私有**的唤醒原因字符串永不参与判定；**不可以**用 sysfs 唤醒计数器推"魔法包"——实测这块网卡
  只开 magic-packet 也照样异常唤醒，计数器命中只说明"有无线活动"。有线网卡没有这套接口（覆盖范围看驱动）。
- 传感器把该报告记进 `wowlan-events.log`（`kind=magic` / `not-wowlan`）；`magic` 走与"听到包"完全相同的
  两个文件（`packet-last.txt` + `session-cause=asked-for`），所以引擎不加新 kind、判定顺序不变；
  `not-wowlan` 用于否掉计数器对无线网卡的指认。改传感器代码后必须 `systemctl restart autoresleep-wol-sensor`。
- **陷阱**：`iw event` 的报告是"带时间戳的 header + 不带时间戳的 bullets"，读块要**直接 `os.read(fd)` 自缓冲**；
  用 `readline()` + `select` 会把整块留在用户态缓冲里（症状：`kind=other`，日志看着一切正常 = 假绿）。

## 配置约定（2026-10-16 起）
- 时间项单位一律是秒：idle_seconds/unknown_idle_seconds/repeat_seconds/min_gap_seconds/
  min_gap_unattended_seconds（旧 *_minutes 键由 Config.load 自动 ×60 兼容读入）。
- 出厂策略 = 除键鼠唤醒、魔法包唤醒外都回睡（看门狗默认全关、strict、resleep-if-unattended）。
  例外：RTC 定时器与 USB 设备唤醒设计上永不回睡（decide.py 写死，配置改不了）。
- config.default.yaml 由代码生成，手改会被模板一致性测试打回；改默认值要改 Config 字段再重新生成。
- 在 root shell 跑测试会有 6 个环境假失败（sudo/属主/提权路径）；对照回归要固定 uid。
- 这条通道可关：`wowlan_wake_reason`（默认 true，Linux）。关掉 = 传感器 `wowlan_file=None`（不落盘、
  不进主循环）+ 判定不读报告（`_wowlan_says_not_wireless(enabled=False)`），行为与通道上线前逐字一致。
  它只会少一个豁免来源，不会让本来不回睡的会话变成回睡。

## 验证入口（先跑这个，别先怀疑代码）
- `python tools/verify_wowlan_chain.py`：5 秒、不睡眠，七段逐条 `ok/FAIL`（解析逐字比对 / 假 `iw` 子进程 →
  真读线程 → 落盘 / magic 豁免 / 反面 `other` / 否认只掉 wifi 候选 / 开关关掉无痕迹 / 读已安装目录）。
  教程 `docs/verify-wowlan.md`（含"驱动不上报""`iw event` 不带 `-f`""机器没醒"三种失败的对策）。
- 脚本必须**在改 env 之前**取"已安装数据目录"的路径，否则读到自己的临时目录；`/root/...` 可能不可 stat，
  要 try/except。

## 平台边界（2026-10-16 查证；别再以为三平台一样）
- 设备级归因（**谁**唤醒）三平台都有：Linux sysfs 计数器/PME/IRQ、Windows `WakeSourceText`+`wake_armed`、
  macOS `pmset` 的 `due to <CODE>`。
- 原因级归因（**为什么**醒）**只有 Linux**：Windows 事件里没有"魔法包 vs 模式匹配"字段；
  macOS 只有硬件码（`ARPT`=无线节点/`GLAN`=有线节点/`EHC`=USB/`RTC`=定时）+ 粗类别 `Network`/`USB`/`Alarm`。
  两平台的替代做法是驱动/OS 侧收紧（关 `Wake on Pattern Match` / `sudo pmset -a womp 1`）。
- macOS 的 `GLAN` 必须判成 `eth-nic`：落到 `other-device` 时引擎永不回睡（有线 WoL 唤醒的 Mac 会一直醒着）。

## 本沙箱（这台 Linux 开发机）的坑
- 不同 Bash 调用可能落在不同挂载视图：`/tmp` 内容、`git` 索引状态、甚至测试结果都不一样
  （同一份代码，一次跑 6 个失败、一次全绿）。**不要把临时目录建在 `/tmp` 再跨调用使用**；
  基线对比要在同一个命令里跑完，或把工作树建在仓库内。
