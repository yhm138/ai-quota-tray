using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using QuotaTray.Core;
using QuotaTray.Win;

namespace QuotaTray.Providers
{
    /// <summary>
    /// TRAE (ByteDance IDE) quota. The Cloud-IDE JWT lives in
    /// %APPDATA%\Trae CN\User\globalStorage\storage.json under iCubeAuthInfo://...,
    /// as plain JSON or base64 of a "byte crypto" blob (AES-128-CBC). With it,
    /// the pay endpoints report the plan and the fast-request usage.
    /// </summary>
    public sealed class TraeProvider : Provider
    {
        public override string Id => "trae";
        public override string Name => "TRAE";

        public const string PayStatusPath = "/trae/api/v1/pay/ide_user_pay_status";
        public const string EntUsagePath = "/trae/api/v1/pay/ide_user_ent_usage";
        private static readonly string[] CnHosts = { "https://api.trae.cn", "https://api.trae.com.cn" };
        private static readonly string[] GlobalHosts = { "https://grow-normal.trae.ai", "https://growsg-normal.trae.ai" };

        private const string AuthPrefix = "iCubeAuthInfo://";
        private const string DefaultProvider = "icube.cloudide";
        private const string UsertagKey = "iCubeAuthInfo://usertag";
        private const string DevicePrefix = "iCubeAuthInfo://icube-dc:";

        private static readonly Dictionary<long, string> ProductTypes = new Dictionary<long, string>
        {
            { 6, "Ultra" }, { 4, "Pro+" }, { 1, "Pro" }, { 9, "Pro" }, { 8, "Lite" }, { 0, "Free" },
        };

        private static readonly Tuple<string, bool>[] Apps = { Tuple.Create("Trae CN", true), Tuple.Create("Trae", false) };

        /// <summary>Tests replace the storage paths searched: [(storage.json, isCn, label)].</summary>
        public Func<List<Tuple<string, bool, string>>> StoragePathsOverride;

        public TraeProvider(Config config) : base(config) { }

        // ------------------------------------------------------------ byte crypto

        private const int Header = 6;
        private const int KeyLen = 32;
        private static readonly byte[] PrefixAes = { 116, 99, 5, 16, 0, 0 };
        private static readonly byte[] PrefixAesPrivate = { 18, 57, 32, 32, 2, 3 };
        private static readonly byte[] AesA = { 82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251, 124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203, 84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78, 8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37 };
        private static readonly byte[] AesB = { 31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95, 96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239, 160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125 };
        private static readonly byte[] AesPrivateA = { 191, 192, 216, 250, 122, 246, 220, 97, 31, 254, 98, 27, 8, 72, 71, 176, 135, 99, 96, 18, 127, 101, 203, 104, 211, 102, 191, 125, 37, 72, 150, 156, 51, 229, 121, 35, 17, 153, 141, 177, 110, 131, 150, 128, 172, 255, 254, 6, 18, 140, 55, 62, 236, 249, 135, 64, 135, 12, 117, 4, 89, 149, 168, 209 };
        private static readonly byte[] AesPrivateB = { 246, 204, 26, 232, 232, 70, 129, 109, 223, 146, 169, 242, 23, 241, 105, 145, 50, 196, 165, 42, 254, 120, 3, 54, 244, 207, 209, 85, 53, 6, 138, 106, 175, 148, 31, 204, 186, 186, 165, 182, 87, 142, 49, 10, 39, 110, 26, 154, 86, 56, 173, 125, 18, 64, 198, 225, 99, 99, 83, 82, 191, 134, 76, 170 };

        public static byte[] Salt(bool priv)
        {
            var a = priv ? AesPrivateA : AesA;
            var b = priv ? AesPrivateB : AesB;
            var salt = new byte[a.Length];
            for (var i = 0; i < a.Length; i++) salt[i] = (byte)(a[i] ^ b[i]);
            return salt;
        }

