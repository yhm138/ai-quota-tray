using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using QuotaTray.Core;
using QuotaTray.Providers;
using QuotaTray.Update;
using QuotaTray.Win;

namespace QuotaTray.Tests
{
    /// <summary>Offline checks, mirroring python/tests/test_providers.py.</summary>
    internal static class Program
    {
        private static readonly List<string> Pass = new List<string>();
        private static readonly List<string> Fail = new List<string>();

        private static void Check(string name, bool cond, object extra = null)
        {
            if (cond) Pass.Add(name);
            else Fail.Add(name);
            Console.WriteLine($"  [{(cond ? "PASS" : "FAIL")}] {name}" + (!cond && extra != null ? $"  -> {extra}" : ""));
        }

        private static JObj J(string json) => Json.ParseObject(json) ?? throw new Exception("bad test JSON: " + json);

        private static Func<HttpRequest, HttpReply> Serve(Dictionary<string, Func<HttpRequest, HttpReply>> routes) => req =>
        {
            var url = req.Url.Split('?')[0];
            return routes.TryGetValue(url, out var f) ? f(req) : new HttpReply(404, "{}");
        };

        private static int Main()
        {
            Paths.Override = Path.Combine(Path.GetTempPath(), "quotatray-tests-" + Guid.NewGuid().ToString("N"));
            var now = new DateTimeOffset(2026, 9, 13, 12, 0, 0, TimeSpan.Zero);
            Time.Clock = () => now;
            var later = Time.Iso(now.AddDays(10));
            var soon = Time.Iso(now.AddDays(1));

            Console.WriteLine("--- JSON and time ---");
            var o = J("{\"b\":1,\"a\":[true,null,\"\\u00e9\\n\"],\"n\":-1.5e2}");
            Check("key order kept", string.Join(",", o.Keys) == "b,a,n");
            Check("round trip", Json.Write(o) == "{\"b\":1,\"a\":[true,null,\"\u00e9\\n\"],\"n\":-150}", Json.Write(o));
            Check("bad JSON rejected", Json.ParseObject("{\"a\":") == null);
            Check("ISO with Z", Time.Parse("2026-09-13T12:00:00Z") == now);
            Check("ISO with nanoseconds", Time.Parse("2026-09-13T12:00:00.123456789+00:00")?.Millisecond == 123);
            Check("epoch seconds", Time.Parse(1789300800.0) == DateTimeOffset.FromUnixTimeSeconds(1789300800));
            Check("epoch millis", Time.Parse(1789300800000.0) == DateTimeOffset.FromUnixTimeSeconds(1789300800));
            Check("tiny number is not a time", Time.Parse(42.0) == null);
            Check("countdown 3d 2h", Time.HumanizeDelta(now.AddDays(3).AddHours(2)) == "3d 2h");
            Check("countdown 4h 12m", Time.HumanizeDelta(now.AddHours(4).AddMinutes(12)) == "4h 12m");
            Check("countdown resetting", Time.HumanizeDelta(now.AddMinutes(-1)) == "resetting");
            Check("percent text", new QuotaWindow("k", "l", 7.25).PercentText == "7.3%" && new QuotaWindow("k", "l", 99.96).PercentText == "100%");

            Console.WriteLine("--- plans and money ---");
            Check("pretty plan max", Account.PrettyPlan("default_claude_max_20x") == "Max 20x");
            Check("pretty plan prolite", Account.PrettyPlan("prolite") == "Pro Lite");
            Check("money", Account.Money(1234.5) == "$1,234.50");
            Check("minor amount", Account.MinorAmount(J("{\"amount_minor\":1359,\"exponent\":2}")) == 13.59);

            Console.WriteLine("--- Claude ---");
            Check("code names", ClaudeProvider.IsCodename("tangelo") && ClaudeProvider.IsCodename("nimbus_quill")
                                && !ClaudeProvider.IsCodename("extra_usage") && !ClaudeProvider.IsCodename("seven_day_opus"));
            Check("unknown period keys read well", ClaudeProvider.LabelFor("seven_day_oauth_apps").Item1 == "7-day Oauth Apps");
            Check("WSL login label", ClaudeProvider.LoginLabel(@"\\wsl.localhost\Arch\home\me\.claude\.credentials.json") == "Claude Code - WSL Arch");

            var coded = J($@"{{
              ""five_hour"": {{""utilization"": 5.0, ""resets_at"": ""{later}""}},
              ""seven_day"": {{""utilization"": 45.0, ""resets_at"": ""{later}""}},
              ""seven_day_opus"": {{""utilization"": 3.0, ""resets_at"": ""{later}""}},
              ""seven_day_overage_included"": {{""utilization"": 12.0, ""resets_at"": ""{later}""}},
              ""iguana_necktie"": {{""utilization"": 7.5, ""resets_at"": ""{later}""}},
              ""nimbus_quill"": {{""utilization"": 0.0, ""resets_at"": null}},
              ""tangelo"": null,
              ""spend"": {{""enabled"": true, ""used"": {{""amount_minor"": 1359, ""exponent"": 2, ""currency"": ""USD""}},
                         ""limit"": {{""amount_minor"": 5000, ""exponent"": 2, ""currency"": ""USD""}}}},
              ""cedar_ember"": {{""eligible"": true, ""grants"": [
                 {{""resets_left"": 1, ""ends_at"": ""{later}"", ""clears"": [""five_hour"", ""seven_day"", ""seven_day_overage_included""], ""usable_now"": true}},
                 {{""resets_left"": 2, ""paused"": true}},
                 {{""resets_left"": 0}}]}}
            }}");
            var profile = J(@"{""account"": {""email_address"": ""me@example.com""},
                             ""organization"": {""rate_limit_tier"": ""default_claude_max_20x"", ""subscription_status"": ""active"",
                                                ""subscription_created_at"": ""2026-04-10T15:53:44.244879Z""}}");
            var seenAuth = new List<string>();
            Http.Send = Serve(new Dictionary<string, Func<HttpRequest, HttpReply>>
            {
                { ClaudeProvider.UsageUrl, r => { seenAuth.Add(r.Headers["Authorization"]); return new HttpReply(200, Json.Write(coded)); } },
                { ClaudeProvider.ProfileUrl, r => new HttpReply(200, Json.Write(profile)) },
            });
            ClaudeProvider.DiscoverLogins = s => Tuple.Create(new List<ClaudeLogin> { new ClaudeLogin("tok-coded", "/fake/.credentials.json", null) }, new List<string>());
            ClaudeProvider.DesktopTokens = () => Tuple.Create(new List<KeyValuePair<string, string>>(), new List<string> { "no desktop" });
            ClaudeProvider.ClearProfiles();
            var cp = new ClaudeProvider(new Config(J("{\"providers\":{\"claude\":{\"order\":[\"oauth\"]}}}"))) { DetectOverride = () => true };
            var r1 = cp.Fetch();
            var labels = r1.SortedWindows().Select(w => w.Label).ToList();
            Check("claude connects", r1.Ok && r1.Source == "Claude Code OAuth (.credentials.json)", r1.Status);
            Check("code names are not usage bars", labels.SequenceEqual(new[] { "5-hour window", "7-day window", "7-day Opus", "7-day overage allowance", "Cloud credit" }),
                string.Join(" | ", labels));
            var cloud = r1.Windows.FirstOrDefault(w => w.Label == "Cloud credit");
            Check("cloud credit bar", cloud != null && cloud.Percent == 7.5 && cloud.Credit && (cloud.Detail ?? "").StartsWith("expires"));
            Check("credit not in tray percentage", r1.WorstPercent == 45.0, r1.WorstPercent);
            Check("hidden experiments in diagnostics", r1.Attempts.Any(a => a.Name == "internal quotas (not shown)" && a.Detail.Contains("nimbus_quill 0%")));
            Check("plan and account from profile", r1.Plan == "Max 20x" && r1.Account == "me@example.com", $"{r1.Plan} {r1.Account}");
            var info = r1.Info.ToDictionary(i => i.Label, i => i.Value);
            Check("extra usage money", info.TryGetValue("Extra usage", out var eu) && eu == "$13.59 of $50.00 used \u00b7 $36.41 left", eu);
            Check("subscribed since", info.TryGetValue("Subscribed", out var ss) && ss.StartsWith("since Apr 10"), ss);
            Check("one usable reset", r1.ResetsAvailable == 1 && r1.Resets[0].Note.Contains("5-hour window + 7-day window + 7-day overage allowance"),
                string.Join(";", r1.Resets.Select(g => g.Note)));
            Check("CLI user agent and cedar_ember", seenAuth.FirstOrDefault() == "Bearer tok-coded");
            var rt = ProviderResult.FromCache(Json.ParseObject(Json.Write(r1.ToCache())));
            Check("details survive the cache", rt.ResetsAvailable == 1 && rt.Info.Count == r1.Info.Count && rt.Windows.Count == r1.Windows.Count);

