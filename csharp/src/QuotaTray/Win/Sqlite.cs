using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace QuotaTray.Win
{
    /// <summary>
    /// A tiny read-only SQLite reader, enough to scan a Chromium cookie store
    /// without a native library (the C# build ships as one managed exe). It
    /// walks table b-trees and decodes records; it does not run SQL.
    /// </summary>
    public sealed class Sqlite : IDisposable
    {
        private readonly byte[] _data;
        private readonly int _pageSize;
        private readonly int _usableSize;

        private Sqlite(byte[] data)
        {
            _data = data;
            if (data.Length < 100 || Encoding.ASCII.GetString(data, 0, 15) != "SQLite format 3")
                throw new InvalidDataException("not a SQLite database");
            var raw = (data[16] << 8) | data[17];
            _pageSize = raw == 1 ? 65536 : raw;
            if (_pageSize < 512 || (_pageSize & (_pageSize - 1)) != 0) throw new InvalidDataException("bad page size");
            _usableSize = _pageSize - data[20];               // reserved bytes at page end
        }

        public static Sqlite Open(string path)
        {
            // Copy first: the app may hold the file open (share read/write/delete).
            byte[] bytes;
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            using (var ms = new MemoryStream())
            {
                fs.CopyTo(ms);
                bytes = ms.ToArray();
            }
            return new Sqlite(bytes);
        }

        public void Dispose() { }

        public sealed class Table
        {
            public string Name;
            public int RootPage;
            public string Sql;
            public List<string> Columns = new List<string>();
            public int RowidAlias = -1;                       // column that is INTEGER PRIMARY KEY (aliases rowid)
        }

        /// <summary>The user tables (name -> table), from sqlite_master on page 1.</summary>
        public Dictionary<string, Table> Tables()
        {
            var tables = new Dictionary<string, Table>(StringComparer.OrdinalIgnoreCase);
            foreach (var rec in ReadTable(1))
            {
                if (rec.Values.Count < 5 || !(rec.Values[0] is string type) || type != "table") continue;
                var name = rec.Values[1] as string;
                var root = ToInt(rec.Values[3]);
                var sql = rec.Values[4] as string;
                if (name == null || root == null) continue;
                var t = new Table { Name = name, RootPage = root.Value, Sql = sql ?? "" };
                ParseColumns(t);
                tables[name] = t;
            }
            return tables;
        }

        public sealed class Row
        {
            public long Rowid;
            public List<object> Values;
        }

        /// <summary>Every row of the table rooted at rootPage.</summary>
        public IEnumerable<Row> ReadTable(int rootPage)
        {
            var seen = new HashSet<int>();
            foreach (var row in WalkTable(rootPage, seen, 0)) yield return row;
        }

        private IEnumerable<Row> WalkTable(int page, HashSet<int> seen, int depth)
        {
            if (page < 1 || depth > 64 || !seen.Add(page)) yield break;
            var baseOffset = (page - 1) * _pageSize;
            var headerOffset = page == 1 ? baseOffset + 100 : baseOffset;
            var type = _data[headerOffset];
            var cellCount = U16(headerOffset + 3);
            var cellPtrArray = headerOffset + (type == 5 || type == 2 ? 12 : 8);
            if (type == 13)                                    // table leaf
            {
                for (var i = 0; i < cellCount; i++)
                {
                    var cell = baseOffset + U16(cellPtrArray + i * 2);
                    var row = ReadLeafCell(cell);
                    if (row != null) yield return row;
                }
            }
            else if (type == 5)                                // table interior
            {
                for (var i = 0; i < cellCount; i++)
                {
                    var cell = baseOffset + U16(cellPtrArray + i * 2);
                    var child = (int)U32(cell);
                    foreach (var row in WalkTable(child, seen, depth + 1)) yield return row;
                }
                var rightMost = (int)U32(headerOffset + 8);
                foreach (var row in WalkTable(rightMost, seen, depth + 1)) yield return row;
            }
        }

        private Row ReadLeafCell(int offset)
        {
            var payloadLen = Varint(ref offset);
            var rowid = Varint(ref offset);
            // Overflow: keep only the in-page portion. Cookie rows are small, so
            // the fields we read (host_key, name, encrypted_value) fit on-page in
            // practice; a spilled record simply yields fewer/blank trailing values.
            var maxLocal = _usableSize - 35;
            var local = payloadLen <= maxLocal ? (int)payloadLen
                : Math.Min((int)payloadLen, maxLocal);
            if (offset + local > _data.Length) local = Math.Max(0, _data.Length - offset);
            var record = new byte[local];
            Buffer.BlockCopy(_data, offset, record, 0, local);
            return new Row { Rowid = rowid, Values = DecodeRecord(record, rowid) };
        }

        private List<object> DecodeRecord(byte[] rec, long rowid)
        {
            var values = new List<object>();
            var pos = 0;
            var headerLen = (int)Varint(rec, ref pos);
            var serialTypes = new List<long>();
            while (pos < headerLen && pos < rec.Length) serialTypes.Add(Varint(rec, ref pos));
            var body = headerLen;
            foreach (var st in serialTypes)
            {
                object value;
                var size = SerialSize(st);
                if (body + size > rec.Length && st != 0 && !(st == 8 || st == 9))
                {
                    values.Add(null);                          // spilled to overflow
                    body += size;
                    continue;
                }
                switch (st)
                {
                    case 0: value = null; break;               // NULL (rowid alias filled in later)
                    case 8: value = 0L; break;
                    case 9: value = 1L; break;
                    case 1: value = (long)(sbyte)rec[body]; break;
                    case 2: value = (long)(short)((rec[body] << 8) | rec[body + 1]); break;
                    case 3: value = SignedBe(rec, body, 3); break;
                    case 4: value = SignedBe(rec, body, 4); break;
                    case 5: value = SignedBe(rec, body, 6); break;
                    case 6: value = SignedBe(rec, body, 8); break;
                    case 7:
                        value = BitConverter.Int64BitsToDouble(SignedBe(rec, body, 8));
                        break;
                    default:
                        if (st >= 12 && st % 2 == 0)           // BLOB
                        {
                            var blob = new byte[size];
                            Buffer.BlockCopy(rec, body, blob, 0, Math.Min(size, rec.Length - body));
                            value = blob;
                        }
                        else if (st >= 13)                     // TEXT
                            value = Encoding.UTF8.GetString(rec, body, Math.Min(size, rec.Length - body));
                        else value = null;
                        break;
                }
                values.Add(value);
                body += size;
            }
            return values;
        }

        private static long SignedBe(byte[] b, int off, int len)
        {
            long v = (b[off] & 0x80) != 0 ? -1L : 0L;          // sign-extend
            for (var i = 0; i < len; i++) v = (v << 8) | b[off + i];
            return v;
        }

        private static int SerialSize(long st)
        {
            switch (st)
            {
                case 0: case 8: case 9: return 0;
                case 1: return 1;
                case 2: return 2;
                case 3: return 3;
                case 4: return 4;
                case 5: return 6;
                case 6: case 7: return 8;
                default: return (int)((st - (st % 2 == 0 ? 12 : 13)) / 2);
            }
        }

        // ------------------------------------------------------------ column names

        private static void ParseColumns(Table t)
        {
            var sql = t.Sql ?? "";
            var open = sql.IndexOf('(');
            var close = sql.LastIndexOf(')');
            if (open < 0 || close <= open) return;
            var inner = sql.Substring(open + 1, close - open - 1);
            foreach (var part in SplitTopLevel(inner))
            {
                var col = part.Trim();
                if (col.Length == 0) continue;
                var upper = col.ToUpperInvariant();
                if (upper.StartsWith("PRIMARY KEY") || upper.StartsWith("UNIQUE") || upper.StartsWith("CONSTRAINT")
                    || upper.StartsWith("CHECK") || upper.StartsWith("FOREIGN KEY")) continue;
                var name = ColumnName(col);
                if (name == null) continue;
                if (upper.Contains("INTEGER PRIMARY KEY")) t.RowidAlias = t.Columns.Count;
                t.Columns.Add(name);
            }
        }

        private static string ColumnName(string def)
        {
            var i = 0;
            while (i < def.Length && char.IsWhiteSpace(def[i])) i++;
            if (i >= def.Length) return null;
            if (def[i] == '"' || def[i] == '`' || def[i] == '[')
            {
                var end = def[i] == '[' ? ']' : def[i];
                var close = def.IndexOf(end, i + 1);
                return close > i ? def.Substring(i + 1, close - i - 1) : null;
            }
            var start = i;
            while (i < def.Length && (char.IsLetterOrDigit(def[i]) || def[i] == '_')) i++;
            return i > start ? def.Substring(start, i - start) : null;
        }

        private static IEnumerable<string> SplitTopLevel(string s)
        {
            var depth = 0;
            var start = 0;
            var inStr = false;
            char strCh = '\0';
            for (var i = 0; i < s.Length; i++)
            {
                var c = s[i];
                if (inStr) { if (c == strCh) inStr = false; continue; }
                if (c == '\'' || c == '"' || c == '`') { inStr = true; strCh = c; }
                else if (c == '(') depth++;
                else if (c == ')') depth--;
                else if (c == ',' && depth == 0) { yield return s.Substring(start, i - start); start = i + 1; }
            }
            yield return s.Substring(start);
        }

        // ------------------------------------------------------------ primitives

        private int U16(int off) => (_data[off] << 8) | _data[off + 1];
        private uint U32(int off) => (uint)((_data[off] << 24) | (_data[off + 1] << 16) | (_data[off + 2] << 8) | _data[off + 3]);

        private long Varint(ref int off)
        {
            long value = 0;
            for (var i = 0; i < 9; i++)
            {
                var b = _data[off++];
                if (i == 8) { value = (value << 8) | b; break; }
                value = (value << 7) | (b & 0x7FL);
                if ((b & 0x80) == 0) break;
            }
            return value;
        }

        private static long Varint(byte[] buf, ref int off)
        {
            long value = 0;
            for (var i = 0; i < 9 && off < buf.Length; i++)
            {
                var b = buf[off++];
                if (i == 8) { value = (value << 8) | b; break; }
                value = (value << 7) | (b & 0x7FL);
                if ((b & 0x80) == 0) break;
            }
            return value;
        }

        private static int? ToInt(object v) => v is long l ? (int)l : (int?)null;
    }
}
