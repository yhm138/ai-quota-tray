using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using QuotaTray.Core;
using QuotaTray.Providers;
using QuotaTray.Update;
using QuotaTray.Win;

namespace QuotaTray.UI
{
    /// <summary>Tray icon, background refresh, update checks and the panel.</summary>
    public sealed class TrayApp : ApplicationContext
    {
        private readonly Config _config;
        private readonly List<Provider> _providers;
        private List<ProviderResult> _results;
        private DateTimeOffset? _lastRefresh;
        private Release _update;
        private bool _updating;
        private string _notifiedTag;
        private readonly ManualResetEvent _stop = new ManualResetEvent(false);
        private readonly AutoResetEvent _wake = new AutoResetEvent(false);
        private readonly NotifyIcon _icon;
        private readonly QuotaPanel _panel;
        private readonly ToolStripMenuItem _updateItem, _remindItem, _autostartItem;
        private Icon _currentIcon;

        public TrayApp(bool manual, string updatedFrom)
        {
            _config = Config.Load();
            _providers = Provider.Build(_config);
            _results = LoadCached();
            _panel = new QuotaPanel(new PanelCallbacks
            {
                Refresh = RequestRefresh,
                Quit = Quit,
                OpenConfig = () => Autostart.Open(Paths.ConfigFile),
                DiagnosticsText = DiagnosticsText,
                OpenDiagnostics = () =>
                {
                    WriteDiagFile();
                    Autostart.Open(Paths.DiagFile);
                },
            });
            var _ = _panel.Handle;                 // so BeginInvoke works before the first show
            _panel.UserScale = (float)_config.Number("panel_scale", 1.0);

            var menu = new ContextMenuStrip();
            var show = new ToolStripMenuItem("Show quota panel", null, (s, e) => ShowPanel("usage")) { Font = new Font(menu.Font, FontStyle.Bold) };
            menu.Items.Add(show);
            menu.Items.Add("Refresh now", null, (s, e) => RequestRefresh());
            menu.Items.Add(new ToolStripSeparator());
            _updateItem = new ToolStripMenuItem("Update", null, (s, e) => ApplyUpdate()) { Visible = false };
            menu.Items.Add(_updateItem);
            menu.Items.Add("Check for updates", null, (s, e) => CheckForUpdatesManually());
            _remindItem = new ToolStripMenuItem("Remind me about unused resets", null, (s, e) => ToggleReminders());
            menu.Items.Add(_remindItem);
            _autostartItem = new ToolStripMenuItem("Run at login", null, (s, e) => ToggleAutostart());
            menu.Items.Add(_autostartItem);
            menu.Items.Add("Diagnostics", null, (s, e) => ShowPanel("diagnostics"));
            menu.Items.Add("Open config file", null, (s, e) => Autostart.Open(Paths.ConfigFile));
            menu.Items.Add("Open log file", null, (s, e) => Autostart.Open(Paths.LogFile));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("Quit", null, (s, e) => Quit());
            menu.Opening += (s, e) =>
            {
                _remindItem.Checked = _config.Flag("remind_unused_resets", true);
                _autostartItem.Checked = Autostart.IsEnabled;
                _updateItem.Visible = _update != null;
                _updateItem.Text = _update != null ? $"Update to {_update.Tag} and restart" : "Update";
            };

            _icon = new NotifyIcon { ContextMenuStrip = menu, Text = $"{AppInfo.Name} {AppInfo.Version}" };
            _icon.MouseClick += (s, e) =>
            {
                if (e.Button == MouseButtons.Left) _panel.Toggle();
            };
            UpdateIcon();
            _icon.Visible = true;

            if (!string.IsNullOrEmpty(updatedFrom))
            {
                Log.Info($"updated from v{updatedFrom} to v{AppInfo.Version}");
                _panel.SetNotice($"Updated from v{updatedFrom} to v{AppInfo.Version}.", "good");
            }
            PushToPanel();

            Start(new Thread(RefreshLoop) { Name = "refresh" });
            Start(new Thread(UpdateLoop) { Name = "update" });
            Start(new Thread(() => Instance.ListenForShow(OnShowRequest, _stop)) { Name = "show" });
            Start(new Thread(TrayPromotionLoop) { Name = "tray" });
            if (manual)
            {
                // Started by hand: show something, since Windows 11 may hide new tray icons.
                var timer = new System.Windows.Forms.Timer { Interval = 1500 };
                timer.Tick += (s, e) =>
                {
                    timer.Dispose();
                    ShowPanel("usage");
                };
                timer.Start();
            }
        }

