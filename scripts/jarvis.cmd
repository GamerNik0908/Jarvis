@echo off
rem Startet JARVIS aus der virtuellen Umgebung des Projekts.
rem Beispiele:  jarvis.cmd                 (JARVIS starten + Dashboard oeffnen)
rem             jarvis.cmd chat            (Chat im Terminal)
rem             jarvis.cmd setup           (gefuehrte Einrichtung)
rem             jarvis.cmd google add      (Gmail-Konto verbinden)
rem             jarvis.cmd improve "Ich haette gerne eine Druckfunktion"
rem             jarvis.cmd stop
setlocal
set "JARVIS_ROOT=%~dp0.."
set "JARVIS_PY=%JARVIS_ROOT%\.venv\Scripts\python.exe"
if not exist "%JARVIS_PY%" (
    echo Virtuelle Umgebung fehlt. Bitte zuerst ausfuehren:
    echo   powershell -ExecutionPolicy Bypass -File "%JARVIS_ROOT%\scripts\install.ps1"
    exit /b 1
)
"%JARVIS_PY%" -m jarvis %*
exit /b %ERRORLEVEL%
