@echo off
chcp 65001 >nul 2>&1

:: Safety net: if run by double-click, restart inside cmd /k so window stays open on error
if "%TRADUCATOR_WRAPPED%"=="1" goto :WRAPPED
set "TRADUCATOR_WRAPPED=1"
cmd /k "%~f0" %*
exit /b

:WRAPPED
setlocal enabledelayedexpansion
title Traducator Offline - Manager
color 0F

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

set CONTAINER_NAME=traducator-offline
set IMAGE_NAME=traducator-offline
set DEFAULT_PORT=80
set DEFAULT_OLLAMA=http://host.containers.internal:11434

:MENU
cls
echo.
echo  ============================================================
echo       TRADUCATOR OFFLINE - Manager
echo  ============================================================
echo.
echo   Container: %CONTAINER_NAME%
echo   Director:  %CD%
echo.
echo  ------------------------------------------------------------
echo.
echo   1.  Incarca imaginea     (podman load din .tar)
echo   2.  Porneste serviciile  (pornire container)
echo   3.  Verifica serviciile  (LibreTranslate, Ollama, Web)
echo   4.  Opreste serviciile   (oprire container)
echo   5.  Loguri in timp real  (loguri container live)
echo   6.  Status container     (podman ps, images, inspect)
echo   7.  Curata fisierele     (sterge uploads/traduceri)
echo   0.  Iesire
echo.
echo  ============================================================
echo.
set /p CHOICE="  Selecteaza optiunea [0-7]: "

if "%CHOICE%"=="1" goto LOAD
if "%CHOICE%"=="2" goto START
if "%CHOICE%"=="3" goto CHECK
if "%CHOICE%"=="4" goto STOP
if "%CHOICE%"=="5" goto LOGS
if "%CHOICE%"=="6" goto STATUS
if "%CHOICE%"=="7" goto CLEANUP
if "%CHOICE%"=="0" goto EXIT

echo.
echo  Optiune invalida. Apasa orice tasta...
pause >nul
goto MENU

:: ============================================================
:: 1. INCARCA IMAGINEA
:: ============================================================
:LOAD
cls
echo.
echo  ============================================================
echo   1. INCARCA IMAGINEA DIN FISIER .TAR
echo  ============================================================
echo.
echo  Se cauta fisiere .tar in directorul curent...
echo  Director: %CD%
echo.

:: Search for .tar files in current directory
set TAR_COUNT=0
set TAR_FILE=
for %%f in (*.tar) do (
    set /a TAR_COUNT+=1
    set "TAR_FILE=%%f"
    for %%A in ("%%f") do (
        set /a SIZE_MB=%%~zA / 1048576
        echo    Gasit: %%f (!SIZE_MB! MB)
    )
)

if %TAR_COUNT%==0 (
    echo  ATENTIE: Nu s-a gasit niciun fisier .tar in directorul curent.
    echo.
    echo  Asigura-te ca fisierul traducator-offline.tar se afla in:
    echo    %CD%
    echo.
    pause
    goto MENU
)

echo.
if %TAR_COUNT%==1 (
    echo  S-a gasit un singur fisier: %TAR_FILE%
    set /p CONFIRM="  Incarc acest fisier? (D/N): "
    if /i not "!CONFIRM!"=="D" goto MENU
) else (
    echo  S-au gasit %TAR_COUNT% fisiere .tar.
    set /p TAR_FILE="  Introdu numele fisierului de incarcat: "
)

if not exist "%TAR_FILE%" (
    echo.
    echo  EROARE: Fisierul nu a fost gasit: %TAR_FILE%
    echo.
    pause
    goto MENU
)

echo.
echo  Se incarca imaginea din %TAR_FILE% ...
echo  (Poate dura cateva minute)
echo.
podman load -i "%TAR_FILE%"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  EROARE: Incarcarea a esuat! Verifica fisierul si incearca din nou.
) else (
    echo.
    echo  SUCCES: Imaginea a fost incarcata!
    echo.
    echo  Verificare:
    podman images %IMAGE_NAME%
)
echo.
pause
goto MENU

