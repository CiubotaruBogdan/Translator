# Traducător Offline — Tutorial Docker/Podman

Acest tutorial acoperă întregul proces de construire, export, transfer și rulare a soluției de traducere offline într-un container Docker/Podman pe o stație Windows 11.

## Cuprins

1. [Partea I — Pregătire pe stația ONLINE](#partea-i--pregătire-pe-stația-online)
2. [Partea II — Instalare pe stația OFFLINE](#partea-ii--instalare-pe-stația-offline)
3. [Partea III — Utilizare](#partea-iii--utilizare)
4. [Depanare](#depanare)

---

## Partea I — Pregătire pe stația ONLINE

### 1.1 Instalare Podman Desktop

Pe stația online, descărcați și instalați **Podman Desktop** de la [https://podman-desktop.io/downloads](https://podman-desktop.io/downloads). Acesta oferă un motor de containere compatibil Docker și o interfață grafică.

### 1.2 Descărcare pachet

Descărcați și dezarhivați pachetul `traducator-docker.zip` într-un folder, de exemplu `D:\traducator-docker\`.

### 1.3 Construire imagine container

Deschideți un terminal (Command Prompt sau PowerShell), navigați în folderul `D:\traducator-docker\` și rulați:

```powershell
.\build.bat
```

Acest script va construi imaginea `traducator-offline` folosind `Containerfile`. Procesul poate dura 5-15 minute la prima rulare, deoarece descarcă imaginea de bază Python și pachetele de limbi.

### 1.4 Export imagine container

După ce imaginea a fost construită, exportați-o într-un fișier `.tar` pentru transfer offline:

```powershell
.\export.bat
```

Acest script va crea fișierul `traducator-offline.tar` în același folder. Fișierul va avea ~1-2 GB.

### 1.5 Copiere pe stick USB

Copiați fișierul `traducator-offline.tar` pe un stick USB sau hard extern.

---

## Partea II — Instalare pe stația OFFLINE

### 2.1 Instalare Podman Desktop

Pe stația offline, instalați Podman Desktop (descărcat în prealabil). Acesta va instala și motorul de containere necesar.

### 2.2 Instalare Ollama (opțional)

Dacă doriți să folosiți și Ollama (pentru calitate superioară), instalați-l separat pe stația Windows. Asigurați-vă că este configurat să asculte pe rețea:

```powershell
setx OLLAMA_HOST "0.0.0.0:11434"
```

### 2.3 Import imagine container

Copiați `traducator-offline.tar` de pe stick USB pe stația offline. Deschideți un terminal și rulați:

```powershell
podman load -i C:\cale\catre\traducator-offline.tar
```

Acest proces va încărca imaginea în Podman. Verificați cu `podman images` — ar trebui să vedeți `traducator-offline` în listă.

### 2.4 Copiere script-uri de start

Copiați `start.bat` și `stop.bat` de pe stick USB într-o locație permanentă pe stația offline.

---

## Partea III — Utilizare

### 3.1 Pornire container

Dublu-click pe `start.bat`. Acest script va porni containerul și va deschide automat un browser la adresa corectă. Implicit, aplicația va rula pe portul **8080**.

**Configurare port și Ollama:**

- **Port personalizat:** `start.bat 9090`
- **URL Ollama personalizat:** `start.bat 8080 http://192.168.1.100:11434`

### 3.2 Accesare interfață

- **Local:** http://localhost:8080 (sau portul configurat)
- **Rețea:** http://IP-STATIE:8080

### 3.3 Funcționalități

- **LibreTranslate** este pre-instalat în container cu limbile română, engleză și franceză.
- **Ollama** este accesat extern (configurabil din interfață sau prin variabila de mediu `OLLAMA_URL`).
- **Curățare automată:** Toate fișierele încărcate și traduse sunt șterse automat în fiecare noapte la miezul nopții.

### 3.4 Oprire container

Dublu-click pe `stop.bat` pentru a opri containerul.

---

## Depanare

- **Containerul nu pornește:** Verificați dacă imaginea a fost încărcată corect (`podman images`).
- **Nu mă pot conecta la Ollama:** Asigurați-vă că `OLLAMA_HOST` este setat corect pe mașina gazdă și că URL-ul din setările aplicației este corect (ex: `http://192.168.1.100:11434`).
- **Vizualizare log-uri:** `podman logs -f traducator-offline`
