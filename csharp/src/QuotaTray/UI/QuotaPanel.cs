using System;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using QuotaTray.Core;

namespace QuotaTray.UI
{
    public sealed class PanelMeta
    {
        public double Warn = 75;
        public double Danger = 90;
        public string Subtitle = "";
        public string Footer = "";
    }

    public sealed class PanelCallbacks
    {
        public Action Refresh;
        public Action Quit;
        public Action OpenConfig;
        public Func<string> DiagnosticsText;
        public Action OpenDiagnostics;
    }

    /// <summary>The tray popup: a borderless dark card pinned above the tray, painted by hand.</summary>
    public sealed class QuotaPanel : Form
    {
        private const int BaseWidth = 430;
        private const int DiagWidth = 620;
        private const int Pad = 16;

        private sealed class Notice
        {
            public string Text, Tone, ActionText;
            public Action Action;
            public double? Progress;             // 0..1, or -1 for "working" with no fraction
            public List<KeyValuePair<string, Action>> More = new List<KeyValuePair<string, Action>>();
        }

        private sealed class Hit
        {
            public Rectangle Rect;
            public Action Action;
            public bool Primary;
        }

        private readonly PanelCallbacks _cb;
        private List<ProviderResult> _results = new List<ProviderResult>();
        private PanelMeta _meta = new PanelMeta();
        private Notice _notice;
        private readonly Dictionary<string, int> _page = new Dictionary<string, int>();
        private readonly List<Hit> _hits = new List<Hit>();
        private Hit _hover;
        private readonly TextBox _diag;
        private string _mode = "usage";
        private Font _fTitle, _fBody, _fSmall, _fPct, _fMono;
        private float _scale = 1f;
        private int _dpi;

        /// <summary>Extra size factor from config.json ("panel_scale").</summary>
        public float UserScale = 1f;

        public string Mode => _mode;
        public string NoticeText => _notice?.Text;

        public QuotaPanel(PanelCallbacks callbacks)
        {
            _cb = callbacks;
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = FormStartPosition.Manual;
            BackColor = Theme.Bg;
            KeyPreview = true;
            DoubleBuffered = true;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint, true);
            Text = AppInfo.Name;
            _diag = new TextBox
            {
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Vertical,
                BorderStyle = BorderStyle.None,
                BackColor = Theme.BgCard,
                ForeColor = Theme.FgDim,
                WordWrap = true,
                Visible = false,
                TabStop = false,
            };
            Controls.Add(_diag);
            MakeFonts();
            KeyDown += (s, e) =>
            {
                if (e.KeyCode == Keys.Escape) Hide();
            };
            Deactivate += (s, e) => Hide();
        }

        protected override CreateParams CreateParams
        {
            get
            {
                var cp = base.CreateParams;
                cp.ExStyle |= 0x80;                   // WS_EX_TOOLWINDOW: no Alt-Tab entry
                cp.ClassStyle |= 0x20000;             // CS_DROPSHADOW
                return cp;
            }
        }

        private int S(float v) => (int)Math.Round(v * _scale);

        [DllImport("user32.dll")]
        private static extern IntPtr MonitorFromPoint(Point pt, int flags);

        [DllImport("shcore.dll")]
        private static extern int GetDpiForMonitor(IntPtr monitor, int type, out uint dpiX, out uint dpiY);

        /// <summary>
        /// The real DPI of the monitor the panel opens on. Form.DeviceDpi
        /// stays at 96 on .NET Framework unless an app.config opts in, which
        /// drew the panel at 100% size on a 125% / 150% screen.
        /// </summary>
        private static int DpiAt(Point pt)
        {
            try
            {
                var mon = MonitorFromPoint(pt, 2);                  // MONITOR_DEFAULTTONEAREST
                if (GetDpiForMonitor(mon, 0, out var x, out _) == 0 && x > 0) return (int)x;   // MDT_EFFECTIVE_DPI
            }
            catch (Exception) { }                                    // before Windows 8.1
            try
            {
                using (var g = Graphics.FromHwnd(IntPtr.Zero)) return (int)Math.Round(g.DpiX);
            }
            catch (Exception) { return 96; }
        }

