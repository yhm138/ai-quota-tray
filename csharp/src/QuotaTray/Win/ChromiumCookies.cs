using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using QuotaTray.Core;

namespace QuotaTray.Win
{
    /// <summary>
    /// Read a cookie out of an Electron/Chromium cookie store: the value is
    /// AES-256-GCM encrypted with the OSCrypt key, which sits in "Local State"
    /// wrapped with DPAPI.
    /// </summary>
    public static class ChromiumCookies
    {
        /// <summary>The OSCrypt key from Local State, unwrapped with DPAPI.</summary>
        public static byte[] MasterKey(string localState)
        {
            var data = Json.ParseObject(File.ReadAllText(localState, Encoding.UTF8))
                       ?? throw new CryptoError("cannot read Local State");
            var encoded = data.Obj("os_crypt")?.Str("encrypted_key");
            if (string.IsNullOrEmpty(encoded)) throw new CryptoError("Local State has no os_crypt.encrypted_key");
            var blob = Convert.FromBase64String(encoded);
            if (blob.Length >= 5 && Encoding.ASCII.GetString(blob, 0, 5) == "DPAPI") blob = blob.Skip(5).ToArray();
            return Dpapi.Unprotect(blob);
        }

        public static string FindLocalState(string root)
        {
            var p = Path.Combine(root, "Local State");
            return File.Exists(p) ? p : null;
        }

        /// <summary>Every Cookies DB under an app data dir (partitions included), newest first.</summary>
        public static List<string> FindCookieDbs(string root, int limit = 8)
        {
            var found = new List<string>();
            void Add(string p) { if (File.Exists(p) && !found.Contains(p)) found.Add(p); }
            Add(Path.Combine(root, "Network", "Cookies"));
            Add(Path.Combine(root, "Cookies"));
            foreach (var pattern in new[] { Path.Combine("Partitions", "*", "Network", "Cookies"),
                                            Path.Combine("Partitions", "*", "Cookies") })
            {
                try
                {
                    var parts = pattern.Split(Path.DirectorySeparatorChar);
                    var partitionsDir = Path.Combine(root, "Partitions");
                    if (!Directory.Exists(partitionsDir)) continue;
                    foreach (var dir in Directory.GetDirectories(partitionsDir))
                    {
                        var candidate = parts.Length == 4 ? Path.Combine(dir, "Network", "Cookies") : Path.Combine(dir, "Cookies");
                        Add(candidate);
                    }
                }
                catch (Exception) { }
            }
            return found.Take(limit).OrderByDescending(p => { try { return File.GetLastWriteTimeUtc(p); } catch (Exception) { return DateTime.MinValue; } }).ToList();
        }

        private static string DecryptValue(byte[] raw, byte[] key)
        {
            if (raw == null || raw.Length == 0) return "";
            if (raw.Length > 3 && (Encoding.ASCII.GetString(raw, 0, 3) == "v10" || Encoding.ASCII.GetString(raw, 0, 3) == "v11"))
            {
                if (key == null) return "";
                var nonce = raw.Skip(3).Take(12).ToArray();
                var sealedData = raw.Skip(15).ToArray();
                byte[] plain;
                try { plain = AesGcmDecryptor.Decrypt(key, nonce, sealedData); }
                catch (CryptoError) { return ""; }
                // Newer stores (schema v24+) prefix a 32-byte SHA-256 of the host.
                if (plain.Length > 32 && !Printable(plain, 0, 32) && Printable(plain, 32, plain.Length - 32))
                    return Encoding.UTF8.GetString(plain, 32, plain.Length - 32);
                return Encoding.UTF8.GetString(plain);
            }
            try { return Encoding.UTF8.GetString(Dpapi.Unprotect(raw)); }   // legacy DPAPI value
            catch (Exception) { return ""; }
        }

        private static bool Printable(byte[] b, int off, int len)
        {
            for (var i = off; i < off + len && i < b.Length; i++)
                if (b[i] != 0x09 && (b[i] < 0x20 || b[i] > 0x7E)) return false;
            return true;
        }

        /// <summary>Cookies for hostContains from a DB: {name: value}.</summary>
        public static Dictionary<string, string> ReadCookies(string db, string hostContains, byte[] key)
        {
            var output = new Dictionary<string, string>(StringComparer.Ordinal);
            using (var sql = Sqlite.Open(db))
            {
                if (!sql.Tables().TryGetValue("cookies", out var table)) return output;
                var host = table.Columns.FindIndex(c => c.Equals("host_key", StringComparison.OrdinalIgnoreCase));
                var name = table.Columns.FindIndex(c => c.Equals("name", StringComparison.OrdinalIgnoreCase));
                var enc = table.Columns.FindIndex(c => c.Equals("encrypted_value", StringComparison.OrdinalIgnoreCase));
                var plainVal = table.Columns.FindIndex(c => c.Equals("value", StringComparison.OrdinalIgnoreCase));
                if (host < 0 || name < 0) return output;
                foreach (var row in sql.ReadTable(table.RootPage))
                {
                    var v = row.Values;
                    var hk = At(v, host) as string;
                    var nm = At(v, name) as string;
                    if (hk == null || nm == null || hk.IndexOf(hostContains, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    var value = "";
                    if (enc >= 0 && At(v, enc) is byte[] blob && blob.Length > 0) value = DecryptValue(blob, key);
                    if (string.IsNullOrEmpty(value) && plainVal >= 0) value = At(v, plainVal) as string ?? "";
                    if (!string.IsNullOrEmpty(value)) output[nm] = value;
                }
            }
            return output;
        }

        private static object At(List<object> v, int i) => i >= 0 && i < v.Count ? v[i] : null;

        /// <summary>Every cookie for hostContains from the first jar that has `required`.</summary>
        public static Tuple<Dictionary<string, string>, List<string>> GetCookies(string appFolder, string hostContains, string required)
        {
            var notes = new List<string>();
            var roots = Proc.AppRoots(appFolder);
            if (roots.Count == 0)
            {
                notes.Add($"no data directory found for {appFolder}");
                return Tuple.Create(new Dictionary<string, string>(), notes);
            }
            foreach (var root in roots)
            {
                byte[] key = null;
                var ls = FindLocalState(root);
                if (ls != null)
                {
                    try { key = MasterKey(ls); }
                    catch (Exception e) { notes.Add($"{Path.GetFileName(root)}: master key failed ({e.Message})"); }
                }
                else notes.Add($"{Path.GetFileName(root)}: no Local State");
                var dbs = FindCookieDbs(root);
                if (dbs.Count == 0) { notes.Add($"{Path.GetFileName(root)}: no Cookies DB found"); continue; }
                foreach (var db in dbs)
                {
                    Dictionary<string, string> cookies;
                    try { cookies = ReadCookies(db, hostContains, key); }
                    catch (Exception e) { notes.Add($"{Path.GetFileName(Path.GetDirectoryName(db))}/Cookies: {e.Message}"); continue; }
                    if (cookies.ContainsKey(required)) return Tuple.Create(cookies, notes);
                    notes.Add($"{Path.GetFileName(Path.GetDirectoryName(db))}/Cookies: {cookies.Count} cookies but no {required}");
                }
            }
            return Tuple.Create(new Dictionary<string, string>(), notes);
        }
    }
}
