using System;
using System.Collections.Generic;
using System.Linq;
using QuotaTray.Core;

namespace QuotaTray.Providers
{
    public abstract class Provider
    {
        public abstract string Id { get; }
        public abstract string Name { get; }

        protected readonly Config Config;
        public JObj Settings;

        protected Provider(Config config)
        {
            Config = config;
            Settings = config.Provider(Id);
        }

        public bool Enabled => Core.Settings.Flag(Settings, "enabled", true);

        /// <summary>Tests replace detection; null means use Detect().</summary>
        public Func<bool> DetectOverride;

        /// <summary>Is this product installed on the machine? Decides panel placement.</summary>
        public abstract bool Detect();

        /// <summary>Walk the fallback chain, filling in result.Windows / result.Attempts.</summary>
        public abstract void Collect(ProviderResult result);

        public ProviderResult Fetch()
        {
            var result = new ProviderResult(Id, Name) { FetchedAt = Time.Now };
            try
            {
                result.Installed = DetectOverride != null ? DetectOverride() : Detect();
            }
            catch (Exception e)
            {
                result.Installed = true;
                result.Attempts.Add(new SourceAttempt("detect install", false, e.Message));
            }
            try
            {
                Collect(result);
            }
            catch (Exception e)
            {
                Log.Error($"{Id} collection failed: {e}");
                result.Attempts.Add(new SourceAttempt("collect", false, e.GetType().Name + ": " + e.Message));
            }
            if (!result.Ok && string.IsNullOrEmpty(result.Status))
            {
                if (!result.Installed) result.Status = "not detected on this machine";
                else
                {
                    var failed = result.Attempts.Where(a => !a.Ok).ToList();
                    result.Status = failed.Count > 0 ? failed.Last().Detail : "no usable quota source";
                }
            }
            return result;
        }

        public static List<Provider> Build(Config config)
        {
            var all = new List<Provider> { new ClaudeProvider(config), new CodexProvider(config), new AntigravityProvider(config) };
            return all.Where(p => p.Enabled).ToList();
        }

        protected static string Short(string s, int n) => s == null ? "" : s.Length <= n ? s : s.Substring(0, n);
    }
}
