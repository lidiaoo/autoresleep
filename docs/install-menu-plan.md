# install / uninstall 界面整理 + 「第二次调用给菜单」设计

> 目标读者：过一眼就能拍板。**第 2 节是已定稿**（我照这个写代码），**第 3 节是要你定的**（没定之前不写）。

## 0. 一句话目标

1. 输出有条理：默认只看到「编号步骤 + 一行结果」，细节收进 `-v`，失败永远展开。
2. 第二次跑 `install`（机器上已经装着了）→ 不再闷头重装，而是给一个**编号菜单**：
   覆盖安装 / 卸载 / 重新生成配置 / 退出。

非目标（明确不做，免得越做越复杂）：不做 TUI/彩色框线、不加新子命令、不改 install/uninstall
**实际做的事**、不动 doctor 的判定逻辑、不改配置文件格式。

## 1. 现状（为什么显得乱）

| 现象 | 证据 |
|---|---|
| 两处 banner | `install.cmd` 先打一次「AutoReSleep - install / ====」，CLI 再打一次「AutoReSleep v2.0.0.dev0 - install / ====」 |
| 段落太多且每段都很长 | install 有 5 段（config / scheduler artifacts / firewall / privileged half / start the resident jobs），每段下 3–10 行细节 |
| 编号不自洽 | uninstall 里有 `---- 2b. the privileged half ----` |
| 语言混着 | 段落标题英文（`---- 1. config ----`），个别行中文（`已存在，保持原样（注释与手工改动都留着）`） |
| 没有「是否已安装」的探测 | 全仓库只有 Windows 专用的 `_registered_task_settings()`（读 judge 的 `<Interval>`），没有任何地方能回答"这台机器装过没有" |

## 2. 已定稿

### 2.1 菜单（放 CLI，不放启动器）

- 位置：`src/autoresleep/cli.py` 的 `cmd_install()` 开头。理由：一处实现、三平台通用、可单测；
  4 个启动器（install.cmd/install.sh/uninstall.cmd/uninstall.sh）保持"只做预检 + 收尾提示"。
  双击 `install.cmd` 与手敲 `autoresleep install` 看到的是同一个菜单。
- **首次安装也给菜单**（不是只有第二次）：菜单标题与状态行按实际情况说（"还没有安装在这台机器上" /
  "已经安装在这台机器上"），选项一致、措辞随状态微调（首次是「安装」、之后是「覆盖安装」；第 3 项
  在首次就是"先生成配置文件再装"，正是首次会想要的）。一个代码路径，两种情况不会各自漂移。
- 触发条件（**全部满足**才进菜单）：

  | 条件 | 说明 |
  |---|---|
  | 交互终端 | 见下面几行（不再要求"已安装"） |
  | 交互终端 | `_stdin_can_answer()`（复用现有闸门） |
  | 没有 `--yes` | 脚本/CI 显式要求"别问" |
  | 没有 `AUTORESLEEP_NO_PROMPT` | 同上，环境变量版 |
  | 不是 `--elevated` 子进程 | 提权子进程**永不提问**（install-pitfalls §2） |
  | 不是 `--dry-run` | 干跑必须完全不阻塞 |

  任何一条不满足 → 行为与今天完全一致（闷头重装），并且**不阻塞**。

- 菜单长相（默认；`1` 是回车默认项）：

  ```
   AutoReSleep 已经安装在这台机器上：

     配置文件  C:\Users\jianghh\AppData\Local\AutoReSleep\config.yaml
     计划任务  AutoReSleep-judge / AutoReSleep-listener / AutoReSleep-collector
     监听器    pid 36508 正在运行，占用 UDP 7/9/45678

   请选择：
     1) 覆盖安装（重建计划任务与服务；配置文件保持原样，手改的注释都留着）   [回车]
     2) 卸载（删任务、撤防火墙规则、停监听器；默认保留配置和日志）
     3) 重新生成配置（用项目里的 config.default.yaml 覆盖，旧文件备份成 config.yaml.bak）
     4) 退出

   > 
  ```

