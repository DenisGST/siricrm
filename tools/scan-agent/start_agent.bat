@echo off
rem ── SiriCRM scan-agent — запуск без окна консоли ──────────────────────────
rem  pythonw.exe = Python без консоли (агент живёт в системном трее).
rem  start "" ... = запустить и сразу выйти, чтобы окно .bat не висело.
rem  %~dp0 = папка этого .bat (кладите рядом со scan_agent.py и config.ini).
cd /d "%~dp0"
start "" pythonw scan_agent.py --config config.ini
