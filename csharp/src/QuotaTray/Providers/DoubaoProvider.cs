using System;
using System.Collections.Generic;
using System.Linq;
using QuotaTray.Core;
using QuotaTray.Win;

namespace QuotaTray.Providers
{
    /// <summary>
    /// Doubao (Douban's AI assistant) desktop client. It is an Electron app for
    /// www.doubao.com and keeps a sessionid cookie in its cookie store. With it,
    /// the profile endpoint reports the signed-in account (and membership, when
    /// the account exposes one). Doubao does not publish a "remaining quota" API,
    /// so no usage bar is shown.
    /// </summary>
    public sealed class DoubaoProvider : Provider
    {
        public override string Id => "doubao";
        public override string Name => "Doubao";

        public const string ProfileUrl = "https://www.doubao.com/alice/profile/self";
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

        public override bool Detect()
        {
            if (Core.Settings.Str(Settings, "session_id").Length > 0) return true;
            return AppFolders.Any(f => Proc.AppRoots(f).Count > 0);
        }

        private Tuple<string, List<string>> SessionKey()
        {
            if (SessionOverride != null) return SessionOverride();
            var manual = Core.Settings.Str(Settings, "session_id");
            if (manual.Length > 0) return Tuple.Create(manual, new List<string> { "using session_id from config.json" });
            if (!Proc.IsWindows) return Tuple.Create((string)null, new List<string> { "reading the Doubao cookie is Windows-only" });
            var notes = new List<string>();
            foreach (var folder in AppFolders)
            {
                var got = ChromiumCookies.GetCookies(folder, "doubao.com", "sessionid");
                notes.AddRange(got.Item2);
                if (got.Item1.TryGetValue("sessionid", out var key) && !string.IsNullOrEmpty(key))
                    return Tuple.Create(key, notes);
            }
            return Tuple.Create((string)null, notes);
        }

        public override void Collect(ProviderResult result)
        {
            var session = SessionKey();
            if (session.Item1 == null)
            {
                result.Attempts.Add(new SourceAttempt("Doubao login", false,
                    session.Item2.Count > 0 ? string.Join("; ", session.Item2.Skip(Math.Max(0, session.Item2.Count - 3))) : "no Doubao sessionid cookie found"));
                result.Status = !result.Installed ? "Doubao not detected" : "sign in to Doubao Desktop first";
                return;
            }
            var headers = new Dictionary<string, string>
            {
                { "Cookie", "sessionid=" + session.Item1 },
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
            if (resp.Status == 401 || resp.Status == 403)
            {
                result.Attempts.Add(new SourceAttempt("Doubao profile", false,
                    $"session cookie rejected (HTTP {resp.Status}); open Doubao Desktop to refresh it"));
                result.Status = "Doubao login expired (open the app)";
                return;
            }
            if (resp.Status >= 400)
            {
                result.Attempts.Add(new SourceAttempt("Doubao profile", false, $"HTTP {resp.Status}: {Short(resp.Text, 160)}"));
                result.Status = $"Doubao profile HTTP {resp.Status}";
                return;
            }
            var payload = Json.ParseObject(resp.Text);
            if (payload == null)
            {
                result.Attempts.Add(new SourceAttempt("Doubao profile", false, "response was not JSON (a login page?)"));
                result.Status = "Doubao returned no profile (open the app and sign in)";
                return;
            }
            var parsed = ParseProfile(payload);
            if (parsed.Item1 == null && parsed.Item2 == null)
            {
                result.Attempts.Add(new SourceAttempt("Doubao profile", false, "no account in response: " + Short(resp.Text, 160)));
                result.Status = "signed in, but Doubao returned no account";
                return;
            }
            result.Ok = true;
            result.Account = parsed.Item1;
            result.Plan = parsed.Item2;
            result.Info = parsed.Item3;
            result.Headline = parsed.Item2 ?? "signed in";
            result.Source = "www.doubao.com/alice/profile/self";
            result.Status = parsed.Item3.Count == 0 ? "connected (Doubao shows usage only in its own \u914d\u989d\u4e2d\u5fc3)" : "connected";
            result.DataTime = Time.Now;
            result.Attempts.Add(new SourceAttempt("Doubao profile", true, parsed.Item1 ?? parsed.Item2 ?? "signed in"));
        }
    }
}
