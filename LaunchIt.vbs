' Start LaunchIt without a console window (pythonw must be on PATH)
Set fso = CreateObject("Scripting.FileSystemObject")
dir_ = fso.GetParentFolderName(WScript.ScriptFullName)
Set ws = CreateObject("WScript.Shell")
ws.CurrentDirectory = dir_
ws.Run "pythonw.exe """ & dir_ & "\launchit.py""", 0, False
