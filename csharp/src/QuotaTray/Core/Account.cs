using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;

namespace QuotaTray.Core
{
    /// <summary>Formatting shared by the plan / subscription / credits / resets details.</summary>
    public static class Account
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        /// <summary>'Oct 22' this year, 'Oct 22, 2027' otherwise, in local time.</summary>
        public static string FmtDate(DateTimeOffset? dt)
        {
            if (dt == null) return "?";
            var local = dt.Value.ToLocalTime();
            var text = local.ToString("MMM", Inv) + " " + local.Day.ToString(Inv);
            if (local.Year != Time.Now.ToLocalTime().Year) text += ", " + local.Year.ToString(Inv);
            return text;
        }

        public static string FmtExpiry(DateTimeOffset? dt)
        {
            if (dt == null) return "no expiry date given";
            var left = Time.HumanizeDelta(dt);
            return !string.IsNullOrEmpty(left) && left != "resetting" ? $"{FmtDate(dt)} (in {left})" : FmtDate(dt);
        }

        public static bool Soon(DateTimeOffset? dt, double days) =>
            dt != null && (dt.Value - Time.Now).TotalSeconds < days * 86400;

        private static readonly Dictionary<string, string> KnownPlans = new Dictionary<string, string>
        {
            { "prolite", "Pro Lite" }, { "plus", "Plus" }, { "pro", "Pro" }, { "team", "Team" },
            { "business", "Business" }, { "enterprise", "Enterprise" }, { "edu", "Edu" }, { "free", "Free" },
            { "ai", "Free" },
        };

        /// <summary>'default_claude_max_20x' -> 'Max 20x', 'prolite' -> 'Pro Lite'.</summary>
        public static string PrettyPlan(object raw)
        {
            var s0 = raw as string;
            if (string.IsNullOrEmpty(s0)) return null;
            var s = s0.Trim().ToLowerInvariant();
            foreach (var prefix in new[] { "default_", "claude_", "chatgpt_" })
                if (s.StartsWith(prefix, StringComparison.Ordinal)) s = s.Substring(prefix.Length);
            s = s.Replace("claude_", "");
            if (KnownPlans.TryGetValue(s, out var known)) return known;
            var words = Regex.Split(s, @"[_\s-]+").Where(w => w.Length > 0)
                .Select(w => Regex.IsMatch(w, @"^\d+x$") ? w : Capitalize(w)).ToList();
            return words.Count > 0 ? string.Join(" ", words) : null;
        }

        public static string Capitalize(string w) =>
            w.Length == 0 ? w : char.ToUpperInvariant(w[0]) + w.Substring(1).ToLowerInvariant();

        /// <summary>Python's str.title(): each run of letters starts upper case.</summary>
        public static string Title(string s)
        {
            var chars = s.ToCharArray();
            var prevLetter = false;
            for (var i = 0; i < chars.Length; i++)
            {
                var c = chars[i];
                if (char.IsLetter(c))
                {
                    chars[i] = prevLetter ? char.ToLowerInvariant(c) : char.ToUpperInvariant(c);
                    prevLetter = true;
                }
                else prevLetter = false;
            }
            return new string(chars);
        }

        public static string Money(double amount, string currency = "USD")
        {
            var cur = (currency ?? "USD").ToUpperInvariant();
            string symbol = cur == "USD" ? "$" : cur == "EUR" ? "\u20ac" : cur == "GBP" ? "\u00a3"
                : cur == "CNY" || cur == "RMB" ? "\u00a5" : null;
            var num = amount.ToString("#,0.00", Inv);
            return symbol != null ? symbol + num : $"{num} {currency}";
        }

        /// <summary>{'amount_minor': 1359, 'exponent': 2} -> 13.59.</summary>
        public static double? MinorAmount(object node)
        {
            if (!(node is JObj o)) return null;
            var minor = o.Num("amount_minor");
            if (minor == null) return null;
            var exp = o.Num("exponent");
            var e = exp != null && exp.Value == Math.Floor(exp.Value) ? (int)exp.Value : 2;
            return minor.Value / Math.Pow(10, e);
        }

