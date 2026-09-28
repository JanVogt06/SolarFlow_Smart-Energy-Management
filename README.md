# SolarFlow Smart Energy Management

<p align="center">
  <img src="assets/logo_without-background.png" alt="SolarFlow Logo" width="512">
</p>

<p align="center">
  <strong>☀️ Intelligentes Energie-Management für Ihre Solaranlage</strong><br>
  <sub>Maximieren Sie Ihren Eigenverbrauch • Sparen Sie Stromkosten • Schonen Sie die Umwelt</sub>
</p>

<p align="center">
  <a href="https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement/releases/latest">
    <img src="https://img.shields.io/github/v/release/JanVogt06/SolarFlow-SmartEnergyManagement?style=for-the-badge&label=Download" alt="Download">
  </a>
  <a href="https://janvogt06.github.io/SolarFlow-SmartEnergyManagement/">
    <img src="https://img.shields.io/badge/Dokumentation-Website-blue?style=for-the-badge" alt="Dokumentation">
  </a>
  <a href="https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/JanVogt06/SolarFlow-SmartEnergyManagement?style=for-the-badge" alt="Lizenz">
  </a>
</p>

---

## 🎯 Was ist SolarFlow?

SolarFlow ist ein benutzerfreundliches Energie-Management-System für **Fronius Solaranlagen**. Es hilft Ihnen, Ihren selbst erzeugten Solarstrom optimal zu nutzen und dadurch Stromkosten zu sparen.

### Das macht SolarFlow für Sie:
- 📊 **Zeigt Ihre Solarproduktion in Echtzeit** im Browser
- 🔌 **Schaltet Philips-Hue-Steckdosen automatisch ein**, wenn genug Solarstrom da ist
- 💰 **Berechnet Ihre Ersparnis** – für Tag, Woche, Monat, Jahr und die gesamte Laufzeit
- 📱 **Funktioniert auf jedem Gerät** mit Webbrowser (PC, Tablet, Smartphone)

## 🚀 Schnellstart (5 Minuten)

### 1️⃣ Programm herunterladen

Laden Sie die passende Version für Ihr System herunter:

| System | Download | Hinweis |
|--------|----------|---------|
| **Windows** | [⬇️ SolarFlow-windows-x64.exe](https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement/releases/latest/download/SolarFlow-windows-x64.exe) | Doppelklick zum Starten |
| **macOS** | [⬇️ SolarFlow-macos-x64](https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement/releases/latest/download/SolarFlow-macos-x64) | Terminal: `chmod +x` dann starten |
| **Linux** | [⬇️ SolarFlow-linux-x64](https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement/releases/latest/download/SolarFlow-linux-x64) | Terminal: `chmod +x` dann starten |

### 2️⃣ Programm starten

**Windows:**
- Doppelklick auf `SolarFlow-windows-x64.exe`
- Falls Windows warnt: "Weitere Informationen" → "Trotzdem ausführen"

**macOS/Linux:**
```bash
# Datei ausführbar machen (nur beim ersten Mal)
chmod +x SolarFlow-*

# Programm starten
./SolarFlow-*
```

### 3️⃣ Dashboard öffnen

Im Browser **http://localhost:8000** aufrufen – von anderen Geräten im Netzwerk über die
Adresse des Rechners, zum Beispiel `http://192.168.1.20:8000`.

### 4️⃣ Wechselrichter und Hue Bridge eintragen

Im Tab **Einstellungen** die IP-Adresse des Fronius Wechselrichters und der Hue Bridge
eintragen und die Hue-Steuerung aktivieren. Beim ersten Verbinden den **Link-Button auf der
Hue Bridge drücken** – SolarFlow koppelt sich innerhalb weniger Sekunden und merkt sich den
Zugang.

Alternativ direkt beim Start:

```bash
python main.py --ip 192.168.178.90 --hue-ip 192.168.178.26
```

## 🐳 Mit Docker starten

Statt der Executable lässt sich SolarFlow auch als Container betreiben — praktisch für
einen Raspberry Pi, NAS oder Server, der ohnehin durchläuft. Der Quellcode wird dafür
nicht gebraucht, eine Datei genügt.

### Ohne Checkout, nur mit `docker pull`

`docker-compose.yml` in einem leeren Verzeichnis anlegen:

```yaml
services:
  solarflow:
    image: ghcr.io/janvogt06/solarflow:latest
    container_name: solarflow
    restart: unless-stopped
    # Zeit zum Ausschalten der Geräte beim Stoppen
    stop_grace_period: 30s
    ports:
      - "${SOLARFLOW_PORT:-8000}:8000"
    environment:
      - TZ=Europe/Berlin
      # IP-Adresse des Fronius Wechselrichters
      - FRONIUS_IP=192.168.178.90
      # Philips Hue
      - ENABLE_HUE=True
      - HUE_BRIDGE_IP=192.168.178.26
    volumes:
      # Einstellungen, Geräte, Datenbank, Log und Hue-Schlüssel
      - solarflow-data:/data

volumes:
  solarflow-data:
```

Dann:

```bash
docker compose pull && docker compose up -d
```

Danach ist das Dashboard unter http://localhost:8000 erreichbar, von anderen Geräten im
Netzwerk über die Adresse des Hosts, zum Beispiel `http://192.168.1.20:8000`.

Statt `latest` lässt sich eine Version festnageln (`:1.2.0`), wenn Sie selbst entscheiden
möchten, wann aktualisiert wird. Die `docker-compose.yml` in diesem Repository trägt
zusätzlich `build: .` für die lokale Entwicklung; auf einer Maschine ohne Quellcode ist
dieser Schlüssel nutzlos und `--build` würde fehlschlagen, dort also weglassen.

### Aus einem Checkout

```bash
docker compose up -d --build
```

### Port ändern

Ohne die Compose-Datei anzufassen:

```bash
SOLARFLOW_PORT=9000 docker compose up -d
```

Soll das Dashboard nur vom Host selbst erreichbar sein, das Port-Mapping auf
`"127.0.0.1:${SOLARFLOW_PORT:-8000}:8000"` ändern.

### Daten und Einstellungen

Alles Veränderliche liegt im Container unter `/data` und damit im Volume
`solarflow-data` — es übersteht Neustarts und Updates:

| Pfad | Inhalt |
| --- | --- |
| `/data/settings.json` | Im Dashboard geänderte Einstellungen |
| `/data/devices.json` | Gerätekonfiguration |
| `/data/solarflow.db` | Messwerte, Schaltvorgänge und Statistik (SQLite) |
| `/data/solar_monitor.log` | Logdatei (rotiert bei 5 MB, zwei alte Dateien bleiben) |
| `/data/.python_hue` | Zugangsschlüssel der Hue Bridge |

**Update von Version 1.x:** Die Datenbank bleibt, wo sie war (`Datalogs/solar_energy.db`).
Beim ersten Start übernimmt SolarFlow aus den CSV-Logs alle Messpunkte und Schaltvorgänge,
die in der Datenbank fehlen, und verkleinert sie – vorher wurde alles doppelt gespeichert.
Das dauert einmalig ein bis zwei Minuten; vorher wird daneben ein Backup
`solar_energy.db.v0.<Zeit>.bak` angelegt. Die übernommenen CSV-Dateien und das Backup
werden **beim darauffolgenden Start** gelöscht – also erst, wenn die migrierte Datenbank
einmal erfolgreich gelaufen ist.

Ein Blick hinein:

```bash
docker compose exec solarflow cat /data/settings.json
```

