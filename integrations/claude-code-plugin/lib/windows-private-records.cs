// Windows authority and durable I/O for one session's content-free records.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using Microsoft.Win32.SafeHandles;

public sealed class WindowsPrivateRecords
{
    const uint READ_CONTROL = 0x20000, READ_ATTRIBUTES = 0x80;
    const uint GENERIC_READ = 0x80000000, GENERIC_WRITE = 0x40000000;
    const uint OPEN_EXISTING = 3, CREATE_NEW = 1;
    const uint OPEN_REPARSE = 0x00200000, BACKUP_SEMANTICS = 0x02000000;
    const uint WRITE_THROUGH = 0x80000000, NORMAL = 0x80;
    const uint DIRECTORY = 0x10, REPARSE = 0x400;
    const int EXISTS = 183, FILE_EXISTS = 80, FULL = 0x001f01ff;
    const int MaxRecord = 65536;
    const int AliasClaimMilliseconds = 500;
    readonly string basePath;
    readonly SecurityIdentifier principal;
    readonly byte[] descriptor;

    [StructLayout(LayoutKind.Sequential)]
    struct SecurityAttributes { public int Length; public IntPtr Descriptor; public int Inherit; }
    [StructLayout(LayoutKind.Sequential)]
    struct FileInformation {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    static extern SafeFileHandle CreateFileW(string p, uint access, uint share, ref SecurityAttributes sa,
        uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    static extern bool CreateDirectoryW(string p, ref SecurityAttributes sa);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle h, out FileInformation info);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint GetFileType(SafeFileHandle h);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetFileSizeEx(SafeFileHandle h, out long size);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool ReadFile(SafeFileHandle h, [Out] byte[] bytes, uint length, out uint read, IntPtr overlap);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool WriteFile(SafeFileHandle h, byte[] bytes, uint length, out uint written, IntPtr overlap);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetFilePointerEx(SafeFileHandle h, long offset, out long position, uint origin);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool FlushFileBuffers(SafeFileHandle h);
    [DllImport("kernel32.dll")]
    static extern IntPtr GetCurrentProcess();
    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool OpenProcessToken(IntPtr process, uint access, out SafeFileHandle token);
    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool GetTokenInformation(SafeFileHandle token, int kind, IntPtr data, uint length, out uint needed);
    [DllImport("advapi32.dll", SetLastError = true)]
    static extern uint GetSecurityInfo(SafeFileHandle h, int kind, uint fields, out IntPtr owner,
        out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr sd);
    [DllImport("advapi32.dll")]
    static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
    [DllImport("kernel32.dll")]
    static extern IntPtr LocalFree(IntPtr memory);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    static extern bool GetVolumeInformationW(string root, StringBuilder volumeName, uint volumeCapacity,
        out uint serial, out uint maxComponent, out uint flags, StringBuilder fsName, uint fsCapacity);

    sealed class Unavailable : InvalidOperationException {
        public readonly int NativeCode;
        public Unavailable(int nativeCode = 0) : base("Session protection state could not be confirmed") { NativeCode = nativeCode; }
    }
    static void Require(bool value) {
        if (!value) throw new Unavailable();
    }

    static SecurityIdentifier ProcessPrincipal() {
        SafeFileHandle token;
        Require(OpenProcessToken(GetCurrentProcess(), 8, out token)); // TOKEN_QUERY
        using (token) {
            uint needed;
            bool first = GetTokenInformation(token, 1, IntPtr.Zero, 0, out needed); // TokenUser
            int firstError = Marshal.GetLastWin32Error();
            Require(!first && firstError == 122 && needed > 0 && needed < 65536);
            IntPtr data = Marshal.AllocHGlobal((int)needed);
            try {
                Require(GetTokenInformation(token, 1, data, needed, out needed));
                return new SecurityIdentifier(Marshal.ReadIntPtr(data));
            } finally { Marshal.FreeHGlobal(data); }
        }
    }

