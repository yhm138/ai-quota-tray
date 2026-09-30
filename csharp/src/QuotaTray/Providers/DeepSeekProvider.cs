using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using QuotaTray.Core;
using QuotaTray.Win;

namespace QuotaTray.Providers
{
    /// <summary>
    /// DeepSeek API balance (pay as you go). The key comes from the tools that
    /// already hold it: OpenCode (~/.local/share/opencode/auth.json, opencode.json),
    /// DeepSeek Harness (~/.dsh/.credentials.yaml, ~/.dsh/.env), DEEPSEEK_API_KEY,
    /// or api_key in config.json; Windows and each WSL distro. A key is only ever
    /// sent to api.deepseek.com.
    /// </summary>
    public sealed class DeepSeekProvider : Provider
    {
        public override string Id => "deepseek";
        public override string Name => "DeepSeek";
        public override string Billing => "payg";

        public const string BalanceUrl = "https://api.deepseek.com/user/balance";
        public const string EnvName = "DEEPSEEK_API_KEY";

        /// <summary>Tests replace the folders searched: [(home, label suffix)].</summary>
        public static Func<JObj, List<KeyValuePair<string, string>>> HomesOverride;

        /// <summary>Tests replace the environment.</summary>
        public static Func<string, string> Env = Environment.GetEnvironmentVariable;

        public DeepSeekProvider(Config config) : base(config) { }

        // ------------------------------------------------------------ file formats

        /// <summary>JSON with // and /* */ comments and trailing commas (opencode.json) -> JSON.</summary>
        public static string StripJsonc(string text)
        {
            var sb = new StringBuilder(text.Length);
            var inStr = false;
            for (var i = 0; i < text.Length; i++)
            {
                var c = text[i];
                if (inStr)
                {
                    sb.Append(c);
                    if (c == '\\' && i + 1 < text.Length) sb.Append(text[++i]);
                    else if (c == '"') inStr = false;
                    continue;
                }
                if (c == '"')
                {
                    inStr = true;
                    sb.Append(c);
                }
                else if (c == '/' && i + 1 < text.Length && text[i + 1] == '/')
                {
                    while (i < text.Length && text[i] != '\n' && text[i] != '\r') i++;
                    i--;
                }
                else if (c == '/' && i + 1 < text.Length && text[i + 1] == '*')
                {
                    var end = text.IndexOf("*/", i + 2, StringComparison.Ordinal);
                    i = end < 0 ? text.Length : end + 1;
                }
                else sb.Append(c);
            }
            return Regex.Replace(sb.ToString(), @",(\s*[}\]])", "$1");
        }

        private static string Unquote(string value)
        {
            value = value.Trim();
            if (value.StartsWith("'") || value.StartsWith("\""))
            {
                var end = value.IndexOf(value[0], 1);
                return end > 0 ? value.Substring(1, end - 1) : value.Substring(1);
            }
            // An unquoted YAML / dotenv value ends at a comment.
            return Regex.Split(value, @"\s+#")[0].Trim();
        }

        /// <summary>refs.DEEPSEEK_API_KEY from a DeepSeek Harness .credentials.yaml (or the flat layout).</summary>
        public static string DshCredentialsKey(string text)
        {
            string section = null;
            foreach (var raw in text.Replace("\r", "").Split('\n'))
            {
                var line = raw.TrimEnd();
                if (line.Trim().Length == 0 || line.TrimStart().StartsWith("#")) continue;
                var indent = line.Length - line.TrimStart().Length;
                var m = Regex.Match(line, @"^\s*([A-Za-z0-9_./-]+)\s*:\s*(.*)$");
                if (!m.Success) continue;
                var key = m.Groups[1].Value;
                var value = m.Groups[2].Value;
                if (indent == 0)
                {
                    section = value.Trim().Length == 0 ? key : null;
                    if (key == EnvName && value.Trim().Length > 0) return NullIfEmpty(Unquote(value));
                    continue;
                }
                if (section == "refs" && key == EnvName && value.Trim() != "" && value.Trim() != "|" && value.Trim() != ">")
                    return NullIfEmpty(Unquote(value));
            }
            return null;
        }