        private static void Start(Thread t)
        {
            t.IsBackground = true;
            t.Start();
        }

        private void Post(Action a)
        {
            try
            {
                if (_panel.IsHandleCreated && !_panel.IsDisposed) _panel.BeginInvoke(a);
            }
            catch (Exception) { }
        }

        private void ShowPanel(string mode) => _panel.ShowPanel(mode);

        private void OnShowRequest() => Post(() =>
        {
            _panel.ShowPanel("usage");
            Instance.AcknowledgeShow();
        });

        // ------------------------------------------------------------ cache

        private static List<ProviderResult> LoadCached()
        {
            var output = new List<ProviderResult>();
            foreach (var item in (Cache.Load().Arr("results") ?? new List<object>()).OfType<JObj>())
            {
                try { output.Add(ProviderResult.FromCache(item)); }
                catch (Exception) { }
            }
            return output;
        }

        private void SaveCached()
        {
            var o = new JObj();
            o["saved_at"] = Time.Iso(Time.Now);
            o["results"] = _results.Select(r => (object)r.ToCache()).ToList();
            Cache.Save(o);
        }

        // ------------------------------------------------------------ icon

        private void UpdateIcon()
        {
            // The bars are for usage limits; a prepaid balance has no percentage.
            var entries = _results.Where(r => r.Billing != "payg")
                .Select(r => new KeyValuePair<string, double?>(r.ProviderId, r.Ok ? r.WorstPercent : null)).ToList();
            if (entries.Count == 0)
                entries = _providers.Where(p => p.Billing != "payg").Select(p => new KeyValuePair<string, double?>(p.Id, null)).ToList();
            var size = SystemInformation.SmallIconSize.Width;
            using (var bmp = IconRenderer.Render(entries, Math.Max(16, size), _config.Number("warn_percent", 75),
                       _config.Number("danger_percent", 90), _config.Text("icon_style", "bars")))
            {
                var icon = IconRenderer.ToIcon(bmp);
                _icon.Icon = icon;
                _currentIcon?.Dispose();
                _currentIcon = icon;
            }
            var bits = new List<string>();
            foreach (var r in _results)
            {
                if (!r.Ok)
                {
                    bits.Add($"{r.Name}: {Clip(r.Status, 16)}");
                    continue;
                }
                var top = r.SortedWindows().Take(2).ToList();
                bits.Add(top.Count == 0 && r.Headline != null ? $"{r.Name}: {r.Headline}"
                    : $"{r.Name}: " + string.Join(" / ", top.Select(w => w.PercentText)));
            }
            if (bits.Count == 0) bits.Add("loading...");
            // NotifyIcon.Text is limited to 63 characters on .NET Framework.
            _icon.Text = Clip("QuotaTray\n" + string.Join("\n", bits), 63);
        }

        private static string Clip(string s, int n) => (s ?? "").Length <= n ? s ?? "" : s.Substring(0, n);

        private void TrayPromotionLoop()
        {
            if (!_config.Flag("promote_tray_icon", true)) return;
            // Explorer writes the per-icon entry a moment after the icon appears.
            for (var i = 0; i < 40; i++)
            {
                if (_stop.WaitOne(3000)) return;
                var how = TrayPromotion.Promote(Autostart.ExePath);
                if (how == "no entry yet") continue;
                Log.Info("tray icon visibility: " + how);
                return;
            }
        }

        // ------------------------------------------------------------ refresh

        private void RequestRefresh() => _wake.Set();