            // Claude Code and Claude Desktop on different accounts -> two pages
            Http.Send = Serve(new Dictionary<string, Func<HttpRequest, HttpReply>>
            {
                { ClaudeProvider.UsageUrl, r => new HttpReply(200, r.Headers["Authorization"].EndsWith("a")
                    ? "{\"five_hour\":{\"utilization\":10},\"seven_day\":{\"utilization\":5}}"
                    : "{\"five_hour\":{\"utilization\":60},\"seven_day\":{\"utilization\":30}}") },
                { ClaudeProvider.ProfileUrl, r => new HttpReply(200, r.Headers["Authorization"].EndsWith("a")
                    ? "{\"account\":{\"email\":\"a@x.com\"}}" : "{\"account\":{\"email\":\"b@x.com\"}}") },
            });
            ClaudeProvider.ClearProfiles();
            ClaudeProvider.DiscoverLogins = s => Tuple.Create(new List<ClaudeLogin> { new ClaudeLogin("tok-a", "/x/.credentials.json", null) }, new List<string>());
            ClaudeProvider.DesktopTokens = () => Tuple.Create(new List<KeyValuePair<string, string>> { new KeyValuePair<string, string>("tok-b", "config.json") }, new List<string>());
            var r2 = new ClaudeProvider(new Config()) { DetectOverride = () => true }.Fetch();
            Check("two accounts, two pages", r2.Pages().Count == 2 && r2.Label == "Claude Code" && r2.Alternates[0].Label == "Claude Desktop"
                                              && r2.Alternates[0].Account == "b@x.com", string.Join(",", r2.Pages().Select(p => p.Label + ":" + p.Account)));

