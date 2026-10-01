"""Offline tests: drive every collection path with fabricated API responses,
session logs and process output. No network, no Windows required.
"""
from __future__ import annotations

import base64
import json
import json as _json_mod
json_dumps = _json_mod.dumps
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


def set_login(token, origin, plan, notes):
    """Stub Claude Code login discovery with at most one login."""
    claude.discover_oauth_tokens = lambda s: ([(token, origin, plan)] if token else [], notes)



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
set_login("tok", "/fake/.credentials.json", "max", [])
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


set_login(None, "", None, ["no credentials"])
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


set_login(None, "", None, ["token expired"])
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

# A live answer is complete for the signed-in account: a session log must not
# top it up (after an account switch the logs hold the old account's numbers).
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
        "secondary": {"used_percent": 77.0, "window_minutes": 10080,
                      "resets_in_seconds": 504000}}}}), encoding="utf-8")
new_login = tmp2 / "auth.json"
new_login.write_text("{}")
_claims = __import__("base64").urlsafe_b64encode(
    json.dumps({"email": "new@example.com"}).encode()).decode().rstrip("=")
cp3 = codex.CodexProvider(Config({"providers": {"codex": {"codex_home": str(tmp2)}}}))
cp3.detect = lambda: True
cp3._auth_tokens = lambda: ({"access_token": "t", "id_token": f"h.{_claims}.s"}, str(new_login))
r = cp3.fetch()
check("live answer is not topped up from logs",
      [(w.label, w.percent) for w in r.windows] == [("7-day window", 18.0)],
      [(w.label, w.percent) for w in r.windows])
check("live answer names only the live source", r.source == "chatgpt.com/wham/usage", r.source)
check("codex card shows the signed-in account", r.account == "new@example.com", r.account)

# Without a live source, logs written before the current login are ignored...
codex.session = lambda: fake_session(get=lambda *a, **k: FakeResp({}, 401))
r = cp3.fetch()
check("logs from before the login are skipped", not r.ok and any(
    "newer than the current Codex login" in a.detail for a in r.attempts), r.attempts)
# ...and newer ones are used.
import os as _os                                                     # noqa: E402
old = (now_utc() - timedelta(hours=3)).timestamp()
_os.utime(new_login, (old, old))
r = cp3.fetch()
check("logs from after the login still work offline", r.ok and "offline snapshot" in r.status,
      r.status)
check("wham window length is read", {w.label for w in codex._windows_from_rate_limits(
    {"primary_window": {"used_percent": 5, "limit_window_seconds": 18000}}, now_utc())}
      == {"5-hour window"})
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


class Redirect:
    def __init__(self, status, location=""):
        self.status_code = status
        self.headers = {"Location": location} if location else {}


def upd_get(api, web):
    def get(url, **_k):
        return api() if "api.github.com" in url else web()
    return get


providers_base.session = lambda: fake_session(get=upd_get(
    lambda: FakeResp({"message": "API rate limit exceeded"}, 403),
    lambda: Redirect(302, "https://github.com/o/r/releases/tag/v99.1.0")))
rel, err = updater.fetch_latest("o/r")
check("update check falls back to the release page", rel and rel.tag == "v99.1.0" and err is None,
      (rel, err))


def boom_net():
    raise ConnectionError("proxy refused")


providers_base.session = lambda: fake_session(get=upd_get(boom_net, lambda: Redirect(404)))
rel, err = updater.fetch_latest("o/r")
check("update check explains a failure", rel is None and "proxy refused" in err
      and "github.com: HTTP 404" in err, err)
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
    {"ProcessId": 14, "Name": "QuotaTray-v1.3.2-windows-x64.exe",
     "ExecutablePath": "/downloads/QuotaTray-v1.3.2-windows-x64.exe"},
]
found = instances.parse_processes(rows, own, {99})
check("other installs found, ours and strangers left alone",
      sorted(pid for pid, _ in found) == [10, 13, 14], found)
found = instances.parse_processes(rows, own, {99}, include_own_dir=True)
check("a hung copy in our own folder can be replaced",
      sorted(pid for pid, _ in found) == [10, 11, 13, 14], found)

from quota_tray.win import trayicon                               # noqa: E402

folders = {"{6D809377-6AF0-444B-8957-A3773F02200E}": str(Path("/pf"))}
entries = [
    ("111", str(Path("/other/app.exe")), None),
    ("222", "{6D809377-6AF0-444B-8957-A3773F02200E}\\QuotaTray\\QuotaTray.exe", 0),
    ("333", str(Path("/soft/QuotaTray.exe")), None),
]
hit = trayicon.find_entries(entries, str(Path("/soft/QuotaTray.exe")), folders.get)
check("tray settings entry found by exe path", [e[0] for e in hit] == ["333"], hit)
hit = trayicon.find_entries(entries, str(Path("/pf/QuotaTray/QuotaTray.exe")), folders.get)
check("known-folder paths are expanded", [e[0] for e in hit] == ["222"], hit)
check("icon check is a no-op off Windows", trayicon.icon_present(None) is None)

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
set_login("tok-acct", "/fake/.credentials.json", None, [])
p6 = claude.ClaudeProvider(Config({"providers": {"claude": {"order": ["oauth"]}}}))
p6.detect = lambda: True
r = p6.fetch()
check("claude: grants and spend stay out of the bars", sorted(w.key for w in r.windows)
      == ["five_hour", "seven_day"], [w.key for w in r.windows])
