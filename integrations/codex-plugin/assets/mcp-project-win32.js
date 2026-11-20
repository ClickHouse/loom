if (WScript.Arguments.length !== 1) WScript.Quit(1);
var pid = String(WScript.Arguments.Item(0));
if (!/^[0-9]+$/.test(pid) || Number(pid) < 2 || Number(pid) > 4294967295) WScript.Quit(1);

var service = GetObject("winmgmts:\\\\.\\root\\cimv2");
var query = "SELECT Name,ParentProcessId,CreationDate FROM Win32_Process WHERE ProcessId=" + pid;
var items = new Enumerator(service.ExecQuery(query, "WQL", 48));
if (items.atEnd()) WScript.Quit(1);
var process = items.item();
var name = String(process.Name);
var parent = String(process.ParentProcessId);
var clock = new ActiveXObject("WbemScripting.SWbemDateTime");
clock.Value = process.CreationDate;
var born = String(clock.GetFileTime(false));
items.moveNext();
if (!items.atEnd() || !name || !/^[0-9]+$/.test(parent) || !/^[0-9]+$/.test(born)) WScript.Quit(1);
WScript.StdOut.WriteLine(encodeURIComponent(name) + "\t" + parent + "\t" + born);