    public WindowsPrivateRecords(string trustedStartupBase) {
        basePath = Path.GetFullPath(trustedStartupBase).TrimEnd('\\');
        // This implementation requires local NTFS with persistent ACLs.
        Require(Regex.IsMatch(basePath, @"^[a-zA-Z]:\\.+$"));
        string volumeRoot = Path.GetPathRoot(basePath);
        uint serial, max, flags;
        StringBuilder fsName = new StringBuilder(32);
        Require(GetVolumeInformationW(volumeRoot, null, 0, out serial, out max, out flags, fsName, 32));
        Require((flags & 8) != 0 && String.Equals(fsName.ToString(), "NTFS", StringComparison.OrdinalIgnoreCase));
        principal = ProcessPrincipal();
        RawSecurityDescriptor sd = new RawSecurityDescriptor("O:" + principal.Value + "D:P(A;;FA;;;" + principal.Value + ")");
        descriptor = new byte[sd.BinaryLength];
        sd.GetBinaryForm(descriptor, 0);
    }

    SecurityAttributes Attributes(out IntPtr allocated) {
        allocated = Marshal.AllocHGlobal(descriptor.Length);
        Marshal.Copy(descriptor, 0, allocated, descriptor.Length);
        return new SecurityAttributes { Length = Marshal.SizeOf(typeof(SecurityAttributes)), Descriptor = allocated, Inherit = 0 };
    }

    void Check(SafeFileHandle handle, bool directory, bool privateAcl) {
        FileInformation info = new FileInformation();
        Require(!handle.IsInvalid && GetFileInformationByHandle(handle, out info));
        Require(GetFileType(handle) == 1); // FILE_TYPE_DISK
        Require((info.Attributes & REPARSE) == 0 && ((info.Attributes & DIRECTORY) != 0) == directory);
        if (!directory) Require(info.Links == 1);
        if (!privateAcl) return;
        IntPtr owner, group, dacl, sacl, sd;
        Require(GetSecurityInfo(handle, 1, 5, out owner, out group, out dacl, out sacl, out sd) == 0);
        try {
            Require(owner != IntPtr.Zero && dacl != IntPtr.Zero && sd != IntPtr.Zero);
            uint length = GetSecurityDescriptorLength(sd);
            Require(length > 0 && length <= 65536);
            byte[] bytes = new byte[(int)length];
            Marshal.Copy(sd, bytes, 0, bytes.Length);
            RawSecurityDescriptor actual = new RawSecurityDescriptor(bytes, 0);
            Require(principal.Equals(actual.Owner));
            Require((actual.ControlFlags & ControlFlags.DiscretionaryAclPresent) != 0);
            Require((actual.ControlFlags & ControlFlags.DiscretionaryAclProtected) != 0);
            Require(actual.DiscretionaryAcl != null && actual.DiscretionaryAcl.Count == 1);
            CommonAce ace = actual.DiscretionaryAcl[0] as CommonAce;
            Require(ace != null && !ace.IsCallback && ace.AceQualifier == AceQualifier.AccessAllowed);
            Require(ace.AceFlags == AceFlags.None && ace.AccessMask == FULL && principal.Equals(ace.SecurityIdentifier));
        } finally { if (sd != IntPtr.Zero) LocalFree(sd); }
    }

    SafeFileHandle OpenDirectory(string p, bool privateAcl) {
        SecurityAttributes empty = new SecurityAttributes { Length = Marshal.SizeOf(typeof(SecurityAttributes)) };
        // No FILE_SHARE_DELETE: retained directory handles exclude rename/delete.
        SafeFileHandle h = CreateFileW(p, READ_CONTROL | READ_ATTRIBUTES, 3, ref empty,
            OPEN_EXISTING, OPEN_REPARSE | BACKUP_SEMANTICS, IntPtr.Zero);
        if (h.IsInvalid) { int error = Marshal.GetLastWin32Error(); h.Dispose(); throw new Unavailable(error); }
        try { Check(h, true, privateAcl); return h; } catch { h.Dispose(); throw; }
    }

    void CreateDirectoryIfMissing(string p) {
        IntPtr allocation;
        SecurityAttributes sa = Attributes(out allocation);
        try {
            if (!CreateDirectoryW(p, ref sa)) Require(Marshal.GetLastWin32Error() == EXISTS);
        } finally { Marshal.FreeHGlobal(allocation); }
        // An existing directory is inspected below, never chmod/repaired.
    }

    static void CheckComponent(string component) {
        Require(component != null && Regex.IsMatch(component, @"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$"));
        Require(!component.EndsWith(".", StringComparison.Ordinal));
        string stem = component.Split('.')[0];
        Require(!Regex.IsMatch(stem, @"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", RegexOptions.IgnoreCase));
    }