        public static string DotenvKey(string text, string name = EnvName)
        {
            foreach (var line in text.Replace("\r", "").Split('\n'))
            {
                var m = Regex.Match(line, @"^\s*(?:export\s+)?" + Regex.Escape(name) + @"\s*=\s*(.*)$");
                if (m.Success) return NullIfEmpty(Unquote(m.Groups[1].Value));
            }
            return null;
        }

        private static string NullIfEmpty(string s) => string.IsNullOrEmpty(s) ? null : s;

        private static bool IsOfficial(string baseUrl) =>
            string.IsNullOrWhiteSpace(baseUrl) || Regex.IsMatch(baseUrl.Trim(), @"^https?://api\.deepseek\.com(?:[:/]|$)", RegexOptions.IgnoreCase);

        /// <summary>API keys stored for DeepSeek providers in OpenCode's auth.json.</summary>
        public static List<string> OpencodeAuthKeys(JObj data)
        {
            var keys = new List<string>();
            if (data == null) return keys;
            foreach (var kv in data)
            {
                if (!kv.Key.ToLowerInvariant().Contains("deepseek") || !(kv.Value is JObj entry)) continue;
                if ((entry.Str("type") ?? "api") != "api") continue;
                var key = entry.Str("key");
                if (!string.IsNullOrWhiteSpace(key)) keys.Add(key.Trim());
            }
            return keys;
        }

        /// <summary>provider.&lt;deepseek&gt;.options.apiKey from opencode.json; relays are skipped.</summary>
        public static List<string> OpencodeConfigKeys(JObj data, Func<string, string> env)
        {
            var keys = new List<string>();
            var providers = data?.Obj("provider");
            if (providers == null) return keys;
            foreach (var kv in providers)
            {
                if (!(kv.Value is JObj entry)) continue;
                var options = entry.Obj("options") ?? new JObj();
                var baseUrl = options.Str("baseURL") ?? options.Str("baseUrl");
                var isDeepseek = kv.Key.ToLowerInvariant().Contains("deepseek") || (baseUrl ?? "").ToLowerInvariant().Contains("deepseek.com");
                if (!isDeepseek || !IsOfficial(baseUrl)) continue;
                var key = options.Str("apiKey");
                if (key == null) continue;
                var m = Regex.Match(key.Trim(), @"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$");
                if (m.Success) key = env(m.Groups[1].Value) ?? "";
                key = key.Trim();
                if (key.Length > 0 && !key.StartsWith("{")) keys.Add(key);
            }
            return keys;
        }

        // ------------------------------------------------------------ discovery

        private static List<KeyValuePair<string, string>> Homes(JObj settings)
        {
            if (HomesOverride != null) return HomesOverride(settings);
            var homes = new List<KeyValuePair<string, string>> { new KeyValuePair<string, string>(Paths.Home, "") };
            if (!Core.Settings.Flag(settings, "scan_wsl", true)) return homes;
            foreach (var distro in Proc.WslDistros())
            {
                var root = $@"\\wsl.localhost\{distro}";
                homes.Add(new KeyValuePair<string, string>(Path.Combine(root, "root"), $" - WSL {distro}"));
                try
                {
                    var home = Path.Combine(root, "home");
                    if (Directory.Exists(home))
                        foreach (var u in Directory.GetDirectories(home).Take(10))
                            homes.Add(new KeyValuePair<string, string>(u, $" - WSL {distro}"));
                }
                catch (Exception) { }
            }
            return homes;
        }

        private static string Read(string path)
        {
            try { return File.Exists(path) ? File.ReadAllText(path, Encoding.UTF8) : null; }
            catch (Exception) { return null; }
        }