        /// <summary>Undo TRAE's "byte crypto"; null when it does not check out.</summary>
        public static byte[] ByteCryptoDecrypt(byte[] raw)
        {
            if (raw.Length <= Header + KeyLen) return null;
            var header = raw.Take(Header).ToArray();
            bool priv;
            if (header.SequenceEqual(PrefixAes)) priv = false;
            else if (header.SequenceEqual(PrefixAesPrivate)) priv = true;
            else return null;
            var keyMaterial = raw.Skip(Header).Take(KeyLen).ToArray();
            var ciphertext = raw.Skip(Header + KeyLen).ToArray();
            if (ciphertext.Length == 0 || ciphertext.Length % 16 != 0) return null;
            byte[] merged;
            using (var sha = SHA512.Create())
            {
                var keyHash = sha.ComputeHash(keyMaterial);
                var input = new byte[keyHash.Length + 64];
                Buffer.BlockCopy(keyHash, 0, input, 0, keyHash.Length);
                Buffer.BlockCopy(Salt(priv), 0, input, keyHash.Length, 64);
                merged = sha.ComputeHash(input);
            }
            byte[] padded;
            try
            {
                using (var aes = Aes.Create())
                {
                    aes.KeySize = 128;
                    aes.Mode = CipherMode.CBC;
                    aes.Padding = PaddingMode.None;
                    aes.Key = merged.Take(16).ToArray();
                    aes.IV = merged.Skip(16).Take(16).ToArray();
                    using (var dec = aes.CreateDecryptor())
                        padded = dec.TransformFinalBlock(ciphertext, 0, ciphertext.Length);
                }
            }
            catch (CryptographicException) { return null; }
            if (padded.Length == 0) return null;
            var pad = padded[padded.Length - 1];
            if (pad < 1 || pad > 16 || pad > padded.Length) return null;
            var plain = padded.Take(padded.Length - pad).ToArray();
            if (plain.Length < 64) return null;
            using (var sha = SHA512.Create())
                if (!sha.ComputeHash(plain.Skip(64).ToArray()).SequenceEqual(plain.Take(64))) return null;
            return plain.Skip(64).ToArray();
        }

        // ------------------------------------------------------------ storage.json

        public static JObj DecodeValue(object value)
        {
            if (value is JObj obj) return obj;
            var text = (value as string)?.Trim();
            if (string.IsNullOrEmpty(text)) return null;
            var parsed = Json.ParseObject(text);
            if (parsed != null) return parsed;
            byte[] raw;
            try { raw = Convert.FromBase64String(text); }
            catch (FormatException) { return null; }
            var plain = ByteCryptoDecrypt(raw);
            return plain != null ? Json.ParseObject(Encoding.UTF8.GetString(plain)) : null;
        }

        private static string UserAuthKey(JObj root)
        {
            if (root[AuthPrefix + DefaultProvider] is string || root[AuthPrefix + DefaultProvider] is JObj)
                return AuthPrefix + DefaultProvider;
            foreach (var key in root.Keys)
                if (key.StartsWith(AuthPrefix, StringComparison.Ordinal) && key != UsertagKey && !key.StartsWith(DevicePrefix, StringComparison.Ordinal))
                    return key;
            return null;
        }

        private static string Pick(JObj node, params string[][] paths)
        {
            foreach (var path in paths)
            {
                object cur = node;
                foreach (var step in path) cur = (cur as JObj)?[step];
                if (cur is string s && s.Length > 0) return s;
            }
            return null;
        }

