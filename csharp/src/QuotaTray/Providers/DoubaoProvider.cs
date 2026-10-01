using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;
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
        // The captured overview succeeds with these stable query params alone.
        // Do not persist transient browser signing parameters or credentials.
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
            var active = data?.Obj("member_info")?.Bool("hasActiveSubscription");
            if (active != null)
            {
                var status = active.Value ? "Active" : "Inactive";
                if (active.Value)
                {
                    if (sub?.Obj("trial_info")?.Bool("is_trialing") == true) status += " (trial)";
                    else if (sub?.Bool("is_gift") == true) status += " (gift)";
                }
                rows.Add(new InfoRow("Plan status", status, active.Value ? "good" : "warn"));
            }
            // Subscription and activity benefit expiry are separate server fields;
            // neither describes billing or an automatic renewal.
            void Until(string label, object value)
            {
                var at = Positive(value) ? Time.Parse(value) : null;
                if (at != null)
                    rows.Add(new InfoRow(label, at.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture)));
            }
            Until("Plan until", sub?["end_time"]);
            Until("Bonus until", data?.Obj("campaign_benefit_info")?["benefit_end_time"]);
            var windows = new List<QuotaWindow>();
            var section = data?.Obj("window_limit_section");
            if (section != null)
            {
                var exhaustedAll = section["usage_exhausted"] is bool eb && eb;
                var order = 10;
                var groups = section.Arr("window_limit_groups") ?? new List<object>();
                var shownGroups = new List<Tuple<string, List<QuotaWindow>>>();
                for (var index = 0; index < groups.Count; index++)
                {
                    if (!(groups[index] is JObj group)) continue;
                    var gname = group.Str("feature_group") ?? "";
                    var groupName = Regex.Replace(group.Str("feature_group_name") ?? "", @"\s+", " ").Trim();
                    if (groupName.Length == 0) groupName = gname == "general" ? "General" : "Group " + (index + 1);
                    var groupWindows = new List<QuotaWindow>();
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
                        var window = new QuotaWindow($"doubao-{gname}-{wtype}-{order}",
                            WindowLabel(wtype), pct,
                            started ? Time.Parse(wl["end_time"]) : (DateTimeOffset?)null,
                            detail, order, exhaustedAll && pct >= 100);
                        windows.Add(window);
                        groupWindows.Add(window);
                        order++;
                    }
                    if (groupWindows.Count > 0) shownGroups.Add(Tuple.Create(groupName, groupWindows));
                }
                if (shownGroups.Count > 0)
                {
                    var nameCounts = shownGroups.GroupBy(g => g.Item1).ToDictionary(g => g.Key, g => g.Count());
                    var groupNames = new List<string>();
                    for (var index = 0; index < shownGroups.Count; index++)
                    {
                        var group = shownGroups[index];
                        var name = group.Item1 + (nameCounts[group.Item1] > 1 ? " (" + (index + 1) + ")" : "");
                        groupNames.Add(name);
                        if (shownGroups.Count > 1)
                            foreach (var window in group.Item2) window.Label = name + " \u00b7 " + window.Label;
                    }
                    rows.Add(new InfoRow(shownGroups.Count == 1 ? "Quota group" : "Quota groups", string.Join(", ", groupNames)));
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
                    // Preserve the host's available companion cookies. The live
                    // overview also succeeds with a valid sessionid alone.
                    var header = string.Join("; ", got.Item1.Where(kv => !string.IsNullOrEmpty(kv.Value))
                        .Select(kv => kv.Key + "=" + kv.Value));
                    return Tuple.Create(header, notes);
                }
            }
            return Tuple.Create((string)null, notes);
        }

        // Keep diagnostics limited to status/code and fixed classifications.
        // Response bodies and exception text may contain echoed credentials.
        private static JObj RequestPayload(HttpRequest req, string endpoint, ProviderResult result,
            out string failure)
        {
            failure = null;
            var name = "Doubao " + endpoint;
            HttpReply resp;
            try { resp = Http.Send(req); }
            catch (Exception)
            {
                result.Attempts.Add(new SourceAttempt(name, false, "request failed (network error)"));
                failure = "could not reach " + name;
                return null;
            }
            var payload = Json.ParseObject(resp.Text);
            var code = payload?.Num("code");
            var detail = "HTTP " + resp.Status;
            if (code != null && !double.IsNaN(code.Value) && !double.IsInfinity(code.Value))
                detail += ", code " + code.Value.ToString(CultureInfo.InvariantCulture);
            if (resp.Status == 401 || resp.Status == 403 || LoginInvalid(payload))
            {
                result.Attempts.Add(new SourceAttempt(name, false, detail + ": login rejected (expired or incomplete cookie)"));
                failure = "Doubao login expired; re-sign in, or paste a fresh session_id";
                return null;
            }
            if (resp.Status >= 300)
            {
                result.Attempts.Add(new SourceAttempt(name, false, detail));
                failure = name + " HTTP " + resp.Status;
                return null;
            }
            if (payload == null)
            {
                result.Attempts.Add(new SourceAttempt(name, false, detail + ": response was not JSON"));
                failure = name + " returned non-JSON";
                return null;
            }
            if (payload.Has("code") && code != 0)
            {
                result.Attempts.Add(new SourceAttempt(name, false, detail + ": API error"));
                failure = name + (code != null && !double.IsNaN(code.Value) && !double.IsInfinity(code.Value)
                    ? " API code " + code.Value.ToString(CultureInfo.InvariantCulture) : " returned an invalid API code");
                return null;
            }
            return payload;
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
            // The captured web client uses POST for profile. Its additional web
            // context is not reproduced here, so this remains optional metadata.
            var req = new HttpRequest { Method = "POST", Url = Http.Query(ProfileUrl, OverviewParams),
                Body = "{\"avatar_format\":\"png\"}", FollowRedirects = false, UseCookies = false };
            req.Headers = new Dictionary<string, string>
            {
                { "Cookie", session.Item1 },
                { "Accept", "application/json" },
                { "Content-Type", "application/json" },
                { "agw-js-conv", "str" },
                { "Referer", "https://www.doubao.com/chat/" },
                { "User-Agent", BrowserUa },
            };
            var payload = RequestPayload(req, "profile", result, out _);
            var parsed = payload != null ? ParseProfile(payload)
                : Tuple.Create((string)null, (string)null, new List<InfoRow>());
            if (payload != null)
                result.Attempts.Add(new SourceAttempt("Doubao profile", parsed.Item1 != null || parsed.Item2 != null,
                    parsed.Item1 != null || parsed.Item2 != null ? "account available" : "no account in response"));
            // Profile can fail while overview succeeds with the same cookie.
            // It is optional metadata, never a gate for the quota request.
            var overview = Overview(session.Item1, result, out var overviewFailure);
            var plan = overview.Item1 ?? parsed.Item2;
            if (parsed.Item1 == null && plan == null && overview.Item2.Count == 0 && overview.Item3.Count == 0)
            {
                result.Status = overviewFailure ?? "Doubao returned no account, plan or usage windows";
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
            result.Source = overview.Item1 != null || overview.Item2.Count > 0 || overview.Item3.Count > 0 ? "www.doubao.com/alice/commerce/sale/subscription/overview"
                : "www.doubao.com/alice/profile/self";
            result.Status = overview.Item2.Count > 0 ? "connected"
                : overviewFailure != null ? "signed in; usage unavailable: " + overviewFailure
                : "connected (Doubao returned no usage windows)";
            result.DataTime = Time.Now;
        }

        /// <summary>Plan, window-limit usage and plan-validity rows, best effort. Never throws.</summary>
        private Tuple<string, List<QuotaWindow>, List<InfoRow>> Overview(string cookieHeader, ProviderResult result,
            out string failure)
        {
            var none = Tuple.Create((string)null, new List<QuotaWindow>(), new List<InfoRow>());
            var req = new HttpRequest { Method = "POST", Url = Http.Query(OverviewUrl, OverviewParams),
                Body = "{\"product_line\":\"membership\"}", TimeoutSeconds = 20, FollowRedirects = false, UseCookies = false };
            req.Headers["Cookie"] = cookieHeader;
            req.Headers["Accept"] = "application/json, text/plain, */*";
            req.Headers["Content-Type"] = "application/json";
            req.Headers["agw-js-conv"] = "str";
            req.Headers["Referer"] = "https://www.doubao.com/chat/";
            req.Headers["User-Agent"] = BrowserUa;
            var payload = RequestPayload(req, "overview", result, out failure);
            if (payload == null) return none;
            var parsed = ParseOverview(payload);
            result.Attempts.Add(new SourceAttempt("Doubao overview", true,
                parsed.Item2.Count > 0 ? $"{parsed.Item2.Count} window(s)" : "no usage windows returned"));
            return parsed;
        }
    }
}
