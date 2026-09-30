using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using QuotaTray.Core;
using QuotaTray.Win;

namespace QuotaTray.Providers
{
    /// <summary>
    /// Antigravity IDE (Google). Its Codeium-family language server is started
    /// with --csrf_token and listens on a random 127.0.0.1 port; its
    /// GetUserStatus RPC returns prompt credits and per-model quota.
    /// </summary>
    public sealed class AntigravityProvider : Provider
    {
        public override string Id => "antigravity";
        public override string Name => "Antigravity";

        public const string RpcPath = "/exa.language_server_pb.LanguageServerService/GetUserStatus";
        private const string RpcBody = "{\"metadata\":{\"ideName\":\"antigravity\",\"extensionName\":\"antigravity\",\"locale\":\"en\"}}";

        // Process list only; ports come from netstat (Get-NetTCPConnection is
        // blocked on some machines). Always prints valid JSON.
        private const string PsProcesses = @"
$ErrorActionPreference='SilentlyContinue'
$procs = @(Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -and ($_.CommandLine -match 'antigravity' -or $_.CommandLine -match 'csrf_token')
} | ForEach-Object {
  [pscustomobject]@{ ProcessId = $_.ProcessId; CommandLine = $_.CommandLine }
})
if ($procs.Count -eq 0) { '[]' } else { ConvertTo-Json -InputObject $procs -Depth 3 -Compress }
";

        private Tuple<string, string> _cached;       // (base url, csrf) that worked last time

        public AntigravityProvider(Config config) : base(config) { }

        public static string ExtractArg(string cmdline, string name)
        {
            var n = Regex.Escape(name);
            foreach (var pattern in new[] { n + "[=\\s]+\"([^\"]+)\"", n + "[=\\s]+'([^']+)'", n + "[=\\s]+([^\\s\"']+)" })
            {
                var m = Regex.Match(cmdline, pattern, RegexOptions.IgnoreCase);
                if (m.Success) return m.Groups[1].Value;
            }
            return null;
        }

        public static int Score(string cmdline)
        {
            var low = cmdline.ToLowerInvariant();
            var score = 0;
            if (low.Contains("antigravity")) score += 1;
            if (low.Contains("language_server") || low.Contains("language-server") || low.Contains("lsp")) score += 50;
            if (low.Contains("--csrf_token")) score += 20;
            if (low.Contains("--extension_server_port")) score += 10;
            return score;
        }

        private static Tuple<List<KeyValuePair<int, string>>, string> Processes()
        {
            var data = Proc.PowerShellJson(PsProcesses, 30);
            if (data is JObj single) data = new List<object> { single };
            if (data is List<object> list)
            {
                var output = new List<KeyValuePair<int, string>>();
                foreach (var item in list.OfType<JObj>())
                {
                    var pid = item.Num("ProcessId");
                    var cmd = item.Str("CommandLine") ?? "";
                    if (pid != null && cmd.Length > 0) output.Add(new KeyValuePair<int, string>((int)pid.Value, cmd));
                }
                return Tuple.Create(output, (string)null);
            }
            // PowerShell gave nothing usable: fall back to wmic.
            var r = Proc.Run("wmic", "process get processid,commandline /format:csv", 30);
            if (r.Code != 0 && r.Out.Trim().Length == 0)
                return Tuple.Create(new List<KeyValuePair<int, string>>(),
                    $"PowerShell returned no JSON and wmic failed ({(r.Err.Trim().Length > 0 ? Short(r.Err.Trim(), 120) : "rc=" + r.Code)})");
            var entries = new List<KeyValuePair<int, string>>();
            foreach (var line in r.Out.Split('\n'))
            {
                var low = line.ToLowerInvariant();
                if (!low.Contains("antigravity") && !low.Contains("csrf_token")) continue;
                var idx = line.LastIndexOf(',');
                if (idx < 0) continue;
                var pidText = line.Substring(idx + 1).Trim();
                if (pidText.Length == 0 || !pidText.All(char.IsDigit)) continue;
                var head = line.Substring(0, idx);
                var comma = head.IndexOf(',');
                entries.Add(new KeyValuePair<int, string>(int.Parse(pidText), comma >= 0 ? head.Substring(comma + 1) : head));
            }
            return Tuple.Create(entries, (string)null);
        }

        /// <summary>pid -> listening TCP ports, from netstat output.</summary>
        public static Dictionary<int, List<int>> ParseNetstat(string output)
        {
            var mapping = new Dictionary<int, List<int>>();
            foreach (var line in output.Split('\n'))
            {
                if (!line.ToUpperInvariant().Contains("LISTEN")) continue;
                var parts = line.Split(new[] { ' ', '\t', '\r' }, StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length < 4) continue;
                var pidText = parts[parts.Length - 1];
                if (!pidText.All(char.IsDigit)) continue;
                var m = Regex.Match(parts[1], @":(\d+)$");
                if (!m.Success) continue;
                var pid = int.Parse(pidText);
                var port = int.Parse(m.Groups[1].Value);
                if (!mapping.TryGetValue(pid, out var ports)) mapping[pid] = ports = new List<int>();
                if (!ports.Contains(port)) ports.Add(port);
            }
            return mapping;
        }