        /// <summary>(access token, email) from a storage.json.</summary>
        public static Tuple<string, string> ReadStorageAuth(string storagePath)
        {
            JObj root;
            try { root = Json.ParseObject(File.ReadAllText(storagePath, Encoding.UTF8)); }
            catch (Exception) { return Tuple.Create((string)null, (string)null); }
            if (root == null) return Tuple.Create((string)null, (string)null);
            var authKey = UserAuthKey(root);
            var auth = (authKey != null ? DecodeValue(root[authKey]) : null) ?? DecodeValue(root[AuthPrefix + DefaultProvider]);
            if (auth == null) return Tuple.Create((string)null, (string)null);
            var token = Pick(auth, new[] { "accessToken" }, new[] { "access_token" }, new[] { "token" },
                new[] { "data", "accessToken" }, new[] { "data", "access_token" }, new[] { "auth", "accessToken" });
            if (token == null) return Tuple.Create((string)null, (string)null);
            var email = Pick(auth, new[] { "email" }, new[] { "account", "email" }, new[] { "data", "email" },
                new[] { "user", "email" }, new[] { "userInfo", "email" });
            return Tuple.Create(token, email);
        }

        // ------------------------------------------------------------ discovery

        private List<Tuple<string, bool, string>> StoragePaths()
        {
            if (StoragePathsOverride != null) return StoragePathsOverride();
            var output = new List<Tuple<string, bool, string>>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            void Add(string b, bool isCn, string label)
            {
                var path = Path.Combine(b, "User", "globalStorage", "storage.json");
                if (seen.Add(path.ToLowerInvariant())) output.Add(Tuple.Create(path, isCn, label));
            }
            var manual = Core.Settings.Str(Settings, "storage_path");
            if (manual.Length > 0) output.Add(Tuple.Create(manual, Core.Settings.Flag(Settings, "cn", true), "TRAE"));
            var appdata = Environment.GetEnvironmentVariable("APPDATA");
            foreach (var app in Apps)
                if (!string.IsNullOrEmpty(appdata)) Add(Path.Combine(appdata, app.Item1), app.Item2, app.Item2 ? "TRAE CN" : "TRAE");
            if (Core.Settings.Flag(Settings, "scan_wsl", true))
                foreach (var distro in Proc.WslDistros())
                {
                    var root = $@"\\wsl.localhost\{distro}";
                    var homes = new List<string> { Path.Combine(root, "root") };
                    try
                    {
                        var home = Path.Combine(root, "home");
                        if (Directory.Exists(home)) homes.AddRange(Directory.GetDirectories(home).Take(10));
                    }
                    catch (Exception) { }
                    foreach (var home in homes)
                        foreach (var app in Apps)
                            Add(Path.Combine(home, ".config", app.Item1), app.Item2, (app.Item2 ? "TRAE CN" : "TRAE") + $" - WSL {distro}");
                }
            return output;
        }

        // ------------------------------------------------------------ usage parsing

        private static double? Num(JObj node, params string[] keys)
        {
            if (node == null) return null;
            foreach (var k in keys)
            {
                var v = Json.ToFloat(node[k]);
                if (v != null && !(node[k] is bool)) return v;
            }
            return null;
        }

        public static List<JObj> FindPackList(object obj)
        {
            if (obj is JObj o)
            {
                var packs = o.Arr("user_entitlement_pack_list");
                if (packs != null) return packs.OfType<JObj>().ToList();
                foreach (var kv in o)
                {
                    var found = FindPackList(kv.Value);
                    if (found != null) return found;
                }
            }
            else if (obj is List<object> l)
                foreach (var v in l)
                {
                    var found = FindPackList(v);
                    if (found != null) return found;
                }
            return null;
        }

        private static long? PackProductType(JObj pack)
        {
            var v = Num(pack.Obj("entitlement_base_info"), "product_type") ?? Num(pack, "product_type");
            return v != null ? (long)v.Value : (long?)null;
        }

        private static JObj PackUsage(JObj pack) => pack.Obj("usage") ?? new JObj();

        private static JObj PackQuota(JObj pack)
        {
            var baseInfo = pack.Obj("entitlement_base_info") ?? new JObj();
            var extra = baseInfo.Obj("product_extra") ?? new JObj();
            return baseInfo.Obj("quota") ?? extra.Obj("subscription_extra")?.Obj("quota") ?? extra.Obj("package_extra")?.Obj("quota") ?? new JObj();
        }