check("claude: plan from profile", r.plan == "Max 20x" and r.account == "me@example.com",
      (r.plan, r.account))
info = {row.label: row.value for row in r.info}
check("claude: extra usage money", info.get("Extra usage") == "$13.59 of $50.00 used \u00b7 $36.41 left",
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

# Shape from openai/codex backend-client tests (rate_limit_resets_tests.rs).
official = {"credits": [
    {"id": "credit-1", "reset_type": "codex_rate_limits", "status": "available",
     "granted_at": "2026-06-17T00:00:00Z", "expires_at": later_iso, "redeemed_at": None,
     "title": "Full reset (Weekly + 5 hr)", "description": "Ready to redeem"},
    {"id": "credit-2", "reset_type": "codex_rate_limits", "status": "available",
     "granted_at": "2026-06-18T00:00:00Z", "expires_at": None},
    {"id": "credit-3", "status": "redeemed", "expires_at": later_iso,
     "redeemed_at": "2026-09-01T00:00:00Z"},
], "available_count": 2, "total_earned_count": 4}
og = codex.codex_resets({}, official)
check("codex: only available credits count", sum(g.count for g in og) == 2, og)
check("codex: reset title kept", any("Full reset (Weekly + 5 hr)" in g.note for g in og), og)
check("codex: string counts accepted",
      codex.codex_reset_count({"rate_limit_reset_credits": {"available_count": "1"}}, {}) == 1)


def wham_run(usage_payload, reset_status, reset_body):
    codex._EXTRAS.clear()

    def get(url, headers=None, **_k):
        if url == codex.WHAM_URL:
            return FakeResp(usage_payload)
        if url == codex.RESET_CREDITS_URL:
            return FakeResp(reset_body, reset_status)
        return FakeResp({}, 404)

    codex.session = lambda: fake_session(get=get)
    prov = codex.CodexProvider(Config({"providers": {"codex": {"order": ["wham"]}}}))
    prov.detect = lambda: True
    prov._auth_tokens = lambda: ({"access_token": "t", "account_id": "acc"}, "/fake/auth.json")
    return prov.fetch()


base_usage = {"plan_type": "pro", "rate_limit": wham_full["rate_limit"]}
rw = wham_run(base_usage, 200, official)
check("codex e2e: resets from the list", rw.resets_available == 2, rw.resets)
rw = wham_run({**base_usage, "rate_limit_reset_credits": {"available_count": 0}}, 403, {})
check("codex e2e: zero shows 'none available'",
      any(i.label == "Resets" and i.value == "none available" for i in rw.info), rw.info)
rw = wham_run(base_usage, 403, {})
row = next((i for i in rw.info if i.label == "Resets"), None)
check("codex e2e: failure is shown, not hidden", row is not None and "HTTP 403" in row.value, rw.info)
check("codex e2e: failure in diagnostics",
      any(a.name == "Codex account details" and "HTTP 403" in a.detail for a in rw.attempts),
      rw.attempts)
rw = wham_run({**base_usage, "rate_limit_reset_credits": {"available_count": 1}}, 403, {})
check("codex e2e: count survives a failed list", rw.resets_available == 1, rw.resets)

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

# Code-named entries, shaped like the user's v1.2.0 panel.
coded = {
    "five_hour": {"utilization": 5.0, "resets_at": later_iso},
    "seven_day": {"utilization": 45.0, "resets_at": later_iso},
    "seven_day_opus": {"utilization": 3.0, "resets_at": later_iso},
    "seven_day_overage_included": {"utilization": 12.0, "resets_at": later_iso},
    "iguana_necktie": {"utilization": 7.5, "resets_at": later_iso},
    "nimbus_quill": {"utilization": 0.0, "resets_at": None},
    "tangelo": None,
    "cedar_ember": {"eligible": True, "grants": [{"resets_left": 1, "ends_at": later_iso,
        "clears": ["five_hour", "seven_day", "seven_day_overage_included"]}]},
}
claude.session = lambda: fake_session(get=lambda url, **k: FakeResp(
    coded if url == claude.OAUTH_USAGE_URL else {}, 200 if url == claude.OAUTH_USAGE_URL else 404))
claude._PROFILES.clear()
set_login("tok-coded", "/fake/.credentials.json", None, [])
pc = claude.ClaudeProvider(Config({"providers": {"claude": {"order": ["oauth"]}}}))
pc.detect = lambda: True
r = pc.fetch()
labels = [w.label for w in r.sorted_windows()]
check("code names are not usage bars", labels == ["5-hour window", "7-day window", "7-day Opus",
                                                  "7-day overage allowance", "Cloud credit"], labels)
cloud = next((w for w in r.windows if w.label == "Cloud credit"), None)
check("iguana_necktie is the cloud credit bar", cloud is not None and cloud.percent == 7.5
      and cloud.credit and (cloud.detail or "").startswith("expires"), cloud)
check("cloud credit is last and not in the tray percentage",
      r.sorted_windows()[-1].label == "Cloud credit" and r.worst_percent == 45.0, r.worst_percent)
check("hidden experiments listed in diagnostics", any(
    a.name == "internal quotas (not shown)" and "nimbus_quill 0%" in a.detail for a in r.attempts),
      [a.detail for a in r.attempts])
check("reset note uses readable names",
      "5-hour window + 7-day window + 7-day overage allowance" in r.resets[0].note, r.resets[0].note)
check("unknown period keys read well", claude._label_for("seven_day_oauth_apps")[0] == "7-day Oauth Apps"
      and not claude.is_codename("extra_usage") and claude.is_codename("tangelo"))

# ------------------------------------------------------------------ several accounts

print("\n--- several accounts ---")


def jwt_with(email):
    body = __import__("base64").urlsafe_b64encode(json.dumps({"email": email}).encode())
    return "h." + body.decode().rstrip("=") + ".s"


def claude_usage_for(pct):
    return {"five_hour": {"utilization": pct}, "seven_day": {"utilization": pct / 2}}


profiles = {"Bearer cli-tok": "cli@example.com", "Bearer desk-tok": "desk@example.com",
            "Bearer desk-same": "cli@example.com"}
usages = {"Bearer cli-tok": 10.0, "Bearer desk-tok": 70.0, "Bearer desk-same": 10.0}


def multi_get(url, headers=None, **_k):
    who = headers.get("Authorization")
    if url == claude.OAUTH_USAGE_URL:
        return FakeResp(claude_usage_for(usages[who]))
    if url == claude.OAUTH_PROFILE_URL:
        return FakeResp({"account": {"email_address": profiles[who]}, "organization": {}})
    return FakeResp({}, 404)


claude.session = lambda: fake_session(get=multi_get)
claude._PROFILES.clear()
claude.discover_oauth_tokens = lambda s: ([("cli-tok", "C:/Users/me/.claude/.credentials.json",
                                            None)], [])
claude.desktop_oauth_tokens = lambda: ([("desk-tok", "config.json [oauth:tokenCacheV2]")], [])
pm = claude.ClaudeProvider(Config())
pm.detect = lambda: True
r = pm.fetch()
check("claude: two accounts, two pages", len(r.pages()) == 2
      and [p.label for p in r.pages()] == ["Claude Code", "Claude Desktop"]
      and [p.account for p in r.pages()] == ["cli@example.com", "desk@example.com"],
      [(p.label, p.account) for p in r.pages()])
check("claude: each page keeps its own numbers",
      [p.windows[0].percent for p in r.pages()] == [10.0, 70.0])
rt = ProviderResult.from_cache(json.loads(json.dumps(r.to_cache())))
check("account pages survive the cache", len(rt.pages()) == 2
      and rt.alternates[0].account == "desk@example.com")

claude.desktop_oauth_tokens = lambda: ([("desk-same", "config.json [oauth:tokenCacheV2]")], [])
r = pm.fetch()
check("claude: same account on both logins is one page",
      len(r.pages()) == 1 and r.label == "Claude Code + Claude Desktop", (len(r.pages()), r.label))
check("claude: WSL login is labelled",
      claude.login_label("\\\\wsl.localhost\\Arch\\root\\.claude\\.credentials.json")
      == "Claude Code - WSL Arch")

win_home, wsl_home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
for home, tok, mail in ((win_home, "win-tok", "app@example.com"), (wsl_home, "wsl-tok", "wsl@example.com")):
    (home / "auth.json").write_text(json.dumps({"tokens": {
        "access_token": tok, "account_id": tok, "id_token": jwt_with(mail)}}))
codex_pct = {"Bearer win-tok": 12.0, "Bearer wsl-tok": 88.0}


def codex_multi_get(url, headers=None, **_k):
    if url == codex.WHAM_URL:
        return FakeResp({"plan_type": "plus", "rate_limit": {"secondary_window": {
            "used_percent": codex_pct[headers["Authorization"]], "limit_window_seconds": 604800}}})
    return FakeResp({}, 404)


codex.session = lambda: fake_session(get=codex_multi_get)
codex._EXTRAS.clear()
real_homes = codex.codex_homes
codex.codex_homes = lambda s: [(win_home, "Codex"), (wsl_home, "Codex - WSL Arch")]
try:
    cm = codex.CodexProvider(Config({"providers": {"codex": {"order": ["wham"]}}}))
    cm.detect = lambda: True
    r = cm.fetch()
finally:
    codex.codex_homes = real_homes
check("codex: Windows and WSL accounts are two pages",
      [(p.label, p.account, p.windows[0].percent) for p in r.pages()]
      == [("Codex", "app@example.com", 12.0), ("Codex - WSL Arch", "wsl@example.com", 88.0)],
      [(p.label, p.account, [w.percent for w in p.windows]) for p in r.pages()])
check("codex: diagnostics say which login", any(a.name.startswith("[Codex - WSL Arch]")
                                                for a in r.attempts), [a.name for a in r.attempts])

r.alternates[0].resets = [__import__("quota_tray.model", fromlist=["ResetGrant"]).ResetGrant(
    1, now_utc() + timedelta(days=5))]
text = reminders.unused_resets_text([r])
check("reminder names the account that has the reset",
      text and "Codex (wsl@example.com): 1 unused reset" in text, text)

# ------------------------------------------------------------------ in-app installer

print("\n--- in-app installer ---")
import hashlib as _hashlib                                          # noqa: E402
import io as _io                                                    # noqa: E402
import zipfile as _zipfile                                          # noqa: E402

from quota_tray import selfupdate                                    # noqa: E402


class StreamResp:
    def __init__(self, body: bytes | None):
        self.status_code = 200 if body is not None else 404
        self.body = body or b""
        self.headers = {"Content-Length": str(len(self.body))}

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.body), 7):
            yield self.body[i:i + 7]

    def close(self):
        pass