    // External ancestors are pinned and checked for reparse/type. The actual
    // privacy base and every descendant must also pass the exact owner/DACL.
    List<SafeFileHandle> PinDirectories(string[] components, bool create, out string leaf) {
        Require(components != null && components.Length > 0 && components.Length <= 4);
        foreach (string component in components) CheckComponent(component);
        List<SafeFileHandle> held = new List<SafeFileHandle>();
        try {
            string root = Path.GetPathRoot(basePath);
            held.Add(OpenDirectory(root, false));
            string current = root;
            string[] baseParts = basePath.Substring(root.Length).Split('\\');
            for (int i = 0; i < baseParts.Length; i++) {
                // Startup components may contain spaces, but no alternate stream.
                Require(baseParts[i].Length > 0 && baseParts[i].IndexOf(':') < 0);
                current = Path.Combine(current, baseParts[i]);
                if (create) CreateDirectoryIfMissing(current);
                held.Add(OpenDirectory(current, i == baseParts.Length - 1));
            }
            for (int i = 0; i < components.Length - 1; i++) {
                current = Path.Combine(current, components[i]);
                if (create) CreateDirectoryIfMissing(current);
                held.Add(OpenDirectory(current, true));
            }
            leaf = Path.Combine(current, components[components.Length - 1]);
            return held;
        } catch { CloseDirectories(held); throw; }
    }

    static void CloseDirectories(List<SafeFileHandle> handles) {
        for (int i = handles.Count - 1; i >= 0; i--) handles[i].Dispose();
    }

    SafeFileHandle OpenRecord(string file, bool write) {
        SecurityAttributes empty = new SecurityAttributes { Length = Marshal.SizeOf(typeof(SecurityAttributes)) };
        SafeFileHandle h = CreateFileW(file, GENERIC_READ | READ_CONTROL | READ_ATTRIBUTES | (write ? GENERIC_WRITE : 0),
            write ? 0u : 1u, ref empty, OPEN_EXISTING, OPEN_REPARSE | (write ? WRITE_THROUGH : 0), IntPtr.Zero);
        if (h.IsInvalid) { int error = Marshal.GetLastWin32Error(); h.Dispose(); throw new Unavailable(error); }
        try { Check(h, false, true); return h; } catch { h.Dispose(); throw; }
    }

    byte[] ReadChecked(SafeFileHandle h) {
        long length;
        Require(GetFileSizeEx(h, out length) && length >= 0 && length <= MaxRecord);
        byte[] bytes = new byte[(int)length];
        if (length != 0) {
            uint count;
            Require(ReadFile(h, bytes, (uint)bytes.Length, out count, IntPtr.Zero) && count == bytes.Length);
        }
        return bytes;
    }

    static void WriteAndFlush(SafeFileHandle h, byte[] bytes) {
        Require(bytes != null && bytes.Length <= MaxRecord);
        uint written;
        Require(WriteFile(h, bytes, (uint)bytes.Length, out written, IntPtr.Zero) && written == bytes.Length);
        Require(FlushFileBuffers(h));
    }

    public byte[] ReadExisting(string[] components) {
        string leaf;
        List<SafeFileHandle> held = PinDirectories(components, false, out leaf);
        try { using (SafeFileHandle h = OpenRecord(leaf, false)) return ReadChecked(h); }
        finally { CloseDirectories(held); }
    }

    // CreateRoot retains every parent; each record still reopens and checks them.
    void CreateRootRecord(string[] components, byte[] bytes) {
        Require(bytes != null && bytes.Length <= MaxRecord);
        string leaf;
        List<SafeFileHandle> held = PinDirectories(components, false, out leaf);
        try {
            IntPtr allocation;
            SecurityAttributes sa = Attributes(out allocation);
            try {
                using (SafeFileHandle h = CreateFileW(leaf, GENERIC_READ | GENERIC_WRITE | READ_CONTROL | READ_ATTRIBUTES,
                    0, ref sa, CREATE_NEW, NORMAL | OPEN_REPARSE | WRITE_THROUGH, IntPtr.Zero)) {
                    Check(h, false, true);
                    WriteAndFlush(h, bytes);
                }
            } finally { Marshal.FreeHGlobal(allocation); }
        } finally { CloseDirectories(held); }
    }

