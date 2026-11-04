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
if "!SCRIPT_DIR:~-1!"=="\" set "SCRIPT_DIR=!SCRIPT_DIR:~0,-1!"
cd /d "!SCRIPT_DIR!"

set "CONTAINER_NAME=traducator-offline"
set "IMAGE_NAME=traducator-offline"
set "DEFAULT_PORT=80"
set "SECRET_NAME=traducator-password"
set "UPLOAD_VOLUME=traducator-uploads"
set "OUTPUT_VOLUME=traducator-outputs"

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
echo   8.  Configureaza Ollama  (setare OLLAMA_HOST pe sistem)
echo   9.  Diagnosticare retea  (test conectivitate container)
echo   0.  Iesire
echo.
echo  ============================================================
echo.
set /p "CHOICE=  Selecteaza optiunea [0-9]: "

if "!CHOICE!"=="1" goto LOAD
if "!CHOICE!"=="2" goto START
if "!CHOICE!"=="3" goto CHECK
if "!CHOICE!"=="4" goto STOP
if "!CHOICE!"=="5" goto LOGS
if "!CHOICE!"=="6" goto STATUS
if "!CHOICE!"=="7" goto CLEANUP
if "!CHOICE!"=="8" goto OLLAMA_SETUP
if "!CHOICE!"=="9" goto NETDIAG
if "!CHOICE!"=="0" goto EXIT

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

set "TAR_COUNT=0"
set "TAR_FILE="
for %%f in (*.tar) do (
    set /a TAR_COUNT+=1
    set "TAR_FILE=%%f"
    echo    Gasit: %%f
)

if !TAR_COUNT! EQU 0 (
    echo  ATENTIE: Nu s-a gasit niciun fisier .tar in directorul curent.
    echo.
    echo  Asigura-te ca fisierul traducator-offline.tar se afla in:
    echo    %CD%
    echo.
    pause
    goto MENU
)

echo.
if !TAR_COUNT! EQU 1 (
    echo  S-a gasit un singur fisier: !TAR_FILE!
    set /p "CONFIRM=  Incarc acest fisier? (D/N): "
    if /i not "!CONFIRM!"=="D" goto MENU
) else (
    echo  S-au gasit !TAR_COUNT! fisiere .tar.
    set /p "TAR_FILE=  Introdu numele fisierului de incarcat: "
)

if not exist "!TAR_FILE!" (
    echo.
    echo  EROARE: Fisierul nu a fost gasit: !TAR_FILE!
    echo.
    pause
    goto MENU
)

echo.
echo  Se incarca imaginea din !TAR_FILE! ...
echo  (Poate dura cateva minute)
echo.
podman load -i "!TAR_FILE!"

