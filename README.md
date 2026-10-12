# J.A.R.V.I.S. – dein persönlicher KI-Assistent für Windows 11

JARVIS läuft dauerhaft auf deinem PC, versteht Sprache („Hey Jarvis“), zeigt dir alles in
einem schönen Dashboard, erledigt Aufgaben selbstständig – und **programmiert sich auf
Wunsch selbst weiter**: Du sagst „Ich hätte gerne eine Druckfunktion“, JARVIS baut sie mit
Claude Code, testet sie, zeigt dir den Zwischenstand und übernimmt sie nach deiner
Freigabe.

**Alles ist kostenlos.** Es gibt keine kostenpflichtigen API-Keys:

| Wofür | Womit | Kosten | Was du brauchst |
|---|---|---|---|
| Denken, Programmieren, Selbstverbesserung | **Claude** über *Claude Code* | 0 € extra – läuft über dein **Claude-Pro-Abo** | einmal im Browser anmelden |
| schnelle Antworten, lange Texte | **Google Gemini** (kostenlose Stufe) | 0 € | kostenloser Key, 2 Minuten |
| private Daten, offline | **lokale Modelle** über Ollama | 0 € | optional, 2–20 GB Platz |
| Bilder erstellen | Pollinations | 0 € | nichts |
| Bilder & Web suchen, Wetter | Openverse, DuckDuckGo, Open-Meteo | 0 € | nichts |
| Gmail (mehrere Konten) & Kalender | Google-API | 0 € | einmalig ein Google-OAuth-Client, 10 Minuten |
| Spracherkennung / Sprachausgabe | Whisper (lokal) / Edge-Stimmen | 0 € | nichts |

Das globale Budget steht auf **0 $** – kostenpflichtige Modelle sind damit hart gesperrt.

---

## Was JARVIS kann

- **Dashboard** (öffnet sich automatisch, auch als eigenes Fenster): Begrüßung, Wetter,
  PC-Auslastung, Termine, ungelesene Mails pro Gmail-Konto, Erinnerungen, Projekte,
  Selbstverbesserungen, eigene Karten („Zeig mir dauerhaft den Bitcoin-Kurs“), Bildergalerie,
  Chat, Protokoll, Modelle und Einstellungen.
- **Sprache**: „Hey Jarvis“ (offline erkannt), Spracherkennung mit Whisper lokal auf dem PC,
  Antworten per natürlicher Stimme. Im Dashboard zusätzlich ein Mikrofon-Knopf;
  Push-to-Talk mit **Strg+Alt+Leertaste**.
- **Bilder**: „Zeig mir ein Bild von einem Fuchs im Schnee“ – JARVIS erstellt eins oder
  sucht passende Fotos im Internet und zeigt sie direkt im Chat und in der Galerie.
- **Werkzeuge**: Websuche und Webseiten lesen, Dateien suchen/lesen/schreiben/verschieben,
  Programme öffnen und schließen, PowerShell, Systemstatus, Wetter, Erinnerungen,
  Gedächtnis („Merk dir, dass …“), Gmail lesen/suchen/Entwürfe/senden (mehrere Konten),
  Kalender lesen/eintragen/löschen, Benachrichtigungen, Dashboard-Karten.
- **KI-Router**: wählt pro Schritt das passende Modell (Qualität, Tempo, Limits) und weicht
  automatisch aus, wenn eins ausfällt oder sein Kontingent aufgebraucht ist. Private Daten
  (Mails, Kalender, Dateien) gehen nur an Claude (Abo) oder lokale Modelle – nie an die
  kostenlose Gemini-Stufe.
- **Projekte**: „Starte ein Projekt: Plane meinen Umzug nach Hamburg“ – JARVIS erstellt
  einen Plan mit Meilensteinen, arbeitet im Hintergrund daran (auch über Tage, auch nach
  einem Neustart weiter), prüft Ergebnisse mit einem zweiten Modell, fragt dich bei
  Unklarheiten und macht inzwischen mit anderen Aufgaben weiter. Tagesbericht um 20 Uhr.
- **Selbstverbesserung** mit Claude Code (siehe unten) – auf Zuruf oder durch einen
  regelmäßigen Selbst-Check, der Verbesserungsvorschläge macht.
- **Immer aktiv**: startet mit Windows, Tray-Icon neben der Uhr, globale Tastenkürzel,
  startet sich nach Absturz oder Update selbst neu.

---

