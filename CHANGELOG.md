# Changelog

## v1.3.5

- **Half the size**: the single-file exe went from 29.5 MB to 15.5 MB (the
  portable zip from 30.2 MB to 15.9 MB):
  - AES-GCM decryption (cookies, Claude Desktop's login) uses Windows' own
    CNG (`bcrypt.dll`) instead of the `cryptography` package (~10 MB)
  - Pillow codecs the tray icon never uses (AVIF alone was 8 MB, WebP, the
    font engine, color management) are left out
  - `curl_cffi` is no longer bundled: Claude usage comes from Claude
    Desktop's login; only the last-resort cookie route loses its Chrome
    TLS fingerprint
  - unused standard-library tooling is left out and docstrings stripped
  - the release build's self-test checks on Windows that everything kept
    still works (AES-GCM known-answer test, tray icon, HTTPS, Tk, SQLite)
- **No `.old` file after an update**: a running exe can only be renamed, so
  the previous version goes to a hidden `.quotatray-trash` folder and the
  new version deletes it for good as soon as it starts (leftover `.old`
  files from v1.3.2-v1.3.4 are removed too)
- **Cloud credit is a bar** like the usage limits, with its expiry date; it
  is not counted in the tray icon's percentage
- The build can no longer hang on an error dialog: startup import errors
  go to `crash.log`, and the smoke test has a time limit

## v1.3.4

- **Fixed: after an in-app update the new version could break**: a
  *Failed to remove temporary directory ..._MEI...* warning, then Claude
  showing *could not decrypt oauth:tokenCache (ModuleNotFoundError)*, and on
  the next start *Tcl wasn't installed properly*. The one-file exe started
  the new version with its own PyInstaller environment, so the new version
  reused the old one's unpacked files, which the old one deleted as it
  exited. New copies now start with a clean environment
  (`PYINSTALLER_RESET_ENVIRONMENT=1`, inherited `_PYI_*` variables dropped),
  after an update, after a self-restart and from `update.ps1`
- A copy started by an older version (the update from v1.3.3) notices and
  relaunches itself cleanly right away, so this update is already safe
- The build requires PyInstaller 6.9 or newer, which supports the reset

## v1.3.3

- **Readable Claude limit names.** Claude's usage reply includes entries
  under internal code names, which showed up as bars like *Iguana Necktie*
  and *Nimbus Quill*:
  - `iguana_necktie` is the Claude Code cloud-sessions credit; it moves out
    of the bars into the details as *Cloud credit: 7.5% used - expires ...*
  - `seven_day_overage_included` reads *7-day overage allowance* (also in
    the resets note); other `five_hour_*` / `seven_day_*` limits read
    *5-hour ...* / *7-day ...*
  - unknown code names (e.g. `nimbus_quill`, `tangelo`) are experiments:
    kept out of the bars and listed in Diagnostics under *internal quotas
    (not shown)*
- **A failed update names its log and opens it**: the message shows the
  full path of `quota-tray.log` and has an *Open log* button next to
  *Retry*. The v1.3.1 message "see update.log" pointed at a file that is
  never written when Windows blocks the update script from starting
- `update.ps1` sets its user agent with `-UserAgent`, which Windows
  PowerShell 5.1 accepts (it can refuse `User-Agent` in `-Headers`)

## v1.3.2

- **Updates run inside the running app, with progress.** *Update now* keeps
  the current version running and shows each step with a progress bar:
  check, download (MB and percent), SHA256 verification, install. Only when
  the new version is in place does it close and start it, and the new
  version confirms *Updated from vX to vY*. If any step fails, the running
  version stays and shows the reason with *Retry*. The single-file build
  renames its running exe to `.old` and puts the new one in its place (the
  leftover is removed after the restart); the portable build unpacks beside
  itself and is copied over right after it exits
- **Release files carry the version and architecture**:
  `QuotaTray-v1.3.2-windows-x64.exe`,
  `QuotaTray-v1.3.2-windows-x64-portable.zip`,
  `QuotaTray-v1.3.2-SHA256SUMS.txt`. Updating keeps whatever name the
  installed exe has, and running copies are recognised under any
  `QuotaTray*.exe` name

## v1.3.1

- **Fixed: after *Update now* QuotaTray could disappear for good.** The app
  closed right away while `update.ps1` looked up the release through the
  GitHub API; when that failed (rate limit, blocked network) the script
  stopped without starting QuotaTray again. Now:
  - the app stays open until the new version is downloaded and its checksum
    verified; if the download fails it stays and shows the reason with
    *Retry*
  - the script downloads straight from the release page (no API call when
    the version is known) and falls back to the release page's redirect
    when the API refuses
  - whatever fails, the script starts QuotaTray again if it had closed for
    the update, restoring the previous exe if needed

## v1.3.0

- **Several accounts per product.** When the logins on this machine belong to
  different accounts, the product's card gets **‹ 1/2 ›** buttons, one page
  per account, each with its own usage, plan, credits and resets:
  - Claude: Claude Code (Windows `~/.claude`, and each WSL distro) and
    Claude Desktop are all checked, instead of stopping at the first that
    works
  - Codex: the Windows `~/.codex` (shared by the Codex app and the Windows
    CLI), a custom `CODEX_HOME`, and each WSL distro's `~/.codex`
  - Logins of the same account share one page; the page says which logins
    it covers (e.g. *Claude Code + Claude Desktop*)
- The daily reset reminder and Diagnostics include every account
- The installed WSL distros are looked up at most every 10 minutes

## v1.2.4 (not released separately; included in v1.3.0)

- **Fixed: after switching Codex accounts the bars kept showing the old
  account** while plan and credits showed the new one, and *Refresh* did not
  help. When the live usage API reported only some windows, the rest were
  filled in from `~/.codex/sessions` logs, which still held the old
  account's numbers. Now:
  - a live answer (usage API or `codex app-server`) is used as is; session
    logs and the local database are only a fallback when nothing live works
  - even then, anything written before the current Codex login is ignored
  - the Codex card shows the signed-in account's email
- Codex windows use the window length the usage API reports
  (`limit_window_seconds`) for their labels

## v1.2.3

- **Check for updates now visibly answers.** It opens the panel with
  *Checking for updates...*, then either *vX is available* with an
  **Update now** button, *You are on the latest version*, or the reason the
  check failed. Before, the answer went only to a Windows notification,
  which Windows 11 often does not show, so the menu item looked dead
- The check falls back to the release page on github.com when the GitHub
  API refuses (it allows 60 anonymous requests an hour) or is unreachable
- *Update now* shows progress and, if the updater cannot start, the error
  with a *Retry* button

## v1.2.2

- **Fixed: after a few days every request failed** with `Could not find a
  suitable TLS CA certificate bundle ... _MEI...\certifi\cacert.pem`, so
  Claude showed nothing and Codex fell back to slower sources. The one-file
  exe unpacks to `%TEMP%\_MEI...`, and Windows temp cleanup deletes files
  there that are not held open. QuotaTray now copies the certificate list to
  `%APPDATA%\QuotaTray\cacert.pem` at start and uses that copy, and if its
  unpacked files disappear anyway it restarts itself to restore them

## v1.2.1

- **Codex resets are always accounted for.** The card now always has a
  *Resets* line: the count and first expiry, *none available*, or *unknown*
  with the reason (for example `HTTP 403`), also listed in Diagnostics under
  *Codex account details*. Before, any failure hid the line silently
- The reset list is read the way the official Codex client reads it: only
  credits whose `status` is `available` count, and each shows its title
  (e.g. "Full reset (Weekly + 5 hr)"). The request uses the same headers as
  the usage call, retrying with the community workaround's headers
- A failed account-details fetch is retried after 5 minutes instead of
  being cached for an hour

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
