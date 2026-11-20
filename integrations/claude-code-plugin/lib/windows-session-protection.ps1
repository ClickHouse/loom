# Durable session records use one native operation per helper invocation.
param([switch]$CleanupCompilation, [switch]$DefinitionsOnly)
$ErrorActionPreference = 'Stop'
# Fixed ASCII diagnostics precede cmdlet and encoding initialization.
$loomDiagnosticStream = [Console]::OpenStandardError()
$loomDiagnosticStream.Write([byte[]](76, 83, 80, 49, 58, 101, 110, 116, 114, 121, 58, 48, 10), 0, 13)
$loomDiagnosticStream.Flush()
$ProgressPreference = 'SilentlyContinue'
$loomDiagnosticStream.Write([byte[]](76, 83, 80, 49, 58, 101, 110, 99, 111, 100, 105, 110, 103, 45, 110, 101, 119, 58, 48, 10), 0, 20)
$loomDiagnosticStream.Flush()
$loomInputEncoding = [Text.UTF8Encoding]::new($false)
$loomDiagnosticStream.Write([byte[]](76, 83, 80, 49, 58, 101, 110, 99, 111, 100, 105, 110, 103, 45, 99, 114, 101, 97, 116, 101, 100, 58, 48, 10), 0, 24)
$loomDiagnosticStream.Flush()
[Console]::InputEncoding = $loomInputEncoding
$loomDiagnosticStream.Write([byte[]](76, 83, 80, 49, 58, 101, 110, 99, 111, 100, 105, 110, 103, 45, 105, 110, 112, 117, 116, 58, 48, 10), 0, 22)
$loomDiagnosticStream.Flush()
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$loomDiagnosticStream.Write([byte[]](76, 83, 80, 49, 58, 101, 110, 99, 111, 100, 105, 110, 103, 45, 111, 117, 116, 112, 117, 116, 58, 48, 10), 0, 23)
$loomDiagnosticStream.Flush()
function Write-LoomProtectionPhase([string]$phase, [int]$code = 0) {
    if ($phase -cnotin @('entry', 'encoding-new', 'encoding-created', 'encoding-input', 'encoding-output', 'startup', 'bootstrap', 'worker-bootstrap', 'guardian-deadline', 'native-job', 'native-timer', 'worker-start', 'worker-assign', 'compiler-private', 'compiler-identity', 'compiler-path', 'compiler-security', 'compiler-parents', 'compiler-directory', 'compiler-receipt', 'compiler-environment', 'compiler-launch', 'compiler-stopped', 'compiler-closed', 'compiler-scanned', 'compiler-bytes', 'compiler-format', 'compiler-stop', 'compiler-query', 'compiler-close', 'compile', 'request', 'request-parse', 'request-keys', 'native-op', 'record-construct', 'record-dispatch', 'root-entry', 'root-flushed', 'output-encode', 'cleanup', 'worker-exit', 'helper-exit')) { throw 'Unavailable' }
    if ($phase -cnotin @('worker-exit', 'helper-exit', 'compiler-bytes', 'compiler-format')) { $script:loomProtectionPhase = $phase }
    try {
        [Console]::Error.WriteLine('LSP1:' + $phase + ':' + $code.ToString([Globalization.CultureInfo]::InvariantCulture))
        [Console]::Error.Flush()
    } catch { $script:loomProtectionDiagnosticFailed = $true }
}
function Invoke-LoomCompilerScope([type]$native, [bool]$CleanupCompilation, [scriptblock]$Operation, [scriptblock]$BeforeCleanup = $null) {
$held = [System.Collections.Generic.List[System.IntPtr]]::new()
$compileDirectory = $null
$compilerHandleStart = -1
$result = $null
$succeeded = $false

try {
    Write-LoomProtectionPhase $(if ($CleanupCompilation) { 'cleanup' } else { 'compiler-private' })
    if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') { throw 'Unavailable' }
    Write-LoomProtectionPhase 'compiler-identity'
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User

    function Pin-Directory([string]$directory, [bool]$private, [bool]$allowMissing = $false) {
        # READ_CONTROL | FILE_READ_ATTRIBUTES; no FILE_SHARE_DELETE.
        $handle = $native::CreateFileW($directory, 0x20080, 3, [IntPtr]::Zero, 3, 0x02200000, [IntPtr]::Zero)
        if ($handle.ToInt64() -eq -1) {
            $openError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            if ($allowMissing -and ($openError -eq 2 -or $openError -eq 3)) { return $false }
            throw 'Unavailable'
        }
        $held.Add($handle)
        $information = [Runtime.InteropServices.Marshal]::AllocHGlobal(52)
        try {
            if (-not $native::GetFileInformationByHandle($handle, $information)) { throw 'Unavailable' }
            $attributes = [Runtime.InteropServices.Marshal]::ReadInt32($information)
            if (($attributes -band 0x400) -ne 0 -or ($attributes -band 0x10) -eq 0) { throw 'Unavailable' }
        } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($information) }
        if (-not $private) { return $true }
        $owner = $group = $dacl = $sacl = $sd = [IntPtr]::Zero
        if ($native::GetSecurityInfo($handle, 1, 5, [ref]$owner, [ref]$group, [ref]$dacl, [ref]$sacl, [ref]$sd) -ne 0) { throw 'Unavailable' }
        try {
            if ($owner -eq [IntPtr]::Zero -or $dacl -eq [IntPtr]::Zero -or $sd -eq [IntPtr]::Zero) { throw 'Unavailable' }
            $length = $native::GetSecurityDescriptorLength($sd)
            if ($length -lt 1 -or $length -gt 65536) { throw 'Unavailable' }
            $bytes = [byte[]]::new($length)
            [Runtime.InteropServices.Marshal]::Copy($sd, $bytes, 0, $length)
            $actual = [System.Security.AccessControl.RawSecurityDescriptor]::new($bytes, 0)
            if (-not $sid.Equals($actual.Owner) -or ($actual.ControlFlags -band 0x1004) -ne 0x1004 -or $null -eq $actual.DiscretionaryAcl -or $actual.DiscretionaryAcl.Count -ne 1) { throw 'Unavailable' }
            $ace = $actual.DiscretionaryAcl[0]
            if ($ace -isnot [System.Security.AccessControl.CommonAce] -or $ace.IsCallback -or $ace.AceQualifier -ne [System.Security.AccessControl.AceQualifier]::AccessAllowed -or $ace.AceFlags -ne 3 -or $ace.AccessMask -ne 0x1f01ff -or -not $sid.Equals($ace.SecurityIdentifier)) { throw 'Unavailable' }
        } finally { if ($sd -ne [IntPtr]::Zero) { [void]$native::LocalFree($sd) } }
        return $true
    }

    function New-PrivateDirectory([string]$directory, [bool]$allowExisting) {
        $binary = $security.GetSecurityDescriptorBinaryForm()
        $descriptor = [Runtime.InteropServices.Marshal]::AllocHGlobal($binary.Length)
        $attributeSize = if ([IntPtr]::Size -eq 8) { 24 } else { 12 }
        $pointerOffset = if ([IntPtr]::Size -eq 8) { 8 } else { 4 }
        $attributes = [Runtime.InteropServices.Marshal]::AllocHGlobal($attributeSize)
        try {
            [Runtime.InteropServices.Marshal]::Copy($binary, 0, $descriptor, $binary.Length)
            [Runtime.InteropServices.Marshal]::WriteInt32($attributes, 0, $attributeSize)
            [Runtime.InteropServices.Marshal]::WriteIntPtr($attributes, $pointerOffset, $descriptor)
            [Runtime.InteropServices.Marshal]::WriteInt32($attributes, $pointerOffset + [IntPtr]::Size, 0)
            if (-not $native::CreateDirectoryW($directory, $attributes)) {
                $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
                if (-not $allowExisting -or $errorCode -ne 183) { throw 'Unavailable' }
            }
        } finally {
            [Runtime.InteropServices.Marshal]::FreeHGlobal($attributes)
            [Runtime.InteropServices.Marshal]::FreeHGlobal($descriptor)
        }
    }

    Write-LoomProtectionPhase 'compiler-path'
    $compilerBase = [IO.Path]::GetFullPath($env:LOOM_PRIVATE_COMPILER_BASE)
    $compilationId = $env:LOOM_PRIVATE_COMPILATION_ID
    if ($compilationId -notmatch '^[0-9a-f]{32}$') { throw 'Unavailable' }
    $root = [IO.Path]::GetPathRoot($compilerBase)
    if ($root -notmatch '^[A-Za-z]:\\$') { throw 'Unavailable' }
    Write-LoomProtectionPhase 'compiler-security'
    $security = [System.Security.AccessControl.DirectorySecurity]::new()
    $security.SetOwner($sid)
    $security.SetAccessRuleProtection($true, $false)
    # Compiler-created files must inherit owner-only authority themselves;
    # directory traversal checks may be bypassed by SeChangeNotifyPrivilege.
    $inherit = [System.Security.AccessControl.InheritanceFlags]'ObjectInherit, ContainerInherit'
    $security.AddAccessRule(([System.Security.AccessControl.FileSystemAccessRule]::new($sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl, $inherit,
        [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)))
    Write-LoomProtectionPhase 'compiler-parents'
    [void](Pin-Directory $root $false)
    $current = $root
    $missing = $false
    $parts = $compilerBase.Substring($root.Length).Split([char]'\')
    for ($i = 0; $i -lt $parts.Length; $i++) {
        $current = [IO.Path]::Combine($current, $parts[$i])
        if (-not (Pin-Directory $current ($i -eq $parts.Length - 1) $true)) {
            if ($CleanupCompilation) {
                $missing = $true
                break
            }
            # Existing ancestors need no directory creation or descriptor copy.
            New-PrivateDirectory $current $true
            [void](Pin-Directory $current ($i -eq $parts.Length - 1))
        }
    }
    $candidate = [IO.Path]::Combine($compilerBase, $compilationId)
    if ($CleanupCompilation) {
        if (-not $missing) {
            $compilerHandleStart = $held.Count
            if (Pin-Directory $candidate $true $true) {
                # A receipt is written only after exclusive directory creation.
                # Without it cleanup refuses rather than deleting unknown data.
                $marker = [IO.File]::ReadAllText([IO.Path]::Combine($candidate, 'created'))
                if ($marker -ne ('loom-compiler-v1:' + $compilationId)) { throw 'Unavailable' }
                $compileDirectory = $candidate
            }
        }
        $succeeded = $true
        $result = $null
    } else {
    Write-LoomProtectionPhase 'compiler-directory'
    New-PrivateDirectory $candidate $false
    $compileDirectory = $candidate
    $compilerHandleStart = $held.Count
    [void](Pin-Directory $compileDirectory $true)
    Write-LoomProtectionPhase 'compiler-receipt'
    [IO.File]::WriteAllText([IO.Path]::Combine($compileDirectory, 'created'), ('loom-compiler-v1:' + $compilationId))

    $result = & $Operation $compileDirectory
    $succeeded = $true
    }
} catch {
    Write-LoomProtectionPhase $script:loomProtectionPhase ([int]$_.Exception.HResult)
    $succeeded = $false
} finally {
    if ($null -ne $BeforeCleanup) { & $BeforeCleanup }
    Write-LoomProtectionPhase 'cleanup'
    if ($compilerHandleStart -ge 0) {
        for ($i = $held.Count - 1; $i -ge $compilerHandleStart; $i--) {
            if (-not $native::CloseHandle($held[$i])) { $succeeded = $false }
            $held.RemoveAt($i)
        }
    }
    if ($null -ne $compileDirectory) {
        try {
            foreach ($file in [IO.Directory]::GetFileSystemEntries($compileDirectory)) {
                $attributes = [IO.File]::GetAttributes($file)
                if (($attributes -band 0x410) -ne 0) { throw 'Unavailable' }
                [IO.File]::Delete($file)
            }
            [IO.Directory]::Delete($compileDirectory, $false)
        } catch {
            Write-LoomProtectionPhase 'cleanup' ([int]$_.Exception.HResult)
            $succeeded = $false
        }
    }
    for ($i = $held.Count - 1; $i -ge 0; $i--) {
        if (-not $native::CloseHandle($held[$i])) { $succeeded = $false }
    }
}
if (-not $succeeded) { throw 'Unavailable' }
return $result
}

function Invoke-LoomCompileRecords([type]$native, [IntPtr]$job, [long]$deadline, [string]$directory) {
    Write-LoomProtectionPhase 'compile'
    $source = [IO.File]::ReadAllText([IO.Path]::Combine($PSScriptRoot, 'windows-private-records.cs'))
    [IO.File]::WriteAllText([IO.Path]::Combine($directory, 'records.cs'), $source, [Text.UTF8Encoding]::new($true))
    # Fixed names under the exclusive, pinned invocation directory. No request
    # value reaches the compiler or its response file.
    if ($directory.Contains('"') -or $directory.Contains("`r") -or $directory.Contains("`n")) { throw 'Unavailable' }
    $runtime = [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()
    if ($runtime.Contains('"') -or $runtime.Contains("`r") -or $runtime.Contains("`n")) { throw 'Unavailable' }
    $options = @('/target:library', '/optimize+', '/warnaserror+', '/debug-', '/nostdlib+',
        ('/reference:"' + [IO.Path]::Combine($runtime, 'mscorlib.dll') + '"'),
        ('/reference:"' + [IO.Path]::Combine($runtime, 'System.dll') + '"'),
        ('/reference:"' + [IO.Path]::Combine($runtime, 'System.Core.dll') + '"'),
        ('/out:"' + [IO.Path]::Combine($directory, 'records.dll') + '"'),
        ('"' + [IO.Path]::Combine($directory, 'records.cs') + '"'))
    [IO.File]::WriteAllLines([IO.Path]::Combine($directory, 'compile.rsp'), $options, [Text.UTF8Encoding]::new($true))
    Invoke-LoomCompilerProcess $native $job $deadline $directory ('/noconfig @"' + [IO.Path]::Combine($directory, 'compile.rsp') + '"')
    Stop-LoomCompilerJob $native $job
    if ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -ge $deadline) { throw 'Unavailable' }
    # Byte loading avoids locking the owned DLL during exact cleanup. Resolve
    # the authority type from THIS assembly, never an ambient type-name cache.
    return [Reflection.Assembly]::Load([IO.File]::ReadAllBytes([IO.Path]::Combine($directory, 'records.dll')))
}

function Invoke-LoomRecordOperation([Reflection.Assembly]$assembly) {
    Write-LoomProtectionPhase 'request'
    $buffer = [char[]]::new(4097)
    $count = 0
    while ($count -lt $buffer.Length) {
        $read = [Console]::In.Read($buffer, $count, $buffer.Length - $count)
        if ($read -eq 0) { break }
        $count += $read
    }
    if ($count -gt 4096) { throw 'Unavailable' }
    $codecType = $assembly.GetType('WindowsPrivateRecords', $true, $false)
    Write-LoomProtectionPhase 'request-parse'
    $request = $codecType::ParseRequest(([string]::new($buffer, 0, $count)))
    Write-LoomProtectionPhase 'request-keys'
    Write-LoomProtectionPhase 'native-op'
    $recordType = $assembly.GetType('WindowsPrivateRecords', $true, $false)
    Write-LoomProtectionPhase 'record-construct'
    $store = [Activator]::CreateInstance($recordType, [object[]]@($request['base']))
    Write-LoomProtectionPhase 'record-dispatch'
    $result = switch ($request['op']) {
        'create' { $store.CreateRoot() }
        'inspect' { $store.IsProtected($request['id']) }
        'protect' { $store.AppendProtection($request['id']); $null }
        'bind' { $store.BindAlias($request['key'], $request['id']); $null }
        'find' { $store.FindAlias($request['key']) }
        'establish' { $store.EstablishAlias($request['key']) }
    }
    Write-LoomProtectionPhase 'output-encode'
    return $recordType::EncodeResponse($request['op'], $result)
}

if ($DefinitionsOnly) { return }
$loomTerminalReady = $false
$loomTerminalExitAttempted = $false
try {
    Write-LoomProtectionPhase 'startup'
    if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') { throw 'Unavailable' }
    . ([IO.Path]::Combine($PSScriptRoot, 'windows-native-bootstrap.ps1'))
    . ([IO.Path]::Combine($PSScriptRoot, 'windows-compiler-job.ps1'))
    Write-LoomProtectionPhase 'bootstrap'
    $native = New-LoomBootstrapApi
    if ($CleanupCompilation) {
        [void](Start-LoomGuardianDeadline $native)
        [void](Invoke-LoomCompilerScope $native $true $null)
        [Console]::Out.Write('{"ok":true,"value":null}')
    } else {
        $compiled = Invoke-LoomGuardedCompiler $native {
            param($compilerNative, $compilerJob, $compilerDeadline, $directory)
            Invoke-LoomCompileRecords $compilerNative $compilerJob $compilerDeadline $directory
        }
        $loomTerminalReady = $true
        # Compilation descendants and owned files are already gone. Preserve
        # the original earlier native-operation cutoff on this same self-job.
        Start-LoomNativeTimer $native 'StopJob' $compiled.Guardian.Job ($compiled.Guardian.Deadline - 150)
        $result = Invoke-LoomRecordOperation $compiled.Value
        [Console]::Out.Write($result)
    }
    Write-LoomProtectionPhase 'helper-exit' 0
    if ($script:loomProtectionDiagnosticFailed) { throw 'Unavailable' }
    if ($loomTerminalReady) {
        $loomTerminalExitAttempted = $true
        [Console]::Out.Flush()
        [Console]::Error.Flush()
        [void]$native::TerminateProcess($native::GetCurrentProcess(), [uint32]0)
        # Exact-self termination must not return, including a false API result.
        throw 'Unavailable'
    }
    exit 0
} catch {
    Write-LoomProtectionPhase $script:loomProtectionPhase ([int]$_.Exception.HResult)
    Write-LoomProtectionPhase 'helper-exit' 1
    [Console]::Out.Write('{"ok":false}')
    if ($loomTerminalReady -and -not $loomTerminalExitAttempted) {
        $loomTerminalExitAttempted = $true
        try {
            [Console]::Out.Flush()
            [Console]::Error.Flush()
            [void]$native::TerminateProcess($native::GetCurrentProcess(), [uint32]1)
        } catch { } # Terminal failure remains failure under the existing watchdogs.
    }
    exit 1
}
