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
if "!SCRIPT_DIR:~-1!"=="\" set "SCRIPT_DIR=!SCRIPT_DIR:~0,-1!"
cd /d "!SCRIPT_DIR!"

set "IMAGE_NAME=traducator-offline"
set "TAR_NAME=traducator-offline.tar"
set "DEFAULT_LANGS=en,ro"

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
set /p "CHOICE=  Selecteaza optiunea [0-4]: "

if "!CHOICE!"=="1" goto SELECT_LANGS
if "!CHOICE!"=="2" goto EXPORT
if "!CHOICE!"=="3" goto SELECT_LANGS_EXPORT
if "!CHOICE!"=="4" goto CHECK
if "!CHOICE!"=="0" goto EXIT

echo.
echo  Optiune invalida. Apasa orice tasta...
pause >nul
goto MENU

:: ============================================================
:: LANGUAGE SELECTION
:: ============================================================
:SELECT_LANGS
set "BUILD_THEN_EXPORT=0"
goto LANG_MENU

:SELECT_LANGS_EXPORT
set "BUILD_THEN_EXPORT=1"
goto LANG_MENU

:LANG_MENU
cls
echo.
echo  ============================================================
echo   SELECTARE LIMBI PENTRU LIBRETRANSLATE
echo  ============================================================
echo.
echo  Engleza (en) si Romana (ro) sunt INTOTDEAUNA incluse.
echo  Selecteaza limbile suplimentare pe care le doresti.
echo.
echo  NOTA: Fiecare limba adaugata mareste dimensiunea imaginii
echo  cu aproximativ 50-100 MB si timpul de build cu 1-2 minute.
echo.
echo  ------------------------------------------------------------
echo   Limbi disponibile:
echo  ------------------------------------------------------------
echo.
echo   Europene:
echo     fr = Franceza          de = Germana         es = Spaniola
echo     it = Italiana          pt = Portugheza      nl = Olandeza
echo     pl = Poloneza          cs = Ceha            sk = Slovaca
echo     hu = Maghiara          bg = Bulgara         el = Greaca
echo     da = Daneza            sv = Suedeza         fi = Finlandeza
echo     nb = Norvegiana        et = Estoniana       lv = Letona
echo     lt = Lituaniana        sl = Slovena         ca = Catalana
echo     gl = Galiciana         eu = Basca           sq = Albaneza
echo     ga = Irlandeza         uk = Ucraineana      ru = Rusa
echo.
echo   Asiatice:
echo     zh = Chineza           zt = Chineza (trad.) ja = Japoneza
echo     ko = Coreeana          hi = Hindi           th = Thailandeza
echo     vi = Vietnameza        id = Indoneziana     ms = Malaeziena
echo     bn = Bengaleza         ur = Urdu            tl = Tagalog
echo.
echo   Alte limbi:
echo     ar = Araba             fa = Persana         he = Ebraica
echo     tr = Turca             az = Azera           ky = Kirgiza
echo     eo = Esperanto
echo.
echo  ------------------------------------------------------------
echo.
echo   Exemple:
echo     en,ro             (doar engleza + romana, minim)
echo     en,ro,fr          (+ franceza)
echo     en,ro,fr,de,es,it (+ franceza, germana, spaniola, italiana)
echo.
set /p "USER_LANGS=  Introdu limbile [%DEFAULT_LANGS%]: "
if "!USER_LANGS!"=="" set "USER_LANGS=%DEFAULT_LANGS%"

:: Ensure en and ro are included
echo !USER_LANGS! | findstr /i "en" >nul 2>&1
if errorlevel 1 set "USER_LANGS=en,!USER_LANGS!"
echo !USER_LANGS! | findstr /i "ro" >nul 2>&1
if errorlevel 1 set "USER_LANGS=!USER_LANGS!,ro"

echo.
echo  Limbi selectate: !USER_LANGS!
echo.
set /p "CONFIRM_LANGS=  Continui cu aceste limbi? (D/N): "
if /i not "!CONFIRM_LANGS!"=="D" goto LANG_MENU

if "!BUILD_THEN_EXPORT!"=="1" goto BUILDEXPORT
goto BUILD

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
echo  Limbi: !USER_LANGS!
echo.
echo  Se construieste imaginea %IMAGE_NAME%...
echo  (Poate dura 5-15 minute la prima rulare, depinde de numarul de limbi)
echo.

podman build -t %IMAGE_NAME% --build-arg LANGUAGES=!USER_LANGS! -f Containerfile .

if !ERRORLEVEL! NEQ 0 (
    echo.
    echo  EROARE: Construirea a esuat! Verifica mesajele de mai sus.
) else (
    echo.
    echo  SUCCES: Imaginea a fost construita cu limbile: !USER_LANGS!
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
echo  Se lanseaza exportul in fereastra noua...
echo  (Aceasta fereastra revine la meniu)
echo.

start "Traducator - Export" cmd /c "chcp 65001 >nul 2>&1 && echo. && echo  ============================================================ && echo   EXPORT IMAGINE .TAR && echo  ============================================================ && echo. && echo  Se exporta %IMAGE_NAME% in %TAR_NAME% ... && echo  (Poate dura cateva minute) && echo. && podman save %IMAGE_NAME% -o "%CD%\%TAR_NAME%" && ( echo. && echo  SUCCES: Imaginea a fost exportata! && echo  Fisier: %CD%\%TAR_NAME% && echo. && echo  Copiaza pe stick USB: && echo    - %TAR_NAME% && echo    - traducator_manager.bat && echo    - TUTORIAL_DOCKER.md ) || ( echo. && echo  EROARE: Exportul a esuat! Asigura-te ca imaginea exista. ) && echo. && pause"

timeout /t 2 >nul
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
echo  Limbi: !USER_LANGS!
echo.
echo  Pasul 1/2: Construire imagine...
echo.

podman build -t %IMAGE_NAME% --build-arg LANGUAGES=!USER_LANGS! -f Containerfile .

if !ERRORLEVEL! NEQ 0 (
    echo.
    echo  EROARE: Construirea a esuat!
    echo.
    pause
    goto MENU
)

echo.
echo  Pasul 2/2: Export .tar - se lanseaza in fereastra noua...
echo.

start "Traducator - Export" cmd /c "chcp 65001 >nul 2>&1 && echo. && echo  ============================================================ && echo   PASUL 2/2: EXPORT .TAR && echo  ============================================================ && echo. && echo  Limbi incluse: !USER_LANGS! && echo. && echo  Se exporta %IMAGE_NAME% in %TAR_NAME% ... && echo  (Poate dura cateva minute) && echo. && podman save %IMAGE_NAME% -o "%CD%\%TAR_NAME%" && ( echo. && echo  ============================================================ && echo   TOTUL GATA! && echo  ============================================================ && echo. && echo  Fisier: %CD%\%TAR_NAME% && echo. && echo  Copiaza pe stick USB catre statia offline: && echo    - %TAR_NAME% && echo    - traducator_manager.bat && echo    - TUTORIAL_DOCKER.md ) || ( echo. && echo  EROARE: Exportul a esuat! ) && echo. && pause"

echo.
echo  Exportul ruleaza in fereastra separata.
echo  Constructia s-a finalizat cu succes.
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
        echo  Fisier .tar gasit: %TAR_NAME% (!SIZE_MB! MB^)
    )
) else (
    echo  Fisierul .tar nu exista inca. Foloseste optiunea 2 sau 3.
)
echo.
echo  Verificare limbi instalate in imagine:
podman run --rm %IMAGE_NAME% cat /app/installed_languages.txt 2>nul
echo.
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
