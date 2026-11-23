Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "G:\jarvis"
WshShell.Run """C:\Users\Ayan\AppData\Local\Programs\Python\Python315\pythonw.exe"" ""G:\jarvis\main.py""", 0, False
