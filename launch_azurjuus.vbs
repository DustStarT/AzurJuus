Option Explicit
Dim shell, files, root, python
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
python = root & "\.venv\Scripts\pythonw.exe"
If Not files.FileExists(python) Then
    MsgBox "Please run tools\setup_runtime.py first.", 16, "AzurJuus"
    WScript.Quit 1
End If
shell.CurrentDirectory = root
shell.Run """" & python & """ -X utf8 """ & root & "\tools\desktop_entry.py""", 0, False
