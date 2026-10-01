using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using QuotaTray.Core;
using QuotaTray.Win;

namespace QuotaTray.Providers
{
    /// <summary>
    /// Gemini CLI quota. Google OAuth tokens live in ~/.gemini/oauth_creds.json;
    /// the per-model quota comes from Google's Code Assist API (v1internal:
    /// loadCodeAssist for the tier, retrieveUserQuota for the buckets). Tokens
    /// are refreshed when expired and only ever sent to Google.
    /// </summary>
    public sealed class GeminiProvider : Provider
    {
        public override string Id => "gemini";
        public override string Name => "Gemini CLI";

        public const string Endpoint = "https://cloudcode-pa.googleapis.com/v1internal";
        private const string LoadMeta = "{\"ideType\":\"IDE_UNSPECIFIED\",\"platform\":\"PLATFORM_UNSPECIFIED\",\"pluginType\":\"GEMINI\"}";

        private static readonly Dictionary<string, string> TierNames = new Dictionary<string, string>
        {
            { "free-tier", "Free" }, { "legacy-tier", "Legacy" }, { "standard-tier", "Standard" },
            { "enterprise-tier", "Enterprise" },
        };

        /// <summary>Tests replace credential discovery.</summary>
        public Func<Tuple<List<KeyValuePair<JObj, string>>, List<string>>> DiscoverOverride;

        public GeminiProvider(Config config) : base(config) { }

        // ------------------------------------------------------------ credentials

        private List<KeyValuePair<string, string>> GeminiDirs()
        {
            var output = new List<KeyValuePair<string, string>>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            void Add(string path, string suffix)
            {
                if (seen.Add(path.ToLowerInvariant())) output.Add(new KeyValuePair<string, string>(path, suffix));
            }
            var envHome = Core.Settings.Str(Settings, "gemini_home");
            if (envHome.Length == 0) envHome = (Environment.GetEnvironmentVariable("GEMINI_CLI_HOME") ?? "").Trim();
            if (envHome.Length > 0) Add(Path.Combine(envHome, ".gemini"), "");
            Add(Path.Combine(Paths.Home, ".gemini"), "");
            if (Core.Settings.Flag(Settings, "scan_wsl", true))
                foreach (var distro in Proc.WslDistros())
                {
                    var root = $@"\\wsl.localhost\{distro}";
                    Add(Path.Combine(root, "root", ".gemini"), $" - WSL {distro}");
                    string[] users;
                    try
                    {
                        var home = Path.Combine(root, "home");
                        users = Directory.Exists(home) ? Directory.GetDirectories(home).Take(10).ToArray() : new string[0];
                    }
                    catch (Exception) { users = new string[0]; }
                    foreach (var u in users) Add(Path.Combine(u, ".gemini"), $" - WSL {distro}");
                }
            return output;
        }

        public Tuple<List<KeyValuePair<JObj, string>>, List<string>> DiscoverCredentials()
        {
            if (DiscoverOverride != null) return DiscoverOverride();
            var creds = new List<KeyValuePair<JObj, string>>();
            var notes = new List<string>();
            var checkedCount = 0;
            foreach (var dir in GeminiDirs())
            {
                var path = Path.Combine(dir.Key, "oauth_creds.json");
                if (!Proc.SafeFileExists(path)) continue;
                checkedCount++;
                JObj data;
                try { var _t = Io.ReadAllTextShared(path); data = _t != null ? Json.ParseObject(_t) : null; }
                catch (Exception) { data = null; }
                if (data == null || (data.Str("access_token") == null && data.Str("refresh_token") == null))
                {
                    notes.Add($"{path}: no OAuth tokens in it (run gemini once and sign in)");
                    continue;
                }
                creds.Add(new KeyValuePair<JObj, string>(data, "Gemini CLI" + dir.Value));
            }
            if (creds.Count == 0 && checkedCount == 0) notes.Add("no ~/.gemini/oauth_creds.json (sign in with `gemini`)");
            return Tuple.Create(creds, notes);
        }