        private void MakeFonts()
        {
            _dpi = DpiAt(Cursor.Position);
            _scale = _dpi / 96f * Math.Max(0.5f, Math.Min(3f, UserScale));
            foreach (var f in new[] { _fTitle, _fBody, _fSmall, _fPct, _fMono }) f?.Dispose();
            _fTitle = new Font("Segoe UI", 15 * _scale, FontStyle.Bold, GraphicsUnit.Pixel);
            _fBody = new Font("Segoe UI", 12 * _scale, GraphicsUnit.Pixel);
            _fSmall = new Font("Segoe UI", 11 * _scale, GraphicsUnit.Pixel);
            _fPct = new Font("Consolas", 13 * _scale, FontStyle.Bold, GraphicsUnit.Pixel);
            _fMono = new Font("Consolas", 12 * _scale, GraphicsUnit.Pixel);
            _diag.Font = _fMono;
        }

        protected override void OnDpiChanged(DpiChangedEventArgs e)
        {
            base.OnDpiChanged(e);
            _dpi = 0;
            Render();
        }

        // ------------------------------------------------------------ state

        public void SetData(List<ProviderResult> results, PanelMeta meta)
        {
            _results = results ?? new List<ProviderResult>();
            _meta = meta ?? new PanelMeta();
            if (Visible) Render();
        }

        public void SetNotice(string text, string tone = "", string actionText = null, Action action = null,
            double? progress = null, List<KeyValuePair<string, Action>> more = null)
        {
            _notice = string.IsNullOrEmpty(text) ? null : new Notice
            {
                Text = text, Tone = tone ?? "", ActionText = actionText, Action = action, Progress = progress,
                More = more ?? new List<KeyValuePair<string, Action>>(),
            };
            if (Visible) Render();
        }

        /// <summary>Change a progress notice in place.</summary>
        public void UpdateProgress(string text, double? progress)
        {
            _notice = new Notice { Text = text, Progress = progress ?? -1.0 };
            if (Visible)
            {
                var before = Height;
                Render();
                if (Height != before) Place();
            }
        }

        public void ShowPanel(string mode = null)
        {
            if (mode != null && mode != _mode)
            {
                _mode = mode;
                _diag.Text = "";
            }
            Render();
            Show();
            Activate();
        }

        public void Toggle()
        {
            if (Visible) Hide();
            else ShowPanel();
        }

        // ------------------------------------------------------------ layout + paint

        private void Render()
        {
            // Opened on another monitor, or the scale setting changed: re-measure.
            if (_dpi != DpiAt(Cursor.Position) || Math.Abs(_scale - _dpi / 96f * Math.Max(0.5f, Math.Min(3f, UserScale))) > 0.001f)
                MakeFonts();
            var width = S(_mode == "diagnostics" ? DiagWidth : BaseWidth);
            int height;
            using (var g = CreateGraphics()) height = LayoutPanel(g, width, false);
            var area = Screen.FromPoint(Cursor.Position).WorkingArea;
            height = Math.Min(height, area.Height - S(20));
            Size = new Size(width, height);
            if (_mode == "diagnostics")
            {
                string text;
                try { text = _cb.DiagnosticsText(); }
                catch (Exception e) { text = "could not build the diagnostics report: " + e.Message; }
                text = text.Replace("\r\n", "\n").Replace("\n", "\r\n");
                if (_diag.Text != text)
                {
                    var first = FirstVisibleLine(_diag);
                    _diag.Text = text;
                    ScrollToLine(_diag, first);
                }
                _diag.Visible = true;
            }
            else _diag.Visible = false;
            Place();
            Invalidate();
        }

        private void Place()
        {
            var area = Screen.FromPoint(Cursor.Position).WorkingArea;
            Location = new Point(Math.Max(area.Left + 8, area.Right - Width - S(12)), Math.Max(area.Top + 8, area.Bottom - Height - S(12)));
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.Clear(Theme.Bg);
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            LayoutPanel(g, Width, true);
            using (var pen = new Pen(Theme.Border)) g.DrawRectangle(pen, 0, 0, Width - 1, Height - 1);
        }

        private static readonly TextFormatFlags One = TextFormatFlags.NoPadding | TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix;
        private static readonly TextFormatFlags Wrap = TextFormatFlags.NoPadding | TextFormatFlags.WordBreak | TextFormatFlags.NoPrefix;

