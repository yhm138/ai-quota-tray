using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using QuotaTray.Core;

namespace QuotaTray.Win
{
    public sealed class ProcResult
    {
        public int Code;
        public string Out = "";
        public string Err = "";
    }

    /// <summary>Run helper programs without flashing a console window.</summary>
    public static class Proc
    {
        public static bool IsWindows => Environment.OSVersion.Platform == PlatformID.Win32NT;

        public static ProcResult Run(string exe, string args, int timeoutSeconds = 20, Encoding encoding = null)
        {
            var result = new ProcResult();
            var psi = new ProcessStartInfo(exe, args)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WindowStyle = ProcessWindowStyle.Hidden,
            };
            if (encoding != null)
            {
                psi.StandardOutputEncoding = encoding;
                psi.StandardErrorEncoding = encoding;
            }
            try
            {
                using (var p = Process.Start(psi))
                {
                    var outTask = Task.Run(() => p.StandardOutput.ReadToEnd());
                    var errTask = Task.Run(() => p.StandardError.ReadToEnd());
                    if (!p.WaitForExit(timeoutSeconds * 1000))
                    {
                        try { p.Kill(); } catch (Exception) { }
                        result.Code = 124;
                        result.Err = "timed out";
                        return result;
                    }
                    p.WaitForExit();
                    result.Code = p.ExitCode;
                    result.Out = outTask.Wait(5000) ? outTask.Result : "";
                    result.Err = errTask.Wait(5000) ? errTask.Result : "";
                }
            }
            catch (System.ComponentModel.Win32Exception)
            {
                result.Code = 127;
                result.Err = "command not found";
            }
            catch (Exception e)
            {
                result.Code = 1;
                result.Err = e.Message;
            }
            return result;
        }

        /// <summary>Run a PowerShell snippet and parse its output as JSON; null on failure.</summary>
        public static object PowerShellJson(string script, int timeoutSeconds = 25)
        {
            if (!IsWindows) return null;
            var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n" + script));
            var r = Run("powershell.exe", "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + encoded,
                timeoutSeconds, Encoding.UTF8);
            var text = (r.Out ?? "").Trim();
            if (text.Length == 0) return null;
            return Json.TryParse(text, out var v) ? v : null;
        }

        private static List<string> _wslCache;
        private static DateTime _wslAt;
        private static readonly object WslGate = new object();

        /// <summary>Installed WSL distros, cached for 10 minutes.</summary>
        public static List<string> WslDistros()
        {
            if (!IsWindows) return new List<string>();
            lock (WslGate)
            {
                if (_wslCache != null && (DateTime.UtcNow - _wslAt).TotalSeconds < 600) return _wslCache.ToList();
                var names = new List<string>();
                var system = Environment.GetFolderPath(Environment.SpecialFolder.System);
                var wsl = Path.Combine(system, "wsl.exe");
                if (File.Exists(wsl))
                {
                    var r = Run(wsl, "-l -q", 8, Encoding.Unicode);
                    if (r.Code == 0)
                        foreach (var line in r.Out.Replace("\0", "").Split('\n'))
                        {
                            var name = line.Trim().Trim('\ufeff');
                            if (name.Length > 0) names.Add(name);
                        }
                }
                _wslCache = names.Take(6).ToList();
                _wslAt = DateTime.UtcNow;
                return _wslCache.ToList();
            }
        }

        /// <summary>Electron app data folders, including Microsoft Store (MSIX) ones.</summary>
        public static List<string> AppRoots(string appFolder, IEnumerable<string> extra = null)
        {
            var roots = new List<string>();
            // Explicit folders first (a portable install the user pointed us at).
            // The cookie store may be in the folder itself or a "User Data" subdir.
            if (extra != null)
                foreach (var raw in extra)
                {
                    var text = (raw ?? "").Trim();
                    if (text.Length == 0) continue;
                    roots.Add(text);
                    roots.Add(Path.Combine(text, "User Data"));
                }
            var appdata = Environment.GetEnvironmentVariable("APPDATA");
            var local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            if (!string.IsNullOrEmpty(appdata)) roots.Add(Path.Combine(appdata, appFolder));
            if (!string.IsNullOrEmpty(local)) roots.Add(Path.Combine(local, appFolder));
            if (!string.IsNullOrEmpty(local))
            {
                try
                {
                    var pkgs = Path.Combine(local, "Packages");
                    if (Directory.Exists(pkgs))
                        foreach (var pkg in Directory.GetDirectories(pkgs, "*" + appFolder + "*").OrderBy(p => p, StringComparer.Ordinal))
                            foreach (var sub in new[] { "Roaming", "Local" })
                                roots.Add(Path.Combine(pkg, "LocalCache", sub, appFolder));
                }
                catch (Exception) { }
            }
            roots.Add(Path.Combine(Paths.Home, ".config", appFolder));
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            return roots.Where(SafeDirExists).Where(r => seen.Add(Path.GetFullPath(r))).ToList();
        }

        /// <summary>User-data dirs of the common Chromium browsers that exist here.</summary>
        public static List<string> BrowserRoots()
        {
            var specs = new List<string>();
            var local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            var appdata = Environment.GetEnvironmentVariable("APPDATA");
            if (!string.IsNullOrEmpty(local))
                specs.AddRange(new[]
                {
                    Path.Combine(local, "Microsoft", "Edge", "User Data"),
                    Path.Combine(local, "Google", "Chrome", "User Data"),
                    Path.Combine(local, "Google", "Chrome Beta", "User Data"),
                    Path.Combine(local, "BraveSoftware", "Brave-Browser", "User Data"),
                    Path.Combine(local, "Chromium", "User Data"),
                    Path.Combine(local, "Vivaldi", "User Data"),
                });
            if (!string.IsNullOrEmpty(appdata))
                specs.Add(Path.Combine(appdata, "Opera Software", "Opera Stable"));
            if (!IsWindows)
            {
                var cfg = Path.Combine(Paths.Home, ".config");
                specs.AddRange(new[]
                {
                    Path.Combine(cfg, "microsoft-edge"), Path.Combine(cfg, "google-chrome"),
                    Path.Combine(cfg, "BraveSoftware", "Brave-Browser"), Path.Combine(cfg, "chromium"),
                });
            }
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            return specs.Where(SafeDirExists).Where(p => seen.Add(Path.GetFullPath(p))).ToList();
        }

        public static bool SafeDirExists(string p)
        {
            try { return Directory.Exists(p); } catch (Exception) { return false; }
        }

        public static bool SafeFileExists(string p)
        {
            try { return File.Exists(p); } catch (Exception) { return false; }
        }
    }
}
