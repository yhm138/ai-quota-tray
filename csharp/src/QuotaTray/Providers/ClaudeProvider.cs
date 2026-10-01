using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using QuotaTray.Core;
using QuotaTray.Win;

namespace QuotaTray.Providers
{
    public sealed class ClaudeLogin
    {
        public string Token;
        public string Origin;       // file it came from, or a description
        public string Plan;

        public ClaudeLogin(string token, string origin, string plan)
        {
            Token = token; Origin = origin; Plan = plan;
        }
    }

    /// <summary>
    /// Claude quota collection, in the order of providers.claude.order:
    ///   oauth          the OAuth credentials Claude Code writes locally (Windows and WSL)
    ///   desktop_oauth  the OAuth token Claude Desktop keeps in config.json, encrypted
    ///                  with the app's OSCrypt key
    /// Both go to api.anthropic.com/api/oauth/usage. The claude.ai cookie paths of
    /// the Python build (desktop_cookie, manual_cookie) are not part of this one.
    /// </summary>
    public sealed class ClaudeProvider : Provider
    {
        public override string Id => "claude";
        public override string Name => "Claude";

        public const string UsageUrl = "https://api.anthropic.com/api/oauth/usage";
        public const string ProfileUrl = "https://api.anthropic.com/api/oauth/profile";
        public const string OAuthBeta = "oauth-2025-04-20";
        // The usage endpoint only reports banked limit resets ("cedar_ember")
        // to the Claude Code CLI; other user agents get ineligible_reason "surface".
        public const string ClaudeCodeUa = "claude-cli/2.1.280 (external, cli)";
        private const int ProfileTtl = 6 * 3600;

        public static readonly string[] DefaultOrder = { "oauth", "desktop_oauth", "desktop_cookie", "manual_cookie" };

        /// <summary>Tests replace these.</summary>
        public static Func<JObj, Tuple<List<ClaudeLogin>, List<string>>> DiscoverLogins = DiscoverOAuthTokens;
        public static Func<Tuple<List<KeyValuePair<string, string>>, List<string>>> DesktopTokens = () => DesktopOAuthTokens(null);

        public ClaudeProvider(Config config) : base(config) { }

        // ------------------------------------------------------------ labels

        private static readonly Dictionary<string, Tuple<string, int>> WindowLabels = new Dictionary<string, Tuple<string, int>>
        {
            { "five_hour", L("5-hour window", 10) },
            { "fivehour", L("5-hour window", 10) },
            { "session", L("Session", 10) },
            { "seven_day", L("7-day window", 20) },
            { "sevenday", L("7-day window", 20) },
            { "weekly", L("7-day window", 20) },
            { "seven_day_opus", L("7-day Opus", 30) },
            { "seven_day_sonnet", L("7-day Sonnet", 31) },
            { "seven_day_haiku", L("7-day Haiku", 32) },
            { "seven_day_fable", L("7-day Fable", 33) },
            { "seven_day_cowork", L("7-day Cowork", 34) },
            { "cowork", L("Cowork", 34) },
            { "seven_day_overage_included", L("7-day overage allowance", 35) },
            { "extra_usage", L("Extra usage", 40) },
            { "monthly", L("Monthly", 45) },
        };

        private static Tuple<string, int> L(string label, int order) => Tuple.Create(label, order);

        // Anthropic ships new limits under random two-word code names first.
        // Known ones are shown as what they are; the rest stay out of the bars.
        public static readonly Dictionary<string, string> CodenameCredits = new Dictionary<string, string>
        {
            { "iguana_necktie", "Cloud credit" },     // $100 Claude Code cloud-sessions credit
        };

        private static readonly string[][] PeriodPrefixes =
        {
            new[] { "five_hour", "5-hour" }, new[] { "seven_day", "7-day" }, new[] { "thirty_day", "30-day" },
            new[] { "monthly", "Monthly" }, new[] { "weekly", "Weekly" }, new[] { "daily", "Daily" },
        };