    public void AppendProtection(string rootId) {
        Require(rootId != null && Regex.IsMatch(rootId, @"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"));
        string leaf;
        List<SafeFileHandle> held = PinDirectories(new string[] { rootId, "state" }, false, out leaf);
        try {
            // Validate semantic fields rather than requiring one JSON encoding.
            string manifestFile = Path.Combine(Path.GetDirectoryName(leaf), "root.json");
            using (SafeFileHandle manifest = OpenRecord(manifestFile, false)) {
                CheckManifest(ReadChecked(manifest), rootId);
                using (SafeFileHandle state = OpenRecord(leaf, true)) {
                    WriteRecordPhase("protect-open");
                    long position;
                    Require(SetFilePointerEx(state, 0, out position, 2));
                    WriteAndFlush(state, new byte[] { 49, 10 });
                    WriteRecordPhase("protect-flushed");
                }
            }
        } finally { CloseDirectories(held); }
    }

    string ConfirmPublishedAlias(string key) {
        string id = FindAlias(key);
        if (id == null) return null;
        string file;
        List<SafeFileHandle> held = PinDirectories(new string[] { "aliases", key + ".json" }, false, out file);
        try {
            using (SafeFileHandle h = OpenRecord(file, true)) {
                Require(CheckAlias(ReadChecked(h), key) == id);
                Require(FlushFileBuffers(h));
            }
            return id;
        } finally { CloseDirectories(held); }
    }

    string WithAliasClaim(string key, string boundRoot, Func<string> operation) {
        Require(key != null && Regex.IsMatch(key, "^[0-9a-f]{64}$"));
        string claimFile;
        List<SafeFileHandle> held = PinDirectories(new string[] { "aliases", key + ".claim" }, true, out claimFile);
        SafeFileHandle claim = null;
        try {
            Stopwatch elapsed = Stopwatch.StartNew();
            while (true) {
                Require(elapsed.ElapsedMilliseconds < AliasClaimMilliseconds);
                IntPtr allocation;
                SecurityAttributes sa = Attributes(out allocation);
                try {
                    claim = CreateFileW(claimFile, GENERIC_READ | GENERIC_WRITE | READ_CONTROL | READ_ATTRIBUTES,
                        0, ref sa, CREATE_NEW, NORMAL | OPEN_REPARSE | WRITE_THROUGH, IntPtr.Zero);
                    if (!claim.IsInvalid) {
                        try { Check(claim, false, true); Require(FlushFileBuffers(claim)); }
                        catch { claim.Dispose(); claim = null; throw; }
                        break;
                    }
                    int error = Marshal.GetLastWin32Error();
                    claim.Dispose();
                    claim = null;
                    Require(error == EXISTS || error == FILE_EXISTS || error == 32); // Sharing violation while claimed.
                } finally { Marshal.FreeHGlobal(allocation); }
                string published = null;
                try { published = ConfirmPublishedAlias(key); }
                catch (Unavailable) {
                    // An in-progress record stays unavailable until complete.
                }
                if (published != null) {
                    Require(boundRoot == null || published == boundRoot);
                    return published;
                }
                int remaining = AliasClaimMilliseconds - (int)elapsed.ElapsedMilliseconds;
                Require(remaining > 0);
                Thread.Sleep(Math.Min(10, remaining));
            }
            return operation();
        } finally {
            try {
                if (claim != null) {
                    try {
                        // A retained claim prevents a new root if its alias is lost.
                        Check(claim, false, true);
                    } finally { claim.Dispose(); }
                }
            } finally { CloseDirectories(held); }
        }
    }

    public string EstablishAlias(string key) {
        return WithAliasClaim(key, null, delegate {
            string id = FindAlias(key);
            if (id != null) return id;
            id = CreateRoot();
            BindAliasClaimed(key, id);
            return id;
        });
    }

    public void BindAlias(string aliasKey, string rootId) {
        Require(aliasKey != null && Regex.IsMatch(aliasKey, "^[0-9a-f]{64}$"));
        Require(RootId(rootId));
        IsProtected(rootId); // Validate existing root/state before creating an alias claim.
        WithAliasClaim(aliasKey, rootId, delegate {
            BindAliasClaimed(aliasKey, rootId);
            return rootId;
        });
    }

