# 数据来源

[首页](../README.zh-CN.md) · [English](providers.md)

各产品的凭据发现、查询接口和兜底路径。配置方法见 [使用指南](user-guide.zh-CN.md#配置)。C# 版与 Python 版的差异见 [开发指南](development.zh-CN.md#c-版)。

- [Claude](#claude)
- [Codex](#codex)
- [Antigravity IDE](#antigravity-ide)
- [Gemini CLI](#gemini-cli)
- [TRAE（Trae CN）](#traetrae-cn)
- [豆包](#豆包)
- [DeepSeek（按量计费）](#deepseek按量计费)

## Claude

| 顺序 | 来源 |
|---|---|
| 1 | `~/.claude/.credentials.json` 里的 OAuth token → `api.anthropic.com/api/oauth/usage` |
| 2 | Claude Desktop 自己保存的登录（其 `config.json` 里的 `oauth:tokenCacheV2`，用应用密钥解密）→ 同一个 `api.anthropic.com` 接口 |
| 3 | 从 Claude Desktop 的 Electron cookie 库解出 `sessionKey` → `claude.ai` 用量接口 |
| 4 | 在 `config.json` 里手填的 `session_key` |

第 2 条同时支持普通安装版（`%APPDATA%\Claude`）和微软商店版（`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`），即使 Claude Desktop 正占用 cookie 库也能读取；`claude.ai` 拒绝返回组织列表时，会改用 `lastActiveOrg` cookie。

第 1 条会搜索：环境变量 `CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CONFIG_DIR`、`~/.claude`、`%APPDATA%\Claude`、`%LOCALAPPDATA%\Claude`，**以及每个 WSL 发行版下的 `~/.claude`** —— 很多人只在 WSL 里登录过。Claude Desktop 和 Claude Code 共用同一个订阅额度池，所以这里的数字就是桌面端也在消耗的那份。

## Codex

| 顺序 | 来源 |
|---|---|
| 1 | `~/.codex/auth.json` 的 access_token → `chatgpt.com/backend-api/wham/usage` |
| 2 | `codex app-server` 的 JSON-RPC `account/rateLimits/read` |
| 3 | 倒着扫 `~/.codex/sessions/**/rollout-*.jsonl` 里的 `rate_limits` |
| 4 | `~/.codex` 下的 sqlite 文件 |

Codex 会**跨来源合并**，而不是拿到第一个结果就停：线上接口有时只返回两个窗口中的一个，缺的那个由下一条来源补上。窗口名做模糊匹配（`primary`、`primary_window`、`primaryWindow` 算同一个），并且**窗口的实际长度优先于键名** —— 一个叫 primary 但重置时间在五天后的窗口，会被重新标成周窗口。

## Antigravity IDE

Antigravity 内部跑一个语言服务器，启动参数里带 `--csrf_token`，并在 `127.0.0.1` 上监听随机端口。QuotaTray 从运行中的进程读出 token 和端口，再调它本地的 Connect RPC `GetUserStatus`，拿到 prompt credits 余额和每个模型的剩余额度。**数据不出本机**。**IDE 必须开着**，关掉后面板会显示 "IDE not running"。

## Gemini CLI

Gemini CLI 用 Google 账号登录，令牌存在 `~/.gemini/oauth_creds.json`（也支持 `GEMINI_CLI_HOME` 和各 WSL 发行版）。QuotaTray 直接用磁盘上的 access token，从 Google 的 Code Assist 接口（`cloudcode-pa.googleapis.com` 的 `loadCodeAssist`、`retrieveUserQuota`）读取套餐档位和各模型的剩余额度——和 CLI 自己调用的是同一批接口。它不自己刷新令牌（CLI 运行时会刷新，所以文件通常是新的；QuotaTray 也就不需要内置任何 OAuth 密钥）；令牌明显过期时会提示你运行一次 `gemini`。每个 Google 账号一页；令牌只发给 Google。

这张卡片**默认隐藏**：Google 已对个人 Google 账号停用 Gemini CLI（登录时会提示迁移到 Antigravity），所以大多数账号的配额接口已经失效。如果你的账号还能用，把配置里的 `providers.gemini.enabled` 设成 `true` 即可。

## TRAE（Trae CN）

TRAE 把它的 Cloud-IDE JWT 存在 `%APPDATA%\Trae CN\User\globalStorage\storage.json`（键名以 `iCubeAuthInfo://` 开头），可能是明文 JSON，也可能是 base64 的 “byte crypto” 密文（AES-128-CBC）。QuotaTray 解出来后调 TRAE 的付费接口（`api.trae.cn` 的 `ide_user_pay_status`、`ide_user_ent_usage`，请求头 `Cloud-IDE-JWT`）拿套餐和 fast request 用量——和 IDE 里用量面板显示的是同一份数字。国内版 Trae CN 和国际版 Trae、Windows 和各 WSL 用户都会读。

## 豆包

豆包桌面客户端是 www.doubao.com 的 Electron 应用。QuotaTray 从它的 cookie 库里读 `sessionid` cookie（和 Claude Desktop 一样的 OSCrypt/DPAPI + AES-GCM 方案），通过订阅 overview 接口（`/alice/commerce/sale/subscription/overview`）拿套餐和**窗口额度用量**，包括“当前时段”和“近 7 天”。尚未开始的时段显示“not started”（不会显示 1970 年），用量不足 1% 的窗口显示“<1% used”。

卡片还会显示接口明确返回的套餐有效状态（*Plan status*），以及试用或赠送标记。套餐本期结束时间（*Plan until*）和活动权益到期时间（*Bonus until*）分成两行，以本地时间显示到分钟。*Quota group* 显示额度分组；多组有用量时，每条用量条都会带上组名。缺少的字段不显示，未知状态码不作推断。`/alice/profile/self` 的 POST 请求用于补充账号信息；它失败时，用量接口仍独立工作。如果只有账号查询成功，卡片会保留账号，并说明用量不可用的原因。

- **便携版（Portable）？** 在 config.json 里把 `providers.doubao.data_dir` 设成存放 cookie 库的文件夹（便携版安装目录，或其下的 `User Data` 子目录）。
- **想用浏览器而不是客户端？** 把 `providers.doubao.scan_browsers` 设成 `true`，QuotaTray 也会从 Edge、Chrome、Brave、Chromium、Vivaldi、Opera（所有 profile）里读 `doubao.com` 的 cookie。默认关闭。
- **读不到 cookie？** 新版豆包**和浏览器**都用 App-Bound 加密 cookie，QuotaTray 无法从外部解密。把 `sessionid` 值复制出来，填到 config.json 的 `providers.doubao.session_id` 即可。`session_id` 也支持整段 `name=value; name=value` 的 Cookie 字符串——如果单独的 `sessionid` 被拒，可以把已登录的 `www.doubao.com` 请求里的整个 Cookie 头粘进去。

## DeepSeek（按量计费）

DeepSeek 的 API 是预充值按量扣费，没有周期额度，所以它放在 *Pay as you go* 标签页，显示 `api.deepseek.com/user/balance` 返回的余额。Key 从已经保存它的工具里读取：

| 顺序 | 来源 |
|---|---|
| 1 | config.json 里手填的 `api_key` |
| 2 | **OpenCode**：`~/.local/share/opencode/auth.json`（`/connect` 保存的位置），以及 `~/.config/opencode/opencode.json` 里的 `provider.deepseek.options.apiKey`（支持 `{env:变量名}`） |
| 3 | **DeepSeek Harness**（`dsh`）：`~/.dsh/.credentials.yaml` 里的 `refs.DEEPSEEK_API_KEY`（*Settings > Models* 保存的位置），其次 `~/.dsh/.env`；设置了 `$DSH_HOME` 就用那个目录 |
| 4 | 环境变量 `DEEPSEEK_API_KEY` |

Windows 和每个 WSL 发行版都会搜索。每个不同的 Key 单独一页（‹ › 翻页），并标明是哪个工具里的；OpenCode 和 dsh 用的是同一个 Key 时合并成一页。每页都有一行 *API key*：鼠标停在 `sk-...1234` 上显示完整 Key，点它或点 **Copy** 复制到剪贴板。

**OpenCode CLI 和 OpenCode Desktop 会分别标明**。Desktop 自己的设置在 `%APPDATA%\ai.opencode.desktop`，但它内部启动的 OpenCode 服务读的是和 CLI **同一个** `auth.json`，所以在 Windows 上两者用的一定是同一个 Key，两个都装了时页面标为 *OpenCode CLI + OpenCode Desktop*。只有 WSL 里的才会不同：发行版里的 CLI 用那个发行版自己的 `auth.json`，Desktop 在 WSL 里启动的服务也一样（标为 *OpenCode Desktop - WSL Ubuntu*）。Diagnostics 里会列出各自在哪里找到。Key 只会发给 `api.deepseek.com`；工具里配置成走中转（自定义 base URL）的 Key 不会被使用。