        private static Size Measure(Graphics g, string text, Font font) =>
            string.IsNullOrEmpty(text) ? new Size(0, font.Height) : TextRenderer.MeasureText(g, text, font, new Size(int.MaxValue, int.MaxValue), One);

        private static Size MeasureWrapped(Graphics g, string text, Font font, int width) =>
            string.IsNullOrEmpty(text) ? new Size(0, font.Height) : TextRenderer.MeasureText(g, text, font, new Size(Math.Max(10, width), int.MaxValue), Wrap);

        private static void Draw(Graphics g, bool draw, string text, Font font, Color color, int x, int y)
        {
            if (draw && !string.IsNullOrEmpty(text)) TextRenderer.DrawText(g, text, font, new Point(x, y), color, One);
        }

        private static void DrawWrapped(Graphics g, bool draw, string text, Font font, Color color, Rectangle r)
        {
            if (draw && !string.IsNullOrEmpty(text)) TextRenderer.DrawText(g, text, font, r, color, Wrap);
        }

        private static string Ellipsis(string text, int limit)
        {
            text = text ?? "";
            return text.Length <= limit ? text : text.Substring(0, limit - 3).TrimEnd() + "...";
        }

        private void AddHit(bool draw, Rectangle r, Action a, bool primary = false)
        {
            if (draw) _hits.Add(new Hit { Rect = r, Action = a, Primary = primary });
        }

        /// <summary>Lays out (and with draw, paints) the whole panel; returns its height.</summary>
        private int LayoutPanel(Graphics g, int width, bool draw)
        {
            if (draw) _hits.Clear();
            var pad = S(Pad);
            var y = pad;

            // header
            var title = _mode == "diagnostics" ? "Diagnostics" : "Quota overview";
            Draw(g, draw, title, _fTitle, Theme.Fg, pad, y);
            var sub = _meta.Subtitle ?? "";
            var subSize = Measure(g, sub, _fSmall);
            Draw(g, draw, sub, _fSmall, Theme.FgFaint, width - pad - subSize.Width, y + S(4));
            y += Measure(g, title, _fTitle).Height + S(2);

            if (_notice != null && _mode == "usage") y = NoticeBar(g, draw, width, y + S(6));
            y += S(4);

            if (_mode == "diagnostics")
            {
                var h = S(420);
                var box = new Rectangle(pad, y + S(4), width - 2 * pad, h);
                if (draw) using (var b = new SolidBrush(Theme.BgCard)) g.FillRectangle(b, box);
                var inner = Rectangle.Inflate(box, -S(10), -S(8));
                if (_diag.Bounds != inner) _diag.Bounds = inner;
                y = box.Bottom;
            }
            else if (_results.Count == 0)
            {
                y += S(20);
                Draw(g, draw, "Loading...", _fBody, Theme.FgDim, pad, y);
                y += _fBody.Height + S(20);
            }
            else
            {
                foreach (var r in _results) y = Card(g, draw, width, y + S(6), r) + S(6);
            }

            // footer
            y += S(8);
            var x = pad;
            var buttons = new List<Tuple<string, Action, bool>> { Tuple.Create("Refresh", _cb.Refresh, true) };
            if (_mode == "diagnostics")
            {
                buttons.Add(Tuple.Create<string, Action, bool>("Back", () => ShowPanel("usage"), false));
                if (_cb.OpenDiagnostics != null) buttons.Add(Tuple.Create("Open as text", _cb.OpenDiagnostics, false));
            }
            else buttons.Add(Tuple.Create<string, Action, bool>("Diagnostics", () => ShowPanel("diagnostics"), false));
            buttons.Add(Tuple.Create("Config", _cb.OpenConfig, false));
            buttons.Add(Tuple.Create("Quit", _cb.Quit, false));
            var bh = 0;
            foreach (var b in buttons)
            {
                var r = Button(g, draw, b.Item1, x, y, b.Item3, b.Item2);
                x = r.Right + S(6);
                bh = r.Height;
            }
            var foot = _meta.Footer ?? "";
            var fs = Measure(g, foot, _fSmall);
            if (x + fs.Width < width - pad)
                Draw(g, draw, foot, _fSmall, Theme.FgFaint, width - pad - fs.Width, y + (bh - fs.Height) / 2 + S(2));
            y += bh + pad;
            return y;
        }

