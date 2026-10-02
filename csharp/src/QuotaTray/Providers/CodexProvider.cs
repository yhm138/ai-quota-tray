using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using QuotaTray.Core;
using QuotaTray.Win;

namespace QuotaTray.Providers
{
    /// <summary>
    /// Codex (OpenAI) quota collection, in the order of providers.codex.order:
    ///   wham        access_token from ~/.codex/auth.json -> chatgpt.com/backend-api/wham/usage
    ///   app_server  `codex app-server`, JSON-RPC account/rateLimits/read
    ///   jsonl       the last rate_limits record in ~/.codex/sessions/**/rollout-*.jsonl
    /// (The Python build's sqlite scan is not part of this one.)
    /// </summary>
    public sealed class CodexProvider : Provider
    {
        public override string Id => "codex";
        public override string Name => "Codex";

        public const string WhamUrl = "https://chatgpt.com/backend-api/wham/usage";
        public const string ResetCreditsUrl = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits";
        public const string SubscriptionsUrl = "https://chatgpt.com/backend-api/subscriptions";
        private const double CreditsPerDollar = 25.0;       // Codex credits are sold at 25 to the dollar
        private const int ExtrasTtl = 3600;
        private const int ExtrasRetry = 300;

        private static readonly string[] SnapshotSources = { "jsonl", "sqlite" };

        /// <summary>Tests replace this: (tokens, file) for the configured home.</summary>
        public Func<Tuple<JObj, string>> AuthTokensOverride;

        public CodexProvider(Config config) : base(config) { }

        private CodexProvider(Config config, JObj settings) : base(config)
        {
            Settings = settings;
        }

        public static string CodexHome(JObj settings)
        {
            var manual = Core.Settings.Str(settings, "codex_home");
            if (manual.Length > 0) return manual;
            var env = Environment.GetEnvironmentVariable("CODEX_HOME");
            if (!string.IsNullOrEmpty(env)) return env;
            return Path.Combine(Paths.Home, ".codex");
        }

        public static string FindCodexExe()
        {
            var path = Environment.GetEnvironmentVariable("PATH") ?? "";
            foreach (var dir in path.Split(Path.PathSeparator))
            {
                if (dir.Trim().Length == 0) continue;
                foreach (var name in new[] { "codex.exe", "codex.cmd", "codex.bat", "codex" })
                {
                    try
                    {
                        var p = Path.Combine(dir.Trim().Trim('"'), name);
                        if (File.Exists(p)) return p;
                    }
                    catch (ArgumentException) { }
                }
            }
            var candidates = new List<string>
            {
                Path.Combine(Paths.Home, ".bun", "bin", "codex.exe"),
                Path.Combine(Paths.Home, ".local", "bin", "codex"),
                Path.Combine(Paths.Home, ".codex", "bin", "codex.exe"),
            };
            foreach (var env in new[] { "APPDATA", "LOCALAPPDATA", "ProgramFiles" })
            {
                var b = Environment.GetEnvironmentVariable(env);
                if (string.IsNullOrEmpty(b)) continue;
                candidates.Add(Path.Combine(b, "npm", "codex.cmd"));
                candidates.Add(Path.Combine(b, "Programs", "codex", "codex.exe"));
            }
            return candidates.FirstOrDefault(Proc.SafeFileExists);
        }

        // ------------------------------------------------------------ window labels

        private static Tuple<string, int> LabelFromMinutes(double? minutes, string fallback)
        {
            if (minutes == null || minutes <= 0) return Tuple.Create(fallback, 0);
            var m = minutes.Value;
            if (m < 90) return Tuple.Create($"{Math.Round(m):0}-minute window", 5);
            if (m < 1440) return Tuple.Create($"{Math.Round(m / 60):0}-hour window", 10);
            return Tuple.Create($"{Math.Round(m / 1440):0}-day window", 20);
        }

        private static readonly Dictionary<string, Tuple<string, int>> KeyAliases = new Dictionary<string, Tuple<string, int>>
        {
            { "primary", Tuple.Create("Short window", 10) },
            { "short", Tuple.Create("Short window", 10) },
            { "fivehour", Tuple.Create("5-hour window", 10) },
            { "5h", Tuple.Create("5-hour window", 10) },
            { "hourly", Tuple.Create("Hourly window", 10) },
            { "daily", Tuple.Create("Daily window", 15) },
            { "secondary", Tuple.Create("Long window", 20) },
            { "long", Tuple.Create("Long window", 20) },
            { "weekly", Tuple.Create("7-day window", 20) },
            { "sevenday", Tuple.Create("7-day window", 20) },
            { "monthly", Tuple.Create("Monthly window", 25) },
            { "tertiary", Tuple.Create("Extra window", 30) },
        };

