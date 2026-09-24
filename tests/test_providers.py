"""Offline tests: drive every collection path with fabricated API responses,
session logs and process output. No network, no Windows required.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_tray.config import Config                                  # noqa: E402
from quota_tray.model import ProviderResult, humanize_delta, now_utc  # noqa: E402
from quota_tray.providers import antigravity, claude, codex           # noqa: E402
from quota_tray.ui.icon import render                                 # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' -- ' + str(extra)) if extra else ''}")


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def fake_session(get=None, post=None):
    s = types.SimpleNamespace()
    s.get = get or (lambda *a, **k: FakeResp({}, 404))
    s.post = post or (lambda *a, **k: FakeResp({}, 404))
    return s


# ------------------------------------------------------------------ Claude

print("\n--- Claude ---")
payload = {
    "five_hour": {"utilization": 42.0, "resets_at": "2026-09-13T18:00:00Z"},
    "seven_day": {"utilization": 78.5, "resets_at": "2026-09-16T00:00:00Z"},
    "seven_day_opus": {"utilization": 93.0, "resets_at": "2026-09-16T00:00:00Z"},
    "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": True, "monthly_limit": 100, "used_credits": 12,
                    "utilization": 12.0},
    "account": {"email_address": "me@example.com"},
}
# Route claude.ai calls through the fake `session` too, not a real curl_cffi one.
claude.web_session = lambda: None
claude.discover_oauth_token = lambda s: ("tok", "/fake/.credentials.json", "max", [])
claude.session = lambda: fake_session(get=lambda *a, **k: FakeResp(payload))
p = claude.ClaudeProvider(Config())
p.detect = lambda: True
r = p.fetch()
check("oauth path succeeds", r.ok, r.status)
check("four windows parsed", len(r.windows) == 4, [w.key for w in r.windows])
check("5-hour is 42%", next(w for w in r.windows if w.key == "five_hour").percent_text == "42%")
check("opus is 93%", next(w for w in r.windows if w.key == "seven_day_opus").percent_text == "93%")
check("extra usage detail", next(w for w in r.windows if w.key == "extra_usage").detail == "12 / 100")
check("account detected", r.account == "me@example.com", r.account)

claude.session = lambda: fake_session(get=lambda *a, **k: FakeResp({}, 401))
r = p.fetch()
check("401 degrades gracefully", (not r.ok) and "rejected" in r.attempts[0].detail,
      r.attempts[0].detail)

claude.session = lambda: fake_session(
    get=lambda *a, **k: FakeResp({"five_hour": {"utilization": 63}, "seven_day": {"utilization": 8}})
)
r = p.fetch()
check("0-100 scale handled", next(w for w in r.windows if w.key == "five_hour").percent_text == "63%")

# early in a window every value is <= 1; that is still a percentage, not a fraction
low = claude._build_windows({"five_hour": {"utilization": 1.0}, "seven_day": {"utilization": 0.0}},
                            "auto")
check("low utilization stays 1%", next(w for w in low if w.key == "five_hour").percent_text == "1%",
      [w.percent_text for w in low])

orgs = [{"uuid": "org-1", "name": "Personal", "capabilities": ["chat", "claude_max"]}]
usage = {"five_hour": {"utilization": 55.0, "resets_at": "2026-09-13T20:00:00Z"}}


def cookie_get(url, **kwargs):
    if url.endswith("/organizations"):
        return FakeResp(orgs)
    if url.endswith("/usage"):
        return FakeResp(usage)
    return FakeResp({}, 404)


claude.discover_oauth_token = lambda s: (None, "", None, ["no credentials"])
claude.session = lambda: fake_session(get=cookie_get)
cfg = Config({"providers": {"claude": {"session_key": "sk-fake"}}})
p2 = claude.ClaudeProvider(cfg)
p2.detect = lambda: True
r = p2.fetch()
check("cookie fallback succeeds", r.ok, r.status)
check("cookie path resolves org", r.plan == "max" and r.account == "Personal", (r.plan, r.account))
check("cookie path is 55%", r.windows[0].percent_text == "55%")

check("lastActiveOrg wins over the first chat org",
      claude._pick_org(orgs + [{"uuid": "org-2", "name": "Team", "capabilities": ["chat"]}],
                       prefer="org-2")[0] == "org-2")
check("chat org beats an earlier api-only org",
      claude._pick_org([{"uuid": "api", "capabilities": ["api"]}] + orgs)[0] == "org-1")

# desktop jar: org list blocked by Cloudflare, lastActiveOrg cookie still gets us there
seen_cookies = []


def blocked_orgs_get(url, **kwargs):
    seen_cookies.append(kwargs["headers"]["Cookie"])
    if url.endswith("/organizations"):
        resp = FakeResp({}, 403)
        resp.text = "<html><title>Just a moment...</title>cloudflare</html>"
        return resp
    if url == "https://claude.ai/api/organizations/org-9/usage":
        return FakeResp(usage)
    return FakeResp({}, 404)


claude.session = lambda: fake_session(get=blocked_orgs_get)
fake_cookies = types.ModuleType("quota_tray.win.chromium_cookies")
fake_cookies.get_cookies = lambda *a: (
    {"sessionKey": "sk-ant-sid01-x", "lastActiveOrg": "org-9", "cf_clearance": "zzz"}, [])
real_cookies = sys.modules.get("quota_tray.win.chromium_cookies")
real_platform = sys.platform
sys.modules["quota_tray.win.chromium_cookies"] = fake_cookies
sys.platform = "win32"
try:
    p3 = claude.ClaudeProvider(Config({"providers": {"claude": {"order": ["desktop_cookie"]}}}))
    p3.detect = lambda: True
    r = p3.fetch()
finally:
    sys.platform = real_platform
    if real_cookies is not None:
        sys.modules["quota_tray.win.chromium_cookies"] = real_cookies
    else:
        del sys.modules["quota_tray.win.chromium_cookies"]
check("desktop cookie survives blocked org list", r.ok, r.attempts[-1].detail)
check("lastActiveOrg forwarded, cf_clearance not",
      "lastActiveOrg=org-9" in seen_cookies[0] and "cf_clearance" not in seen_cookies[0],
      seen_cookies[:1])

claude.session = lambda: fake_session(get=blocked_orgs_get)
p4 = claude.ClaudeProvider(Config({"providers": {"claude": {
    "order": ["manual_cookie"], "session_key": "sk-fake"}}}))
p4.detect = lambda: True          # CI runners have no Claude install
r = p4.fetch()
check("cloudflare block is named", "Cloudflare" in r.attempts[-1].detail, r.attempts[-1].detail)
check("panel status names the real problem", "Cloudflare" in r.status, r.status)

sent = {}


class FakeWeb:
    def get(self, url, headers=None, timeout=None):
        sent.update(headers or {})
        return FakeResp(usage if url.endswith("/usage") else orgs)


claude.web_session = lambda: FakeWeb()
r = claude.ClaudeProvider(Config({"providers": {"claude": {
    "order": ["manual_cookie"], "session_key": "sk-fake"}}})).fetch()
check("browser-fingerprint client is used for claude.ai", r.ok and "User-Agent" not in sent, sent)
claude.web_session = lambda: None

# Claude Desktop keeps its own OAuth login in config.json, encrypted with the
# same OSCrypt key as its cookies.
from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # noqa: E402

from quota_tray.win import chromium_cookies as cc_mod           # noqa: E402

desk_key = bytes(range(32))
now_ms = now_utc().timestamp() * 1000


def seal(obj):
    nonce = b"\x02" * 12
    blob = b"v10" + nonce + AESGCM(desk_key).encrypt(nonce, json.dumps(obj).encode(), None)
    return __import__("base64").b64encode(blob).decode()


desk = Path(tempfile.mkdtemp()) / "Claude"
desk.mkdir()
(desk / "Local State").write_text("{}")
(desk / "config.json").write_text(json.dumps({
    "oauth:tokenCache": seal({"old:u:https://api.anthropic.com:user:inference user:profile":
                              {"token": "legacy-token", "expiresAt": now_ms + 9e6}}),
    "oauth:tokenCacheV2": seal({
        "i:u:https://api.anthropic.com:user:inference user:profile":
            {"token": "live-token", "expiresAt": now_ms + 3.6e6, "refreshToken": "r"},
        "i:u:https://api.anthropic.com:user:inference":
            {"token": "narrow-token", "expiresAt": now_ms + 9e6},
        "i:u:https://api.anthropic.com:user:profile user:inference x":
            {"token": "dead-token", "expiresAt": now_ms - 1000},
    }),
    "windowBounds": {"x": 1},
}))
real_master = cc_mod.master_key
cc_mod.master_key = lambda _p: desk_key
try:
    toks, notes = claude.desktop_oauth_tokens([desk])
finally:
    cc_mod.master_key = real_master
check("desktop login: V2 full-scope token first",
      [t for t, _ in toks][:1] == ["live-token"], (toks, notes))
check("desktop login: expired entries dropped", "dead-token" not in [t for t, _ in toks])

seen_auth = []


def usage_get(url, headers=None, **_k):
    seen_auth.append(headers.get("Authorization"))
    return FakeResp(payload) if url == claude.OAUTH_USAGE_URL else FakeResp({}, 404)


claude.discover_oauth_token = lambda s: (None, "", None, ["token expired"])
claude.desktop_oauth_tokens = lambda: ([("live-token", "config.json [oauth:tokenCacheV2]")], [])
claude.session = lambda: fake_session(get=usage_get)
old_order = Config({"providers": {"claude": {
    "order": ["oauth", "desktop_cookie", "manual_cookie"]}}})     # saved by v1.1.x
p5 = claude.ClaudeProvider(old_order)
p5.detect = lambda: True
r = p5.fetch()
check("desktop login works for configs saved before it existed",
      r.ok and r.source == "Claude Desktop login" and seen_auth[:1] == ["Bearer live-token"],
      (r.status, seen_auth))

# ------------------------------------------------------------------ Codex

print("\n--- Codex ---")
wham = {
    "rate_limits": {
        "primary": {"used_percent": 41.0, "window_minutes": 300, "resets_in_seconds": 3600},
        "secondary": {"used_percent": 9.0, "window_minutes": 10080, "resets_in_seconds": 200000},
    },
    "plan": "plus",
}
codex.session = lambda: fake_session(get=lambda *a, **k: FakeResp(wham))
cp = codex.CodexProvider(Config())
cp.detect = lambda: True
cp._auth_tokens = lambda: ({"access_token": "t", "account_id": "acc"}, "/fake/auth.json")
r = cp.fetch()
check("wham path succeeds", r.ok, r.status)
check("two codex windows", len(r.windows) == 2, [w.label for w in r.windows])
check("codex 5-hour is 41%", r.sorted_windows()[0].percent_text == "41%")
check("codex plan detected", r.plan == "plus", r.plan)

tmp = Path(tempfile.mkdtemp())
day = tmp / "sessions" / "2026" / "09" / "13"
day.mkdir(parents=True)
stamp = (now_utc() - timedelta(hours=3)).isoformat()
lines = [
    json.dumps({"timestamp": stamp, "type": "message", "payload": {"text": "hi"}}),
    json.dumps({"timestamp": stamp, "type": "event_msg", "payload": {
        "type": "token_count",
        "rate_limits": {
            "primary": {"used_percent": 77.5, "window_minutes": 299, "resets_in_seconds": 1800},
            "secondary": {"used_percent": 33.0, "window_minutes": 10079,
                          "resets_in_seconds": 300000},
        }}}),
    json.dumps({"timestamp": stamp, "type": "message", "payload": {"text": "bye"}}),
]
(day / "rollout-2026-09-13T10-00-00.jsonl").write_text("\n".join(lines), encoding="utf-8")
cfg = Config({"providers": {"codex": {"codex_home": str(tmp), "order": ["jsonl"]}}})
cp2 = codex.CodexProvider(cfg)
r = cp2.fetch()
check("jsonl fallback succeeds", r.ok, r.status)
check("jsonl reads 77.5%", r.sorted_windows()[0].percent_text == "78%",
      r.sorted_windows()[0].percent_text)
check("jsonl marked as snapshot", "offline snapshot" in r.status, r.status)
check("jsonl reset is record-relative", r.sorted_windows()[0].resets_at < now_utc(),
      r.sorted_windows()[0].resets_at)

# window labelling: key name loses to the actual reset horizon
for node, expect in [
    ({"primary": {"used_percent": 41, "window_minutes": 300},
      "secondary": {"used_percent": 9, "window_minutes": 10080}},
     {"5-hour window", "7-day window"}),
    ({"primary_window": {"used_percent": 18, "resets_in_seconds": 504000}},
     {"7-day window"}),
    ({"primaryWindow": {"usedPercent": 33, "window_size_seconds": 18000}},
     {"5-hour window"}),
]:
    got = {w.label for w in codex._windows_from_rate_limits(node, now_utc())}
    check(f"codex labels {sorted(expect)}", got == expect, got)

# merging: wham returns only the weekly window, the session log supplies the 5-hour one
wham_partial = {"rate_limits": {"primary_window": {"used_percent": 18.0,
                                                   "resets_in_seconds": 504000}}, "plan": "pro"}
codex.session = lambda: fake_session(get=lambda *a, **k: FakeResp(wham_partial))
tmp2 = Path(tempfile.mkdtemp())
day2 = tmp2 / "sessions" / "2026" / "09" / "13"
day2.mkdir(parents=True)
(day2 / "rollout-merge.jsonl").write_text(json.dumps({
    "timestamp": (now_utc() - timedelta(hours=1)).isoformat(),
    "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 63.0, "window_minutes": 300, "resets_in_seconds": 5400},
        "secondary": {"used_percent": 18.0, "window_minutes": 10080,
                      "resets_in_seconds": 504000}}}}), encoding="utf-8")
cp3 = codex.CodexProvider(Config({"providers": {"codex": {"codex_home": str(tmp2)}}}))
cp3.detect = lambda: True
cp3._auth_tokens = lambda: ({"access_token": "t"}, "/fake/auth.json")
r = cp3.fetch()
check("merge covers both windows",
      {w.label for w in r.windows} == {"5-hour window", "7-day window"},
      [w.label for w in r.windows])
check("merge names both sources", " + " in (r.source or ""), r.source)
check("merge flags partial staleness", "offline snapshot" in r.status, r.status)
check("merge dedupes the weekly window", len(r.windows) == 2, len(r.windows))

# ------------------------------------------------------------------ Antigravity

print("\n--- Antigravity ---")
ag_payload = {"userStatus": {
    "email": "dev@example.com",
    "planStatus": {"availablePromptCredits": 812,
                   "planInfo": {"monthlyPromptCredits": 1000, "planName": "Pro"}},
    "cascadeModelConfigData": {"clientModelConfigs": [
        {"label": "Gemini 3 Pro", "modelOrAlias": {"model": "MODEL_GEMINI_3_PRO"},
         "quotaInfo": {"remainingFraction": 0.35, "resetTime": "2026-09-13T22:00:00Z"}},
        {"label": "Claude Sonnet 4.5", "modelOrAlias": {"model": "MODEL_SONNET"},
         "quotaInfo": {"remainingFraction": 0.0}},
    ]}}}
antigravity.session = lambda: fake_session(post=lambda *a, **k: FakeResp(ag_payload))
antigravity.discover_endpoints = lambda: ([("https://127.0.0.1:51234", 51234)], "csrf", [])
ap = antigravity.AntigravityProvider(Config())
ap.detect = lambda: True
r = ap.fetch()
check("antigravity succeeds", r.ok, r.status)
check("credits plus two models", len(r.windows) == 3, [w.label for w in r.windows])
check("credits at 19%", r.sorted_windows()[0].percent_text == "19%")
check("exhausted model flagged", any(w.exhausted for w in r.windows))
check("email and plan parsed", r.account == "dev@example.com" and r.plan == "Pro")

antigravity.discover_endpoints = lambda: ([], None, ["no language server running"])
ap2 = antigravity.AntigravityProvider(Config())
ap2.detect = lambda: True
r = ap2.fetch()
check("IDE-not-running degrades", (not r.ok) and "not running" in r.status, r.status)

# ------------------------------------------------------------------ cookie store

print("\n--- Electron cookie store ---")
import sqlite3                                                   # noqa: E402

from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # noqa: E402

from quota_tray.win import chromium_cookies as cc               # noqa: E402

key = bytes(range(32))
nonce = b"\x01" * 12
host_hash = bytes([0x7F] + [0x41] * 31)         # happens to look printable-ish
enc = b"v10" + nonce + AESGCM(key).encrypt(nonce, host_hash + b"sk-ant-sid01-abc", None)
check("domain hash stripped when schema says so",
      cc.decrypt_value(enc, key, has_domain_hash=True) == "sk-ant-sid01-abc")
enc_old = b"v10" + nonce + AESGCM(key).encrypt(nonce, b"sk-ant-sid01-abc", None)
check("old schema left intact", cc.decrypt_value(enc_old, key, has_domain_hash=False)
      == "sk-ant-sid01-abc")

msix = Path(tempfile.mkdtemp())
root = msix / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
(root / "Network").mkdir(parents=True)
db = root / "Network" / "Cookies"
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
conn.execute("INSERT INTO meta VALUES ('version', '24')")
conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
             "encrypted_value BLOB, expires_utc INTEGER)")
conn.execute("INSERT INTO cookies VALUES ('.claude.ai', 'sessionKey', '', ?, 1)",
             (b"v10" + nonce + AESGCM(key).encrypt(nonce, b"\x00" * 32 + b"sk-ant-1", None),))
conn.execute("INSERT INTO cookies VALUES ('claude.ai', 'lastActiveOrg', 'org-9', x'', 1)")
conn.commit()
conn.close()
(root / "Partitions" / "webview" / "Network").mkdir(parents=True)

real_env = dict(cc.os.environ)
cc.os.environ["LOCALAPPDATA"] = str(msix)
cc.os.environ.pop("APPDATA", None)
try:
    roots = cc.app_roots("Claude")
finally:
    cc.os.environ.clear()
    cc.os.environ.update(real_env)
check("MSIX data dir discovered", root in roots, roots)
check("cookie DB found", cc.find_cookie_dbs(root) == [db], cc.find_cookie_dbs(root))
jar = cc.read_cookies(db, "%claude.ai", key)
check("sessionKey read and hash stripped", jar.get("sessionKey") == "sk-ant-1", jar)
check("plain cookies read too", jar.get("lastActiveOrg") == "org-9", jar)

# ------------------------------------------------------------------ updater

print("\n--- updater ---")
from quota_tray import __version__, updater                      # noqa: E402
from quota_tray.providers import base as providers_base          # noqa: E402

check("version parse", updater.parse_version("v1.2") == (1, 2, 0))
check("newer tag detected", updater.is_newer("v1.10.0", "1.9.3"))
check("same tag is not newer", not updater.is_newer(f"v{__version__}"))
check("junk tag ignored", not updater.is_newer("nightly"))

real_session = providers_base.session
providers_base.session = lambda: fake_session(
    get=lambda url, **k: FakeResp({"tag_name": "v99.0.0", "html_url": "https://x/rel"}))
found = updater.check("o/r")
check("check finds a newer release", found is not None and found.tag == "v99.0.0", found)
providers_base.session = lambda: fake_session(
    get=lambda url, **k: FakeResp({"tag_name": f"v{__version__}"}))
check("check is quiet when current", updater.check("o/r") is None)
providers_base.session = lambda: fake_session(get=lambda url, **k: FakeResp({}, 404))
check("check tolerates no releases", updater.check("o/r") is None)
providers_base.session = real_session

# ------------------------------------------------------------------ robustness

print("\n--- robustness ---")
big = Path(tempfile.mkdtemp()) / "rollout-big.jsonl"
with big.open("wb") as fh:
    fh.write(b'{"first": 1}\n')
    fh.write(b'{"rate_limits": {"primary": {"used_percent": 5}}}\n')
    fh.write(b'{"image": "' + b"A" * 3_000_000 + b'"}\n')      # a huge pasted image
    fh.write(b'{"rate_limits": {"primary": {"used_percent": 7}}}\n')
got = list(codex.iter_lines_reverse(big, max_bytes=10_000_000, chunk=64 * 1024))
check("reverse reader skips huge lines",
      [json.loads(x).get("rate_limits", {}).get("primary", {}).get("used_percent") for x in got]
      == [7, 5, None], [x[:40] for x in got])
check("reverse reader keeps the first line", got[-1] == '{"first": 1}', got[-1:])
small = list(codex.iter_lines_reverse(big, max_bytes=200))
check("reverse reader drops a cut-off line", len(small) == 1 and "7" in small[0], small)

bad_db = Path(tempfile.mkdtemp()) / "state.sqlite"
bad_db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 5000)
check("malformed sqlite is skipped", codex._scan_sqlite_for_rate_limits(bad_db) is None)


def boom(_probe):
    raise MemoryError()


cp4 = codex.CodexProvider(Config({"providers": {"codex": {"order": ["wham", "jsonl"]}}}))
cp4.detect = lambda: True
codex.session = lambda: fake_session(get=lambda *a, **k: FakeResp(wham))
cp4._auth_tokens = lambda: ({"access_token": "t"}, "/fake/auth.json")
cp4._try_jsonl = boom
r = cp4.fetch()
check("a crashing source keeps earlier windows", r.ok and len(r.windows) == 2, r.attempts)

from quota_tray.win import instances                              # noqa: E402

own = Path(tempfile.mkdtemp())
old_src = Path(tempfile.mkdtemp())
(old_src / "quota_tray").mkdir()
unrelated = Path(tempfile.mkdtemp())
rows = [
    {"ProcessId": 10, "Name": "pythonw.exe",
     "CommandLine": f'"{old_src}/.venv/Scripts/pythonw.exe" "{old_src}/run.pyw"'},
    {"ProcessId": 11, "Name": "QuotaTray.exe", "ExecutablePath": str(own / "QuotaTray.exe")},
    {"ProcessId": 12, "Name": "pythonw.exe", "CommandLine": f'pythonw "{unrelated}/run.pyw"'},
    {"ProcessId": 13, "Name": "QuotaTray.exe", "ExecutablePath": "/elsewhere/QuotaTray.exe"},
    {"ProcessId": 99, "Name": "QuotaTray.exe", "ExecutablePath": "/elsewhere/QuotaTray.exe"},
]
found = instances.parse_processes(rows, own, {99})
check("other installs found, ours and strangers left alone",
      sorted(pid for pid, _ in found) == [10, 13], found)

import logging                                                   # noqa: E402
import os                                                        # noqa: E402

from quota_tray import config as qt_config                       # noqa: E402

log_file = Path(tempfile.mkdtemp()) / "qt.log"
handler = qt_config._SafeRotatingFileHandler(log_file, maxBytes=200, backupCount=2,
                                             encoding="utf-8", delay=True)
handler.setFormatter(logging.Formatter("%(message)s"))
real_rename = os.rename


def locked_rename(*_a, **_k):
    raise PermissionError(32, "The process cannot access the file")


os.rename = locked_rename
try:
    for i in range(20):
        handler.emit(logging.makeLogRecord({"msg": f"line {i:02d} " + "x" * 30}))
finally:
    os.rename = real_rename
    handler.close()
check("logging survives a locked log file", "line 19" in log_file.read_text(encoding="utf-8"))

# ------------------------------------------------------------------ plan, credits, resets

print("\n--- plan, credits, resets ---")
from quota_tray import reminders                                  # noqa: E402
from quota_tray.providers import account                          # noqa: E402

check("plan names", [account.pretty_plan(x) for x in
                     ("default_claude_max_20x", "prolite", "plus", "default_claude_ai")]
      == ["Max 20x", "Pro Lite", "Plus", "Free"])

soon_iso = (now_utc() + timedelta(days=2)).isoformat()
later_iso = (now_utc() + timedelta(days=20)).isoformat()
claude_usage = {
    "five_hour": {"utilization": 30.0, "resets_at": later_iso},
    "seven_day": {"utilization": 60.0, "resets_at": later_iso},
    "spend": {"used": {"amount_minor": 1359, "currency": "USD", "exponent": 2},
              "limit": {"amount_minor": 5000, "currency": "USD", "exponent": 2},
              "percent": 27, "enabled": True},
    "cedar_ember": {"eligible": True, "grants": [
        {"id": "g1", "resets_total": 1, "resets_left": 1, "ends_at": later_iso,
         "clears": ["five_hour", "seven_day"], "usable_now": True, "percent_used": 0},
        {"id": "g2", "resets_left": 0, "ends_at": later_iso},
        {"id": "g3", "resets_left": 2, "ends_at": later_iso, "paused": True},
    ]},
}
profile = {"account": {"has_claude_max": True, "email_address": "me@example.com"},
           "organization": {"organization_type": "claude_max",
                            "rate_limit_tier": "default_claude_max_20x",
                            "subscription_status": "active",
                            "subscription_created_at": "2026-04-10T15:53:44.244879Z"}}


def account_get(url, **_k):
    if url == claude.OAUTH_USAGE_URL:
        return FakeResp(claude_usage)
    if url == claude.OAUTH_PROFILE_URL:
        return FakeResp(profile)
    return FakeResp({}, 404)


claude.session = lambda: fake_session(get=account_get)
claude._PROFILES.clear()
claude.discover_oauth_token = lambda s: ("tok-acct", "/fake/.credentials.json", None, [])
p6 = claude.ClaudeProvider(Config({"providers": {"claude": {"order": ["oauth"]}}}))
p6.detect = lambda: True
r = p6.fetch()
check("claude: grants and spend stay out of the bars", sorted(w.key for w in r.windows)
      == ["five_hour", "seven_day"], [w.key for w in r.windows])
check("claude: plan from profile", r.plan == "Max 20x" and r.account == "me@example.com",
      (r.plan, r.account))
info = {row.label: row.value for row in r.info}
check("claude: extra usage money", info.get("Extra usage") == "$13.59 of $50.00 used · $36.41 left",
      info)
check("claude: subscribed since", info.get("Subscribed", "").startswith("since Apr 10"), info)
check("claude: one usable reset (paused and spent grants skipped)",
      r.resets_available == 1 and "5-hour window + 7-day window" in r.resets[0].note,
      [(g.count, g.note) for g in r.resets])
rt = ProviderResult.from_cache(json.loads(json.dumps(r.to_cache())))
check("details survive the cache", rt.resets_available == 1 and len(rt.info) == len(r.info))

wham_full = {
    "plan_type": "prolite",
    "rate_limit": {"primary_window": {"used_percent": 10, "limit_window_seconds": 18000},
                   "secondary_window": {"used_percent": 20, "limit_window_seconds": 604800}},
    "credits": {"has_credits": True, "unlimited": False, "balance": "1026.11",
                "overage_limit_reached": False},
    "spend_control": {"reached": False, "individual_limit": {
        "unit": "credit", "limit": "2500", "used": "501.77", "used_percent": 20,
        "reset_at": 1790812800}},
    "rate_limit_reset_credits": {"available_count": 2},
}
reset_list = {"available_count": 2, "credits": [
    {"granted_at": "2026-09-01T00:00:00Z", "expires_at": soon_iso},
    {"granted_at": "2026-09-05T00:00:00Z", "expires_at": later_iso}]}
sub = {"plan_type": "pro", "active_until": later_iso, "billing_period": "monthly",
       "will_renew": True, "is_delinquent": False}
plan, rows = codex.codex_account_info(wham_full, sub, {})
vals = {row.label: row.value for row in rows}
check("codex: plan and renewal", plan == "Pro Lite" and vals["Subscription"].startswith("renews")
      and "(monthly)" in vals["Subscription"], vals)
check("codex: credits in dollars", vals.get("Credits") == "1,026 left (~$41.04)", vals)
check("codex: spend limit", vals.get("Spend limit", "").startswith("502 of 2,500 credits"), vals)
grants = codex.codex_resets(wham_full, reset_list)
check("codex: resets with expiry", sum(g.count for g in grants) == 2
      and all(g.expires_at for g in grants), grants)
check("codex: count without the list", sum(g.count for g in codex.codex_resets(wham_full, {})) == 2)
_hdr = __import__("base64").urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {
    "chatgpt_plan_type": "plus", "chatgpt_subscription_active_until": later_iso}}).encode()).decode()
plan, rows = codex.codex_account_info({}, {}, codex.jwt_claims(f"x.{_hdr.rstrip('=')}.y"))
check("codex: login-token fallback", plan == "Plus" and "may be stale" in rows[-1].value, rows)

cr = ProviderResult("codex", "Codex", ok=True)
cr.resets = grants
text = reminders.unused_resets_text([r, cr])
check("reminder lists both providers", text and "Claude: 1 unused reset" in text
      and "Codex: 2 unused resets" in text, text)
check("no reminder without resets", reminders.unused_resets_text([ProviderResult("x", "X")]) is None)
morning = datetime(2026, 9, 24, 9, 0)
check("reminder waits for the hour", not reminders.due(None, morning, 10))
check("reminder once a day", reminders.due(None, morning.replace(hour=11), 10)
      and not reminders.due("2026-09-24", morning.replace(hour=11), 10))

# ------------------------------------------------------------------ misc

print("\n--- misc ---")
src = ProviderResult("claude", "Claude", ok=True, status="connected", fetched_at=now_utc())
src.windows = list(claude._build_windows(payload, "auto"))
round_trip = ProviderResult.from_cache(json.loads(json.dumps(src.to_cache())))
check("cache round-trip", len(round_trip.windows) == len(src.windows)
      and round_trip.windows[0].percent == src.windows[0].percent)

img = render([("claude", 42.0), ("codex", 88.0), ("antigravity", None)])
check("tray icon renders", img.size == (128, 128))

t = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
check("countdown 3d 2h", humanize_delta(t + timedelta(days=3, hours=2), now=t) == "3d 2h")
check("countdown 4h 12m", humanize_delta(t + timedelta(hours=4, minutes=12), now=t) == "4h 12m")
check("countdown resetting", humanize_delta(t - timedelta(minutes=1), now=t) == "resetting")

print(f"\npassed {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("failures:", FAIL)
sys.exit(1 if FAIL else 0)
