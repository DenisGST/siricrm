@echo off
chcp 65001 >nul
rem ── Добавить SiriCRM scan-agent в автозагрузку Windows (текущий пользователь) ──
rem  Положите этот .bat в папку агента (рядом со scan_agent.py и config.ini)
rem  и запустите ОДИН раз. Он создаёт в папке «Автозагрузка» скрытый запускатель
rem  (.vbs), который при входе в Windows стартует агент без окна (иконка в трее).
setlocal
set "AGENTDIR=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LAUNCHER=%STARTUP%\SiriScanAgent.vbs"

if not exist "%AGENTDIR%scan_agent.py" (
  echo [ОШИБКА] Рядом с этим .bat нет scan_agent.py.
  echo Положите install_autostart.bat в папку агента и запустите снова.
  pause
  exit /b 1
)

rem Генерируем .vbs в папке автозагрузки: запуск pythonw СКРЫТО из папки агента.
> "%LAUNCHER%" echo ' Автозапуск SiriCRM scan-agent (создано install_autostart.bat)
>>"%LAUNCHER%" echo Set sh = CreateObject("WScript.Shell")
>>"%LAUNCHER%" echo sh.CurrentDirectory = "%AGENTDIR%"
>>"%LAUNCHER%" echo sh.Run "pythonw scan_agent.py --config config.ini", 0, False

echo.
echo Готово. Агент добавлен в автозагрузку:
echo   %LAUNCHER%
echo Будет запускаться при входе в Windows (скрыто, значок в трее).
echo Удалить из автозагрузки — remove_autostart.bat.
echo.
choice /m "Запустить агент сейчас"
if errorlevel 2 goto :end
start "" pythonw "%AGENTDIR%scan_agent.py" --config "%AGENTDIR%config.ini"
echo Запущен (смотрите значок в трее).
:end
pause