        private Rectangle Button(Graphics g, bool draw, string text, int x, int y, bool primary, Action action, Color? bgOverride = null)
        {
            var size = Measure(g, text, _fSmall);
            var r = new Rectangle(x, y, size.Width + S(20), size.Height + S(10));
            if (draw)
            {
                var hovered = _hover != null && _hover.Rect == r;
                var bg = bgOverride ?? (primary ? Theme.Accent : hovered ? Theme.BgRow : Theme.BgCard);
                using (var b = new SolidBrush(bg)) g.FillRectangle(b, r);
                Draw(g, true, text, _fSmall, primary ? Color.White : hovered ? Theme.Fg : Theme.FgDim, x + S(10), y + S(5));
            }
            AddHit(draw, r, action, primary);
            return r;
        }

        private int NoticeBar(Graphics g, bool draw, int width, int y)
        {
            var n = _notice;
            var pad = S(Pad);
            var color = n.Tone == "good" ? Theme.Ok : n.Tone == "warn" ? Theme.Warn : Theme.FgDim;
            var inner = width - 2 * pad - S(20);
            var closeW = Measure(g, "x", _fSmall).Width + S(16);
            var textSize = MeasureWrapped(g, n.Text, _fBody, inner - closeW);
            var h = S(8) + textSize.Height + S(8);
            if (n.Progress != null) h += S(4) + S(8);
            var buttons = new List<KeyValuePair<string, Action>>();
            if (n.ActionText != null && n.Action != null) buttons.Add(new KeyValuePair<string, Action>(n.ActionText, n.Action));
            buttons.AddRange(n.More);
            var btnH = Measure(g, "X", _fSmall).Height + S(10);
            if (buttons.Count > 0) h += btnH + S(8);
            var box = new Rectangle(pad, y, width - 2 * pad, h);
            if (draw) using (var b = new SolidBrush(Theme.BgRow)) g.FillRectangle(b, box);
            DrawWrapped(g, draw, n.Text, _fBody, color, new Rectangle(pad + S(10), y + S(8), inner - closeW, textSize.Height + 2));
            var closeRect = new Rectangle(box.Right - closeW, y + S(4), closeW, _fSmall.Height + S(8));
            Draw(g, draw, "x", _fSmall, Theme.FgFaint, closeRect.X + S(8), closeRect.Y + S(4));
            AddHit(draw, closeRect, () => SetNotice(null));
            var cy = y + S(8) + textSize.Height + S(8);
            if (n.Progress != null)
            {
                var track = new Rectangle(pad + S(10), cy, box.Width - S(20), S(4));
                if (draw)
                {
                    using (var b = new SolidBrush(Theme.BgCard)) g.FillRectangle(b, track);
                    if (n.Progress >= 0)
                        using (var b = new SolidBrush(Theme.Accent))
                            g.FillRectangle(b, track.X, track.Y, (int)(track.Width * Math.Min(1.0, n.Progress.Value)), track.Height);
                }
                cy += S(4) + S(8);
            }
            var x = pad + S(10);
            for (var i = 0; i < buttons.Count; i++)
            {
                var r = Button(g, draw, buttons[i].Key, x, cy, i == 0, buttons[i].Value);
                x = r.Right + S(6);
            }
            return box.Bottom;
        }

        private int Card(Graphics g, bool draw, int width, int y, ProviderResult product)
        {
            var pages = product.Pages();
            var index = (_page.TryGetValue(product.ProviderId, out var p) ? p : 0) % pages.Count;
            var result = pages[index];
            var pad = S(Pad);
            var left = pad + S(12);
            var right = width - pad - S(12);
            var top = y;
            var cy = y + S(10);
            var titleH = _fTitle.Height;

            // Background first: paint the card after measuring its height.
            var height = CardBody(g, false, product, result, pages, index, left, right, cy) - top + S(10);
            if (draw)
            {
                using (var b = new SolidBrush(Theme.BgCard)) g.FillRectangle(b, new Rectangle(pad, top, width - 2 * pad, height));
                CardBody(g, true, product, result, pages, index, left, right, cy);
            }
            return top + height;
        }