        /// <summary>primary_window / primaryWindow / Primary Rate Limit -> primary</summary>
        public static string Squash(string key)
        {
            var k = Regex.Replace((key ?? "").ToLowerInvariant(), "[^a-z0-9]", "");
            foreach (var noise in new[] { "ratelimit", "window", "limit", "quota", "usage" }) k = k.Replace(noise, "");
            return k;
        }

        private static Tuple<string, int> LabelForKey(string key)
        {
            if (KeyAliases.TryGetValue(Squash(key), out var known)) return known;
            var pretty = Account.Title(Regex.Replace(key, @"[_\-]+", " ").Trim());
            return Tuple.Create(pretty.Length > 0 ? pretty : "Window", 50);
        }

        /// <summary>Group windows so the same quota from two sources is deduplicated.</summary>
        public static string BucketOf(QuotaWindow w)
        {
            if (w.Order <= 15) return "short";
            if (w.Order <= 25) return "long";
            var s = Squash(w.Key);
            return s.Length > 0 ? s : w.Label.ToLowerInvariant();
        }

        private static Tuple<string, int> CorrectByReset(string label, int order, DateTimeOffset? resetsAt)
        {
            if (resetsAt == null || order > 15) return Tuple.Create(label, order);
            var hours = (resetsAt.Value - Time.Now).TotalHours;
            if (hours <= 26) return Tuple.Create(label, order);
            if (hours <= 8 * 24) return Tuple.Create("7-day window", 20);
            return Tuple.Create("Long window", 20);
        }

        /// <summary>Turn {primary: {...}, secondary: {...}} into quota windows.</summary>
        public static List<QuotaWindow> WindowsFromRateLimits(JObj node, DateTimeOffset? baseTime)
        {
            var output = new List<QuotaWindow>();
            if (node == null) return output;
            var entries = new List<KeyValuePair<string, JObj>>();
            foreach (var kv in node)
                if (kv.Value is JObj sub && Extract.Used(sub) != null) entries.Add(new KeyValuePair<string, JObj>(kv.Key, sub));
            if (entries.Count == 0)
            {
                // One more level down: some payloads wrap the windows in a container.
                foreach (var kv in node)
                {
                    if (!(kv.Value is JObj sub)) continue;
                    foreach (var inner in sub)
                        if (inner.Value is JObj io && Extract.Used(io) != null)
                            entries.Add(new KeyValuePair<string, JObj>($"{kv.Key}_{inner.Key}", io));
                }
            }
            var scale = Extract.DecideScale(entries.Select(e => Extract.Used(e.Value)).Where(v => v != null).Select(v => v.Value));
            foreach (var e in entries)
            {
                var sub = e.Value;
                var used = Extract.Used(sub);
                if (used == null) continue;
                var fallback = LabelForKey(e.Key);
                var minutes = NonZero(sub.Num("window_minutes")) ?? NonZero(sub.Num("windowMinutes")) ?? NonZero(sub.Num("window_size_minutes"));
                var seconds = NonZero(sub.Num("window_seconds")) ?? NonZero(sub.Num("windowSeconds"))
                              ?? NonZero(sub.Num("window_size_seconds")) ?? NonZero(sub.Num("limit_window_seconds"))
                              ?? NonZero(sub.Num("windowDurationSeconds"));
                if (minutes == null && seconds != null && seconds > 0) minutes = seconds / 60.0;
                var lm = LabelFromMinutes(minutes, fallback.Item1);
                var resetsAt = Extract.Reset(sub, baseTime);
                var label = lm.Item1;
                var order = lm.Item2 != 0 ? lm.Item2 : fallback.Item2;
                if (minutes == null)
                {
                    // Time-to-reset is a hard lower bound on the window length.
                    var c = CorrectByReset(label, order, resetsAt);
                    label = c.Item1;
                    order = c.Item2;
                }
                var pct = Extract.ApplyScale(used, scale);
                output.Add(new QuotaWindow(e.Key, label, pct, resetsAt, null, order, pct != null && pct >= 99.9));
            }
            return output;
        }

        private static double? NonZero(double? v) => v == null || v.Value == 0 ? null : v;

        /// <summary>A rate_limits / rateLimits node anywhere in a nested structure.</summary>
        public static JObj FindRateLimits(object obj)
        {
            if (obj is JObj o)
            {
                foreach (var key in new[] { "rate_limits", "rateLimits", "rate_limit", "rateLimit" })
                    if (o[key] is JObj n && n.Count > 0) return n;
                foreach (var kv in o)
                {
                    var f = FindRateLimits(kv.Value);
                    if (f != null) return f;
                }
            }
            else if (obj is List<object> l)
            {
                foreach (var v in l)
                {
                    var f = FindRateLimits(v);
                    if (f != null) return f;
                }
            }
            return null;
        }

