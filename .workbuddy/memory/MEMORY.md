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