- 选择行为：
  | 输入 | 做什么 |
  |---|---|
  | `1` / 回车 / 空 | 走今天的 install 全流程 |
  | `2` | 走 uninstall 全流程，完了打印一句「要装回来：install」 |
  | `3` | 备份 + 覆盖配置文件，然后**继续**走 install 全流程（覆盖安装的目的就是让它生效） |
  | `4` / `q` / `n` | 什么都不做，退出码 0 |
  | 其它 | 打印"没听懂，请输入 1-4"，再问一次（最多 3 次，之后按 4 退出） |
  | Ctrl-C | 什么都不做退出（退出码 0，和现有"首次生成配置时取消"一致） |

- 每项都有**等价的命令行形式**（脚本可以不看菜单）：
  `install`（=1）、`uninstall`（=2）、`install --regen-config`（=3，**新增 flag**）。

### 2.2 「已安装」怎么判（新增 `_installed_state()`）

不靠猜，问 OS：

| 平台 | 问什么 | 得到什么 |
|---|---|---|
| Windows | `schtasks /Query /TN \AutoReSleep\AutoReSleep-{judge,listener,collector}` 的返回码 | 每个任务在不在 |
| Linux | `systemctl --user list-unit-files autoresleep-*`（+ `/etc/systemd/system/autoresleep-wol-sensor.service` 是否存在） | 每个单元在不在 |
| macOS | `~/Library/LaunchAgents/com.autoresleep.{judge,listener}.plist` 是否存在 | 两个 agent 在不在 |

再加两项：配置文件在不在、`listener.lock` 里的 pid 是否还活着（复用现有读锁代码）。
返回值统一成一个小结构（`job 名单 / 配置文件路径 / 监听器 pid`），**doctor 也改用它**——
今天 doctor 与 install 各写各的探测，这本身就是"同一件事两处实现"的隐患。

无 systemd / 无 launchctl / 权限不足 → 该平台这一项**如实报"问不到"**，不假装没有。

### 2.3 输出的条理性（默认精简，细节进 `-v`）

默认形态：一条 banner + 编号步骤 + 每步一行结果 + 一句收尾。

```
==================================================================
 AutoReSleep v2.0.0.dev0 - install        (Windows)
==================================================================
  1) 配置        config.yaml 已存在，保持原样
  2) 计划任务    judge / listener / collector 重建 3/3
  3) 防火墙      UDP 9,7,67,68 + 45678 已放行
  4) 监听器      pid 36508 占用 7/9/45678，心跳正常

 完成。下一步：autoresleep doctor  |  改配置：<路径>  |  卸载：uninstall.cmd
```

- **默认只打印上面这些**：每步一行，含"关键值"（路径/数量/pid/端口）。
- 今天的那些细节（每个产物的完整路径、`[ok] schtasks /Delete …`、`[dry-run] would write …`、
  成功的 `hint:`）→ 只在 `install -v` / `uninstall -v` 时打印（**新增 `-v` / `--verbose`**）。
- **失败永远展开**：任何一步失败，立刻完整打印该步的细节 + 提权进度文件路径 + 补救命令，
  然后以非 0 退出（与今天一致，一个字都不省）。
- 提权子进程的行式进度：默认**只显示最后一行**（形如 `[3/7] 注册 AutoReSleep-listener`），
  全量进度照旧写文件；`-v` 时逐行显示（今天的"边等边念"就是这一档）。
- uninstall 用同一套编号（`2b` 消失）：配置保留 → 计划任务 → 防火墙 → 监听器 → 保留的配置/日志说明。
- 语言：**标签用中文，标识符保持英文**（路径、flag、任务名、单元名照旧），与 `config.default.yaml`
  的风格一致（中文注释 + 英文键）。