        /// <summary>Lines from the end of a file, reading at most maxBytes; oversized lines are skipped.</summary>
        public static IEnumerable<string> LinesReverse(string path, long maxBytes = 4000000, int maxLine = 512000, int chunk = 256 * 1024)
        {
            FileStream fh;
            long size;
            try
            {
                fh = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                size = fh.Length;
            }
            catch (Exception) { yield break; }
            using (fh)
            {
                var floor = Math.Max(0, size - maxBytes);
                var pos = size;
                var tail = new List<byte>();        // start of the line that continues into later blocks
                var skipping = false;
                while (pos > floor)
                {
                    var step = (int)Math.Min(chunk, pos - floor);
                    pos -= step;
                    var block = new byte[step];
                    try
                    {
                        fh.Seek(pos, SeekOrigin.Begin);
                        var read = 0;
                        while (read < step)
                        {
                            var n = fh.Read(block, read, step - read);
                            if (n <= 0) break;
                            read += n;
                        }
                    }
                    catch (Exception) { yield break; }
                    var all = new List<byte>(block.Length + tail.Count);
                    all.AddRange(block);
                    all.AddRange(tail);
                    var parts = Split(all);
                    tail = parts[0];
                    parts.RemoveAt(0);
                    if (parts.Count > 0 && skipping)
                    {
                        parts.RemoveAt(parts.Count - 1);     // the rest of the oversized line
                        skipping = false;
                    }
                    for (var i = parts.Count - 1; i >= 0; i--)
                    {
                        var line = parts[i];
                        if (line.Count <= maxLine && !IsBlank(line)) yield return Encoding.UTF8.GetString(line.ToArray());
                    }
                    if (tail.Count > maxLine)
                    {
                        tail = new List<byte>();
                        skipping = true;
                    }
                }
                // Only a line that starts at the beginning of the file is complete.
                if (pos == 0 && !IsBlank(tail) && !skipping) yield return Encoding.UTF8.GetString(tail.ToArray());
            }
        }

        private static List<List<byte>> Split(List<byte> data)
        {
            var parts = new List<List<byte>>();
            var start = 0;
            for (var i = 0; i < data.Count; i++)
            {
                if (data[i] != (byte)'\n') continue;
                parts.Add(data.GetRange(start, i - start));
                start = i + 1;
            }
            parts.Add(data.GetRange(start, data.Count - start));
            return parts;
        }

        private static bool IsBlank(List<byte> b) => b.All(x => x == ' ' || x == '\t' || x == '\r' || x == '\n');

        // ------------------------------------------------------------ plan, credits, resets

        /// <summary>Claims of a JWT, unverified (we only read our own login token).</summary>
        public static JObj JwtClaims(object token)
        {
            var s = token as string;
            if (s == null || s.Count(c => c == '.') < 2) return new JObj();
            var part = s.Split('.')[1].Replace('-', '+').Replace('_', '/');
            part += new string('=', (4 - part.Length % 4) % 4);
            try { return Json.ParseObject(Encoding.UTF8.GetString(Convert.FromBase64String(part))) ?? new JObj(); }
            catch (Exception) { return new JObj(); }
        }

        private sealed class Extras
        {
            public DateTime At;
            public JObj Resets, Subscription;
            public bool Complete;
            public List<string> Notes;
        }

        private static readonly Dictionary<string, Extras> ExtrasCache = new Dictionary<string, Extras>();

        public static void ClearExtras()
        {
            lock (ExtrasCache) ExtrasCache.Clear();
        }