        private static Tuple<Dictionary<int, List<int>>, string> ListeningPorts()
        {
            var r = Proc.Run("netstat", "-ano -p TCP", 25);
            if (r.Code != 0 && r.Out.Trim().Length == 0) r = Proc.Run("netstat", "-ano", 25);
            if (r.Out.Trim().Length == 0)
                return Tuple.Create(new Dictionary<int, List<int>>(),
                    $"netstat produced no output ({(r.Err.Trim().Length > 0 ? Short(r.Err.Trim(), 120) : "rc=" + r.Code)})");
            return Tuple.Create(ParseNetstat(r.Out), (string)null);
        }

        /// <summary>([base url], csrf token, diagnostics).</summary>
        private static Tuple<List<string>, string, List<string>> DiscoverEndpoints()
        {
            var notes = new List<string>();
            if (!Proc.IsWindows) return Tuple.Create(new List<string>(), (string)null, new List<string> { "process discovery is Windows-only" });
            var procs = Processes();
            if (procs.Item2 != null) return Tuple.Create(new List<string>(), (string)null, new List<string> { procs.Item2 });
            if (procs.Item1.Count == 0)
                return Tuple.Create(new List<string>(), (string)null,
                    new List<string> { "no Antigravity language server process - open Antigravity IDE first" });
            var ports = ListeningPorts();
            if (ports.Item2 != null) notes.Add(ports.Item2);
            var scored = procs.Item1.OrderByDescending(p => Score(p.Value)).ThenByDescending(p => p.Key).ToList();
            var token = scored.Select(p => ExtractArg(p.Value, "--csrf_token")).FirstOrDefault(t => t != null);
            var candidates = new List<string>();
            var portSet = new SortedSet<int>();
            foreach (var p in scored)
            {
                if (!ports.Item1.TryGetValue(p.Key, out var list)) continue;
                foreach (var port in list)
                    foreach (var scheme in new[] { "https", "http" })
                    {
                        var url = $"{scheme}://127.0.0.1:{port}";
                        if (!candidates.Contains(url)) candidates.Add(url);
                        portSet.Add(port);
                    }
            }
            if (candidates.Count == 0)
            {
                notes.Add($"found {procs.Item1.Count} Antigravity process(es) (pids {string.Join(", ", scored.Take(4).Select(p => p.Key))}) but none are listening on TCP");
                return Tuple.Create(new List<string>(), token, notes);
            }
            notes.Add($"{procs.Item1.Count} process(es), candidate ports [{string.Join(", ", portSet)}], csrf_token {(token != null ? "found" : "missing")}");
            return Tuple.Create(candidates.Take(24).ToList(), token, notes);
        }

        private static object CallRpc(string baseUrl, string csrf)
        {
            var req = new HttpRequest
            {
                Method = "POST",
                Url = baseUrl + RpcPath,
                Body = RpcBody,
                TimeoutSeconds = 6,
                Insecure = true,
            };
            req.Headers["Accept"] = "application/json";
            req.Headers["Content-Type"] = "application/json";
            req.Headers["Connect-Protocol-Version"] = "1";
            if (csrf != null) req.Headers["X-Codeium-Csrf-Token"] = csrf;
            var resp = Http.Send(req);
            if (resp.Status >= 400) throw new IOException("HTTP " + resp.Status);
            return resp.Json();
        }