        /// <summary>([(key, where)], notes) in priority order, duplicates kept.</summary>
        public static Tuple<List<KeyValuePair<string, string>>, List<string>> DiscoverKeys(JObj settings)
        {
            var found = new List<KeyValuePair<string, string>>();
            var notes = new List<string>();
            void Add(string key, string where) => found.Add(new KeyValuePair<string, string>(key, where));

            var manual = Core.Settings.Str(settings, "api_key");
            if (manual.Length > 0) Add(manual, "config.json");

            foreach (var h in Homes(settings))
            {
                var home = h.Key;
                var suffix = h.Value;
                var local = suffix.Length == 0;
                if (Core.Settings.Flag(settings, "scan_opencode", true))
                {
                    var authFiles = new List<string>();
                    if (local && !string.IsNullOrEmpty(Env("OPENCODE_AUTH_JSON"))) authFiles.Add(Env("OPENCODE_AUTH_JSON"));
                    if (local && !string.IsNullOrEmpty(Env("XDG_DATA_HOME"))) authFiles.Add(Path.Combine(Env("XDG_DATA_HOME"), "opencode", "auth.json"));
                    authFiles.Add(Path.Combine(home, ".local", "share", "opencode", "auth.json"));
                    if (local)
                        foreach (var v in new[] { "LOCALAPPDATA", "APPDATA" })
                            if (!string.IsNullOrEmpty(Env(v))) authFiles.Add(Path.Combine(Env(v), "opencode", "auth.json"));
                    foreach (var path in authFiles)
                    {
                        var text = Read(path);
                        if (text == null) continue;
                        var data = Json.ParseObject(text);
                        if (data == null)
                        {
                            notes.Add($"{path}: not valid JSON");
                            continue;
                        }
                        var keys = OpencodeAuthKeys(data);
                        foreach (var k in keys) Add(k, "OpenCode" + suffix);
                        if (keys.Count == 0) notes.Add($"{path}: no DeepSeek key (add one with /connect in OpenCode)");
                    }
                    var configDirs = new List<string>();
                    if (local && !string.IsNullOrEmpty(Env("XDG_CONFIG_HOME"))) configDirs.Add(Path.Combine(Env("XDG_CONFIG_HOME"), "opencode"));
                    configDirs.Add(Path.Combine(home, ".config", "opencode"));
                    var configFiles = configDirs.SelectMany(d => new[] { "opencode.json", "opencode.jsonc", "config.json" }.Select(n => Path.Combine(d, n))).ToList();
                    if (local && !string.IsNullOrEmpty(Env("OPENCODE_CONFIG"))) configFiles.Insert(0, Env("OPENCODE_CONFIG"));
                    foreach (var path in configFiles)
                    {
                        var text = Read(path);
                        if (text == null) continue;
                        var data = Json.ParseObject(StripJsonc(text));
                        if (data == null)
                        {
                            notes.Add($"{path}: could not be parsed");
                            continue;
                        }
                        foreach (var k in OpencodeConfigKeys(data, Env)) Add(k, "OpenCode" + suffix);
                    }
                }
                if (Core.Settings.Flag(settings, "scan_dsh", true))
                {
                    var dshHome = local && !string.IsNullOrWhiteSpace(Env("DSH_HOME")) ? Env("DSH_HOME") : Path.Combine(home, ".dsh");
                    var text = Read(Path.Combine(dshHome, ".credentials.yaml"));
                    var key = text != null ? DshCredentialsKey(text) : null;
                    if (key == null)
                    {
                        var envText = Read(Path.Combine(dshHome, ".env"));
                        key = envText != null ? DotenvKey(envText) : null;
                    }
                    if (key != null) Add(key, "DeepSeek Harness" + suffix);
                    else if (text != null) notes.Add($"{dshHome}: no {EnvName} saved (Settings > Models in dsh)");
                }
            }
            var envKey = (Env(EnvName) ?? "").Trim();
            if (envKey.Length > 0) Add(envKey, "env " + EnvName);
            if (found.Count == 0) notes.Add("no DeepSeek API key in OpenCode, DeepSeek Harness or DEEPSEEK_API_KEY");
            return Tuple.Create(found, notes);
        }

        public static string Mask(string key) => key.Length > 8 ? "sk-..." + key.Substring(key.Length - 4) : "sk-...";

        // ------------------------------------------------------------ parsing

