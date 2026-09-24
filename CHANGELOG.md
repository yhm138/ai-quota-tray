# Changelog

## Unreleased

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
