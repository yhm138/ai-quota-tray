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
    "five_hour": {"utilization": 0.42, "resets_at": "2026-09-13T18:00:00Z"},
    "seven_day": {"utilization": 0.785, "resets_at": "2026-09-16T00:00:00Z"},
    "seven_day_opus": {"utilization": 0.93, "resets_at": "2026-09-16T00:00:00Z"},
    "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": True, "monthly_limit": 100, "used_credits": 12,
                    "utilization": 0.12},
    "account": {"email_address": "me@example.com"},
}
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

orgs = [{"uuid": "org-1", "name": "Personal", "capabilities": ["chat", "claude_max"]}]
usage = {"five_hour": {"utilization": 0.55, "resets_at": "2026-09-13T20:00:00Z"}}


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