## Installation (einmalig, ca. 15 Minuten)

> Du brauchst: Windows 11 (10 geht auch), Internet und dein Claude-Pro-Konto.

**1. PowerShell öffnen** (Startmenü → „PowerShell“) und Python + Git installieren,
falls noch nicht vorhanden:

```powershell
winget install Python.Python.3.12
winget install Git.Git
```

Danach das PowerShell-Fenster **schließen und ein neues öffnen**.

**2. JARVIS herunterladen und installieren:**

```powershell
cd $HOME
git clone https://github.com/GamerNik0908/Jarvis.git
cd Jarvis
git checkout claude/jarvis-ai-assistant-7vy3ok   # solange die Version noch nicht in main ist
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

Das Skript installiert alle Pakete, bietet **Claude Code** und (optional) **Ollama** per
`winget` an, legt eine Verknüpfung **„JARVIS“** auf dem Desktop an und startet die geführte
Einrichtung. Einfach den Fragen folgen – jeder Schritt kann mit Enter übersprungen und
später im Dashboard unter **Einrichtung** nachgeholt werden.

> `-ExecutionPolicy Bypass` gilt nur für diesen einen Aufruf und ändert nichts am System.

**3. Fertig.** JARVIS startet, das Dashboard öffnet sich unter <http://127.0.0.1:8765>.
Ab jetzt startet JARVIS automatisch bei jeder Windows-Anmeldung (falls du das bei der
Einrichtung bestätigt hast; ändern mit `jarvis autostart on|off`).

---

## Was du holen musst – Checkliste

### ✅ Claude (dein Pro-Abo) – Pflicht

Dein Claude-Pro-Abo enthält **keinen API-Key** – den braucht JARVIS aber auch nicht. JARVIS
nutzt stattdessen **Claude Code**, das offizielle Programm von Anthropic, das mit dem
Pro-Abo ohne Zusatzkosten läuft. Einmalig anmelden:

1. Neues PowerShell-Fenster öffnen, `claude` eintippen, Enter.
2. Anmeldung mit deinem **Claude-Konto (Pro-Abo)** im Browser bestätigen.
3. Claude mit `/exit` beenden. Fertig – JARVIS erkennt die Anmeldung automatisch.

Hinweis: Claude Code und claude.ai teilen sich die Nutzungslimits des Pro-Abos (pro
5-Stunden-Fenster und pro Woche). Ist das Limit erreicht, weicht JARVIS automatisch auf
Gemini oder lokale Modelle aus und nimmt Claude danach wieder dazu.

### ✅ Gemini-Key (kostenlos) – empfohlen

1. <https://aistudio.google.com/apikey> öffnen, mit einem Google-Konto anmelden.
2. **„API-Schlüssel erstellen“** klicken (keine Kreditkarte nötig).
3. Den Schlüssel im Dashboard unter **Einrichtung → Gemini** einfügen und speichern.
   Er landet verschlüsselt im Windows Credential Manager.

Datenschutz: In der kostenlosen Stufe darf Google Eingaben zur Verbesserung seiner Dienste
nutzen. JARVIS schickt deshalb **keine privaten Daten** (Mails, Kalender, Dateien) an Gemini.

### ⭕ Gmail & Kalender – optional, ca. 10 Minuten

Google verlangt dafür einen eigenen, kostenlosen „OAuth-Client“. Einmalig:

1. <https://console.cloud.google.com/> öffnen → oben **Projekt auswählen → Neues Projekt**
   → Name „JARVIS“ → Erstellen.
2. **APIs & Dienste → Bibliothek**: „Gmail API“ suchen → **Aktivieren**. Ebenso
   „Google Calendar API“ → **Aktivieren**.
3. **APIs & Dienste → OAuth-Zustimmungsbildschirm** (bzw. „Google Auth Platform“):
   App-Name „JARVIS“, deine E-Mail als Support- und Entwickleradresse, Nutzertyp
   **Extern**. Unter **Zielgruppe / Testnutzer** alle Gmail-Adressen eintragen, die JARVIS
   nutzen soll.
4. **Wichtig:** Veröffentlichungsstatus auf **„In Produktion“** stellen (Button
   „App veröffentlichen“). Im Status „Test“ laufen die Anmeldungen sonst nach 7 Tagen ab.
   Eine Überprüfung durch Google ist für die private Nutzung nicht nötig.
5. **Clients / Anmeldedaten → Client erstellen → Anwendungstyp „Desktop-App“** → Erstellen
   → **JSON herunterladen**.
6. Die Datei umbenennen in `credentials.json` und hierhin kopieren:
   `%LOCALAPPDATA%\Jarvis\google\credentials.json`
   (in die Adresszeile des Explorers eintippen; Ordner ggf. anlegen).
7. Pro Gmail-Konto einmal ausführen: `.\scripts\jarvis.cmd google add`
   Im Browser das Konto wählen. Erscheint „Google hat diese App nicht überprüft“:
   **Erweitert → Weiter zu JARVIS** – es ist ja deine eigene App.

Weitere Konten jederzeit mit `jarvis google add`, Übersicht mit `jarvis google list`,
trennen mit `jarvis google remove <adresse>`. Der Kalender wird vom ersten Konto gelesen
(anderes Konto: `google.calendar_account` in den Einstellungen).

### ⭕ Ollama (lokale KI) – optional

Für komplett private Antworten ohne Internet: `winget install Ollama.Ollama`. JARVIS erkennt
deine Hardware und lädt passende Modelle im Dashboard unter **Einrichtung** per Knopfdruck.

---

## Bedienung

| Was | Wie |
|---|---|
| Dashboard öffnen | Desktop-Verknüpfung „JARVIS“, Tray-Icon oder **Strg+Alt+J** |
| Sprechen | „**Hey Jarvis**, …“ oder **Strg+Alt+Leertaste** gedrückt halten bzw. Mikrofon-Knopf im Chat |
| **Not-Aus** | **Strg+Alt+Umschalt+X**, roter Knopf im Dashboard oder Tray-Menü – stoppt sofort alle Aufgaben und Projekte |
| Rückfragen | Riskante Aktionen erscheinen als Fenster im Dashboard (und per Sprache: „ja“/„nein“) |
| Beenden | Tray-Menü → Beenden, oder `jarvis stop` |

Beispiele für Anfragen:

- „Wie wird das Wetter morgen?“ · „Was steht diese Woche in meinem Kalender?“
- „Habe ich wichtige ungelesene Mails?“ · „Schreib einen Entwurf an Max, dass ich später komme“
- „Zeig mir ein Bild vom Mars“ · „Such mir Fotos vom Eiffelturm bei Nacht“
- „Erinnere mich morgen um 9 an den Zahnarzt“ · „Merk dir, dass ich vegetarisch esse“
- „Öffne Spotify“ · „Wie voll ist meine Festplatte?“
- „Zeig mir dauerhaft eine Karte mit den Fußball-Ergebnissen auf der Übersicht“
- „Starte ein Projekt: Recherchiere die besten E-Bikes unter 2000 €“

---

## JARVIS bringt sich selbst Neues bei

Sag einfach (im Chat, per Sprache oder unter **Selbstverbesserung**):

> „Ich hätte gerne eine Druckoption für Chat-Antworten.“

Dann passiert Folgendes – du siehst jeden Schritt live im Dashboard:

1. JARVIS legt eine **Arbeitskopie** des eigenen Codes an (eigener Git-Branch – der
   laufende JARVIS bleibt unberührt).
2. **Claude Code** (über dein Pro-Abo) plant und programmiert die Änderung und schreibt
   Tests dazu.
3. Ein **Wächter** prüft, dass keine Sicherheits- oder Berechtigungsdateien angefasst
   wurden – das darf JARVIS grundsätzlich nicht.
4. Alle **automatischen Tests** laufen in der Arbeitskopie. Schlagen sie fehl, bessert
   Claude nach (mehrere Runden).
5. Du bekommst eine **Zusammenfassung mit allen Änderungen (Diff)** und klickst
   **Übernehmen** oder **Verwerfen**. (Optional: kleine, getestete Plugin-Änderungen
   automatisch übernehmen – Einstellungen → Selbstverbesserung.)
6. Nach dem Übernehmen startet JARVIS neu. Startet die neue Version nicht sauber, wird sie
   **automatisch zurückgerollt** und du bekommst eine Meldung. Jede Änderung steht im
   [CHANGELOG](CHANGELOG.md).

Zusätzlich prüft JARVIS **wöchentlich** (einstellbar: täglich/aus) selbst, was sich
verbessern lässt – z. B. wiederkehrende Fehler aus dem Protokoll – und schlägt es dir
auf der Übersicht vor. Umgesetzt wird nur, was du mit **Umsetzen** freigibst.

> **„Sandbox“?** Damit ist nur gemeint: Neue Änderungen werden erst in einer getrennten
> Kopie gebaut und getestet, bevor sie in den echten JARVIS kommen. Du musst dafür nichts
> einrichten.

---

## Sicherheit

- **Berechtigungsstufen**: Harmloses (Wetter, Suchen, Lesen) läuft automatisch.
  Riskantes – Löschen (immer in den Papierkorb), Überschreiben, Mails senden,
  Termine löschen, Programme beenden, PowerShell-Befehle mit Änderungen, Admin-Befehle –
  braucht deine Bestätigung. Systemordner (`C:\Windows`, `C:\Program Files` …) und
  Schlüsseldateien (`.env`, `*.pem`, Tokens) sind gesperrt. Pro Werkzeug einstellbar
  (`security.tool_levels`).
- **Not-Aus** per Tastenkürzel stoppt sofort alles, auch laufende Projekte und
  Selbstverbesserungen.
- **Keys** liegen im Windows Credential Manager (oder `.env`), nie im Code; in Logs werden
  sie automatisch geschwärzt.
- **Aktionsprotokoll**: Jede Aktion mit Zeit, Werkzeug, Entscheidung und Ergebnis
  (Dashboard → Protokoll).
- **Dashboard** nur auf `127.0.0.1` erreichbar, mit geheimem Zugriffsschlüssel.
- **Geschützter Code**: Sicherheits-, Berechtigungs- und Updatecode
  (`jarvis/security/`, Supervisor, Wächter) kann JARVIS selbst nicht ändern.

---

## Kommandozeile

Ohne Aktivierung der virtuellen Umgebung: `.\scripts\jarvis.cmd <befehl>`.

```text
jarvis                     JARVIS starten (Hintergrund) und Dashboard öffnen
jarvis stop | status       beenden / Status
jarvis window              Chat-Fenster öffnen
jarvis setup               geführte Einrichtung
jarvis chat                Chat im Terminal
jarvis ask "Frage"         eine Frage stellen
jarvis improve "Wunsch"    Selbstverbesserung starten
jarvis google add|list|remove
jarvis secrets set|delete|status
jarvis autostart on|off|status
jarvis models | cost | hw | check
```

## Konfiguration

Alle Einstellungen stehen kommentiert in [`config.yaml`](config.yaml). Das Dashboard
(**Einstellungen**) schreibt deine Änderungen in eine `config.local.yaml` daneben – die
überschreibt nur, was du änderst, und wird bei Updates nicht angefasst.

**Daten** liegen unter `%LOCALAPPDATA%\Jarvis` (getrennt vom Code): Datenbank
(`jarvis.db`), Logs, Bilder, Google-Zugänge, Arbeitskopien der Selbstverbesserung.

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| „Ausführung von Skripts ist deaktiviert“ | wie oben mit `powershell -ExecutionPolicy Bypass -File …` starten |
| „Claude Code ist nicht angemeldet“ | neues Terminal: `claude` → anmelden → `/exit`; dann Einrichtung → Neu prüfen |
| „Claude-Abo-Limit erreicht“ | normal bei viel Nutzung; JARVIS nutzt bis zum Reset Gemini/lokale Modelle |
| Gemini: „kein API-Key“ | Key unter Einrichtung einfügen (kostenlos, siehe oben) |
| Gmail: „OAuth-Client fehlt“ | `credentials.json` wie oben beschrieben ablegen, dann `jarvis google add` |
| Gmail-Anmeldung nach 7 Tagen weg | OAuth-App auf „In Produktion“ stellen (Schritt 4), Konto neu verbinden |
| „Hey Jarvis“ reagiert nicht | Mikrofon in Windows-Einstellungen → Datenschutz freigeben; Empfindlichkeit in den Einstellungen senken |
| Port 8765 belegt | läuft JARVIS schon? `jarvis status`; sonst `web.port` ändern |
| Nach einem Update startet JARVIS nicht | passiert nicht dauerhaft: der Supervisor rollt fehlerhafte Updates automatisch zurück. Logs: `%LOCALAPPDATA%\Jarvis\logs` |

## Entwicklung

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Dev
.\.venv\Scripts\python.exe -m pytest        # Tests (ohne Netzwerk, ohne Kosten)
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy          # strikte Typprüfung
```

Architektur: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Änderungen: [CHANGELOG.md](CHANGELOG.md)
