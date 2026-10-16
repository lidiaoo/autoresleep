# 验证：唤醒原因通道（WoWLAN 报告 → 「魔法包唤醒不回睡」）

这份文档对应 `config.wowlan_wake_reason`（默认 `true`）。它验证的是一条**只有 Linux 有**的通道：
网卡固件自己说「这次为什么醒」。脚本在 `tools/verify_wowlan_chain.py`，本机可跑、**不用真的睡**。

## 1. 为什么单独验证它

物理事实（第 8 节也写了）：**把机器从睡眠中唤醒的那一个包，被网卡固件吃掉了**，主机侧任何 socket
都看不到它。所以「发一个魔法包 → 机器保持醒着」这件事，靠监听 UDP 9/7 永远做不到——除非有人补发。

无线驱动会在 resume 时从固件把唤醒原因问回来，经 `cfg80211` 归一化成内核结构体，`iw event` 把它
渲染成一行。实测（本机 2026-10-16，`sudo iw event -t -f`）：

```
1792130691.782796: wlan0 (phy #0): WoWLAN wakeup
        * magic packet received
        * packet (might be truncated): ... c0:a8:00:8c c0:a8:00:ff a4:07 00:09 ... 28:d0:ea:27:40:0c ...
1792130720.783661: wlan0 (phy #0): WoWLAN wakeup
        wakeup not due to WoWLAN
```

- 第一条：真魔法包（帧里能看到手机 IP、UDP 9、6×FF + 本机 MAC 重复）→ 记为 `kind=magic`，**锁存整个会话**；
- 第二条：固件明说**不是**无线引起的 → 记为 `kind=not-wowlan`，从此不许把这次唤醒记到无线网卡头上
  （实测踩过：一次非无线唤醒被记成 `kind=wifi-nic who=wakeup-event:wlan0@14s`）。

这两条结论都不解析任何厂商私有字符串——读的是内核统一结构体经 `iw` 渲染的那一行。

## 2. 三层验证

| 层 | 验什么 | 要多久 | 命令 |
|---|---|---|---|
| **L1 脚本自检** | 解析、接线、开关、豁免链（假 `iw` + 假计数器，全部在临时目录里） | 5 秒 | `python tools/verify_wowlan_chain.py` |
| **L2 真机抓原始报告** | 你这块网卡/驱动的 `iw event` 到底打印不打印那条 bullet | 一轮睡眠 | `sudo iw event -t \| tee /tmp/iwev.log` + 一次唤醒 |
| **L3 真机全链** | 传感器自动捕获 → 文件 → 判定 → 不回睡 | 一轮睡眠 | 见第 4 节 |

**L1 通过不代表机器上真的能用**（驱动可能不上报），但它把「代码接线」和「驱动行为」彻底分开：
L1 挂了是代码问题，L1 过了而 L2 空则是驱动问题。

## 3. L1：脚本自检（先做这个）

```bash
python tools/verify_wowlan_chain.py          # 退出码 0 = 全过
```

它按顺序打印七段，每段一行 `ok` / `FAIL`：

| 段 | 内容 | 失败意味着 |
|---|---|---|
| 1 | 解析层：用**逐字**的真实 `iw` 输出（上面那两条）验证分类成 `magic` / `not-wowlan` / `other` | 解析正则被改坏了 |
| 2 | tap：一个假 `iw` 子进程 → 真读线程 → `wowlan-events.log` 出现 `kind=magic` | 读块逻辑坏了（bullet 行没有时间戳，必须整块收） |
| 3 | 豁免：`kind=magic` → `packet-last.txt` → 引擎第 1 层 → `skip reason=wake-requested` + 会话锁 `asked-for` | 魔法包不再豁免（最要命的那种） |
| 3b | 反面：`kind=other:*` 什么都不做（不锁、不写包文件） | 未分类的报告被当成了包 |
| 4 | 否认：`not-wowlan` 时无线计数器**失去**指认权，有线候选仍在 | 一次非无线唤醒又会被记到 wlan0 |
| 5 | 开关 `wowlan_wake_reason=false`：报告**根本不被读**，tap 目标变成 `None`，线上有魔法包也不落文件 | 开关没生效（关掉还会豁免） |
| 6 | 本机**已安装**状态目录里 `wowlan-events.log` 的最新几条（只读） | 只提示，不算失败 |
| 7 | 打印 L2/L3 的实机步骤，以及 Windows/macOS 对应的命令 | 提示 |

脚本全程只写临时目录，**不睡眠、不绑 9/7、不动真的 `iw`**（跑的时候往 PATH 前面塞一个假 `iw`），
所以机器上的监听器不受影响。

## 4. L2 / L3：真机一轮（不补按、不敲键盘）

```bash
# 0) 传感器是常驻 system unit，改过代码必须重启才吃到
sudo systemctl restart autoresleep-wol-sensor

# 1) 先看一眼驱动到底报不报（这一步决定后面值不值得继续）
sudo iw event -t | tee /tmp/iwev.log        # 另开一个终端留着它跑

# 2) 睡下
systemctl suspend

# 3) 手机/另一台机器对 wlan0 的 MAC **发一个**魔法包（一个就够）

# 4) 醒来后什么键鼠都别碰，等 15 秒，然后：
python tools/verify_wowlan_chain.py         # 看第 6 段
grep -i wowlan /tmp/iwev.log                # 看第 1 步留下的原始记录
tail -3 ~/.local/state/autoresleep/wowlan-events.log
tail -5 ~/.local/state/autoresleep/decisions.log
```

