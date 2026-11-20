# Fixed P/Invoke declarations needed before source compilation can begin.
function New-LoomBootstrapApi {
    $assemblyName = [System.Reflection.AssemblyName]::new('LoomPrivateCompilerBootstrap')
    $assembly = [AppDomain]::CurrentDomain.DefineDynamicAssembly($assemblyName, [System.Reflection.Emit.AssemblyBuilderAccess]::Run)
    $module = $assembly.DefineDynamicModule($assemblyName.Name)
    $callback = $module.DefineType('LoomNativeTimerCallback', [System.Reflection.TypeAttributes]'Public, Sealed', [MulticastDelegate])
    $callingConvention = [Runtime.InteropServices.UnmanagedFunctionPointerAttribute].GetConstructor([type[]]@([Runtime.InteropServices.CallingConvention]))
    $callback.SetCustomAttribute(([Reflection.Emit.CustomAttributeBuilder]::new($callingConvention,
        [object[]]@([Runtime.InteropServices.CallingConvention]::Winapi))))
    $constructor = $callback.DefineConstructor([System.Reflection.MethodAttributes]'Public, HideBySig, RTSpecialName',
        [System.Reflection.CallingConventions]::Standard, [type[]]@([object], [IntPtr]))
    $constructor.SetImplementationFlags([System.Reflection.MethodImplAttributes]'Runtime, Managed')
    $invoke = $callback.DefineMethod('Invoke', [System.Reflection.MethodAttributes]'Public, HideBySig, NewSlot, Virtual',
        [void], [type[]]@([IntPtr], [byte]))
    $invoke.SetImplementationFlags([System.Reflection.MethodImplAttributes]'Runtime, Managed')
    [void]$callback.CreateType()
    $builder = $module.DefineType('LoomPrivateCompilerBootstrap', [System.Reflection.TypeAttributes]'Public, Abstract, Sealed')
    $nativeMethods = @{}
    function Add-Native([string]$name, [string]$dll, [type]$returns, [type[]]$parameters) {
        $method = $builder.DefinePInvokeMethod($name, $dll,
            [System.Reflection.MethodAttributes]'Public, Static, PinvokeImpl',
            [System.Reflection.CallingConventions]::Standard, $returns, $parameters,
            [System.Runtime.InteropServices.CallingConvention]::Winapi, [System.Runtime.InteropServices.CharSet]::Unicode)
        $method.SetImplementationFlags($method.GetMethodImplementationFlags() -bor [System.Reflection.MethodImplAttributes]::PreserveSig)
        $constructor = [System.Runtime.InteropServices.DllImportAttribute].GetConstructor([type[]]@([string]))
        $fields = [System.Reflection.FieldInfo[]]@(
            [System.Runtime.InteropServices.DllImportAttribute].GetField('SetLastError'),
            [System.Runtime.InteropServices.DllImportAttribute].GetField('CharSet'),
            [System.Runtime.InteropServices.DllImportAttribute].GetField('ExactSpelling'),
            [System.Runtime.InteropServices.DllImportAttribute].GetField('CallingConvention'))
        $attribute = [System.Reflection.Emit.CustomAttributeBuilder]::new($constructor, [object[]]@($dll),
            $fields, [object[]]@($true, [System.Runtime.InteropServices.CharSet]::Unicode, $true,
                [System.Runtime.InteropServices.CallingConvention]::Winapi))
        $method.SetCustomAttribute($attribute)
        $nativeMethods[$name] = $method
    }
    $refPointer = [IntPtr].MakeByRefType()
    Add-Native 'CreateFileW' 'kernel32.dll' ([IntPtr]) ([type[]]@([string], [uint32], [uint32], [IntPtr], [uint32], [uint32], [IntPtr]))
    Add-Native 'CreateDirectoryW' 'kernel32.dll' ([bool]) ([type[]]@([string], [IntPtr]))
    Add-Native 'GetFileInformationByHandle' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr], [IntPtr]))
    Add-Native 'CloseHandle' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr]))
    Add-Native 'GetSecurityInfo' 'advapi32.dll' ([uint32]) ([type[]]@([IntPtr], [int], [uint32], $refPointer, $refPointer, $refPointer, $refPointer, $refPointer))
    Add-Native 'GetSecurityDescriptorLength' 'advapi32.dll' ([uint32]) ([type[]]@([IntPtr]))
    Add-Native 'LocalFree' 'kernel32.dll' ([IntPtr]) ([type[]]@([IntPtr]))
    Add-Native 'GetCurrentProcess' 'kernel32.dll' ([IntPtr]) ([type[]]@())
    Add-Native 'CreateJobObjectW' 'kernel32.dll' ([IntPtr]) ([type[]]@([IntPtr], [IntPtr]))
    Add-Native 'SetInformationJobObject' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr], [int], [IntPtr], [uint32]))
    Add-Native 'AssignProcessToJobObject' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr], [IntPtr]))
    Add-Native 'TerminateJobObject' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr], [uint32]))
    Add-Native 'WaitForSingleObject' 'kernel32.dll' ([uint32]) ([type[]]@([IntPtr], [uint32]))
    Add-Native 'QueryInformationJobObject' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr], [int], [IntPtr], [uint32], [IntPtr]))
    Add-Native 'InitializeProcThreadAttributeList' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr], [int], [uint32], $refPointer))
    Add-Native 'UpdateProcThreadAttribute' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr], [uint32], [IntPtr], [IntPtr], [IntPtr], [IntPtr], [IntPtr]))
    Add-Native 'DeleteProcThreadAttributeList' 'kernel32.dll' ([void]) ([type[]]@([IntPtr]))
    Add-Native 'CreateProcessW' 'kernel32.dll' ([bool]) ([type[]]@([string], [Text.StringBuilder], [IntPtr], [IntPtr], [bool], [uint32], [IntPtr], [string], [IntPtr], [IntPtr]))
    Add-Native 'ResumeThread' 'kernel32.dll' ([uint32]) ([type[]]@([IntPtr]))
    Add-Native 'TerminateProcess' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr], [uint32]))
    Add-Native 'GetExitCodeProcess' 'kernel32.dll' ([bool]) ([type[]]@([IntPtr], [uint32].MakeByRefType()))
    Add-Native 'CreateTimerQueueTimer' 'kernel32.dll' ([bool]) ([type[]]@($refPointer, [IntPtr], [IntPtr], [IntPtr], [uint32], [uint32], [uint32]))
        $method = $builder.DefineMethod('StopJob', [System.Reflection.MethodAttributes]'Public, Static',
            [void], [type[]]@([IntPtr], [byte]))
        $il = $method.GetILGenerator()
        $il.Emit([System.Reflection.Emit.OpCodes]::Ldarg_0)
        # Distinguish native deadline termination from ordinary failure code 1.
        $il.Emit([System.Reflection.Emit.OpCodes]::Ldc_I4, [int]1460)
        $il.Emit([System.Reflection.Emit.OpCodes]::Call, $nativeMethods['TerminateJobObject'])
        $il.Emit([System.Reflection.Emit.OpCodes]::Pop)
        $il.Emit([System.Reflection.Emit.OpCodes]::Ret)
    return $builder.CreateType()
}
