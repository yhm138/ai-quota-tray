using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;

namespace QuotaTray.Core
{
    /// <summary>One quota window, e.g. Claude's 5-hour window or Codex's weekly window.</summary>
    public sealed class QuotaWindow
    {
        public string Key;
        public string Label;
        public double? Percent;              // percent USED, 0-100
        public DateTimeOffset? ResetsAt;     // UTC
        public string Detail;                // e.g. "812 / 1,000 left"
        public int Order = 100;
        public bool Exhausted;
        public bool Credit;                  // a prepaid credit, not a usage limit

        public QuotaWindow(string key, string label, double? percent = null, DateTimeOffset? resetsAt = null,
            string detail = null, int order = 100, bool exhausted = false, bool credit = false)
        {
            Key = key; Label = label; Percent = percent; ResetsAt = resetsAt; Detail = detail;
            Order = order; Exhausted = exhausted; Credit = credit;
        }

        public string PercentText
        {
            get
            {
                if (Percent == null) return "--";
                var p = Percent.Value;
                if (p >= 99.95) return "100%";
                if (p < 10 && Math.Abs(p - Math.Round(p)) > 0.05) return p.ToString("0.0", CultureInfo.InvariantCulture) + "%";
                return p.ToString("0", CultureInfo.InvariantCulture) + "%";
            }
        }
    }

    /// <summary>One line of account detail under the usage bars, e.g. Plan: Max 20x.</summary>
    public sealed class InfoRow
    {
        public string Label;
        public string Value;
        public string Tone;                  // "" | "good" | "warn"

        public InfoRow(string label, string value, string tone = "")
        {
            Label = label ?? ""; Value = value ?? ""; Tone = tone ?? "";
        }
    }

    /// <summary>A banked limit reset: saved on the account until used or expired.</summary>
    public sealed class ResetGrant
    {
        public int Count;
        public DateTimeOffset? ExpiresAt;
        public string Note;

        public ResetGrant(int count, DateTimeOffset? expiresAt = null, string note = "")
        {
            Count = count; ExpiresAt = expiresAt; Note = note ?? "";
        }

        public bool Active(DateTimeOffset? now = null)
        {
            var t = now ?? Time.Now;
            return Count > 0 && (ExpiresAt == null || ExpiresAt > t);
        }
    }

    /// <summary>Diagnostic record for one collection attempt.</summary>
    public sealed class SourceAttempt
    {
        public string Name;
        public bool Ok;
        public string Detail;

        public SourceAttempt(string name, bool ok, string detail = "")
        {
            Name = name; Ok = ok; Detail = detail ?? "";
        }
    }

    public sealed class ProviderResult
    {
        public string ProviderId;
        public string Name;
        public bool Ok;
        public string Status = "";
        public string Source;
        public string Account;
        public string Plan;
        public List<QuotaWindow> Windows = new List<QuotaWindow>();
        public DateTimeOffset? FetchedAt;
        public DateTimeOffset? DataTime;
        public List<SourceAttempt> Attempts = new List<SourceAttempt>();
        public bool Installed = true;
        public List<InfoRow> Info = new List<InfoRow>();
        public List<ResetGrant> Resets = new List<ResetGrant>();
        public string Label;                 // which login this is, e.g. "Claude Desktop"
        // Other accounts of the same product signed in elsewhere on this
        // machine (Claude Code vs Claude Desktop, Windows vs WSL Codex).
        public List<ProviderResult> Alternates = new List<ProviderResult>();

        public ProviderResult(string providerId, string name)
        {
            ProviderId = providerId; Name = name;
        }

        public List<ProviderResult> Pages()
        {
            var l = new List<ProviderResult> { this };
            l.AddRange(Alternates);
            return l;
        }

        /// <summary>Take over everything a single-account page found.</summary>
        public void Adopt(ProviderResult page)
        {
            Ok = page.Ok; Status = page.Status; Source = page.Source; Account = page.Account;
            Plan = page.Plan; Windows = page.Windows; DataTime = page.DataTime; Info = page.Info;
            Resets = page.Resets; Label = page.Label;
        }

        public List<ResetGrant> ActiveResets(DateTimeOffset? now = null) =>
            Resets.Where(g => g.Active(now)).OrderBy(g => g.ExpiresAt ?? DateTimeOffset.MaxValue).ToList();

        public int ResetsAvailable => ActiveResets().Sum(g => g.Count);

        /// <summary>Highest usage among the limits (credits are not limits you can hit).</summary>
        public double? WorstPercent
        {
            get
            {
                var vals = Windows.Where(w => w.Percent != null && !w.Credit).Select(w => w.Percent.Value).ToList();
                return vals.Count > 0 ? vals.Max() : (double?)null;
            }
        }