if errorlevel 1 (
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
:: Start preserves the existing container and its documents/settings.
podman container exists %CONTAINER_NAME% >nul 2>&1
if not errorlevel 1 goto START_EXISTING

set "USER_PORT="
set /p "USER_PORT=  Port web [%DEFAULT_PORT%]: "
if "!USER_PORT!"=="" set "USER_PORT=%DEFAULT_PORT%"
powershell -NoProfile -Command "$n=0; if (-not [int]::TryParse($env:USER_PORT,[ref]$n) -or $n -lt 1 -or $n -gt 65535) { exit 1 }"
if errorlevel 1 goto START_INVALID_SETTINGS

echo.
echo  Introdu adresa serverului Ollama; Enter pentru detectie automata.
set "USER_OLLAMA="
set /p "USER_OLLAMA=  Ollama URL [auto]: "
if "!USER_OLLAMA!"=="" set "USER_OLLAMA=auto"
powershell -NoProfile -Command "if ($env:USER_OLLAMA -eq 'auto') { exit 0 }; $u=$null; if (-not [uri]::TryCreate($env:USER_OLLAMA,[UriKind]::Absolute,[ref]$u)) { exit 1 }; if ($u.Scheme -notin @('http','https') -or $u.UserInfo -or $u.Query -or $u.Fragment -or $u.AbsolutePath -ne '/') { exit 1 }"
if errorlevel 1 goto START_INVALID_SETTINGS

:: Passwords are never read into cmd.exe or passed on a command line.
podman secret inspect %SECRET_NAME% >nul 2>&1
if not errorlevel 1 goto START_SECRET_READY
powershell -NoProfile -ExecutionPolicy Bypass -File "!SCRIPT_DIR!\configure_access.ps1"
if errorlevel 1 goto START_FAILED
podman secret create %SECRET_NAME% "!SCRIPT_DIR!\translator-password.txt" >nul
if errorlevel 1 goto START_FAILED

:START_SECRET_READY
echo.
echo  Se creeaza un container cu volume persistente si acces cu parola.
echo  Utilizator in browser: translator
podman run -d --name %CONTAINER_NAME% --label org_translator_manager=2 -p !USER_PORT!:8080 -e "OLLAMA_URL=!USER_OLLAMA!" -e PORT=8080 -e TRANSLATOR_PASSWORD_FILE=/run/secrets/translator-password --secret %SECRET_NAME%,type=mount,target=translator-password -v %UPLOAD_VOLUME%:/app/uploads -v %OUTPUT_VOLUME%:/app/outputs --restart unless-stopped %IMAGE_NAME%
if errorlevel 1 goto START_FAILED
goto START_WAIT

:START_EXISTING
set "MANAGED_VERSION="
for /f "delims=" %%a in ('podman inspect --format "{{.Config.Labels.org_translator_manager}}" %CONTAINER_NAME% 2^>nul') do set "MANAGED_VERSION=%%a"
if not "!MANAGED_VERSION!"=="2" goto START_LEGACY
podman start %CONTAINER_NAME% >nul
if errorlevel 1 goto START_FAILED
set "USER_PORT=%DEFAULT_PORT%"
for /f "delims=" %%a in ('powershell -NoProfile -Command "$p=@(podman port $env:CONTAINER_NAME 8080/tcp); if ($LASTEXITCODE -ne 0 -or $p.Count -eq 0) { exit 1 }; ($p[0] -split ':')[-1]"') do set "USER_PORT=%%a"
echo  Containerul existent a fost pornit. Documentele si configuratia sunt pastrate.
echo  Pentru actualizarea imaginii, urmeaza procedura de upgrade din README.
goto START_WAIT

:START_LEGACY
echo  Containerul existent provine dintr-o versiune anterioara sau are alta configuratie.
echo  Nu a fost sters sau pornit automat. Documentele sale au fost pastrate.
echo  Urmeaza pasii de backup si migrare din README inainte de recreare.
echo  Versiunea noua necesita parola comuna si volume persistente.
pause
goto MENU

:START_INVALID_SETTINGS
echo  EROARE: Port invalid sau URL Ollama invalid.
echo  Foloseste port 1-65535 si URL http/https cu gazda si port, fara cale sau parola.
pause
goto MENU

:START_WAIT
set "WAIT_COUNT=0"
:WAIT_LOOP
set /a WAIT_COUNT+=1
if !WAIT_COUNT! GTR 30 goto START_TIMEOUT
curl --fail --silent --connect-timeout 2 --max-time 4 -o NUL "http://127.0.0.1:!USER_PORT!/healthz" >nul 2>&1
if not errorlevel 1 goto START_READY
echo    Se asteapta initializarea motorului... !WAIT_COUNT!/30
timeout /t 2 /nobreak >nul
goto WAIT_LOOP

:START_TIMEOUT
echo  Motorul selectat nu a devenit disponibil. Verifica optiunile 3 si 5.
echo  Containerul si documentele sunt pastrate.
goto START_DONE

:START_READY
echo.
echo  Aplicatia si motorul selectat sunt pregatite.
echo  Interfata web: http://localhost:!USER_PORT!
echo  Din retea: http://%COMPUTERNAME%:!USER_PORT!
echo  Utilizator: translator - parola stabilita la prima configurare.

:START_DONE
set "OPEN_BROWSER="
set /p "OPEN_BROWSER=  Deschid browserul? (D/N): "
if /i "!OPEN_BROWSER!"=="D" start "" "http://localhost:!USER_PORT!"
pause
goto MENU

:START_FAILED
echo  EROARE: Pornirea a esuat. Verifica imaginea, parola si logurile.
echo  Niciun container existent nu a fost sters.
pause
goto MENU

:: ============================================================
:: 3. VERIFICA SERVICIILE
:: ============================================================
:CHECK
cls
echo.
echo  Verificare autentificata a aplicatiei si motoarelor...
podman exec %CONTAINER_NAME% python3 /app/manager_client.py status
if errorlevel 1 echo  Verificarea a esuat. Consulta logurile cu optiunea 5.
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

podman stop %CONTAINER_NAME% >nul 2>&1

if errorlevel 1 (
    echo  Containerul nu rula sau nu exista.
) else (
    echo  Containerul a fost oprit cu succes.
)

echo.
echo  Stergerea containerului vechi fara volume poate pierde documentele sale.
echo  Volumele traducator-uploads/traducator-outputs ale versiunii noi se pastreaza.
set "REMOVE="
set /p "REMOVE=  Stergi si containerul? (D/N): "
if /i "!REMOVE!"=="D" (
    podman rm %CONTAINER_NAME% >nul 2>&1
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
podman ps -a --filter "name=%CONTAINER_NAME%"
echo.
echo  --- Imagini ---
podman images %IMAGE_NAME%
echo.
echo  --- Detalii container ---
for /f "tokens=*" %%a in ('podman inspect %CONTAINER_NAME% --format "{{.Name}}" 2^>nul') do echo    Nume:     %%a
for /f "tokens=*" %%a in ('podman inspect %CONTAINER_NAME% --format "{{.State.Status}}" 2^>nul') do echo    Stare:    %%a
for /f "tokens=*" %%a in ('podman inspect %CONTAINER_NAME% --format "{{.State.StartedAt}}" 2^>nul') do echo    Pornit:   %%a
echo.
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
set /p "CONFIRM_CLEAN=  Esti sigur? (D/N): "
if /i not "!CONFIRM_CLEAN!"=="D" goto MENU

echo.
echo  Se curata fisierele eligibile; joburile active sunt protejate.
podman exec %CONTAINER_NAME% python3 /app/manager_client.py cleanup
if errorlevel 1 (
    echo  EROARE: Curatarea nu a reusit. Fisierele neprocesate au fost pastrate.
) else (
    echo  Curatarea s-a finalizat.
)
echo.

pause
goto MENU

:: ============================================================
:: 8. CONFIGUREAZA OLLAMA
:: ============================================================
:OLLAMA_SETUP
cls
echo.
echo  ============================================================
echo   8. CONFIGURARE OLLAMA PENTRU ACCES DIN CONTAINER
echo  ============================================================
echo.
echo  Pentru ca containerul sa poata accesa Ollama, acesta trebuie
echo  sa asculte pe toate interfetele de retea (0.0.0.0).
echo.
echo  Aceasta optiune va seta variabila de mediu OLLAMA_HOST
echo  la nivel de sistem (permanenta, supravietuieste restart).
echo.
echo  NOTA: Dupa setare, Ollama trebuie repornit.
echo.

:: Check current value
echo  [STARE CURENTA]
set "CURRENT_OLLAMA_HOST="
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v OLLAMA_HOST 2^>nul ^| findstr OLLAMA_HOST') do set "CURRENT_OLLAMA_HOST=%%b"

if defined CURRENT_OLLAMA_HOST (
    echo    OLLAMA_HOST = !CURRENT_OLLAMA_HOST!
) else (
    echo    OLLAMA_HOST nu este setat (Ollama asculta doar pe 127.0.0.1)
)
echo.

:: Check if Ollama is running
echo  [OLLAMA PROCESS]
tasklist /fi "imagename eq ollama.exe" 2>nul | findstr /i "ollama" >nul 2>&1
if not errorlevel 1 (
    echo    Ollama ruleaza.
) else (
    echo    Ollama NU ruleaza.
)
echo.

set /p "DO_SETUP=  Setez OLLAMA_HOST=0.0.0.0:11434? (D/N): "
if /i not "!DO_SETUP!"=="D" goto MENU

echo.
echo  Se seteaza OLLAMA_HOST=0.0.0.0:11434 ...

:: Set user environment variable (persistent)
setx OLLAMA_HOST "0.0.0.0:11434" >nul 2>&1
if errorlevel 1 (
    echo  EROARE: Nu s-a putut seta variabila de mediu.
    echo  Incearca sa rulezi acest script ca Administrator.
    echo.
    pause
    goto MENU
)

:: Also set for current session
set "OLLAMA_HOST=0.0.0.0:11434"

echo  SUCCES: OLLAMA_HOST=0.0.0.0:11434 setat permanent.
echo.
echo  IMPORTANT: Trebuie sa repornesti Ollama pentru a aplica setarea.
echo.

set /p "RESTART_OLLAMA=  Repornesc Ollama acum? (D/N): "
if /i "!RESTART_OLLAMA!"=="D" (
    echo.
    echo  Se opreste Ollama...
    taskkill /f /im ollama.exe >nul 2>&1
    timeout /t 2 /nobreak >nul

    echo  Se porneste Ollama...
    start "" "ollama" serve
    timeout /t 3 /nobreak >nul

    echo  Ollama a fost repornit.
    echo.
    echo  Verificare:
    curl --fail --silent --connect-timeout 2 --max-time 5 "http://localhost:11434/api/version" 2>nul
    echo.
)

echo.
pause
goto MENU

:: ============================================================
:: 9. DIAGNOSTICARE RETEA
:: ============================================================
:NETDIAG
cls
echo.
echo  ============================================================
echo   9. DIAGNOSTICARE RETEA (CONTAINER - GAZDA)
echo  ============================================================
echo.

:: Check container is running
set "CONTAINER_RUNNING=0"
for /f "tokens=*" %%a in ('podman ps --filter "name=%CONTAINER_NAME%" --format "{{.Status}}" 2^>nul') do (
    set "CONTAINER_RUNNING=1"
)

if !CONTAINER_RUNNING! EQU 0 (
    echo  Containerul nu ruleaza. Porneste-l cu optiunea 2.
    echo.
    pause
    goto MENU
)

echo  [GAZDA WINDOWS]
echo    Hostname: %COMPUTERNAME%
echo.

echo  Se verifica daca Ollama ruleaza pe gazda...
curl --fail --silent --connect-timeout 2 --max-time 5 "http://localhost:11434/api/version" >nul 2>&1
if not errorlevel 1 (
    echo    Ollama pe gazda: DA (port 11434 raspunde)
    for /f "delims=" %%a in ('curl --fail --silent --connect-timeout 2 --max-time 5 "http://localhost:11434/api/version" 2^>nul') do echo    Versiune: %%a
) else (
    echo    Ollama pe gazda: NU (port 11434 nu raspunde)
    echo    Porneste Ollama si asigura-te ca OLLAMA_HOST=0.0.0.0:11434
)
echo.

echo  [DIAGNOSTICARE DIN CONTAINER]
echo  Se ruleaza teste de retea din container...
echo.

:: Run network diagnostics inside container
echo  --- Rute container ---
podman exec %CONTAINER_NAME% ip route 2>nul
echo.

echo  --- IP container ---
podman exec %CONTAINER_NAME% hostname -I 2>nul
echo.

echo  --- Ping host.containers.internal ---
podman exec %CONTAINER_NAME% ping -c 1 -W 2 host.containers.internal 2>nul
echo.

echo  --- Ping host.docker.internal ---
podman exec %CONTAINER_NAME% ping -c 1 -W 2 host.docker.internal 2>nul
echo.

echo  --- Test gateway ---
for /f "tokens=3" %%a in ('podman exec %CONTAINER_NAME% ip route show default 2^>nul') do (
    echo  Gateway: %%a
    echo  --- Ping gateway ---
    podman exec %CONTAINER_NAME% ping -c 1 -W 2 %%a 2>nul
    echo.
    echo  --- Test Ollama pe gateway ---
    podman exec %CONTAINER_NAME% curl --fail --silent --connect-timeout 3 --max-time 5 "http://%%a:11434/api/version" 2>nul
    echo.
)
echo.

echo  --- Test Ollama pe host.containers.internal ---
podman exec %CONTAINER_NAME% curl --fail --silent --connect-timeout 3 --max-time 5 "http://host.containers.internal:11434/api/version" 2>nul
echo.

echo  --- Rezolvare DNS ---
podman exec %CONTAINER_NAME% getent hosts host.containers.internal 2>nul
podman exec %CONTAINER_NAME% getent hosts host.docker.internal 2>nul
echo.

:: Diagnostics authenticate inside the container.
echo.

echo  --- Diagnosticare via API web ---
podman exec %CONTAINER_NAME% python3 /app/manager_client.py network
echo.
echo.

echo  ============================================================
echo.
echo  Daca Ollama nu e accesibil din container:
echo    1. Asigura-te ca Ollama ruleaza (ollama serve)
echo    2. Seteaza OLLAMA_HOST=0.0.0.0:11434 (optiunea 8)
echo    3. Verifica firewall-ul Windows (permite portul 11434)
echo    4. Reporneste containerul dupa modificari (opt. 4 apoi 2)
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