    void BindAliasClaimed(string aliasKey, string rootId) {
        Require(aliasKey != null && Regex.IsMatch(aliasKey, "^[0-9a-f]{64}$"));
        Require(rootId != null && Regex.IsMatch(rootId, @"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"));
        string stateFile;
        List<SafeFileHandle> rootHeld = PinDirectories(new string[] { rootId, "state" }, false, out stateFile);
        try {
            using (SafeFileHandle manifest = OpenRecord(Path.Combine(Path.GetDirectoryName(stateFile), "root.json"), false))
            using (SafeFileHandle state = OpenRecord(stateFile, false)) {
                CheckManifest(ReadChecked(manifest), rootId);
                ReadChecked(state); // Missing or unreadable state cannot acquire a new alias.
                string aliasFile;
                List<SafeFileHandle> aliasHeld = PinDirectories(new string[] { "aliases", aliasKey + ".json" }, true, out aliasFile);
                try {
                    string expected = "{\"version\":1,\"identity\":\"" + aliasKey + "\",\"root\":\"" + rootId + "\"}";
                    byte[] bytes = Encoding.UTF8.GetBytes(expected);
                    IntPtr allocation;
                    SecurityAttributes sa = Attributes(out allocation);
                    try {
                        using (SafeFileHandle h = CreateFileW(aliasFile, GENERIC_READ | GENERIC_WRITE | READ_CONTROL | READ_ATTRIBUTES,
                            0, ref sa, CREATE_NEW, NORMAL | OPEN_REPARSE | WRITE_THROUGH, IntPtr.Zero)) {
                            if (!h.IsInvalid) {
                                Check(h, false, true);
                                WriteAndFlush(h, bytes);
                                return;
                            }
                            int error = Marshal.GetLastWin32Error();
                            Require(error == EXISTS || error == FILE_EXISTS);
                        }
                    } finally { Marshal.FreeHGlobal(allocation); }
                    // Existing aliases are confirmed and flushed, never rebound.
                    using (SafeFileHandle h = OpenRecord(aliasFile, true)) {
                        Require(CheckAlias(ReadChecked(h), aliasKey) == rootId);
                        Require(FlushFileBuffers(h));
                    }
                } finally { CloseDirectories(aliasHeld); }
            }
        } finally { CloseDirectories(rootHeld); }
    }

    static bool RootId(string value) {
        return value != null && Regex.IsMatch(value, @"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$");
    }

    public static Dictionary<string, string> ParseRequest(string text) {
        Require(text != null && text.Length <= 4096);
        Dictionary<string, object> fields = new RecordReader(text).Read(true);
        object operation;
        Require(fields.TryGetValue("op", out operation) && operation is string);
        string op = (string)operation;
        string[] keys;
        switch (op) {
            case "create": keys = new string[] { "base", "op" }; break;
            case "inspect": case "protect": keys = new string[] { "base", "id", "op" }; break;
            case "bind": keys = new string[] { "base", "id", "key", "op" }; break;
            case "find": case "establish": keys = new string[] { "base", "key", "op" }; break;
            default: throw new Unavailable();
        }
        Require(fields.Count == keys.Length);
        Dictionary<string, string> request = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (string key in keys) {
            object value;
            Require(fields.TryGetValue(key, out value) && value is string);
            request[key] = (string)value;
        }
        Require(request["base"].Length <= 2048 && Path.IsPathRooted(request["base"]));
        return request;
    }

    public static string EncodeResponse(string op, object value) {
        string json;
        switch (op) {
            case "inspect":
                Require(value is bool);
                json = (bool)value ? "true" : "false";
                break;
            case "protect": case "bind":
                Require(value == null);
                json = "null";
                break;
            case "create": case "establish": case "find":
                if (value == null) {
                    Require(op == "find");
                    json = "null";
                } else {
                    string id = value as string;
                    Require(id != null && id.Length == 36 && RootId(id));
                    json = "\"" + id + "\"";
                }
                break;
            default: throw new Unavailable();
        }
        return "{\"ok\":true,\"value\":" + json + "}";
    }

