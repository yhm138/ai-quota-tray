using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace QuotaTray.Core
{
    /// <summary>A JSON object that keeps its keys in document order.</summary>
    public sealed class JObj : IEnumerable<KeyValuePair<string, object>>
    {
        private readonly List<string> _keys = new List<string>();
        private readonly Dictionary<string, object> _map = new Dictionary<string, object>(StringComparer.Ordinal);

        public int Count => _keys.Count;
        public IEnumerable<string> Keys => _keys;

        public object this[string key]
        {
            get { object v; return key != null && _map.TryGetValue(key, out v) ? v : null; }
            set
            {
                if (!_map.ContainsKey(key)) _keys.Add(key);
                _map[key] = value;
            }
        }

        public bool Has(string key) => key != null && _map.ContainsKey(key);

        public void Remove(string key)
        {
            if (_map.Remove(key)) _keys.Remove(key);
        }

        public IEnumerator<KeyValuePair<string, object>> GetEnumerator()
        {
            foreach (var k in _keys.ToArray()) yield return new KeyValuePair<string, object>(k, _map[k]);
        }

        IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();

        // Typed accessors: a wrong type reads as missing, the way the Python
        // code's isinstance() checks treat it.
        public JObj Obj(string key) => this[key] as JObj;
        public List<object> Arr(string key) => this[key] as List<object>;
        public string Str(string key) => this[key] as string;
        public double? Num(string key) => Json.AsNumber(this[key]);
        public bool? Bool(string key) => this[key] is bool b ? b : (bool?)null;

        /// <summary>A non-empty string, or null.</summary>
        public string Text(string key)
        {
            var s = this[key] as string;
            return string.IsNullOrEmpty(s) ? null : s;
        }

        /// <summary>Python truthiness of a value.</summary>
        public bool Truthy(string key) => Json.Truthy(this[key]);
    }

    /// <summary>Small JSON reader/writer (System.Text.Json is not part of .NET Framework).</summary>
    public static class Json
    {
        public sealed class ParseError : FormatException
        {
            public ParseError(string message) : base(message) { }
        }

        public static object Parse(string text)
        {
            if (text == null) throw new ParseError("no input");
            var p = new Reader(text);
            p.SkipWs();
            var value = p.Value(0);
            p.SkipWs();
            if (!p.End) throw new ParseError("trailing characters at " + p.Pos);
            return value;
        }

        public static bool TryParse(string text, out object value)
        {
            try { value = Parse(text); return true; }
            catch (FormatException) { value = null; return false; }
        }

        public static JObj ParseObject(string text) => TryParse(text, out var v) ? v as JObj : null;

        public static double? AsNumber(object v)
        {
            switch (v)
            {
                case double d: return d;
                case int i: return i;
                case long l: return l;
                case float f: return f;
                case decimal m: return (double)m;
                default: return null;                  // bools are not numbers here
            }
        }

        /// <summary>Numbers, or strings holding a number (like Python's float(str)).</summary>
        public static double? ToFloat(object v)
        {
            var n = AsNumber(v);
            if (n != null) return n;
            if (v is string s && double.TryParse(s.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var d)) return d;
            return null;
        }

        public static bool Truthy(object v)
        {
            switch (v)
            {
                case null: return false;
                case bool b: return b;
                case string s: return s.Length > 0;
                case JObj o: return o.Count > 0;
                case List<object> l: return l.Count > 0;
                default: var n = AsNumber(v); return n == null || n.Value != 0;
            }
        }

        // ---------------------------------------------------------- writing

        public static string Write(object value, bool indent = false)
        {
            var sb = new StringBuilder();
            WriteValue(sb, value, indent, 0);
            return sb.ToString();
        }

        private static void NewLine(StringBuilder sb, bool indent, int level)
        {
            if (!indent) return;
            sb.Append('\n');
            sb.Append(' ', level * 2);
        }

        private static void WriteValue(StringBuilder sb, object value, bool indent, int level)
        {
            switch (value)
            {
                case null: sb.Append("null"); return;
                case bool b: sb.Append(b ? "true" : "false"); return;
                case string s: WriteString(sb, s); return;
                case JObj o:
                    if (o.Count == 0) { sb.Append("{}"); return; }
                    sb.Append('{');
                    var first = true;
                    foreach (var kv in o)
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        NewLine(sb, indent, level + 1);
                        WriteString(sb, kv.Key);
                        sb.Append(indent ? ": " : ":");
                        WriteValue(sb, kv.Value, indent, level + 1);
                    }
                    NewLine(sb, indent, level);
                    sb.Append('}');
                    return;
                case IList l:
                    if (l.Count == 0) { sb.Append("[]"); return; }
                    sb.Append('[');
                    for (var i = 0; i < l.Count; i++)
                    {
                        if (i > 0) sb.Append(',');
                        NewLine(sb, indent, level + 1);
                        WriteValue(sb, l[i], indent, level + 1);
                    }
                    NewLine(sb, indent, level);
                    sb.Append(']');
                    return;
                default:
                    var n = AsNumber(value);
                    if (n == null) { WriteString(sb, value.ToString()); return; }
                    var d = n.Value;
                    if (double.IsNaN(d) || double.IsInfinity(d)) { sb.Append("null"); return; }
                    if (d == Math.Floor(d) && Math.Abs(d) < 1e15) sb.Append(((long)d).ToString(CultureInfo.InvariantCulture));
                    else sb.Append(d.ToString("R", CultureInfo.InvariantCulture));
                    return;
            }
        }

        private static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        // ---------------------------------------------------------- reading

        private sealed class Reader
        {
            private readonly string _s;
            public int Pos;

            public Reader(string s)
            {
                _s = s;
                if (_s.Length > 0 && _s[0] == '\ufeff') Pos = 1;
            }

            public bool End => Pos >= _s.Length;

            public void SkipWs()
            {
                while (Pos < _s.Length && (_s[Pos] == ' ' || _s[Pos] == '\t' || _s[Pos] == '\n' || _s[Pos] == '\r')) Pos++;
            }

            private char Peek() => Pos < _s.Length ? _s[Pos] : '\0';

            private Exception Fail(string what) => new ParseError(what + " at " + Pos);

            public object Value(int depth)
            {
                if (depth > 256) throw Fail("nesting too deep");
                SkipWs();
                var c = Peek();
                switch (c)
                {
                    case '{': return Object(depth);
                    case '[': return Array(depth);
                    case '"': return String();
                    case 't': Literal("true"); return true;
                    case 'f': Literal("false"); return false;
                    case 'n': Literal("null"); return null;
                    default:
                        if (c == '-' || (c >= '0' && c <= '9')) return Number();
                        throw Fail("unexpected character '" + c + "'");
                }
            }

            private void Literal(string word)
            {
                if (string.CompareOrdinal(_s, Pos, word, 0, word.Length) != 0) throw Fail("bad literal");
                Pos += word.Length;
            }

            private JObj Object(int depth)
            {
                var o = new JObj();
                Pos++;
                SkipWs();
                if (Peek() == '}') { Pos++; return o; }
                while (true)
                {
                    SkipWs();
                    if (Peek() != '"') throw Fail("expected a key");
                    var key = String();
                    SkipWs();
                    if (Peek() != ':') throw Fail("expected ':'");
                    Pos++;
                    o[key] = Value(depth + 1);
                    SkipWs();
                    var c = Peek();
                    Pos++;
                    if (c == ',') continue;
                    if (c == '}') return o;
                    throw Fail("expected ',' or '}'");
                }
            }

            private List<object> Array(int depth)
            {
                var l = new List<object>();
                Pos++;
                SkipWs();
                if (Peek() == ']') { Pos++; return l; }
                while (true)
                {
                    l.Add(Value(depth + 1));
                    SkipWs();
                    var c = Peek();
                    Pos++;
                    if (c == ',') continue;
                    if (c == ']') return l;
                    throw Fail("expected ',' or ']'");
                }
            }

            private string String()
            {
                Pos++;
                var sb = new StringBuilder();
                while (true)
                {
                    if (Pos >= _s.Length) throw Fail("unterminated string");
                    var c = _s[Pos++];
                    if (c == '"') return sb.ToString();
                    if (c != '\\') { sb.Append(c); continue; }
                    if (Pos >= _s.Length) throw Fail("bad escape");
                    var e = _s[Pos++];
                    switch (e)
                    {
                        case '"': sb.Append('"'); break;
                        case '\\': sb.Append('\\'); break;
                        case '/': sb.Append('/'); break;
                        case 'b': sb.Append('\b'); break;
                        case 'f': sb.Append('\f'); break;
                        case 'n': sb.Append('\n'); break;
                        case 'r': sb.Append('\r'); break;
                        case 't': sb.Append('\t'); break;
                        case 'u':
                            if (Pos + 4 > _s.Length) throw Fail("bad \\u escape");
                            if (!int.TryParse(_s.Substring(Pos, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var code))
                                throw Fail("bad \\u escape");
                            sb.Append((char)code);
                            Pos += 4;
                            break;
                        default: throw Fail("bad escape");
                    }
                }
            }

            private object Number()
            {
                var start = Pos;
                if (Peek() == '-') Pos++;
                while (Pos < _s.Length && "0123456789.eE+-".IndexOf(_s[Pos]) >= 0) Pos++;
                var text = _s.Substring(start, Pos - start);
                if (!double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var d))
                    throw Fail("bad number '" + text + "'");
                return d;
            }
        }
    }
}
