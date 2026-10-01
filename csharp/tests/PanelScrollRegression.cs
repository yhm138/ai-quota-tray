using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Reflection;
using System.Windows.Forms;
using QuotaTray.Core;
using QuotaTray.UI;

// Windows-only, offscreen GDI regression against the executable supplied to
// Run-PanelScrollRegression.ps1. Fixtures never load config, cache or credentials.
public static class PanelScrollRegression
{
    private const BindingFlags Hidden = BindingFlags.Instance | BindingFlags.NonPublic;
    private static readonly Type PanelType = typeof(QuotaPanel);
    private static readonly List<string> Failures = new List<string>();
    private static int Checks;

    private static T Field<T>(QuotaPanel panel, string name)
    {
        return (T)PanelType.GetField(name, Hidden).GetValue(panel);
    }

    private static void Scroll(QuotaPanel panel, int value)
    {
        PanelType.GetMethod("ScrollTo", Hidden).Invoke(panel, new object[] { value });
    }

    private static void Check(string label, bool passed, string detail)
    {
        Checks++;
        Console.WriteLine("[" + (passed ? "PASS" : "FAIL") + "] " + label + (detail.Length > 0 ? ": " + detail : ""));
        if (!passed) Failures.Add(label);
    }

    private static List<ProviderResult> Fixture()
    {
        var all = new List<ProviderResult>();
        // Even the shortest failed card exceeds 80 pixels at the smallest test
        // scale. Grow the fixture on taller displays so both tabs always scroll.
        var count = Math.Max(10, Screen.FromPoint(Cursor.Position).WorkingArea.Height / 80 + 2);
        foreach (var tab in new[] { "subscription", "payg" })
        {
            for (var index = 0; index < count; index++)
            {
                var result = new ProviderResult(tab + index, "Fixture card " + index)
                {
                    Billing = tab, Ok = index % 4 != 3, Plan = "Synthetic plan",
                    Source = "offline test fixture", Label = tab == "payg" ? "Synthetic account" : null,
                    Status = "Synthetic unavailable status with enough words to wrap across several lines and exercise clipped DrawWrapped text while scrolling."
                };
                result.Windows.Add(new QuotaWindow("period", "Current period", 23, detail: "not started", order: 10));
                result.Windows.Add(new QuotaWindow("week", "Last 7 days", 67, detail: "test detail", order: 20));
                result.Info.Add(new InfoRow("Plan status", "Active", "good"));
                result.Info.Add(new InfoRow("Long note", "Synthetic wrapped information row. It deliberately spans several lines so text crosses the upper and lower viewport boundaries during scrolling."));
                result.Info.Add(new InfoRow("Quota groups", "General, Synthetic research group, Synthetic long display name for a separate quota group"));
                all.Add(result);
            }
        }
        return all;
    }

    private static int Changed(Bitmap before, Bitmap after, Rectangle area)
    {
        var count = 0;
        for (var y = area.Top; y < area.Bottom; y++)
            for (var x = area.Left; x < area.Right; x++)
                if (before.GetPixel(x, y).ToArgb() != after.GetPixel(x, y).ToArgb()) count++;
        return count;
    }

    private static int ChangedOutside(Bitmap bitmap, Rectangle allowed, Color original)
    {
        var count = 0;
        for (var y = 0; y < bitmap.Height; y++)
            for (var x = 0; x < bitmap.Width; x++)
                if (!allowed.Contains(x, y) && bitmap.GetPixel(x, y).ToArgb() != original.ToArgb()) count++;
        return count;
    }

    private static int ChangedInside(Bitmap bitmap, Rectangle allowed, Color original)
    {
        var count = 0;
        for (var y = allowed.Top; y < allowed.Bottom; y++)
            for (var x = allowed.Left; x < allowed.Right; x++)
                if (bitmap.GetPixel(x, y).ToArgb() != original.ToArgb()) count++;
        return count;
    }

    private static void PartialPaint(QuotaPanel panel, string label)
    {
        var viewport = Field<Rectangle>(panel, "_viewport");
        var incoming = new Rectangle(30, viewport.Top + 30, panel.Width / 2, Math.Min(100, viewport.Height / 2));
        var sentinel = Color.Magenta;
        using (var bitmap = new Bitmap(panel.Width, panel.Height))
        using (var graphics = Graphics.FromImage(bitmap))
        {
            graphics.Clear(sentinel);
            graphics.SetClip(incoming);
            PanelType.GetMethod("LayoutPanel", Hidden).Invoke(panel, new object[] { graphics, panel.Width, true });
            Check(label + " incoming clip restored", graphics.ClipBounds == incoming, graphics.ClipBounds.ToString());
            var changed = ChangedOutside(bitmap, incoming, sentinel);
            Check(label + " incoming clip respected", changed == 0, changed + " pixels changed outside paint region");
        }
    }