        private void RefreshLoop()
        {
            var handles = new WaitHandle[] { _stop, _wake };
            while (true)
            {
                try { RefreshOnce(); }
                catch (Exception e) { Log.Error("refresh failed: " + e); }
                if (WaitHandle.WaitAny(handles, TimeSpan.FromSeconds(_config.RefreshSeconds)) == 0) return;
            }
        }

        public static List<ProviderResult> FetchAll(List<Provider> providers)
        {
            var tasks = providers.Select(p => Task.Run(() =>
            {
                try { return p.Fetch(); }
                catch (Exception e)
                {
                    Log.Error($"{p.Id} fetch crashed: {e}");
                    return new ProviderResult(p.Id, p.Name) { FetchedAt = Time.Now, Status = "internal error: " + e.Message };
                }
            })).ToArray();
            Task.WaitAll(tasks);
            return tasks.Select(t => t.Result).ToList();
        }

        private void RefreshOnce()
        {
            var results = FetchAll(_providers);
            var hide = _config.Flag("hide_not_installed", true);
            var visible = results.Where(r => r.Installed || r.Ok || !hide).ToList();
            _results = visible.Count > 0 ? visible : results;
            _lastRefresh = Time.Now;
            SaveCached();
            WriteDiagFile();
            try { MaybeRemindResets(); }
            catch (Exception e) { Log.Error("reset reminder failed: " + e); }
            Post(() =>
            {
                try { UpdateIcon(); }
                catch (Exception e) { Log.Debug("failed to update the tray icon: " + e); }
                PushToPanel();
            });
        }

        private void PushToPanel()
        {
            _panel.SetData(_results, new PanelMeta
            {
                Warn = _config.Number("warn_percent", 75),
                Danger = _config.Number("danger_percent", 90),
                Subtitle = _lastRefresh != null ? "updated " + Time.HumanizeAge(_lastRefresh) : "",
                Footer = $"v{AppInfo.Version} C# - every {_config.RefreshSeconds / 60} min" + (_update != null ? $" - {_update.Tag} available" : ""),
            });
        }

        private void MaybeRemindResets()
        {
            if (!_config.Flag("remind_unused_resets", true)) return;
            var now = DateTime.Now;
            var hour = (int)_config.Number("reset_reminder_hour", 10);
            if (!Reminders.Due(_config.Data.Str("last_reset_reminder"), now, hour)) return;
            var text = Reminders.UnusedResetsText(_results);
            if (text == null) return;
            Log.Info("reminding about unused resets: " + text.Replace("\n", " | "));
            Notify(text);
            _config.Data["last_reset_reminder"] = now.ToString("yyyy-MM-dd");
            _config.Save();
        }

        private void Notify(string text) => Post(() =>
        {
            try { _icon.ShowBalloonTip(10000, AppInfo.Name, text, ToolTipIcon.Info); }
            catch (Exception e) { Log.Debug("tray notification failed: " + e.Message); }
        });

        private void ToggleReminders()
        {
            _config.Data["remind_unused_resets"] = !_config.Flag("remind_unused_resets", true);
            _config.Save();
        }

        private void ToggleAutostart()
        {
            if (Autostart.IsEnabled) Autostart.Disable();
            else Autostart.Enable();
        }

        // ------------------------------------------------------------ updates

        private string Repo => _config.Text("update_repo", Updater.DefaultRepo);

        private void UpdateLoop()
        {
            if (_stop.WaitOne(TimeSpan.FromSeconds(90))) return;
            while (true)
            {
                if (_config.Flag("check_updates", true))
                {
                    try { CheckUpdate(false); }
                    catch (Exception e) { Log.Error("update check failed: " + e); }
                }
                if (_stop.WaitOne(TimeSpan.FromHours(24))) return;
            }
        }

        private void CheckForUpdatesManually()
        {
            _panel.SetNotice("Checking for updates...");
            ShowPanel("usage");
            Task.Run(() =>
            {
                try { CheckUpdate(true); }
                catch (Exception e) { Log.Error("update check failed: " + e); }
            });
        }

