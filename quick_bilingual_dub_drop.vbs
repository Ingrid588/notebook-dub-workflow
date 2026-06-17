' Drag a folder onto this file (safe for long paths; avoids bat drag-drop bugs).
Option Explicit

Dim fso, sh, bat, folder, cmd

If WScript.Arguments.Count < 1 Then
    MsgBox "请把视频文件夹拖到这个文件上。" & vbCrLf & vbCrLf & _
           "Please drag a folder onto this file.", vbInformation, "Quick Bilingual Dub"
    WScript.Quit 1
End If

folder = WScript.Arguments(0)
Set fso = CreateObject("Scripting.FileSystemObject")

If Not fso.FolderExists(folder) Then
    MsgBox "不是有效文件夹：" & folder, vbExclamation, "Quick Bilingual Dub"
    WScript.Quit 1
End If

bat = fso.GetParentFolderName(WScript.ScriptFullName) & "\quick_bilingual_dub.bat"
If Not fso.FileExists(bat) Then
    MsgBox "找不到：" & bat, vbCritical, "Quick Bilingual Dub"
    WScript.Quit 1
End If

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "cmd /c """ & bat & """ """ & folder & """"
sh.Run cmd, 1, True