        private static string JwtEmail(object token)
        {
            var s = token as string;
            if (s == null || s.Count(c => c == '.') < 2) return null;
            var part = s.Split('.')[1].Replace('-', '+').Replace('_', '/');
            part += new string('=', (4 - part.Length % 4) % 4);
            try { return Json.ParseObject(Encoding.UTF8.GetString(Convert.FromBase64String(part)))?.Str("email"); }
            catch (Exception) { return null; }
        }

        // ------------------------------------------------------------ tokens

        /// <summary>
        /// The access token on disk. QuotaTray does not refresh it itself (that
        /// would need Gemini CLI's OAuth client secret, which is not ours to
        /// embed): the CLI refreshes it whenever it runs, so the file is usually
        /// current. A clearly expired token is reported instead.
        /// </summary>
        public static Tuple<string, string> AccessToken(JObj creds)
        {
            var token = creds.Str("access_token");
            var expiry = creds.Num("expiry_date");                 // ms epoch
            var now = Time.Now.ToUnixTimeSeconds();
            if (!string.IsNullOrEmpty(token))
            {
                var expired = expiry != null && expiry.Value / 1000.0 < now - 60;
                if (expired) return Tuple.Create((string)null, "the saved token has expired; run `gemini` once to refresh it");
                return Tuple.Create(token, (string)null);
            }
            return Tuple.Create((string)null, "no access token in oauth_creds.json (sign in with `gemini`)");
        }

        // ------------------------------------------------------------ parsing

        public static List<QuotaWindow> QuotaWindows(JObj payload)
        {
            var output = new List<QuotaWindow>();
            var order = 10;
            foreach (var bucket in (payload.Arr("buckets") ?? new List<object>()).OfType<JObj>())
            {
                var model = bucket.Str("modelId");
                var frac = bucket.Num("remainingFraction");
                if (string.IsNullOrEmpty(model) || frac == null) continue;
                var pct = Math.Max(0.0, Math.Min(100.0, (1.0 - frac.Value) * 100.0));
                string detail = null;
                var amount = bucket.Str("remainingAmount");
                if (!string.IsNullOrEmpty(amount))
                {
                    var unit = bucket.Str("tokenType");
                    detail = amount + " left" + (!string.IsNullOrEmpty(unit) ? $" ({unit.ToLowerInvariant()})" : "");
                }
                output.Add(new QuotaWindow(model, ModelLabel(model), pct, Time.Parse(bucket["resetTime"]), detail, order++, frac.Value <= 0));
            }
            return output;
        }

        public static string ModelLabel(string model)
        {
            var words = model.Replace("-", " ").Replace("_", " ").Trim().Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(w => w.Any(char.IsDigit) ? w : Account.Capitalize(w));
            var name = string.Join(" ", words);
            return name.Length > 0 ? name : model;
        }

        public static Tuple<string, List<InfoRow>> TierInfo(JObj load)
        {
            var rows = new List<InfoRow>();
            var tier = load.Obj("currentTier") ?? load.Obj("paidTier");
            if (tier == null) return Tuple.Create((string)null, rows);
            var tid = tier.Str("id");
            string plan = null;
            if (tid != null && TierNames.TryGetValue(tid, out var known)) plan = known;
            plan = plan ?? Account.PrettyPlan(tier.Str("name")) ?? Account.PrettyPlan(tid);
            foreach (var credit in (tier.Arr("availableCredits") ?? new List<object>()).OfType<JObj>())
            {
                var amount = credit["amount"] ?? credit["balance"];
                if (amount == null) continue;
                rows.Add(new InfoRow(credit.Str("name") ?? "Credits", Convert.ToString(amount, CultureInfo.InvariantCulture)));
            }
            return Tuple.Create(plan, rows);
        }

        // ------------------------------------------------------------ provider

        public override bool Detect() => DiscoverCredentials().Item1.Count > 0;

