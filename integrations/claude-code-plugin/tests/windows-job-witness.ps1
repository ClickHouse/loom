param([switch]$CompileControl)
$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') { throw 'Unavailable' }
$lib = [IO.Path]::GetFullPath([IO.Path]::Combine($PSScriptRoot, '..', 'lib'))
. ([IO.Path]::Combine($lib, 'windows-native-bootstrap.ps1'))
. ([IO.Path]::Combine($lib, 'windows-compiler-job.ps1'))
. ([IO.Path]::Combine($lib, 'windows-session-protection.ps1')) -DefinitionsOnly
$native = New-LoomBootstrapApi
function Write-LoomWitnessPhase([string]$phase) {
    if ($phase -cnotin @('assigned', 'live', 'identity', 'announced')) { throw 'Unavailable' }
    try {
        [Console]::Error.WriteLine('LSP1:witness-' + $phase + ':0')
        [Console]::Error.Flush()
    } catch { } # Missing diagnostic output does not establish a failed boundary.
}
[void](Invoke-LoomGuardedCompiler $native {
    param($compilerNative, $compilerJob, $compilerDeadline, $directory)
    if ($directory.Contains('"') -or $directory -match '[\x00-\x1f]') { throw 'Unavailable' }
    $runtime = [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()
    if ($runtime.Contains('"') -or $runtime.Contains("`r") -or $runtime.Contains("`n")) { throw 'Unavailable' }
    $sourceFile = [IO.Path]::Combine($directory, 'witness.cs')
    $assemblyFile = [IO.Path]::Combine($directory, 'witness.dll')
    $responseFile = [IO.Path]::Combine($directory, 'witness.rsp')
    [IO.File]::WriteAllText($sourceFile, 'public sealed class LoomCompilerWitness { public const int Value = 1; }', [Text.UTF8Encoding]::new($true))
    $options = @('/target:library', '/nostdlib+',
        ('/reference:"' + [IO.Path]::Combine($runtime, 'mscorlib.dll') + '"'),
        ('/out:"' + $assemblyFile + '"'), ('"' + $sourceFile + '"'))
    [IO.File]::WriteAllLines($responseFile, $options, [Text.UTF8Encoding]::new($true))
    Invoke-LoomCompilerProcess $compilerNative $compilerJob $compilerDeadline $directory ('/noconfig @"' + $responseFile + '"') {
        param($compilerHandle, $compilerPid, [bool]$assigned = $false)
        if (-not $assigned -or $CompileControl) { return }
        Write-LoomWitnessPhase 'assigned'
        if ($compilerNative::WaitForSingleObject($compilerHandle, 0) -ne 258) { throw 'Unavailable' }
        Write-LoomWitnessPhase 'live'
        $compiler = [Diagnostics.Process]::GetProcessById($compilerPid)
        try {
            $guardian = [Diagnostics.Process]::GetCurrentProcess()
            $receipt = @{ pid = $compilerPid; started = $compiler.StartTime.ToUniversalTime().Ticks.ToString();
                directory = $directory; guardian = @{ pid = $guardian.Id; started = $guardian.StartTime.ToUniversalTime().Ticks.ToString() } }
            Write-LoomWitnessPhase 'identity'
            # Only the validated path needs escaping in this fixed receipt.
            $json = '{"pid":' + $receipt.pid.ToString([Globalization.CultureInfo]::InvariantCulture) +
                ',"started":"' + $receipt.started + '","directory":"' + $directory.Replace('\', '\\') +
                '","guardian":{"pid":' + $receipt.guardian.pid.ToString([Globalization.CultureInfo]::InvariantCulture) +
                ',"started":"' + $receipt.guardian.started + '"}}'
            [Console]::Out.WriteLine($json)
            [Console]::Out.Flush()
            Write-LoomWitnessPhase 'announced'
        } finally { $compiler.Dispose() }
        # Existing native job deadlines bound this exact-process wait.
        if ($compilerNative::WaitForSingleObject($compilerHandle, [uint32]::MaxValue) -ne 0) { throw 'Unavailable' }
    }
    $compiled = [Reflection.Assembly]::Load([IO.File]::ReadAllBytes($assemblyFile))
    $fixtureType = $compiled.GetType('LoomCompilerWitness', $true, $false)
    $field = $fixtureType.GetField('Value')
    if ($null -eq $field -or -not $field.IsLiteral -or $field.FieldType -ne [int] -or $field.GetRawConstantValue() -ne 1) { throw 'Unavailable' }
})
if (-not $CompileControl) { throw 'Unavailable' }
[Console]::Out.Write('{"compiled":true}')
[Console]::Out.Flush()
[Console]::Error.Flush()
[void]$native::TerminateProcess($native::GetCurrentProcess(), [uint32]0)
throw 'Unavailable'
