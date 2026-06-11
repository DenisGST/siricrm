' ── SiriCRM scan-agent — запуск ПОЛНОСТЬЮ без окна (для автозагрузки) ────────
' Запускает pythonw в скрытом режиме (windowStyle=0) из папки этого скрипта.
' Положите рядом со scan_agent.py; ярлык на этот .vbs — в автозагрузку
' (Win+R -> shell:startup), чтобы агент стартовал при входе в Windows без вспышки окна.
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "pythonw scan_agent.py --config config.ini", 0, False