        private void CheckUpdate(bool manual)
        {
            var latest = Updater.FetchLatest(Repo);
            var found = latest.Item1 != null && Updater.IsNewer(latest.Item1.Tag, AppInfo.Version) ? latest.Item1 : null;
            if (found != null) _update = found;
            Log.Info("update check: " + (found?.Tag ?? latest.Item2 ?? "up to date"));
            Post(PushToPanel);
            // The panel carries the answer: Windows 11 often swallows tray notifications.
            if (found != null)
            {
                Post(() => _panel.SetNotice($"{AppInfo.Name} {found.Tag} is available (you have v{AppInfo.Version}).", "good",
                    "Update now", ApplyUpdate));
                if (manual) Post(() => ShowPanel("usage"));
                else if (_notifiedTag != found.Tag)
                {
                    _notifiedTag = found.Tag;
                    Notify($"{AppInfo.Name} {found.Tag} is available. Right-click the tray icon and choose \"Update\".");
                }
            }
            else if (manual && latest.Item2 != null)
                Post(() => _panel.SetNotice("Could not check for updates: " + latest.Item2, "warn"));
            else if (manual)
            {
                var text = $"You are on the latest version (v{AppInfo.Version}).";
                Post(() => _panel.SetNotice(text, "good"));
                Task.Delay(8000).ContinueWith(_ => Post(() =>
                {
                    if (_panel.NoticeText == text) _panel.SetNotice(null);
                }));
            }
        }

        /// <summary>Download, verify and install while this version keeps running; only then restart.</summary>
        private void ApplyUpdate()
        {
            var release = _update;
            if (release == null || _updating) return;
            _updating = true;
            ShowPanel("usage");
            Task.Run(() =>
            {
                ProcessStartInfo next;
                try
                {
                    next = Updater.Install(release.Tag, Repo, Autostart.ExePath, (step, total, text, fraction) =>
                    {
                        var overall = (step - 1 + (fraction ?? 0.0)) / total;
                        var line = $"Updating to {release.Tag} - step {step}/{total}: {text}";
                        Post(() => _panel.UpdateProgress(line, overall));
                    });
                    Post(() => _panel.UpdateProgress($"Updating to {release.Tag} - step 5/5: Restarting into {release.Tag}...", 1.0));
                    Log.Info($"update to {release.Tag} installed; starting it");
                    Process.Start(next);
                }
                catch (Exception e)
                {
                    Log.Error("update failed: " + e);
                    _updating = false;
                    var why = e.Message;
                    Post(() => _panel.SetNotice($"Update failed, still running v{AppInfo.Version}: {why}\nDetails: {Paths.LogFile}",
                        "warn", "Retry", ApplyUpdate,
                        more: new List<KeyValuePair<string, Action>>
                        {
                            new KeyValuePair<string, Action>("Open log", () => Autostart.Open(Paths.LogFile)),
                        }));
                    return;
                }
                Thread.Sleep(600);
                Post(Quit);
            });
        }

        // ------------------------------------------------------------ diagnostics

        public string DiagnosticsText()
        {
            var lines = new List<string> { $"{AppInfo.Name} v{AppInfo.Version} ({AppInfo.Edition} edition)", "", "SUMMARY" };
            if (_results.Count == 0) lines.Add("  (still collecting, try again in a moment)");
            foreach (var r in _results)
            {
                if (r.Ok)
                {
                    lines.Add($"  {r.Name}: OK via {r.Source ?? "?"}");
                    continue;
                }
                lines.Add($"  {r.Name}: NOT WORKING");
                foreach (var a in r.Attempts.Where(a => !a.Ok)) lines.Add($"    - {a.Name}: {Clip(a.Detail, 220)}");
            }
            lines.Add("");
            lines.Add("DETAILS");
            lines.Add($".NET Framework {Environment.Version} on {Environment.OSVersion}");
            lines.Add($"Config file: {Paths.ConfigFile}");
            lines.Add($"Log file:    {Paths.LogFile}");
            lines.Add($"Run at login: {(Autostart.IsEnabled ? "enabled" : "disabled")}");
            lines.Add($"Launch cmd:   {Autostart.LaunchCommand}");
            lines.Add("");
            lines.AddRange(Report.Details(_results));
            return string.Join("\n", lines);
        }

