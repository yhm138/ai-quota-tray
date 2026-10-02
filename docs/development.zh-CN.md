# 开发指南

[首页](../README.zh-CN.md) · [English](development.md)

- [开发环境准备](#开发环境准备)
- [目录结构](#目录结构)
- [C# 版](#c-版)
- [各个脚本](#各个脚本)
- [测试与发布](#测试与发布)

## 开发环境准备

除命令明确切换目录外，均从仓库根目录执行。桌面程序需要 Windows；Linux 和 macOS 可以运行离线测试、编译 C# 程序，但不能验证 Windows 托盘或界面。

- Python 3.10+（CI 测试 3.10 和 3.12）；Windows 界面开发还需要 tcl/tk。
- .NET SDK 8.0.x，用于跨平台测试和 .NET Framework 4.8 编译。
- 离线测试不需要任何产品的登录凭据。

Windows 源码安装会启动程序并设置开机自启，见 [使用指南](user-guide.zh-CN.md#方式二从源码运行)。只运行测试时：

```powershell
python -m venv python/.venv
python/.venv/Scripts/python.exe -m pip install -r python/requirements.txt cryptography
cd python
.venv/Scripts/python.exe tests/test_providers.py
.venv/Scripts/python.exe -m compileall -q quota_tray tools run.pyw
cd ..
```

Linux 或 macOS 使用 `python3` 和 `python/.venv/bin/python`：

```bash
python3 -m venv python/.venv
python/.venv/bin/python -m pip install -r python/requirements.txt cryptography
(cd python && .venv/bin/python tests/test_providers.py)
(cd python && .venv/bin/python -m compileall -q quota_tray tools run.pyw)
```

`cryptography` 是 CI 使用的测试依赖。Linux 上的 TLS 测试还需要可写的 `~/.quota-tray` 目录，用于保存 CA 证书副本。

```bash
dotnet run --project csharp/tests/QuotaTray.Tests -c Release
dotnet build csharp/src/QuotaTray/QuotaTray.csproj -c Release -warnaserror:CS0104
```

C# 项目通过 NuGet 恢复 .NET Framework 引用程序集，跨平台编译无需 Windows 运行时。Windows 专属检查和发布打包见 [构建工作流](../.github/workflows/build.yml)。

## 目录结构

| 目录 | 内容 |
|---|---|
| `python/` | Python 版：`quota_tray/`（程序本体）、`tests/`、`tools/`、`run.pyw` 和各个 `.bat` 脚本 |
| `csharp/` | C# 版：`src/QuotaTray/`（程序本体）和 `tests/QuotaTray.Tests/` |
| `scripts/` | 维护者脚本：发布、打 Release、生成 workflow |
| `update.ps1` | 独立更新脚本（留在根目录，旧版本从这个位置下载它） |
| `docs/` | 中英文指南与面板截图 |

## C# 版

**单个 250 KB 的 `.exe`，双击即可运行。** 用 C# 重写的同一个程序，基于 Windows 10（1903+）/ 11 自带的 .NET Framework 4.8，所以什么都不用装、也不用解压：秒开，内存占用也比约 15 MB 的 Python 版小得多。

数据覆盖相同：Claude（Claude Code 登录，含 WSL；Claude Desktop 自己的登录）、Codex（实时用量接口及套餐、积分、重置，`codex app-server`，会话日志）、Antigravity；同样支持多账号翻页、重置提醒、一键应用内更新、诊断和开机自启。未包含的只有 Python 版作为最后兜底的 claude.ai cookie 路径和 Codex SQLite 扫描。

自己编译需要 .NET SDK（任何系统都能编译）：

```powershell
dotnet build csharp\src\QuotaTray\QuotaTray.csproj -c Release
dotnet run --project csharp\tests\QuotaTray.Tests     # 离线测试
```

`QuotaTray.exe --diagnose` 输出各数据源的探测报告，`--selftest` 在本机检查 AES-GCM、托盘图标和面板。

Windows 上还可检查滚动裁剪：用人工数据在 100%、125%、150% 缩放下离屏绘制，不读取账号配置。

```powershell
powershell.exe -NoProfile -STA -File csharp\tests\Run-PanelScrollRegression.ps1 -AssemblyPath csharp\src\QuotaTray\bin\Release\net48\QuotaTray.exe
```

## 各个脚本

Python 版的脚本在 `python/`，维护者脚本在 `scripts/`。

| 文件 | 作用 |
|---|---|
| `install.bat` | 建虚拟环境、装依赖、启动、写开机自启 |
| `run.bat` | 手动启动 |
| `diagnose.bat` | **出问题先跑这个** —— 打印每条采集路径的探测结果 |
| `probe.bat` | 把代理配置、原始 API 响应、进程探测输出写进 `_probe.txt`（已脱敏） |
| `preview.bat` | 用假数据画一次面板，确认界面正常 |
| `claude_login.bat` | 用指定的代理端口启动 Claude Code，方便登录 |
| `build_exe.bat` | 本地打包出 `dist\QuotaTray.exe` |
| `scripts\publish.bat` | 建 GitHub 仓库并推送（只需一次） |
| `scripts\release.bat` | 打版本 tag，自动构建并发布 Release |
| `scripts\fix_push.bat` | push 失败时重试，并显示完整错误 |
| `scripts\find_gh.bat` | PATH 没刷新导致找不到 git/gh 时定位它们 |
| `uninstall.bat` | 移除开机自启、结束进程、可选删除配置 |

## 测试与发布

测试用伪造的响应跑通全部三条兜底链，覆盖 HTTP 401 降级、两种百分比口径（0–1 和 0–100）、窗口键名模糊匹配、跨来源合并、IDE 未运行、缓存往返和倒计时格式。

运行上面的环境准备步骤完成测试后，从仓库根目录发布新版本。发布新版本靠打 tag。用 `scripts\release.bat` 更稳，它会先确认 workflow 确实在提交里（打了 tag 但仓库里没有 workflow 是不会有任何构建的），提交未保存的改动，推送 tag，然后盯着构建跑完：

```powershell
.\scripts\release.bat
```

或者不用 git：先改 `python/quota_tray/__init__.py` 里的 `__version__` 和 `csharp/src/QuotaTray/QuotaTray.csproj` 里的 `<Version>`，再到 **Actions → build → Run workflow** 填入同样的版本号（如 `v1.2.1`），它会自动打 tag 并发布。

两种方式都会触发 [构建工作流](../.github/workflows/build.yml)，自动把 exe、便携版 zip 和 SHA256 附到 Release 上。