:: ============================================================
:: 2. PORNESTE SERVICIILE
:: ============================================================
:START
cls
echo.
echo  ============================================================
echo   2. PORNESTE SERVICIILE
echo  ============================================================
echo.
echo  Setari implicite:
echo    Port:       %DEFAULT_PORT%
echo    Ollama URL: %DEFAULT_OLLAMA%
echo.

set /p USER_PORT="  Port [%DEFAULT_PORT%]: "
if "%USER_PORT%"=="" set USER_PORT=%DEFAULT_PORT%

set /p USER_OLLAMA="  Ollama URL [%DEFAULT_OLLAMA%]: "
if "%USER_OLLAMA%"=="" set USER_OLLAMA=%DEFAULT_OLLAMA%

echo.
echo  Se opreste containerul existent (daca exista)...
podman stop %CONTAINER_NAME% 2>nul
podman rm %CONTAINER_NAME% 2>nul

echo.
echo  Se porneste containerul...
echo    Port:       %USER_PORT%
echo    Ollama URL: %USER_OLLAMA%
echo.

podman run -d ^
    --name %CONTAINER_NAME% ^
    -p %USER_PORT%:8080 ^
    -e OLLAMA_URL=%USER_OLLAMA% ^
    -e PORT=8080 ^
    --restart unless-stopped ^
    %IMAGE_NAME%

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  EROARE: Containerul nu a pornit!
    echo  Asigura-te ca imaginea este incarcata (optiunea 1).
    echo.
    pause
    goto MENU
)

echo.
echo  Containerul a pornit! Se asteapta initializarea serviciilor...
echo.

:: Wait for web server to be ready
for /L %%i in (1,1,30) do (
    curl -s -o nul http://localhost:%USER_PORT%/api/status 2>nul && goto START_READY
    echo    Se asteapta... %%i/30
    timeout /t 2 /nobreak >nul
)

:START_READY
echo.
echo  ============================================================
echo   SERVICIILE AU PORNIT
echo  ============================================================
echo.
echo   Interfata web:  http://localhost:%USER_PORT%
echo   Din retea:      http://%COMPUTERNAME%:%USER_PORT%
echo.
echo   Deschid browserul? (D/N)
set /p OPEN_BROWSER="  "
if /i "%OPEN_BROWSER%"=="D" start http://localhost:%USER_PORT%

echo.
pause
goto MENU

:: ============================================================
:: 3. VERIFICA SERVICIILE
:: ============================================================
:CHECK
cls
echo.
echo  ============================================================
echo   3. VERIFICARE SERVICII
echo  ============================================================
echo.

:: Check container
echo  [CONTAINER]
podman ps --filter name=%CONTAINER_NAME% --format "    Stare: {{.Status}}" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo    Stare: NU RULEAZA
)
echo.

:: Detect port
set CHECK_PORT=%DEFAULT_PORT%
for /f "tokens=*" %%a in ('podman port %CONTAINER_NAME% 8080 2^>nul') do (
    for /f "tokens=2 delims=:" %%b in ("%%a") do set CHECK_PORT=%%b
)

:: Check Web Server
echo  [SERVER WEB - port %CHECK_PORT%]
curl -s -o nul -w "    HTTP Status: %%{http_code}" http://localhost:%CHECK_PORT%/api/status 2>nul
echo.
if %ERRORLEVEL% NEQ 0 (
    echo    Rezultat: INACCESIBIL
) else (
    echo    Rezultat: OK
)
echo.

:: Check LibreTranslate
echo  [LIBRETRANSLATE - port intern 5000]
curl -s http://localhost:%CHECK_PORT%/api/libretranslate 2>nul | findstr /i "true" >nul
if %ERRORLEVEL%==0 (
    echo    Stare: CONECTAT
    for /f "delims=" %%a in ('curl -s http://localhost:%CHECK_PORT%/api/libretranslate 2^>nul') do (
        echo    Raspuns: %%a
    )
) else (
    echo    Stare: DECONECTAT sau IN CURS DE INITIALIZARE
    echo    (LibreTranslate poate dura pana la 60s sa porneasca)
)
echo.

