using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using QuotaTray.Core;
using QuotaTray.Win;

namespace QuotaTray.Providers
{
    /// <summary>
    /// Doubao desktop client. It is an Electron app for www.doubao.com and keeps
    /// a sessionid cookie in its cookie store. With it, the profile endpoint
    /// reports the signed-in account and the subscription "overview" endpoint
    /// reports the plan and the window-limit usage (a short rolling window and a
    /// weekly one, the same shape as Claude's windows).
    /// </summary>
    public sealed class DoubaoProvider : Provider
    {
        public override string Id => "doubao";
        public override string Name => "Doubao";

        public const string ProfileUrl = "https://www.doubao.com/alice/profile/self";
        public const string OverviewUrl = "https://www.doubao.com/alice/commerce/sale/subscription/overview/";
        // The stable query params Doubao's web client sends; per-request signing
        // params (msToken, a_bogus, device_id) are left off and usually not required.
        private static readonly string[] OverviewParams = {
            "version_code", "20800", "language", "zh", "device_platform", "web",
            "aid", "497858", "real_aid", "497858", "region", "CN", "sys_region", "CN",
            "samantha_web", "1", "web_platform", "browser", "use-olympus-account", "1",
        };
        private static readonly string[] AppFolders = { "Doubao", "doubao" };
        private const string BrowserUa = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                          + "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

        private static readonly string[] PlanKeys = { "vip_type", "member_type", "membership", "plan",
            "subscription", "user_level", "vip_level", "member_level" };
        private static readonly Dictionary<string, string> PlanLabels = new Dictionary<string, string>
        {
            { "0", null }, { "1", "Member" }, { "2", "Pro" }, { "3", "Premium" },
        };
        private static readonly string[] RemainKeys = { "remaining", "remain_count", "left_count", "available", "quota_remaining" };

        /// <summary>Tests replace the cookie lookup: (sessionid, notes).</summary>
        public Func<Tuple<string, List<string>>> SessionOverride;

        public DoubaoProvider(Config config) : base(config) { }

        private static object Find(object node, string[] keys, int depth = 0)
        {
            if (depth > 6 || !(node is JObj obj)) return null;
            foreach (var kv in obj)
                if (keys.Contains(kv.Key.ToLowerInvariant()) && (kv.Value is string || kv.Value is bool || Json.AsNumber(kv.Value) != null))
                    return kv.Value;
            foreach (var kv in obj)
            {
                if (kv.Value is JObj)
                {
                    var found = Find(kv.Value, keys, depth + 1);
                    if (found != null) return found;
                }
                else if (kv.Value is List<object> list)
                    foreach (var item in list)
                    {
                        var found = Find(item, keys, depth + 1);
                        if (found != null) return found;
                    }
            }
            return null;
        }

        public static Tuple<string, string, List<InfoRow>> ParseProfile(JObj payload)
        {
            var data = payload.Obj("data");
            var profile = data?.Obj("profile_brief") ?? data ?? payload;
            string account = null;
            foreach (var key in new[] { "nickname", "nick_name", "user_name", "name" })
            {
                var v = profile.Str(key);
                if (!string.IsNullOrEmpty(v)) { account = v.Trim(); break; }
            }
            var rawPlan = Find(payload, PlanKeys);
            string plan = null;
            if (rawPlan is bool b) plan = b ? "Member" : null;
            else
            {
                var num = Json.AsNumber(rawPlan);
                if (num != null)
                {
                    var i = (int)num.Value;
                    plan = PlanLabels.TryGetValue(i.ToString(), out var known) ? known : (i != 0 ? "Level " + i : null);
                }
                else if (rawPlan is string s && s.Trim().Length > 0) plan = Account.Title(s.Trim());
            }
            var rows = new List<InfoRow>();
            var remain = Json.AsNumber(Find(payload, RemainKeys));
            if (remain != null) rows.Add(new InfoRow("Remaining", Account.N0(remain.Value)));
            return Tuple.Create(account, plan, rows);
        }

        // Doubao labels windows by window_type, not duration: 1 is the current
        // rolling period (length not fixed, so not named in hours), 2 is the
        // last-7-days window. Other enums stay generic until confirmed.
        private static string WindowLabel(double? windowType)
        {
            if (windowType == 1) return "Current period";
            if (windowType == 2) return "Last 7 days";
            return "Usage window";
        }

        private static bool Positive(object v)
        {
            var n = Json.AsNumber(v);
            return n != null && n.Value > 0;
        }

