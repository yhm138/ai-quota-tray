using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using QuotaTray.Core;
using QuotaTray.Providers;
using QuotaTray.UI;
using QuotaTray.Update;
using QuotaTray.Win;

namespace QuotaTray
{
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            AppDomain.CurrentDomain.UnhandledException += (s, e) => Crash(e.ExceptionObject as Exception);
            Application.ThreadException += (s, e) => Crash(e.Exception);
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
            try
            {
                return Run(args.ToList());
            }
            catch (Exception e)
            {
                Crash(e);
                return 1;
            }
        }

        private static string ArgAfter(List<string> args, string name) =>
            args.IndexOf(name) is var i && i >= 0 && i + 1 < args.Count ? args[i + 1] : null;

        private static int Run(List<string> args)
        {
            Log.Verbose = args.Contains("--verbose");
            if (args.Contains("--selftest")) return SelfTest();
            if (args.Contains("--once") || args.Contains("--diagnose")) return Diagnose(args.Contains("--diagnose"));

            if (int.TryParse(ArgAfter(args, "--wait-pid"), out var waitPid))
                Instance.WaitForExit(waitPid);               // a self-restart: let the old copy exit first
            var manual = !args.Contains("--autostart");
            Log.Info($"launch: v{AppInfo.Version} (C#) from {Autostart.ExePath}, args {(args.Count > 0 ? string.Join(" ", args) : "none")}");

            if (!Instance.Acquire())
            {
                if (args.Contains("--no-takeover") || !Instance.TakeOver())
                {
                    var answer = Instance.SignalRunning(8000);
                    if (answer == "acked")
                    {
                        Log.Info("already running: that copy opened its panel");
                        return 0;
                    }
                    if (!manual || args.Contains("--no-takeover"))
                    {
                        Log.Info($"already running ({answer}); leaving it be");
                        return 0;
                    }
                    // No answer: a hung copy holds the lock. The user just started
                    // this one, so replace it instead of quietly exiting.
                    Log.Warn($"the running copy did not answer ({answer}); replacing it");
                    if (!Instance.TakeOver(true))
                    {
                        MessageBoxSafe("QuotaTray is already running but does not respond.\n\n" +
                                       "End QuotaTray in Task Manager, then start it again.", false);
                        return 0;
                    }
                }
            }
            Log.Info($"{AppInfo.Name} v{AppInfo.Version} (C#) starting");
            Task.Run(() => Instance.RetireOtherCopies());

            if (!args.Contains("--no-autostart"))
            {
                var cfg = Config.Load();
                if (!cfg.Flag("autostart_initialized", false))
                {
                    // Enable run-at-login on the first run only; after that the menu wins.
                    Autostart.Enable();
                    cfg.Data["autostart_initialized"] = true;
                    cfg.Save();
                }
                else if (Autostart.IsEnabled && Autostart.Registered() != Autostart.LaunchCommand)
                {
                    // Run-at-login points at another copy: the one running now takes it over.
                    Log.Info("run-at-login pointed at " + Autostart.Registered() + ", moving it here");
                    Autostart.Enable();
                }
            }
            // Delete what an update left behind (the previous exe), on every start.
            Task.Run(() => Updater.CleanupAfterUpdate(Path.GetDirectoryName(Autostart.ExePath)));

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new TrayApp(manual, ArgAfter(args, "--updated-from")));
            return 0;
        }

        // ------------------------------------------------------------ console modes

        [DllImport("kernel32.dll")]
        private static extern bool AttachConsole(int pid);

        /// <summary>Print when started from a console; otherwise save the report and open it.</summary>
        private static void Emit(List<string> lines)
        {
            var text = string.Join(Environment.NewLine, lines);
            try { File.WriteAllText(Paths.DiagFile, text, new UTF8Encoding(false)); }
            catch (Exception) { }
            if (AttachConsole(-1))
            {
                try
                {
                    var stdout = new StreamWriter(Console.OpenStandardOutput()) { AutoFlush = true };
                    stdout.WriteLine();
                    stdout.WriteLine(text);
                    return;
                }
                catch (Exception) { }
            }
            // QUOTATRAY_NO_OPEN keeps the CI smoke test from opening Notepad.
            if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("QUOTATRAY_NO_OPEN")))
                Autostart.Open(Paths.DiagFile);
        }

        private static int Diagnose(bool diagnose)
        {
            Log.Verbose = true;
            var results = TrayApp.FetchAll(Provider.Build(Config.Load()));
            Emit(Report.Console(results, diagnose));
            return 0;
        }

        /// <summary>--selftest: prove this machine has what the app needs; exit 1 if not.</summary>
        private static int SelfTest()
        {
            var failures = new List<string>();
            void Step(string name, Action a)
            {
                try { a(); }
                catch (Exception e) { failures.Add($"{name}: {e.GetType().Name}: {e.Message}"); }
            }
            Step("AES-GCM", AesGcmDecryptor.SelfTest);
            Step("tray icon", () =>
            {
                using (var bmp = IconRenderer.Render(new List<KeyValuePair<string, double?>>
                       {
                           new KeyValuePair<string, double?>("claude", 42.0),
                           new KeyValuePair<string, double?>("codex", 91.0),
                       }, 32))
                using (var icon = IconRenderer.ToIcon(bmp))
                {
                    if (icon.Width != 32) throw new InvalidOperationException("wrong icon size " + icon.Width);
                    if (IconRenderer.ToIco(new[] { bmp }).Length < 100) throw new InvalidOperationException("empty ICO");
                }
            });
            Step("JSON", () =>
            {
                var o = Json.ParseObject("{\"a\":[1,2.5,\"x\\u00e9\"],\"b\":{\"c\":null,\"d\":true}}");
                if (Json.Write(o) != "{\"a\":[1,2.5,\"x\u00e9\"],\"b\":{\"c\":null,\"d\":true}}") throw new InvalidOperationException(Json.Write(o));
            });
            Step("config", () => new Config().Provider("claude").Arr("order").Count.ToString());
            Step("panel", () =>
            {
                using (var p = new QuotaPanel(new PanelCallbacks { DiagnosticsText = () => "" }))
                {
                    p.SetData(new List<ProviderResult>(), new PanelMeta());
                    var _ = p.Handle;
                }
            });
            Emit(failures.Count > 0 ? failures : new List<string> { "selftest OK" });
            return failures.Count > 0 ? 1 : 0;
        }

        // ------------------------------------------------------------ crashes

        private static void Crash(Exception e)
        {
            var report = e?.ToString() ?? "unknown error";
            Log.Error("QuotaTray crashed:\n" + report);
            try { File.AppendAllText(Paths.CrashFile, $"--- {DateTime.Now:s} v{AppInfo.Version} (C#)\n{report}\n"); }
            catch (Exception) { }
            MessageBoxSafe($"QuotaTray v{AppInfo.Version} ran into a problem.\n\n{e?.Message}\n\nDetails: {Paths.CrashFile}", true);
            Environment.Exit(1);
        }

        private static void MessageBoxSafe(string text, bool error)
        {
            // A modal box would hang unattended runs (the CI smoke test).
            if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("QUOTATRAY_NO_OPEN"))) return;
            try
            {
                MessageBox.Show(text, AppInfo.Name, MessageBoxButtons.OK, error ? MessageBoxIcon.Error : MessageBoxIcon.Information,
                    MessageBoxDefaultButton.Button1, MessageBoxOptions.DefaultDesktopOnly);
            }
            catch (Exception) { }
        }
    }
}