        private void WriteDiagFile()
        {
            try { File.WriteAllText(Paths.DiagFile, DiagnosticsText(), new UTF8Encoding(false)); }
            catch (Exception) { }
        }

        // ------------------------------------------------------------ stop

        private void Quit()
        {
            _stop.Set();
            _wake.Set();
            try
            {
                _icon.Visible = false;
                _icon.Dispose();
            }
            catch (Exception) { }
            try { _panel.Close(); }
            catch (Exception) { }
            ExitThread();
        }
    }

    /// <summary>Text reports shared by the panel's Diagnostics and --diagnose.</summary>
    public static class Report
    {
        public static List<string> Details(List<ProviderResult> results)
        {
            var lines = new List<string>();
            if (results.Count == 0) lines.Add("(no results collected yet)");
            foreach (var r in results)
            {
                lines.Add($"-- {r.Name} --" + (r.Alternates.Count > 0 && r.Label != null ? $" [{r.Label}]" : ""));
                lines.Add($"   installed: {(r.Installed ? "yes" : "no")}");
                lines.Add($"   status:    {(string.IsNullOrEmpty(r.Status) ? "-" : r.Status)}");
                lines.Add($"   source:    {r.Source ?? "none"}");
                if (r.DataTime != null) lines.Add($"   data time: {r.DataTime.Value.ToLocalTime():yyyy-MM-dd HH:mm}");
                foreach (var a in r.Attempts) lines.Add($"   [{(a.Ok ? "OK  " : "FAIL")}] {a.Name}: {a.Detail}");
                AddPage(lines, r);
                var n = 2;
                foreach (var alt in r.Alternates)
                {
                    lines.Add($"   == account page {n++}: {alt.Label ?? "?"} ({alt.Account ?? "unknown"}) ==");
                    lines.Add($"   source:    {alt.Source ?? "none"}");
                    AddPage(lines, alt);
                }
                lines.Add("");
            }
            return lines;
        }

        private static void AddPage(List<string> lines, ProviderResult r)
        {
            foreach (var w in r.SortedWindows())
            {
                var reset = Time.HumanizeDelta(w.ResetsAt);
                lines.Add($"   * {w.Label}: {w.PercentText}" + (reset.Length > 0 ? $"  (resets in {reset})" : ""));
            }
            foreach (var row in r.Info) lines.Add($"   {(row.Label.Length > 0 ? row.Label : " ")}: {row.Value}");
            foreach (var g in r.Resets)
            {
                var exp = g.ExpiresAt != null ? g.ExpiresAt.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm") : "?";
                lines.Add($"   reset x{g.Count}, expires {exp}" + (g.Note.Length > 0 ? $" ({g.Note})" : ""));
            }
        }

        /// <summary>The --once / --diagnose console report.</summary>
        public static List<string> Console(List<ProviderResult> results, bool diagnose)
        {
            var lines = new List<string>();
            foreach (var r in results)
            {
                lines.Add("");
                lines.Add($"=== {r.Name}" + (r.Plan != null ? $" [{r.Plan}]" : "") + " ===");
                lines.Add($"  status: {(string.IsNullOrEmpty(r.Status) ? (r.Ok ? "connected" : "unavailable") : r.Status)}");
                if (r.Source != null) lines.Add($"  source: {r.Source}");
                if (r.Billing == "payg")
                    foreach (var row in r.Info) lines.Add($"  {(row.Label.Length > 0 ? row.Label : " "),-18} {row.Value}");
                foreach (var w in r.SortedWindows())
                {
                    var reset = Time.HumanizeDelta(w.ResetsAt);
                    lines.Add($"  {w.Label,-18} {w.PercentText,6}" + (w.Detail != null ? "  " + w.Detail : "")
                              + (reset.Length > 0 ? $"   resets in {reset}" : ""));
                }
                if (diagnose)
                    foreach (var a in r.Attempts) lines.Add($"    [{(a.Ok ? "OK" : "FAIL")}] {a.Name}: {a.Detail}");
            }
            lines.Add("");
            return lines;
        }
    }
}
