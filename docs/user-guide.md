# QuotaTray 使用指南

[首页](../README.md) · [English](user-guide.en.md)

- [安装](#安装)
- [套餐、积分与重置](#套餐积分与重置)
- [更新](#更新)
- [配置](#配置)
- [排错](#排错)
- [隐私](#隐私)
- [已知限制](#已知限制)

## 安装

### 方式一：直接用 exe（不需要 Python）

从 [最新 Release](https://github.com/yhm138/ai-quota-tray/releases/latest) 下载。有两个版本，共用设置和面板布局，但兜底路径有所不同，见 [版本说明](development.md#c-版)。

| 文件 | 版本 | 什么时候用 |
|---|---|---|
| `QuotaTray-<版本>-windows-x64.exe` | Python | 单文件，约 15 MB，功能最全（所有兜底路径）。可以改名为 `QuotaTray.exe`，更新时会保留你的文件名 |
| `QuotaTray-<版本>-windows-x64-portable.zip` | Python | 解压后运行里面的 `QuotaTray.exe`。启动更快，也更不容易被杀软误报 |
| **`QuotaTray-<版本>-csharp-windows-anycpu.exe`** | **C#** | **推荐。单个 250 KB 文件，双击即可运行**，秒开。用 Windows 10/11 自带的 .NET Framework 4.8，x64 和 ARM64 都原生运行。见 [C# 版](development.md#c-版) |
| `QuotaTray-<版本>-SHA256SUMS.txt` | | 校验下载的文件（见下） |

不管哪个版本，同一时间只会运行一个 QuotaTray：两个版本共用 `%APPDATA%\QuotaTray` 里的设置，启动其中一个会替换掉另一个。

文件名都带版本号和架构，例如 `QuotaTray-v1.3.2-windows-x64.exe`（v1.3.2 之前的版本用的是不带版本号的旧文件名）。

**第一次运行前**先把它放到最终位置（比如 `C:\Tools\QuotaTray\`）—— 首次启动会把当时的路径写进开机自启。

> **关于杀软误报**。这个程序会读 Chromium 的 cookie 数据库、调用 DPAPI、扫描进程命令行 —— 这几样正好是启发式扫描重点盯的行为，而且 Release 里的二进制没有代码签名，所以 Defender / 360 / 火绒有可能拦它。这是误报，但你不该只听我一面之词：二进制全部由 [GitHub Actions](https://github.com/yhm138/ai-quota-tray/actions/workflows/build.yml) 基于本仓库构建，构建日志公开可查，每个 Release 都附 SHA256。如果你就是不想跑未签名的程序，走方式二。

校验下载：

```powershell
Get-FileHash .\QuotaTray-v1.3.2-windows-x64.exe -Algorithm SHA256
# 和同一个 Release 里的 QuotaTray-v1.3.2-SHA256SUMS.txt 对一下
```

### 方式二：从源码运行

```powershell
git clone https://github.com/yhm138/ai-quota-tray.git
cd ai-quota-tray\python
.\install.bat
```

`install.bat` 会建虚拟环境、装依赖、启动程序并写入开机自启。需要 Python 3.10+，安装时**务必勾选 tcl/tk and IDLE**（面板用的是 tkinter）：

```powershell
winget install Python.Python.3.12
```

## 套餐、积分与重置

用量条下面列出各家账号接口返回的信息：套餐、订阅开始和续费/到期日期（Codex）或开始日期（Claude 不公开续费日期）、剩余积分或额外用量，以及**可用的额度重置**：Anthropic 和 OpenAI 发放的一次性重置，留在账号里直到用掉或过期，在各自应用的 Settings → Usage 里使用。有未使用的重置时，每天提醒一次（托盘菜单：*Remind me about unused resets*，时间用 `reset_reminder_hour` 设置）。

## 更新

**v1.1.0 起**，QuotaTray 每天自动检查一次新版本。有新版时，右键托盘图标点 *Check for updates*，
再在面板里点 **Update now**。v1.3.2 起由正在运行的程序自己完成全部步骤并显示进度条：检查、下载、
校验 SHA256、安装。新版本就位后才关闭旧版并启动新版；任何一步失败，旧版继续运行并显示原因和 *Retry*。
新版启动后会提示"Updated from vX to vY"。

**从 v1.0.x 升级**（旧版还没有更新按钮），打开 PowerShell 粘贴：

```powershell
irm https://raw.githubusercontent.com/yhm138/ai-quota-tray/main/update.ps1 | iex
```

脚本会通过开机自启项（或正在运行的进程）找到你的安装位置，停掉它、装上最新版并重新启动。
exe、便携版、源码安装都适用，`%APPDATA%\QuotaTray` 里的设置会保留。源码安装也可以直接运行
`python\update.bat`（v1.4.0 之前的源码安装还没有 `python\` 目录，用 `git pull` 或上面的脚本更新）。这个脚本的记录在 `%APPDATA%\QuotaTray\update.log`；从托盘菜单发起的更新记录在 `%APPDATA%\QuotaTray\quota-tray.log`，更新失败时面板上有 *Open log* 按钮可直接打开。

## 配置

配置在 `%APPDATA%\QuotaTray\config.json`（托盘菜单 → *Open config file*），保存后下次刷新即生效，不用重启。几个常用字段如下；完整示例见 [英文配置说明](user-guide.en.md#configuration)。

- `refresh_seconds` — 刷新间隔，最小 60
- `warn_percent` / `danger_percent` — 变黄、变红的阈值
- `icon_style` — `bars`（三条用量条）或 `ring`（圆环）
- `panel_scale` — C# 版面板的缩放倍数，默认 `1.0`，想再大一点可设 `1.2`
- 每个 provider 的 `enabled` 改成 `false` 就不再采集它
- 每个 provider 的 `order` 可以调整兜底顺序，或者直接删掉某条路径（比如嫌 `app_server` 每次都要起进程）
- `check_updates` — 每天检查新版本
- `promote_tray_icon` — Windows 11 下把托盘图标固定显示在任务栏上，而不是藏在 ^ 里（你自己在设置里改过的不会被覆盖）
- `remind_unused_resets` / `reset_reminder_hour` — 未使用重置的每日提醒及其时间
- `providers.deepseek.low_balance` — DeepSeek 余额低于这个数（按账户币种）时变黄，默认 5；`api_key` 可以手填 Key

## 排错

**先跑 `diagnose.bat`**，它会逐条打印每个来源的结果：

```
=== Claude [max] ===
  status: connected
  source: Claude Code OAuth (.credentials.json)
  5-hour window         31%   resets in 2h 1m
  7-day window          12%   resets in 6d 12h
    [OK] OAuth credentials: C:\Users\you\.claude\.credentials.json
```

| 现象 | 处理 |
|---|---|
| Claude 全部 FAIL | 打开 Claude Desktop 并确认已登录；或在 cmd 里跑一次 `claude` |
| `token expired`（OAuth credentials） | 只要 *Claude Desktop login* 正常就无妨；Claude Code 使用时会自己续期 |
| `Claude Desktop login: the saved login has expired` | 打开 Claude Desktop，它会自己续期登录 |
| `App-Bound encryption (v20)` | 新版 Electron 的 cookie 解不开；改用 Claude Code 凭据，或把 `session_key` 填进配置 |
| `could not copy the cookie DB` | cookie 库被 Claude Desktop 锁住；从它的托盘图标彻底退出一次，再点 *Refresh now* |
| `blocked by Cloudflare` | `claude.ai` 拦截了请求；打开一次 Claude Desktop 刷新会话，或改用 Claude Code OAuth |
| Codex 显示 "offline snapshot" | 走的是会话日志兜底；跑一次 `codex` 会刷新 |
| Codex 显示 "partly from an offline snapshot" | 线上接口只返回了一个窗口，另一个来自日志。正常现象 |
| Antigravity 显示 "IDE not running" | 打开 IDE，然后在托盘菜单点 *Refresh now* |
| Claude Code 连不上你的代理端口 | 改 `claude_login.bat` 开头的 `PROXY_PORT`，运行它，再 `/login` |
| 托盘没图标 | 手动运行 `QuotaTray.exe`，会直接弹出面板。Windows 11 默认把新图标藏在 `^` 里，拖出来即可。启动失败会弹窗并写 `%APPDATA%\QuotaTray\crash.log` |
| `Could not find a suitable TLS CA certificate bundle ... _MEI...` | Windows 清理临时文件时删掉了单文件 exe 解压出的文件。v1.2.2 已修复（自带证书副本并自动重启）；便携版 zip 不解压到临时目录，不受影响 |
| 面板不显示 / 报 tkinter | Python 安装时没勾 tcl/tk，重装 Python |
| 某个百分比明显不对 | 把该 provider 的 `percent_scale` 从 `auto` 改成 `percent` |

日志：`%APPDATA%\QuotaTray\quota-tray.log` · 诊断快照：`%APPDATA%\QuotaTray\diagnostics.txt`

## 隐私

- 凭据只用在 HTTP 的 `Authorization` / `Cookie` 头里，不写日志、不发给任何第三方。只读取 token，从不刷新，不会影响你的 Claude Code / Claude Desktop 登录
- 唯一落盘的是最近一次可用的 Claude Desktop 会话 cookie，用 DPAPI 绑定你的 Windows 账户加密，存在 `%APPDATA%\QuotaTray\claude-session.bin`（Claude Desktop 锁住 cookie 文件时使用），可随时删除
- 额度查询只连 `api.anthropic.com`、`claude.ai`、`chatgpt.com`、`api.deepseek.com`、`cloudcode-pa.googleapis.com`、`api.trae.cn`、`www.doubao.com`、`127.0.0.1`；每日检查更新连 `api.github.com`、`github.com`、`raw.githubusercontent.com`（可用 `check_updates` 关闭）
- 额度请求都在 `python/quota_tray/providers/`（C# 版在 `csharp/src/QuotaTray/Providers/`）里，更新检查在 `python/quota_tray/updater.py`，整个面很小，可以自己读完
- `probe.bat` 生成报告时会脱敏 token 和 cookie，但 `_probe.txt` 里仍有本机路径，公开粘贴前先看一眼

## 已知限制

- 这些都是各家的**内部接口**，没有公开文档，厂商随时可能改。解析器写成「递归查找 `utilization` / `used_percent` 字段」而不是写死路径，所以外层结构变了还能读；真失效了 `diagnose.bat` 会告诉你是哪条断的
- Antigravity 必须开着 IDE
- Claude 不公开续费日期，所以只显示订阅开始日期
- Claude 的用量接口里还有一些内部代号条目。已知的会显示成真实含义（`iguana_necktie` 是 Claude Code 的 *Cloud credit* 云会话额度）；未知的不显示为进度条，列在诊断的 *internal quotas (not shown)* 里
- Codex 的会话日志兜底是快照，不是实时数据
- Release 里的二进制未签名，见 [安装](#安装) 中的杀软说明