Fronius-IP, Hue-Bridge, Strompreise und Schwellwerte lassen sich auch nach dem Start im
Tab **Einstellungen** ändern; die Umgebungsvariablen der Compose-Datei sind nur die
Startwerte (siehe [Umgebungsvariablen](#umgebungsvariablen)).

Wer die frühere Compose-Datei mit den einzelnen Bind-Mounts (`./devices.json`,
`./settings.json`, …) benutzt hat, kopiert die vorhandenen Dateien einmalig ins Volume:

```bash
docker compose cp devices.json solarflow:/data/devices.json
docker compose cp settings.json solarflow:/data/settings.json
docker compose cp .python_hue solarflow:/data/.python_hue
docker compose restart
```

### Aktualisieren

```bash
docker compose pull && docker compose up -d
```

## 📦 Releases

Ein Tag, der mit `v` beginnt, baut ein Multi-Architektur-Image (`linux/amd64` und
`linux/arm64`), veröffentlicht es unter `ghcr.io/janvogt06/solarflow` als `<version>`,
`<major>.<minor>` und `latest`, erstellt die Executables für Windows, macOS und Linux
und legt daraus ein GitHub-Release an.

## 📸 So sieht's aus

### Web-Dashboard
![Dashboard Screenshot](assets/dashboard-screenshot.png)
*Modernes Web-Dashboard mit Live-Daten Ihrer Solaranlage*

### Terminal-Ansicht
![Live Display](assets/live-display-demo.png)
*Läuft SolarFlow in einem Terminal, gibt es dort eine Live-Ansicht*

## ✨ Hauptfunktionen

### 📊 Live-Monitoring
- **Echtzeitdaten** von Ihrem Fronius Wechselrichter: PV, Hausverbrauch, Netz und Akku
- **Tagesverlauf** mit Erzeugung und Verbrauch je Stunde

### 🔌 Automatische Gerätesteuerung mit Philips Hue
- **Einschalten bei Überschuss**, Ausschalten, sobald der Überschuss ohne das Gerät unter
  dessen Ausschalt-Schwellwert fiele
- **Prioritäten**: wichtige Geräte zuerst; bei Bedarf werden niedriger priorisierte
  Geräte zugunsten wichtigerer abgeschaltet
- **Zeitfenster, Mindest- und Maximallaufzeit, Wartezeit nach dem Ausschalten**
- **Akku zuerst**: Einschalten erst ab einem Mindest-Ladestand
- **Manueller Modus**: Wer ein Gerät im Dashboard oder in der Hue-App schaltet, pausiert
  dafür die Automatik (Standard: 30 Minuten)
- **Ehrlicher Status**: Ist die Bridge oder ein Gerät nicht erreichbar, wird nichts
  geschaltet und das Dashboard sagt, warum
- **Sicher beim Beenden**: Beim Stoppen und wenn der Wechselrichter länger keine Daten
  liefert, werden die automatisch gesteuerten Geräte ausgeschaltet
- **Laufzeiten überstehen Neustarts** – sie werden aus dem Schaltprotokoll wiederhergestellt

### 💰 Statistik und Kosten
- **Tag, Woche, Monat, Jahr und Gesamt** mit Blättern in die Vergangenheit
- **Erzeugung, Verbrauch, Eigenverbrauch, Netzbezug, Einspeisung**, Autarkie und
  Eigenverbrauchsquote
- **Ersparnis** gegenüber reinem Netzbezug inklusive Nachttarif und Einspeisevergütung
- **Laufzeit, Energie und Starts** jedes gesteuerten Geräts
- Alles wird aus den gespeicherten Messwerten berechnet – nichts setzt sich beim Neustart zurück

## ⚙️ Erweiterte Einstellungen

### Einstellungen im Browser

Im Tab **Einstellungen** lassen sich Fronius-IP, Hue-Bridge, Tarife und die Regeln der
Automatik ändern. Die Werte greifen sofort und landen in `settings.json`.

### Geräte konfigurieren

Geräte legen Sie im Tab **Geräte** an und bearbeiten sie dort. Der Name muss exakt dem
Namen des Geräts in der Hue-App entsprechen – das Formular bietet die gefundenen
Hue-Geräte zur Auswahl an. Gespeichert wird in `devices.json`:

```json
[
  {
    "name": "Heizkörper Wohnzimmer",
    "power_consumption": 2000,
    "priority": 2,
    "switch_on_threshold": 2200,
    "switch_off_threshold": 1800,
    "allowed_time_ranges": [["06:00", "22:00"]]
  }
]
```

### Kommandozeilen-Optionen

```bash
python main.py --ip 192.168.178.90       # Fronius Wechselrichter
python main.py --hue-ip 192.168.178.26   # Hue Bridge (aktiviert die Hue-Steuerung)
python main.py --interval 10             # Abfrage alle 10 Sekunden
python main.py --port 9000               # Dashboard auf Port 9000
python main.py --data-dir /pfad/zu/daten # Ablage für Einstellungen, Geräte und Datenbank
```

### Umgebungsvariablen

Kommandozeile schlägt im Dashboard gespeicherte Einstellungen, diese schlagen die Umgebung.

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `DATA_DIR` | `.` | Ordner für alle Daten |
| `API_PORT` | `8000` | Port des Dashboards |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `FRONIUS_IP` | `192.168.178.90` | Wechselrichter |
| `UPDATE_INTERVAL` | `5` | Abfrageintervall in Sekunden |
| `ENABLE_HUE` | `False` | Hue-Steuerung an/aus |
| `HUE_BRIDGE_IP` | `192.168.178.26` | Hue Bridge |
| `DEVICE_HYSTERESIS_MINUTES` | `5` | Wartezeit nach dem Ausschalten |
| `DEVICE_MANUAL_OVERRIDE_MINUTES` | `30` | Pause der Automatik nach Handschaltung |
| `DEVICE_MIN_BATTERY_SOC_ON` | `95` | Einschalten erst ab diesem Akkustand (%) |
| `DEVICE_MIN_BATTERY_SOC_OFF` | `20` | Ausschalten unter diesem Akkustand (%) |
| `ELECTRICITY_PRICE` | `0.40` | Strompreis €/kWh |
| `ELECTRICITY_PRICE_NIGHT` | `0.30` | Nachtpreis €/kWh |
| `NIGHT_TARIFF_START` / `NIGHT_TARIFF_END` | `22` / `6` | Nachttarif (volle Stunden) |
| `FEED_IN_TARIFF` | `0.082` | Einspeisevergütung €/kWh |

## 🔗 Nützliche Links

- 🌐 **Web-Dashboard**: http://localhost:8000 (nach dem Start)
- 📚 **API-Dokumentation**: http://localhost:8000/docs
- 🏠 **Projekt-Website**: [janvogt06.github.io/SolarFlow-SmartEnergyManagement](https://janvogt06.github.io/SolarFlow-SmartEnergyManagement/)
- 🐛 **Probleme melden**: [GitHub Issues](https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement/issues)

## 💡 Häufige Fragen

<details>
<summary><b>Wie finde ich die IP-Adresse meines Fronius Wechselrichters?</b></summary>

1. **Im Router nachschauen**: 
   - Router-Oberfläche öffnen (meist `192.168.1.1` oder `192.168.178.1`)
   - Nach "Verbundene Geräte" oder "DHCP-Clients" suchen
   - Nach "Fronius" oder "Solar" suchen

2. **Am Wechselrichter-Display**:
   - Menü → Einstellungen → Netzwerk → IP-Adresse

3. **Mit der Fronius Solar.web App**:
   - In der App ist die lokale IP sichtbar
</details>

<details>
<summary><b>Funktioniert SolarFlow mit meinem Wechselrichter?</b></summary>

SolarFlow funktioniert mit allen **Fronius Wechselrichtern**, die die Solar API unterstützen:
- Fronius Symo
- Fronius Primo  
- Fronius GEN24
- Fronius Tauro
- Und weitere...

Die Solar API ist bei den meisten Fronius Wechselrichtern ab Baujahr 2013 verfügbar.
</details>

<details>
<summary><b>Kann ich SolarFlow von unterwegs nutzen?</b></summary>

Standardmäßig läuft SolarFlow nur in Ihrem Heimnetzwerk. Für Zugriff von außen:
- VPN zu Ihrem Heimnetzwerk einrichten
- Oder Port-Weiterleitung im Router (Sicherheitsrisiko beachten!)
</details>

<details>
<summary><b>Was kostet SolarFlow?</b></summary>

**Nichts!** SolarFlow ist komplett kostenlos und Open Source. Sie können es beliebig nutzen und sogar den Quellcode anpassen.
</details>

## 🛠️ Für Entwickler

<details>
<summary><b>Von Quellcode ausführen</b></summary>

```bash
git clone https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement.git
cd SolarFlow-SmartEnergyManagement
pip install -r requirements.txt
python main.py --ip <FRONIUS_IP>
```

Läuft SolarFlow in einem Terminal, zeigt es dort zusätzlich eine Live-Ansicht.
</details>

<details>
<summary><b>Tests</b></summary>

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```
</details>

<details>
<summary><b>Eigene Builds erstellen</b></summary>

```bash
# PyInstaller installieren
pip install pyinstaller

# Executable erstellen
pyinstaller SolarFlow.spec --clean
```
</details>

## 🤝 Unterstützung & Beitrag

- **Probleme?** [Issue erstellen](https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement/issues/new)
- **Fragen?** [Discussions](https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement/discussions)
- **Verbesserungen?** Pull Requests sind willkommen!

## 📄 Lizenz

Dieses Projekt steht unter der MIT-Lizenz - siehe [LICENSE](LICENSE) für Details.

---

<p align="center">
  <b>⭐ Gefällt Ihnen SolarFlow?</b><br>
  Geben Sie dem Projekt einen Stern auf GitHub!<br><br>
  <sub>Made with ❤️ für nachhaltige Energienutzung</sub>
</p>