        /// <summary>(reset credits, subscription, notes), cached an hour per account.</summary>
        private static Extras AccountExtras(Dictionary<string, string> headers, string accountId)
        {
            var key = accountId ?? (headers.TryGetValue("Authorization", out var a) ? a : "");
            if (key.Length > 40) key = key.Substring(key.Length - 40);
            lock (ExtrasCache)
            {
                if (ExtrasCache.TryGetValue(key, out var hit) &&
                    (DateTime.UtcNow - hit.At).TotalSeconds < (hit.Complete ? ExtrasTtl : ExtrasRetry))
                    return hit;
            }
            var notes = new List<string>();

            JObj Get(string name, string url)
            {
                // The official client sends the same headers as wham/usage; a
                // community workaround adds these two, so try that second.
                var status = 0;
                foreach (var extra in new[] { false, true })
                {
                    var h = new Dictionary<string, string>(headers);
                    if (extra)
                    {
                        h["OpenAI-Beta"] = "codex-1";
                        h["originator"] = "Codex Desktop";
                    }
                    HttpReply resp;
                    try { resp = Http.Get(url, h); }
                    catch (Exception e)
                    {
                        notes.Add($"{name}: {e.GetType().Name}");
                        return null;
                    }
                    if (resp.Status == 200)
                    {
                        if (!Json.TryParse(resp.Text, out var data))
                        {
                            notes.Add($"{name}: not JSON");
                            return null;
                        }
                        return data as JObj;
                    }
                    status = resp.Status;
                }
                notes.Add($"{name}: HTTP {status}");
                return null;
            }

            var resets = Get("reset list", ResetCreditsUrl);
            var subscription = accountId != null ? Get("subscription", Http.Query(SubscriptionsUrl, "account_id", accountId)) : new JObj();
            var result = new Extras
            {
                At = DateTime.UtcNow,
                Resets = resets ?? new JObj(),
                Subscription = subscription ?? new JObj(),
                Complete = resets != null && subscription != null,
                Notes = notes,
            };
            lock (ExtrasCache) ExtrasCache[key] = result;
            return result;
        }

        /// <summary>(plan, rows): plan, subscription end, credits, spend limit.</summary>
        public static Tuple<string, List<InfoRow>> AccountInfo(JObj usage, JObj subscription, JObj claims)
        {
            var auth = claims.Obj("https://api.openai.com/auth") ?? new JObj();
            var plan = Account.PrettyPlan(usage["plan_type"]) ?? Account.PrettyPlan(subscription["plan_type"])
                       ?? Account.PrettyPlan(auth["chatgpt_plan_type"]);
            var rows = new List<InfoRow>();
            if (plan != null) rows.Add(new InfoRow("Plan", plan));

            // The login token also carries when the active subscription started; it
            // may be the current billing period's start, so it is shown as "since".
            var started = Time.Parse(auth["chatgpt_subscription_active_start"]);
            var since = started != null && started <= Time.Now ? $"since {Account.FmtDate(started)} \u00b7 " : "";
            var until = Time.Parse(subscription["active_until"]);
            if (until != null)
            {
                var period = subscription.Str("billing_period");
                if (subscription.Truthy("is_delinquent"))
                    rows.Add(new InfoRow("Subscription", $"payment problem \u00b7 paid until {Account.FmtDate(until)}", "warn"));
                else if (subscription.Bool("will_renew") == false)
                    rows.Add(new InfoRow("Subscription", $"{since}ends {Account.FmtDate(until)} (won't renew)", Account.Soon(until, 7) ? "warn" : ""));
                else
                    rows.Add(new InfoRow("Subscription", $"{since}renews {Account.FmtDate(until)}" + (period != null ? $" ({period})" : "")));
            }
            else
            {
                var claimed = Time.Parse(auth["chatgpt_subscription_active_until"]);
                if (claimed != null)
                    rows.Add(new InfoRow("Subscription", $"{since}active until {Account.FmtDate(claimed)} (from the login token, may be stale)"));
            }

            var credits = usage.Obj("credits");
            if (credits != null && (credits.Truthy("has_credits") || credits.Truthy("unlimited")))
            {
                if (credits.Truthy("unlimited")) rows.Add(new InfoRow("Credits", "unlimited", "good"));
                else
                {
                    var balance = Json.ToFloat(credits["balance"]);
                    if (balance != null)
                    {
                        var text = $"{Account.N0(balance.Value)} left (~${Account.N2(balance.Value / CreditsPerDollar)})";
                        rows.Add(credits.Truthy("overage_limit_reached")
                            ? new InfoRow("Credits", text + " \u00b7 overage limit reached", "warn")
                            : new InfoRow("Credits", text));
                    }
                }
            }

            var spend = usage.Obj("spend_control");
            var limit = spend?.Obj("individual_limit");
            if (limit != null)
            {
                var used = Json.ToFloat(limit["used"]);
                var cap = Json.ToFloat(limit["limit"]);
                if (used != null && cap != null && cap.Value != 0)
                {
                    var unit = limit.Text("unit") ?? "credit";
                    var reset = Time.Parse(limit["reset_at"]);
                    rows.Add(new InfoRow("Spend limit",
                        $"{Account.N0(used.Value)} of {Account.N0(cap.Value)} {unit}s" + (reset != null ? $", resets {Account.FmtDate(reset)}" : ""),
                        spend.Truthy("reached") ? "warn" : ""));
                }
            }
            return Tuple.Create(plan, rows);
        }

