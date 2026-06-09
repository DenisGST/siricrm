@echo off
chcp 65001 >nul
rem ── Убрать SiriCRM scan-agent из автозагрузки Windows (текущий пользователь) ──
rem  Удаляет запускатель из папки «Автозагрузка». Сам агент НЕ останавливает —
rem  если он сейчас запущен, закройте его в трее (правый клик → «Выход»).
set "LAUNCHER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\SiriScanAgent.vbs"
if exist "%LAUNCHER%" (
  del "%LAUNCHER%"
  echo Убрано из автозагрузки: %LAUNCHER%
) else (
  echo В автозагрузке записи не было: %LAUNCHER%
)
pause