        private HttpReply Post(string method, string token, string body)
        {
            var req = new HttpRequest { Method = "POST", Url = $"{Endpoint}:{method}", Body = body, TimeoutSeconds = 25 };
            req.Headers["Authorization"] = "Bearer " + token;
            req.Headers["Content-Type"] = "application/json";
            req.Headers["Accept"] = "application/json";
            return Http.Send(req);
        }

        private ProviderResult Page(JObj creds, string where, DateTimeOffset? fetchedAt)
        {
            var page = new ProviderResult(Id, Name) { FetchedAt = fetchedAt, Label = where };
            var tag = $"Code Assist API ({where})";
            var tokenResult = AccessToken(creds);
            if (tokenResult.Item1 == null)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, tokenResult.Item2 ?? "no access token"));
                return page;
            }
            var token = tokenResult.Item1;
            HttpReply loadResp;
            try { loadResp = Post("loadCodeAssist", token, "{\"metadata\":" + LoadMeta + "}"); }
            catch (Exception e)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, "loadCodeAssist failed: " + e.Message));
                return page;
            }
            if (loadResp.Status == 401 || loadResp.Status == 403)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, $"token rejected (HTTP {loadResp.Status}); sign in to Gemini again"));
                return page;
            }
            var load = (loadResp.Status < 400 ? Json.ParseObject(loadResp.Text) : null) ?? new JObj();
            var project = load.Str("cloudaicompanionProject");
            var tier = TierInfo(load);

            var body = string.IsNullOrEmpty(project) ? "{\"metadata\":" + LoadMeta + "}" : "{\"project\":" + Json.Write(project) + "}";
            HttpReply quotaResp;
            try { quotaResp = Post("retrieveUserQuota", token, body); }
            catch (Exception e)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, "retrieveUserQuota failed: " + e.Message));
                return page;
            }
            if (quotaResp.Status >= 400)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, $"retrieveUserQuota HTTP {quotaResp.Status}: {Short(quotaResp.Text, 160)}"));
                return page;
            }
            var quota = Json.ParseObject(quotaResp.Text);
            if (quota == null)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, "quota response was not JSON"));
                return page;
            }
            var windows = QuotaWindows(quota);
            if (windows.Count == 0 && tier.Item1 == null)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, "no quota in response: " + Short(quotaResp.Text, 160)));
                return page;
            }
            page.Windows = windows;
            page.Ok = true;
            page.Plan = tier.Item1;
            page.Info = tier.Item2;
            page.Account = JwtEmail(creds["id_token"]);
            page.Source = "cloudcode-pa.googleapis.com";
            page.Status = "connected";
            page.DataTime = Time.Now;
            page.Attempts.Add(new SourceAttempt(tag, true, where));
            return page;
        }

        public override void Collect(ProviderResult result)
        {
            var found = DiscoverCredentials();
            if (found.Item1.Count == 0)
            {
                result.Attempts.Add(new SourceAttempt("credentials", false,
                    found.Item2.Count > 0 ? string.Join("; ", found.Item2) : "no Gemini CLI login"));
                result.Status = !result.Installed ? "Gemini CLI not detected" : "no Gemini CLI login found";
                return;
            }
            foreach (var note in found.Item2) result.Attempts.Add(new SourceAttempt("credentials", false, note));
            var pages = new List<ProviderResult>();
            foreach (var c in found.Item1)
            {
                var page = Page(c.Key, c.Value, result.FetchedAt);
                result.Attempts.AddRange(page.Attempts);
                if (page.Ok) pages.Add(page);
            }
            if (pages.Count > 0)
            {
                pages = Account.GroupPages(pages);
                result.Adopt(pages[0]);
                result.Alternates = pages.Skip(1).ToList();
                return;
            }
            var failed = result.Attempts.Where(a => !a.Ok && a.Name.StartsWith("Code Assist")).ToList();
            result.Status = failed.Count > 0 ? failed.Last().Detail : "could not read Gemini quota";
        }
    }
}