        private int CardBody(Graphics g, bool draw, ProviderResult product, ProviderResult result, List<ProviderResult> pages,
            int index, int left, int right, int y)
        {
            var titleH = _fTitle.Height;
            var dot = S(8);
            if (draw) using (var b = new SolidBrush(Theme.Tint(result.ProviderId))) g.FillEllipse(b, left, y + (titleH - dot) / 2, dot, dot);
            var x = left + dot + S(6);
            Draw(g, draw, result.Name, _fTitle, Theme.Fg, x, y);
            x += Measure(g, result.Name, _fTitle).Width + S(10);
            if (pages.Count > 1)
            {
                foreach (var step in new[] { -1, 0, 1 })
                {
                    if (step == 0)
                    {
                        var t = $"{index + 1}/{pages.Count}";
                        Draw(g, draw, t, _fSmall, Theme.FgFaint, x + S(2), y + S(3));
                        x += Measure(g, t, _fSmall).Width + S(4);
                        continue;
                    }
                    var arrow = step < 0 ? "\u2039" : "\u203a";
                    var size = Measure(g, arrow, _fBody);
                    var r = new Rectangle(x, y + S(1), size.Width + S(12), size.Height + S(2));
                    if (draw)
                    {
                        using (var b = new SolidBrush(Theme.BgRow)) g.FillRectangle(b, r);
                        Draw(g, true, arrow, _fBody, Theme.Fg, x + S(6), y + S(2));
                    }
                    var id = product.ProviderId;
                    var count = pages.Count;
                    AddHit(draw, r, () => Flip(id, step, count));
                    x = r.Right + S(2);
                }
            }
            var metaBits = new[] { result.Plan, result.Account }.Where(s => !string.IsNullOrEmpty(s)).ToList();
            if (metaBits.Count > 0)
            {
                var text = Ellipsis(string.Join(" - ", metaBits), pages.Count > 1 ? 24 : 34);
                var size = Measure(g, text, _fSmall);
                Draw(g, draw, text, _fSmall, Theme.FgFaint, Math.Max(x + S(6), right - size.Width), y + S(4));
            }
            y += titleH;

            if (pages.Count > 1 && !string.IsNullOrEmpty(result.Label))
            {
                y += S(2);
                Draw(g, draw, result.Label, _fSmall, Theme.FgDim, left, y);
                y += _fSmall.Height;
            }

            if (!result.Ok)
            {
                y += S(6);
                var text = string.IsNullOrEmpty(result.Status) ? "unavailable" : result.Status;
                var size = MeasureWrapped(g, text, _fSmall, right - left);
                DrawWrapped(g, draw, text, _fSmall, Theme.FgDim, new Rectangle(left, y, right - left, size.Height + 2));
                return y + size.Height;
            }

            // usage rows: label | bar | percent | trailing
            y += S(8);
            var windows = result.SortedWindows();
            var labelW = windows.Select(w => Measure(g, Ellipsis(w.Label, 24), _fBody).Width).DefaultIfEmpty(0).Max();
            var pctW = Measure(g, "100%", _fPct).Width;
            var trailing = windows.Select(w => w.Detail ?? Time.HumanizeDelta(w.ResetsAt)).ToList();
            var trailW = trailing.Select(t => Measure(g, t, _fSmall).Width).DefaultIfEmpty(0).Max();
            var barX = left + labelW + S(12);
            var barRight = right - trailW - S(6) - pctW - S(8);
            if (barRight - barX < S(60)) barX = barRight - S(60);
            var rowH = Math.Max(_fBody.Height, _fPct.Height) + S(6);
            for (var i = 0; i < windows.Count; i++)
            {
                var w = windows[i];
                var cy = y + (rowH - _fBody.Height) / 2;
                Draw(g, draw, Ellipsis(w.Label, 24), _fBody, Theme.FgDim, left, cy);
                if (draw)
                {
                    var bar = new RectangleF(barX, y + (rowH - S(8)) / 2f, barRight - barX, S(8));
                    Theme.FillRounded(g, Theme.BgRow, bar, S(4));
                    if (w.Percent != null)
                    {
                        var filled = Math.Max(bar.Height, (float)(bar.Width * Math.Min(100.0, w.Percent.Value) / 100.0));
                        Theme.FillRounded(g, Theme.StatusColor(w.Percent, _meta.Warn, _meta.Danger),
                            new RectangleF(bar.X, bar.Y, filled, bar.Height), S(4));
                    }
                }
                var pct = w.PercentText;
                var ps = Measure(g, pct, _fPct);
                Draw(g, draw, pct, _fPct, Theme.StatusColor(w.Percent, _meta.Warn, _meta.Danger),
                    barRight + S(8) + pctW - ps.Width, y + (rowH - _fPct.Height) / 2);
                var ts = Measure(g, trailing[i], _fSmall);
                Draw(g, draw, trailing[i], _fSmall, Theme.FgFaint, right - ts.Width, y + (rowH - _fSmall.Height) / 2);
                y += rowH;
            }

            // plan, subscription, credits, resets
            var where = result.ProviderId == "claude" ? "Claude > Settings > Usage"
                : result.ProviderId == "codex" ? "Codex > Settings > Usage" : "the app's usage settings";
            var rows = result.Info.Concat(Account.ResetRows(result.Resets, where)).ToList();
            if (rows.Count > 0)
            {
                y += S(8);
                var lw = rows.Select(r => Measure(g, r.Label, _fSmall).Width).DefaultIfEmpty(0).Max() + S(10);
                foreach (var row in rows)
                {
                    Draw(g, draw, row.Label, _fSmall, Theme.FgFaint, left, y);
                    var color = row.Tone == "good" ? Theme.Ok : row.Tone == "warn" ? Theme.Warn : Theme.FgDim;
                    var size = MeasureWrapped(g, row.Value, _fSmall, right - left - lw);
                    DrawWrapped(g, draw, row.Value, _fSmall, color, new Rectangle(left + lw, y, right - left - lw, size.Height + 2));
                    y += size.Height + S(2);
                }
            }

            var foot = new List<string>();
            if (!string.IsNullOrEmpty(result.Source)) foot.Add(result.Source);
            if (result.DataTime != null && (Time.Now - result.DataTime.Value).TotalSeconds > 900)
                foot.Add("data " + Time.HumanizeAge(result.DataTime));
            if (foot.Count > 0)
            {
                y += S(8);
                Draw(g, draw, Ellipsis(string.Join(" - ", foot), 58), _fSmall, Theme.FgFaint, left, y);
                y += _fSmall.Height;
            }
            return y;
        }