            // desktop token cache parsing
            var nowMs = now.ToUnixTimeMilliseconds();
            var cache = J($@"{{
              ""i:u:https://api.anthropic.com:user:inference user:profile"": {{""token"": ""live"", ""expiresAt"": {nowMs + 3600000}}},
              ""i:u:https://api.anthropic.com:user:inference"": {{""token"": ""partial"", ""expiresAt"": {nowMs + 7200000}}},
              ""i:u:https://api.anthropic.com:user:inference user:profile x"": {{""token"": ""dead"", ""expiresAt"": {nowMs - 1000}}}
            }}");
            var parsed = ClaudeProvider.ParseDesktopTokenCache(Json.Write(cache), nowMs).OrderByDescending(p => p.Key).Select(p => p.Value).ToList();
            Check("desktop cache: full-scope token first, expired dropped", parsed.SequenceEqual(new[] { "live", "partial" }), string.Join(",", parsed));

            Console.WriteLine("--- AES-GCM ---");
            try
            {
                AesGcmDecryptor.SelfTest();
                Check("known-answer vector", true);
            }
            catch (Exception e) { Check("known-answer vector", false, e.Message); }

            Console.WriteLine("--- Codex ---");
            var wham = J(@"{""rate_limit"": {""primary_window"": {""used_percent"": 10, ""limit_window_seconds"": 18000},
                             ""secondary_window"": {""used_percent"": 20, ""limit_window_seconds"": 604800}},
                            ""plan_type"": ""prolite"",
                            ""credits"": {""has_credits"": true, ""unlimited"": false, ""balance"": ""1026.11"", ""overage_limit_reached"": false},
                            ""spend_control"": {""reached"": false, ""individual_limit"": {""unit"": ""credit"", ""limit"": ""2500"", ""used"": ""501.77"", ""reset_at"": 1790812800}},
                            ""rate_limit_reset_credits"": {""available_count"": 2}}");
            var windows = CodexProvider.WindowsFromRateLimits(CodexProvider.FindRateLimits(wham), now);
            Check("codex labels", windows.Select(w => w.Label).SequenceEqual(new[] { "5-hour window", "7-day window" }), string.Join(",", windows.Select(w => w.Label)));
            var sub = J($@"{{""plan_type"": ""pro"", ""active_until"": ""{later}"", ""billing_period"": ""monthly"", ""will_renew"": true}}");
            var ai = CodexProvider.AccountInfo(wham, sub, new JObj());
            var vals = ai.Item2.ToDictionary(r => r.Label, r => r.Value);
            Check("codex plan and renewal", ai.Item1 == "Pro Lite" && vals["Subscription"].StartsWith("renews") && vals["Subscription"].Contains("(monthly)"));
            Check("codex credits in dollars", vals.TryGetValue("Credits", out var cr) && cr == "1,026 left (~$41.04)", cr);
            Check("codex spend limit", vals.TryGetValue("Spend limit", out var sl) && sl.StartsWith("502 of 2,500 credits"), sl);
            var official = J($@"{{""credits"": [
                {{""status"": ""available"", ""expires_at"": ""{later}"", ""title"": ""Full reset (Weekly + 5 hr)"", ""description"": ""Ready to redeem""}},
                {{""status"": ""available"", ""expires_at"": null}},
                {{""status"": ""redeemed"", ""expires_at"": ""{later}"", ""redeemed_at"": ""2026-09-01T00:00:00Z""}}],
                ""available_count"": 2}}");
            var og = CodexProvider.Resets(new JObj(), official);
            Check("codex: only available credits count", og.Sum(g => g.Count) == 2);
            Check("codex: reset title kept", og.Any(g => g.Note.Contains("Full reset (Weekly + 5 hr)")));
            Check("codex: count without the list", CodexProvider.Resets(wham, new JObj()).Sum(g => g.Count) == 2);
            Check("codex: string counts", CodexProvider.ResetCount(J("{\"rate_limit_reset_credits\":{\"available_count\":\"1\"}}"), new JObj()) == 1);
            var jwtBody = Convert.ToBase64String(Encoding.UTF8.GetBytes("{\"email\":\"c@x.com\"}")).TrimEnd('=').Replace('+', '-').Replace('/', '_');
            Check("jwt email", CodexProvider.JwtClaims("h." + jwtBody + ".s").Str("email") == "c@x.com");

            CodexProvider.ClearExtras();
            Http.Send = Serve(new Dictionary<string, Func<HttpRequest, HttpReply>>
            {
                { CodexProvider.WhamUrl, r => new HttpReply(200, Json.Write(wham)) },
                { CodexProvider.ResetCreditsUrl, r => new HttpReply(403, "{}") },
                { CodexProvider.SubscriptionsUrl, r => new HttpReply(200, Json.Write(sub)) },
            });
            var codex = new CodexProvider(new Config(J("{\"providers\":{\"codex\":{\"order\":[\"wham\"],\"scan_wsl\":false,\"codex_home\":\"/nonexistent-codex\"}}}")))
            {
                DetectOverride = () => true,
                AuthTokensOverride = () => Tuple.Create(J("{\"access_token\":\"t\",\"account_id\":\"acc\",\"id_token\":\"h." + jwtBody + ".s\"}"), "/fake/auth.json"),
            };
            var rc = codex.Fetch();
            Check("codex e2e connects", rc.Ok && rc.Account == "c@x.com" && rc.Windows.Count == 2, rc.Status);
            Check("codex e2e: count survives a failed list", rc.ResetsAvailable == 2);
            Check("codex e2e: failure in diagnostics", rc.Attempts.Any(a => a.Name == "Codex account details" && a.Detail.Contains("HTTP 403")));

            // jsonl fallback reads the newest record, relative to its timestamp
            var home = Path.Combine(Paths.AppDir, "codex-home");
            var day = Path.Combine(home, "sessions", "2026", "09", "13");
            Directory.CreateDirectory(day);
            var stamp = Time.Iso(now.AddHours(-3));
            File.WriteAllText(Path.Combine(day, "rollout-1.jsonl"), string.Join("\n",
                $"{{\"timestamp\":\"{stamp}\",\"type\":\"message\"}}",
                $"{{\"timestamp\":\"{stamp}\",\"payload\":{{\"rate_limits\":{{\"primary\":{{\"used_percent\":77.5,\"window_minutes\":299,\"resets_in_seconds\":1800}},\"secondary\":{{\"used_percent\":33,\"window_minutes\":10079}}}}}}}}",
                $"{{\"timestamp\":\"{stamp}\",\"type\":\"message\"}}"));
            var jl = new CodexProvider(new Config(J($"{{\"providers\":{{\"codex\":{{\"order\":[\"jsonl\"],\"scan_wsl\":false,\"codex_home\":{Json.Write(home)}}}}}}}")))
            {
                DetectOverride = () => true,
            }.Fetch();
            Check("jsonl fallback", jl.Ok && jl.Status.Contains("offline snapshot") && jl.SortedWindows()[0].Percent == 77.5, jl.Status);
            Check("jsonl reset is record-relative", jl.SortedWindows()[0].ResetsAt < now);

            var big = Path.Combine(Paths.AppDir, "big.jsonl");
            File.WriteAllText(big, "first\n" + new string('x', 600000) + "\nlast\n");
            var rev = CodexProvider.LinesReverse(big).ToList();
            Check("reverse reader skips oversized lines", rev.SequenceEqual(new[] { "last", "first" }), string.Join(",", rev.Select(l => l.Length)));

            Console.WriteLine("--- Antigravity ---");
            var ag = J(@"{""userStatus"": {""email"": ""g@x.com"",
                ""planStatus"": {""availablePromptCredits"": 250, ""planInfo"": {""planName"": ""Pro"", ""monthlyPromptCredits"": 1000}},
                ""cascadeModelConfigData"": {""clientModelConfigs"": [
                    {""label"": ""Gemini 3 Pro"", ""modelOrAlias"": {""model"": ""MODEL_A_HIGH""}, ""quotaInfo"": {""remainingFraction"": 0.25}},
                    {""label"": ""Gemini 3 Pro"", ""modelOrAlias"": {""model"": ""MODEL_A_LOW""}, ""quotaInfo"": {""remainingFraction"": 1}},
                    {""label"": ""No quota""}]}}}");
            var agp = AntigravityProvider.ParseUserStatus(ag, new JObj());
            Check("prompt credits", agp.Item1[0].Label == "Prompt credits" && agp.Item1[0].Percent == 75 && agp.Item1[0].Detail == "250 / 1,000 left");
            Check("duplicate model names told apart", agp.Item1.Select(w => w.Label).Contains("Gemini 3 Pro (Low)"), string.Join(",", agp.Item1.Select(w => w.Label)));
            Check("antigravity account", agp.Item2 == "g@x.com" && agp.Item3 == "Pro");
            Check("csrf from command line", AntigravityProvider.ExtractArg("ls.exe --csrf_token abc-123 --x", "--csrf_token") == "abc-123");
            var ports = AntigravityProvider.ParseNetstat("  TCP    127.0.0.1:50123   0.0.0.0:0   LISTENING   4242\n  TCP 127.0.0.1:1 1.2.3.4:5 ESTABLISHED 4242\n");
            Check("netstat ports", ports.ContainsKey(4242) && ports[4242].SequenceEqual(new[] { 50123 }));

            Console.WriteLine("--- accounts, reminders, updates ---");
            var a1 = new ProviderResult("x", "X") { Account = "A@x.com", Label = "one" };
            var a2 = new ProviderResult("x", "X") { Account = "a@x.com", Label = "two" };
            var grouped = Account.GroupPages(new List<ProviderResult> { a1, a2 });
            Check("same account shares a page", grouped.Count == 1 && grouped[0].Label == "one + two");
            var withResets = new ProviderResult("codex", "Codex") { Ok = true, Resets = og };
            var text = Reminders.UnusedResetsText(new List<ProviderResult> { r1, withResets });
            Check("reminder lists both", text != null && text.Contains("Claude: 1 unused reset") && text.Contains("Codex: 2 unused resets"), text);
            Check("reminder once a day", Reminders.Due(null, new DateTime(2026, 9, 24, 11, 0, 0), 10)
                                         && !Reminders.Due("2026-09-24", new DateTime(2026, 9, 24, 11, 0, 0), 10)
                                         && !Reminders.Due(null, new DateTime(2026, 9, 24, 9, 0, 0), 10));
            Check("version compare", Updater.IsNewer("v1.4.0", "1.3.6") && !Updater.IsNewer("v1.3.6", "1.3.6") && !Updater.IsNewer("junk", "1.0.0"));
            var sums = Updater.ParseSums("0123456789abcdef0123456789abcdef0123456789abcdef0123456789ABCDEF  QuotaTray-v1.4.0-csharp-windows-anycpu.exe\r\n");
            Check("checksum list", sums.TryGetValue("QuotaTray-v1.4.0-csharp-windows-anycpu.exe", out var h) && h.EndsWith("abcdef"));
            Check("asset name", Updater.AssetName("v1.4.0") == "QuotaTray-v1.4.0-csharp-windows-anycpu.exe");

            var folder = Path.Combine(Paths.AppDir, "install");
            Directory.CreateDirectory(Path.Combine(folder, Updater.TrashDir));
            File.WriteAllText(Path.Combine(folder, Updater.TrashDir, "QuotaTray.exe.123"), "old");
            File.WriteAllText(Path.Combine(folder, "QuotaTray.exe.old"), "old");
            File.WriteAllText(Path.Combine(folder, "QuotaTray.exe"), "new");
            Check("update leftovers removed", Updater.CleanupAfterUpdate(folder, 1, 0)
                                              && !Directory.Exists(Path.Combine(folder, Updater.TrashDir))
                                              && Directory.GetFiles(folder).Length == 1);

            Check("known-folder tray paths expand", TrayPromotion.SamePath(
                TrayPromotion.ExpandPath(@"{6D809377-6AF0-444B-8957-A3773F02200E}\QuotaTray\QuotaTray.exe", g => @"C:\Program Files"),
                @"C:\Program Files\QuotaTray\QuotaTray.exe"));

            var merged = Config.Merge(Config.Defaults(), J("{\"refresh_seconds\":10,\"providers\":{\"codex\":{\"enabled\":false}}}"));
            var cfg = new Config(merged);
            Check("config merge keeps defaults", cfg.RefreshSeconds == 60 && !Settings.Flag(cfg.Provider("codex"), "enabled", true)
                                                && Settings.List(cfg.Provider("claude"), "order").Count == 4);

            try { Directory.Delete(Paths.AppDir, true); } catch (Exception) { }
            Console.WriteLine($"\npassed {Pass.Count} / {Pass.Count + Fail.Count}");
            if (Fail.Count > 0) Console.WriteLine("failures: " + string.Join("; ", Fail));
            return Fail.Count > 0 ? 1 : 0;
        }
    }
}
