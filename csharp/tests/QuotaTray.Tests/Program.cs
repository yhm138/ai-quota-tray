using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Security.Cryptography;
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
            // Existing local Codex homes add an account label to attempt names.
            Check("codex e2e: failure in diagnostics", rc.Attempts.Any(a => a.Name.EndsWith("Codex account details", StringComparison.Ordinal) && a.Detail.Contains("HTTP 403")));

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

            Console.WriteLine("--- Gemini CLI ---");
            var freshTok = GeminiProvider.AccessToken(J("{\"access_token\":\"live\",\"expiry_date\":" + ((now.ToUnixTimeSeconds() + 3600) * 1000) + "}"));
            Check("gemini: fresh token used as-is", freshTok.Item1 == "live" && freshTok.Item2 == null);
            var expTok = GeminiProvider.AccessToken(J("{\"access_token\":\"old\",\"expiry_date\":1}"));
            Check("gemini: expired token asks to run gemini", expTok.Item1 == null && expTok.Item2.Contains("run `gemini`"), expTok.Item2);
            Check("gemini: no token explained", GeminiProvider.AccessToken(new JObj()).Item2.Contains("no access token"));
            Http.Send = req =>
            {
                if (req.Url.EndsWith(":loadCodeAssist"))
                    return new HttpReply(200, "{\"currentTier\":{\"id\":\"standard-tier\",\"name\":\"Standard\"},\"cloudaicompanionProject\":\"proj-123\"}");
                if (req.Url.EndsWith(":retrieveUserQuota"))
                {
                    Check("gemini: quota query used the project id", req.Body == "{\"project\":\"proj-123\"}", req.Body);
                    return new HttpReply(200, "{\"buckets\":[{\"modelId\":\"gemini-2.5-pro\",\"remainingFraction\":0.4,\"remainingAmount\":\"600\",\"tokenType\":\"REQUESTS\",\"resetTime\":\"" + later + "\"},{\"modelId\":\"gemini-2.5-flash\",\"remainingFraction\":1.0}]}");
                }
                return new HttpReply(404, "{}");
            };
            var gid = Convert.ToBase64String(Encoding.UTF8.GetBytes("{\"email\":\"me@gmail.com\"}")).TrimEnd('=').Replace('+', '-').Replace('/', '_');
            var gp = new GeminiProvider(new Config())
            {
                DiscoverOverride = () => Tuple.Create(new List<KeyValuePair<JObj, string>>
                {
                    new KeyValuePair<JObj, string>(J("{\"access_token\":\"t\",\"expiry_date\":" + ((now.ToUnixTimeSeconds() + 3600) * 1000) + ",\"id_token\":\"h." + gid + ".s\"}"), "Gemini CLI"),
                }, new List<string>()),
            };
            var gr = gp.Fetch();
            Check("gemini connects", gr.Ok && gr.Source == "cloudcode-pa.googleapis.com", gr.Status);
            Check("gemini: two model windows", gr.SortedWindows().Select(w => w.Label).SequenceEqual(new[] { "Gemini 2.5 Pro", "Gemini 2.5 Flash" }),
                string.Join(",", gr.SortedWindows().Select(w => w.Label)));
            Check("gemini: 2.5 pro is 60% used", gr.SortedWindows()[0].PercentText == "60%" && gr.SortedWindows()[0].Detail == "600 left (requests)",
                gr.SortedWindows()[0].PercentText + " / " + gr.SortedWindows()[0].Detail);
            Check("gemini: flash unused", gr.SortedWindows()[1].Percent == 0.0);
            Check("gemini: plan and account", gr.Plan == "Standard" && gr.Account == "me@gmail.com", gr.Plan + " / " + gr.Account);
            Check("gemini: subscription tab", gr.Billing == "subscription");
            Check("gemini: survives the cache", ProviderResult.FromCache(Json.ParseObject(Json.Write(gr.ToCache()))).Plan == "Standard");
            Http.Send = req => new HttpReply(401, "{}");
            gp.DiscoverOverride = () => Tuple.Create(new List<KeyValuePair<JObj, string>>
            {
                new KeyValuePair<JObj, string>(J("{\"access_token\":\"t\",\"expiry_date\":" + ((now.ToUnixTimeSeconds() + 3600) * 1000) + "}"), "Gemini CLI"),
            }, new List<string>());
            var g401 = gp.Fetch();
            Check("gemini: 401 is explained", !g401.Ok && g401.Status.Contains("sign in to Gemini again"), g401.Status);

            Console.WriteLine("--- TRAE ---");
            Func<byte[], bool, byte[]> traeEncrypt = (plaintext, priv) =>
            {
                var keyMaterial = new byte[32];
                using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(keyMaterial);
                byte[] merged;
                using (var sha = SHA512.Create())
                {
                    var kh = sha.ComputeHash(keyMaterial);
                    var input = new byte[kh.Length + 64];
                    Buffer.BlockCopy(kh, 0, input, 0, kh.Length);
                    Buffer.BlockCopy(TraeProvider.Salt(priv), 0, input, kh.Length, 64);
                    merged = sha.ComputeHash(input);
                }
                byte[] payload;
                using (var sha = SHA512.Create())
                {
                    var digest = sha.ComputeHash(plaintext);
                    payload = digest.Concat(plaintext).ToArray();
                }
                var pad = 16 - (payload.Length % 16);
                payload = payload.Concat(Enumerable.Repeat((byte)pad, pad)).ToArray();
                byte[] ct;
                using (var aes = Aes.Create())
                {
                    aes.KeySize = 128; aes.Mode = CipherMode.CBC; aes.Padding = PaddingMode.None;
                    aes.Key = merged.Take(16).ToArray(); aes.IV = merged.Skip(16).Take(16).ToArray();
                    using (var enc = aes.CreateEncryptor()) ct = enc.TransformFinalBlock(payload, 0, payload.Length);
                }
                var header = priv ? new byte[] { 18, 57, 32, 32, 2, 3 } : new byte[] { 116, 99, 5, 16, 0, 0 };
                return header.Concat(keyMaterial).Concat(ct).ToArray();
            };
            var authDoc = "{\"accessToken\":\"trae-jwt-123\",\"email\":\"me@trae.cn\"}";
            var blob = traeEncrypt(Encoding.UTF8.GetBytes(authDoc), false);
            var b64 = Convert.ToBase64String(blob);
            Check("trae: byte-crypto round trip", TraeProvider.ByteCryptoDecrypt(blob) != null
                && Encoding.UTF8.GetString(TraeProvider.ByteCryptoDecrypt(blob)) == authDoc);
            var tampered = (byte[])blob.Clone(); tampered[tampered.Length - 1] ^= 1;
            Check("trae: tampered blob rejected", TraeProvider.ByteCryptoDecrypt(tampered) == null);
            Check("trae: private-variant round trip", Encoding.UTF8.GetString(TraeProvider.ByteCryptoDecrypt(traeEncrypt(Encoding.UTF8.GetBytes("hello"), true))) == "hello");
            var storePath = Path.Combine(Paths.AppDir, "storage.json");
            File.WriteAllText(storePath, "{\"iCubeAuthInfo://icube.cloudide\":" + Json.Write(b64) + ",\"iCubeAuthInfo://usertag\":\"x\"}");
            var sauth = TraeProvider.ReadStorageAuth(storePath);
            Check("trae: token and email from storage.json", sauth.Item1 == "trae-jwt-123" && sauth.Item2 == "me@trae.cn", sauth.Item1 + "/" + sauth.Item2);
            var plainStore = Path.Combine(Paths.AppDir, "storage-plain.json");
            File.WriteAllText(plainStore, "{\"iCubeAuthInfo://icube.cloudide\":\"{\\\"token\\\":\\\"plain-jwt\\\"}\"}");
            Check("trae: plain-JSON storage still works", TraeProvider.ReadStorageAuth(plainStore).Item1 == "plain-jwt");

            var usagePayload = J("{\"code\":0,\"user_entitlement_pack_list\":[{\"entitlement_base_info\":{\"product_type\":1,\"end_time\":" + (now.ToUnixTimeSeconds() + 15 * 86400) + ",\"quota\":{\"premium_model_fast_request_limit\":600}},\"usage\":{\"premium_model_fast_amount\":150},\"status\":1}]}");
            var pu = TraeProvider.ParseUsage(usagePayload);
            Check("trae: fast request window", pu.Item1.Count == 1 && pu.Item1[0].Label == "Fast requests"
                && pu.Item1[0].PercentText == "25%" && pu.Item1[0].Detail == "150 / 600", pu.Item1.Count > 0 ? pu.Item1[0].Detail : "none");
            Check("trae: plan from product_type", pu.Item3 == "Pro");
            Check("trae: renews row", pu.Item2.Any(r => r.Label == "Renews"));
            var unlimited = J("{\"user_entitlement_pack_list\":[{\"entitlement_base_info\":{\"product_type\":6,\"quota\":{\"premium_model_fast_request_limit\":-1}},\"usage\":{\"premium_model_fast_amount\":5}}]}");
            var uu = TraeProvider.ParseUsage(unlimited);
            Check("trae: unlimited fast requests", uu.Item1[0].Detail == "unlimited" && uu.Item1[0].Percent == 0.0 && uu.Item3 == "Ultra");
            var ps = TraeProvider.ParsePayStatus(J("{\"code\":0,\"user_pay_identity_str\":\"Pro+\",\"detail\":{\"subscription_renew_time\":" + (now.ToUnixTimeSeconds() + 30 * 86400) + "}}"));
            Check("trae: pay status plan and renew", ps.Item1 == "Pro+" && ps.Item2 != null);

            var seenTrae = new List<string>();
            Http.Send = req =>
            {
                seenTrae.Add(req.Url + "|" + req.Headers["Authorization"]);
                if (req.Url.EndsWith(TraeProvider.EntUsagePath)) return new HttpReply(200, Json.Write(usagePayload));
                if (req.Url.EndsWith(TraeProvider.PayStatusPath)) return new HttpReply(200, "{\"code\":0,\"user_pay_identity_str\":\"Pro\"}");
                return new HttpReply(404, "{}");
            };
            var tp = new TraeProvider(new Config())
            {
                StoragePathsOverride = () => new List<Tuple<string, bool, string>> { Tuple.Create(storePath, true, "TRAE CN") },
            };
            var tr = tp.Fetch();
            Check("trae connects (CN host, Cloud-IDE-JWT)", tr.Ok && tr.Source == "api.trae.cn"
                && seenTrae[0].Contains("Cloud-IDE-JWT trae-jwt-123"), tr.Status + " | " + (seenTrae.Count > 0 ? seenTrae[0] : ""));
            Check("trae: fast window in panel", tr.SortedWindows()[0].Label == "Fast requests" && tr.Plan == "Pro");
            Check("trae: account and subscription tab", tr.Account == "me@trae.cn" && tr.Billing == "subscription");
            Check("trae: survives the cache", ProviderResult.FromCache(Json.ParseObject(Json.Write(tr.ToCache()))).Plan == "Pro");

            Console.WriteLine("--- Doubao ---");
            // A real Chromium-format cookie DB (page_size 512, 62 rows: a v10
            // sessionid, a v24 host-hash-prefixed cookie, and enough rows to
            // force an interior b-tree page), key = bytes 0..31.
            const string cookieDbB64 = "U1FMaXRlIGZvcm1hdCAzAAIAAQEAQCAgAAAAAwAAAAgAAAAAAAAAAAAAAAIAAAAEAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAC52iQ0AAAABATgAATgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgUUBBxcbGwGCYXRhYmxlY29va2llc2Nvb2tpZXMCQ1JFQVRFIFRBQkxFIGNvb2tpZXMoY3JlYXRpb25fdXRjIElOVEVHRVIgTk9UIE5VTEwsaG9zdF9rZXkgVEVYVCBOT1QgTlVMTCwKICBuYW1lIFRFWFQgTk9UIE5VTEwsIHZhbHVlIFRFWFQgTk9UIE5VTEwsIGVuY3J5cHRlZF92YWx1ZSBCTE9CIERFRkFVTFQgJycsIHBhdGggVEVYVCBOT1QgTlVMTCkFAAAABQHnAAAAAAgB+wH2AfEB7AHnAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABzUAAAAGKgAAAAUfAAAABBQAAAADCQ0AAAAJADsAAbQBUwErAQMA2wCzAIsAYwA7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJgkHATETHQwPai5zaXRlNi5leGFtcGxlLmNvbWMwNnBsYWludmFsLyYIBwExEx0MD2kuc2l0ZTUuZXhhbXBsZS5jb21jMDVwbGFpbnZhbC8mBwcBMRMdDA9oLnNpdGU0LmV4YW1wbGUuY29tYzA0cGxhaW52YWwvJgYHATETHQwPZy5zaXRlMy5leGFtcGxlLmNvbWMwM3BsYWludmFsLyYFBwExEx0MD2Yuc2l0ZTIuZXhhbXBsZS5jb21jMDJwbGFpbnZhbC8mBAcBMRMdDA9lLnNpdGUxLmV4YW1wbGUuY29tYzAxcGxhaW52YWwvJgMHATETHQwPZC5zaXRlMC5leGFtcGxlLmNvbWMwMHBsYWludmFsL18CCAEhFQ2BGg8CLm90aGVyLmNvbWNzcmZ2MTBqYEQLQ0kE5B4bbzstUzBmsYAiAT+D6Cjlv7Xf/9D5nBVDLIWinTq9jeskm00RFyyS96R64KbCWO/KPNEhf7WeMhC3Ly9KAQcJIx8NaA8uZG91YmFvLmNvbXNlc3Npb25pZHYxMDbnHUNIcMH25wBAQySQS8nUdau+aoIk9rh6MAD5NnZwQYlHMxD1xcw3d8EvDQAAAAsAQAAB2AGwAYgBXwE2AQ0A5AC7AJIAaQBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACcUBwEzEx0MD3Uuc2l0ZTE3LmV4YW1wbGUuY29tYzE3cGxhaW52YWwvJxMHATMTHQwPdC5zaXRlMTYuZXhhbXBsZS5jb21jMTZwbGFpbnZhbC8nEgcBMxMdDA9zLnNpdGUxNS5leGFtcGxlLmNvbWMxNXBsYWludmFsLycRBwEzEx0MD3Iuc2l0ZTE0LmV4YW1wbGUuY29tYzE0cGxhaW52YWwvJxAHATMTHQwPcS5zaXRlMTMuZXhhbXBsZS5jb21jMTNwbGFpbnZhbC8nDwcBMxMdDA9wLnNpdGUxMi5leGFtcGxlLmNvbWMxMnBsYWludmFsLycOBwEzEx0MD28uc2l0ZTExLmV4YW1wbGUuY29tYzExcGxhaW52YWwvJw0HATMTHQwPbi5zaXRlMTAuZXhhbXBsZS5jb21jMTBwbGFpbnZhbC8mDAcBMRMdDA9tLnNpdGU5LmV4YW1wbGUuY29tYzA5cGxhaW52YWwvJgsHATETHQwPbC5zaXRlOC5leGFtcGxlLmNvbWMwOHBsYWludmFsLyYKBwExEx0MD2suc2l0ZTcuZXhhbXBsZS5jb21jMDdwbGFpbnZhbC8NAAAACwA8AAHXAa4BhQFcATMBCgDhALgAjwBmADwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoHwcCMxMdDA8AgC5zaXRlMjguZXhhbXBsZS5jb21jMjhwbGFpbnZhbC8nHgcBMxMdDA9/LnNpdGUyNy5leGFtcGxlLmNvbWMyN3BsYWludmFsLycdBwEzEx0MD34uc2l0ZTI2LmV4YW1wbGUuY29tYzI2cGxhaW52YWwvJxwHATMTHQwPfS5zaXRlMjUuZXhhbXBsZS5jb21jMjVwbGFpbnZhbC8nGwcBMxMdDA98LnNpdGUyNC5leGFtcGxlLmNvbWMyNHBsYWludmFsLycaBwEzEx0MD3suc2l0ZTIzLmV4YW1wbGUuY29tYzIzcGxhaW52YWwvJxkHATMTHQwPei5zaXRlMjIuZXhhbXBsZS5jb21jMjJwbGFpbnZhbC8nGAcBMxMdDA95LnNpdGUyMS5leGFtcGxlLmNvbWMyMXBsYWludmFsLycXBwEzEx0MD3guc2l0ZTIwLmV4YW1wbGUuY29tYzIwcGxhaW52YWwvJxYHATMTHQwPdy5zaXRlMTkuZXhhbXBsZS5jb21jMTlwbGFpbnZhbC8nFQcBMxMdDA92LnNpdGUxOC5leGFtcGxlLmNvbWMxOHBsYWludmFsLw0AAAALADIAAdYBrAGCAVgBLgEEANoAsACGAFwAMgAAAAAAAAAAAAAAAAAAAAAAAAAAKCoHAjMTHQwPAIsuc2l0ZTM5LmV4YW1wbGUuY29tYzM5cGxhaW52YWwvKCkHAjMTHQwPAIouc2l0ZTM4LmV4YW1wbGUuY29tYzM4cGxhaW52YWwvKCgHAjMTHQwPAIkuc2l0ZTM3LmV4YW1wbGUuY29tYzM3cGxhaW52YWwvKCcHAjMTHQwPAIguc2l0ZTM2LmV4YW1wbGUuY29tYzM2cGxhaW52YWwvKCYHAjMTHQwPAIcuc2l0ZTM1LmV4YW1wbGUuY29tYzM1cGxhaW52YWwvKCUHAjMTHQwPAIYuc2l0ZTM0LmV4YW1wbGUuY29tYzM0cGxhaW52YWwvKCQHAjMTHQwPAIUuc2l0ZTMzLmV4YW1wbGUuY29tYzMzcGxhaW52YWwvKCMHAjMTHQwPAIQuc2l0ZTMyLmV4YW1wbGUuY29tYzMycGxhaW52YWwvKCIHAjMTHQwPAIMuc2l0ZTMxLmV4YW1wbGUuY29tYzMxcGxhaW52YWwvKCEHAjMTHQwPAIIuc2l0ZTMwLmV4YW1wbGUuY29tYzMwcGxhaW52YWwvKCAHAjMTHQwPAIEuc2l0ZTI5LmV4YW1wbGUuY29tYzI5cGxhaW52YWwvDQAAAAsAMgAB1gGsAYIBWAEuAQQA2gCwAIYAXAAyAAAAAAAAAAAAAAAAAAAAAAAAAAAoNQcCMxMdDA8Ali5zaXRlNTAuZXhhbXBsZS5jb21jNTBwbGFpbnZhbC8oNAcCMxMdDA8AlS5zaXRlNDkuZXhhbXBsZS5jb21jNDlwbGFpbnZhbC8oMwcCMxMdDA8AlC5zaXRlNDguZXhhbXBsZS5jb21jNDhwbGFpbnZhbC8oMgcCMxMdDA8Aky5zaXRlNDcuZXhhbXBsZS5jb21jNDdwbGFpbnZhbC8oMQcCMxMdDA8Aki5zaXRlNDYuZXhhbXBsZS5jb21jNDZwbGFpbnZhbC8oMAcCMxMdDA8AkS5zaXRlNDUuZXhhbXBsZS5jb21jNDVwbGFpbnZhbC8oLwcCMxMdDA8AkC5zaXRlNDQuZXhhbXBsZS5jb21jNDRwbGFpbnZhbC8oLgcCMxMdDA8Ajy5zaXRlNDMuZXhhbXBsZS5jb21jNDNwbGFpbnZhbC8oLQcCMxMdDA8Aji5zaXRlNDIuZXhhbXBsZS5jb21jNDJwbGFpbnZhbC8oLAcCMxMdDA8AjS5zaXRlNDEuZXhhbXBsZS5jb21jNDFwbGFpbnZhbC8oKwcCMxMdDA8AjC5zaXRlNDAuZXhhbXBsZS5jb21jNDBwbGFpbnZhbC8NAAAACQCGAAHWAawBggFYAS4BBADaALAAhgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACg+BwIzEx0MDwCfLnNpdGU1OS5leGFtcGxlLmNvbWM1OXBsYWludmFsLyg9BwIzEx0MDwCeLnNpdGU1OC5leGFtcGxlLmNvbWM1OHBsYWludmFsLyg8BwIzEx0MDwCdLnNpdGU1Ny5leGFtcGxlLmNvbWM1N3BsYWludmFsLyg7BwIzEx0MDwCcLnNpdGU1Ni5leGFtcGxlLmNvbWM1NnBsYWludmFsLyg6BwIzEx0MDwCbLnNpdGU1NS5leGFtcGxlLmNvbWM1NXBsYWludmFsLyg5BwIzEx0MDwCaLnNpdGU1NC5leGFtcGxlLmNvbWM1NHBsYWludmFsLyg4BwIzEx0MDwCZLnNpdGU1My5leGFtcGxlLmNvbWM1M3BsYWludmFsLyg3BwIzEx0MDwCYLnNpdGU1Mi5leGFtcGxlLmNvbWM1MnBsYWludmFsLyg2BwIzEx0MDwCXLnNpdGU1MS5leGFtcGxlLmNvbWM1MXBsYWludmFsLw==";
            {
                var dbPath = Path.Combine(Paths.AppDir, "cookies-test.db");
                File.WriteAllBytes(dbPath, Convert.FromBase64String(cookieDbB64));
                var cookieKey = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
                var jar = ChromiumCookies.ReadCookies(dbPath, "doubao.com", cookieKey);
                Check("sqlite: reads a v10 cookie from a real Chromium DB", jar.TryGetValue("sessionid", out var sid) && sid == "sk-cookie-token",
                    jar.ContainsKey("sessionid") ? jar["sessionid"] : "(" + jar.Count + " cookies)");
                var other = ChromiumCookies.ReadCookies(dbPath, "other.com", cookieKey);
                Check("sqlite: strips the v24 host-hash prefix", other.TryGetValue("csrf", out var cv) && cv == "csrf-val",
                    other.ContainsKey("csrf") ? other["csrf"] : "missing");
                using (var sq = Sqlite.Open(dbPath))
                {
                    var rows = sq.ReadTable(sq.Tables()["cookies"].RootPage).Count();
                    Check("sqlite: walks interior b-tree pages (62 rows)", rows == 62, rows.ToString());
                }
            }
            // Portable install: an explicit data_dir whose cookie store is laid
            // out the browser way (User Data/Default/Network/Cookies).
            {
                var portable = Path.Combine(Path.GetTempPath(), "qt-portable-" + Guid.NewGuid().ToString("N"));
                var pdir = Path.Combine(portable, "User Data", "Default", "Network");
                Directory.CreateDirectory(pdir);
                File.WriteAllText(Path.Combine(pdir, "Cookies"), "x");
                var proots = Proc.AppRoots("Doubao", new[] { portable });
                Check("portable folder discovered via extra", proots.Contains(Path.Combine(portable, "User Data")), string.Join("|", proots));
                var pdbs = ChromiumCookies.FindCookieDbs(Path.Combine(portable, "User Data"));
                Check("portable Default/ cookie DB found", pdbs.Any(p => p.EndsWith(Path.Combine("Default", "Network", "Cookies"))), string.Join("|", pdbs));
                var pcfg = new Config(J("{\"providers\":{\"doubao\":{\"data_dir\":\"" + portable.Replace("\\", "\\\\") + "\"}}}"));
                Check("doubao detects a portable data_dir", new DoubaoProvider(pcfg).Detect());
            }
            // Optional browser scan: a doubao.com cookie in an Edge "Profile 1".
            {
                var browserHome = Path.Combine(Path.GetTempPath(), "qt-browser-" + Guid.NewGuid().ToString("N"));
                var edge = Path.Combine(browserHome, "Microsoft", "Edge", "User Data");
                var eprof = Path.Combine(edge, "Profile 1", "Network");
                Directory.CreateDirectory(eprof);
                File.WriteAllText(Path.Combine(eprof, "Cookies"), "x");
                var oldLocal = Environment.GetEnvironmentVariable("LOCALAPPDATA");
                Environment.SetEnvironmentVariable("LOCALAPPDATA", browserHome);
                try
                {
                    Check("BrowserRoots finds Edge user-data", Proc.BrowserRoots().Contains(edge), string.Join("|", Proc.BrowserRoots()));
                    var edbs = ChromiumCookies.FindCookieDbs(edge);
                    Check("Profile */ cookie DB found", edbs.Any(p => p.EndsWith(Path.Combine("Profile 1", "Network", "Cookies"))), string.Join("|", edbs));
                    var onCfg = new Config(J("{\"providers\":{\"doubao\":{\"scan_browsers\":true}}}"));
                    Check("doubao scan_browsers adds Edge root", new DoubaoProvider(onCfg).Detect());
                    var offCfg = new Config(J("{\"providers\":{\"doubao\":{\"scan_browsers\":false}}}"));
                    Check("doubao without scan_browsers ignores browsers", !new DoubaoProvider(offCfg).Detect() || Proc.AppRoots("Doubao").Count > 0);
                }
                finally { Environment.SetEnvironmentVariable("LOCALAPPDATA", oldLocal); }
            }

            var dprof = DoubaoProvider.ParseProfile(J("{\"data\":{\"profile_brief\":{\"nickname\":\"\\u5c0f\\u8c46\",\"user_name\":\"doubao_user\",\"id\":42,\"vip_type\":2}}}"));
            Check("doubao: account and plan from profile", dprof.Item1 == "\u5c0f\u8c46" && dprof.Item2 == "Pro", dprof.Item1 + "/" + dprof.Item2);
            var dfree = DoubaoProvider.ParseProfile(J("{\"data\":{\"profile_brief\":{\"nickname\":\"Free User\",\"is_vip\":false}}}"));
            Check("doubao: free account has no plan", dfree.Item1 == "Free User" && dfree.Item2 == null, dfree.Item1 + "/" + (dfree.Item2 ?? "null"));
            var drem = DoubaoProvider.ParseProfile(J("{\"data\":{\"profile_brief\":{\"nickname\":\"x\"},\"benefit\":{\"remaining\":88}}}"));
            Check("doubao: a remaining count is shown if present", drem.Item3.Any(r => r.Label == "Remaining" && r.Value == "88"));
            // The subscription overview carries the plan and window-limit usage.
            // Mirrors the real capture: current period not started (0/0), last-7-days at <1%.
            const string ovJson = "{\"code\":0,\"data\":{\"current_subscription\":{\"display\":{\"product_name\":\"\\u4e2a\\u4eba\\u8ba2\\u9605\",\"short_name\":\"\\u6807\\u51c6\\u5957\\u9910\"},\"sku_key\":\"doubao_personal_std\",\"is_gift\":false,\"end_time\":1794303044488},\"campaign_benefit_info\":{\"benefit_end_time\":1792393764820,\"campaign_tag\":3},\"window_limit_section\":{\"usage_exhausted\":false,\"window_limit_groups\":[{\"feature_group\":\"general\",\"window_limits\":[{\"start_time\":0,\"end_time\":0,\"used_percent\":0,\"less_than_one_percent\":false,\"window_type\":1,\"item_type\":0},{\"start_time\":1790457137809,\"end_time\":1791061937809,\"used_percent\":0,\"less_than_one_percent\":true,\"window_type\":2,\"item_type\":0}]}]}}}";
            var dov = DoubaoProvider.ParseOverview(J(ovJson));
            Check("doubao: overview plan name", dov.Item1 == "\u6807\u51c6\u5957\u9910", dov.Item1);
            Check("doubao: windows labelled by type",
                string.Join(",", dov.Item2.Select(w => w.Label)) == "Current period,Last 7 days", string.Join(",", dov.Item2.Select(w => w.Label)));
            Check("doubao: not-started window has no 1970 reset",
                dov.Item2[0].ResetsAt == null && dov.Item2[0].Detail == "not started", (dov.Item2[0].ResetsAt?.ToString() ?? "null") + "/" + dov.Item2[0].Detail);
            Check("doubao: last-7-days <1% kept distinct from a true 0",
                dov.Item2[1].Percent == 0 && dov.Item2[1].Detail == "<1% used" && dov.Item2[1].ResetsAt != null, dov.Item2[1].Detail);
            string DoubaoLocalMinute(long epochMillis) => DateTimeOffset.FromUnixTimeMilliseconds(epochMillis).ToLocalTime()
                .ToString("yyyy-MM-dd HH:mm", System.Globalization.CultureInfo.InvariantCulture);
            Check("doubao: separate subscription and benefit expiry retain local minutes",
                dov.Item3.Any(r => r.Label == "Bonus until" && r.Value == DoubaoLocalMinute(1792393764820))
                && dov.Item3.Any(r => r.Label == "Plan until" && r.Value == DoubaoLocalMinute(1794303044488)),
                string.Join(",", dov.Item3.Select(r => r.Label + "=" + r.Value)));
            var dpu = DoubaoProvider.ParseOverview(J("{\"code\":0,\"data\":{\"current_subscription\":{\"end_time\":1794303044488}}}")).Item3;
            Check("doubao: plain subscription end becomes a Plan-until row",
                dpu.Any(r => r.Label == "Plan until" && r.Value == DoubaoLocalMinute(1794303044488)), string.Join(",", dpu.Select(r => r.Label + "=" + r.Value)));

            Tuple<string, List<QuotaWindow>, List<InfoRow>> DoubaoData(string dataJson) =>
                DoubaoProvider.ParseOverview(J("{\"code\":0,\"data\":" + dataJson + "}"));
            var activeOverview = J(ovJson);
            activeOverview.Obj("data")["member_info"] = J(@"{""hasActiveSubscription"":true}");
            var activeRows = DoubaoProvider.ParseOverview(activeOverview).Item3;
            Check("doubao: active membership is explicit and green", activeRows.Any(r => r.Label == "Plan status" && r.Value == "Active" && r.Tone == "good"));
            Check("doubao: detail rows have a stable order", string.Join(",", activeRows.Select(r => r.Label)) == "Plan status,Plan until,Bonus until,Quota group");
            var inactiveRows = DoubaoData(@"{""member_info"":{""hasActiveSubscription"":false},""current_subscription"":{""trial_info"":{""is_trialing"":true},""is_gift"":true}}").Item3;
            Check("doubao: inactive membership warns without trial or gift suffix", inactiveRows.Count == 1
                && inactiveRows[0].Label == "Plan status" && inactiveRows[0].Value == "Inactive" && inactiveRows[0].Tone == "warn");
            var trialRows = DoubaoData(@"{""member_info"":{""hasActiveSubscription"":true},""current_subscription"":{""trial_info"":{""is_trialing"":true},""is_gift"":true}}").Item3;
            Check("doubao: explicit active trial takes priority over gift", trialRows.Single().Value == "Active (trial)");
            var giftRows = DoubaoData(@"{""member_info"":{""hasActiveSubscription"":true},""current_subscription"":{""is_gift"":true}}").Item3;
            Check("doubao: explicit active gift is labelled", giftRows.Single().Value == "Active (gift)");
            var unknownFlags = new[]
            {
                @"{""member_info"":{""status"":1},""current_subscription"":{""status"":1}}",
                @"{""member_info"":{""hasActiveSubscription"":1}}",
                @"{""member_info"":{""hasActiveSubscription"":""true""}}",
                @"{""member_info"":{""hasActiveSubscription"":null}}",
            };
            Check("doubao: missing or nonboolean membership does not guess status", unknownFlags.All(x =>
                !DoubaoData(x).Item3.Any(r => r.Label == "Plan status")));
            var untypedFlags = DoubaoData(@"{""member_info"":{""hasActiveSubscription"":true},""current_subscription"":{""trial_info"":{""is_trialing"":1},""is_gift"":""true""}}").Item3;
            Check("doubao: trial and gift also require boolean flags", untypedFlags.Single().Value == "Active");
            var bonusOnlyRows = DoubaoData(@"{""campaign_benefit_info"":{""benefit_end_time"":1792393764820}}").Item3;
            Check("doubao: activity expiry survives missing subscription", bonusOnlyRows.Count == 1 && bonusOnlyRows[0].Label == "Bonus until"
                && bonusOnlyRows[0].Value == DoubaoLocalMinute(1792393764820));
            var zeroExpiryRows = DoubaoData(@"{""current_subscription"":{""end_time"":0,""is_gift"":true},""campaign_benefit_info"":{""benefit_end_time"":0}}").Item3;
            Check("doubao: zero expiry sentinels never show 1970", zeroExpiryRows.Count == 0);
            var invalidExpiryRows = DoubaoData(@"{""current_subscription"":{""end_time"":1},""campaign_benefit_info"":{""benefit_end_time"":-1}}").Item3;
            Check("doubao: invalid expiry values omitted", invalidExpiryRows.Count == 0);
            var giftExpiryRows = DoubaoData(@"{""current_subscription"":{""end_time"":1794303044488,""is_gift"":true}}").Item3;
            Check("doubao: gift flag never relabels subscription expiry as bonus", giftExpiryRows.Single().Label == "Plan until");

            Check("doubao: a single general group keeps short window labels", dov.Item3.Any(r => r.Label == "Quota group" && r.Value == "General")
                && dov.Item2.Select(w => w.Label).SequenceEqual(new[] { "Current period", "Last 7 days" }));
            var namedGroup = J(ovJson);
            namedGroup.Obj("data").Obj("window_limit_section").Arr("window_limit_groups").OfType<JObj>().Single()["feature_group_name"] = "  Shared\n   quota \t ";
            var namedQuota = DoubaoProvider.ParseOverview(namedGroup);
            Check("doubao: backend group name wins and whitespace collapses", namedQuota.Item3.Any(r => r.Label == "Quota group" && r.Value == "Shared quota")
                && namedQuota.Item2[0].Label == "Current period");
            namedGroup.Obj("data").Obj("window_limit_section").Arr("window_limit_groups").OfType<JObj>().Single()["feature_group_name"] = 123;
            Check("doubao: nonstring group names use the safe fallback", DoubaoProvider.ParseOverview(namedGroup).Item3.Any(r => r.Label == "Quota group" && r.Value == "General"));
            var doubaoGrouped = DoubaoData(@"{""window_limit_section"":{""usage_exhausted"":true,""window_limit_groups"":[
                {""feature_group"":""unused_internal"",""window_limits"":[]},
                {""feature_group"":""private_internal"",""feature_group_name"":""  "",""window_limits"":[{""used_percent"":100,""window_type"":1,""start_time"":0,""end_time"":0}]},
                {""feature_group"":""general"",""window_limits"":[{""used_percent"":0,""window_type"":2,""start_time"":1790457137809,""end_time"":1791061937809,""less_than_one_percent"":true}]}
            ]}}");
            Check("doubao: empty groups excluded and unknown names use original positions", doubaoGrouped.Item3.Single().Label == "Quota groups"
                && doubaoGrouped.Item3.Single().Value == "Group 2, General"
                && doubaoGrouped.Item2.Select(w => w.Label).SequenceEqual(new[] { "Group 2 \u00b7 Current period", "General \u00b7 Last 7 days" }));
            Check("doubao: group labels preserve keys, ordering, exhaustion and reset details", doubaoGrouped.Item2[0].Key == "doubao-private_internal-1-10"
                && doubaoGrouped.Item2[0].Order == 10 && doubaoGrouped.Item2[0].Exhausted && doubaoGrouped.Item2[0].Detail == "not started" && doubaoGrouped.Item2[0].ResetsAt == null
                && doubaoGrouped.Item2[1].Key == "doubao-general-2-11" && doubaoGrouped.Item2[1].Order == 11 && !doubaoGrouped.Item2[1].Exhausted
                && doubaoGrouped.Item2[1].Detail == "<1% used" && doubaoGrouped.Item2[1].ResetsAt != null);
            var duplicateGroups = DoubaoData(@"{""window_limit_section"":{""window_limit_groups"":[
                {""feature_group_name"":""Empty"",""window_limits"":[]},
                {""feature_group_name"":""Shared"",""window_limits"":[{""used_percent"":20,""window_type"":1}]},
                {""feature_group_name"":""Other"",""window_limits"":[{""used_percent"":30,""window_type"":1}]},
                {""feature_group_name"":""Shared"",""window_limits"":[{""used_percent"":40,""window_type"":2}]}
            ]}}");
            Check("doubao: duplicate names use the effective group position", duplicateGroups.Item3.Single().Value == "Shared (1), Other, Shared (3)"
                && duplicateGroups.Item2[0].Label == "Shared (1) \u00b7 Current period" && duplicateGroups.Item2[2].Label == "Shared (3) \u00b7 Last 7 days");
            var unusableGroups = DoubaoData(@"{""window_limit_section"":{""window_limit_groups"":[
                {""feature_group_name"":""Empty"",""window_limits"":[]},
                {""feature_group_name"":""Invalid"",""window_limits"":[{""used_percent"":""10""}]}
            ]}}");
            Check("doubao: groups without parseable windows are omitted", unusableGroups.Item2.Count == 0 && unusableGroups.Item3.Count == 0);
            var seenDoubao = new List<string>();
            var doubaoRequests = new List<HttpRequest>();
            Http.Send = req =>
            {
                doubaoRequests.Add(req);
                seenDoubao.Add(req.Headers.TryGetValue("Cookie", out var ck) ? ck : "");
                if (req.Url.StartsWith(DoubaoProvider.ProfileUrl))
                    return new HttpReply(200, "{\"data\":{\"profile_brief\":{\"nickname\":\"\\u5c0f\\u8c46\",\"vip_type\":2}}}");
                if (req.Url.StartsWith(DoubaoProvider.OverviewUrl)) return new HttpReply(200, ovJson);
                return new HttpReply(404, "{}");
            };
            var dbp = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("sk-cookie", new List<string>()) };
            var dbr = dbp.Fetch();
            Check("doubao connects", dbr.Ok && dbr.Source.Contains("overview"), dbr.Status);
            Check("doubao: sends the sessionid cookie", seenDoubao.Any(c => c.Contains("sessionid=sk-cookie")), string.Join("|", seenDoubao));
            Check("doubao: profile uses the captured POST shape", doubaoRequests.Any(r => r.Url.StartsWith(DoubaoProvider.ProfileUrl)
                && r.Method == "POST" && r.Body == "{\"avatar_format\":\"png\"}" && r.Headers["agw-js-conv"] == "str"));
            Check("doubao: overview request shape retained", doubaoRequests.Any(r => r.Url.StartsWith(DoubaoProvider.OverviewUrl)
                && r.Method == "POST" && r.Body == "{\"product_line\":\"membership\"}" && r.Headers["agw-js-conv"] == "str"));
            var queryNames = "aid,device_platform,language,real_aid,region,samantha_web,sys_region,use-olympus-account,version_code,web_platform";
            Check("doubao: stable query only, no browser signing credentials", doubaoRequests.Count == 2 && doubaoRequests.All(r =>
                string.Join(",", new Uri(r.Url).Query.TrimStart('?').Split('&').Select(p => p.Split('=')[0]).OrderBy(p => p, StringComparer.Ordinal)) == queryNames));
            Check("doubao: credentialed requests do not follow redirects", doubaoRequests.All(r => !r.FollowRedirects));
            Check("doubao: explicit cookies bypass the .NET CookieContainer", doubaoRequests.All(r => !r.UseCookies));
            Check("HTTP: other providers keep the default cookie policy", new HttpRequest().UseCookies);
            Check("doubao: account and overview plan win", dbr.Account == "\u5c0f\u8c46" && dbr.Plan == "\u6807\u51c6\u5957\u9910" && dbr.Billing == "subscription", dbr.Plan);
            Check("doubao: window bars shown",
                string.Join(",", dbr.SortedWindows().Select(w => w.Label)) == "Current period,Last 7 days", string.Join(",", dbr.Windows.Select(w => w.Label)));
            Check("doubao: survives the cache",
                string.Join(",", ProviderResult.FromCache(Json.ParseObject(Json.Write(dbr.ToCache()))).Windows.Select(w => w.Label)) == "Current period,Last 7 days");
            // An overview failure is visible even when optional account metadata succeeds.
            Http.Send = req => req.Url.StartsWith(DoubaoProvider.ProfileUrl)
                ? new HttpReply(200, "{\"data\":{\"profile_brief\":{\"nickname\":\"\\u5c0f\\u8c46\",\"vip_type\":2}}}")
                : new HttpReply(200, "{\"code\":1,\"msg\":\"verify\"}");
            var dbr2 = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("sk-cookie", new List<string>()) }.Fetch();
            Check("doubao: survives an overview rejection", dbr2.Ok && dbr2.Account == "\u5c0f\u8c46" && dbr2.Windows.Count == 0, dbr2.Status);
            Check("doubao: overview rejection is visible on a partial card", dbr2.Status == "signed in; usage unavailable: Doubao overview API code 1"
                && dbr2.Source.Contains("profile/self"), dbr2.Status);
            Http.Send = req => new HttpReply(401, "<html>login</html>");
            var d401 = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("expired", new List<string>()) }.Fetch();
            Check("doubao: expired login explained", !d401.Ok && d401.Status.ToLowerInvariant().Contains("expired"), d401.Status);
            // Doubao answers an invalid login with HTTP 200 and an error code, not a 401.
            Http.Send = req => new HttpReply(200, "{\"code\":710012001,\"msg\":\"\\u767b\\u5f55\\u5df2\\u8fc7\\u671f\",\"message\":\"login invalid\"}");
            var dInvalid = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("stale", new List<string>()) }.Fetch();
            Check("doubao: 200 login-invalid reported as expired, not 'no account'",
                !dInvalid.Ok && dInvalid.Status.ToLowerInvariant().Contains("expired") && !dInvalid.Status.ToLowerInvariant().Contains("no account"), dInvalid.Status);
            // session_id may be a whole "k=v; k=v" Cookie string, sent verbatim.
            string seenCookie = null;
            Http.Send = req => { if (req.Url.StartsWith(DoubaoProvider.ProfileUrl)) req.Headers.TryGetValue("Cookie", out seenCookie); return new HttpReply(200, req.Url.StartsWith(DoubaoProvider.ProfileUrl) ? "{\"data\":{\"profile_brief\":{\"nickname\":\"\\u5c0f\\u8c46\"}}}" : "{\"code\":1}"); };
            new DoubaoProvider(new Config(J("{\"providers\":{\"doubao\":{\"session_id\":\"sessionid=abc; sid_tt=xyz\"}}}"))).Fetch();
            Check("doubao: a full cookie string is sent verbatim", seenCookie == "sessionid=abc; sid_tt=xyz", seenCookie);

            // Synthetic markers prove that reflected response/exception values never
            // enter diagnostics. No captured credentials are used in these tests.
            const string reflected = "DO_NOT_LOG_REFLECTED_VALUE";
            var profileFailures = new Dictionary<string, Func<HttpReply>>
            {
                { "404", () => new HttpReply(404, reflected) },
                { "401", () => new HttpReply(401, reflected) },
                { "403", () => new HttpReply(403, reflected) },
                { "login code", () => new HttpReply(200, "{\"code\":710012001,\"msg\":\"" + reflected + "\"}") },
                { "system code", () => new HttpReply(200, "{\"code\":710010202,\"msg\":\"" + reflected + "\"}") },
                { "non-JSON", () => new HttpReply(200, reflected) },
                { "network error", () => throw new IOException(reflected) },
            };
            foreach (var scenario in profileFailures)
            {
                var overviewCalls = 0;
                Http.Send = req =>
                {
                    if (req.Url.StartsWith(DoubaoProvider.ProfileUrl)) return scenario.Value();
                    overviewCalls++;
                    return new HttpReply(200, ovJson);
                };
                var actual = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("dummy", new List<string>()) }.Fetch();
                Check("doubao: overview survives profile " + scenario.Key,
                    actual.Ok && actual.Windows.Count == 2 && actual.Plan == "\u6807\u51c6\u5957\u9910" && actual.Account == null
                    && actual.Status == "connected" && overviewCalls == 1 && actual.Attempts.Any(a => a.Name == "Doubao profile" && !a.Ok), actual.Status);
                Check("doubao: profile " + scenario.Key + " diagnostics omit reflected values",
                    !actual.Status.Contains(reflected) && actual.Attempts.All(a => !a.Detail.Contains(reflected)));
            }
            var overviewFailures = new[]
            {
                Tuple.Create("login code", new HttpReply(200, "{\"code\":710012001,\"msg\":\"" + reflected + "\"}"), "Doubao login expired; re-sign in, or paste a fresh session_id"),
                Tuple.Create("HTTP 401", new HttpReply(401, reflected), "Doubao login expired; re-sign in, or paste a fresh session_id"),
                Tuple.Create("HTTP 403", new HttpReply(403, reflected), "Doubao login expired; re-sign in, or paste a fresh session_id"),
                Tuple.Create("HTTP 503", new HttpReply(503, reflected), "Doubao overview HTTP 503"),
                Tuple.Create("HTTP 302", new HttpReply(302, reflected), "Doubao overview HTTP 302"),
                Tuple.Create("API code", new HttpReply(200, "{\"code\":710010202,\"msg\":\"" + reflected + "\"}"), "Doubao overview API code 710010202"),
                Tuple.Create("invalid code", new HttpReply(200, "{\"code\":\"" + reflected + "\"}"), "Doubao overview returned an invalid API code"),
                Tuple.Create("non-JSON", new HttpReply(200, reflected), "Doubao overview returned non-JSON"),
                Tuple.Create("network error", (HttpReply)null, "could not reach Doubao overview"),
            };
            foreach (var scenario in overviewFailures)
            {
                Http.Send = req => req.Url.StartsWith(DoubaoProvider.ProfileUrl)
                    ? new HttpReply(404, reflected) : scenario.Item2 ?? throw new IOException(reflected);
                var actual = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("dummy", new List<string>()) }.Fetch();
                Check("doubao: both fail, overview " + scenario.Item1 + " explained",
                    !actual.Ok && actual.Status == scenario.Item3 && actual.Attempts.Count == 2
                    && actual.Attempts.All(a => !a.Ok && !a.Detail.Contains(reflected)), actual.Status);
            }
            Http.Send = req => req.Url.StartsWith(DoubaoProvider.ProfileUrl) ? new HttpReply(404)
                : new HttpReply(200, "{\"code\":0,\"data\":{\"current_subscription\":{\"display\":{\"short_name\":\"Synthetic plan\"}}}}");
            var planOnly = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("dummy", new List<string>()) }.Fetch();
            Check("doubao: overview plan usable without profile or windows", planOnly.Ok && planOnly.Plan == "Synthetic plan"
                && planOnly.Windows.Count == 0 && planOnly.Status == "connected (Doubao returned no usage windows)", planOnly.Status);
            Http.Send = req => req.Url.StartsWith(DoubaoProvider.ProfileUrl) ? new HttpReply(404)
                : new HttpReply(200, "{\"code\":0,\"data\":{\"member_info\":{\"hasActiveSubscription\":false}}}");
            var statusOnly = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("dummy", new List<string>()) }.Fetch();
            Check("doubao: status-only overview survives missing account and quota", statusOnly.Ok && statusOnly.Account == null && statusOnly.Plan == null
                && statusOnly.Windows.Count == 0 && statusOnly.Status == "connected (Doubao returned no usage windows)"
                && statusOnly.Source.Contains("overview") && statusOnly.Info.Count == 1 && statusOnly.Info[0].Label == "Plan status"
                && statusOnly.Info[0].Value == "Inactive" && statusOnly.Info[0].Tone == "warn", statusOnly.Status);
            Http.Send = req => req.Url.StartsWith(DoubaoProvider.ProfileUrl) ? new HttpReply(404)
                : new HttpReply(200, "{\"code\":0,\"data\":{\"campaign_benefit_info\":{\"benefit_end_time\":1792393764820}}}");
            var bonusOnly = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("dummy", new List<string>()) }.Fetch();
            Check("doubao: bonus-only overview survives missing subscription and quota", bonusOnly.Ok && bonusOnly.Account == null && bonusOnly.Plan == null
                && bonusOnly.Windows.Count == 0 && bonusOnly.Status == "connected (Doubao returned no usage windows)"
                && bonusOnly.Source.Contains("overview") && bonusOnly.Info.Count == 1 && bonusOnly.Info[0].Label == "Bonus until"
                && bonusOnly.Info[0].Value == DoubaoLocalMinute(1792393764820), bonusOnly.Status);
            Http.Send = req => req.Url.StartsWith(DoubaoProvider.ProfileUrl) ? new HttpReply(404)
                : new HttpReply(200, "{\"code\":0,\"data\":{}}");
            var emptyOverview = new DoubaoProvider(new Config()) { SessionOverride = () => Tuple.Create("dummy", new List<string>()) }.Fetch();
            Check("doubao: empty overview is not false usage success", !emptyOverview.Ok
                && emptyOverview.Status == "Doubao returned no account, plan or usage windows", emptyOverview.Status);

            Console.WriteLine("--- DeepSeek ---");
            Check("jsonc comments and trailing commas", Json.Write(Json.ParseObject(DeepSeekProvider.StripJsonc(
                "{\n // note\n \"a\": \"http://x//y\", /* block */ \"b\": [1, 2,],\n}"))) == "{\"a\":\"http://x//y\",\"b\":[1,2]}");
            var v1 = "version: 1\n\nrefs:\n  # mine\n  DEEPSEEK_API_KEY: sk-v1key\n  OPENAI_API_KEY: sk-other\nrecords:\n  x/y:\n    kind: grant\n";
            Check("dsh credentials (version 1)", DeepSeekProvider.DshCredentialsKey(v1) == "sk-v1key");
            Check("dsh credentials (flat, quoted)", DeepSeekProvider.DshCredentialsKey("DEEPSEEK_API_KEY: 'sk-flat'\n") == "sk-flat");
            Check("dsh credentials without the key", DeepSeekProvider.DshCredentialsKey("version: 1\nrefs:\n  OPENAI_API_KEY: x\n") == null);
            Check("dotenv key", DeepSeekProvider.DotenvKey("export DEEPSEEK_API_KEY=\"sk-env\" # comment\n") == "sk-env");
            Check("opencode auth.json", DeepSeekProvider.OpencodeAuthKeys(J("{\"deepseek\":{\"type\":\"api\",\"key\":\"sk-oc\"},\"anthropic\":{\"type\":\"oauth\"}}"))
                .SequenceEqual(new[] { "sk-oc" }));
            var ocKeys = DeepSeekProvider.OpencodeConfigKeys(J(@"{""provider"": {
                ""deepseek"": {""options"": {""apiKey"": ""{env:MY_DS}""}},
                ""ds-relay"": {""options"": {""baseURL"": ""https://relay.example/v1"", ""apiKey"": ""sk-relay""}},
                ""mine"": {""options"": {""baseURL"": ""https://api.deepseek.com/v1"", ""apiKey"": ""sk-cfg""}}}}"), n => n == "MY_DS" ? "sk-fromenv" : null);
            Check("opencode.json key, env reference, relay skipped", ocKeys.SequenceEqual(new[] { "sk-fromenv", "sk-cfg" }), string.Join(",", ocKeys));

            Check("opencode labels", DeepSeekProvider.OpencodeLabel(true, true) == "OpenCode CLI + OpenCode Desktop"
                                     && DeepSeekProvider.OpencodeLabel(false, true) == "OpenCode Desktop"
                                     && DeepSeekProvider.OpencodeLabel(true, false) == "OpenCode CLI"
                                     && DeepSeekProvider.OpencodeLabel(false, false) == "OpenCode"
                                     && DeepSeekProvider.OpencodeLabel(true, true, "Ubuntu", new[] { "Ubuntu" }) == "OpenCode CLI + OpenCode Desktop - WSL Ubuntu"
                                     && DeepSeekProvider.OpencodeLabel(true, true, "Arch", new[] { "Ubuntu" }) == "OpenCode CLI - WSL Arch");
            var ocAppdata = Path.Combine(Paths.AppDir, "oc-appdata");
            Directory.CreateDirectory(Path.Combine(ocAppdata, "ai.opencode.desktop"));
            File.WriteAllText(Path.Combine(ocAppdata, "ai.opencode.desktop", "opencode.settings"),
                "{\"wslServers\":{\"servers\":[{\"id\":\"wsl:Ubuntu\",\"distro\":\"Ubuntu\"}]}}");
            var desk = DeepSeekProvider.OpencodeDesktop(n => n == "APPDATA" ? ocAppdata : null);
            Check("opencode desktop and its WSL servers", desk.Item1 != null && desk.Item2.SetEquals(new[] { "Ubuntu" }));
            var ocBin = Path.Combine(Paths.AppDir, "oc-bin");
            Directory.CreateDirectory(ocBin);
            File.WriteAllText(Path.Combine(ocBin, "opencode.exe"), "");
            Check("opencode cli on PATH", DeepSeekProvider.OpencodeCli(Path.Combine(Paths.AppDir, "nohome"), n => n == "PATH" ? ocBin : null, true)
                                          == Path.Combine(ocBin, "opencode.exe"));
            Check("no opencode cli", DeepSeekProvider.OpencodeCli(Path.Combine(Paths.AppDir, "nohome"), n => null, true) == null);
            var dsHome = Path.Combine(Paths.AppDir, "ds-home");
            Directory.CreateDirectory(Path.Combine(dsHome, ".local", "share", "opencode"));
            File.WriteAllText(Path.Combine(dsHome, ".local", "share", "opencode", "auth.json"), "{\"deepseek\":{\"type\":\"api\",\"key\":\"sk-shared1234\"}}");
            Directory.CreateDirectory(Path.Combine(dsHome, ".dsh"));
            File.WriteAllText(Path.Combine(dsHome, ".dsh", ".credentials.yaml"), "version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-shared1234\n");
            DeepSeekProvider.HomesOverride = st => new List<KeyValuePair<string, string>> { new KeyValuePair<string, string>(dsHome, "") };
            DeepSeekProvider.Env = n => n == "DEEPSEEK_API_KEY" ? "sk-envonly9999" : null;
            var dk = DeepSeekProvider.DiscoverKeys(new JObj());
            Check("keys from OpenCode, dsh and the environment", dk.Item1.Select(k => k.Value)
                .SequenceEqual(new[] { "OpenCode", "DeepSeek Harness", "env DEEPSEEK_API_KEY" }), string.Join(",", dk.Item1.Select(k => k.Value)));

            var seenUrls = new HashSet<string>();
            Http.Send = req =>
            {
                seenUrls.Add(req.Url);
                return req.Headers["Authorization"] == "Bearer sk-envonly9999"
                    ? new HttpReply(200, "{\"is_available\":false,\"balance_infos\":[{\"currency\":\"USD\",\"total_balance\":\"0.40\",\"granted_balance\":\"0.00\",\"topped_up_balance\":\"0.40\"}]}")
                    : new HttpReply(200, "{\"is_available\":true,\"balance_infos\":[{\"currency\":\"CNY\",\"total_balance\":\"110.00\",\"granted_balance\":\"10.00\",\"topped_up_balance\":\"100.00\"}]}");
            };
            var dr = new DeepSeekProvider(new Config()).Fetch();
            Check("deepseek: one page per key, shared key merged", dr.Ok && dr.Pages().Count == 2 && dr.Label == "OpenCode + DeepSeek Harness"
                                                                   && dr.Alternates[0].Label == "env DEEPSEEK_API_KEY",
                string.Join(",", dr.Pages().Select(pg => pg.Label + ":" + pg.Status)));
            var bal = dr.Info.FirstOrDefault(i => i.Label == "Balance");
            Check("deepseek: balance in yuan", bal != null && bal.Value == "\u00a5110.00" && bal.Tone == "good" && dr.Headline == "\u00a5110.00");
            Check("deepseek: topped up and granted", dr.Info.Any(i => i.Value.Contains("\u00a5100.00 topped up") && i.Value.Contains("granted")));
            var lowPage = dr.Alternates[0];
            Check("deepseek: low balance warns", lowPage.Info.Any(i => i.Label == "Balance" && i.Tone == "warn") && lowPage.Info.Any(i => i.Label == "Status"));
            Check("deepseek: pay-as-you-go tab", dr.Billing == "payg" && dr.Plan == "Pay as you go" && dr.Account == "key sk-...1234");
            Check("deepseek: keys only go to api.deepseek.com", seenUrls.SetEquals(new[] { DeepSeekProvider.BalanceUrl }));
            Check("deepseek: survives the cache", ProviderResult.FromCache(Json.ParseObject(Json.Write(dr.ToCache()))).Billing == "payg");
            Check("deepseek: full key kept for hover / Copy", dr.Secret == "sk-shared1234" && dr.Alternates[0].Secret == "sk-envonly9999");
            Check("deepseek: full key never cached", !Json.Write(dr.ToCache()).Contains("sk-shared1234")
                                                     && ProviderResult.FromCache(dr.ToCache()).Secret == null);
            DeepSeekProvider.Env = n => null;
            File.Delete(Path.Combine(dsHome, ".local", "share", "opencode", "auth.json"));
            File.Delete(Path.Combine(dsHome, ".dsh", ".credentials.yaml"));
            var nr = new DeepSeekProvider(new Config()).Fetch();
            Check("deepseek: no key, not installed", !nr.Ok && !nr.Installed);

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

            Console.WriteLine("--- shared credential reads ---");
            var sdir = Path.Combine(Paths.AppDir, "shared-read");
            Directory.CreateDirectory(sdir);
            var sf = Path.Combine(sdir, "oauth_creds.json");
            File.WriteAllText(sf, "{\"access_token\":\"abc\"}");
            Check("io: reads a credential file", Json.ParseObject(Core.Io.ReadAllTextShared(sf))?.Str("access_token") == "abc");
            File.WriteAllBytes(sf, new byte[] { 0xEF, 0xBB, 0xBF }.Concat(Encoding.UTF8.GetBytes("{\"x\":1}")).ToArray());
            Check("io: strips a UTF-8 BOM", Json.ParseObject(Core.Io.ReadAllTextShared(sf))?.Num("x") == 1);
            Check("io: missing file is null", Core.Io.ReadAllTextShared(Path.Combine(sdir, "nope.json")) == null);
            // The crux of the fix: while QuotaTray holds the file open with our
            // share mode, the owning CLI can still replace it (atomic rename).
            using (new FileStream(sf, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                var tmp = sf + ".tmp";
                File.WriteAllText(tmp, "{\"x\":2}");
                var replaced = false;
                try { File.Replace(tmp, sf, null); replaced = true; }
                catch (Exception) { try { File.Delete(tmp); } catch (Exception) { } }
                Check("io: owner can replace the file while QuotaTray reads it", replaced);
            }

            Console.WriteLine("--- config.json hand edits ---");
            {
                var cf = Paths.ConfigFile;
                var hadConfig = File.Exists(cf) ? File.ReadAllBytes(cf) : null;
                try
                {
                    // Notepad can save UTF-8 with a BOM; it must still load.
                    var bom = new byte[] { 0xEF, 0xBB, 0xBF };
                    File.WriteAllBytes(cf, bom.Concat(Encoding.UTF8.GetBytes("{\"providers\":{\"doubao\":{\"session_id\":\"bom-sid\"}}}")).ToArray());
                    var c = Config.Load();
                    Check("config: a BOM-prefixed config.json loads", c.Error == null && c.Provider("doubao").Str("session_id") == "bom-sid", c.Error);
                    Check("config: unchanged file is not reloaded", !c.ChangedOnDisk());
                    // A hand edit is noticed without a restart.
                    File.WriteAllText(cf, "{\"providers\":{\"doubao\":{\"session_id\":\"new-sid\"}}}");
                    File.SetLastWriteTimeUtc(cf, DateTime.UtcNow.AddSeconds(5));
                    Check("config: an edit on disk is noticed", c.ChangedOnDisk());
                    Check("config: reload picks up the new session_id", Config.Load().Provider("doubao").Str("session_id") == "new-sid");
                    // A typo in the file must NOT be overwritten with defaults.
                    const string broken = "{\"providers\": {\"doubao\": {\"session_id\": \"keep-me\",,}}}";
                    File.WriteAllText(cf, broken);
                    var cb = Config.Load();
                    Check("config: a broken file reports an error", cb.Error != null, cb.Error ?? "(no error)");
                    cb.Data["last_reset_reminder"] = "2026-10-01";
                    cb.Save();
                    Check("config: a broken file is never overwritten", File.ReadAllText(cf) == broken);
                }
                finally
                {
                    if (hadConfig != null) File.WriteAllBytes(cf, hadConfig);
                    else try { File.Delete(cf); } catch (Exception) { }
                }
            }

            try { Directory.Delete(Paths.AppDir, true); } catch (Exception) { }
            Console.WriteLine($"\npassed {Pass.Count} / {Pass.Count + Fail.Count}");
            if (Fail.Count > 0) Console.WriteLine("failures: " + string.Join("; ", Fail));
            return Fail.Count > 0 ? 1 : 0;
        }
    }
}
