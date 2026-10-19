using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using QuotaTray.Core;
using QuotaTray.Win;

namespace QuotaTray.Providers
{
    /// <summary>Harness-managed Platform wallets, separate from DeepSeek API keys.</summary>
    public sealed class DshManagedProvider : Provider
    {
        public override string Id => "dsh_managed";
        public override string Name => "\u7531 Harness \u7ba1\u7406";
        public override string Billing => "payg";
        public const string Issuer = "https://platform.deepseek.com";
        public const string SummaryUrl = Issuer + "/api/v0/users/get_user_summary";
        private const string RecordKey = "deepseek-account-platform/default";
        private const string LoginStatus = "sign in again in DeepSeek Harness";
        private static readonly Regex Amount = new Regex(@"\A-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\z", RegexOptions.CultureInvariant);

        public DshManagedProvider(Config config) : base(config) { }

        private static bool Integer(object value, out double number)
        {
            if (value is string || value is bool) { number = double.NaN; return false; }
            number = Json.AsNumber(value) ?? double.NaN;
            return !double.IsNaN(number) && !double.IsInfinity(number) && number == Math.Truncate(number);
        }

        // Project only the mapping/scalar fields of the selected Harness grant.
        // Other records stay opaque; selected fields cannot use aliases or tags.
        private static string WithoutComment(string text)
        {
            char quote = '\0';
            for (var i = 0; i < text.Length; i++)
            {
                var ch = text[i];
                if (quote == '"' && ch == '\\') { i++; continue; }
                if (quote == '\'' && ch == '\'' && i + 1 < text.Length && text[i + 1] == '\'') { i++; continue; }
                if (quote != '\0') { if (ch == quote) quote = '\0'; continue; }
                if (ch == '"' || ch == '\'') quote = ch;
                else if (ch == '#' && (i == 0 || char.IsWhiteSpace(text[i - 1]))) return text.Substring(0, i).TrimEnd();
            }
            return text.TrimEnd();
        }

        private static bool Scalar(string text, out object value)
        {
            value = null;
            if (text.StartsWith("\"", StringComparison.Ordinal))
                return Json.TryParse(text, out value) && value is string;
            if (text.StartsWith("'", StringComparison.Ordinal))
            {
                if (text.Length < 2 || !text.EndsWith("'", StringComparison.Ordinal)) return false;
                var inner = text.Substring(1, text.Length - 2);
                if (inner.Replace("''", "").Contains("'")) return false;
                value = inner.Replace("''", "'");
                return true;
            }
            if (text == "{}") { value = new JObj(); return true; }
            if (text == "[]") { value = new List<object>(); return true; }
            if (text.Length == 0 || "&*!|>[{".Contains(text[0])) return false;
            if (text == "null" || text == "~") return true;
            if (text == "true" || text == "false") { value = text == "true"; return true; }
            if (Regex.IsMatch(text, @"^-?[0-9]+$"))
            {
                if (!double.TryParse(text, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var n)) return false;
                value = n;
                return true;
            }
            value = text;
            return true;
        }

        private static bool UniqueJsonKeys(string text)
        {
            // Json.ParseObject has already checked JSON syntax. Walk string tokens
            // separately so duplicate object members cannot silently replace a grant.
            var containers = new Stack<HashSet<string>>();
            for (var i = 0; i < text.Length; i++)
            {
                var ch = text[i];
                if (ch == '{') containers.Push(new HashSet<string>(StringComparer.Ordinal));
                else if (ch == '[') containers.Push(null);
                else if (ch == '}' || ch == ']') containers.Pop();
                else if (ch == '"')
                {
                    var start = i++;
                    while (i < text.Length)
                    {
                        if (text[i] == '\\') { i += 2; continue; }
                        if (text[i] == '"') break;
                        i++;
                    }
                    var next = i + 1;
                    while (next < text.Length && char.IsWhiteSpace(text[next])) next++;
                    if (next < text.Length && text[next] == ':' && containers.Count > 0 && containers.Peek() != null)
                    {
                        if (!Json.TryParse(text.Substring(start, i - start + 1), out var decoded)
                            || !(decoded is string key) || !containers.Peek().Add(key)) return false;
                    }
                }
            }
            return true;
        }