    // Stored duplicate keys retain their last value, matching JSON.parse. Earlier
    // overwritten values still need strict syntax, even if not schema values.
    sealed class RecordReader {
        readonly string text;
        int offset;
        public RecordReader(byte[] bytes) : this(Encoding.UTF8.GetString(bytes)) {}
        public RecordReader(string text) { this.text = text; }
        void Space() {
            while (offset < text.Length && (text[offset] == ' ' || text[offset] == '\t' || text[offset] == '\r' || text[offset] == '\n')) offset++;
        }
        bool Take(char expected) {
            Space();
            if (offset == text.Length || text[offset] != expected) return false;
            offset++;
            return true;
        }
        string String() {
            Require(Take('"'));
            StringBuilder value = new StringBuilder();
            while (offset < text.Length) {
                char next = text[offset++];
                if (next == '"') return value.ToString();
                Require(next >= 0x20);
                if (next != '\\') { value.Append(next); continue; }
                Require(offset < text.Length);
                switch (text[offset++]) {
                    case '"': value.Append('"'); break;
                    case '\\': value.Append('\\'); break;
                    case '/': value.Append('/'); break;
                    case 'b': value.Append('\b'); break;
                    case 'f': value.Append('\f'); break;
                    case 'n': value.Append('\n'); break;
                    case 'r': value.Append('\r'); break;
                    case 't': value.Append('\t'); break;
                    case 'u':
                        Require(offset + 4 <= text.Length);
                        int code = 0;
                        for (int i = 0; i < 4; i++) {
                            char digit = text[offset++];
                            int hex = digit >= '0' && digit <= '9' ? digit - '0' :
                                digit >= 'a' && digit <= 'f' ? digit - 'a' + 10 :
                                digit >= 'A' && digit <= 'F' ? digit - 'A' + 10 : -1;
                            Require(hex >= 0);
                            code = code * 16 + hex;
                        }
                        value.Append((char)code);
                        break;
                    default: throw new Unavailable();
                }
            }
            throw new Unavailable();
        }
        bool Digit() { return offset < text.Length && text[offset] >= '0' && text[offset] <= '9'; }
        double Number() {
            Space();
            int start = offset;
            if (offset < text.Length && text[offset] == '-') offset++;
            Require(Digit());
            if (text[offset] == '0') offset++;
            else { while (Digit()) offset++; }
            if (offset < text.Length && text[offset] == '.') {
                offset++;
                Require(Digit());
                while (Digit()) offset++;
            }
            if (offset < text.Length && (text[offset] == 'e' || text[offset] == 'E')) {
                offset++;
                if (offset < text.Length && (text[offset] == '+' || text[offset] == '-')) offset++;
                Require(Digit());
                while (Digit()) offset++;
            }
            double value;
            if (!Double.TryParse(text.Substring(start, offset - start), System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out value)) {
                // Valid numbers outside the Framework double parser's range
                // cannot equal schema version 1, but may be overwritten keys.
                value = text[start] == '-' ? Double.NegativeInfinity : Double.PositiveInfinity;
            }
            return value;
        }
        object Value() {
            Space();
            Require(offset < text.Length);
            if (text[offset] == '"') return String();
            if (text[offset] == '{' || text[offset] == '[') { Composite(); return new object(); }
            foreach (string literal in new string[] { "true", "false", "null" }) {
                if (text.Length - offset >= literal.Length &&
                    System.String.CompareOrdinal(text, offset, literal, 0, literal.Length) == 0) {
                    offset += literal.Length;
                    return new object();
                }
            }
            return Number();
        }
        void NestedValue(List<int> states) {
            Space();
            Require(offset < text.Length);
            if (text[offset] == '{') { offset++; states.Add(0); }
            else if (text[offset] == '[') { offset++; states.Add(5); }
            else { Value(); }
        }
        void Composite() {
            // An explicit stack avoids recursive parsing of overwritten values.
            List<int> states = new List<int>();
            NestedValue(states);
            while (states.Count > 0) {
                int top = states.Count - 1;
                switch (states[top]) {
                    case 0: // Object: first key or close.
                        if (Take('}')) { states.RemoveAt(top); break; }
                        String(); states[top] = 2; break;
                    case 1: // Object: required key after comma.
                        String(); states[top] = 2; break;
                    case 2:
                        Require(Take(':')); states[top] = 3; break;
                    case 3:
                        states[top] = 4; NestedValue(states); break;
                    case 4:
                        if (Take('}')) { states.RemoveAt(top); break; }
                        Require(Take(',')); states[top] = 1; break;
                    case 5: // Array: first value or close.
                        if (Take(']')) { states.RemoveAt(top); break; }
                        states[top] = 7; NestedValue(states); break;
                    case 6: // Array: required value after comma.
                        states[top] = 7; NestedValue(states); break;
                    case 7:
                        if (Take(']')) { states.RemoveAt(top); break; }
                        Require(Take(',')); states[top] = 6; break;
                    default: throw new Unavailable();
                }
            }
        }
        public Dictionary<string, object> Read(bool rejectDuplicates = false) {
            Dictionary<string, object> result = new Dictionary<string, object>(StringComparer.Ordinal);
            Require(Take('{'));
            if (!Take('}')) {
                do {
                    string key = String();
                    if (rejectDuplicates) Require(!result.ContainsKey(key));
                    Require(Take(':'));
                    Space();
                    Require(offset < text.Length);
                    result[key] = Value();
                    if (Take('}')) break;
                    Require(Take(','));
                } while (true);
            }
            Space();
            Require(offset == text.Length);
            return result;
        }
    }