        public static string N0(double v) => v.ToString("#,0", Inv);
        public static string N2(double v) => v.ToString("#,0.00", Inv);

        /// <summary>Panel lines for banked resets.</summary>
        public static List<InfoRow> ResetRows(List<ResetGrant> resets, string where)
        {
            var active = resets.Where(g => g.Active()).ToList();
            if (active.Count == 0) return new List<InfoRow>();
            var total = active.Sum(g => g.Count);
            var first = active.Where(g => g.ExpiresAt != null).Select(g => g.ExpiresAt).DefaultIfEmpty(null).Min();
            var value = $"{total} unused" + (first != null ? $" \u00b7 first expires {FmtExpiry(first)}" : "");
            var rows = new List<InfoRow> { new InfoRow("Resets", value, Soon(first, 3) ? "warn" : "good") };
            var notes = active.Where(g => !string.IsNullOrEmpty(g.Note)).Select(g => g.Note).Distinct()
                .OrderBy(n => n, StringComparer.Ordinal).ToList();
            if (notes.Count > 0) rows.Add(new InfoRow("", string.Join("; ", notes) + $" \u00b7 use in {where}"));
            return rows;
        }

        /// <summary>Same account when the emails match; without emails, identical usage.</summary>
        public static bool SameAccount(ProviderResult a, ProviderResult b)
        {
            if (!string.IsNullOrEmpty(a.Account) && !string.IsNullOrEmpty(b.Account))
                return string.Equals(a.Account.Trim(), b.Account.Trim(), StringComparison.OrdinalIgnoreCase);
            if (a.Windows.Count != b.Windows.Count) return false;
            for (var i = 0; i < a.Windows.Count; i++)
            {
                var x = a.Windows[i];
                var y = b.Windows[i];
                if (x.Key != y.Key || Math.Round(x.Percent ?? 0, 1) != Math.Round(y.Percent ?? 0, 1)) return false;
            }
            return true;
        }

        /// <summary>One page per account; logins of the same account share a page.</summary>
        public static List<ProviderResult> GroupPages(List<ProviderResult> pages)
        {
            var output = new List<ProviderResult>();
            foreach (var page in pages)
            {
                var twin = output.FirstOrDefault(p => SameAccount(p, page));
                if (twin == null) output.Add(page);
                else if (!string.IsNullOrEmpty(page.Label) && !(twin.Label ?? "").Contains(page.Label))
                    twin.Label = !string.IsNullOrEmpty(twin.Label) ? $"{twin.Label} + {page.Label}" : page.Label;
            }
            return output;
        }
    }

    /// <summary>Daily nudge about banked limit resets that are going unused.</summary>
    public static class Reminders
    {
        public static bool Due(string lastDay, DateTime nowLocal, int hour) =>
            nowLocal.Hour >= hour && lastDay != nowLocal.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);

        public static string UnusedResetsText(List<ProviderResult> results)
        {
            var lines = new List<string>();
            foreach (var product in results)
            {
                var pages = product.Pages();
                foreach (var r in pages)
                {
                    var active = r.ActiveResets();
                    if (active.Count == 0) continue;
                    var count = active.Sum(g => g.Count);
                    var first = active.Where(g => g.ExpiresAt != null).Select(g => g.ExpiresAt).DefaultIfEmpty(null).Min();
                    var who = r.Account ?? r.Label;
                    var name = pages.Count > 1 && !string.IsNullOrEmpty(who) ? $"{product.Name} ({who})" : product.Name;
                    var line = $"{name}: {count} unused reset{(count != 1 ? "s" : "")}";
                    if (first != null) line += $", first expires {Account.FmtExpiry(first)}";
                    lines.Add(line);
                }
            }
            if (lines.Count == 0) return null;
            return string.Join("\n", lines) + "\nApply them in the app's Settings > Usage before they expire.";
        }
    }
}