        private static JObj CredentialDocument(string text)
        {
            if (string.IsNullOrWhiteSpace(text)) return null;
            text = text.TrimStart('\ufeff');
            if (text.TrimStart().StartsWith("{", StringComparison.Ordinal))
            {
                var json = Json.ParseObject(text);
                return json != null && UniqueJsonKeys(text) ? json : null;
            }
            var root = new JObj();
            var stack = new List<Tuple<int, JObj>> { Tuple.Create(-1, root) };
            var indents = new Dictionary<JObj, int> { { root, 0 } };
            var sections = new Dictionary<JObj, string> { { root, "root" } };
            int? skipIndent = null;
            foreach (var raw in text.Replace("\r", "").Split('\n'))
            {
                var line = WithoutComment(raw);
                if (string.IsNullOrWhiteSpace(line)) continue;
                if (line == "---") continue;
                var indent = line.Length - line.TrimStart(' ').Length;
                if (skipIndent != null && indent > skipIndent.Value) continue;
                skipIndent = null;
                if (line.Substring(indent).StartsWith("\t", StringComparison.Ordinal)) return null;
                var match = Regex.Match(line.Substring(indent), @"^(?<key>""(?:[^""\\]|\\.)*""|'(?:[^']|'')*'|[A-Za-z0-9_./-]+)\s*:\s*(?<value>.*)$");
                if (!match.Success) return null;
                if (!Scalar(match.Groups["key"].Value, out var keyValue) || !(keyValue is string key)) return null;
                while (stack.Count > 1 && indent <= stack[stack.Count - 1].Item1) stack.RemoveAt(stack.Count - 1);
                if (stack.Count == 1 && indent != 0) return null;
                var parent = stack[stack.Count - 1].Item2;
                if (indents.TryGetValue(parent, out var expected) && expected != indent) return null;
                indents[parent] = indent;
                var section = sections[parent];
                var relevant = section == "root" ? key == "version" || key == "records"
                    : section == "records" ? key == RecordKey
                    : section == "grant" ? key == "kind" || key == "payload"
                    : section == "payload" && (key == "version" || key == "token" || key == "issuer");
                if (!relevant) { skipIndent = indent; continue; }
                if (parent.Has(key)) return null;
                var rawValue = match.Groups["value"].Value.Trim();
                if (rawValue.Length == 0)
                {
                    var child = new JObj();
                    parent[key] = child;
                    stack.Add(Tuple.Create(indent, child));
                    sections[child] = section == "root" && key == "records" ? "records"
                        : section == "records" && key == RecordKey ? "grant"
                        : section == "grant" && key == "payload" ? "payload" : "invalid";
                }
                else
                {
                    if (!Scalar(rawValue, out var value)) return null;
                    parent[key] = value;
                }
            }
            return root;
        }

        /// <summary>Returns the official grant token only, without retaining other fields.</summary>
        public static string CredentialToken(string text)
        {
            try
            {
                var document = CredentialDocument(text);
                if (document == null || !Integer(document["version"], out var version) || version != 1) return null;
                var record = document.Obj("records")?.Obj(RecordKey);
                if (record?.Str("kind") != "grant") return null;
                var payload = record.Obj("payload");
                if (payload == null || !Integer(payload["version"], out version) || version != 1 || payload.Str("issuer") != Issuer) return null;
                var token = payload.Str("token");
                return !string.IsNullOrEmpty(token) && token.All(c => c >= '!' && c <= '~') ? token : null;
            }
            catch (Exception) { return null; }
        }

        private List<KeyValuePair<string, string>> CredentialFiles()
        {
            var files = new List<KeyValuePair<string, string>>();
            try
            {
                foreach (var home in DeepSeekProvider.Homes(Settings))
                {
                    var local = string.IsNullOrEmpty(home.Value);
                    if (!local && !Core.Settings.Flag(Settings, "scan_wsl", true)) continue;
                    var manual = local ? Core.Settings.Str(Settings, "home") : "";
                    var fromEnv = local ? DeepSeekProvider.Env("DSH_HOME") : null;
                    var directory = manual.Length > 0 ? manual : !string.IsNullOrWhiteSpace(fromEnv) ? fromEnv : Path.Combine(home.Key, ".dsh");
                    var path = Path.Combine(directory, ".credentials.yaml");
                    if (!files.Any(f => string.Equals(f.Key, path, StringComparison.OrdinalIgnoreCase)))
                        files.Add(new KeyValuePair<string, string>(path, "DeepSeek Harness" + (local ? "" : " - WSL " + home.Value)));
                }
            }
            catch (Exception) { }
            return files;
        }

