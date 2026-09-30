using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;

namespace QuotaTray.UI
{
    public static class Theme
    {
        public static readonly Color Bg = Hex("#17181C");
        public static readonly Color BgCard = Hex("#1F2126");
        public static readonly Color BgRow = Hex("#25272E");
        public static readonly Color Fg = Hex("#ECEDEF");
        public static readonly Color FgDim = Hex("#9AA0A8");
        public static readonly Color FgFaint = Hex("#6C727B");
        public static readonly Color Border = Hex("#32353D");
        public static readonly Color Accent = Hex("#C96442");
        public static readonly Color Ok = Hex("#3FB950");
        public static readonly Color Warn = Hex("#D9A21B");
        public static readonly Color Danger = Hex("#E5534B");
        public static readonly Color Idle = Hex("#4B5059");

        public static readonly Dictionary<string, Color> ProviderTint = new Dictionary<string, Color>
        {
            { "claude", Hex("#C96442") },
            { "codex", Hex("#10A37F") },
            { "antigravity", Hex("#4285F4") },
            { "deepseek", Hex("#4D6BFE") },
        };

        public static Color Tint(string id) => ProviderTint.TryGetValue(id ?? "", out var c) ? c : Accent;

        public static Color Hex(string hex, int alpha = 255)
        {
            var h = hex.TrimStart('#');
            return Color.FromArgb(alpha, Convert.ToInt32(h.Substring(0, 2), 16), Convert.ToInt32(h.Substring(2, 2), 16),
                Convert.ToInt32(h.Substring(4, 2), 16));
        }

        public static Color StatusColor(double? percent, double warn = 75, double danger = 90)
        {
            if (percent == null) return Idle;
            if (percent >= danger) return Danger;
            if (percent >= warn) return Warn;
            return Ok;
        }

        public static GraphicsPath Rounded(RectangleF r, float radius)
        {
            var path = new GraphicsPath();
            radius = Math.Min(radius, Math.Min(r.Width, r.Height) / 2f);
            if (radius <= 0.5f)
            {
                path.AddRectangle(r);
                return path;
            }
            var d = radius * 2;
            path.AddArc(r.X, r.Y, d, d, 180, 90);
            path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            path.CloseFigure();
            return path;
        }

        public static void FillRounded(Graphics g, Color color, RectangleF r, float radius)
        {
            using (var path = Rounded(r, radius))
            using (var brush = new SolidBrush(color))
                g.FillPath(brush, path);
        }
    }

    /// <summary>The tray icon: one small bar per product, readable at 16x16.</summary>
    public static class IconRenderer
    {
        public static Bitmap Render(List<KeyValuePair<string, double?>> entries, int size, double warn = 75, double danger = 90,
            string style = "bars")
        {
            entries = entries.Take(3).ToList();
            var bmp = new Bitmap(size, size, PixelFormat.Format32bppArgb);
            var s = size / 128f;                    // drawn on a 128-unit grid
            using (var g = Graphics.FromImage(bmp))
            {
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.Clear(Color.Transparent);
                // Dark rounded plate so the icon stays legible on any taskbar.
                Theme.FillRounded(g, Theme.Hex("#1B1D22", 245), new RectangleF(2 * s, 2 * s, 123 * s, 123 * s), 26 * s);
                if (style == "ring")
                {
                    Ring(g, entries, s, warn, danger);
                    return bmp;
                }
                if (entries.Count == 0)
                {
                    Theme.FillRounded(g, Theme.Hex("#4B5059", 200), new RectangleF(26 * s, 58 * s, 75 * s, 12 * s), 6 * s);
                    return bmp;
                }
                const float padX = 20;
                const float totalH = 128 - 2 * 26;
                var gap = entries.Count > 1 ? 12f : 0f;
                var barH = Math.Max(10f, (float)Math.Floor((totalH - gap * (entries.Count - 1)) / entries.Count));
                var y = (float)Math.Floor((128 - (barH * entries.Count + gap * (entries.Count - 1))) / 2);
                foreach (var e in entries)
                {
                    float x0 = padX, x1 = 128 - padX;
                    var radius = barH / 2;
                    Theme.FillRounded(g, Theme.Hex("#41454D"), new RectangleF(x0 * s, y * s, (x1 - x0) * s, barH * s), radius * s);
                    var pct = e.Value;
                    if (pct != null && pct > 0)
                    {
                        var width = Math.Max(barH, (float)Math.Floor((x1 - x0) * Math.Min(100.0, pct.Value) / 100.0));
                        Theme.FillRounded(g, Theme.StatusColor(pct, warn, danger), new RectangleF(x0 * s, y * s, width * s, barH * s), radius * s);
                    }
                    else if (pct == null)
                    {
                        Theme.FillRounded(g, Theme.Idle, new RectangleF(x0 * s, y * s, barH * s, barH * s), radius * s);
                    }
                    // A dot of product color on the left tells the rows apart.
                    using (var b = new SolidBrush(Theme.Tint(e.Key)))
                        g.FillEllipse(b, (x0 - 8) * s, (y + barH / 2 - 3) * s, 6 * s, 6 * s);
                    y += barH + gap;
                }
            }
            return bmp;
        }

        private static void Ring(Graphics g, List<KeyValuePair<string, double?>> entries, float s, double warn, double danger)
        {
            var vals = entries.Where(e => e.Value != null).Select(e => e.Value.Value).ToList();
            double? pct = vals.Count > 0 ? vals.Max() : (double?)null;
            var box = new RectangleF(29 * s, 29 * s, 70 * s, 70 * s);
            using (var pen = new Pen(Theme.Hex("#41454D"), 14 * s)) g.DrawEllipse(pen, box);
            if (pct == null) return;
            using (var pen = new Pen(Theme.StatusColor(pct, warn, danger), 14 * s))
                g.DrawArc(pen, box, -90, (float)(360 * Math.Min(100.0, pct.Value) / 100.0));
        }

        /// <summary>A multi-size .ico built from PNG frames (Vista+ format).</summary>
        public static byte[] ToIco(IList<Bitmap> frames)
        {
            var pngs = frames.Select(f =>
            {
                using (var ms = new MemoryStream())
                {
                    f.Save(ms, ImageFormat.Png);
                    return ms.ToArray();
                }
            }).ToList();
            using (var ms = new MemoryStream())
            using (var w = new BinaryWriter(ms))
            {
                w.Write((short)0);
                w.Write((short)1);
                w.Write((short)frames.Count);
                var offset = 6 + 16 * frames.Count;
                for (var i = 0; i < frames.Count; i++)
                {
                    w.Write((byte)(frames[i].Width >= 256 ? 0 : frames[i].Width));
                    w.Write((byte)(frames[i].Height >= 256 ? 0 : frames[i].Height));
                    w.Write((byte)0);
                    w.Write((byte)0);
                    w.Write((short)1);
                    w.Write((short)32);
                    w.Write(pngs[i].Length);
                    w.Write(offset);
                    offset += pngs[i].Length;
                }
                foreach (var p in pngs) w.Write(p);
                return ms.ToArray();
            }
        }

        [System.Runtime.InteropServices.DllImport("user32.dll")]
        private static extern bool DestroyIcon(IntPtr handle);

        /// <summary>An Icon that owns its own copy (the GDI handle is released at once).</summary>
        public static Icon ToIcon(Bitmap bmp)
        {
            var h = bmp.GetHicon();
            try
            {
                using (var tmp = Icon.FromHandle(h)) return (Icon)tmp.Clone();
            }
            finally { DestroyIcon(h); }
        }
    }
}
