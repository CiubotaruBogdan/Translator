# Traducător Offline — Tutorial de Instalare și Utilizare

Acest tutorial acoperă întregul proces de construire, export, transfer și rulare a soluției de traducere offline într-un container Podman pe o stație Windows 11. Procesul este simplificat folosind scripturile interactive `builder.bat` (pe stația online) și `traducator_manager.bat` (pe stația offline).

## Cuprins

1.  [Partea I — Pregătire pe stația ONLINE](#partea-i--pregătire-pe-stația-online)
2.  [Partea II — Instalare pe stația OFFLINE](#partea-ii--instalare-pe-stația-offline)
3.  [Partea III — Utilizare și Management](#partea-iii--utilizare-și-management)
4.  [Depanare Avansată](#depanare-avansată)

---

## Partea I — Pregătire pe stația ONLINE

Acești pași se execută pe o stație cu acces la internet pentru a construi pachetul de instalare.

### 1.1 Cerințe

- **Windows 10/11**
- **Podman Desktop**: Instalați de la [https://podman-desktop.io/downloads](https://podman-desktop.io/downloads). Acesta va instala și motorul de containere necesar.

### 1.2 Descărcare Pachet

Descărcați și dezarhivați pachetul `traducator-docker.zip` într-un folder, de exemplu `D:\traducator-docker\`.

### 1.3 Construire și Export

Pe stația online, rulați scriptul `builder.bat`.

1.  Navigați în folderul unde ați dezarhivat pachetul (`D:\traducator-docker\`).
2.  Dublu-click pe **`builder.bat`**.
3.  Selectați opțiunea **3. Construiește + Exportă**.

Acest script va executa automat trei pași:

1.  **Construirea imaginii `traducator-offline`**: Descarcă toate componentele necesare (Python, LibreTranslate, modele de limbă etc.) și le asamblează în imaginea containerului. Poate dura 5-20 de minute.
2.  **Verificare offline**: Sunt executate traduceri pentru perechile instalate într-un container fără rețea. Lipsa unui model sau cache oprește exportul.
3.  **Exportarea imaginii într-un fișier `.tar`**: Salvează imaginea completă într-un singur fișier pentru transferul offline.

La final, veți avea un fișier numit `traducator-offline.tar` în același folder.

### 1.4 Copiere pe Stick USB

Copiați următoarele fișiere pe un stick USB sau hard disk extern pentru a le transfera pe stația offline:

-   `traducator-offline.tar` (fișierul mare, ~2-3 GB)
-   `traducator_manager.bat` (scriptul de management pentru stația offline)
-   `configure_access.ps1` (configurarea locală a parolei)
-   `TUTORIAL_DOCKER.md` (acest ghid)

> **Notă:** Fișierele vechi precum `start.bat`, `stop.bat` sau `ollama_translator_activator.bat` **nu mai sunt necesare**.

---

## Partea II — Instalare pe stația OFFLINE

Acești pași se execută pe stația finală, fără acces la internet.

### 2.1 Cerințe

- **Windows 10/11**
- **Podman Desktop**: Trebuie instalat în prealabil (transferați kitul de instalare de pe stația online).
- **Ollama (Opțional, pentru traduceri GPU)**: Dacă doriți traduceri de calitate superioară folosind placa video, instalați Ollama de la [https://ollama.com](https://ollama.com).

### 2.2 Copiere Fisiere

Creați un folder permanent pe stația offline (ex: `C:\Traducator`) și copiați în el fișierele de pe stick-ul USB:

-   `traducator-offline.tar`
-   `traducator_manager.bat`
-   `configure_access.ps1`
-   `TUTORIAL_DOCKER.md`

### 2.3 Rulare Manager

Dublu-click pe **`traducator_manager.bat`**. Acesta este punctul de intrare pentru gestionarea aplicației; păstrați `configure_access.ps1` alături de el.

### 2.4 Încărcare Imagine

1.  În meniul managerului, selectați opțiunea **1. Incarca imaginea**.
2.  Scriptul va detecta automat fișierul `.tar` și vă va cere confirmarea.
3.  Așteptați finalizarea procesului. Poate dura câteva minute.

### 2.5 Configurare Ollama (Opțional)

Acest pas este **critic** dacă doriți să folosiți Ollama.

1.  Asigurați-vă că Ollama este instalat și pornit.
2.  În meniul managerului, selectați opțiunea **8. Configureaza Ollama**.
3.  Confirmați acțiunea. Scriptul va seta automat variabila de mediu `OLLAMA_HOST` la nivel de sistem, permițând containerului să comunice cu Ollama.
4.  **Reporniți Ollama** pentru ca setarea să fie aplicată (scriptul vă va oferi opțiunea de a face asta automat).

---

## Partea III — Utilizare și Management

Toate operațiunile se fac din meniul interactiv `traducator_manager.bat`.

### 3.1 Pornire Servicii

1.  Selectați opțiunea **2. Porneste serviciile**.
2.  Puteți lăsa portul și URL-ul Ollama la valorile implicite (apăsați Enter).
    -   **Port web**: Portul pe care va rula interfața web (implicit: 80).
    -   **Ollama URL**: Lăsați `auto` pentru detecție automată (recomandat).
3.  La prima pornire, helper-ul solicită mascat o **parolă comună de minimum 12 caractere**, cu confirmare. Utilizatorul din browser este **`translator`**. Parola este salvată local în `translator-password.txt` cu acces restrâns și introdusă în secretul Podman `traducator-password`. Nu distribuiți fișierul parolei pe stick sau împreună cu arhiva aplicației.
4.  Containerul folosește volumele persistente `traducator-uploads` și `traducator-outputs`. La următoarele porniri managerul reutilizează containerul și configurația; nu șterge documentele. Fișierele inactive sunt supuse retenției automate de 24 de ore, configurabilă prin `FILE_RETENTION_HOURS`.
5.  Managerul așteaptă disponibilitatea motorului selectat prin `/healthz`. Dacă inițializarea eșuează, consultați logurile; aplicația nu este declarată pregătită doar pentru că portul web răspunde.
6.  La final, puteți deschide browserul. Pentru containere vechi sau schimbarea imaginii folosiți procedura de migrare de mai jos.

### 3.2 Accesare Interfață

-   **Local:** http://localhost:80 (sau portul configurat)
-   **Din rețea:** http://NUME-COMPUTER:80 (sau adresa IP a stației)

Browserul solicită utilizatorul `translator` și parola comună. Toți utilizatorii autentificați au acces la același spațiu de documente. Pentru o rețea în care traficul poate fi interceptat, folosiți un reverse proxy HTTPS: HTTP Basic nu criptează parola sau documentele. API-ul și conexiunile WebSocket/SSE sunt protejate; numai starea minimă `/healthz` este publică.

### 3.3 Meniu Manager

-   **Opțiunea 3 (Verifica serviciile)**: Arată starea containerului și conectivitatea la LibreTranslate și Ollama.
-   **Opțiunea 4 (Opreste serviciile)**: Oprește containerul fără a șterge datele din volume.
-   **Opțiunea 5 (Loguri in timp real)**: Afișează logurile live ale containerului, util pentru depanare.
-   **Opțiunea 9 (Diagnosticare retea)**: Rulează o serie de teste de conectivitate între container și gazdă, esențial pentru a rezolva problemele cu Ollama.

### 3.4 Funcționalități Web

-   **Motor de traducere**: Puteți alege între `LibreTranslate` (rapid, CPU) și `Ollama` (calitate superioară, GPU) direct din interfață la încărcarea unui fișier.
-   **Formate suportate**: DOCX (cu păstrarea formatării), PDF (digital și scanat/OCR), TXT.
-   **Pagina de Loguri & Sistem**: Accesați `http://localhost:80/logs.html` pentru informații detaliate despre sistem, fișiere, și noua unealtă de **diagnosticare rețea**.

---

## Actualizare și migrare cu păstrarea documentelor

Încărcarea unei imagini noi nu înlocuiește imaginea unui container deja creat. Managerul refuză reutilizarea automată a containerelor vechi fără eticheta/configurația de securitate nouă. Acesta este un refuz preventiv; containerul vechi și fișierele lui sunt păstrate.

Pentru **migrarea unui container vechi**, executați în PowerShell din directorul aplicației, verificând succesul fiecărei comenzi înainte de a continua:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDir = Join-Path (Get-Location) "backup-$stamp"
New-Item -ItemType Directory -Path "$backupDir/uploads", "$backupDir/outputs"
podman stop traducator-offline
podman cp traducator-offline:/app/uploads/. "$backupDir/uploads"
podman cp traducator-offline:/app/outputs/. "$backupDir/outputs"
# Verificati ca fisierele dorite exista in backup inainte de redenumire.
podman rename traducator-offline "traducator-offline-backup-$stamp"
```

Păstrați acest backup privat. Nu îl includeți în pachetul distribuit. Porniți noua imagine prin manager, configurați parola, apoi opriți noul container înainte de a copia documentele:

```powershell
podman stop traducator-offline
podman cp "$backupDir/uploads/." traducator-offline:/app/uploads/
podman cp "$backupDir/outputs/." traducator-offline:/app/outputs/
podman start traducator-offline
```

Dacă volumele `traducator-uploads`/`traducator-outputs` conțin deja alte fișiere, faceți și backupul acestora și comparați numele înainte de copiere; nu suprascrieți rezultate existente. Documentele vechi pot fi descărcate din **Loguri & Sistem** după autentificare. Joburile și istoricul prompturilor din procesul vechi nu se restaurează, fiind stocate în memorie. Politica de retenție continuă să se aplice fișierelor copiate.

Pentru **upgrade între versiuni care folosesc deja volumele persistente**, faceți backup, opriți și redenumiți containerul curent, apoi creați noul container prin manager. Volumele și secretul existente sunt reutilizate; copierea fișierelor nu este necesară. Păstrați containerul anterior oprit pentru verificare/rollback și nu porniți simultan două containere care scriu în aceleași volume.

Pentru schimbarea parolei, recreați secretul și containerul într-o fereastră de mentenanță, după backup, folosind noul fișier protejat. Modificarea doar a fișierului local nu actualizează un secret deja importat în Podman. Nu eliminați volumele de documente la rotația parolei.

## Depanare Avansată

### Problema: acces refuzat sau HTTP 503

- Pentru HTTP 401, verificați utilizatorul `translator` și parola comună.
- Dacă accesul nu este configurat, verificați montarea secretului și `TRANSLATOR_PASSWORD_FILE` în configurația containerului. Parola nu este afișată în loguri.
- `/healthz` întoarce 503 dacă accesul sau motorul selectat nu sunt pregătite. Opțiunile 3 și 5 din manager ajută la diagnostic.
- Dacă o adresă de motor este respinsă, administratorul trebuie să o configureze la pornire în `TRANSLATOR_ALLOWED_ENGINE_URLS` sau ca adresă implicită. Sunt acceptate numai origini HTTP(S), fără cale, query, fragment sau credențiale.

### Problema: Ollama apare ca „Deconectat"

Aceasta este cea mai frecventă problemă. Containerul nu poate comunica cu Ollama de pe gazda Windows.

**Pași de rezolvare:**

1.  **Verificați că Ollama rulează**: Deschideți un terminal pe Windows și rulați `curl http://localhost:11434/api/version`. Dacă nu răspunde, porniți Ollama.
2.  **Configurați OLLAMA_HOST**: Rulați opțiunea **8** din `traducator_manager.bat`. Aceasta setează permanent variabila `OLLAMA_HOST=0.0.0.0:11434`, permițând Ollama să accepte conexiuni din container.
3.  **Reporniți Ollama**: După setarea variabilei, Ollama trebuie repornit.
4.  **Verificați firewall-ul**: Windows Firewall poate bloca portul 11434. Adăugați o regulă de intrare (Inbound Rule) pentru portul TCP 11434.
5.  **Diagnosticare avansată**: Folosiți opțiunea **9** din manager sau accesați pagina `http://localhost/logs.html` și apăsați butonul „Rulează diagnostic" din secțiunea de rețea.

### Problema: Containerul nu pornește

-   Verificați că imaginea a fost încărcată corect: `podman images traducator-offline`
-   Verificați logurile: opțiunea **5** din manager.
-   Asigurați-vă că portul ales nu este deja ocupat de altă aplicație.

### Problema: LibreTranslate nu răspunde

LibreTranslate are nevoie de 30-60 de secunde pentru a se inițializa la pornirea containerului. Așteptați și verificați din nou cu opțiunea **3**.

### Problema: Eroare „`. was unexpected at this time.`"

Această eroare era cauzată de caracterele speciale din URL-uri procesate de batch cu `delayedexpansion`. Scriptul actual `traducator_manager.bat` rezolvă această problemă. Asigurați-vă că folosiți scriptul din acest pachet.

### Acces shell în container (pentru depanare avansată)

```powershell
podman exec -it traducator-offline bash
```

Din interiorul containerului puteți rula:

```bash
# Test ping către gazdă
ping -c 1 host.containers.internal

# Test conectivitate Ollama
curl http://host.containers.internal:11434/api/version

# Verificare rute de rețea
ip route

# Test cu netcat
nc -zv host.containers.internal 11434
```