        private static int? Count(object v)
        {
            if (v is bool) return null;
            var n = Json.AsNumber(v);
            if (n != null) return n.Value == Math.Floor(n.Value) ? (int)n.Value : (int?)null;
            if (v is string s && s.Trim().Length > 0 && s.Trim().All(char.IsDigit)) return int.Parse(s.Trim());
            return null;
        }

        /// <summary>Available resets per the list endpoint, else the usage summary.</summary>
        public static int? ResetCount(JObj usage, JObj resetPayload) =>
            Count(resetPayload["available_count"]) ?? Count(usage.Obj("rate_limit_reset_credits")?["available_count"]);

        /// <summary>Banked rate-limit resets, read the way the official client does.</summary>
        public static List<ResetGrant> Resets(JObj usage, JObj resetPayload)
        {
            var grants = new List<ResetGrant>();
            foreach (var credit in (resetPayload.Arr("credits") ?? new List<object>()).OfType<JObj>())
            {
                var status = credit["status"];
                if (status != null && !string.Equals(Convert.ToString(status, System.Globalization.CultureInfo.InvariantCulture), "available", StringComparison.OrdinalIgnoreCase))
                    continue;
                if (credit.Truthy("redeemed_at") || credit.Truthy("redeem_started_at")) continue;
                var note = credit.Text("title") ?? "resets Codex limits";
                if (credit.Truthy("description")) note = $"{note} ({credit["description"]})";
                grants.Add(new ResetGrant(1, Time.Parse(credit["expires_at"]), note));
            }
            var count = ResetCount(usage, resetPayload);
            var listed = grants.Count;
            if (count != null && count > listed) grants.Add(new ResetGrant(count.Value - listed, null, "resets Codex limits"));
            if (count != null && count < listed)
                grants = grants.OrderBy(g => g.ExpiresAt ?? Time.Now).Take(count.Value).ToList();
            return grants;
        }

        /// <summary>[(CODEX_HOME, label)]: the default home first, then others with a login.</summary>
        public static List<KeyValuePair<string, string>> CodexHomes(JObj settings)
        {
            var def = CodexHome(settings);
            var homes = new List<KeyValuePair<string, string>> { new KeyValuePair<string, string>(def, "Codex") };
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { Norm(def) };

            void Add(string home, string label)
            {
                try
                {
                    if (seen.Contains(Norm(home)) || !File.Exists(Path.Combine(home, "auth.json"))) return;
                }
                catch (Exception) { return; }
                seen.Add(Norm(home));
                homes.Add(new KeyValuePair<string, string>(home, label));
            }

            Add(Path.Combine(Paths.Home, ".codex"), "Codex - ~/.codex");
            if (Core.Settings.Flag(settings, "scan_wsl", true))
            {
                foreach (var distro in Proc.WslDistros())
                {
                    var root = $@"\\wsl.localhost\{distro}";
                    Add(Path.Combine(root, "root", ".codex"), $"Codex - WSL {distro}");
                    string[] users;
                    try
                    {
                        var home = Path.Combine(root, "home");
                        users = Directory.Exists(home) ? Directory.GetDirectories(home).Take(10).ToArray() : new string[0];
                    }
                    catch (Exception) { users = new string[0]; }
                    foreach (var u in users) Add(Path.Combine(u, ".codex"), $"Codex - WSL {distro}");
                }
            }
            return homes;
        }

        private static string Norm(string p) => p.TrimEnd('\\', '/');

        // ------------------------------------------------------------ provider

        public override bool Detect() => Proc.SafeDirExists(CodexHome(Settings)) || FindCodexExe() != null;

        private Tuple<JObj, string> AuthTokens()
        {
            if (AuthTokensOverride != null) return AuthTokensOverride();
            var home = CodexHome(Settings);
            var candidates = new List<string> { Path.Combine(home, "auth.json") };
            var secrets = Path.Combine(home, "secrets");
            try
            {
                if (Directory.Exists(secrets))
                    candidates.AddRange(Directory.GetFiles(secrets, "*.json").OrderBy(p => p, StringComparer.Ordinal).Take(5));
            }
            catch (Exception) { }
            foreach (var path in candidates)
            {
                try
                {
                    if (!File.Exists(path)) continue;
                    var _t = Io.ReadAllTextShared(path);
                    var tokens = _t != null ? Json.ParseObject(_t)?.Obj("tokens") : null;
                    if (tokens != null && tokens.Truthy("access_token")) return Tuple.Create(tokens, path);
                }
                catch (Exception) { }
            }
            return Tuple.Create<JObj, string>(null, "");
        }