        public override bool Detect() => CredentialFiles().Any(f => Proc.SafeFileExists(f.Key));

        private static bool DecimalAmount(object raw, out decimal amount)
        {
            amount = 0;
            if (!(raw is string text) || !Amount.IsMatch(text)
                || !decimal.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out amount)) return false;
            // Avoid silently replacing a nonzero number outside decimal's range by zero.
            var coefficient = text.Split('e', 'E')[0];
            return amount != 0 || !coefficient.Any(c => c >= '1' && c <= '9');
        }

        private static string Money(decimal amount, string currency)
        {
            var symbol = currency == "CNY" ? "\u00a5" : "$";
            if (amount > 0 && amount < 0.01m) return "<" + symbol + "0.01";
            var magnitude = Math.Abs(amount);
            decimal display;
            if (amount < 0)
                display = Math.Max(0.01m, decimal.Round(magnitude, 2, MidpointRounding.AwayFromZero));
            else
            {
                // Truncate the positive fractional part without overflowing on
                // a large integral balance; match Harness's wallet formatter.
                var whole = decimal.Truncate(magnitude);
                display = whole + decimal.Truncate((magnitude - whole) * 100m) / 100m;
            }
            return (amount < 0 ? "-" : "") + symbol + display.ToString("N2", CultureInfo.InvariantCulture);
        }

        /// <summary>Strictly validates every wallet before returning any balance.</summary>
        public static Tuple<List<InfoRow>, string, bool> BalanceRows(JObj payload, double low)
        {
            if (double.IsNaN(low) || double.IsInfinity(low) || low < 0 || low > (double)decimal.MaxValue) low = 5;
            var data = payload?.Obj("data");
            if (payload == null || !Integer(payload["code"], out var code) || code != 0
                || data == null || !Integer(data["biz_code"], out code) || code != 0) return null;
            var summary = data.Obj("biz_data");
            var normal = summary?.Arr("normal_wallets");
            var bonus = summary?.Arr("bonus_wallets");
            if (normal == null || bonus == null || normal.Count + bonus.Count == 0) return null;
            var totals = new Dictionary<string, decimal[]>();
            try
            {
                for (var kind = 0; kind < 2; kind++)
                    foreach (var raw in kind == 0 ? normal : bonus)
                    {
                        if (!(raw is JObj wallet)) return null;
                        var currency = wallet.Str("currency");
                        if ((currency != "CNY" && currency != "USD") || !DecimalAmount(wallet["balance"], out var balance)) return null;
                        if (!totals.ContainsKey(currency)) totals[currency] = new decimal[2];
                        totals[currency][kind] = checked(totals[currency][kind] + balance);
                    }
                var rows = new List<InfoRow>();
                var headlines = new List<string>();
                var healthy = true;
                foreach (var currency in new[] { "CNY", "USD" })
                {
                    if (!totals.TryGetValue(currency, out var parts)) continue;
                    var sum = checked(parts[0] + parts[1]);
                    var warn = (double)sum < low;
                    healthy &= !warn;
                    var total = Money(sum, currency);
                    rows.Add(new InfoRow("Balance", total, warn ? "warn" : "good"));
                    rows.Add(new InfoRow("Topped up", Money(parts[0], currency)));
                    rows.Add(new InfoRow("Granted", Money(parts[1], currency) + " (can expire)"));
                    headlines.Add(total);
                }
                return Tuple.Create(rows, string.Join(" / ", headlines), healthy);
            }
            catch (OverflowException) { return null; }
        }