        /// <summary>(rows, headline, available) from /user/balance.</summary>
        public static Tuple<List<InfoRow>, string, bool> BalanceRows(JObj payload, double low)
        {
            var rows = new List<InfoRow>();
            string headline = null;
            var available = payload.Bool("is_available");
            foreach (var info in (payload.Arr("balance_infos") ?? new List<object>()).OfType<JObj>())
            {
                var cur = info.Text("currency") ?? "CNY";
                var total = Json.ToFloat(info["total_balance"]);
                if (total == null) continue;
                var granted = Json.ToFloat(info["granted_balance"]);
                var topped = Json.ToFloat(info["topped_up_balance"]);
                var warn = available == false || total < low;
                rows.Add(new InfoRow("Balance", Account.Money(total.Value, cur), warn ? "warn" : "good"));
                var parts = new List<string>();
                if (topped != null) parts.Add($"{Account.Money(topped.Value, cur)} topped up");
                if (granted != null && granted.Value != 0) parts.Add($"{Account.Money(granted.Value, cur)} granted (can expire)");
                if (parts.Count > 0) rows.Add(new InfoRow("", string.Join(" \u00b7 ", parts)));
                headline = headline ?? Account.Money(total.Value, cur);
            }
            if (available == false)
                rows.Add(new InfoRow("Status", "balance too low for API calls; top up at platform.deepseek.com", "warn"));
            return Tuple.Create(rows, headline, available != false);
        }

        public override bool Detect() => DiscoverKeys(Settings).Item1.Count > 0;

        private ProviderResult Page(string key, string where, ProviderResult result)
        {
            var page = new ProviderResult(Id, Name) { FetchedAt = result.FetchedAt, Billing = Billing, Label = where };
            var tag = $"balance API ({where})";
            HttpReply resp;
            try
            {
                resp = Http.Get(BalanceUrl, new Dictionary<string, string>
                {
                    { "Authorization", "Bearer " + key },
                    { "Accept", "application/json" },
                });
            }
            catch (Exception e)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, "request failed: " + e.Message));
                return page;
            }
            if (resp.Status == 401 || resp.Status == 403)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, $"key {Mask(key)} rejected (HTTP {resp.Status})"));
                return page;
            }
            if (resp.Status >= 400)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, $"HTTP {resp.Status}: {Short(resp.Text, 160)}"));
                return page;
            }
            var payload = Json.ParseObject(resp.Text);
            if (payload == null)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, "response was not JSON"));
                return page;
            }
            var parsed = BalanceRows(payload, Core.Settings.Num(Settings, "low_balance", 5));
            if (parsed.Item1.Count == 0)
            {
                page.Attempts.Add(new SourceAttempt(tag, false, "no balance in response: " + Short(resp.Text, 160)));
                return page;
            }
            page.Ok = true;
            page.Info = parsed.Item1;
            page.Headline = parsed.Item2;
            page.Plan = "Pay as you go";
            page.Account = "key " + Mask(key);
            page.Source = "api.deepseek.com/user/balance";
            page.Status = parsed.Item3 ? "connected" : "balance too low";
            page.DataTime = Time.Now;
            page.Attempts.Add(new SourceAttempt(tag, true, Mask(key)));
            return page;
        }

        public override void Collect(ProviderResult result)
        {
            var found = DiscoverKeys(Settings);
            if (found.Item1.Count == 0)
            {
                result.Attempts.Add(new SourceAttempt("API key", false, string.Join("; ", found.Item2)));
                result.Status = "no DeepSeek API key found (OpenCode, DeepSeek Harness, DEEPSEEK_API_KEY)";
                return;
            }
            foreach (var note in found.Item2) result.Attempts.Add(new SourceAttempt("key search", false, note));
            // One page per distinct key; every tool holding it is named on it.
            var merged = new List<KeyValuePair<string, List<string>>>();
            foreach (var kv in found.Item1)
            {
                var entry = merged.FirstOrDefault(m => m.Key == kv.Key);
                if (entry.Key == null)
                {
                    entry = new KeyValuePair<string, List<string>>(kv.Key, new List<string>());
                    merged.Add(entry);
                }
                if (!entry.Value.Contains(kv.Value)) entry.Value.Add(kv.Value);
            }
            var pages = new List<ProviderResult>();
            foreach (var m in merged)
            {
                var page = Page(m.Key, string.Join(" + ", m.Value), result);
                result.Attempts.AddRange(page.Attempts);
                if (page.Ok) pages.Add(page);
            }
            if (pages.Count > 0)
            {
                result.Adopt(pages[0]);
                result.Alternates = pages.Skip(1).ToList();
                return;
            }
            var failed = result.Attempts.Where(a => !a.Ok && a.Name.StartsWith("balance")).ToList();
            result.Status = failed.Count > 0 ? failed.Last().Detail : "could not read the DeepSeek balance";
        }
    }
}
