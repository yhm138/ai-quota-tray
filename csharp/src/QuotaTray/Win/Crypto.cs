using System;
using System.ComponentModel;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;

namespace QuotaTray.Win
{
    public sealed class CryptoError : Exception
    {
        public CryptoError(string message) : base(message) { }
    }

    /// <summary>DPAPI (CryptUnprotectData) for the current Windows user.</summary>
    public static class Dpapi
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct Blob
        {
            public int cbData;
            public IntPtr pbData;
        }

        [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern bool CryptUnprotectData(ref Blob dataIn, IntPtr description, IntPtr entropy,
            IntPtr reserved, IntPtr prompt, int flags, ref Blob dataOut);

        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr mem);

        public static byte[] Unprotect(byte[] data)
        {
            var handle = GCHandle.Alloc(data, GCHandleType.Pinned);
            try
            {
                var input = new Blob { cbData = data.Length, pbData = handle.AddrOfPinnedObject() };
                var output = new Blob();
                const int uiForbidden = 0x1;
                if (!CryptUnprotectData(ref input, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, uiForbidden, ref output))
                    throw new CryptoError("DPAPI could not unwrap the key (" + new Win32Exception(Marshal.GetLastWin32Error()).Message + ")");
                try
                {
                    var result = new byte[output.cbData];
                    Marshal.Copy(output.pbData, result, 0, output.cbData);
                    return result;
                }
                finally { LocalFree(output.pbData); }
            }
            finally { handle.Free(); }
        }
    }

    /// <summary>
    /// AES-GCM decryption. .NET Framework has no AesGcm class, so Windows'
    /// own CNG (bcrypt.dll) does it; the .NET 8 test build uses AesGcm.
    /// </summary>
    public static class AesGcmDecryptor
    {
        public const int TagLength = 16;

        // Known-answer vector shared with the Python build: key 00..1f, nonce 64..6f.
        private static readonly byte[] KatKey = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
        private static readonly byte[] KatNonce = Enumerable.Range(100, 12).Select(i => (byte)i).ToArray();
        private static readonly byte[] KatPlain = Encoding.ASCII.GetBytes("QuotaTray AES-GCM self-test");
        private const string KatSealedHex =
            "196eb11218bd24ff47421ead89482dbe0fe2756fe70ade06c2a2d8f4b3e40a873225316aa50f12de16c28a";

        /// <summary>sealed = ciphertext || 16-byte tag. Throws CryptoError on a bad tag.</summary>
        public static byte[] Decrypt(byte[] key, byte[] nonce, byte[] sealedData)
        {
            if (sealedData.Length < TagLength) throw new CryptoError("ciphertext shorter than the GCM tag");
            var cipher = new byte[sealedData.Length - TagLength];
            var tag = new byte[TagLength];
            Buffer.BlockCopy(sealedData, 0, cipher, 0, cipher.Length);
            Buffer.BlockCopy(sealedData, cipher.Length, tag, 0, TagLength);
#if NETFRAMEWORK
            return BCryptDecrypt(key, nonce, cipher, tag);
#else
            var plain = new byte[cipher.Length];
            try
            {
                using (var gcm = new System.Security.Cryptography.AesGcm(key, TagLength))
                    gcm.Decrypt(nonce, cipher, tag, plain);
            }
            catch (System.Security.Cryptography.CryptographicException e)
            {
                throw new CryptoError("authentication failed (" + e.GetType().Name + ")");
            }
            return plain;
#endif
        }

        /// <summary>Decrypt the known-answer vector; throws if the implementation is broken.</summary>
        public static void SelfTest()
        {
            var sealedData = Hex(KatSealedHex);
            if (!Decrypt(KatKey, KatNonce, sealedData).SequenceEqual(KatPlain))
                throw new CryptoError("AES-GCM self-test produced the wrong plaintext");
            sealedData[sealedData.Length - 1] ^= 1;
            try { Decrypt(KatKey, KatNonce, sealedData); }
            catch (CryptoError) { return; }
            throw new CryptoError("AES-GCM self-test accepted a forged tag");
        }

        public static byte[] Hex(string hex)
        {
            var b = new byte[hex.Length / 2];
            for (var i = 0; i < b.Length; i++) b[i] = Convert.ToByte(hex.Substring(i * 2, 2), 16);
            return b;
        }

#if NETFRAMEWORK
        [StructLayout(LayoutKind.Sequential)]
        private struct AuthInfo
        {
            public int cbSize;
            public int dwInfoVersion;
            public IntPtr pbNonce;
            public int cbNonce;
            public IntPtr pbAuthData;
            public int cbAuthData;
            public IntPtr pbTag;
            public int cbTag;
            public IntPtr pbMacContext;
            public int cbMacContext;
            public int cbAAD;
            public long cbData;
            public int dwFlags;
        }

        [DllImport("bcrypt.dll", CharSet = CharSet.Unicode)]
        private static extern int BCryptOpenAlgorithmProvider(out IntPtr alg, string algId, string impl, int flags);

        [DllImport("bcrypt.dll", CharSet = CharSet.Unicode)]
        private static extern int BCryptSetProperty(IntPtr handle, string prop, byte[] input, int size, int flags);

        [DllImport("bcrypt.dll")]
        private static extern int BCryptGenerateSymmetricKey(IntPtr alg, out IntPtr key, IntPtr keyObject,
            int keyObjectSize, byte[] secret, int secretSize, int flags);

        [DllImport("bcrypt.dll")]
        private static extern int BCryptDecrypt(IntPtr key, byte[] input, int inputSize, ref AuthInfo padding,
            IntPtr iv, int ivSize, byte[] output, int outputSize, out int written, int flags);

        [DllImport("bcrypt.dll")]
        private static extern int BCryptDestroyKey(IntPtr key);

        [DllImport("bcrypt.dll")]
        private static extern int BCryptCloseAlgorithmProvider(IntPtr alg, int flags);

        private static void Check(int status, string what)
        {
            if (status != 0) throw new CryptoError($"{what} failed (NTSTATUS 0x{status:X8})");
        }

        private static byte[] BCryptDecrypt(byte[] key, byte[] nonce, byte[] cipher, byte[] tag)
        {
            Check(BCryptOpenAlgorithmProvider(out var alg, "AES", null, 0), "open AES");
            var keyHandle = IntPtr.Zero;
            var nonceH = GCHandle.Alloc(nonce, GCHandleType.Pinned);
            var tagH = GCHandle.Alloc(tag, GCHandleType.Pinned);
            try
            {
                var mode = Encoding.Unicode.GetBytes("ChainingModeGCM\0");
                Check(BCryptSetProperty(alg, "ChainingMode", mode, mode.Length, 0), "select GCM");
                // A null key object lets CNG allocate it (Windows 7+).
                Check(BCryptGenerateSymmetricKey(alg, out keyHandle, IntPtr.Zero, 0, key, key.Length, 0), "import key");
                var info = new AuthInfo
                {
                    cbSize = Marshal.SizeOf(typeof(AuthInfo)),
                    dwInfoVersion = 1,
                    pbNonce = nonceH.AddrOfPinnedObject(),
                    cbNonce = nonce.Length,
                    pbTag = tagH.AddrOfPinnedObject(),
                    cbTag = tag.Length,
                };
                var output = new byte[Math.Max(1, cipher.Length)];
                var status = BCryptDecrypt(keyHandle, cipher, cipher.Length, ref info, IntPtr.Zero, 0,
                    output, cipher.Length, out var written, 0);
                if (status != 0) throw new CryptoError($"authentication failed (NTSTATUS 0x{status:X8})");
                var result = new byte[written];
                Buffer.BlockCopy(output, 0, result, 0, written);
                return result;
            }
            finally
            {
                nonceH.Free();
                tagH.Free();
                if (keyHandle != IntPtr.Zero) BCryptDestroyKey(keyHandle);
                BCryptCloseAlgorithmProvider(alg, 0);
            }
        }
#endif
    }
}
