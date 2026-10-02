# Data sources

[Home](../README.en.md) · [中文](providers.md)

Credential discovery, quota endpoints and fallback paths for each product. See the [user guide](user-guide.en.md#configuration) for configuration and the [development guide](development.en.md#the-c-edition) for edition differences.

- [Claude](#claude)
- [Codex](#codex)
- [Antigravity IDE](#antigravity-ide)
- [Gemini CLI](#gemini-cli)
- [TRAE (Trae CN)](#trae-trae-cn)
- [Doubao](#doubao)
- [DeepSeek (pay as you go)](#deepseek-pay-as-you-go)

## Claude

| Order | Source |
|---|---|
| 1 | OAuth token from `~/.claude/.credentials.json` → `api.anthropic.com/api/oauth/usage` |
| 2 | Claude Desktop's own login (`oauth:tokenCacheV2` in its `config.json`, decrypted with the app's key) → the same `api.anthropic.com` endpoint |
| 3 | `sessionKey` decrypted out of Claude Desktop's Electron cookie store → `claude.ai` usage endpoints |
| 4 | `session_key` pasted into `config.json` |

Path 1 searches the `CLAUDE_CODE_OAUTH_TOKEN` env var, `CLAUDE_CONFIG_DIR`,
`~/.claude`, `%APPDATA%\Claude`, `%LOCALAPPDATA%\Claude`, **and every WSL
distro's `~/.claude`** — plenty of people only ever signed in inside WSL.
Claude Desktop and Claude Code draw from the same subscription pool, so these
numbers are the budget the desktop app spends too.

Path 2 covers both the regular installer (`%APPDATA%\Claude`) and the
Microsoft Store build (`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`),
reads the cookie DB even while Claude Desktop holds it open, and falls back to
the `lastActiveOrg` cookie when `claude.ai` refuses the organization list.

## Codex

| Order | Source |
|---|---|
| 1 | access_token from `~/.codex/auth.json` → `chatgpt.com/backend-api/wham/usage` |
| 2 | `codex app-server` + JSON-RPC `account/rateLimits/read` |
| 3 | `~/.codex/sessions/**/rollout-*.jsonl` scanned backwards for `rate_limits` |
| 4 | sqlite files under `~/.codex` |

Codex **merges** across sources instead of stopping at the first hit: the live
endpoint sometimes reports only one of the two windows, so the next source fills
in the gap. Window names are matched loosely (`primary`, `primary_window`,
`primaryWindow` are the same thing), and a window's real length beats its key
name — a "primary" window whose reset is five days out is relabelled as the
weekly one.

## Antigravity IDE

Antigravity runs a language server launched with `--csrf_token`, listening on a
random port on `127.0.0.1`. QuotaTray reads that token and port off the running
process, then calls the server's local Connect RPC `GetUserStatus` for the
prompt-credit balance and each model's remaining quota. Nothing leaves your
machine. **The IDE has to be open**, otherwise the panel says "IDE not running".

## Gemini CLI

Gemini CLI signs in with Google and keeps the tokens in
`~/.gemini/oauth_creds.json` (`GEMINI_CLI_HOME` and each WSL distro too).
QuotaTray reads the tier and the per-model quota from Google's Code Assist API
(`cloudcode-pa.googleapis.com` — `loadCodeAssist` and `retrieveUserQuota`),
the same calls the CLI makes, using the access token on disk. It does not
refresh the token itself (the CLI does that on its own use, so the file is
usually current, and QuotaTray embeds no OAuth secret); a clearly expired
token is reported so you can run `gemini` once. One page per Google account;
tokens go to Google only.

This card is **off by default**: Google deprecated Gemini CLI for individual
Google accounts (sign-in now tells you to migrate to Antigravity), so its
quota API fails for most accounts. Set `providers.gemini.enabled` to `true` in the
config if your account still works.

## TRAE (Trae CN)

TRAE keeps its Cloud-IDE JWT in `%APPDATA%\Trae CN\User\globalStorage\storage.json`
(under `iCubeAuthInfo://...`), as plain JSON or a base64 "byte crypto" blob
(AES-128-CBC). QuotaTray decodes it, then calls TRAE's pay endpoints
(`api.trae.cn/trae/api/v1/pay/ide_user_pay_status` and `ide_user_ent_usage`,
header `Cloud-IDE-JWT`) for the plan and the fast-request usage, the same
numbers the IDE's usage panel shows. Trae CN and international Trae, Windows
and each WSL user.

## Doubao

Doubao's desktop client is an Electron app for www.doubao.com. QuotaTray reads
its `sessionid` cookie from the app's cookie store (the same OSCrypt/DPAPI +
AES-GCM scheme as Claude Desktop), calls `/alice/profile/self` for the signed-in
account, and reads the subscription *overview*
(`/alice/commerce/sale/subscription/overview`) for the plan and the
**window-limit usage** — a *Current period* window and a *Last 7 days* one. A
period that has not started yet shows "not started" (no 1970 date), and a
window under one percent shows "<1% used". The card also shows the API's
active/inactive plan status, with trial or gift status when explicitly supplied.
The subscription's *Plan until* and promotional *Bonus until* are separate
rows, shown in local time to the minute. *Quota group* names the usage group;
when several groups have usage, each bar is prefixed with its group name.
Unknown status codes are not interpreted, and missing fields are omitted.
The profile POST is optional: a profile failure does not prevent the overview
from supplying plan and usage data. If only the profile succeeds, the card
keeps its account information and explains why usage is unavailable.

- **Portable install?** Set `providers.doubao.data_dir` in config.json to the folder that
  holds the app's cookie store (the portable install folder, or its `User Data`
  subfolder).
- **Use the browser instead of the app?** Set `providers.doubao.scan_browsers` to `true`
  and QuotaTray also reads the `doubao.com` cookie from Edge, Chrome, Brave,
  Chromium, Vivaldi and Opera (every profile). Off by default.
- **Card can't read the cookie?** Recent Doubao *and browser* builds encrypt
  cookies with App-Bound encryption, which QuotaTray cannot decrypt from
  outside. Copy the `sessionid` value and paste it into `providers.doubao.session_id` in
  config.json. `session_id` also accepts a whole `name=value; name=value`
  Cookie string, so if a lone `sessionid` is rejected you can paste the entire
  Cookie header from a logged-in `www.doubao.com` request.

## DeepSeek (pay as you go)

DeepSeek bills API keys against a prepaid balance, so this card sits on the
*Pay as you go* tab and shows what is left, from `api.deepseek.com/user/balance`.
The key is read from the tools that already hold it:

| Order | Source |
|---|---|
| 1 | `api_key` in config.json |
| 2 | **OpenCode**: `~/.local/share/opencode/auth.json` (what `/connect` saves) and `provider.deepseek.options.apiKey` in `~/.config/opencode/opencode.json`, `{env:NAME}` included |
| 3 | **DeepSeek Harness** (`dsh`): `refs.DEEPSEEK_API_KEY` in `~/.dsh/.credentials.yaml` (what *Settings > Models* saves), then `~/.dsh/.env`; `$DSH_HOME` moves the folder |
| 4 | the `DEEPSEEK_API_KEY` environment variable |

Windows and every WSL distro are searched. Each distinct key gets its own page
(‹ ›), labelled with the tools that hold it; the same key in OpenCode and dsh
is one page. Each page has an *API key* row: hover over `sk-...1234` for the
whole key, click it or **Copy** to copy it.

**OpenCode CLI vs OpenCode Desktop**: the page says which one uses the key.
Desktop keeps its own settings in `%APPDATA%\ai.opencode.desktop`, but the
OpenCode server it runs reads the same `auth.json` as the CLI, so on Windows
the two always share one key and the page reads *OpenCode CLI + OpenCode
Desktop* when both are installed. A key only one of them sees comes from WSL:
the CLI inside a distro has that distro's own `auth.json`, and so do the WSL
servers Desktop starts (*OpenCode Desktop - WSL Ubuntu*). Diagnostics lists
where each app was found. A key is only ever sent to `api.deepseek.com`, and a key that a
tool points at a relay (its own base URL) is left alone.
