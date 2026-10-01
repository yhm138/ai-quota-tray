using System;
using System.IO;
using System.Text;

namespace QuotaTray.Core
{
    /// <summary>Reading files without blocking the program that owns them.</summary>
    public static class Io
    {
        /// <summary>
        /// Read a text file with full sharing (read/write/delete). CLIs like
        /// gemini-cli, codex and Claude Code refresh their token on start and
        /// replace the credential file with an atomic rename; that rename is
        /// denied while another process holds the file open without
        /// FileShare.Delete (File.ReadAllText's default is FileShare.Read). So
        /// every credential read goes through here, or QuotaTray would make the
        /// owning CLI fail to persist its refreshed token and re-authorize on
        /// each run. Returns null if the file cannot be read.
        /// </summary>
        public static string ReadAllTextShared(string path)
        {
            try
            {
                using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read,
                           FileShare.ReadWrite | FileShare.Delete))
                using (var reader = new StreamReader(fs, Encoding.UTF8, detectEncodingFromByteOrderMarks: true))
                    return reader.ReadToEnd();
            }
            catch (Exception)
            {
                return null;
            }
        }
    }
}
