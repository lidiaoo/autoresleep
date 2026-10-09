# install / uninstall 界面整理 + 二次调用菜单 —— 勾选清单

> 每个 `- [ ]` 自带验收命令。**每阶段单测全绿才进下一阶段**（`python -m unittest discover -s tests`）。
> 阶段 S1 之前不动代码：等 plan 第 3 节的开放项拍板。

## S0 定稿

- [x] S0.1 plan 第 3 节的 O.1–O.6 拍板（CLI vs 启动器 / 第 5 项 / 备份名 / uninstall 确认 / 成功 hint / 非交互多打一行）
      验收：`docs/install-menu-plan.md` 第 3 节的"我的建议"栏被改成"已定"或改写

## S1 已安装探测（`_installed_state()`）

- [x] S1.1 新增 `_installed_state()`：返回 `{jobs: [...存在的任务名...], config: Path|None, listener_pid: int|None, unknown: [问不到的平台项]}`
      验收：`python -m unittest tests.test_installed_state -v` 全绿（含 Windows/Linux/macOS 三套夹具 + "什么都没装" + "问不到"）
- [x] S1.2 Windows：`schtasks /Query /TN` 三个任务逐个问，返回码判在不在（复用 `_registered_task_settings` 的调用方式与 `proc_kwargs`）
      验收：夹具用真机导出的 `schtasks /Query /XML` 与「错误: 系统找不到指定的文件」两种情况
- [x] S1.3 Linux：`systemctl --user list-unit-files`（judge.service/judge.timer/listener.service）+ `/etc/systemd/system/autoresleep-wol-sensor.service` 是否存在；无 systemd → 该平台项计入 `unknown`
      验收：夹具用真 `systemctl` 输出（有/无 unit）+ 无 systemd 的报错文本
- [x] S1.4 macOS：两个 LaunchAgent plist 是否存在（path 用 `paths`/`scheduling.macos` 里已有的定义，不另写一份）
      验收：夹具用临时 HOME 造两个 plist，断言判定结果
- [x] S1.5 监听器存活：读 `listener.lock` 的 pid，用现有"pid 还活着吗"的代码判；锁在但进程没了 → `listener_pid=None` 且记一条 note
      验收：`python -m unittest tests.test_installed_state -v` 含「锁在/进程死」「锁不在」两个用例
- [x] S1.6 `doctor` 改用 `_installed_state()`（删掉它自己那份重复探测）
      验收：`python autoresleep.py doctor` 输出里的"注册任务"段与真机一致；全量单测绿

## S2 输出结构（默认精简 / `-v` 展开 / 失败必展开）

- [x] S2.1 新增 `-v` / `--verbose`（install / uninstall / doctor 共用；写进 `common()` 或各自的 parser）
      验收：`python autoresleep.py install --dry-run -v` 显示今天的全量细节；不带 `-v` 只有编号步骤
- [x] S2.2 install 输出改成「编号步骤 + 一行结果 + 收尾一句」，段落编号 1) 2) 3) 4)
      验收：`python autoresleep.py install --dry-run | findstr /r "^  [0-9])"` 得到 4 行；且**不含** `[ok] schtasks`
- [x] S2.3 提权进度：默认只显示最后一行，`-v` 逐行（今天的行为移到 `-v`）
      验收：真机 `install`（一次 UAC）默认输出里 progress 行 ≤1 行；`install -v` ≥3 行
- [x] S2.4 uninstall 用同一套编号（干掉 `2b`）+ 同一套精简规则
      验收：`python autoresleep.py uninstall --dry-run` 的编号是 1) 2) 3) 且无 `2b`
- [x] S2.5 失败路径：任何一步失败 → 完整细节 + 进度文件路径 + 补救命令，非 0 退出（保持今天的信息量）
      验收：`python autoresleep.py install --platform linux --dry-run`（伪造一个失败）与既有的失败单测
- [x] S2.6 标签统一中文 + 标识符英文（与 config.default.yaml 风格一致）
      验收：人眼过一遍 install/uninstall 全量输出；单测里断言段落标题文案

## S3 二次调用菜单

- [x] S3.0 **首次安装也给菜单**（状态行如实说"还没有安装"；`1)` 措辞为「安装」、第 3 项在首次
      就是"先生成配置再装"）。验收：`tests/test_install_menu.py`
      里"全新机器也能看到同一个菜单 / 回车即安装 / 第 3 项可用"三例
- [x] S3.1 `cmd_install()` 开头接闸门：已安装 + tty + 非 `--yes` + 非 `AUTORESLEEP_NO_PROMPT` + 非 `--elevated` + 非 `--dry-run` 才进菜单
      验收：`python -m unittest tests.test_install_menu -v` 里"六种不进菜单"的用例全绿
- [x] S3.2 菜单本体打印（配置文件/任务/监听器三行状态 + 四项），`1` 为回车默认
      验收：单测用假 stdin（mock `_stdin_can_answer` + `input`）断言打印内容与所选动作
- [x] S3.3 选择分派：1=覆盖安装、2=卸载、3=重生成配置后继续安装、4=退出、其它=重问（≤3 次）
      验收：`input` 序列 `["2"]`/`["3"]`/`["4"]`/`["9","x","4"]` 各一个用例，断言调用了对应函数且没有多余动作