        private static readonly HashSet<string> KnownWords = new HashSet<string>
        {
            "opus", "sonnet", "haiku", "fable", "cowork", "extra", "usage", "session", "overage", "included", "oauth", "apps",
        };

        private static readonly string[] NotWindows = { "cedar_ember", "spend" };

        /// <summary>'nimbus_quill', 'tangelo' yes; 'seven_day_opus', 'extra_usage' no.</summary>
        public static bool IsCodename(string key)
        {
            var k = key.ToLowerInvariant();
            if (WindowLabels.ContainsKey(k) || PeriodPrefixes.Any(p => k.StartsWith(p[0], StringComparison.Ordinal))) return false;
            var words = k.Split('_');
            return (words.Length == 1 || words.Length == 2)
                   && words.All(w => w.Length > 0 && w.All(char.IsLetter))
                   && !words.Any(KnownWords.Contains);
        }

        public static Tuple<string, int> LabelFor(string key)
        {
            var k = key.ToLowerInvariant();
            if (WindowLabels.TryGetValue(k, out var known)) return known;
            foreach (var p in PeriodPrefixes)
            {
                if (k.StartsWith(p[0] + "_", StringComparison.Ordinal))
                {
                    var rest = k.Substring(p[0].Length + 1).Replace("_", " ").Trim();
                    return L($"{p[1]} {Account.Title(rest)}", 36);
                }
            }
            return L(Account.Title(key.Replace("_", " ").Trim()), 50);
        }

        // ------------------------------------------------------------ credentials

        public static List<string> CredentialCandidates(JObj settings)
        {
            var seen = new List<string>();
            void Add(string p)
            {
                if (!string.IsNullOrEmpty(p) && !seen.Contains(p)) seen.Add(p);
            }

            var manual = Core.Settings.Str(settings, "credentials_path");
            if (manual.Length > 0) Add(manual);

            var bases = new List<string>();
            var cfgDir = Environment.GetEnvironmentVariable("CLAUDE_CONFIG_DIR");
            if (!string.IsNullOrEmpty(cfgDir))
                bases.AddRange(cfgDir.Split(Path.PathSeparator).Where(s => s.Trim().Length > 0));
            bases.Add(Path.Combine(Paths.Home, ".claude"));
            foreach (var env in new[] { "APPDATA", "LOCALAPPDATA", "USERPROFILE" })
            {
                var b = Environment.GetEnvironmentVariable(env);
                if (string.IsNullOrEmpty(b)) continue;
                bases.Add(Path.Combine(b, "Claude"));
                bases.Add(Path.Combine(b, ".claude"));
            }
            bases.Add(Path.Combine(Paths.Home, ".config", "claude"));
            foreach (var b in bases)
            {
                Add(Path.Combine(b, ".credentials.json"));
                Add(Path.Combine(b, "credentials.json"));
            }

            if (Core.Settings.Flag(settings, "scan_wsl", true))
            {
                foreach (var distro in Proc.WslDistros())
                {
                    foreach (var prefix in new[] { @"\\wsl.localhost", @"\\wsl$" })
                    {
                        var root = $@"{prefix}\{distro}";
                        Add(Path.Combine(root, "root", ".claude", ".credentials.json"));
                        try
                        {
                            var home = Path.Combine(root, "home");
                            if (Directory.Exists(home))
                                foreach (var user in Directory.GetDirectories(home).Take(10))
                                    Add(Path.Combine(user, ".claude", ".credentials.json"));
                        }
                        catch (Exception) { }
                    }
                }
            }
            return seen;
        }

        /// <summary>(access token, expiresAt in ms, subscriptionType) from a credentials file.</summary>
        public static Tuple<string, double?, string> TokenFromFile(string path)
        {
            JObj data;
            try { var _t = Io.ReadAllTextShared(path); data = _t != null ? Json.ParseObject(_t) : null; }
            catch (Exception) { return Tuple.Create<string, double?, string>(null, null, null); }
            if (data == null) return Tuple.Create<string, double?, string>(null, null, null);
            var node = data.Obj("claudeAiOauth") ?? data.Obj("oauth") ?? data;
            var token = node.Str("accessToken") ?? node.Str("access_token");
            if (token == null || token.Length < 20) return Tuple.Create<string, double?, string>(null, null, null);
            var expires = node.Num("expiresAt") ?? node.Num("expires_at");
            var plan = node.Str("subscriptionType") ?? node.Str("subscription_type");
            return Tuple.Create(token, expires, plan);
        }

