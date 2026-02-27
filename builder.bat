@echo off
chcp 65001 >nul 2>&1

:: Safety net: if run by double-click, restart inside cmd /k so window stays open on error
if "%TRADUCATOR_WRAPPED%"=="1" goto :WRAPPED
set "TRADUCATOR_WRAPPED=1"
cmd /k "%~f0" %*
exit /b

:WRAPPED
setlocal enabledelayedexpansion
title Traducator Offline - Builder
color 0F

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

set IMAGE_NAME=traducator-offline
set TAR_NAME=traducator-offline.tar

:MENU
cls
echo.
echo  ============================================================
echo       TRADUCATOR OFFLINE - Builder (statie ONLINE)
echo  ============================================================
echo.
echo   Director: %CD%
echo   Imagine:  %IMAGE_NAME%
echo.
echo  ------------------------------------------------------------
echo.
echo   1.  Construieste imaginea  (podman build)
echo   2.  Exporta .tar           (podman save)
echo   3.  Construieste + Exporta (build + save)
echo   4.  Verifica imagine       (podman images)
echo   0.  Iesire
echo.
echo  ============================================================
echo.
set /p CHOICE="  Selecteaza optiunea [0-4]: "

if "%CHOICE%"=="1" goto BUILD
if "%CHOICE%"=="2" goto EXPORT
if "%CHOICE%"=="3" goto BUILDEXPORT
if "%CHOICE%"=="4" goto CHECK
if "%CHOICE%"=="0" goto EXIT

echo.
echo  Optiune invalida. Apasa orice tasta...
pause >nul
goto MENU

:: ============================================================
:: 1. BUILD
:: ============================================================
:BUILD
cls
echo.
echo  ============================================================
echo   CONSTRUIRE IMAGINE
echo  ============================================================
echo.
echo  Se construieste imaginea %IMAGE_NAME%...
echo  (Poate dura 5-15 minute la prima rulare)
echo.

podman build -t %IMAGE_NAME% -f Containerfile .

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  EROARE: Construirea a esuat! Verifica mesajele de mai sus.
) else (
    echo.
    echo  SUCCES: Imaginea a fost construita!
    echo.
    podman images %IMAGE_NAME%
)

echo.
pause
goto MENU

:: ============================================================
:: 2. EXPORT
:: ============================================================
:EXPORT
cls
echo.
echo  ============================================================
echo   EXPORT IMAGINE .TAR
echo  ============================================================
echo.
echo  Se exporta %IMAGE_NAME% in %TAR_NAME% ...
echo  (Poate dura cateva minute)
echo.

podman save %IMAGE_NAME% -o "%TAR_NAME%"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  EROARE: Exportul a esuat! Asigura-te ca imaginea exista (optiunea 1).
) else (
    echo.
    echo  SUCCES: Imaginea a fost exportata!
    for %%A in ("%TAR_NAME%") do (
        set /a SIZE_MB=%%~zA / 1048576
        echo  Fisier: %CD%\%TAR_NAME% (!SIZE_MB! MB)
    )
    echo.
    echo  Copiaza pe stick USB:
    echo    - %TAR_NAME%
    echo    - traducator_manager.bat
)

echo.
pause
goto MENU

:: ============================================================
:: 3. BUILD + EXPORT
:: ============================================================
:BUILDEXPORT
cls
echo.
echo  ============================================================
echo   CONSTRUIRE + EXPORT
echo  ============================================================
echo.
echo  Pasul 1/2: Construire imagine...
echo.

podman build -t %IMAGE_NAME% -f Containerfile .

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  EROARE: Construirea a esuat!
    echo.
    pause
    goto MENU
)

echo.
echo  Pasul 2/2: Export .tar...
echo.

podman save %IMAGE_NAME% -o "%TAR_NAME%"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  EROARE: Exportul a esuat!
) else (
    echo.
    echo  ============================================================
    echo   TOTUL GATA!
    echo  ============================================================
    for %%A in ("%TAR_NAME%") do (
        set /a SIZE_MB=%%~zA / 1048576
        echo  Fisier: %CD%\%TAR_NAME% (!SIZE_MB! MB)
    )
    echo.
    echo  Copiaza pe stick USB catre statia offline:
    echo    - %TAR_NAME%
    echo    - traducator_manager.bat
)

echo.
pause
goto MENU

:: ============================================================
:: 4. CHECK
:: ============================================================
:CHECK
cls
echo.
echo  ============================================================
echo   VERIFICARE IMAGINE
echo  ============================================================
echo.
podman images %IMAGE_NAME%
echo.
if exist "%TAR_NAME%" (
    for %%A in ("%TAR_NAME%") do (
        set /a SIZE_MB=%%~zA / 1048576
        echo  Fisier .tar gasit: %TAR_NAME% (!SIZE_MB! MB)
    )
) else (
    echo  Fisierul .tar nu exista inca. Foloseste optiunea 2 sau 3.
)
echo.
pause
goto MENU

:: ============================================================
:: 0. EXIT
:: ============================================================
:EXIT
cls
echo.
echo  La revedere!
echo.
endlocal
exit