- [x] S3.4 Ctrl-C / EOF → 什么都不做、退出码 0（与现有"首次生成配置时取消"一致）
      验收：断言 `KeyboardInterrupt`/`EOFError` 两种输入下的返回值与打印
- [x] S3.5 非交互时按 O.6 打印一行「已安装；要卸载请跑 uninstall」，然后照常重装
      验收：`echo "" | python autoresleep.py install --dry-run` 含该行且退出码 0
- [x] S3.6 菜单里显示的状态行与 `_installed_state()` 一致（不另算一遍）
      验收：单测断言菜单文案里的 pid/任务名来自同一个结构

## S4 「重新生成配置」

- [x] S4.1 新增 `install --regen-config`：备份 `config.yaml` → `config.yaml.bak`，再用 `config.default.yaml` 覆盖
      验收：`python -m unittest tests.test_regen_config -v`（含"没有现成配置时不做 .bak"".bak 被覆盖""dry-run 只打印"）
- [x] S4.2 覆盖后打印"手改的注释不会保留" + 旧文件路径
      验收：单测断言这两句
- [x] S4.3 `--regen-config` 之后继续走 install（第 1 步那行显示"已重新生成（旧文件 .bak）"）
      验收：`python autoresleep.py install --regen-config --dry-run` 输出里第 1) 步是这句话

## S5 启动器去重（4 个脚本）

- [x] S5.1 `install.cmd` / `uninstall.cmd`：删掉自己的 banner 与"计划做什么"清单，只留预检 + 单行招呼 + 转发 + 收尾/补救
      验收：`MSYS_NO_PATHCONV=1 AUTORESLEEP_NO_PAUSE=1 cmd /c "install.cmd --dry-run"` 输出里 `AutoReSleep` 只出现一次 banner
- [x] S5.2 `install.sh` / `uninstall.sh`：同样处理，三端文案对称
      验收：`AUTORESLEEP_NO_PAUSE=1 ./install.sh --dry-run --platform linux`（本机 MSYS 下会被守卫拦住 → 用临时副本 or 直接 `sh -n` + 人眼）
- [x] S5.3 收尾提示仍带平台相关文件名（install.cmd / uninstall.sh / doctor 一行）与失败补救命令
      验收：两处 `grep` 合约测试 or 人眼过

## S6 文档与提交

- [ ] S6.1 README：新增「安装器怎么用」小节（首次 / 第二次菜单 / 三个等价的命令行形式）+ 三端并列
      验收：README 里有那张对照表（我想做什么 → 从哪开始 → 得到什么）
- [ ] S6.2 README 的"多种使用方式"表加上菜单入口
      验收：人眼过 + 表格行数变化
- [ ] S6.3 提交（英文消息 + 空行 + 中文，`git commit -F`；提交后 `git show --stat`）
      验收：`git log --oneline -1` + `git show --stat HEAD`
- [ ] S6.4 真机走一遍：`install`（新装/覆盖两种）→ `doctor` → `uninstall`（确认提示）→ 装回
      验收：三段输出贴进 todo 作为现场记录；注册任务/防火墙/监听器逐项复核

## 实施记录（2026-10-09）

- S0 定稿：O.1 菜单放 CLI、O.2 加第 5 项"查看状态"、O.3 备份名 `.bak`、O.4 uninstall 加一次确认、
  O.5 成功的 hint 收进 `-v`、O.6 非交互多打一行。
- S1 探测落地（`tests/test_installed_state.py`，14 例）：三平台夹具 + "问不到" 与 "没装" 必须区分。
  **实测抓到**：SYSTEM 任务（collector）对非管理员查询失败，和"任务不存在"长得一模一样 →
  新增"列出可见任务名"兜底 + 未提权时返回 None（unknown），菜单/doctor 都不再谎报数量。
  另一个真 bug：`os.getuid` 在 Windows 不存在，`install --dry-run --platform darwin` 会崩 → 已修。
- S2 输出：`1)…5)` 编号 + 一行一步；细节（产物路径、`[ok]` 行、成功 hint、提权进度逐行）收进新
  的 `-v`；提权子进程默认只显示**最后一行**；失败永远展开。uninstall 的 `2b` 编号消失。
- S3 菜单：`tests/test_install_menu.py`，29 例（六种"不许问" + 五个选项 + 三次重问 + Ctrl-C/EOF）。
  **实测抓到的真 bug**：`_ask_tool` 里 `proc_kwargs` 没导入，被一个过宽的 `except Exception: None`
  吞掉 → 表现成"所有产物都问不到"；已改成局部导入 + 只吞 FileNotFoundError/OSError。
- S4 `install --regen-config`：备份 → 覆盖 → 说明"注释不会保留"（4 例单测）。
- S5 四个启动器去掉重复 banner 与成功提示（CLI 独占呈现）；`sh -n` + 真机 `install.cmd --dry-run`
  各一次；退出码实测：干跑 0、失败 2。
- S6 见 README「安装器怎么用」与本次提交。
- 未做（真机/平台相关，留给对应机器）：真实 UAC/交互终端下的菜单走一遍（本机 stdin 非 tty，
  菜单只能用假 stdin 单测覆盖）；Linux/macOS 上 `./install.sh` 走一遍。