        /// <summary>Every usable Claude Code login: Windows and each WSL distro can differ.</summary>
        public static Tuple<List<ClaudeLogin>, List<string>> DiscoverOAuthTokens(JObj settings)
        {
            var notes = new List<string>();
            var logins = new List<ClaudeLogin>();
            var manual = Core.Settings.Str(settings, "oauth_token");
            if (manual.Length > 0) logins.Add(new ClaudeLogin(manual, "token from config.json", null));
            var envToken = (Environment.GetEnvironmentVariable("CLAUDE_CODE_OAUTH_TOKEN") ?? "").Trim();
            if (envToken.Length > 0) logins.Add(new ClaudeLogin(envToken, "env CLAUDE_CODE_OAUTH_TOKEN", null));

            var checkedCount = 0;
            foreach (var path in CredentialCandidates(settings))
            {
                checkedCount++;
                if (!Proc.SafeFileExists(path)) continue;
                var t = TokenFromFile(path);
                if (t.Item1 == null)
                {
                    notes.Add($"{path}: file exists but no accessToken in it");
                    continue;
                }
                if (t.Item2 != null && t.Item2.Value / 1000.0 < Time.Now.ToUnixTimeSeconds() - 60)
                {
                    notes.Add($"{path}: token expired (run Claude Code once to refresh it)");
                    continue;
                }
                if (logins.All(l => l.Token != t.Item1)) logins.Add(new ClaudeLogin(t.Item1, path, t.Item3));
            }
            if (logins.Count == 0) notes.Add($"checked {checkedCount} candidate paths, no usable credentials");
            return Tuple.Create(logins, notes);
        }

        public static string LoginLabel(string origin)
        {
            var m = Regex.Match(origin ?? "", @"^[\\/]{2}wsl(?:\.localhost|\$)[\\/]([^\\/]+)");
            return m.Success ? $"Claude Code - WSL {m.Groups[1].Value}" : "Claude Code";
        }

        // ------------------------------------------------------------ parsing

        public static List<QuotaWindow> BuildWindows(object payload, string scaleMode, DateTimeOffset? baseTime = null)
        {
            var nodes = Extract.QuotaNodes(payload).ToList();
            var windows = new List<QuotaWindow>();
            if (nodes.Count == 0) return windows;
            var raw = nodes.Select(n => Extract.Used(n.Value)).Where(v => v != null).Select(v => v.Value).ToList();
            double scale;
            if (scaleMode == "percent") scale = 1.0;
            else if (scaleMode == "fraction") scale = 100.0;
            // Both Claude endpoints report `utilization` as 0-100; guessing from
            // magnitude would turn 1% (early in a window) into 100%.
            else if (nodes.All(n => Json.AsNumber(n.Value["utilization"]) != null)) scale = 1.0;
            else scale = Extract.DecideScale(raw);

            var seen = new HashSet<string>();
            foreach (var kv in nodes)
            {
                var path = kv.Key;
                var node = kv.Value;
                var key = path.Length > 0 ? path[path.Length - 1] : "usage";
                if (key.All(char.IsDigit) && path.Length >= 2) key = path[path.Length - 2];
                if (seen.Contains(key)) key = path.Length >= 2 ? string.Join("/", path.Skip(path.Length - 2)) : key;
                seen.Add(key);
                var used = Extract.Used(node);
                if (used == null || IsCodename(key)) continue;       // code-named experiments are not bars
                var lo = LabelFor(key);
                string detail = null;
                var limit = node.Num("monthly_limit") ?? node.Num("limit");
                var usedCredits = node.Num("used_credits") ?? node.Num("used");
                if (limit != null && usedCredits != null) detail = $"{Account.N0(usedCredits.Value)} / {Account.N0(limit.Value)}";
                if (node.Bool("is_enabled") == false) detail = detail != null ? detail + " - off" : "off";
                var pct = Extract.ApplyScale(used, scale);
                windows.Add(new QuotaWindow(key, lo.Item1, pct, Extract.Reset(node, baseTime), detail, lo.Item2,
                    pct != null && pct >= 99.9));
            }
            return windows;
        }