### 2.4 启动器侧（去重）

- 4 个启动器去掉自己的 banner 与"计划做什么"清单（那些由 CLI 负责），只留：
  1) 预检查（UNC 路径 / MSYS 守卫 / 找解释器 + 版本）；
  2) 单行招呼 `AutoReSleep - install（Windows）`；
  3) 转发给 CLI；
  4) 收尾提示（平台相关的文件名与一行 doctor 命令）；
  5) 失败时的补救命令。
- 4 个脚本的行为继续完全对称（Linux/macOS/Windows 三端同一套文案与段落）。

### 2.5 「重新生成配置」的确切语义

`install --regen-config`（菜单第 3 项）：

1. 把现有 `config.yaml` 备份成 `config.yaml.bak`（**覆盖上一次的 .bak**，不堆一串时间戳）；
2. 用项目里的 `config.default.yaml` 覆盖 `config.yaml`；
3. 打印一句"已重新生成；旧文件在 `<路径>.bak`；**你手改的注释不会保留**"；
4. 然后继续走 install（第 1 步"配置"那行会显示"已重新生成（旧文件 .bak）"）。
5. 没有现成 `config.yaml` 时 → 等同于首次生成，`.bak` 不产生。

## 3. 待确认 / 开放项（你拍板，我不猜）

| # | 问题 | 我的建议 |
|---|---|---|
| O.1 | 菜单放 **CLI** 还是放 4 个启动器脚本？（sol 的约定是"体验归安装脚本"） | **CLI**。sol 那边启动器就是唯一入口；这边 CLI 是唯一逻辑所在，放脚本要写 4 份且不能单测 |
| O.2 | 菜单第 5 项要不要加「查看状态（doctor）」？ | **不加**，保持 4 项与 sol 的 1应用/2卸载/3重生成配置/4退出 对齐；收尾行已经写着 doctor 命令 |
| O.3 | 备份名 `config.yaml.bak` 还是 `config.yaml.bak.20261009-1730`？ | **`config.yaml.bak`**（简单、只有一个；要历史就 git） |
| O.4 | `uninstall` 单独调用时，要不要也加一次 `y/n` 确认（现在直接就删）？ | **加**（交互终端才问，`--yes` 跳过）——它删的是系统里的东西，值得一次确认；但这改变了现有行为，所以问你 |
| O.5 | 默认精简档里，**成功的** hint（比如防火墙状态提示）要不要留？ | **不留**（进 `-v`）；失败相关的一律留 |
| O.6 | `install` 在"已安装 + 非交互"时，除了闷头重装，要不要**额外打印一行**「已安装；要卸载请跑 uninstall」？ | **要**（一行，不阻塞；非交互日志里也能看懂发生了什么） |

## 4. 验收方式（每项都能照做）

```bash
# 单测（每条 todo 都要求这一步先绿）
cd E:/Common/Project/IdeaProjects/autoresleep
python -m unittest discover -s tests

# 不看菜单：脚本路径必须与今天一致（非交互、--yes、dry-run 三种）
python autoresleep.py install --dry-run                 # 只打印计划，不问
python autoresleep.py install --yes --dry-run           # 同上
echo "" | python autoresleep.py install --dry-run       # stdin 不是 tty → 不进菜单

# 看菜单（本地已经装着 → 会进菜单；用 4 退出，不动系统）
python autoresleep.py install            # 选 4
python autoresleep.py install            # 选 2（卸载）→ 之后用 install 装回来

# 重新生成配置（会覆盖，先备份）
python autoresleep.py install --regen-config --dry-run  # 打印"会备份+覆盖"
python autoresleep.py install --regen-config --yes      # 真做（一次 UAC）

# 精简 vs 详细
python autoresleep.py install --dry-run                 # 默认：编号步骤，一行一步
python autoresleep.py install --dry-run -v              # 详细：今天的全量细节
```