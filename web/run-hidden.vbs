' conversation-recall hidden launcher.
' Runs server.py with pythonw: no console window, no taskbar item.
' The server opens the web UI itself (--open-browser).
' Stop it from the web UI shutdown button (top-right power icon),
' or manually: taskkill /IM pythonw.exe /F
Set sh = CreateObject("WScript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.CurrentDirectory = here
sh.Run "pythonw """ & here & "server.py"" --open-browser", 0, False