        public List<QuotaWindow> SortedWindows() =>
            Windows.OrderBy(w => w.Order).ThenBy(w => w.Label, StringComparer.Ordinal).ToList();

        // ---------------------------------------------------------- cache (same shape as the Python build)

        public JObj ToCache()
        {
            var o = new JObj();
            o["provider_id"] = ProviderId;
            o["name"] = Name;
            o["ok"] = Ok;
            o["status"] = Status;
            o["source"] = Source;
            o["account"] = Account;
            o["plan"] = Plan;
            o["installed"] = Installed;
            o["fetched_at"] = Time.Iso(FetchedAt);
            o["data_time"] = Time.Iso(DataTime);
            o["windows"] = Windows.Select(w =>
            {
                var j = new JObj();
                j["key"] = w.Key; j["label"] = w.Label; j["percent"] = w.Percent;
                j["resets_at"] = Time.Iso(w.ResetsAt); j["detail"] = w.Detail; j["order"] = (double)w.Order;
                j["exhausted"] = w.Exhausted; j["credit"] = w.Credit;
                return (object)j;
            }).ToList();
            o["info"] = Info.Select(i =>
            {
                var j = new JObj();
                j["label"] = i.Label; j["value"] = i.Value; j["tone"] = i.Tone;
                return (object)j;
            }).ToList();
            o["resets"] = Resets.Select(g =>
            {
                var j = new JObj();
                j["count"] = (double)g.Count; j["expires_at"] = Time.Iso(g.ExpiresAt); j["note"] = g.Note;
                return (object)j;
            }).ToList();
            o["label"] = Label;
            o["alternates"] = Alternates.Select(a => (object)a.ToCache()).ToList();
            return o;
        }

        public static ProviderResult FromCache(JObj d)
        {
            var r = new ProviderResult(d.Str("provider_id") ?? "?", d.Str("name") ?? "?")
            {
                Ok = d.Truthy("ok"),
                Status = d.Str("status") ?? "",
                Source = d.Str("source"),
                Account = d.Str("account"),
                Plan = d.Str("plan"),
                Installed = !d.Has("installed") || d.Truthy("installed"),
                FetchedAt = Time.Parse(d["fetched_at"]),
                DataTime = Time.Parse(d["data_time"]),
                Label = d.Str("label"),
            };
            foreach (var w in (d.Arr("windows") ?? new List<object>()).OfType<JObj>())
            {
                r.Windows.Add(new QuotaWindow(w.Str("key") ?? "?", w.Str("label") ?? "?", w.Num("percent"),
                    Time.Parse(w["resets_at"]), w.Str("detail"), (int)(w.Num("order") ?? 100),
                    w.Truthy("exhausted"), w.Truthy("credit")));
            }
            foreach (var i in (d.Arr("info") ?? new List<object>()).OfType<JObj>())
                r.Info.Add(new InfoRow(i.Str("label") ?? "", i.Str("value") ?? "", i.Str("tone") ?? ""));
            foreach (var g in (d.Arr("resets") ?? new List<object>()).OfType<JObj>())
                r.Resets.Add(new ResetGrant((int)(g.Num("count") ?? 0), Time.Parse(g["expires_at"]), g.Str("note") ?? ""));
            foreach (var a in (d.Arr("alternates") ?? new List<object>()).OfType<JObj>())
                r.Alternates.Add(FromCache(a));
            return r;
        }
    }

    public static class Time
    {
        /// <summary>Tests pin the clock through this.</summary>
        public static Func<DateTimeOffset> Clock = () => DateTimeOffset.UtcNow;

        public static DateTimeOffset Now => Clock();

        public static string Iso(DateTimeOffset? t) =>
            t?.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.ffffff+00:00", CultureInfo.InvariantCulture);

        public static DateTimeOffset? FromEpoch(double ts)
        {
            // Both second and millisecond epochs show up in the wild.
            if (ts > 1e12) ts /= 1000.0;
            if (ts < 1e8) return null;              // clearly not an epoch timestamp
            try { return DateTimeOffset.FromUnixTimeMilliseconds((long)Math.Round(ts * 1000.0)); }
            catch (ArgumentOutOfRangeException) { return null; }
        }

        private static readonly Regex Fraction = new Regex(@"(\.\d{7})\d+");

