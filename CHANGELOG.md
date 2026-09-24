# Changelog

## v1.2.0

- **Plan, subscription and credits** under each provider's bars:
  - Claude: plan tier and status (`/api/oauth/profile`), subscription start
    date (Anthropic exposes no renewal date, so none is guessed), and extra
    usage spent / cap / left
  - Codex: plan, renewal or end date from `backend-api/subscriptions` (falls
    back to the login token's date, marked as possibly stale), credit balance
    with its dollar value, and the workspace spend limit
- **Banked limit resets**: how many unused resets each account has and when
  the first one expires (Claude's grants from the usage reply, Codex's from
  `wham/rate-limit-reset-credits`). Shown amber when one expires within 3 days
- **Daily reminder** about unused resets, once a day from 10:00 local time,
  naming the soonest expiry. Toggle it in the tray menu (*Remind me about
  unused resets*); change the hour with `reset_reminder_hour` in config.json

## v1.1.4

- **Claude Desktop usage now comes from Claude Desktop's own login.** The
  app keeps an OAuth token in its `config.json` (`oauth:tokenCacheV2`,
  encrypted with the same key as its cookies). QuotaTray decrypts it and asks
  `api.anthropic.com/api/oauth/usage`, the endpoint Claude Code's `/usage`
  uses. This avoids both problems of the cookie route: Claude Desktop keeps
  its cookie file exclusively locked while it runs, and `claude.ai` sits
  behind Cloudflare. Works for the regular and the Microsoft Store install.
  QuotaTray only reads the token; Claude Desktop keeps renewing it itself
- Saved configs from older versions pick up the new source automatically

## v1.1.3

- **A stuck old copy no longer blocks startup.** An old v1.0.0 process that
  had lost its tray icon kept holding the single-instance lock, `taskkill`
  hung on it, and every new launch gave up. This version uses a new lock that
  old copies cannot hold, and stops old copies in the background with
  `TerminateProcess`, logging exactly why if Windows refuses
- If the tray icon's loop ever dies, QuotaTray now logs it and exits instead
  of lingering invisibly while holding the lock

## v1.1.2

- **Nothing happened on launch, and nothing was logged**: once the log
  reached 512 KB, rotating it failed while another copy held it open, and
  Python then dropped every log line. Rotation failures now keep appending.
  Every launch is logged, and a crash shows a message box and writes
  `crash.log` instead of vanishing
- Starting QuotaTray by hand opens its panel (Windows 11 hides new tray
  icons); starting it again while it runs opens the running copy's panel.
  Run-at-login starts it quietly (`--autostart`)
- **Claude Desktop**: `claude.ai` requests use a Chrome TLS fingerprint
  (`curl_cffi`), since Cloudflare answers plain Python clients with a
  challenge page. The last working session is kept (DPAPI-encrypted) for
  when Claude Desktop holds its cookie file locked. The panel shows the
  actual reason when Claude fails
- **Diagnostics**: a plain summary of what is broken comes first, the view
  keeps its scroll position when data refreshes, the mouse wheel no longer
  double-scrolls, and *Open as text* opens the full report in Notepad
- The release build checks that every bundled dependency loads (`--selftest`)

## v1.1.1

- Starting QuotaTray from a new folder (say a downloaded `QuotaTray.exe` next
  to an old source install) now stops the old copy and takes over, instead of
  exiting with "another instance is already running". Run-at-login moves to
  the copy that is running
- **Codex**: session logs are read backwards in small blocks and multi-MB
  lines are skipped, fixing a `MemoryError`
- **Codex**: the local database is copied together with its `-wal` file, and
  a damaged one is skipped instead of raising "database disk image is
  malformed"
- **Codex**: one failing source no longer discards windows other sources
  already found

## v1.1.0

- **Updates**: QuotaTray checks for a new release once a day and shows
  *Update to vX.Y.Z and restart* in the tray menu; one click downloads it,
  verifies its SHA256, replaces the program in place and restarts it.
  *Check for updates* checks on demand, and `QuotaTray.exe --update` does the
  same from the command line. Turn the daily check off with
  `"check_updates": false` in config.json
- `update.ps1` / `update.bat` update an existing install (exe, portable or
  source) in place; run-at-login keeps working because nothing moves
- Releases can be cut from the Actions tab (*build > Run workflow* with a
  version) without pushing a tag

## v1.0.1

This release was never published; its changes ship in v1.1.0.

- **Claude Desktop**: find the Microsoft Store (MSIX) install's data directory
- **Claude Desktop**: read the cookie DB while the app has it open, instead of
  failing on the sharing violation
- **Claude Desktop**: strip the cookie domain hash based on the DB schema
  version rather than guessing from the bytes, and open the copied DB by plain
  path so Windows paths no longer break the SQLite URI
- **Claude Desktop**: use the `lastActiveOrg` cookie to pick the organization,
  and still reach the usage endpoint when the organization list is blocked;
  failures now say whether Cloudflare or a stale session was the cause
- **Claude**: `utilization` is always read as 0-100, so 1% usage early in a
  window is no longer shown as 100%

## v1.0.0

First public release.

- Tray icon that draws one usage bar per product, colour-coded by threshold
- Click-to-open panel with per-window usage, reset countdowns and data freshness
- **Claude**: Claude Code OAuth credentials (including every WSL distro) ->
  `api.anthropic.com/api/oauth/usage`, falling back to the Claude Desktop
  cookie store and finally a hand-pasted session key
- **Codex**: `chatgpt.com/backend-api/wham/usage` -> `codex app-server` JSON-RPC
  -> session-log snapshot -> sqlite scan, **merged** so a short and a long
  window are both covered even when one source only reports one of them
- **Antigravity**: reads `--csrf_token` and the listening port off the running
  language server, then queries its local Connect RPC endpoint
- Diagnostics view and `diagnose.bat` showing exactly which source was used and
  why the others were skipped
- Run-at-login on first launch, respecting the tray-menu toggle afterwards
- Single-instance lock, cached last-known values, rotating log file
