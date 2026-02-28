# Traducător Offline — Tutorial de Instalare și Utilizare

**Versiune: 4.0**

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

Acest script va executa automat doi pași:

1.  **Construirea imaginii `traducator-offline`**: Descarcă toate componentele necesare (Python, LibreTranslate, modele de limbă etc.) și le asamblează în imaginea containerului. Poate dura 5-20 de minute.
2.  **Exportarea imaginii într-un fișier `.tar`**: Salvează imaginea completă într-un singur fișier pentru transferul offline.

La final, veți avea un fișier numit `traducator-offline.tar` în același folder.

### 1.4 Copiere pe Stick USB

Copiați următoarele fișiere pe un stick USB sau hard disk extern pentru a le transfera pe stația offline:

-   `traducator-offline.tar` (fișierul mare, ~2-3 GB)
-   `traducator_manager.bat` (scriptul de management pentru stația offline)
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
-   `TUTORIAL_DOCKER.md`

### 2.3 Rulare Manager

Dublu-click pe **`traducator_manager.bat`**. Acesta este singurul script de care aveți nevoie pentru a gestiona aplicația.

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
3.  Scriptul va porni containerul. La final, vă va întreba dacă doriți să deschideți interfața web în browser.

### 3.2 Accesare Interfață

-   **Local:** http://localhost:80 (sau portul configurat)
-   **Din rețea:** http://NUME-COMPUTER:80 (sau adresa IP a stației)

### 3.3 Meniu Manager

-   **Opțiunea 3 (Verifica serviciile)**: Arată starea containerului și conectivitatea la LibreTranslate și Ollama.
-   **Opțiunea 4 (Opreste serviciile)**: Oprește și, opțional, șterge containerul.
-   **Opțiunea 5 (Loguri in timp real)**: Afișează logurile live ale containerului, util pentru depanare.
-   **Opțiunea 9 (Diagnosticare retea)**: Rulează o serie de teste de conectivitate între container și gazdă, esențial pentru a rezolva problemele cu Ollama.

### 3.4 Funcționalități Web

-   **Motor de traducere**: Puteți alege între `LibreTranslate` (rapid, CPU) și `Ollama` (calitate superioară, GPU) direct din interfață la încărcarea unui fișier.
-   **Formate suportate**: DOCX (cu păstrarea formatării), PDF (digital și scanat/OCR), TXT.
-   **Pagina de Loguri & Sistem**: Accesați `http://localhost:80/logs.html` pentru informații detaliate despre sistem, fișiere, și noua unealtă de **diagnosticare rețea**.

---

## Depanare Avansată

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

Această eroare apărea în versiunile anterioare ale scriptului din cauza caracterelor speciale din URL-uri procesate de batch cu `delayedexpansion`. Versiunea actuală a scriptului `traducator_manager.bat` rezolvă această problemă. Asigurați-vă că folosiți versiunea actualizată.

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