        private static bool Visible(JObj pack)
        {
            if (pack.Bool("is_hide") == true) return false;
            var status = Num(pack, "status", "entitlement_status");
            return status == null || status == 1;
        }

        public static Tuple<List<QuotaWindow>, List<InfoRow>, string> ParseUsage(JObj response)
        {
            var windows = new List<QuotaWindow>();
            var rows = new List<InfoRow>();
            string plan = null;
            var packs = (FindPackList(response) ?? new List<JObj>()).Where(Visible).ToList();
            if (packs.Count == 0) return Tuple.Create(windows, rows, plan);

            foreach (var want in new long[] { 6, 4, 1, 9, 8, 0 })
                if (packs.Any(p => PackProductType(p) == want)) { plan = ProductTypes[want]; break; }

            var fastUsed = packs.Sum(p => Num(PackUsage(p), "premium_model_fast_amount") ?? 0.0);
            var fastLimits = packs.Select(p => Num(PackQuota(p), "premium_model_fast_request_limit")).Where(v => v != null).Select(v => v.Value).ToList();
            if (fastLimits.Count > 0)
            {
                if (fastLimits.Any(l => l == -1))
                    windows.Add(new QuotaWindow("fast_request", "Fast requests", 0.0, null, "unlimited", 10));
                else
                {
                    var limit = fastLimits.Sum();
                    double? pct = limit > 0 ? Math.Min(100.0, fastUsed / limit * 100.0) : (double?)null;
                    windows.Add(new QuotaWindow("fast_request", "Fast requests", pct, null,
                        $"{Account.N0(fastUsed)} / {Account.N0(limit)}", 10, limit > 0 && fastUsed >= limit));
                }
            }

            var main = packs.FirstOrDefault(p => { var t = PackProductType(p); return t != null && t != 3; }) ?? packs[0];
            var basicLimit = Num(PackQuota(main), "basic_usage_limit");
            var basicUsed = Num(PackUsage(main), "basic_usage_amount");
            if (basicLimit != null && basicLimit >= 0 && basicUsed != null)
            {
                if (windows.Count == 0)
                {
                    double? pct = basicLimit > 0 ? Math.Min(100.0, basicUsed.Value / basicLimit.Value * 100.0) : (double?)null;
                    windows.Add(new QuotaWindow("basic_usage", "Usage", pct, null,
                        $"{Account.N0(basicUsed.Value)} / {Account.N0(basicLimit.Value)}", 15, basicLimit > 0 && basicUsed >= basicLimit));
                }
                else rows.Add(new InfoRow("Basic usage", $"{Account.N0(basicUsed.Value)} / {Account.N0(basicLimit.Value)}"));
            }
            var bonusLimit = Num(PackQuota(main), "bonus_usage_limit");
            var bonusUsed = Num(PackUsage(main), "bonus_usage_amount");
            if (bonusLimit != null && bonusLimit > 0 && bonusUsed != null)
                rows.Add(new InfoRow("Bonus usage", $"{Account.N0(bonusUsed.Value)} / {Account.N0(bonusLimit.Value)}"));

            var reset = Time.Parse(Num(main, "end_time") ?? Num(PackQuota(main), "end_time"))
                        ?? Time.Parse(Num(main.Obj("entitlement_base_info"), "end_time"));
            if (reset != null) rows.Add(new InfoRow("Renews", Account.FmtDate(reset)));
            return Tuple.Create(windows, rows, plan);
        }

        public static Tuple<string, InfoRow> ParsePayStatus(JObj response)
        {
            if (response == null) return Tuple.Create((string)null, (InfoRow)null);
            var plan = Pick(response, new[] { "user_pay_identity_str" });
            var renew = Time.Parse(Num(response.Obj("detail"), "subscription_renew_time"));
            return Tuple.Create(plan, renew != null ? new InfoRow("Renews", Account.FmtDate(renew)) : null);
        }

        // ------------------------------------------------------------ provider