        public static Tuple<List<QuotaWindow>, string, string> ParseUserStatus(JObj payload, JObj settings)
        {
            var status = payload.Obj("userStatus") ?? payload;
            var windows = new List<QuotaWindow>();
            var account = status.Str("email");
            string plan = null;

            var planStatus = status.Obj("planStatus");
            if (planStatus != null)
            {
                var info = planStatus.Obj("planInfo") ?? new JObj();
                plan = info.Text("planName") ?? info.Text("name") ?? planStatus.Text("planName");
                var available = planStatus.Num("availablePromptCredits");
                var monthly = NonZero(info.Num("monthlyPromptCredits")) ?? planStatus.Num("monthlyPromptCredits");
                if (available != null && monthly != null && monthly > 0)
                {
                    var used = Math.Max(0.0, monthly.Value - available.Value);
                    windows.Add(new QuotaWindow("prompt_credits", "Prompt credits", Math.Min(100.0, used / monthly.Value * 100.0),
                        Time.Parse(planStatus["resetTime"] ?? info["resetTime"]),
                        $"{Account.N0(available.Value)} / {Account.N0(monthly.Value)} left", 5, available <= 0));
                }
            }

            if (Core.Settings.Flag(settings, "show_models", true))
            {
                var configs = status.Obj("cascadeModelConfigData")?.Arr("clientModelConfigs");
                if (configs != null)
                {
                    var limit = (int)Core.Settings.Num(settings, "max_models", 6);
                    var rank = 10;
                    var seenLabels = new HashSet<string>();
                    foreach (var model in configs.OfType<JObj>())
                    {
                        var quota = model.Obj("quotaInfo");
                        var remaining = quota?.Num("remainingFraction");
                        if (remaining == null) continue;
                        var modelId = model.Obj("modelOrAlias")?.Str("model");
                        var label = model.Text("label") ?? modelId ?? "Model";
                        // The same display name can appear twice with different model ids.
                        if (seenLabels.Contains(label))
                        {
                            var suffix = Account.Title((modelId ?? "").Split('_').Last());
                            label = suffix.Length > 0 ? $"{label} ({suffix})" : $"{label} #2";
                        }
                        seenLabels.Add(label);
                        windows.Add(new QuotaWindow(modelId ?? label, label,
                            Math.Max(0.0, Math.Min(100.0, (1.0 - remaining.Value) * 100.0)),
                            Time.Parse(quota["resetTime"]), null, rank, remaining.Value <= 0));
                        rank++;
                        if (windows.Count >= limit + 1) break;
                    }
                }
            }
            return Tuple.Create(windows, account, plan);
        }

        private static double? NonZero(double? v) => v == null || v.Value == 0 ? null : v;

        public override bool Detect()
        {
            foreach (var env in new[] { "APPDATA", "LOCALAPPDATA", "USERPROFILE" })
            {
                var b = Environment.GetEnvironmentVariable(env);
                if (string.IsNullOrEmpty(b)) continue;
                foreach (var name in new[] { "Antigravity IDE", "Antigravity", ".antigravity", ".antigravity-ide" })
                    if (Proc.SafeDirExists(Path.Combine(b, name))) return true;
            }
            return Proc.SafeDirExists(Path.Combine(Paths.Home, ".antigravity"));
        }

        public override void Collect(ProviderResult result)
        {
            const string tag = "local language server";
            var manualPort = (int)Core.Settings.Num(Settings, "port", 0);
            var manualCsrf = Core.Settings.Str(Settings, "csrf_token");
            if (manualCsrf.Length == 0) manualCsrf = null;

            var tries = new List<Tuple<string, string>>();
            if (_cached != null) tries.Add(_cached);
            if (manualPort > 0)
                foreach (var scheme in new[] { "https", "http" })
                    tries.Add(Tuple.Create($"{scheme}://127.0.0.1:{manualPort}", manualCsrf));

            var notes = new List<string>();
            if (tries.Count == 0 || _cached == null)
            {
                var found = DiscoverEndpoints();
                notes = found.Item3;
                var token = manualCsrf ?? found.Item2;
                tries.AddRange(found.Item1.Select(u => Tuple.Create(u, token)));
            }
            if (tries.Count == 0)
            {
                result.Attempts.Add(new SourceAttempt(tag, false, notes.Count > 0 ? string.Join("; ", notes) : "no Antigravity language server found"));
                result.Status = result.Installed ? "IDE not running" : "Antigravity not detected";
                return;
            }
            var lastError = "";
            foreach (var t in tries)
            {
                Tuple<List<QuotaWindow>, string, string> parsed;
                try
                {
                    var payload = CallRpc(t.Item1, t.Item2) as JObj ?? new JObj();
                    parsed = ParseUserStatus(payload, Settings);
                }
                catch (Exception e)
                {
                    lastError = $"{t.Item1}: {e.GetType().Name}";
                    continue;
                }
                if (parsed.Item1.Count == 0)
                {
                    lastError = $"{t.Item1}: connected but no quota fields in the response";
                    continue;
                }
                _cached = t;
                result.Windows = parsed.Item1;
                result.Ok = true;
                result.Source = "local language server " + t.Item1;
                result.Account = parsed.Item2;
                result.Plan = parsed.Item3;
                result.Status = "connected";
                result.DataTime = Time.Now;
                result.Attempts.Add(new SourceAttempt(tag, true, t.Item1));
                return;
            }
            _cached = null;
            result.Attempts.Add(new SourceAttempt(tag, false,
                lastError.Length > 0 ? lastError : notes.Count > 0 ? string.Join("; ", notes) : "no endpoint responded"));
            result.Status = "IDE not running or not signed in";
        }
    }
}
