using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using QuotaTray.Core;

namespace QuotaTray.Update
{
    public sealed class Release
    {
        public string Tag;
        public string Url;

        public Release(string tag, string url)
        {
            Tag = tag; Url = url;
        }
    }

    public sealed class UpdateError : Exception
    {
        public UpdateError(string message) : base(message) { }
    }

    /// <summary>
    /// Finds a newer GitHub release and installs it while this version keeps
    /// running: download, verify, then swap the exe (a running exe can be
    /// renamed but not deleted, so it moves into a hidden trash folder that
    /// the new version empties) and start the new one.
    /// </summary>
    public static class Updater
    {
        public const string DefaultRepo = "yhm138/ai-quota-tray";
        public const int Steps = 5;             // check, download, verify, install, restart
        public const string TrashDir = ".quotatray-trash";
        public const string StageDir = ".quotatray-update";

        /// <summary>'v1.2.3' / '1.2' / 'v1.2.3-beta' -> [1, 2, 3]; unparseable -> empty.</summary>
        public static int[] ParseVersion(string text)
        {
            var m = Regex.Match(text ?? "", @"^\s*v?(\d+(?:\.\d+)*)");
            if (!m.Success) return new int[0];
            var parts = m.Groups[1].Value.Split('.').Select(int.Parse).ToList();
            while (parts.Count < 3) parts.Add(0);
            return parts.ToArray();
        }

        public static bool IsNewer(string tag, string current)
        {
            var a = ParseVersion(tag);
            var b = ParseVersion(current);
            if (a.Length == 0 || b.Length == 0) return false;
            for (var i = 0; i < Math.Max(a.Length, b.Length); i++)
            {
                var x = i < a.Length ? a[i] : 0;
                var y = i < b.Length ? b[i] : 0;
                if (x != y) return x > y;
            }
            return false;
        }

        /// <summary>(newest release, null) or (null, why it could not be determined).</summary>
        public static Tuple<Release, string> FetchLatest(string repo)
        {
            var problems = new List<string>();
            try
            {
                var resp = Http.Get($"https://api.github.com/repos/{repo}/releases/latest",
                    new Dictionary<string, string> { { "Accept", "application/vnd.github+json" }, { "User-Agent", "QuotaTray-updater" } }, 15);
                if (resp.Status == 200)
                {
                    var data = Json.ParseObject(resp.Text);
                    var tag = data?.Text("tag_name");
                    if (tag != null) return Tuple.Create(new Release(tag, data.Text("html_url") ?? $"https://github.com/{repo}/releases"), (string)null);
                    problems.Add("GitHub API: no tag in the reply");
                }
                else problems.Add($"GitHub API: HTTP {resp.Status}");
            }
            catch (Exception e) { problems.Add("GitHub API: " + Clip(Http.Innermost(e))); }

            // The API allows 60 anonymous calls an hour; the release page's
            // redirect to /releases/tag/<tag> gives the same answer.
            try
            {
                var resp = Http.Get($"https://github.com/{repo}/releases/latest",
                    new Dictionary<string, string> { { "User-Agent", "QuotaTray-updater" } }, 15, false);
                var where = resp.Status >= 301 && resp.Status <= 308 ? resp.Header("Location") : "";
                var m = Regex.Match(where, "/releases/tag/([^/?#]+)");
                if (m.Success) return Tuple.Create(new Release(m.Groups[1].Value, where), (string)null);
                problems.Add($"github.com: HTTP {resp.Status}");
            }
            catch (Exception e) { problems.Add("github.com: " + Clip(Http.Innermost(e))); }
            Log.Info("update check failed: " + string.Join("; ", problems));
            return Tuple.Create((Release)null, string.Join("; ", problems));
        }

        private static string Clip(string s) => s.Length <= 120 ? s : s.Substring(0, 117) + "...";

        /// <summary>This build's release asset, and the checksum list.</summary>
        public static string AssetName(string tag) => $"QuotaTray-{tag}-csharp-windows-anycpu.exe";

        public static string[] SumsNames(string tag) => new[] { $"QuotaTray-{tag}-SHA256SUMS.txt", "SHA256SUMS.txt" };

        public static Dictionary<string, string> ParseSums(string text)
        {
            var output = new Dictionary<string, string>();
            foreach (var line in text.Split('\n'))
            {
                var m = Regex.Match(line, @"^\s*([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$");
                if (m.Success) output[m.Groups[2].Value] = m.Groups[1].Value.ToLowerInvariant();
            }
            return output;
        }

        public static string Sha256(string path)
        {
            using (var sha = SHA256.Create())
            using (var fs = File.OpenRead(path))
                return BitConverter.ToString(sha.ComputeHash(fs)).Replace("-", "").ToLowerInvariant();
        }

        private static string Mb(long n) => (n / 1048576.0).ToString("0.0", System.Globalization.CultureInfo.InvariantCulture) + " MB";

        private static string FetchFirst(string repo, string tag, IEnumerable<string> names, string destDir, Action<long, long> progress = null)
        {
            var baseUrl = $"https://github.com/{repo}/releases/download/{tag}";
            var tried = new List<string>();
            foreach (var name in names)
            {
                tried.Add(name);
                var dest = Path.Combine(destDir, name);
                if (Http.Download($"{baseUrl}/{name}", dest, progress)) return dest;
            }
            throw new UpdateError($"{tag} has none of: {string.Join(", ", tried)}");
        }