        private void Flip(string id, int step, int count)
        {
            var cur = _page.TryGetValue(id, out var p) ? p : 0;
            _page[id] = ((cur + step) % count + count) % count;
            Render();
        }

        // ------------------------------------------------------------ mouse

        private Hit HitAt(Point pt) => _hits.LastOrDefault(h => h.Rect.Contains(pt));

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            var h = HitAt(e.Location);
            Cursor = h != null ? Cursors.Hand : Cursors.Default;
            if (h != _hover)
            {
                _hover = h;
                Invalidate();
            }
        }

        protected override void OnMouseLeave(EventArgs e)
        {
            base.OnMouseLeave(e);
            if (_hover != null)
            {
                _hover = null;
                Invalidate();
            }
        }

        protected override void OnMouseClick(MouseEventArgs e)
        {
            base.OnMouseClick(e);
            if (e.Button != MouseButtons.Left) return;
            var h = HitAt(e.Location);
            if (h == null) return;
            try { h.Action?.Invoke(); }
            catch (Exception ex) { Log.Error("panel action failed: " + ex); }
        }

        // ------------------------------------------------------------ keep the diagnostics scroll position

        [DllImport("user32.dll")]
        private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

        private static int FirstVisibleLine(TextBox box) =>
            box.IsHandleCreated ? (int)SendMessage(box.Handle, 0x00CE, IntPtr.Zero, IntPtr.Zero) : 0;   // EM_GETFIRSTVISIBLELINE

        private static void ScrollToLine(TextBox box, int line)
        {
            if (!box.IsHandleCreated || line <= 0) return;
            SendMessage(box.Handle, 0x00B6, IntPtr.Zero, (IntPtr)line);                               // EM_LINESCROLL
        }
    }
}