        /// <summary>When the current login was written; older local data may be another account's.</summary>
        private DateTimeOffset? LoginTime()
        {
            var origin = AuthTokens().Item2;
            try
            {
                return !string.IsNullOrEmpty(origin) && File.Exists(origin)
                    ? new DateTimeOffset(File.GetLastWriteTimeUtc(origin), TimeSpan.Zero)
                    : (DateTimeOffset?)null;
            }
            catch (Exception) { return null; }
        }

        private bool TryWham(ProviderResult result)
        {
            const string tag = "ChatGPT usage API";
            var auth = AuthTokens();
            var tokens = auth.Item1;
            if (tokens == null)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, $"no access_token in {CodexHome(Settings)}/auth.json"));
                return false;
            }
            var headers = new Dictionary<string, string>
            {
                { "Authorization", "Bearer " + tokens.Str("access_token") },
                { "Accept", "application/json" },
                { "User-Agent", "codex_cli_rs/0.0.0 (quota-tray)" },
            };
            var accountId = tokens["account_id"] != null ? Convert.ToString(tokens["account_id"], System.Globalization.CultureInfo.InvariantCulture) : null;
            if (!string.IsNullOrEmpty(accountId)) headers["chatgpt-account-id"] = accountId;
            else accountId = null;
            HttpReply resp;
            try { resp = Http.Get(WhamUrl, headers); }
            catch (Exception e)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "request failed: " + e.Message));
                return false;
            }
            if (resp.Status >= 400)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, $"HTTP {resp.Status} (token may be expired; run codex once to refresh)"));
                return false;
            }
            if (!Json.TryParse(resp.Text, out var payloadObj))
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "response was not JSON"));
                return false;
            }
            var payload = payloadObj as JObj ?? new JObj();
            var windows = WindowsFromRateLimits(FindRateLimits(payloadObj) ?? new JObj(), Time.Now);
            if (windows.Count == 0)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "no rate limits in response: " + Short(Json.Write(payloadObj), 160)));
                return false;
            }
            var claims = JwtClaims(tokens["id_token"]);
            result.Windows = windows;
            result.Ok = true;
            result.Source = "chatgpt.com/wham/usage";
            result.Plan = PlanOf(payload);
            result.Account = claims.Str("email");
            result.Status = "connected";
            result.DataTime = Time.Now;
            result.Attempts.Add(new SourceAttempt(tag, true, auth.Item2));

            // Each detail on its own: one failing must not hide the others.
            var resetsPayload = new JObj();
            var subscription = new JObj();
            var notes = new List<string>();
            try
            {
                var extras = AccountExtras(headers, accountId);
                resetsPayload = extras.Resets;
                subscription = extras.Subscription;
                notes = extras.Notes.ToList();
            }
            catch (Exception e) { notes = new List<string> { "account details: " + e.Message }; }
            try
            {
                var info = AccountInfo(payload, subscription, claims);
                result.Plan = info.Item1 ?? result.Plan;
                result.Info = info.Item2;
            }
            catch (Exception e) { notes.Add("plan/credits: " + e.Message); }
            try
            {
                result.Resets = Resets(payload, resetsPayload);
                var count = ResetCount(payload, resetsPayload);
                if (result.Resets.Count == 0)
                {
                    // Always say something, so a missing line is never a mystery.
                    if (count == 0) result.Info.Add(new InfoRow("Resets", "none available"));
                    else
                    {
                        var why = string.Join("; ", notes.Where(n => n.StartsWith("reset", StringComparison.Ordinal)));
                        result.Info.Add(new InfoRow("Resets", $"unknown ({(why.Length > 0 ? why : "not reported")})", "warn"));
                    }
                }
            }
            catch (Exception e) { notes.Add("resets: " + e.Message); }
            if (notes.Count > 0) result.Attempts.Add(new SourceAttempt("Codex account details", false, string.Join("; ", notes)));
            return true;
        }

        private bool TryAppServer(ProviderResult result)
        {
            const string tag = "codex app-server";
            var exe = FindCodexExe();
            if (exe == null)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "no codex executable on PATH"));
                return false;
            }
            object payload;
            try { payload = AppServerRateLimits(exe, (int)Core.Settings.Num(Settings, "app_server_timeout", 20)); }
            catch (Exception e)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, e.Message));
                return false;
            }
            var node = FindRateLimits(payload) ?? payload as JObj;
            var windows = WindowsFromRateLimits(node ?? new JObj(), Time.Now);
            if (windows.Count == 0)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "no quota parsed: " + Short(Json.Write(payload), 160)));
                return false;
            }
            result.Windows = windows;
            result.Ok = true;
            result.Source = $"codex app-server ({Path.GetFileName(exe)})";
            result.Status = "connected";
            result.DataTime = Time.Now;
            result.Attempts.Add(new SourceAttempt(tag, true, exe));
            return true;
        }

        private static object AppServerRateLimits(string exe, int timeout)
        {
            var isScript = exe.EndsWith(".cmd", StringComparison.OrdinalIgnoreCase) || exe.EndsWith(".bat", StringComparison.OrdinalIgnoreCase);
            var psi = new ProcessStartInfo(isScript ? "cmd.exe" : exe, isScript ? $"/c \"\"{exe}\" app-server\"" : "app-server")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
            };
            using (var p = Process.Start(psi))
            {
                object result = null;
                string error = null;
                var done = new ManualResetEventSlim(false);
                void Send(string json)
                {
                    try
                    {
                        p.StandardInput.Write(json + "\n");
                        p.StandardInput.Flush();
                    }
                    catch (Exception) { }
                }
                var reader = new Thread(() =>
                {
                    try
                    {
                        string line;
                        while ((line = p.StandardOutput.ReadLine()) != null)
                        {
                            var msg = Json.ParseObject(line.Trim());
                            if (msg == null) continue;
                            var id = msg.Num("id");
                            if (id == 1)
                            {
                                if (msg.Has("error")) error = Short(Json.Write(msg["error"]), 200);
                                else result = msg["result"] ?? new JObj();
                                done.Set();
                                return;
                            }
                            if (id == 0 && msg.Has("result"))
                            {
                                Send("{\"jsonrpc\":\"2.0\",\"method\":\"initialized\",\"params\":{}}");
                                Send("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"account/rateLimits/read\",\"params\":{}}");
                            }
                        }
                    }
                    catch (Exception) { }
                }) { IsBackground = true };
                reader.Start();
                Send("{\"jsonrpc\":\"2.0\",\"id\":0,\"method\":\"initialize\",\"params\":{\"clientInfo\":{\"name\":\"quota-tray\",\"title\":\"QuotaTray\",\"version\":\"1.0.0\"}}}");
                done.Wait(TimeSpan.FromSeconds(timeout));
                try { p.Kill(); } catch (Exception) { }
                if (result != null) return result;
                if (error != null) throw new InvalidOperationException("app-server error: " + error);
                throw new TimeoutException($"no response within {timeout}s (you may need to run `codex login`)");
            }
        }

        private bool TryJsonl(ProviderResult result)
        {
            const string tag = "session logs";
            var sessions = Path.Combine(CodexHome(Settings), "sessions");
            if (!Proc.SafeDirExists(sessions))
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "no " + sessions));
                return false;
            }
            List<FileInfo> files;
            try
            {
                files = new DirectoryInfo(sessions).EnumerateFiles("*.jsonl", SearchOption.AllDirectories)
                    .OrderByDescending(f => f.LastWriteTimeUtc).Take(30).ToList();
            }
            catch (Exception e)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "walk failed: " + e.Message));
                return false;
            }
            if (files.Count == 0)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, "no rollout logs"));
                return false;
            }
            var cutoff = Time.Now.AddDays(-Core.Settings.Num(Settings, "jsonl_max_days", 14));
            var login = LoginTime();
            // Logs from before the current login may belong to another account.
            if (login != null && login > cutoff) cutoff = login.Value;
            foreach (var file in files)
            {
                foreach (var line in LinesReverse(file.FullName))
                {
                    if (!line.Contains("rate_limit")) continue;
                    var record = Json.ParseObject(line);
                    if (record == null) continue;
                    var node = FindRateLimits(record);
                    if (node == null) continue;
                    var stamp = Time.Parse(record["timestamp"] ?? record["ts"])
                                ?? new DateTimeOffset(file.LastWriteTimeUtc, TimeSpan.Zero);
                    if (stamp < cutoff) continue;
                    var windows = WindowsFromRateLimits(node, stamp);
                    if (windows.Count == 0) continue;
                    result.Windows = windows;
                    result.Ok = true;
                    result.Source = "session log " + file.Name;
                    result.DataTime = stamp;
                    result.Status = "connected (offline snapshot)";
                    result.Attempts.Add(new SourceAttempt(tag, true, file.FullName));
                    return true;
                }
            }
            var why = $"scanned {files.Count} logs, no rate_limits";
            if (login != null && cutoff == login.Value)
                why += $" newer than the current Codex login ({login.Value.ToLocalTime():yyyy-MM-dd HH:mm})";
            result.Attempts.Add(new SourceAttempt(tag, false, why));
            return false;
        }

        /// <summary>One page per Codex login (Windows ~/.codex, CODEX_HOME, each WSL distro).</summary>
        public override void Collect(ProviderResult result)
        {
            var homes = CodexHomes(Settings);
            var pages = new List<ProviderResult>();
            ProviderResult first = null;
            for (var i = 0; i < homes.Count; i++)
            {
                var page = new ProviderResult(Id, Name) { FetchedAt = result.FetchedAt, Installed = result.Installed };
                var settings = Config.Merge(Settings, new JObj());
                settings["codex_home"] = homes[i].Key;
                var sub = new CodexProvider(Config, settings) { AuthTokensOverride = AuthTokensOverride };
                // codex app-server always uses its own default home.
                sub.CollectHome(page, i == 0);
                page.Label = homes[i].Value;
                if (homes.Count > 1)
                    foreach (var a in page.Attempts) a.Name = $"[{homes[i].Value}] {a.Name}";
                result.Attempts.AddRange(page.Attempts);
                first = first ?? page;
                if (page.Ok) pages.Add(page);
            }
            if (pages.Count > 0)
            {
                pages = Account.GroupPages(pages);
                result.Adopt(pages[0]);
                result.Alternates = pages.Skip(1).ToList();
            }
            else if (first != null) result.Status = first.Status;
        }

        /// <summary>Walk the sources and merge their windows until a short and a long one are covered.</summary>
        private void CollectHome(ProviderResult result, bool allowAppServer)
        {
            var steps = new Dictionary<string, Func<ProviderResult, bool>>
            {
                { "wham", TryWham },
                { "app_server", TryAppServer },
                { "jsonl", TryJsonl },
            };
            var order = Core.Settings.List(Settings, "order");
            if (order.Count == 0) order = new List<string> { "wham", "app_server", "jsonl", "sqlite" };

            var merged = new List<QuotaWindow>();
            var buckets = new HashSet<string>();
            var sources = new List<string>();
            var stale = false;
            DateTimeOffset? oldest = null;
            var liveOk = false;

            foreach (var name in order)
            {
                if (!steps.TryGetValue(name, out var fn) || (name == "app_server" && !allowAppServer)) continue;
                // A live answer is complete for the signed-in account; topping it
                // up from local logs mixed in other accounts' data.
                if (liveOk && SnapshotSources.Contains(name)) continue;
                var probe = new ProviderResult(Id, Name) { FetchedAt = Time.Now };
                bool ok;
                try { ok = fn(probe); }
                catch (Exception e)
                {
                    Log.Warn($"codex source {name} failed: {e}");
                    probe.Attempts.Add(new SourceAttempt(name, false, "crashed: " + e.Message));
                    ok = false;
                }
                result.Attempts.AddRange(probe.Attempts);
                if (!ok) continue;
                if (!SnapshotSources.Contains(name)) liveOk = true;
                if (probe.Account != null && result.Account == null) result.Account = probe.Account;
                var added = false;
                foreach (var w in probe.Windows)
                {
                    if (!buckets.Add(BucketOf(w))) continue;
                    merged.Add(w);
                    added = true;
                }
                if (probe.Info.Count > 0 && result.Info.Count == 0) result.Info = probe.Info;
                if (probe.Resets.Count > 0 && result.Resets.Count == 0) result.Resets = probe.Resets;
                if (added)
                {
                    sources.Add(probe.Source ?? name);
                    if (probe.Plan != null && result.Plan == null) result.Plan = probe.Plan;
                    if ((probe.Status ?? "").Contains("snapshot")) stale = true;
                    if (probe.DataTime != null && (oldest == null || probe.DataTime < oldest)) oldest = probe.DataTime;
                }
                if (buckets.Contains("short") && buckets.Contains("long")) break;
            }
            if (merged.Count > 0)
            {
                result.Windows = merged;
                result.Ok = true;
                result.Source = string.Join(" + ", sources.Distinct());
                result.DataTime = oldest;
                result.Status = stale ? "connected (partly from an offline snapshot)" : "connected";
                return;
            }
            result.Status = !result.Installed ? "Codex not detected" : "no usable quota source - see Diagnostics";
        }

        private static string PlanOf(JObj payload)
        {
            foreach (var key in new[] { "plan", "plan_type", "planType", "subscription", "tier" })
            {
                if (payload.Text(key) != null) return payload.Text(key);
                var o = payload.Obj(key);
                if (o == null) continue;
                foreach (var f in new[] { "name", "type", "id" })
                    if (o.Str(f) != null) return o.Str(f);
            }
            return null;
        }
    }
}
