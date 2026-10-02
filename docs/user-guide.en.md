# QuotaTray User guide

[Home](../README.en.md) · [中文](user-guide.md)

- [Install](#install)
- [Plan, credits and resets](#plan-credits-and-resets)
- [Updating](#updating)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Privacy](#privacy)
- [Known limitations](#known-limitations)

## Install

### Option 1 — the executable (no Python needed)

Download from the [latest release](https://github.com/yhm138/ai-quota-tray/releases/latest). There are two
editions that share settings and the panel layout. Their fallback paths differ; see the [edition comparison](development.en.md#the-c-edition).

| File | Edition | When to use it |
|---|---|---|
| `QuotaTray-<version>-windows-x64.exe` | Python | Single file, ~15 MB. The original; has every fallback path. Rename it to `QuotaTray.exe` if you like; updates keep whatever name it has. |
| `QuotaTray-<version>-windows-x64-portable.zip` | Python | Unzip and run `QuotaTray.exe` inside. Starts faster and is less likely to trip antivirus. |
| **`QuotaTray-<version>-csharp-windows-anycpu.exe`** | **C#** | **Recommended. One 250 KB file, double-click to run**, starts instantly. Uses the .NET Framework 4.8 built into Windows 10/11, runs natively on x64 and ARM64. See [the C# edition](development.en.md#the-c-edition). |
| `QuotaTray-<version>-SHA256SUMS.txt` | | Verify what you downloaded (see below). |

Only one QuotaTray runs at a time, whichever edition: both share the settings
in `%APPDATA%\QuotaTray`, and starting one replaces the other.

Every file carries the version and architecture, e.g.
`QuotaTray-v1.3.2-windows-x64.exe`. Releases before v1.3.2 used plain names.

Put it where it will live (for example `C:\Tools\QuotaTray\`) **before** the
first run — the first launch writes that path into run-at-login.

> **Antivirus false positives.** This app reads the Chromium cookie database,
> calls DPAPI and scans process command lines. Those are exactly the behaviours
> heuristic scanners flag, and the release binaries are not code-signed, so
> Defender / 360 / Huorong may block them. That is a false positive, but you
> should not take my word for it — the binaries are built by
> [GitHub Actions](https://github.com/yhm138/ai-quota-tray/actions/workflows/build.yml) from this repository, in a
> public log you can read, and each release ships SHA256 sums. If you would
> rather not run an unsigned binary at all, use option 2.

Verify a download:

```powershell
Get-FileHash .\QuotaTray-v1.3.2-windows-x64.exe -Algorithm SHA256
# compare with QuotaTray-v1.3.2-SHA256SUMS.txt from the same release
```

### Option 2 — from source

```powershell
git clone https://github.com/yhm138/ai-quota-tray.git
cd ai-quota-tray\python
.\install.bat
```

`install.bat` creates a virtualenv, installs the dependencies, starts the app
and enables run-at-login. You need Python 3.10+ with **tcl/tk and IDLE** ticked
during setup (the panel uses tkinter):

```powershell
winget install Python.Python.3.12
```

## Plan, credits and resets

Under the usage bars each card lists what the account API reports: the plan,
when the subscription started and renews or ends (Codex), or only when it
started (Claude, which publishes no renewal date), credits or extra usage left, and **banked limit resets**, the
one-time resets Anthropic and OpenAI hand out that sit on your account until
you use them or they expire. If you have unused resets, QuotaTray reminds you
once a day (tray menu: *Remind me about unused resets*).

## Updating

**From v1.1.0 on**, QuotaTray checks for a new release once a day. When one is
out, right-click the tray icon and choose **Check for updates**, then
**Update now** in the panel. From v1.3.2 the running copy does all the work
itself and shows each step with a progress bar: check, download, verify the
SHA256, install. Only when the new version is in place does it close and start
it; if any step fails it keeps running and says why, with *Retry*. The new
version confirms "Updated from vX to vY".

**Coming from v1.0.x** (no update button yet), open PowerShell and paste:

```powershell
irm https://raw.githubusercontent.com/yhm138/ai-quota-tray/main/update.ps1 | iex
```

It finds your install through its run-at-login entry (or the running
process), stops it, installs the latest release over it and starts it again.
It works for `QuotaTray.exe`, the portable folder and source installs, and
settings in `%APPDATA%\QuotaTray` are kept. Source installs can also just run
`python\update.bat` (a source install from before v1.4.0, which has no
`python\` folder yet, updates with `git pull` or this script). This script logs to `%APPDATA%\QuotaTray\update.log`; updates
started from the tray menu log to `%APPDATA%\QuotaTray\quota-tray.log`, and a
failed one has an *Open log* button.

## Configuration

`%APPDATA%\QuotaTray\config.json` (tray menu → *Open config file*). Saved changes apply on the next refresh; no restart needed.

```jsonc
{
  "refresh_seconds": 300,          // minimum 60
  "hide_not_installed": true,      // skip products that aren't installed
  "warn_percent": 75,              // amber threshold
  "danger_percent": 90,            // red threshold
  "icon_style": "bars",            // "bars" or "ring"
  "panel_scale": 1.0,              // C# edition: make the panel bigger (1.2) or smaller (0.9)
  "check_updates": true,           // look for a new release once a day
  "promote_tray_icon": true,       // Windows 11: keep the icon out of the ^ overflow
  "remind_unused_resets": true,    // daily notification about banked resets
  "reset_reminder_hour": 10,       // ...from this local hour on
  "providers": {
    "claude": {
      "enabled": true,
      "percent_scale": "auto",     // auto / percent / fraction
      "oauth_token": "",           // manual fallbacks
      "session_key": "",
      "credentials_path": "",
      "scan_wsl": true,
      "order": ["oauth", "desktop_oauth", "desktop_cookie", "manual_cookie"]
    },
    "codex": {
      "enabled": true,
      "codex_home": "",
      "order": ["wham", "app_server", "jsonl", "sqlite"]
    },
    "antigravity": {
      "enabled": true,
      "csrf_token": "",
      "port": 0,
      "show_models": true,
      "max_models": 6
    },
    "deepseek": {
      "enabled": true,
      "api_key": "",               // manual fallback
      "scan_opencode": true,
      "scan_dsh": true,
      "scan_wsl": true,
      "low_balance": 5             // amber below this (in the account's currency)
    }
  }
}
```

Set `enabled` to `false` to skip a product. Edit `order` to change the fallback
sequence or drop a path entirely — for example remove `app_server` if you would
rather not spawn a process on every refresh.

## Troubleshooting

Run **`diagnose.bat`** first. It prints the outcome of every source:

```
=== Claude [max] ===
  status: connected
  source: Claude Desktop login
  5-hour window          5%   resets in 2h 59m
  7-day window          45%   resets in 2d 15h
    [FAIL] OAuth credentials: ...\.credentials.json: token expired
    [OK] Claude Desktop login: ...\Claude\config.json [oauth:tokenCacheV2]
```

In the panel, *Diagnostics* starts with a plain **SUMMARY** of what is broken,
and *Open as text* opens the whole report in Notepad.

| Symptom | Fix |
|---|---|
| Every Claude source FAILs | Open Claude Desktop and make sure you are signed in; or run `claude` once |
| `token expired` (OAuth credentials) | Harmless when *Claude Desktop login* works; Claude Code renews its own token when you use it |
| `Claude Desktop login: the saved login has expired` | Open Claude Desktop; it renews its login by itself |
| `App-Bound encryption (v20)` | Newer Electron cookies can't be decrypted; use Claude Code credentials or paste a `session_key` into config |
| `could not copy the cookie DB` | Claude Desktop locked the file; quit it from its tray icon once, then *Refresh now* |
| `blocked by Cloudflare` | `claude.ai` challenged the request; open Claude Desktop so the session is fresh, or rely on Claude Code OAuth |
| Codex "offline snapshot" | It fell through to the session-log path. Run `codex` once |
| Codex "partly from an offline snapshot" | The live endpoint reported only one window; the other came from the log. Normal |
| Antigravity "IDE not running" | Open the IDE, then *Refresh now* in the tray menu |
| Claude Code can't reach your proxy | Edit `PROXY_PORT` at the top of `claude_login.bat`, run it, then `/login` |
| No tray icon | Start `QuotaTray.exe` by hand: it opens its panel. Windows 11 hides new tray icons under the `^` arrow; drag it out. If it cannot start you get a message and `%APPDATA%\QuotaTray\crash.log` |
| `Could not find a suitable TLS CA certificate bundle ... _MEI...` | Windows temp cleanup deleted files the one-file exe unpacked. Fixed in v1.2.2 (it keeps its own copy and restarts itself); the portable zip never unpacks to temp at all |
| No plan / resets lines | They come from the account APIs; check the log. Codex needs the ChatGPT usage API source to work |
| Panel missing / tkinter error | Python was installed without tcl/tk — reinstall it |
| A percentage looks wrong | Set that provider's `percent_scale` from `auto` to `percent` |

Log: `%APPDATA%\QuotaTray\quota-tray.log` · Snapshot: `%APPDATA%\QuotaTray\diagnostics.txt`

## Privacy

- Credentials are used only in HTTP `Authorization` / `Cookie` headers, never logged and never sent anywhere else. Tokens are only read, never refreshed, so your Claude Code / Claude Desktop logins stay untouched.
- One thing is written to disk: the last working Claude Desktop session cookie, DPAPI-encrypted to your Windows account, in `%APPDATA%\QuotaTray\claude-session.bin` (used when Claude Desktop keeps its cookie file locked). Delete it any time.
- Hosts contacted: `api.anthropic.com`, `claude.ai`, `chatgpt.com`, `api.deepseek.com`, `cloudcode-pa.googleapis.com`, `api.trae.cn`, `www.doubao.com` and `127.0.0.1` for quotas; `api.github.com`, `github.com` and `raw.githubusercontent.com` for the daily update check (turn off with `check_updates`).
- Every quota call lives in the files under `python/quota_tray/providers/` (C#: `csharp/src/QuotaTray/Providers/`), the update check in `python/quota_tray/updater.py`, so the whole surface is short enough to read.
- `probe.bat` redacts tokens and cookies before writing its report, but `_probe.txt` still contains local paths — do not paste it publicly without a look.

## Known limitations

- These are **undocumented internal endpoints**. Vendors can change them at any time. The parsers hunt recursively for `utilization` / `used_percent` fields rather than fixed paths, so a reshuffled envelope still parses — and if one truly breaks, `diagnose.bat` names it.
- Antigravity needs the IDE running.
- Claude publishes no renewal date, so only the subscription start is shown.
- Claude's usage reply also carries entries under internal code names. Known ones are shown as what they are (`iguana_necktie` is the Claude Code *Cloud credit*); unknown ones are kept out of the bars and listed in Diagnostics under *internal quotas (not shown)*.
- The Codex session-log fallback is a snapshot, not live data.
- The release binaries are unsigned. See the antivirus note under [Install](#install).