        /// <summary>(plan, windows) from the subscription overview response.</summary>
        public static Tuple<string, List<QuotaWindow>, List<InfoRow>> ParseOverview(JObj payload)
        {
            var data = payload?.Obj("data");
            string plan = null;
            var rows = new List<InfoRow>();
            var sub = data?.Obj("current_subscription");
            var disp = sub?.Obj("display");
            if (disp != null)
                foreach (var key in new[] { "short_name", "product_name" })
                {
                    var v = disp.Str(key);
                    if (!string.IsNullOrEmpty(v)) { plan = v.Trim(); break; }
                }
            if (sub != null)
            {
                // The validity date the app shows ("free trial until ..."): the
                // activity benefit end wins over the subscription's own period
                // end -- different fields, not to be conflated.
                var benefit = data?.Obj("campaign_benefit_info")?["benefit_end_time"];
                var isGift = sub["is_gift"] is bool g && g;
                DateTimeOffset? until = Positive(benefit) ? Time.Parse(benefit)
                    : Positive(sub["end_time"]) ? Time.Parse(sub["end_time"]) : (DateTimeOffset?)null;
                if (until != null)
                {
                    var label = (Positive(benefit) || isGift) ? "Bonus until" : "Plan until";
                    rows.Add(new InfoRow(label, until.Value.ToLocalTime().ToString("yyyy-MM-dd", CultureInfo.InvariantCulture)));
                }
            }
            var windows = new List<QuotaWindow>();
            var section = data?.Obj("window_limit_section");
            if (section != null)
            {
                var exhaustedAll = section["usage_exhausted"] is bool eb && eb;
                var order = 10;
                foreach (var group in (section.Arr("window_limit_groups") ?? new List<object>()).OfType<JObj>())
                {
                    var gname = group.Str("feature_group") ?? "";
                    foreach (var wl in (group.Arr("window_limits") ?? new List<object>()).OfType<JObj>())
                    {
                        var pctNum = wl.Num("used_percent");
                        if (pctNum == null) continue;
                        var pct = Math.Max(0.0, Math.Min(100.0, pctNum.Value));
                        var wtype = wl.Num("window_type");
                        var lessThanOne = wl["less_than_one_percent"] is bool lb && lb;
                        // start_time=end_time=0 is the "not started" sentinel (the
                        // period begins on first use), NOT an epoch of 1970.
                        var started = Positive(wl["start_time"]) || Positive(wl["end_time"]);
                        string detail = !started ? "not started"
                            : (lessThanOne && pct <= 0) ? "<1% used" : null;
                        windows.Add(new QuotaWindow($"doubao-{gname}-{wtype}-{order}",
                            WindowLabel(wtype), pct,
                            started ? Time.Parse(wl["end_time"]) : (DateTimeOffset?)null,
                            detail, order, exhaustedAll && pct >= 100));
                        order++;
                    }
                }
            }
            return Tuple.Create(plan, windows, rows);
        }

        private List<string> ExtraRoots()
        {
            var roots = new List<string>();
            var text = Core.Settings.Str(Settings, "data_dir");
            if (text.Length > 0) roots.Add(text);
            if (Core.Settings.Flag(Settings, "scan_browsers", false)) roots.AddRange(Proc.BrowserRoots());
            return roots;
        }

        public override bool Detect()
        {
            if (Core.Settings.Str(Settings, "session_id").Length > 0) return true;
            if (ExtraRoots().Any(Proc.SafeDirExists)) return true;
            return AppFolders.Any(f => Proc.AppRoots(f).Count > 0);
        }

        // Doubao answers a stale/invalid login with HTTP 200 and this code, not a 401.
        private static bool LoginInvalid(JObj payload)
        {
            if (payload == null) return false;
            var code = payload.Num("code");
            if (code == null || code.Value == 0) return false;
            var text = ((payload.Str("msg") ?? "") + " " + (payload.Str("message") ?? "")).ToLowerInvariant();
            return (int)code.Value == 710012001 || text.Contains("login invalid") || text.Contains("\u767b\u5f55");
        }

        private static string AsCookieHeader(string value) =>
            value == null ? null : (value.Contains("=") ? value : "sessionid=" + value);

        private Tuple<string, List<string>> CookieHeader()
        {
            if (SessionOverride != null)
            {
                var ov = SessionOverride();
                return Tuple.Create(AsCookieHeader(ov.Item1), ov.Item2);
            }
            // session_id may be a bare sessionid value or a whole "k=v; k=v" string.
            var manual = Core.Settings.Str(Settings, "session_id");
            if (manual.Length > 0) return Tuple.Create(AsCookieHeader(manual), new List<string> { "using session_id from config.json" });
            if (!Proc.IsWindows) return Tuple.Create((string)null, new List<string> { "reading the Doubao cookie is Windows-only" });
            var extra = ExtraRoots();
            var notes = new List<string>();
            for (var i = 0; i < AppFolders.Length; i++)
            {
                // The configured portable folder only needs searching once.
                var got = ChromiumCookies.GetCookies(AppFolders[i], "doubao.com", "sessionid", i == 0 ? extra : null);
                notes.AddRange(got.Item2);
                if (got.Item1.ContainsKey("sessionid") || got.Item1.ContainsKey("session_id"))
                {
                    // Doubao validates more than the lone sessionid, so send every
                    // cookie for the host, as the app does.
                    var header = string.Join("; ", got.Item1.Where(kv => !string.IsNullOrEmpty(kv.Value))
                        .Select(kv => kv.Key + "=" + kv.Value));
                    return Tuple.Create(header, notes);
                }
            }
            return Tuple.Create((string)null, notes);
        }

