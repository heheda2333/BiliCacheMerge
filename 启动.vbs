Option Explicit
Dim fso, sh, base, script, q, ok
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
script = base & "\BiliCacheMerge.py"
sh.CurrentDirectory = base
q = Chr(34)

If Not fso.FileExists(script) Then
    MsgBox "找不到 BiliCacheMerge.py，请把本文件放回程序文件夹。", 16, "B站缓存合并器"
    WScript.Quit 1
End If

ok = False
On Error Resume Next
sh.Run q & "pythonw.exe" & q & " " & q & script & q, 0, False
If Err.Number = 0 Then ok = True

If Not ok Then
    Err.Clear
    sh.Run q & "python.exe" & q & " " & q & script & q, 0, False
    If Err.Number = 0 Then ok = True
End If
On Error GoTo 0

If Not ok Then
    MsgBox "启动失败：没有找到 Python。请安装 Python 3.8 或更高版本，或直接运行 BiliCacheMerge.py。", 16, "B站缓存合并器"
    WScript.Quit 1
End If
