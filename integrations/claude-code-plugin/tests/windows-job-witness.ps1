$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') { throw 'Unavailable' }
$lib = [IO.Path]::GetFullPath([IO.Path]::Combine($PSScriptRoot, '..', 'lib'))
. ([IO.Path]::Combine($lib, 'windows-native-bootstrap.ps1'))
. ([IO.Path]::Combine($lib, 'windows-compiler-job.ps1'))
. ([IO.Path]::Combine($lib, 'windows-session-protection.ps1')) -DefinitionsOnly
$native = New-LoomBootstrapApi
Invoke-LoomGuardedCompiler $native {
    param($compilerNative, $compilerJob, $compilerDeadline, $directory)
    $pipeName = 'loom-compiler-' + $env:LOOM_PRIVATE_COMPILATION_ID
    $pipe = [IO.Pipes.NamedPipeServerStream]::new($pipeName, [IO.Pipes.PipeDirection]::Out, 1,
        [IO.Pipes.PipeTransmissionMode]::Byte, [IO.Pipes.PipeOptions]::Asynchronous)
    try {
        $connected = $pipe.BeginWaitForConnection($null, $null)
        $state = @{ Announced = $false }
        Invoke-LoomCompilerProcess $compilerNative $compilerJob $compilerDeadline $directory (
            '/noconfig /target:library /out:witness.dll @"\\.\pipe\' + $pipeName + '"') {
            param($compilerHandle, $compilerPid, $closedLogPath = $null)
            if ($null -ne $closedLogPath) {
                $operandSeen = 0
                try {
                    $stream = [IO.File]::Open($closedLogPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
                    try {
                        $bytes = [byte[]]::new(8192)
                        $count = $stream.Read($bytes, 0, $bytes.Length)
                        # Map byte values directly; no compiler encoding is inferred.
                        $text = [string]::new([char[]]$bytes, 0, $count)
                        foreach ($line in $text.Split([char[]]@(10, 13))) {
                            if ($line.IndexOf('CS2021:', [StringComparison]::Ordinal) -ge 0 -and
                                $line.IndexOf(('\\.\pipe\' + $pipeName), [StringComparison]::Ordinal) -ge 0) {
                                $operandSeen = 1
                                break
                            }
                        }
                    } finally { $stream.Dispose() }
                } catch { $operandSeen = 0 } # Observation failure means unproved.
                try {
                    if ($operandSeen -eq 1) { [Console]::Error.WriteLine('LSP1:compiler-operand:1') }
                    else { [Console]::Error.WriteLine('LSP1:compiler-operand:0') }
                    [Console]::Error.Flush()
                } catch { } # Diagnostics must not replace the original compiler failure.
                return
            }
            if (-not $state.Announced -and $connected.IsCompleted) {
                $pipe.EndWaitForConnection($connected)
                if ($compilerNative::WaitForSingleObject($compilerHandle, 0) -ne 258) { throw 'Unavailable' }
                $compiler = [Diagnostics.Process]::GetProcessById($compilerPid)
                try {
                    $guardian = [Diagnostics.Process]::GetCurrentProcess()
                    $receipt = @{ pid = $compilerPid; started = $compiler.StartTime.ToUniversalTime().Ticks.ToString();
                        directory = $directory; guardian = @{ pid = $guardian.Id; started = $guardian.StartTime.ToUniversalTime().Ticks.ToString() } }
                    [Console]::Out.WriteLine((ConvertTo-Json -Compress -Depth 4 $receipt))
                    [Console]::Out.Flush()
                    $state.Announced = $true
                } finally { $compiler.Dispose() }
            }
        }
    } finally { $pipe.Dispose() }
}
