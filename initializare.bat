@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" goto VENV
py -3 --version >nul 2>&1
if not errorlevel 1 goto PYLAUNCHER
python --version >nul 2>&1
if not errorlevel 1 goto PYTHON
echo Python nu a fost gasit. Instaleaza Python 3.11 sau mai nou, apoi reia.
pause
exit /b 1

:VENV
"%~dp0.venv\Scripts\python.exe" "%~dp0initializare.py" %*
goto FINISH

:PYLAUNCHER
py -3 "%~dp0initializare.py" %*
goto FINISH

:PYTHON
python "%~dp0initializare.py" %*
goto FINISH

:FINISH
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Operatia nu s-a incheiat cu succes. Consulta mesajul de mai sus.
pause
exit /b %RESULT%