判读表：

| 现象 | 结论 | 下一步 |
|---|---|---|
| `wowlan-events.log` 有 `kind=magic`，`decisions.log` 是 `skip reason=wake-requested`，机器不回睡 | **全链通** | 完事。以后单发一个包就能保持醒 |
| `iw` 原始记录里有 `* magic packet received`，但 `wowlan-events.log` 是空的 / 没有这个文件 | 传感器没吃到（旧的传感器进程 / 没重启） | `sudo systemctl restart autoresleep-wol-sensor`，再跑一轮 |
| `iw` 原始记录里那一行**没有** bullet（只有 `WoWLAN wakeup` 一行） | 传感器用的 `iw event -t`（不带 `-f`）没拿到续行 | 给 tap 加 `-f`：`src/autoresleep/iwevent.py` 的 `cmd` 改成 `["iw", "event", "-t", "-f"]`（`-f` 按 `iw --help` 只影响 auth/assoc 的整帧打印，理论上无关，但要实测） |
| `iw event` 里**完全没有** `WoWLAN wakeup` 这一行，机器却被唤醒了 | 这块驱动/网卡不上报原因 | 通道对这台机器无效 → `autoresleep config --set wowlan_wake_reason=false`，行为退回「包监听 + 回睡窗口」（不会更糟，只是回到补发/敲键那条路） |
| 机器压根没醒 | WoWLAN 没武装（与本文档无关） | `iw dev wlan0 wowlan show`，必要时 `sudo iw dev wlan0 wowlan enable magic-packet` |
| `decisions.log` 里 `who=wakeup-event:wlan0@…`（而不是 `asked-for-this-session`） | 报告没赶上这次会话 | 看 `wowlan-events.log` 的时间戳与 `evidence-last.json` 的 `session` 是否对得上；差得远说明传感器重启晚了 |

## 5. 关掉它（`wowlan_wake_reason: false`）

```bash
autoresleep config --set wowlan_wake_reason=false
sudo systemctl restart autoresleep-wol-sensor     # 让 tap 真的不再写
autoresleep doctor                                # 会打印这一项当前的状态
```

关掉之后**行为与这条通道上线之前完全一致**：`wowlan-events.log` 不再新增，判定只看
「听到的包 + 键鼠 + 链路」，魔法包唤醒仍需补发证据波或键鼠（见 README 第 3 节的窗口说明）。
它**不会**让本来不回睡的会话变成回睡——关掉只是少一个豁免来源。

## 6. 平台边界（Windows / macOS 为什么没有这个）

| 平台 | 设备级（谁唤醒的） | 原因级（为什么唤醒） | 本项目怎么做 |
|---|---|---|---|
| Linux | sysfs 每设备唤醒计数 + PME + IRQ 反查 | **有**：cfg80211 结构体 → `iw event` | 本文档这条链；`config.wowlan_wake_reason` 控制 |
| Windows | Power-Troubleshooter 事件 1 的 `WakeSourceText`（网卡名）+ `powercfg /devicequery wake_armed` | **没有**：事件里只有设备名，「魔法包 vs 模式匹配包」查不到 | 保持设备级归因；要收紧就在**驱动侧**只留魔法包 |
| macOS | `pmset -g log` 的 `due to <CODE>`（`ARPT` 无线节点 / `GLAN` 有线节点 / `EHC` USB / `RTC` 定时） | **没有**：只有硬件码 + 粗类别（`Network`/`USB`/`Alarm`） | 设备级归因（`GLAN` → 有线网卡，2026-10-16 修：此前落到 `other-device`，而 other-device 设计上永不回睡） |

Windows 侧的操作（提高"只有魔法包才能唤醒"的把握）：

```powershell
powercfg /lastwake                                     # 上一次是谁（设备名）
powercfg /devicequery wake_armed                       # 现在谁被允许唤醒
Get-NetAdapter | Select-Object Name, InterfaceDescription
Get-NetAdapterAdvancedProperty -Name "以太网" | Where-Object DisplayName -Match 'Wake|唤醒'
# 只留"魔术封包唤醒"，关掉"样式比对唤醒(Wake on Pattern Match)"与"关机网络唤醒"
```

macOS 侧的操作：

```bash
pmset -g log | grep -iE "Wake.*due to" | tail -20      # 唤醒原因码
pmset -g assertions | grep -i magicwake                # 魔法包唤醒有没有武装
sudo pmset -a womp 1                                   # 开：以太网魔法包唤醒
sudo pmset -a tcpkeepalive 0                           # 关：一两小时自己醒一次的常见元凶
```

## 7. 相关文件

| 文件 | 作用 |
|---|---|
| `wowlan-events.log` | 传感器记录的固件自报原因（`kind=magic` / `not-wowlan` / `other:*`），与链路口志 `wlan-events.log` 分开 |
| `packet-last.txt` | `kind=magic` 的报告写进**与"听到包"完全相同**的文件 → 引擎第 1 层原样生效 |
| `session-cause.json` | 会话豁免锁（`asked-for`），键鼠唤醒写 `input` |
| `decisions.log` | 判定行：`pkt=magic@Ns who=asked-for-this-session` |
| `docs/resleep-window-plan.md` §16 | 这条通道的实施记录与实测数据 |
