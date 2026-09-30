using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32;
using QuotaTray.Core;

namespace QuotaTray.Win
{
    /// <summary>Run-at-login via HKCU\Software\Microsoft\Windows\CurrentVersion\Run (the same value the Python build uses).</summary>
    public static class Autostart
    {
        private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";

        public static string ExePath => Process.GetCurrentProcess().MainModule?.FileName ?? "";

        // --autostart: started at login, so stay quietly in the tray.
        public static string LaunchCommand => $"\"{ExePath}\" --autostart";

        public static string Registered()
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(RunKey))
                    return key?.GetValue(AppInfo.Name) as string;
            }
            catch (Exception) { return null; }
        }

        public static bool IsEnabled => !string.IsNullOrEmpty(Registered());

        public static bool Enable()
        {
            try
            {
                using (var key = Registry.CurrentUser.CreateSubKey(RunKey))
                    key.SetValue(AppInfo.Name, LaunchCommand, RegistryValueKind.String);
                Log.Info("run-at-login enabled: " + LaunchCommand);
                return true;
            }
            catch (Exception e)
            {
                Log.Warn("failed to write the autostart registry value: " + e.Message);
                return false;
            }
        }

        public static bool Disable()
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(RunKey, true))
                    key?.DeleteValue(AppInfo.Name, false);
                Log.Info("run-at-login disabled");
                return true;
            }
            catch (Exception e)
            {
                Log.Warn("failed to delete the autostart registry value: " + e.Message);
                return false;
            }
        }

        public static void Open(string path)
        {
            try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
            catch (Exception e) { Log.Warn($"failed to open {path}: {e.Message}"); }
        }
    }

    /// <summary>One QuotaTray at a time, shared with the Python build.</summary>
    public static class Instance
    {
        // Same names as the Python build, so either edition sees the other.
        private const string MutexName = @"Local\QuotaTray-singleton-2";
        private const string ShowEventName = @"Local\QuotaTray-show";
        private static Mutex _mutex;

        public static bool Acquire()
        {
            try
            {
                var m = new Mutex(false, MutexName, out var created);
                if (!created)
                {
                    m.Dispose();
                    return false;
                }
                _mutex = m;
                return true;
            }
            catch (Exception) { return true; }
        }

        private const string AckEventName = @"Local\QuotaTray-shown";

        /// <summary>
        /// Ask the running QuotaTray to open its panel. "acked" when it answered
        /// within the wait (v1.3.6+ of either edition), "sent" when it did not,
        /// "none" when nothing listens.
        /// </summary>
        public static string SignalRunning(int waitMs)
        {
            try
            {
                if (!EventWaitHandle.TryOpenExisting(ShowEventName, out var ev)) return "none";
                using (ev)
                using (var ack = new EventWaitHandle(false, EventResetMode.AutoReset, AckEventName))
                {
                    ack.Reset();
                    if (!ev.Set()) return "none";
                    return ack.WaitOne(waitMs) ? "acked" : "sent";
                }
            }
            catch (Exception) { return "none"; }
        }

        /// <summary>Tell a new launch that asked for the panel that we are alive.</summary>
        public static void AcknowledgeShow()
        {
            try
            {
                if (EventWaitHandle.TryOpenExisting(AckEventName, out var ack))
                    using (ack) ack.Set();
            }
            catch (Exception) { }
        }

        /// <summary>Calls onShow whenever another launch signals us, until stop is set.</summary>
        public static void ListenForShow(Action onShow, ManualResetEvent stop)
        {
            EventWaitHandle ev;
            try { ev = new EventWaitHandle(false, EventResetMode.AutoReset, ShowEventName); }
            catch (Exception) { return; }
            using (ev)
            {
                var handles = new WaitHandle[] { ev, stop };
                while (WaitHandle.WaitAny(handles) == 0) onShow();
            }
        }

        public static void WaitForExit(int pid, int timeoutMs = 30000)
        {
            try
            {
                using (var p = Process.GetProcessById(pid)) p.WaitForExit(timeoutMs);
            }
            catch (Exception) { }
        }

        /// <summary>QuotaTray exes (either edition) running from another folder.</summary>
        public static List<Process> OtherCopies(bool includeOwnDir = false)
        {
            var output = new List<Process>();
            var me = Process.GetCurrentProcess();
            var ownDir = Path.GetDirectoryName(Autostart.ExePath) ?? "";
            foreach (var p in Process.GetProcesses())
            {
                try
                {
                    if (p.Id == me.Id || !p.ProcessName.StartsWith("QuotaTray", StringComparison.OrdinalIgnoreCase)) continue;
                    var dir = Path.GetDirectoryName(p.MainModule?.FileName ?? "");
                    if (string.IsNullOrEmpty(dir)) continue;
                    if (!includeOwnDir && string.Equals(dir.TrimEnd('\\'), ownDir.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                        continue;
                    output.Add(p);
                }
                catch (Exception) { }
            }
            return output;
        }

        /// <summary>Stop copies started from another folder (or anywhere, for a hung one).</summary>
        public static int RetireOtherCopies(bool includeOwnDir = false)
        {
            var stopped = 0;
            foreach (var p in OtherCopies(includeOwnDir))
            {
                try
                {
                    var path = p.MainModule?.FileName;
                    p.Kill();
                    p.WaitForExit(5000);
                    Log.Info($"stopped the copy in {path} (pid {p.Id})");
                    stopped++;
                }
                catch (Exception e) { Log.Info($"could not stop pid {p.Id}: {e.Message}"); }
            }
            return stopped;
        }

        public static bool TakeOver(bool includeOwnDir = false)
        {
            if (RetireOtherCopies(includeOwnDir) == 0) return false;
            for (var i = 0; i < 10; i++)
            {
                if (Acquire()) return true;
                Thread.Sleep(500);
            }
            Log.Warn("the other copy did not exit in time");
            return false;
        }
    }

    /// <summary>
    /// Windows 11 puts every icon from a new exe path under the ^ overflow.
    /// Mark ours "show on the taskbar" in HKCU\Control Panel\NotifyIconSettings,
    /// but only while the user has not chosen for it.
    /// </summary>
    public static class TrayPromotion
    {
        private const string Root = @"Control Panel\NotifyIconSettings";

        [DllImport("shell32.dll")]
        private static extern int SHGetKnownFolderPath([MarshalAs(UnmanagedType.LPStruct)] Guid id, int flags, IntPtr token, out IntPtr path);

        [DllImport("ole32.dll")]
        private static extern void CoTaskMemFree(IntPtr p);

        public static string KnownFolder(string guid)
        {
            try
            {
                if (SHGetKnownFolderPath(new Guid(guid), 0, IntPtr.Zero, out var p) != 0) return null;
                try { return Marshal.PtrToStringUni(p); }
                finally { CoTaskMemFree(p); }
            }
            catch (Exception) { return null; }
        }

        /// <summary>Explorer stores paths under known folders as {GUID}\rest.</summary>
        public static string ExpandPath(string stored, Func<string, string> resolve)
        {
            var m = System.Text.RegularExpressions.Regex.Match(stored ?? "", @"^(\{[0-9A-Fa-f-]{36}\})\\(.*)$");
            if (m.Success)
            {
                var b = resolve(m.Groups[1].Value);
                if (!string.IsNullOrEmpty(b)) return Path.Combine(b, m.Groups[2].Value);
            }
            return stored ?? "";
        }

        public static bool SamePath(string a, string b) =>
            string.Equals(a.Replace('/', '\\').TrimEnd('\\'), b.Replace('/', '\\').TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);

        public static string Promote(string exe)
        {
            try
            {
                using (var root = Registry.CurrentUser.OpenSubKey(Root))
                {
                    if (root == null) return "no entry yet";
                    var found = 0;
                    var changed = 0;
                    foreach (var name in root.GetSubKeyNames())
                    {
                        using (var key = root.OpenSubKey(name, true))
                        {
                            var path = key?.GetValue("ExecutablePath") as string;
                            if (path == null || !SamePath(ExpandPath(path, KnownFolder), exe)) continue;
                            found++;
                            if (key.GetValue("IsPromoted") != null) continue;     // the user already decided
                            key.SetValue("IsPromoted", 1, RegistryValueKind.DWord);
                            changed++;
                        }
                    }
                    if (found == 0) return "no entry yet";
                    return changed > 0 ? $"promoted {changed} entr{(changed == 1 ? "y" : "ies")}" : "left as the user set it";
                }
            }
            catch (Exception e) { return "failed: " + e.Message; }
        }
    }
}