        // ------------------------------------------------------------ provider

        public override bool Detect()
        {
            if (Proc.AppRoots("Claude").Count > 0) return true;
            if (Proc.SafeDirExists(Path.Combine(Paths.Home, ".claude"))) return true;
            return DiscoverLogins(Settings).Item1.Count > 0;
        }

        private List<ProviderResult> OAuthPages(ProviderResult result)
        {
            var found = DiscoverLogins(Settings);
            if (found.Item1.Count == 0)
            {
                result.Attempts.Add(new SourceAttempt("OAuth credentials", false,
                    found.Item2.Count > 0 ? string.Join("; ", found.Item2) : "no Claude Code credentials on this machine"));
                return new List<ProviderResult>();
            }
            var pages = new List<ProviderResult>();
            foreach (var login in found.Item1)
            {
                var page = new ProviderResult(Id, Name) { FetchedAt = result.FetchedAt };
                var origin = login.Origin;
                var where = origin.Contains("\\") || origin.Contains("/") ? Path.GetFileName(origin) : origin;
                if (UsageWithToken(page, login.Token, "OAuth credentials", $"Claude Code OAuth ({where})", origin, login.Plan))
                {
                    page.Label = LoginLabel(origin);
                    pages.Add(page);
                }
                result.Attempts.AddRange(page.Attempts);
            }
            return pages;
        }

        private List<ProviderResult> DesktopPages(ProviderResult result)
        {
            var page = new ProviderResult(Id, Name) { FetchedAt = result.FetchedAt };
            var ok = TryDesktopOAuth(page);
            result.Attempts.AddRange(page.Attempts);
            if (!ok) return new List<ProviderResult>();
            page.Label = "Claude Desktop";
            return new List<ProviderResult> { page };
        }

        private bool TryDesktopOAuth(ProviderResult result)
        {
            const string tag = "Claude Desktop login";
            var found = DesktopTokens();
            if (found.Item1.Count == 0)
            {
                var notes = found.Item2;
                result.Attempts.Add(new SourceAttempt(tag, false,
                    notes.Count > 0 ? string.Join("; ", notes.Skip(Math.Max(0, notes.Count - 3))) : "no Claude Desktop login found"));
                return false;
            }
            foreach (var t in found.Item1.Take(2))
                if (UsageWithToken(result, t.Key, tag, "Claude Desktop login", t.Value, null)) return true;
            return false;
        }

        public static Dictionary<string, string> OAuthHeaders(string token) => new Dictionary<string, string>
        {
            { "Authorization", "Bearer " + token },
            { "anthropic-beta", OAuthBeta },
            { "User-Agent", ClaudeCodeUa },
            { "Accept", "application/json" },
        };