        private ProviderResult Page(string token, string label)
        {
            var page = new ProviderResult(Id, Name) { Billing = Billing, Installed = true, Label = label };
            const string tag = "Harness wallet";
            var request = new HttpRequest { Url = SummaryUrl, FollowRedirects = false, UseCookies = false, TimeoutSeconds = 30 };
            request.Headers["x-dsh-auth-token"] = token;
            request.Headers["Accept"] = "*/*";
            request.Headers["User-Agent"] = "QuotaTray/" + AppInfo.Version;
            request.Headers["x-client-bundle-id"] = "";
            request.Headers["x-client-platform"] = "desktop-win";
            request.Headers["x-client-version"] = AppInfo.Version;
            request.Headers["x-client-locale"] = "zh_CN";
            request.Headers["x-client-timezone-offset"] = ((int)TimeZoneInfo.Local.GetUtcOffset(Time.Now).TotalSeconds).ToString(CultureInfo.InvariantCulture);
            void Fail(string status, string detail)
            {
                page.Status = status;
                page.Attempts.Add(new SourceAttempt(tag, false, detail));
            }
            try
            {
                var response = Http.Send(request);
                var payload = Json.ParseObject(response.Text);
                var hasCode = Integer(payload?["code"], out var code);
                var hasBiz = Integer(payload?.Obj("data")?["biz_code"], out var biz);
                var detail = "HTTP " + response.Status;
                if (hasCode) detail += ", code " + code.ToString(CultureInfo.InvariantCulture);
                if (hasBiz) detail += ", biz_code " + biz.ToString(CultureInfo.InvariantCulture);
                if (response.Status == 401 || (hasCode && code == 40003))
                    Fail(LoginStatus, detail + ": login rejected");
                else if (response.Status != 200) Fail("Harness wallet HTTP " + response.Status, detail);
                else if (payload == null) Fail("Harness wallet returned non-JSON", detail + ": response was not JSON");
                else if (!hasCode) Fail("no valid wallets in Harness response", detail + ": invalid API envelope");
                else if (code != 0) Fail("Harness response code " + code.ToString(CultureInfo.InvariantCulture), detail);
                else if (!hasBiz) Fail("no valid wallets in Harness response", detail + ": invalid API envelope");
                else if (biz != 0) Fail("Harness business code " + biz.ToString(CultureInfo.InvariantCulture), detail);
                else
                {
                    var parsed = BalanceRows(payload, Core.Settings.Num(Settings, "low_balance", 5));
                    if (parsed == null) Fail("no valid wallets in Harness response", detail + ": invalid wallet data");
                    else
                    {
                        page.Ok = true;
                        page.Info = parsed.Item1;
                        page.Headline = parsed.Item2;
                        page.Plan = "Harness managed";
                        page.Source = "platform.deepseek.com/api/v0/users/get_user_summary";
                        page.Status = "connected";
                        page.DataTime = Time.Now;
                        page.Attempts.Add(new SourceAttempt(tag, true, "wallet balances received"));
                    }
                }
            }
            catch (Exception) { Fail("could not reach Harness wallet", "request or response processing failed"); }
            return page;
        }

        public override void Collect(ProviderResult result)
        {
            // Never allow credential-bearing exception text to escape to the base
            // provider's generic exception logger.
            try
            {
                var credentials = new Dictionary<string, List<string>>(StringComparer.Ordinal);
                foreach (var file in CredentialFiles())
                {
                    if (!Proc.SafeFileExists(file.Key)) continue;
                    string token;
                    try { token = CredentialToken(Io.ReadAllTextShared(file.Key)); }
                    catch (Exception) { token = null; }
                    if (token == null)
                    {
                        result.Attempts.Add(new SourceAttempt("Harness credentials", false, "no supported official managed grant"));
                        continue;
                    }
                    if (!credentials.ContainsKey(token)) credentials[token] = new List<string>();
                    if (!credentials[token].Contains(file.Value)) credentials[token].Add(file.Value);
                }
                if (credentials.Count == 0)
                {
                    result.Status = result.Installed ? LoginStatus : "DeepSeek Harness managed login not detected";
                    return;
                }
                var pages = new List<ProviderResult>();
                foreach (var credential in credentials)
                {
                    var page = Page(credential.Key, string.Join(" + ", credential.Value));
                    result.Attempts.AddRange(page.Attempts);
                    pages.Add(page);
                }
                var ordered = pages.OrderByDescending(p => p.Ok).ToList();
                result.Adopt(ordered[0]);
                result.Alternates = ordered.Skip(1).ToList();
            }
            catch (Exception)
            {
                result.Status = "could not read Harness managed balance";
                result.Attempts.Add(new SourceAttempt("Harness wallet", false, "collection failed"));
            }
        }
    }
}