def serve(files):
    """Fake github.com release downloads: {asset name: bytes}."""
    def get(url, **_k):
        return StreamResp(files.get(url.rsplit("/", 1)[-1]))
    return lambda: fake_session(get=get)


def sums_for(files):
    return "".join(f"{_hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in files.items()).encode()


def run_install(kind_dir, files, exe_name="QuotaTray.exe"):
    exe = kind_dir / exe_name
    steps = []
    providers_base.session = serve(files)
    sys.frozen, sys.executable = True, str(exe)
    try:
        plan = selfupdate.install("v9.0.0", "o/r", lambda s, n, t, f: steps.append((s, t)))
        return plan, steps, None
    except selfupdate.UpdateError as exc:
        return None, steps, str(exc)
    finally:
        del sys.frozen
        sys.executable = real_executable
        providers_base.session = real_session


real_executable, real_session = sys.executable, providers_base.session
new_name = f"QuotaTray-v9.0.0-windows-{selfupdate.arch()}.exe"

d = Path(tempfile.mkdtemp())
(d / "QuotaTray.exe").write_bytes(b"old version")
files = {new_name: b"NEW VERSION"}
files["QuotaTray-v9.0.0-SHA256SUMS.txt"] = sums_for(files)
plan, steps, err = run_install(d, files)
parked = list((d / selfupdate.TRASH_DIR).glob("QuotaTray.exe.*"))
check("exe update installs while the old one runs", err is None
      and (d / "QuotaTray.exe").read_bytes() == b"NEW VERSION"
      and not (d / "QuotaTray.exe.old").exists()
      and len(parked) == 1 and parked[0].read_bytes() == b"old version", (err, parked))
(d / "QuotaTray-v1.3.3.exe.old").write_bytes(b"left by v1.3.3")
check("the new version deletes the previous exe for good",
      selfupdate.cleanup_after_update(d, attempts=1)
      and sorted(p.name for p in d.iterdir()) == ["QuotaTray.exe"], sorted(p.name for p in d.iterdir()))
check("progress goes through every step", sorted({s for s, _ in steps}) == [1, 2, 3, 4], steps[-3:])
check("new version waits for the old one", plan and "--wait-pid" in plan.command
      and plan.command[0] == str(d / "QuotaTray.exe"), plan and plan.command)
check("no download leftovers", not (d / ".quotatray-update").exists()
      and not list(d.glob("*.new")) and not list(d.glob("*.part")))

d = Path(tempfile.mkdtemp())
(d / "QuotaTray.exe").write_bytes(b"old version")
bad = {new_name: b"TAMPERED", "QuotaTray-v9.0.0-SHA256SUMS.txt": sums_for({new_name: b"real"})}
plan, steps, err = run_install(d, bad)
check("checksum mismatch aborts and keeps the running version",
      err and "checksum" in err and (d / "QuotaTray.exe").read_bytes() == b"old version"
      and not (d / "QuotaTray.exe.old").exists(), err)

d = Path(tempfile.mkdtemp())
(d / "QuotaTray-v8-windows-x64.exe").write_bytes(b"old version")
legacy = {"QuotaTray.exe": b"NEW VERSION"}
legacy["SHA256SUMS.txt"] = sums_for(legacy)
plan, steps, err = run_install(d, legacy, "QuotaTray-v8-windows-x64.exe")
check("old asset names still work, installed name kept", err is None
      and (d / "QuotaTray-v8-windows-x64.exe").read_bytes() == b"NEW VERSION", err)

d = Path(tempfile.mkdtemp()) / "QuotaTray"
(d / "_internal").mkdir(parents=True)
(d / "QuotaTray.exe").write_bytes(b"old version")
buf = _io.BytesIO()
with _zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("QuotaTray.exe", b"NEW VERSION")
    zf.writestr("_internal/base_library.zip", b"x")
zname = new_name.replace(".exe", "-portable.zip")
pfiles = {zname: buf.getvalue()}
pfiles["QuotaTray-v9.0.0-SHA256SUMS.txt"] = sums_for(pfiles)
plan, steps, err = run_install(d, pfiles)
staged = d.with_name("QuotaTray.update")
check("portable update is staged beside the install", err is None
      and (staged / "QuotaTray.exe").read_bytes() == b"NEW VERSION"
      and (d / "QuotaTray.exe").read_bytes() == b"old version", err)
check("portable swap happens after exit", plan and plan.command[0] == "powershell.exe"
      and "Wait-Process" in plan.command[-1] and "robocopy" in plan.command[-1], plan and plan.command)
check("sums parser", selfupdate.parse_sums("ab" * 32 + "  *a b.exe\n") == {"a b.exe": "ab" * 32})

from quota_tray.win.proc import independent_env                    # noqa: E402

inherited = {"PATH": "/bin", "_PYI_ARCHIVE_FILE": "C:/app.exe", "_PYI_APPLICATION_HOME_DIR": "C:/T/_MEI1",
             "_MEIPASS2": "C:/T/_MEI1", "_PYI_PARENT_PROCESS_LEVEL": "1"}
fresh = independent_env(inherited)
check("new copies start with a clean PyInstaller environment",
      fresh == {"PATH": "/bin", "PYINSTALLER_RESET_ENVIRONMENT": "1"}, fresh)

from quota_tray import app as qt_app                                 # noqa: E402

launched = []
import subprocess as _subprocess                                     # noqa: E402

orig_popen = _subprocess.Popen
_subprocess.Popen = lambda cmd, **kw: launched.append((cmd, kw.get("env", {})))
sys.frozen = True
try:
    stepped_aside = qt_app._relaunch_clean(["--wait-pid", "4321", "--updated-from", "1.3.3"])
    again = qt_app._relaunch_clean(["--wait-pid", "4321", "--updated-from", "1.3.3", "--fresh"])
finally:
    _subprocess.Popen = orig_popen
    del sys.frozen
check("copy started by an old version relaunches clean once",
      stepped_aside and not again and len(launched) == 1
      and launched[0][0][-1] == "--fresh" and "4321" in launched[0][0]
      and launched[0][1].get("PYINSTALLER_RESET_ENVIRONMENT") == "1", launched)

# ------------------------------------------------------------------ TLS bundle

print("\n--- TLS bundle ---")
from quota_tray import tls                                         # noqa: E402

for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
    os.environ.pop(var, None)
stable = tls.ca_bundle()
check("CA bundle copied out of the package",
      stable is not None and Path(stable).is_file()
      and Path(stable).parent == qt_config.app_dir(), stable)
check("CA bundle copy is complete", Path(stable).stat().st_size
      == Path(__import__("certifi").where()).stat().st_size)
custom = Path(tempfile.mkdtemp()) / "corp.pem"
custom.write_text("x")
os.environ["REQUESTS_CA_BUNDLE"] = str(custom)
check("a configured CA bundle wins", tls.ca_bundle() == str(custom))
os.environ.pop("REQUESTS_CA_BUNDLE")

mei = Path(tempfile.mkdtemp()) / "_MEI12345"
(mei / "certifi").mkdir(parents=True)
(mei / "certifi" / "cacert.pem").write_text("x")
sys.frozen, sys._MEIPASS = True, str(mei)
try:
    healthy = tls.bundle_damaged()
    (mei / "certifi" / "cacert.pem").unlink()        # what Windows temp cleanup does
    damaged = tls.bundle_damaged()
finally:
    del sys.frozen, sys._MEIPASS
check("intact unpacked folder is fine", healthy is False)
check("cleaned-up unpacked folder is detected", damaged is True)
check("source runs are never 'damaged'", tls.bundle_damaged() is False)

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

# ------------------------------------------------------------------ Gemini CLI

print("\n--- Gemini CLI ---")
from quota_tray.providers import gemini                           # noqa: E402

tok, err = gemini.access_token({"access_token": "live", "expiry_date": (now_utc().timestamp() + 3600) * 1000})
check("gemini: fresh token used as-is", tok == "live" and err is None)
tok, err = gemini.access_token({"access_token": "old", "expiry_date": 1})
check("gemini: expired token asks to run gemini", tok is None and "run `gemini`" in err, (tok, err))
tok, err = gemini.access_token({})
check("gemini: no token explained", tok is None and "no access token" in err, (tok, err))

gid = base64.urlsafe_b64encode(json.dumps({"email": "me@gmail.com"}).encode()).decode().rstrip("=")
load_payload = {"currentTier": {"id": "standard-tier", "name": "Standard"},
                "cloudaicompanionProject": "proj-123"}
quota_payload = {"buckets": [
    {"modelId": "gemini-2.5-pro", "remainingFraction": 0.4, "remainingAmount": "600",
     "tokenType": "REQUESTS", "resetTime": later_iso},
    {"modelId": "gemini-2.5-flash", "remainingFraction": 1.0, "resetTime": later_iso}]}
seen_bodies = []


def gemini_post(url, headers=None, json=None, **_k):
    seen_bodies.append((url, json))
    if url.endswith(":loadCodeAssist"):
        return FakeResp(load_payload)
    if url.endswith(":retrieveUserQuota"):
        return FakeResp(quota_payload)
    return FakeResp({}, 404)


gemini.session = lambda: fake_session(post=gemini_post)
gemini.discover_credentials = lambda settings: (
    [({"access_token": "t", "expiry_date": (now_utc().timestamp() + 3600) * 1000, "id_token": f"h.{gid}.s"},
      "Gemini CLI")], [])
gp = gemini.GeminiProvider(Config())
gp.detect = lambda: True
r = gp.fetch()
check("gemini connects", r.ok and r.source == "cloudcode-pa.googleapis.com", r.status)
check("gemini: two model windows", [w.label for w in r.sorted_windows()] == ["Gemini 2.5 Pro", "Gemini 2.5 Flash"],
      [w.label for w in r.sorted_windows()])
check("gemini: 2.5 pro is 60% used", r.sorted_windows()[0].percent_text == "60%" and r.sorted_windows()[0].detail == "600 left (requests)",
      (r.sorted_windows()[0].percent_text, r.sorted_windows()[0].detail))
check("gemini: flash unused", r.sorted_windows()[1].percent == 0.0)
check("gemini: plan and account", r.plan == "Standard" and r.account == "me@gmail.com", (r.plan, r.account))
check("gemini: quota query used the project id", seen_bodies[-1] == (gemini.ENDPOINT + ":retrieveUserQuota", {"project": "proj-123"}),
      seen_bodies[-1])
check("gemini: subscription tab", r.billing == "subscription")
check("gemini: survives the cache", ProviderResult.from_cache(json.loads(json_dumps(r.to_cache()))).plan == "Standard")


def gemini_401(url, headers=None, json=None, **_k):
    return FakeResp({}, 401)


gemini.session = lambda: fake_session(post=gemini_401)
r = gemini.GeminiProvider(Config()).fetch()
check("gemini: 401 is explained", not r.ok and "sign in to Gemini again" in r.status, r.status)

# ------------------------------------------------------------------ TRAE

print("\n--- TRAE ---")
from quota_tray.providers import trae                             # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher as _Cipher, algorithms as _algs, modes as _modes  # noqa: E402


def _trae_encrypt(plaintext, private=False):
    import hashlib as _h
    import os as _os
    key_material = _os.urandom(32)
    merged = _h.sha512(_h.sha512(key_material).digest() + trae._salt(private)).digest()
    enc = _Cipher(_algs.AES(merged[:16]), _modes.CBC(merged[16:32])).encryptor()
    payload = _h.sha512(plaintext).digest() + plaintext
    pad = 16 - (len(payload) % 16)
    padded = payload + bytes([pad]) * pad
    ct = enc.update(padded) + enc.finalize()
    header = trae._PREFIX_AES_PRIVATE if private else trae._PREFIX_AES
    return (trae._PREFIX_AES if not private else header)[:0] + header + key_material + ct


_auth_doc = {"accessToken": "trae-jwt-123", "email": "me@trae.cn"}
_blob = __import__("base64").b64encode(_trae_encrypt(json.dumps(_auth_doc).encode())).decode()
check("trae: byte-crypto round trip", trae.byte_crypto_decrypt(__import__("base64").b64decode(_blob)) is not None
      and json.loads(trae.byte_crypto_decrypt(__import__("base64").b64decode(_blob))) == _auth_doc)
check("trae: tampered blob rejected",
      trae.byte_crypto_decrypt(__import__("base64").b64decode(_blob)[:-1] + b"\x00") is None)

_store = Path(tempfile.mkdtemp()) / "storage.json"
_store.write_text(json.dumps({"iCubeAuthInfo://icube.cloudide": _blob,
                              "iCubeAuthInfo://usertag": "ignored"}), encoding="utf-8")
tok, mail = trae.read_storage_auth(_store)
check("trae: token and email from storage.json", tok == "trae-jwt-123" and mail == "me@trae.cn", (tok, mail))
_plain_store = Path(tempfile.mkdtemp()) / "storage.json"
_plain_store.write_text(json.dumps({"iCubeAuthInfo://icube.cloudide": json.dumps({"token": "plain-jwt"})}),
                        encoding="utf-8")
check("trae: plain-JSON storage still works", trae.read_storage_auth(_plain_store)[0] == "plain-jwt")

usage_payload = {"code": 0, "user_entitlement_pack_list": [
    {"entitlement_base_info": {"product_type": 1, "end_time": int((now_utc().timestamp() + 15 * 86400))},
     "usage": {"premium_model_fast_amount": 150},
     "product_extra_ignored": True,
     "status": 1}]}
usage_payload["user_entitlement_pack_list"][0]["entitlement_base_info"]["quota"] = {
    "premium_model_fast_request_limit": 600}
windows, rows, plan = trae.parse_usage(usage_payload)
check("trae: fast request window", len(windows) == 1 and windows[0].label == "Fast requests"
      and windows[0].percent_text == "25%" and windows[0].detail == "150 / 600", (windows and windows[0].__dict__))
check("trae: plan from product_type", plan == "Pro", plan)
check("trae: renews row", any(r.label == "Renews" for r in rows), rows)
unlimited = {"user_entitlement_pack_list": [{"entitlement_base_info": {"product_type": 6,
             "quota": {"premium_model_fast_request_limit": -1}}, "usage": {"premium_model_fast_amount": 5}}]}
uw, _r, up = trae.parse_usage(unlimited)
check("trae: unlimited fast requests", uw[0].detail == "unlimited" and uw[0].percent == 0.0 and up == "Ultra", (uw[0].__dict__, up))
ps_plan, ps_row = trae.parse_pay_status({"code": 0, "user_pay_identity_str": "Pro+",
    "detail": {"subscription_renew_time": int(now_utc().timestamp() + 30 * 86400)}})
check("trae: pay status plan and renew", ps_plan == "Pro+" and ps_row is not None, (ps_plan, ps_row))

seen_trae = []


def trae_post(url, headers=None, json=None, **_k):
    seen_trae.append((url, headers.get("Authorization")))
    if url.endswith(trae.ENT_USAGE_PATH):
        return FakeResp(usage_payload)
    if url.endswith(trae.PAY_STATUS_PATH):
        return FakeResp({"code": 0, "user_pay_identity_str": "Pro"})
    return FakeResp({}, 404)


trae.session = lambda: fake_session(post=trae_post)
tp = trae.TraeProvider(Config())
tp.detect = lambda: True
tp._page_token = ("trae-jwt-123", "me@trae.cn")
trae._storage_paths = lambda settings: [(_store, True, "TRAE CN")]
r = tp.fetch()
check("trae connects (CN host, Cloud-IDE-JWT)", r.ok and r.source == "api.trae.cn"
      and seen_trae[0][1] == "Cloud-IDE-JWT trae-jwt-123", (r.status, seen_trae[:1]))
check("trae: fast window in panel", r.sorted_windows()[0].label == "Fast requests" and r.plan == "Pro", (r.plan, [w.label for w in r.windows]))
check("trae: account and subscription tab", r.account == "me@trae.cn" and r.billing == "subscription")
check("trae: survives the cache", ProviderResult.from_cache(json.loads(json.dumps(r.to_cache()))).plan == "Pro")

# ------------------------------------------------------------------ Doubao

print("\n--- Doubao ---")
from quota_tray.providers import doubao                           # noqa: E402

acct, plan, rows = doubao.parse_profile({"data": {"profile_brief": {
    "nickname": "\u5c0f\u8c46", "user_name": "doubao_user", "id": 42, "vip_type": 2}}})
check("doubao: account and plan from profile", acct == "\u5c0f\u8c46" and plan == "Pro", (acct, plan))
acct2, plan2, rows2 = doubao.parse_profile({"data": {"profile_brief": {"nickname": "Free User", "is_vip": False}}})
check("doubao: free account has no plan", acct2 == "Free User" and plan2 is None, (acct2, plan2))
_, _, rows3 = doubao.parse_profile({"data": {"profile_brief": {"nickname": "x"}, "benefit": {"remaining": 88}}})
check("doubao: a remaining count is shown if present", any(r.label == "Remaining" and r.value == "88" for r in rows3), rows3)


def doubao_get(url, headers=None, **_k):
    check("doubao: sends the sessionid cookie", "sessionid=sk-cookie" in (headers or {}).get("Cookie", ""),
          headers)
    return FakeResp({"data": {"profile_brief": {"nickname": "\u5c0f\u8c46", "vip_type": 2}}})


doubao.session = lambda: fake_session(get=doubao_get)
dp = doubao.DoubaoProvider(Config({"providers": {"doubao": {"session_id": "sk-cookie"}}}))
dp.detect = lambda: True
r = dp.fetch()
check("doubao connects", r.ok and r.source == "www.doubao.com/alice/profile/self", r.status)
check("doubao: account, plan, subscription tab", r.account == "\u5c0f\u8c46" and r.plan == "Pro"
      and r.billing == "subscription", (r.account, r.plan))
check("doubao: says usage is in the app", "config" not in r.status.lower() and "central" not in r.status.lower(),
      r.status)
check("doubao: survives the cache", ProviderResult.from_cache(json.loads(json.dumps(r.to_cache()))).plan == "Pro")


def doubao_401(url, headers=None, **_k):
    return FakeResp("<html>login</html>", 401)


doubao.session = lambda: fake_session(get=doubao_401)
r = doubao.DoubaoProvider(Config({"providers": {"doubao": {"session_id": "expired"}}})).fetch()
check("doubao: expired login explained", not r.ok and "expired" in r.status.lower(), r.status)

# ------------------------------------------------------------------ DeepSeek

print("\n--- DeepSeek ---")
from quota_tray.providers import deepseek                         # noqa: E402

check("jsonc comments and trailing commas", json.loads(deepseek.strip_jsonc(
    '{\n // note\n "a": "http://x//y", /* block */ "b": [1, 2,],\n}')) == {"a": "http://x//y", "b": [1, 2]})
v1 = "version: 1\n\nrefs:\n  # mine\n  DEEPSEEK_API_KEY: sk-v1key\n  OPENAI_API_KEY: sk-other\nrecords:\n  x/y:\n    kind: grant\n"
check("dsh credentials (version 1)", deepseek.dsh_credentials_key(v1) == "sk-v1key")
check("dsh credentials (flat, quoted)", deepseek.dsh_credentials_key("DEEPSEEK_API_KEY: 'sk-flat'\n") == "sk-flat")
check("dsh credentials without the key", deepseek.dsh_credentials_key("version: 1\nrefs:\n  OPENAI_API_KEY: x\n") is None)
check("dotenv key", deepseek.dotenv_key('export DEEPSEEK_API_KEY="sk-env" # comment\n') == "sk-env")
check("opencode auth.json", deepseek.opencode_auth_keys(
    {"deepseek": {"type": "api", "key": "sk-oc"}, "anthropic": {"type": "oauth"}}) == ["sk-oc"])
check("opencode.json key, env reference, relay skipped", deepseek.opencode_config_keys({"provider": {
    "deepseek": {"options": {"apiKey": "{env:MY_DS}"}},
    "ds-relay": {"options": {"baseURL": "https://relay.example/v1", "apiKey": "sk-relay"}},
    "mine": {"options": {"baseURL": "https://api.deepseek.com/v1", "apiKey": "sk-cfg"}},
}}, env={"MY_DS": "sk-fromenv"}) == ["sk-fromenv", "sk-cfg"])

check("opencode labels", deepseek.opencode_label(True, True) == "OpenCode CLI + OpenCode Desktop"
      and deepseek.opencode_label(False, True) == "OpenCode Desktop"
      and deepseek.opencode_label(True, False) == "OpenCode CLI"
      and deepseek.opencode_label(False, False) == "OpenCode"
      and deepseek.opencode_label(True, True, "Ubuntu", {"Ubuntu"}) == "OpenCode CLI + OpenCode Desktop - WSL Ubuntu"
      and deepseek.opencode_label(True, True, "Arch", {"Ubuntu"}) == "OpenCode CLI - WSL Arch")
oc_appdata = Path(tempfile.mkdtemp())
(oc_appdata / "ai.opencode.desktop").mkdir()
(oc_appdata / "ai.opencode.desktop" / "opencode.settings").write_text(
    json.dumps({"wslServers": {"servers": [{"id": "wsl:Ubuntu", "distro": "Ubuntu"}]}}), encoding="utf-8")
desk, desk_distros = deepseek.opencode_desktop({"APPDATA": str(oc_appdata)})
check("opencode desktop and its WSL servers", desk is not None and desk_distros == {"Ubuntu"})
oc_bin = Path(tempfile.mkdtemp())
(oc_bin / "opencode.exe").write_text("", encoding="utf-8")
check("opencode cli on PATH", deepseek.opencode_cli(Path(tempfile.mkdtemp()), {"PATH": str(oc_bin)}, True)
      == oc_bin / "opencode.exe")
check("no opencode cli", deepseek.opencode_cli(Path(tempfile.mkdtemp()), {"PATH": ""}, True) is None)
ds_home = Path(tempfile.mkdtemp())
(ds_home / ".local" / "share" / "opencode").mkdir(parents=True)
(ds_home / ".local" / "share" / "opencode" / "auth.json").write_text(
    json.dumps({"deepseek": {"type": "api", "key": "sk-shared1234"}}), encoding="utf-8")
(ds_home / ".dsh").mkdir()
(ds_home / ".dsh" / ".credentials.yaml").write_text(
    "version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-shared1234\n", encoding="utf-8")
deepseek._homes = lambda settings: [(ds_home, "")]
found, _notes = deepseek.discover_keys({"scan_wsl": False}, env={"DEEPSEEK_API_KEY": "sk-envonly9999"})
check("keys from OpenCode, dsh and the environment", [w for _k, w in found]
      == ["OpenCode", "DeepSeek Harness", "env DEEPSEEK_API_KEY"], found)

seen_keys = []


def balance_get(url, headers=None, **_k):
    seen_keys.append((url, headers.get("Authorization")))
    if headers.get("Authorization") == "Bearer sk-envonly9999":
        return FakeResp({"is_available": False, "balance_infos": [
            {"currency": "USD", "total_balance": "0.40", "granted_balance": "0.00", "topped_up_balance": "0.40"}]})
    return FakeResp({"is_available": True, "balance_infos": [
        {"currency": "CNY", "total_balance": "110.00", "granted_balance": "10.00", "topped_up_balance": "100.00"}]})


deepseek.session = lambda: fake_session(get=balance_get)
real_discover = deepseek.discover_keys
deepseek.discover_keys = lambda settings, env=None: real_discover(
    settings, env={"DEEPSEEK_API_KEY": "sk-envonly9999"})
dr = deepseek.DeepSeekProvider(Config()).fetch()
check("deepseek: one page per key, shared key merged", dr.ok and len(dr.pages()) == 2
      and dr.label == "OpenCode + DeepSeek Harness" and dr.alternates[0].label == "env DEEPSEEK_API_KEY",
      [(p.label, p.status) for p in dr.pages()])
dinfo = {row.label: (row.value, row.tone) for row in dr.info}
check("deepseek: balance in yuan", dinfo.get("Balance") == ("\u00a5110.00", "good") and dr.headline == "\u00a5110.00",
      dinfo)
check("deepseek: topped up and granted", any("\u00a5100.00 topped up" in row.value and "granted" in row.value
                                             for row in dr.info), dr.info)
low = dr.alternates[0]
check("deepseek: low balance warns", {row.label: row.tone for row in low.info}.get("Balance") == "warn"
      and any(row.label == "Status" for row in low.info), low.info)
check("deepseek: pay-as-you-go tab", dr.billing == "payg" and dr.plan == "Pay as you go"
      and dr.account == "key sk-...1234")
check("deepseek: keys only go to api.deepseek.com", {u for u, _a in seen_keys} == {deepseek.BALANCE_URL})
check("deepseek: survives the cache", ProviderResult.from_cache(json.loads(json.dumps(dr.to_cache()))).billing == "payg")
check("deepseek: full key kept for hover / Copy", dr.secret == "sk-shared1234" and dr.alternates[0].secret == "sk-envonly9999")
check("deepseek: full key never cached", "sk-shared1234" not in json.dumps(dr.to_cache())
      and ProviderResult.from_cache(dr.to_cache()).secret is None)
deepseek.discover_keys = lambda settings, env=None: ([], ["none"])
nr = deepseek.DeepSeekProvider(Config()).fetch()
check("deepseek: no key, not installed", not nr.ok and not nr.installed)
deepseek.discover_keys = real_discover

# ------------------------------------------------------------------ shared reads

print("\n--- shared credential reads ---")
from quota_tray.win import shareio                                 # noqa: E402

_sdir = Path(tempfile.mkdtemp())
_sf = _sdir / "oauth_creds.json"
_sf.write_text(json.dumps({"access_token": "abc"}), encoding="utf-8")
check("shareio: reads a credential file", shareio.read_text(_sf) is not None
      and json.loads(shareio.read_text(_sf))["access_token"] == "abc")
_sf.write_bytes(b"\xef\xbb\xbf" + json.dumps({"x": 1}).encode())
check("shareio: strips a UTF-8 BOM", json.loads(shareio.read_text(_sf)) == {"x": 1})
check("shareio: missing file is None", shareio.read_text(_sdir / "nope.json") is None)
# The owning CLI must be able to replace the file; the reader never holds it.
import os as _os                                                   # noqa: E402
_before = shareio.read_text(_sf)
_tmp = _sf.with_suffix(".tmp")
_tmp.write_text(json.dumps({"x": 2}), encoding="utf-8")
_os.replace(_tmp, _sf)
check("shareio: owner can replace after a read", json.loads(shareio.read_text(_sf)) == {"x": 2})

print(f"\npassed {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("failures:", FAIL)
sys.exit(1 if FAIL else 0)