:: Check Ollama
echo  [OLLAMA - extern]
curl -s http://localhost:%CHECK_PORT%/api/ollama 2>nul | findstr /i "true" >nul
if %ERRORLEVEL%==0 (
    echo    Stare: CONECTAT
    for /f "delims=" %%a in ('curl -s http://localhost:%CHECK_PORT%/api/models 2^>nul') do (
        echo    Raspuns: %%a
    )
) else (
    echo    Stare: DECONECTAT
    echo    (Ollama ruleaza extern - asigura-te ca este pornit pe statie)
)
echo.

echo  ============================================================
echo.
pause
goto MENU

:: ============================================================
:: 4. OPRESTE SERVICIILE
:: ============================================================
:STOP
cls
echo.
echo  ============================================================
echo   4. OPRIRE SERVICII
echo  ============================================================
echo.
echo  Se opreste containerul %CONTAINER_NAME%...
echo.

podman stop %CONTAINER_NAME% 2>nul

if %ERRORLEVEL% NEQ 0 (
    echo  Containerul nu rula.
) else (
    echo  Containerul a fost oprit cu succes.
)

echo.
set /p REMOVE="  Stergi si containerul? (D/N): "
if /i "%REMOVE%"=="D" (
    podman rm %CONTAINER_NAME% 2>nul
    echo  Containerul a fost sters.
)

echo.
pause
goto MENU

:: ============================================================
:: 5. LOGURI IN TIMP REAL
:: ============================================================
:LOGS
cls
echo.
echo  ============================================================
echo   5. LOGURI IN TIMP REAL
echo  ============================================================
echo.
echo  Se afiseaza logurile pentru %CONTAINER_NAME%...
echo  Apasa Ctrl+C pentru a opri, apoi orice tasta pentru meniu.
echo.
echo  ============================================================
echo.

podman logs -f %CONTAINER_NAME%

echo.
echo  Fluxul de loguri s-a incheiat.
echo.
pause
goto MENU

:: ============================================================
:: 6. STATUS CONTAINER
:: ============================================================
:STATUS
cls
echo.
echo  ============================================================
echo   6. STATUS CONTAINER SI IMAGINE
echo  ============================================================
echo.
echo  --- Containere ---
podman ps -a --filter name=%CONTAINER_NAME%
echo.
echo  --- Imagini ---
podman images %IMAGE_NAME%
echo.
echo  --- Detalii container ---
podman inspect %CONTAINER_NAME% --format "    Nume:     {{.Name}}" 2>nul
podman inspect %CONTAINER_NAME% --format "    Stare:    {{.State.Status}}" 2>nul
podman inspect %CONTAINER_NAME% --format "    Pornit:   {{.State.StartedAt}}" 2>nul
echo  --- Porturi ---
podman port %CONTAINER_NAME% 2>nul
echo.
pause
goto MENU

:: ============================================================
:: 7. CURATA FISIERELE
:: ============================================================
:CLEANUP
cls
echo.
echo  ============================================================
echo   7. CURATARE FISIERE INCARCATE SI TRADUSE
echo  ============================================================
echo.
echo  Aceasta actiune va sterge TOATE fisierele incarcate
echo  si traduse din container.
echo.
set /p CONFIRM_CLEAN="  Esti sigur? (D/N): "
if /i not "%CONFIRM_CLEAN%"=="D" goto MENU

:: Detect port
set CLEAN_PORT=%DEFAULT_PORT%
for /f "tokens=*" %%a in ('podman port %CONTAINER_NAME% 8080 2^>nul') do (
    for /f "tokens=2 delims=:" %%b in ("%%a") do set CLEAN_PORT=%%b
)

echo.
echo  Se curata fisierele...
curl -s -X POST http://localhost:%CLEAN_PORT%/api/files/cleanup
echo.
echo.
echo  Curatarea s-a finalizat.
echo.
pause
goto MENU

:: ============================================================
:: 0. IESIRE
:: ============================================================
:EXIT
cls
echo.
echo  La revedere!
echo.
endlocal
exit