    private static void TextPrimitives()
    {
        foreach (var wrapped in new[] { false, true })
        {
            var name = wrapped ? "DrawWrapped" : "Draw";
            var allowed = new Rectangle(40, 30, 140, 50);
            var sentinel = Color.Magenta;
            using (var bitmap = new Bitmap(250, 130))
            using (var graphics = Graphics.FromImage(bitmap))
            using (var font = new Font("Segoe UI", 18, GraphicsUnit.Pixel))
            {
                graphics.Clear(sentinel);
                graphics.SetClip(allowed);
                var method = PanelType.GetMethod(name, BindingFlags.Static | BindingFlags.NonPublic);
                var text = "Synthetic text crosses every edge of the requested drawing region.";
                var args = wrapped
                    ? new object[] { graphics, true, text, font, Color.White, new Rectangle(20, 10, 210, 110) }
                    : new object[] { graphics, true, text, font, Color.White, 20, 22 };
                method.Invoke(null, args);
                var changed = ChangedOutside(bitmap, allowed, sentinel);
                Check(name + " obeys graphics clip", changed == 0, changed + " pixels changed outside text clip");
                var inside = ChangedInside(bitmap, allowed, sentinel);
                Check(name + " draws inside graphics clip", inside > 0, inside + " text pixels inside clip");
            }
        }
    }

    public static int Run(string outputDirectory)
    {
        Failures.Clear();
        Checks = 0;
        Directory.CreateDirectory(outputDirectory);
        TextPrimitives();
        foreach (var scale in new[] { 1f, 1.25f, 1.5f })
        {
            using (var panel = new QuotaPanel(new PanelCallbacks { DiagnosticsText = () => "synthetic diagnostics" }))
            {
                var unused = panel.Handle;
                // Normalize the monitor DPI so these runs exercise exact effective
                // 100%, 125% and 150% scales without changing Windows settings.
                var dpi = Field<int>(panel, "_dpi");
                panel.UserScale = scale * 96f / dpi;
                panel.SetData(Fixture(), new PanelMeta { Subtitle = "Offline pixel test", Footer = "fixture" });
                foreach (var tab in new[] { "subscription", "payg", "subscription" })
                {
                    panel.SelectTab(tab);
                    var label = (int)(scale * 100) + "% " + tab;
                    using (var top = panel.Snapshot())
                    {
                        var viewport = Field<Rectangle>(panel, "_viewport");
                        var maximum = Field<int>(panel, "_contentH") - Field<int>(panel, "_visibleH");
                        Check(label + " has scrollable fixture", maximum > 200, "maximum scroll " + maximum);
                        Check(label + " tab starts at top", Field<int>(panel, "_scroll") == 0, "");
                        Check(label + " effective scale", Math.Abs(Field<float>(panel, "_scale") - scale) < 0.001f, "");
                        top.Save(Path.Combine(outputDirectory, (int)(scale * 100) + "-" + tab + "-top.png"), ImageFormat.Png);
                        foreach (var offset in new[] { maximum / 2, maximum })
                        {
                            Scroll(panel, offset);
                            using (var scrolled = panel.Snapshot())
                            {
                                var position = offset == maximum ? "bottom" : "middle";
                                var stableLayout = top.Size == scrolled.Size && viewport == Field<Rectangle>(panel, "_viewport");
                                Check(label + " " + position + " stable layout", stableLayout, "");
                                if (stableLayout)
                                {
                                    var headerDiff = Changed(top, scrolled, new Rectangle(0, 0, top.Width, viewport.Top));
                                    var footerDiff = Changed(top, scrolled, new Rectangle(0, viewport.Bottom, top.Width, top.Height - viewport.Bottom));
                                    Check(label + " " + position + " header unchanged", headerDiff == 0, headerDiff + " differing pixels");
                                    Check(label + " " + position + " footer unchanged", footerDiff == 0, footerDiff + " differing pixels");
                                    Check(label + " " + position + " body actually scrolled", Changed(top, scrolled, viewport) > 0, "");
                                }
                                scrolled.Save(Path.Combine(outputDirectory, (int)(scale * 100) + "-" + tab + "-" + position + ".png"), ImageFormat.Png);
                            }
                        }
                        PartialPaint(panel, label);
                    }
                }
                Check((int)(scale * 100) + "% stays offscreen", !panel.Visible, "");
            }
        }
        Console.WriteLine("Panel scroll regression: " + (Checks - Failures.Count) + "/" + Checks + " passed");
        return Failures.Count == 0 ? 0 : 1;
    }
}
