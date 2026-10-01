using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace QuotaTray.Core
{
    public static class AppInfo
    {
        public const string Name = "QuotaTray";
        public const string Edition = "C#";

        public static string Version
        {
            get
            {
                var v = typeof(AppInfo).Assembly.GetName().Version;
                return v == null ? "0.0.0" : $"{v.Major}.{v.Minor}.{v.Build}";
            }
        }
    }

    /// <summary>Everything lives under %APPDATA%\QuotaTray, shared with the Python build.</summary>
    public static class Paths
    {
        /// <summary>Tests point this somewhere else.</summary>
        public static string Override;

        public static string AppDir
        {
            get
            {
                var dir = Override;
                if (string.IsNullOrEmpty(dir))
                {
                    var appdata = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
                    dir = !string.IsNullOrEmpty(appdata) && Environment.OSVersion.Platform == PlatformID.Win32NT
                        ? Path.Combine(appdata, AppInfo.Name)
                        : Path.Combine(Home, ".quota-tray");
                }
                try { Directory.CreateDirectory(dir); } catch (Exception) { }
                return dir;
            }
        }

        public static string Home => Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        public static string ConfigFile => Path.Combine(AppDir, "config.json");
        public static string CacheFile => Path.Combine(AppDir, "cache.json");
        public static string LogFile => Path.Combine(AppDir, "quota-tray-csharp.log");
        public static string DiagFile => Path.Combine(AppDir, "diagnostics.txt");
        public static string CrashFile => Path.Combine(AppDir, "crash.log");
    }

    public sealed class Config
    {
        public JObj Data;

        public Config(JObj data = null)
        {
            Data = Merge(Defaults(), data ?? new JObj());
        }

        public static JObj Defaults()
        {
            return Json.ParseObject(@"{
  ""refresh_seconds"": 300,
  ""refresh_seconds_min"": 60,
  ""hide_not_installed"": true,
  ""warn_percent"": 75,
  ""danger_percent"": 90,
  ""icon_style"": ""bars"",
  ""panel_scale"": 1.0,
  ""check_updates"": true,
  ""remind_unused_resets"": true,
  ""reset_reminder_hour"": 10,
  ""update_repo"": ""yhm138/ai-quota-tray"",
  ""providers"": {
    ""claude"": {
      ""enabled"": true,
      ""percent_scale"": ""auto"",
      ""oauth_token"": """",
      ""session_key"": """",
      ""credentials_path"": """",
      ""scan_wsl"": true,
      ""order"": [""oauth"", ""desktop_oauth"", ""desktop_cookie"", ""manual_cookie""]
    },
    ""codex"": {
      ""enabled"": true,
      ""codex_home"": """",
      ""order"": [""wham"", ""app_server"", ""jsonl"", ""sqlite""],
      ""app_server_timeout"": 20,
      ""jsonl_max_days"": 14
    },
    ""antigravity"": {
      ""enabled"": true,
      ""csrf_token"": """",
      ""port"": 0,
      ""show_models"": true,
      ""max_models"": 6
    },
    ""gemini"": {
      ""enabled"": false,
      ""gemini_home"": """",
      ""scan_wsl"": true
    },
    ""trae"": {
      ""enabled"": true,
      ""storage_path"": """",
      ""cn"": true,
      ""scan_wsl"": true
    },
    ""doubao"": {
      ""enabled"": true,
      ""session_id"": """",
      ""data_dir"": """",
      ""scan_browsers"": false
    },
    ""deepseek"": {
      ""enabled"": true,
      ""api_key"": """",
      ""scan_opencode"": true,
      ""scan_dsh"": true,
      ""scan_wsl"": true,
      ""low_balance"": 5
    }
  }
}");
        }

        public static JObj Merge(JObj baseObj, JObj over)
        {
            var output = new JObj();
            foreach (var kv in baseObj) output[kv.Key] = kv.Value;
            foreach (var kv in over)
            {
                if (kv.Value is JObj o && output[kv.Key] is JObj b) output[kv.Key] = Merge(b, o);
                else output[kv.Key] = kv.Value;
            }
            return output;
        }

        /// <summary>config.json's write time when it was read or written.</summary>
        public DateTime? Mtime;
        /// <summary>Why config.json could not be read, if it couldn't.</summary>
        public string Error;

        private static DateTime? FileTime(string path)
        {
            try { return File.Exists(path) ? File.GetLastWriteTimeUtc(path) : (DateTime?)null; }
            catch (Exception) { return null; }
        }

        public static Config Load()
        {
            var path = Paths.ConfigFile;
            if (File.Exists(path))
            {
                string why = "not a JSON object";
                try
                {
                    // ReadAllText detects a UTF-8 BOM (Notepad may save one).
                    var data = Json.ParseObject(File.ReadAllText(path, Encoding.UTF8));
                    if (data != null) return new Config(data) { Mtime = FileTime(path) };
                }
                catch (Exception e) { why = e.Message; }
                Log.Warn("config.json could not be read, using defaults until it is fixed: " + why);
                // Never overwrite the user's file here: it may be a hand edit with a
                // typo, and saving defaults over it would wipe it.
                return new Config { Mtime = FileTime(path), Error = "config.json could not be read (" + why + ")" };
            }
            var cfg = new Config();
            cfg.Save();
            return cfg;
        }

        /// <summary>True when config.json was edited (or replaced) since this was read.</summary>
        public bool ChangedOnDisk() => FileTime(Paths.ConfigFile) != Mtime;

        public void Save()
        {
            // The file on disk is the user's (broken) edit; don't clobber it.
            if (Error != null) return;
            try
            {
                File.WriteAllText(Paths.ConfigFile, Json.Write(Data, true), new UTF8Encoding(false));
                Mtime = FileTime(Paths.ConfigFile);
            }
            catch (Exception e) { Log.Warn("failed to write config.json: " + e.Message); }
        }

        public JObj Provider(string id) => Data.Obj("providers")?.Obj(id) ?? new JObj();

        public object Get(string key) => Data[key];

        public bool Flag(string key, bool fallback)
        {
            var v = Data[key];
            return v == null ? fallback : Json.Truthy(v);
        }

        public double Number(string key, double fallback) => Json.ToFloat(Data[key]) ?? fallback;

        public string Text(string key, string fallback) => Data.Text(key) ?? fallback;

        public int RefreshSeconds
        {
            get
            {
                var v = (int)Number("refresh_seconds", 300);
                return Math.Max((int)Number("refresh_seconds_min", 60), v);
            }
        }
    }

    /// <summary>Settings of one provider, with Python-style defaults.</summary>
    public static class Settings
    {
        public static string Str(JObj s, string key) => (s[key] as string ?? "").Trim();

        public static bool Flag(JObj s, string key, bool fallback) => s.Has(key) ? Json.Truthy(s[key]) : fallback;

        public static double Num(JObj s, string key, double fallback) => Json.ToFloat(s[key]) ?? fallback;

        public static List<string> List(JObj s, string key)
        {
            var l = new List<string>();
            foreach (var v in s.Arr(key) ?? new List<object>())
                if (v is string str) l.Add(str);
            return l;
        }
    }

    public static class Cache
    {
        public static JObj Load()
        {
            try { return File.Exists(Paths.CacheFile) ? Json.ParseObject(File.ReadAllText(Paths.CacheFile, Encoding.UTF8)) ?? new JObj() : new JObj(); }
            catch (Exception) { return new JObj(); }
        }

        public static void Save(JObj payload)
        {
            try { File.WriteAllText(Paths.CacheFile, Json.Write(payload), new UTF8Encoding(false)); }
            catch (Exception) { }
        }
    }

    public static class Log
    {
        private static readonly object Gate = new object();
        public static bool Verbose;

        public static void Info(string msg) => Write("INFO   ", msg);
        public static void Warn(string msg) => Write("WARNING", msg);
        public static void Error(string msg) => Write("ERROR  ", msg);

        public static void Debug(string msg)
        {
            if (Verbose) Write("DEBUG  ", msg);
        }

        private static void Write(string level, string msg)
        {
            try
            {
                lock (Gate)
                {
                    var path = Paths.LogFile;
                    var info = new FileInfo(path);
                    if (info.Exists && info.Length > 512000)
                    {
                        try { File.Copy(path, path + ".1", true); File.WriteAllText(path, ""); }
                        catch (IOException) { }
                    }
                    File.AppendAllText(path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss,fff} {level} {msg}{Environment.NewLine}", Encoding.UTF8);
                }
            }
            catch (Exception) { }
        }
    }
}