    static Dictionary<string, object> Record(byte[] bytes, string[] keys) {
        Dictionary<string, object> record = new RecordReader(bytes).Read();
        Require(record.Count == keys.Length);
        foreach (string key in keys) Require(record.ContainsKey(key));
        object version = record["version"];
        Require(version is double && (double)version == 1);
        return record;
    }

    static void CheckManifest(byte[] bytes, string id) {
        Dictionary<string, object> record = Record(bytes, new string[] { "id", "version" });
        Require(record["id"] is string && (string)record["id"] == id);
    }

    static string CheckAlias(byte[] bytes, string key) {
        Dictionary<string, object> record = Record(bytes, new string[] { "identity", "root", "version" });
        Require(record["identity"] is string && (string)record["identity"] == key);
        string id = record["root"] as string;
        Require(RootId(id));
        return id;
    }

    static void WriteRecordPhase(string phase) {
        try {
            Console.Error.WriteLine("LSP1:" + phase + ":0");
            Console.Error.Flush();
        } catch (Exception) { // Diagnostic I/O must not interrupt authority or cleanup.
        }
    }

    public string CreateRoot() {
        WriteRecordPhase("root-entry");
        string id = Guid.NewGuid().ToString("D");
        string unused;
        List<SafeFileHandle> baseHeld = PinDirectories(new string[] { "root.json" }, true, out unused);
        try {
            IntPtr allocation;
            SecurityAttributes sa = Attributes(out allocation);
            string rootPath = Path.Combine(basePath, id);
            try { Require(CreateDirectoryW(rootPath, ref sa)); }
            finally { Marshal.FreeHGlobal(allocation); }
            using (SafeFileHandle root = OpenDirectory(rootPath, true)) {
                CreateRootRecord(new string[] { id, "state" }, Encoding.UTF8.GetBytes("0\n"));
                CreateRootRecord(new string[] { id, "root.json" }, Encoding.UTF8.GetBytes("{\"version\":1,\"id\":\"" + id + "\"}"));
                WriteRecordPhase("root-flushed");
                Require(!IsProtected(id));
                return id;
            }
        } finally { CloseDirectories(baseHeld); }
    }

    public bool IsProtected(string id) {
        Require(RootId(id));
        string stateFile;
        List<SafeFileHandle> held = PinDirectories(new string[] { id, "state" }, false, out stateFile);
        try {
            using (SafeFileHandle manifest = OpenRecord(Path.Combine(Path.GetDirectoryName(stateFile), "root.json"), false))
            using (SafeFileHandle state = OpenRecord(stateFile, false)) {
                CheckManifest(ReadChecked(manifest), id);
                byte[] bytes = ReadChecked(state);
                return !(bytes.Length == 2 && bytes[0] == 48 && bytes[1] == 10);
            }
        } finally { CloseDirectories(held); }
    }

    public string FindAlias(string key) {
        Require(key != null && Regex.IsMatch(key, "^[0-9a-f]{64}$"));
        string id;
        try { id = CheckAlias(ReadExisting(new string[] { "aliases", key + ".json" }), key); }
        catch (Unavailable error) { if (error.NativeCode == 2 || error.NativeCode == 3) return null; throw; }
        // Only the alias lookup reports absence. A dangling alias is damaged.
        CheckManifest(ReadExisting(new string[] { id, "root.json" }), id);
        return id;
    }
}