        private bool UsageWithToken(ProviderResult result, string token, string tag, string label, string origin, string plan)
        {
            HttpReply resp;
            try
            {
                resp = Http.Get(Http.Query(UsageUrl, "cedar_ember", "1"), OAuthHeaders(token));
            }
            catch (Exception e)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "request failed: " + e.Message));
                return false;
            }
            if (resp.Status == 401 || resp.Status == 403)
            {
                var hint = tag.Contains("Desktop") ? "open Claude Desktop so it renews its login" : "sign in to Claude Code again";
                result.Attempts.Add(new SourceAttempt(tag, false, $"token rejected (HTTP {resp.Status}), {hint}"));
                return false;
            }
            if (resp.Status == 429)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "rate limited (429), will retry on next refresh"));
                return false;
            }
            if (resp.Status >= 400)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, $"HTTP {resp.Status}: {Short(resp.Text, 160)}"));
                return false;
            }
            if (!Json.TryParse(resp.Text, out var payload))
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "response was not JSON"));
                return false;
            }

            // Reset grants carry a percent field and spend is money, not a window.
            object bars = payload;
            if (payload is JObj po)
            {
                var copy = new JObj();
                foreach (var kv in po) if (!NotWindows.Contains(kv.Key)) copy[kv.Key] = kv.Value;
                bars = copy;
            }
            var windows = BuildWindows(bars, Settings.Str("percent_scale") ?? "auto");
            if (windows.Count == 0)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "no quota fields in response: " + Short(Json.Write(payload), 160)));
                return false;
            }
            var usage = payload as JObj ?? new JObj();
            result.Windows = windows;
            result.Ok = true;
            result.Source = label;
            result.Plan = plan ?? GuessPlan(usage);
            result.Account = GuessAccount(usage);
            result.Status = "connected";
            result.Attempts.Add(new SourceAttempt(tag, true, origin));
            try
            {
                var profile = Profile(token);
                var info = ProfileInfo(profile);
                result.Plan = info.Item1 ?? result.Plan;
                result.Account = result.Account ?? info.Item2;
                result.Info = info.Item3.Concat(SpendInfo(usage)).ToList();
                result.Windows.AddRange(CreditWindows(usage));
                result.Resets = Resets(usage);
                var hidden = HiddenCodenames(usage);
                if (hidden.Count > 0)
                    result.Attempts.Add(new SourceAttempt("internal quotas (not shown)", true, string.Join("; ", hidden)));
            }
            catch (Exception e)
            {
                Log.Debug("claude account details failed: " + e);
            }
            return true;
        }

        public override void Collect(ProviderResult result)
        {
            var order = Core.Settings.List(Settings, "order");
            if (order.Count == 0) order = DefaultOrder.ToList();
            // Configs saved before desktop_oauth existed hold the old default list.
            if (!order.Contains("desktop_oauth") && order.Contains("desktop_cookie"))
                order.Insert(order.IndexOf("desktop_cookie"), "desktop_oauth");
            // Every login is checked: Claude Code and Claude Desktop (or WSL)
            // can be signed in to different accounts.
            var pages = new List<ProviderResult>();
            foreach (var step in order)
            {
                if (step == "oauth") pages.AddRange(OAuthPages(result));
                else if (step == "desktop_oauth") pages.AddRange(DesktopPages(result));
            }
            if (pages.Count > 0)
            {
                pages = Account.GroupPages(pages);
                result.Adopt(pages[0]);
                result.Alternates = pages.Skip(1).ToList();
                return;
            }
            if (!result.Installed)
            {
                result.Status = "Claude not detected";
                return;
            }
            var failed = result.Attempts.Where(a => !a.Ok).ToList();
            var best = failed.FirstOrDefault(a => a.Name == "Claude Desktop login")
                       ?? failed.FirstOrDefault(a => a.Name.Contains("Desktop"))
                       ?? failed.LastOrDefault();
            result.Status = best != null ? Short($"{best.Name}: {best.Detail}", 200) : "no usable quota source";
        }

        // ------------------------------------------------------------ plan, credits, resets

        private static readonly Dictionary<string, Tuple<DateTime, JObj>> Profiles = new Dictionary<string, Tuple<DateTime, JObj>>();

        public static void ClearProfiles()
        {
            lock (Profiles) Profiles.Clear();
        }

        private static JObj Profile(string token)
        {
            string key;
            using (var sha = SHA256.Create())
                key = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(token)));
            lock (Profiles)
            {
                if (Profiles.TryGetValue(key, out var hit) && (DateTime.UtcNow - hit.Item1).TotalSeconds < ProfileTtl) return hit.Item2;
            }
            var resp = Http.Get(ProfileUrl, OAuthHeaders(token));
            var data = resp.Status == 200 ? Json.ParseObject(resp.Text) ?? new JObj() : new JObj();
            lock (Profiles) Profiles[key] = Tuple.Create(DateTime.UtcNow, data);
            return data;
        }

        /// <summary>(plan, account, rows) from /api/oauth/profile.</summary>
        public static Tuple<string, string, List<InfoRow>> ProfileInfo(JObj profile)
        {
            var account = profile.Obj("account") ?? new JObj();
            var org = profile.Obj("organization") ?? new JObj();
            var plan = Account.PrettyPlan(org["rate_limit_tier"]) ?? Account.PrettyPlan(org["organization_type"]);
            if (plan == null)
            {
                if (account.Truthy("has_claude_max")) plan = "Max";
                else if (account.Truthy("has_claude_pro")) plan = "Pro";
            }
            var rows = new List<InfoRow>();
            var status = org.Str("subscription_status");
            if (plan != null)
            {
                var value = status == null ? plan : $"{plan} \u00b7 {status}";
                rows.Add(new InfoRow("Plan", value, status == null || status == "active" ? "" : "warn"));
            }
            var started = Time.Parse(org["subscription_created_at"]);
            if (started != null) rows.Add(new InfoRow("Subscribed", "since " + Account.FmtDate(started)));
            var email = account.Str("email_address") ?? account.Str("email");
            return Tuple.Create(plan, email, rows);
        }

        /// <summary>Extra usage (pay-as-you-go credit beyond the plan).</summary>
        public static List<InfoRow> SpendInfo(JObj usage)
        {
            var spend = usage.Obj("spend");
            if (spend == null) return new List<InfoRow>();
            if (spend.Bool("enabled") == false) return new List<InfoRow> { new InfoRow("Extra usage", "off") };
            var used = Account.MinorAmount(spend["used"]);
            var limit = Account.MinorAmount(spend["limit"]);
            var currency = spend.Obj("used")?.Str("currency");
            if (used == null) return new List<InfoRow>();
            if (limit != null && limit.Value != 0)
            {
                var left = Math.Max(0.0, limit.Value - used.Value);
                var tone = left <= limit.Value * 0.1 ? "warn" : "";
                return new List<InfoRow>
                {
                    new InfoRow("Extra usage",
                        $"{Account.Money(used.Value, currency)} of {Account.Money(limit.Value, currency)} used \u00b7 {Account.Money(left, currency)} left",
                        tone),
                };
            }
            return new List<InfoRow> { new InfoRow("Extra usage", $"{Account.Money(used.Value, currency)} used") };
        }

        /// <summary>Known code-named credits (the cloud credit) as bars after the limits.</summary>
        public static List<QuotaWindow> CreditWindows(JObj usage)
        {
            var output = new List<QuotaWindow>();
            foreach (var kv in CodenameCredits)
            {
                var node = usage.Obj(kv.Key);
                if (node == null) continue;
                var used = Extract.Used(node);
                if (used == null) continue;
                var ends = Extract.Reset(node);
                var pct = Math.Max(0.0, Math.Min(100.0, used.Value));
                output.Add(new QuotaWindow(kv.Key, kv.Value, pct, null,
                    ends != null ? "expires " + Account.FmtDate(ends) : null, 60, pct >= 99.9, true));
            }
            return output;
        }

        /// <summary>Code-named entries kept out of the panel, for Diagnostics.</summary>
        public static List<string> HiddenCodenames(JObj usage)
        {
            var output = new List<string>();
            foreach (var kv in usage)
            {
                if (!IsCodename(kv.Key) || CodenameCredits.ContainsKey(kv.Key) || !(kv.Value is JObj node)) continue;
                var used = Extract.Used(node);
                output.Add(used != null ? $"{kv.Key} {used.Value.ToString("0.######", CultureInfo.InvariantCulture)}%" : $"{kv.Key} (empty)");
            }
            return output;
        }

        /// <summary>Banked limit resets ("cedar_ember" grants).</summary>
        public static List<ResetGrant> Resets(JObj usage)
        {
            var output = new List<ResetGrant>();
            var node = usage.Obj("cedar_ember");
            if (node == null || node.Bool("eligible") == false) return output;
            foreach (var g in (node.Arr("grants") ?? new List<object>()).OfType<JObj>())
            {
                if (g.Truthy("paused")) continue;
                var left = g.Num("resets_left");
                if (left == null || left.Value != Math.Floor(left.Value) || left.Value <= 0) continue;
                var clears = (g.Arr("clears") ?? new List<object>()).OfType<string>().ToList();
                var bits = new List<string>();
                if (clears.Count > 0) bits.Add("clears " + string.Join(" + ", clears.Select(c => LabelFor(c).Item1)));
                if (g.Truthy("use_requires_limit")) bits.Add("usable once you hit a limit");
                else if (g.Truthy("usable_now")) bits.Add("usable now");
                output.Add(new ResetGrant((int)left.Value, Time.Parse(g["ends_at"]), string.Join(", ", bits)));
            }
            return output;
        }

        private static string GuessAccount(JObj payload)
        {
            foreach (var key in new[] { "account", "user", "profile" })
            {
                var node = payload.Obj(key);
                if (node == null) continue;
                foreach (var f in new[] { "email_address", "email", "display_name", "full_name" })
                    if (node.Str(f) != null) return node.Str(f);
            }
            return payload.Str("email_address") ?? payload.Str("email");
        }

        private static string GuessPlan(JObj payload)
        {
            foreach (var f in new[] { "subscription_type", "subscriptionType", "plan", "tier", "rate_limit_tier" })
                if (payload.Text(f) != null) return payload.Text(f);
            return null;
        }

        // ------------------------------------------------------------ Claude Desktop login

        // Newest layout first: current builds keep the live grant in V2.
        public static readonly string[] DesktopTokenCacheKeys = { "oauth:tokenCacheV2", "oauth:tokenCache" };

        public sealed class TokenRank : IComparable<TokenRank>
        {
            public bool FullScope, Inference, V2;
            public double Expires;

            public int CompareTo(TokenRank o)
            {
                var c = FullScope.CompareTo(o.FullScope);
                if (c != 0) return c;
                c = Inference.CompareTo(o.Inference);
                if (c != 0) return c;
                c = V2.CompareTo(o.V2);
                return c != 0 ? c : Expires.CompareTo(o.Expires);
            }
        }

        /// <summary>
        /// Usable tokens from a decrypted Claude Desktop token cache, which maps
        /// "&lt;install&gt;:&lt;user&gt;:&lt;base url&gt;:&lt;scopes&gt;" to {token, expiresAt, refreshToken}.
        /// </summary>
        public static List<KeyValuePair<TokenRank, string>> ParseDesktopTokenCache(string plaintext, double nowMs)
        {
            var output = new List<KeyValuePair<TokenRank, string>>();
            var entries = Json.ParseObject(plaintext);
            if (entries == null) return output;
            foreach (var kv in entries)
            {
                if (!(kv.Value is JObj entry)) continue;
                var token = entry.Text("token") ?? entry.Text("accessToken");
                if (token == null || token.Trim().Length == 0) continue;
                var expires = entry.Num("expiresAt");
                if (expires != null && expires.Value < nowMs) continue;
                var scopes = new HashSet<string>(Regex.Matches(kv.Key, "user:[a-z_]+").Cast<Match>().Select(m => m.Value));
                var rank = new TokenRank
                {
                    FullScope = scopes.Contains("user:inference") && scopes.Contains("user:profile"),
                    Inference = scopes.Contains("user:inference"),
                    Expires = expires ?? 0.0,
                };
                output.Add(new KeyValuePair<TokenRank, string>(rank, token.Trim()));
            }
            return output;
        }

        /// <summary>The OSCrypt key from Local State, unwrapped with DPAPI.</summary>
        public static byte[] MasterKey(string localState)
        {
            var data = Json.ParseObject(Io.ReadAllTextShared(localState) ?? "")
                       ?? throw new CryptoError("cannot read Local State");
            var encoded = data.Obj("os_crypt")?.Str("encrypted_key");
            if (string.IsNullOrEmpty(encoded)) throw new CryptoError("Local State has no os_crypt.encrypted_key");
            var blob = Convert.FromBase64String(encoded);
            if (blob.Length >= 5 && Encoding.ASCII.GetString(blob, 0, 5) == "DPAPI") blob = blob.Skip(5).ToArray();
            return Dpapi.Unprotect(blob);
        }

        /// <summary>blob = "v10" || 12-byte nonce || ciphertext || 16-byte tag.</summary>
        public static byte[] DecryptV10(byte[] key, byte[] blob)
        {
            if (blob.Length < 15 + AesGcmDecryptor.TagLength || Encoding.ASCII.GetString(blob, 0, 3) != "v10")
                throw new CryptoError("not a v10 value");
            var nonce = blob.Skip(3).Take(12).ToArray();
            return AesGcmDecryptor.Decrypt(key, nonce, blob.Skip(15).ToArray());
        }

        /// <summary>Every usable Claude Desktop OAuth token, best first: [(token, where)].</summary>
        public static Tuple<List<KeyValuePair<string, string>>, List<string>> DesktopOAuthTokens(List<string> roots,
            Func<string, byte[]> masterKey = null)
        {
            var notes = new List<string>();
            roots = roots ?? Proc.AppRoots("Claude");
            masterKey = masterKey ?? MasterKey;
            var found = new List<Tuple<TokenRank, string, string>>();
            var nowMs = Time.Now.ToUnixTimeMilliseconds();
            foreach (var root in roots)
            {
                var name = Path.GetFileName(root.TrimEnd('\\', '/'));
                var cfg = Path.Combine(root, "config.json");
                JObj data;
                try { var _c = Io.ReadAllTextShared(cfg); data = _c != null ? Json.ParseObject(_c) : null; }
                catch (Exception) { data = null; }
                if (data == null)
                {
                    notes.Add($"{name}: no readable config.json");
                    continue;
                }
                var caches = DesktopTokenCacheKeys.Where(k => data.Text(k) != null).Select(k => Tuple.Create(k, data.Text(k))).ToList();
                if (caches.Count == 0)
                {
                    notes.Add($"{name}: config.json has no saved login (sign in to Claude Desktop)");
                    continue;
                }
                var localState = Path.Combine(root, "Local State");
                if (!File.Exists(localState))
                {
                    notes.Add($"{name}: no Local State next to config.json");
                    continue;
                }
                byte[] key;
                try { key = masterKey(localState); }
                catch (Exception e)
                {
                    notes.Add($"{name}: could not unwrap the app key ({e.Message})");
                    continue;
                }
                var before = found.Count;
                foreach (var c in caches)
                {
                    string plain;
                    try
                    {
                        plain = Encoding.UTF8.GetString(DecryptV10(key, Convert.FromBase64String(c.Item2)));
                    }
                    catch (Exception e)
                    {
                        notes.Add($"{name}: could not decrypt {c.Item1} ({e.GetType().Name})");
                        continue;
                    }
                    foreach (var t in ParseDesktopTokenCache(plain, nowMs))
                    {
                        t.Key.V2 = c.Item1.EndsWith("V2", StringComparison.Ordinal);
                        found.Add(Tuple.Create(t.Key, t.Value, $"{cfg} [{c.Item1}]"));
                    }
                }
                if (found.Count == before) notes.Add($"{name}: the saved login has expired (open Claude Desktop to renew it)");
            }
            var tokens = new List<KeyValuePair<string, string>>();
            var seen = new HashSet<string>();
            foreach (var f in found.OrderByDescending(f => f.Item1))
                if (seen.Add(f.Item2)) tokens.Add(new KeyValuePair<string, string>(f.Item2, f.Item3));
            return Tuple.Create(tokens, notes);
        }
    }
}
