"""Collect everything needed to debug a QuotaTray install, into _probe.txt.

Writes next to the project so it can be read back easily. Secrets are redacted:
tokens, cookies and keys are replaced with <redacted:N chars> before printing.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_tray.providers import antigravity, claude, codex   # noqa: E402
from quota_tray.win.proc import run                           # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "_probe.txt"
LINES: list[str] = []

SECRET_KEYS = re.compile(
    r"(token|secret|key|cookie|password|authorization|credential)", re.IGNORECASE
)


def say(text: str = "") -> None:
    LINES.append(text)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode())


def section(title: str) -> None:
    say("")
    say("=" * 68)
    say(f"== {title}")
    say("=" * 68)


def redact(value):
    """Blank out anything that looks like a credential, keep the shape."""
    if isinstance(value, str):
        if len(value) > 24 and re.fullmatch(r"[A-Za-z0-9_\-.+/=]{24,}", value):
            return f"<redacted:{len(value)} chars>"
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if SECRET_KEYS.search(str(k)) and isinstance(v, str) and v:
                out[k] = f"<redacted:{len(v)} chars>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def dump_json(obj, limit: int = 6000) -> None:
    try:
        text = json.dumps(redact(obj), indent=2, ensure_ascii=True)
    except (TypeError, ValueError):
        text = repr(obj)[:limit]
    if len(text) > limit:
        text = text[:limit] + f"\n... (truncated, {len(text)} chars total)"
    say(text)


# --------------------------------------------------------------- 1. proxy

def probe_proxy() -> None:
    section("1. PROXY CONFIGURATION")
    say("-- process environment --")
    found = False
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ):
        value = os.environ.get(name)
        if value:
            found = True
            say(f"  {name} = {value}")
    if not found:
        say("  (no proxy variables in this process)")

    say("")
    say("-- HKCU\\Environment (persistent user variables) --")
    code, out, err = run(["reg", "query", "HKCU\\Environment"], timeout=15)
    for line in out.splitlines():
        if re.search(r"proxy", line, re.IGNORECASE):
            say(f"  {line.strip()}")
    if code != 0:
        say(f"  (reg query failed: {err.strip()[:120]})")

    say("")
    say("-- Windows Internet Settings (system proxy) --")
    code, out, _ = run(
        ["reg", "query",
         "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings"],
        timeout=15,
    )
    for line in out.splitlines():
        if re.search(r"ProxyServer|ProxyEnable|ProxyOverride|AutoConfigURL", line):
            say(f"  {line.strip()}")


# --------------------------------------------------------------- 2. claude

def probe_claude() -> None:
    section("2. CLAUDE CODE CONFIG")
    home = Path.home()
    settings = home / ".claude" / "settings.json"
    say(f"-- {settings} --")
    if settings.is_file():
        try:
            dump_json(json.loads(settings.read_text(encoding="utf-8", errors="replace")))
        except json.JSONDecodeError as exc:
            say(f"  (not valid JSON: {exc})")
    else:
        say("  (file does not exist)")

    for extra in (home / ".claude" / "settings.local.json", home / ".claude" / "config.json"):
        if extra.is_file():
            say("")
            say(f"-- {extra} --")
            try:
                dump_json(json.loads(extra.read_text(encoding="utf-8", errors="replace")), 3000)
            except json.JSONDecodeError:
                say("  (not valid JSON)")

    say("")
    big = home / ".claude.json"
    say(f"-- {big}: lines mentioning proxy / a port --")
    if big.is_file():
        try:
            hits = 0
            for i, line in enumerate(big.read_text(encoding="utf-8", errors="replace").splitlines()):
                if re.search(r"proxy|78\d\d|10808|1080", line, re.IGNORECASE):
                    say(f"  L{i + 1}: {line.strip()[:200]}")
                    hits += 1
                    if hits >= 30:
                        say("  ... (more hits suppressed)")
                        break
            if not hits:
                say("  (no proxy-like lines)")
        except OSError as exc:
            say(f"  (unreadable: {exc})")
    else:
        say("  (file does not exist)")

    say("")
    say("-- cc-switch profiles mentioning proxy --")
    ccs = home / ".cc-switch"
    if ccs.is_dir():
        for path in list(ccs.rglob("*.json"))[:10]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"proxy|78\d\d", text, re.IGNORECASE):
                say(f"  {path}")
                for line in text.splitlines():
                    if re.search(r"proxy|78\d\d", line, re.IGNORECASE):
                        say(f"    {line.strip()[:180]}")
    else:
        say("  (~/.cc-switch does not exist)")

    say("")
    say("-- claude executable --")
    for cmd in (["where", "claude"], ["where", "claude.cmd"]):
        code, out, _ = run(cmd, timeout=10)
        if code == 0 and out.strip():
            for line in out.splitlines():
                say(f"  {line.strip()}")

    say("")
    say("-- credentials found (no token values) --")
    settings_dict = {"scan_wsl": True}
    for path in claude.credential_candidates(settings_dict):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        token, expires, plan = claude._token_from_file(path)
        from datetime import datetime, timezone
        when = (
            datetime.fromtimestamp(expires / 1000.0, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            if expires
            else "unknown"
        )
        say(f"  {path}")
        say(f"      token present: {'yes' if token else 'no'}, expires: {when}, plan: {plan}")


# --------------------------------------------------------------- 3. codex

def probe_codex() -> None:
    section("3. CODEX RAW DATA")
    settings = {"codex_home": "", "jsonl_max_days": 30}
    home = codex.codex_home(settings)
    say(f"codex home: {home}  (exists: {home.is_dir()})")
    say(f"codex exe:  {codex.find_codex_exe()}")

    say("")
    say("-- raw response from chatgpt.com/backend-api/wham/usage --")
    provider = codex.CodexProvider.__new__(codex.CodexProvider)
    provider.settings = settings
    tokens, origin = codex.CodexProvider._auth_tokens(provider)
    if not tokens:
        say("  (no access_token, skipping)")
    else:
        say(f"  auth file: {origin}")
        headers = {
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json",
            "User-Agent": "codex_cli_rs/0.0.0 (quota-tray)",
        }
        if tokens.get("account_id"):
            headers["chatgpt-account-id"] = str(tokens["account_id"])
        try:
            from quota_tray.providers.base import session
            resp = session().get(codex.WHAM_URL, headers=headers, timeout=25)
            say(f"  HTTP {resp.status_code}")
            try:
                dump_json(resp.json(), 8000)
            except ValueError:
                say(f"  (not JSON) {resp.text[:800]}")
        except Exception as exc:                                # noqa: BLE001
            say(f"  request failed: {exc}")

    say("")
    say("-- last rate_limits record in the session logs --")
    sessions = home / "sessions"
    if not sessions.is_dir():
        say(f"  (no {sessions})")
        return
    try:
        files = sorted(
            (p for p in sessions.rglob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:30]
    except OSError as exc:
        say(f"  (walk failed: {exc})")
        return
    say(f"  {len(files)} recent log files")
    for path in files:
        for line in codex.iter_lines_reverse(path):
            if "rate_limit" not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            node = codex._find_rate_limits(record)
            if node:
                say(f"  from {path.name} (timestamp {record.get('timestamp')})")
                dump_json(node, 3000)
                return
    say("  (no rate_limits record found)")


# --------------------------------------------------------------- 4. antigravity

def probe_antigravity() -> None:
    section("4. ANTIGRAVITY DISCOVERY")
    say("-- powershell Get-CimInstance Win32_Process --")
    code, out, err = run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", antigravity.PS_PROCESSES],
        timeout=40,
    )
    say(f"  exit code: {code}")
    say(f"  stdout ({len(out)} chars): {out.strip()[:1500] or '(empty)'}")
    if err.strip():
        say(f"  stderr: {err.strip()[:800]}")

    say("")
    say("-- tasklist rows that look like a language server --")
    code, out, _ = run(["tasklist"], timeout=20)
    for line in out.splitlines():
        if re.search(r"antigravity|language_server|codeium", line, re.IGNORECASE):
            say(f"  {line.rstrip()}")

    say("")
    say("-- discover_endpoints() result --")
    try:
        candidates, token, notes = antigravity.discover_endpoints()
        say(f"  candidates: {candidates}")
        say(f"  csrf_token: {'found' if token else 'missing'}")
        for n in notes:
            say(f"  note: {n}")
    except Exception as exc:                                    # noqa: BLE001
        say(f"  raised: {exc!r}")

    say("")
    say("-- raw GetUserStatus response --")
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        candidates, token, _ = antigravity.discover_endpoints()
        if not candidates:
            say("  (no endpoint to call)")
        else:
            for base_url, _port in candidates[:6]:
                try:
                    payload = antigravity.call_rpc(base_url, token)
                except Exception as exc:                        # noqa: BLE001
                    say(f"  {base_url}: {type(exc).__name__}: {str(exc)[:140]}")
                    continue
                say(f"  {base_url}: OK")
                dump_json(payload, 8000)
                break
    except Exception as exc:                                    # noqa: BLE001
        say(f"  raised: {exc!r}")


def main() -> int:
    say("QuotaTray probe report")
    say(f"Python {sys.version.split()[0]} on {sys.platform}")
    for fn in (probe_proxy, probe_claude, probe_codex, probe_antigravity):
        try:
            fn()
        except Exception as exc:                                # noqa: BLE001
            say(f"\n!! {fn.__name__} crashed: {exc!r}")
    OUT.write_text("\n".join(LINES), encoding="utf-8")
    print(f"\n\nReport written to: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