        public override bool Detect() => StoragePaths().Any(p => Proc.SafeFileExists(p.Item1));

        private Tuple<JObj, string> PostAny(string token, string[] hosts, string path, string body)
        {
            var problems = new List<string>();
            foreach (var host in hosts)
            {
                var req = new HttpRequest { Method = "POST", Url = host + path, Body = body };
                req.Headers["Authorization"] = "Cloud-IDE-JWT " + token;
                req.Headers["Accept"] = "application/json";
                req.Headers["Content-Type"] = "application/json";
                req.Headers["User-Agent"] = "Trae/1.0.0 (quota-tray)";
                HttpReply resp;
                try { resp = Http.Send(req); }
                catch (Exception e) { problems.Add($"{host}: {e.Message}"); continue; }
                if (resp.Status >= 400) { problems.Add($"{host}: HTTP {resp.Status}"); continue; }
                var data = Json.ParseObject(resp.Text);
                if (data == null) { problems.Add($"{host}: not JSON"); continue; }
                var code = data.Num("code");
                if (code != null && code != 0) { problems.Add($"{host}: code {code}"); continue; }
                return Tuple.Create(data, (string)null);
            }
            return Tuple.Create((JObj)null, string.Join("; ", problems));
        }

        private ProviderResult Page(string token, string email, bool isCn, string label, DateTimeOffset? fetchedAt)
        {
            var page = new ProviderResult(Id, Name) { FetchedAt = fetchedAt, Label = label };
            var tag = $"pay API ({label})";
            var hosts = isCn ? CnHosts : GlobalHosts;
            var usage = PostAny(token, hosts, EntUsagePath, "{\"require_usage\":true}");
            var status = PostAny(token, hosts, PayStatusPath, "{}");
            if (usage.Item1 == null && status.Item1 == null)
            {
                var detail = usage.Item2 ?? status.Item2 ?? "no response";
                if (detail.Contains("HTTP 401") || detail.Contains("HTTP 403")) detail += " (open TRAE to refresh its login)";
                page.Attempts.Add(new SourceAttempt(tag, false, detail));
                return page;
            }
            var parsed = ParseUsage(usage.Item1 ?? new JObj());
            var pay = ParsePayStatus(status.Item1);
            page.Windows = parsed.Item1;
            page.Plan = pay.Item1 ?? parsed.Item3;
            page.Info = parsed.Item2.ToList();
            if (pay.Item2 != null && !page.Info.Any(r => r.Label == "Renews")) page.Info.Add(pay.Item2);
            page.Account = email;
            page.Ok = page.Windows.Count > 0 || page.Plan != null;
            page.Source = hosts[0].Replace("https://", "");
            page.Status = page.Ok ? "connected" : "signed in, but no usage reported";
            page.DataTime = Time.Now;
            page.Attempts.Add(new SourceAttempt(tag, page.Ok, label));
            return page;
        }

        public override void Collect(ProviderResult result)
        {
            var pages = new List<ProviderResult>();
            var found = 0;
            foreach (var entry in StoragePaths())
            {
                if (!Proc.SafeFileExists(entry.Item1)) continue;
                var auth = ReadStorageAuth(entry.Item1);
                if (auth.Item1 == null)
                {
                    result.Attempts.Add(new SourceAttempt($"login ({entry.Item3})", false, $"{entry.Item1}: no access token (sign in to TRAE)"));
                    continue;
                }
                found++;
                var page = Page(auth.Item1, auth.Item2, entry.Item2, entry.Item3, result.FetchedAt);
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
            if (!result.Installed) result.Status = "TRAE not detected";
            else if (found == 0) result.Status = "no TRAE login found (sign in to TRAE)";
            else
            {
                var failed = result.Attempts.Where(a => !a.Ok && a.Name.StartsWith("pay API")).ToList();
                result.Status = failed.Count > 0 ? failed.Last().Detail : "could not read TRAE usage";
            }
        }
    }
}
