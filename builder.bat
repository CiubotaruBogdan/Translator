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

:: Master list of language codes supported by install_languages.py (lowercase, space-separated)
set "VALID=sq ar az eu bn bg ca zt zh cs da nl en eo et fi fr gl de el he hi hu id ga it ja ko ky lv lt ms nb fa pl pt pb ro ru sk sl es sv tl th tr uk ur vi"

:: Detect podman once (re-checked before each action too)
set "PODMAN_OK=1"
where podman >nul 2>&1 || set "PODMAN_OK=0"

:MENU
cls
echo.
echo  ============================================================
echo       TRADUCATOR OFFLINE - Builder (statie ONLINE)
echo  ============================================================
echo.
echo   Director: %CD%
echo   Imagine:  %IMAGE_NAME%
if "!PODMAN_OK!"=="0" (
    echo.
    echo   [ATENTIE] podman nu a fost gasit in PATH.
    echo       Instaleaza Podman Desktop si redeschide acest script.
)
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
set "CHOICE="
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
echo     eo = Esperanto         pb = Portugheza (BR)
echo.
echo  ------------------------------------------------------------
echo.
echo   Exemple:
echo     en,ro             (doar engleza + romana, minim)
echo     en,ro,fr          (+ franceza)
echo     en,ro,fr,de,es,it (+ franceza, germana, spaniola, italiana)
echo.
set "USER_LANGS="
set /p "USER_LANGS=  Introdu limbile [%DEFAULT_LANGS%]: "
if "!USER_LANGS!"=="" set "USER_LANGS=%DEFAULT_LANGS%"

:: -- Validate, normalize (lowercase canonical), de-duplicate --
:: 'for' splits on commas and spaces, so comma- or space-separated input both work.
set "CLEAN="
set "BADCODES="
for %%t in (!USER_LANGS!) do (
    set "canon="
    for %%v in (!VALID!) do if /i "%%v"=="%%t" set "canon=%%v"
    if defined canon (
        echo  !CLEAN!  | findstr /i /c:" !canon! " >nul
        if errorlevel 1 set "CLEAN=!CLEAN! !canon!"
    ) else (
        set "BADCODES=!BADCODES! %%t"
    )
)

:: Always put en + ro first, then the rest
set "FINAL=en ro"
for %%t in (!CLEAN!) do (
    if /i not "%%t"=="en" if /i not "%%t"=="ro" set "FINAL=!FINAL! %%t"
)
:: Trim leading space and turn spaces into commas
for /f "tokens=*" %%f in ("!FINAL!") do set "FINAL=%%f"
set "USER_LANGS=!FINAL: =,!"

echo.
if defined BADCODES echo  ATENTIE: coduri necunoscute ignorate:!BADCODES!
echo  Limbi selectate: !USER_LANGS!
echo.
set "CONFIRM_LANGS="
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
call :REQUIRE_BUILD_PREREQS || goto MENU
echo  Limbi: !USER_LANGS!
echo.
echo  Se construieste imaginea %IMAGE_NAME%...
echo  (Poate dura 5-15 minute la prima rulare, depinde de numarul de limbi)
echo.

podman build -t %IMAGE_NAME% --build-arg LANGUAGES=!USER_LANGS! -f Containerfile .

if errorlevel 1 (
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
call :REQUIRE_PODMAN || goto MENU
call :DO_EXPORT
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
call :REQUIRE_BUILD_PREREQS || goto MENU
echo  Limbi: !USER_LANGS!
echo.
echo  Pasul 1/2: Construire imagine...
echo.

podman build -t %IMAGE_NAME% --build-arg LANGUAGES=!USER_LANGS! -f Containerfile .

if errorlevel 1 (
    echo.
    echo  EROARE: Construirea a esuat! Exportul a fost anulat.
    echo.
    pause
    goto MENU
)

echo.
echo  Pasul 2/2: Export .tar...
echo.
call :DO_EXPORT
if "!EXPORT_OK!"=="1" (
    echo.
    echo  ============================================================
    echo   TOTUL GATA!
    echo  ============================================================
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
call :REQUIRE_PODMAN || goto MENU

podman image exists %IMAGE_NAME% 2>nul
if errorlevel 1 (
    echo  Imaginea %IMAGE_NAME% nu exista inca. Foloseste optiunea 1 sau 3.
) else (
    podman images %IMAGE_NAME%
    echo.
    echo  Limbi instalate in imagine:
    podman run --rm %IMAGE_NAME% printenv INSTALLED_LANGUAGES 2>nul
)
echo.
call :SHOW_TAR_INFO
echo.
pause
goto MENU

:: ============================================================
:: SUBROUTINES
:: ============================================================

:: Ensure podman is available; returns errorlevel 1 if not.
:REQUIRE_PODMAN
where podman >nul 2>&1
if errorlevel 1 (
    set "PODMAN_OK=0"
    echo  EROARE: Podman nu este instalat sau nu este in PATH.
    echo  Instaleaza Podman Desktop, apoi redeschide acest script.
    echo.
    pause
    exit /b 1
)
set "PODMAN_OK=1"
exit /b 0

:: Ensure podman + the files needed for a build are present.
:REQUIRE_BUILD_PREREQS
call :REQUIRE_PODMAN || exit /b 1
set "MISSING="
if not exist "Containerfile"        set "MISSING=!MISSING! Containerfile"
if not exist "install_languages.py" set "MISSING=!MISSING! install_languages.py"
if not exist "app\"                 set "MISSING=!MISSING! app\"
if defined MISSING (
    echo  EROARE: lipsesc fisiere necesare pentru build:!MISSING!
    echo  Ruleaza acest script din folderul proiectului.
    echo.
    pause
    exit /b 1
)
exit /b 0

:: Save the image to a .tar in the script directory. Sets EXPORT_OK=0/1.
:DO_EXPORT
set "EXPORT_OK=0"
podman image exists %IMAGE_NAME% 2>nul
if errorlevel 1 (
    echo  EROARE: imaginea %IMAGE_NAME% nu exista. Construieste-o intai (optiunea 1 sau 3).
    exit /b 1
)
echo  Se exporta %IMAGE_NAME% in %TAR_NAME% ...
echo  (Poate dura cateva minute)
echo.
podman save %IMAGE_NAME% -o "%CD%\%TAR_NAME%"
if errorlevel 1 (
    echo  EROARE: Exportul a esuat!
    exit /b 1
)
set "EXPORT_OK=1"
echo.
echo  SUCCES: Imaginea a fost exportata.
call :SHOW_TAR_INFO
echo.
echo  Copiaza pe stick USB catre statia offline:
echo    - %TAR_NAME%
echo    - traducator_manager.bat
echo    - TUTORIAL_DOCKER.md
exit /b 0

:: Print the .tar file size in a human-readable form (handles files over 2 GB).
:SHOW_TAR_INFO
if not exist "%TAR_NAME%" (
    echo  Fisierul .tar nu exista inca.
    exit /b 0
)
set "TAR_SIZE="
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$b=(Get-Item '%CD%\%TAR_NAME%').Length; if($b -ge 1073741824){'{0:N2} GB' -f ($b/1GB)}else{'{0:N1} MB' -f ($b/1MB)}" 2^>nul`) do set "TAR_SIZE=%%S"
if defined TAR_SIZE (
    echo  Fisier .tar: %TAR_NAME%  ^(!TAR_SIZE!^)
) else (
    for %%A in ("%TAR_NAME%") do echo  Fisier .tar: %TAR_NAME%  ^(%%~zA bytes^)
)
exit /b 0

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