        public override void Collect(ProviderResult result)
        {
            var session = CookieHeader();
            if (session.Item1 == null)
            {
                result.Attempts.Add(new SourceAttempt("Doubao login", false,
                    session.Item2.Count > 0 ? string.Join("; ", session.Item2.Skip(Math.Max(0, session.Item2.Count - 3))) : "no Doubao sessionid cookie found"));
                result.Status = !result.Installed ? "Doubao not detected" : "sign in to Doubao Desktop first";
                return;
            }
            var headers = new Dictionary<string, string>
            {
                { "Cookie", session.Item1 },
                { "Accept", "application/json" },
                { "Referer", "https://www.doubao.com/chat/" },
                { "User-Agent", BrowserUa },
            };
            HttpReply resp;
            try { resp = Http.Get(ProfileUrl, headers); }
            catch (Exception e)
            {
                result.Attempts.Add(new SourceAttempt("Doubao profile", false, "request failed: " + e.Message));
                result.Status = "could not reach www.doubao.com";
                return;
            }
            var payload = Json.ParseObject(resp.Text);
            // Doubao answers an invalid/expired login with HTTP 200 and an error
            // code, not a 401, so that is checked before anything else.
            if (resp.Status == 401 || resp.Status == 403 || LoginInvalid(payload))
            {
                result.Attempts.Add(new SourceAttempt("Doubao profile", false, "login rejected (expired or incomplete cookie)"));
                result.Status = "Doubao login expired; re-sign in, or paste a fresh session_id";
                return;
            }
            if (resp.Status >= 400)
            {
                result.Attempts.Add(new SourceAttempt("Doubao profile", false, $"HTTP {resp.Status}: {Short(resp.Text, 160)}"));
                result.Status = $"Doubao profile HTTP {resp.Status}";
                return;
            }
            if (payload == null)
            {
                result.Attempts.Add(new SourceAttempt("Doubao profile", false, "response was not JSON (a login page?)"));
                result.Status = "Doubao returned no profile (open the app and sign in)";
                return;
            }
            var parsed = ParseProfile(payload);
            // The subscription overview adds the plan name, the plan validity
            // date and the window-limit usage; best effort (Doubao's web signing
            // may reject our call).
            var overview = Overview(session.Item1, result);
            var plan = overview.Item1 ?? parsed.Item2;
            if (parsed.Item1 == null && plan == null && overview.Item2.Count == 0)
            {
                result.Attempts.Add(new SourceAttempt("Doubao profile", false, "no account in response: " + Short(resp.Text, 160)));
                result.Status = "signed in, but Doubao returned no account";
                return;
            }
            var info = new List<InfoRow>(parsed.Item3);
            info.AddRange(overview.Item3);
            result.Ok = true;
            result.Account = parsed.Item1;
            result.Plan = plan;
            result.Info = info;
            result.Windows = overview.Item2;
            result.Headline = plan ?? "signed in";
            result.Source = "www.doubao.com/alice/commerce/sale/subscription/overview";
            result.Status = overview.Item2.Count > 0 ? "connected" : "connected (open Doubao's \u914d\u989d\u4e2d\u5fc3 for usage)";
            result.DataTime = Time.Now;
            result.Attempts.Add(new SourceAttempt("Doubao profile", true, parsed.Item1 ?? plan ?? "signed in"));
        }

        /// <summary>Plan, window-limit usage and plan-validity rows, best effort. Never throws.</summary>
        private Tuple<string, List<QuotaWindow>, List<InfoRow>> Overview(string cookieHeader, ProviderResult result)
        {
            var none = Tuple.Create((string)null, new List<QuotaWindow>(), new List<InfoRow>());
            var req = new HttpRequest { Method = "POST", Url = Http.Query(OverviewUrl, OverviewParams),
                Body = "{\"product_line\":\"membership\"}", TimeoutSeconds = 20 };
            req.Headers["Cookie"] = cookieHeader;
            req.Headers["Accept"] = "application/json, text/plain, */*";
            req.Headers["Content-Type"] = "application/json";
            req.Headers["agw-js-conv"] = "str";
            req.Headers["Referer"] = "https://www.doubao.com/chat/";
            req.Headers["User-Agent"] = BrowserUa;
            HttpReply resp;
            try { resp = Http.Send(req); }
            catch (Exception e)
            {
                result.Attempts.Add(new SourceAttempt("Doubao overview", false, "request failed: " + e.Message));
                return none;
            }
            if (resp.Status >= 400)
            {
                result.Attempts.Add(new SourceAttempt("Doubao overview", false, $"HTTP {resp.Status}"));
                return none;
            }
            var payload = Json.ParseObject(resp.Text);
            var code = payload?.Num("code");
            if (payload == null || (code != null && code.Value != 0))
            {
                result.Attempts.Add(new SourceAttempt("Doubao overview", false,
                    payload == null ? "response was not JSON" : $"code {(int)code.Value}: {Short(payload.Str("msg") ?? payload.Str("message") ?? "", 120)}"));
                return none;
            }
            var parsed = ParseOverview(payload);
            result.Attempts.Add(new SourceAttempt("Doubao overview", true,
                $"{parsed.Item2.Count} window(s)" + (parsed.Item1 != null ? ", " + parsed.Item1 : "")));
            return parsed;
        }
    }
}