        /// <summary>Parse the many timestamp shapes these APIs use.</summary>
        public static DateTimeOffset? Parse(object value)
        {
            switch (value)
            {
                case null: return null;
                case DateTimeOffset dto: return dto.ToUniversalTime();
                case bool _: return null;
                case string s:
                    s = s.Trim();
                    if (s.Length == 0) return null;
                    if (s.All(char.IsDigit))
                        return double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var n) ? FromEpoch(n) : null;
                    s = Fraction.Replace(s, "$1");
                    if (DateTimeOffset.TryParse(s, CultureInfo.InvariantCulture,
                            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var t))
                        return t.ToUniversalTime();
                    return null;
                default:
                    var num = Json.AsNumber(value);
                    return num == null ? (DateTimeOffset?)null : FromEpoch(num.Value);
            }
        }

        /// <summary>Time until a reset: 3d 2h / 4h 12m / 7m.</summary>
        public static string HumanizeDelta(DateTimeOffset? target, DateTimeOffset? now = null)
        {
            if (target == null) return "";
            var secs = (long)Math.Floor((target.Value - (now ?? Now)).TotalSeconds);
            if (secs <= 0) return "resetting";
            var days = secs / 86400;
            var rem = secs % 86400;
            var hours = rem / 3600;
            rem %= 3600;
            var minutes = rem / 60;
            if (days > 0) return hours > 0 ? $"{days}d {hours}h" : $"{days}d";
            if (hours > 0) return minutes > 0 ? $"{hours}h {minutes}m" : $"{hours}h";
            if (minutes > 0) return $"{minutes}m";
            return "<1m";
        }

        public static string HumanizeAge(DateTimeOffset? t, DateTimeOffset? now = null)
        {
            if (t == null) return "";
            var secs = Math.Max(0, (long)((now ?? Now) - t.Value).TotalSeconds);
            if (secs < 60) return "just now";
            if (secs < 3600) return $"{secs / 60}m ago";
            if (secs < 86400) return $"{secs / 3600}h ago";
            return $"{secs / 86400}d ago";
        }
    }

    /// <summary>Generic, change-tolerant extraction of quota windows from API replies.</summary>
    public static class Extract
    {
        public static readonly string[] UsedKeys =
        {
            "utilization", "used_percent", "usedPercent", "percentUsed", "percent_used",
            "usedPercentage", "used_percentage",
        };

        private static readonly string[] ResetAtKeys =
        {
            "resets_at", "resetsAt", "reset_at", "resetAt", "reset_time", "resetTime",
            "resetsAtUtc", "resets_at_utc",
        };

        private static readonly string[] ResetInKeys =
        {
            "resets_in_seconds", "resetsInSeconds", "reset_in_seconds", "secondsUntilReset",
            "seconds_until_reset", "resets_in_s",
        };

        /// <summary>Every object that carries a usage-percentage field, with its path.</summary>
        public static IEnumerable<KeyValuePair<string[], JObj>> QuotaNodes(object obj, string[] path = null)
        {
            path = path ?? new string[0];
            if (obj is JObj o)
            {
                if (UsedKeys.Any(k => Json.ToFloat(o[k]) != null))
                    yield return new KeyValuePair<string[], JObj>(path, o);
                foreach (var kv in o)
                    foreach (var hit in QuotaNodes(kv.Value, path.Concat(new[] { kv.Key }).ToArray()))
                        yield return hit;
            }
            else if (obj is List<object> l)
            {
                for (var i = 0; i < l.Count; i++)
                    foreach (var hit in QuotaNodes(l[i], path.Concat(new[] { i.ToString(CultureInfo.InvariantCulture) }).ToArray()))
                        yield return hit;
            }
        }

        public static double? Used(JObj node)
        {
            foreach (var k in UsedKeys)
            {
                var v = Json.ToFloat(node[k]);
                if (v != null) return v;
            }
            return null;
        }

        public static DateTimeOffset? Reset(JObj node, DateTimeOffset? baseTime = null)
        {
            foreach (var k in ResetAtKeys)
            {
                var t = Time.Parse(node[k]);
                if (t != null) return t;
            }
            foreach (var k in ResetInKeys)
            {
                var secs = Json.ToFloat(node[k]);
                if (secs != null && secs > 0) return (baseTime ?? Time.Now).AddSeconds(secs.Value);
            }
            return null;
        }

        /// <summary>1 when the batch is already 0-100, 100 when it is 0-1 fractions.</summary>
        public static double DecideScale(IEnumerable<double> values)
        {
            var vals = values.ToList();
            if (vals.Count == 0) return 1.0;
            return vals.Max() > 1.0 ? 1.0 : 100.0;
        }

        public static double? ApplyScale(double? value, double scale) =>
            value == null ? (double?)null : Math.Max(0.0, Math.Min(100.0, value.Value * scale));
    }
}
