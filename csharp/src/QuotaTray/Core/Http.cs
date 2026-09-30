using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;

namespace QuotaTray.Core
{
    public sealed class HttpReply
    {
        public int Status;
        public string Text = "";
        public Dictionary<string, string> Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        public HttpReply(int status, string text = "")
        {
            Status = status; Text = text ?? "";
        }

        public object Json() => Core.Json.TryParse(Text, out var v) ? v : throw new FormatException("response was not JSON");

        public string Header(string name) => Headers.TryGetValue(name, out var v) ? v : "";
    }

    public sealed class HttpRequest
    {
        public string Method = "GET";
        public string Url;
        public Dictionary<string, string> Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        public string Body;                     // JSON body for POST
        public int TimeoutSeconds = 20;
        public bool Insecure;                   // localhost only: self-signed language server
        public bool FollowRedirects = true;
    }

    /// <summary>
    /// All HTTP goes through Http.Send, which tests replace with a fake.
    /// The real one uses the system proxy and certificate store.
    /// </summary>
    public static class Http
    {
        public static Func<HttpRequest, HttpReply> Send = RealSend;

        private static HttpClient _client, _insecure, _noRedirect;
        private static readonly object Gate = new object();

        public static HttpReply Get(string url, Dictionary<string, string> headers = null, int timeout = 20,
            bool followRedirects = true)
        {
            var req = new HttpRequest { Url = url, TimeoutSeconds = timeout, FollowRedirects = followRedirects };
            if (headers != null) foreach (var kv in headers) req.Headers[kv.Key] = kv.Value;
            return Send(req);
        }

        public static string Query(string url, params string[] pairs)
        {
            var sb = new StringBuilder(url);
            for (var i = 0; i + 1 < pairs.Length; i += 2)
            {
                sb.Append(i == 0 && !url.Contains("?") ? '?' : '&');
                sb.Append(Uri.EscapeDataString(pairs[i])).Append('=').Append(Uri.EscapeDataString(pairs[i + 1]));
            }
            return sb.ToString();
        }

        private static HttpClient Client(HttpRequest req)
        {
            lock (Gate)
            {
                if (_client == null)
                {
                    try
                    {
                        // .NET Framework only offers what it is told to on older setups.
                        ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
                    }
                    catch (NotSupportedException) { }
                    _client = new HttpClient(new HttpClientHandler
                    {
                        AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate,
                        UseProxy = true,
                    }) { Timeout = Timeout.InfiniteTimeSpan };
                    _noRedirect = new HttpClient(new HttpClientHandler
                    {
                        AllowAutoRedirect = false,
                        UseProxy = true,
                    }) { Timeout = Timeout.InfiniteTimeSpan };
                    _insecure = new HttpClient(new HttpClientHandler
                    {
                        UseProxy = false,
                        ServerCertificateCustomValidationCallback = (m, c, ch, e) => true,
                    }) { Timeout = Timeout.InfiniteTimeSpan };
                }
                if (req.Insecure) return _insecure;
                return req.FollowRedirects ? _client : _noRedirect;
            }
        }

        private static HttpReply RealSend(HttpRequest req)
        {
            var msg = new HttpRequestMessage(req.Method == "POST" ? HttpMethod.Post : HttpMethod.Get, req.Url);
            foreach (var kv in req.Headers)
            {
                if (kv.Key.Equals("Content-Type", StringComparison.OrdinalIgnoreCase)) continue;
                msg.Headers.TryAddWithoutValidation(kv.Key, kv.Value);
            }
            if (!req.Headers.ContainsKey("User-Agent"))
                msg.Headers.TryAddWithoutValidation("User-Agent", $"QuotaTray/{AppInfo.Version}");
            if (req.Body != null)
            {
                var type = req.Headers.TryGetValue("Content-Type", out var t) ? t : "application/json";
                msg.Content = new StringContent(req.Body, Encoding.UTF8);
                msg.Content.Headers.ContentType = System.Net.Http.Headers.MediaTypeHeaderValue.Parse(type);
            }
            using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(req.TimeoutSeconds)))
            {
                HttpResponseMessage resp;
                try
                {
                    resp = Client(req).SendAsync(msg, cts.Token).GetAwaiter().GetResult();
                }
                catch (OperationCanceledException)
                {
                    throw new TimeoutException($"no answer within {req.TimeoutSeconds}s");
                }
                catch (HttpRequestException e)
                {
                    throw new IOException(Innermost(e), e);
                }
                using (resp)
                {
                    var reply = new HttpReply((int)resp.StatusCode);
                    foreach (var h in resp.Headers) reply.Headers[h.Key] = string.Join(", ", h.Value);
                    foreach (var h in resp.Content.Headers) reply.Headers[h.Key] = string.Join(", ", h.Value);
                    if (resp.Headers.Location != null) reply.Headers["Location"] = resp.Headers.Location.ToString();
                    var bytes = resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult();
                    reply.Text = Encoding.UTF8.GetString(bytes);
                    return reply;
                }
            }
        }

        /// <summary>The useful part of a nested network exception.</summary>
        public static string Innermost(Exception e)
        {
            while (e.InnerException != null) e = e.InnerException;
            return e.Message;
        }

        /// <summary>Stream a file to disk with progress; false on 404.</summary>
        public static bool Download(string url, string dest, Action<long, long> progress, CancellationToken token = default(CancellationToken))
        {
            var client = Client(new HttpRequest { Url = url });
            var msg = new HttpRequestMessage(HttpMethod.Get, url);
            msg.Headers.TryAddWithoutValidation("User-Agent", $"QuotaTray/{AppInfo.Version}");
            using (var resp = client.SendAsync(msg, HttpCompletionOption.ResponseHeadersRead, token).GetAwaiter().GetResult())
            {
                if ((int)resp.StatusCode == 404) return false;
                if ((int)resp.StatusCode != 200)
                    throw new IOException($"download failed: HTTP {(int)resp.StatusCode} for {url.Split('/').Last()}");
                var total = resp.Content.Headers.ContentLength ?? 0;
                var tmp = dest + ".part";
                using (var src = resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult())
                using (var fs = new FileStream(tmp, FileMode.Create, FileAccess.Write))
                {
                    var buf = new byte[1 << 16];
                    long done = 0;
                    int n;
                    while ((n = src.Read(buf, 0, buf.Length)) > 0)
                    {
                        token.ThrowIfCancellationRequested();
                        fs.Write(buf, 0, n);
                        done += n;
                        progress?.Invoke(done, total);
                    }
                }
                if (File.Exists(dest)) File.Delete(dest);
                File.Move(tmp, dest);
                return true;
            }
        }
    }
}