        /// <summary>
        /// Everything short of exiting; returns the command line that starts the
        /// new version. Throws UpdateError with a readable reason, leaving the
        /// running version exactly as it was.
        /// progress(step, total, text, fraction or null)
        /// </summary>
        public static ProcessStartInfo Install(string tag, string repo, string exePath, Action<int, int, string, double?> progress)
        {
            var folder = Path.GetDirectoryName(exePath);
            var pid = Process.GetCurrentProcess().Id;
            if (!CanWrite(folder)) throw new UpdateError($"{folder} is not writable; move QuotaTray to a folder you own");

            progress(1, Steps, $"Checking {tag}...", null);
            var stage = Path.Combine(folder, StageDir);
            try
            {
                Directory.CreateDirectory(stage);
                var sumsFile = FetchFirst(repo, tag, SumsNames(tag), stage);
                var sums = ParseSums(File.ReadAllText(sumsFile));

                var last = DateTime.MinValue;
                var payload = FetchFirst(repo, tag, new[] { AssetName(tag) }, stage, (done, total) =>
                {
                    if ((DateTime.UtcNow - last).TotalMilliseconds < 200 && done < total) return;
                    last = DateTime.UtcNow;
                    if (total > 0) progress(2, Steps, $"Downloading {Mb(done)} / {Mb(total)}", (double)done / total);
                    else progress(2, Steps, $"Downloading {Mb(done)}", null);
                });

                progress(3, Steps, "Verifying the download...", null);
                var name = Path.GetFileName(payload);
                if (!sums.TryGetValue(name, out var want)) throw new UpdateError($"the checksum list has no entry for {name}");
                if (Sha256(payload) != want) throw new UpdateError($"checksum mismatch for {name}");

                progress(4, Steps, "Installing...", null);
                var fresh = exePath + ".new";
                if (File.Exists(fresh)) File.Delete(fresh);
                File.Move(payload, fresh);
                var trash = Path.Combine(folder, TrashDir);
                var dir = Directory.CreateDirectory(trash);
                dir.Attributes |= FileAttributes.Hidden;
                var old = Path.Combine(trash, $"{Path.GetFileName(exePath)}.{pid}");
                if (File.Exists(old)) File.Delete(old);
                File.Move(exePath, old);
                try { File.Move(fresh, exePath); }
                catch (Exception)
                {
                    File.Move(old, exePath);          // put the running version back
                    throw;
                }
            }
            catch (UpdateError) { throw; }
            catch (Exception e) { throw new UpdateError(e.Message); }
            finally
            {
                try { Directory.Delete(stage, true); } catch (Exception) { }
                try { if (File.Exists(exePath + ".new")) File.Delete(exePath + ".new"); } catch (Exception) { }
            }
            return new ProcessStartInfo(exePath, $"--wait-pid {pid} --updated-from {AppInfo.Version}")
            {
                UseShellExecute = false,
                WorkingDirectory = folder,
            };
        }

        private static bool CanWrite(string folder)
        {
            try
            {
                var probe = Path.Combine(folder, $".quotatray-write-test-{Process.GetCurrentProcess().Id}");
                File.WriteAllText(probe, "");
                File.Delete(probe);
                return true;
            }
            catch (Exception) { return false; }
        }

        /// <summary>
        /// Permanently delete what an update left next to the exe (the previous
        /// version in the trash folder, stray downloads). The previous process may
        /// still be exiting, so retry for a while. True when nothing is left.
        /// </summary>
        public static bool CleanupAfterUpdate(string folder, int attempts = 30, int delayMs = 2000)
        {
            List<string> remaining = new List<string>();
            for (var i = 0; i < Math.Max(1, attempts); i++)
            {
                remaining.Clear();
                var leftovers = new List<string> { Path.Combine(folder, TrashDir), Path.Combine(folder, StageDir) };
                try
                {
                    leftovers.AddRange(Directory.GetFiles(folder, "*.exe.old"));
                    leftovers.AddRange(Directory.GetFiles(folder, "*.exe.new"));
                }
                catch (Exception) { }
                foreach (var item in leftovers)
                {
                    try
                    {
                        if (Directory.Exists(item))
                        {
                            foreach (var f in Directory.GetFiles(item, "*", SearchOption.AllDirectories))
                                File.SetAttributes(f, FileAttributes.Normal);
                            Directory.Delete(item, true);
                            Log.Info("removed update leftover " + Path.GetFileName(item));
                        }
                        else if (File.Exists(item))
                        {
                            File.Delete(item);
                            Log.Info("removed update leftover " + Path.GetFileName(item));
                        }
                    }
                    catch (Exception) { remaining.Add(item); }
                }
                if (remaining.Count == 0) return true;
                Thread.Sleep(delayMs);
            }
            Log.Info("could not remove " + string.Join(", ", remaining.Select(Path.GetFileName)) + " yet; will retry next start");
            return false;
        }
    }
}
