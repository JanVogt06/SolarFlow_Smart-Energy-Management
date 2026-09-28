"""
Schema-Migrationen für die SQLite-Datenbank.

Die Datenbank führt ihre Version in `PRAGMA user_version`. Jede Migration ist
eine nummerierte Funktion; beim Start werden alle Schritte angewendet, deren
Nummer größer als die aktuelle Version ist. Vor dem ersten Schritt eines Laufs
wird die Datenbank gesichert.

Neue Migration hinzufügen: Funktion schreiben und unten in MIGRATIONS mit der
nächsten freien Nummer eintragen. Bestehende Schritte werden nie geändert.

Aufgeräumt wird immer erst beim nächsten Start: die übernommenen CSV-Logs und
das Backup bleiben liegen, bis die migrierte Datenbank einmal erfolgreich
gelaufen ist (siehe `cleanup_legacy_files`).
"""

import bisect
import csv
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

LEGACY_SUBDIRS = ("Solardata", "Devicelogs", "Dailystats")

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@dataclass
class MigrationContext:
    """Was die Migrationen außerhalb der Datenbank brauchen."""

    # Ordner der alten CSV-Logs (früher DATA_LOG_DIR, Standard "Datalogs")
    legacy_log_dir: Optional[Path] = None
    # Gerätename -> (Leistung, Priorität) aus devices.json
    devices: Dict[str, Tuple[float, int]] = field(default_factory=dict)


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    """
    Führt mehrere Anweisungen aus, ohne die laufende Transaktion zu beenden.

    conn.executescript() würde vorher COMMIT absetzen und das rollback() im
    Runner wirkungslos machen.
    """
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


def _migration_001_baseline(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """
    Ausgangsschema: die Tabellen, die das alte Logging-System angelegt hat.
    Auf einer bestehenden Datenbank ändert dieser Schritt nichts.
    """
    _run_script(conn, """
        CREATE TABLE IF NOT EXISTS solar_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            pv_power REAL NOT NULL,
            grid_power REAL NOT NULL,
            battery_power REAL DEFAULT 0,
            load_power REAL NOT NULL,
            battery_soc REAL,
            feed_in_power REAL NOT NULL,
            grid_consumption REAL NOT NULL,
            self_consumption REAL NOT NULL,
            autarky_rate REAL NOT NULL,
            surplus_power REAL NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_solar_timestamp ON solar_data (timestamp);

        CREATE TABLE IF NOT EXISTS daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS device_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            device_name TEXT NOT NULL,
            action TEXT NOT NULL,
            old_state TEXT,
            new_state TEXT NOT NULL,
            reason TEXT,
            surplus_power REAL,
            device_power REAL NOT NULL,
            priority INTEGER NOT NULL,
            runtime_today INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS device_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            total_devices_on INTEGER NOT NULL DEFAULT 0,
            total_consumption REAL NOT NULL DEFAULT 0,
            surplus_power REAL NOT NULL DEFAULT 0,
            used_surplus REAL NOT NULL DEFAULT 0,
            device_states TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _migration_002_wal(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """WAL: Dashboard-Abfragen lesen, während der Monitor schreibt."""
    conn.execute("PRAGMA journal_mode = WAL")


def _migration_003_compact_solar_data(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """
    Baut solar_data auf die fünf Messwerte zurück.

    Einspeisung, Netzbezug, Eigenverbrauch, Autarkie und Überschuss lassen sich
    aus Netz- und Hausleistung jederzeit berechnen und wurden nur doppelt
    gespeichert. Der Zeitstempel wird Primärschlüssel: das spart den separaten
    Index und verhindert doppelte Messpunkte beim CSV-Import. Die Leistungen
    waren schon vorher auf ganze Watt gerundet.
    """
    _run_script(conn, """
        CREATE TABLE solar_data_new (
            timestamp TEXT PRIMARY KEY,
            pv_power INTEGER NOT NULL,
            grid_power INTEGER NOT NULL,
            battery_power INTEGER NOT NULL,
            load_power INTEGER NOT NULL,
            battery_soc REAL
        ) WITHOUT ROWID;

        INSERT OR IGNORE INTO solar_data_new
        SELECT substr(timestamp, 1, 19),
               CAST(ROUND(pv_power) AS INTEGER),
               CAST(ROUND(grid_power) AS INTEGER),
               CAST(ROUND(COALESCE(battery_power, 0)) AS INTEGER),
               CAST(ROUND(load_power) AS INTEGER),
               battery_soc
        FROM solar_data
        WHERE timestamp IS NOT NULL
        ORDER BY timestamp;

        DROP TABLE solar_data;
        ALTER TABLE solar_data_new RENAME TO solar_data
    """)


def _read_csv(path: Path) -> Iterator[Dict[str, str]]:
    """
    Liest eine alte Log-Datei als Dicts. Kommentar-, Leer- und kaputte Zeilen
    (nach einem Absturz stehen gelegentlich NUL-Bytes in der Datei) werden
    übersprungen.
    """
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        lines = (line.replace("\0", "") for line in f)
        header_line = next(lines, "")
        delimiter = next((d for d in (";", "\t", "|", ",") if d in header_line), ";")
        header = next(csv.reader([header_line], delimiter=delimiter), [])

        for row in csv.reader(lines, delimiter=delimiter):
            if not row or row[0].startswith("#") or len(row) != len(header):
                continue
            yield dict(zip(header, row))


def _number(raw: Optional[str]) -> Optional[float]:
    """Parst "1234", "+12", "-3", "60,5" und "60.5"; "-" und "" sind None."""
    if raw is None:
        return None
    value = raw.strip().replace(",", ".")
    if value in ("", "-"):
        return None
    return float(value)


# Das alte System schrieb die Spaltenschlüssel; deutsche Überschriften für den Fall,
# dass jemand die Datei von Hand neu erzeugt hat
_SOLAR_COLUMNS = {
    "timestamp": ("timestamp", "Zeitstempel"),
    "pv_power": ("pv_power", "PV-Erzeugung (W)"),
    "grid_power": ("grid_power", "Netz (W)"),
    "battery_power": ("battery_power", "Batterie (W)"),
    "load_power": ("load_power", "Hausverbrauch (W)"),
    "battery_soc": ("battery_soc", "Batterie-Stand (%)"),
}


def _solar_rows(path: Path) -> Iterator[tuple]:
    for row in _read_csv(path):
        values = {key: next((row[c] for c in names if c in row), None)
                  for key, names in _SOLAR_COLUMNS.items()}
        timestamp = (values["timestamp"] or "").strip()
        if not _TIMESTAMP.match(timestamp):
            continue
        try:
            powers = [_number(values[k]) for k in ("pv_power", "grid_power", "battery_power", "load_power")]
            soc = _number(values["battery_soc"])
        except ValueError:
            continue
        if powers[0] is None or powers[1] is None or powers[3] is None:
            continue
        yield (timestamp, *(round(p or 0) for p in powers), soc)


def _legacy_files(ctx: MigrationContext) -> List[Path]:
    if not ctx.legacy_log_dir or not ctx.legacy_log_dir.is_dir():
        return []
    files = []
    for sub in LEGACY_SUBDIRS:
        folder = ctx.legacy_log_dir / sub
        if folder.is_dir():
            files += sorted(p for p in folder.iterdir() if p.suffix in (".csv", ".txt"))
    return files


def _migration_004_import_legacy_csv(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """
    Holt aus den alten CSV-Logs alles in die Datenbank, was dort fehlt.

    Das alte System schrieb jeden Messwert doppelt - in CSV und Datenbank - und
    verlor bei jedem Stopp des Containers den ungeschriebenen Puffer, in der
    Datenbank mehr als in den CSV-Dateien. Hier werden beide zusammengeführt:
    bereits vorhandene Messpunkte bleiben, fehlende kommen dazu.

    Tagesstatistiken, Status-Snapshots und Tageszusammenfassungen werden nicht
    übernommen - sie lassen sich aus Messwerten und Schaltereignissen neu
    berechnen. Alle Dateien landen in legacy_files und werden beim nächsten
    Start gelöscht.
    """
    conn.execute("""
        CREATE TABLE legacy_files (
            path TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            rows_read INTEGER NOT NULL DEFAULT 0,
            rows_added INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL
        )
    """)

    known_events: Set[tuple] = set(conn.execute(
        "SELECT timestamp, device_name, new_state FROM device_events"
    ))
    imported_at = datetime.now().isoformat()
    totals = {"solar": [0, 0], "events": [0, 0]}

    for path in _legacy_files(ctx):
        name = path.name
        read = added = 0

        if name.startswith("solar_data_") and path.suffix == ".csv":
            kind = "solar"
            for row in _solar_rows(path):
                read += 1
                added += conn.execute(
                    "INSERT OR IGNORE INTO solar_data VALUES (?, ?, ?, ?, ?, ?)", row
                ).rowcount

        elif name.startswith("device_events_") and path.suffix == ".csv":
            kind = "events"
            for row in _read_csv(path):
                timestamp = row.get("timestamp", "").strip()
                if not _TIMESTAMP.match(timestamp) or not row.get("device_name"):
                    continue
                read += 1
                key = (timestamp, row["device_name"], row.get("to_state"))
                if key in known_events:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO device_events (timestamp, device_name, action, old_state, new_state, "
                        "reason, surplus_power, device_power, priority, runtime_today) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (timestamp, row["device_name"], row.get("action", ""), row.get("from_state"),
                         row.get("to_state"), row.get("reason"), _number(row.get("surplus_power")),
                         _number(row.get("device_power")) or 0, int(_number(row.get("priority")) or 5),
                         _number(row.get("runtime_today")))
                    )
                except ValueError:
                    continue
                known_events.add(key)
                added += 1
        else:
            kind = "superseded"

        if kind in totals:
            totals[kind][0] += read
            totals[kind][1] += added
        conn.execute("INSERT INTO legacy_files VALUES (?, ?, ?, ?, ?)",
                     (str(path.resolve()), kind, read, added, imported_at))

    logger.info(
        f"CSV-Übernahme: {totals['solar'][1]} von {totals['solar'][0]} Messpunkten und "
        f"{totals['events'][1]} von {totals['events'][0]} Schaltereignissen fehlten in der Datenbank"
    )


def _status_key(name: str) -> str:
    """So hat das alte Status-Logging Gerätenamen in JSON-Schlüssel verwandelt."""
    return name.lower().replace(" ", "_")


def _migration_005_events_from_status(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """
    Löst die Tabelle device_status auf.

    Sie hielt alle paar Sekunden den Zustand jedes Geräts fest - über 1 GB, das
    nie jemand gelesen hat. Die Schaltereignisse in device_events tragen
    dieselbe Information, nur fehlten dort externe Schaltungen (Hue-App) und
    Zustandswechsel beim Neustart. Genau diese Übergänge werden hier aus den
    Snapshots nachgetragen, danach wird die Tabelle gelöscht.

    Ebenfalls gelöscht wird daily_stats: das alte System hat die Tageswerte bei
    jedem Neustart falsch aufaddiert. Die Statistik wird ab jetzt aus den
    Messwerten berechnet.
    """
    events: Dict[Tuple[str, bool], List[datetime]] = {}
    last_known: Dict[str, Tuple[float, int]] = dict(ctx.devices)
    names: Dict[str, str] = {_status_key(n): n for n in ctx.devices}

    for timestamp, name, new_state, power, priority in conn.execute(
            "SELECT timestamp, device_name, new_state, device_power, priority "
            "FROM device_events ORDER BY timestamp"):
        names.setdefault(_status_key(name), name)
        last_known[name] = (power, priority)
        try:
            events.setdefault((name, new_state == "on"), []).append(datetime.fromisoformat(timestamp))
        except ValueError:
            continue

    def has_event(name: str, on: bool, since: datetime, until: datetime) -> bool:
        stamps = events.get((name, on), [])
        margin = timedelta(seconds=90)
        i = bisect.bisect_left(stamps, since - margin)
        return i < len(stamps) and stamps[i] <= until + margin

    state: Dict[str, Tuple[bool, datetime]] = {}
    added = 0
    rows = conn.execute("SELECT timestamp, surplus_power, device_states FROM device_status ORDER BY timestamp")
    for timestamp, surplus, states_json in rows:
        try:
            now = datetime.fromisoformat(timestamp)
            states = json.loads(states_json or "{}")
        except ValueError:
            continue

        for key, value in states.items():
            if not key.endswith("_state"):
                continue
            device_key = key[:-len("_state")]
            on = value == "1"
            previous = state.get(device_key)
            state[device_key] = (on, now)
            if previous is None and not on:
                continue
            if previous is not None and previous[0] == on:
                continue

            name = names.get(device_key) or device_key.replace("_", " ").capitalize()
            since = previous[1] if previous else now
            if has_event(name, on, since, now):
                continue

            power, priority = last_known.get(name, (0.0, 5))
            runtime = states.get(f"{device_key}_runtime")
            conn.execute(
                "INSERT INTO device_events (timestamp, device_name, action, old_state, new_state, "
                "reason, surplus_power, device_power, priority, runtime_today) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (timestamp, name, "eingeschaltet" if on else "ausgeschaltet",
                 None if previous is None else ("on" if previous[0] else "off"),
                 "on" if on else "off",
                 "Nachgetragen aus dem Statusprotokoll (extern oder beim Neustart geschaltet)",
                 surplus, power, priority, _number(runtime) if isinstance(runtime, str) else None)
            )
            added += 1

    logger.info(f"{added} Schaltvorgänge aus device_status nachgetragen")

    _run_script(conn, """
        DROP TABLE device_status;
        DROP TABLE daily_stats;
        CREATE INDEX idx_device_events_timestamp ON device_events (timestamp)
    """)


def _migration_006_hourly_energy(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """
    Stundenwerte als Cache für die Statistik.

    Die Rohdaten sind die Quelle der Wahrheit; eine Abfrage über ein ganzes Jahr
    Messpunkte dauert aber Sekunden. Abgeschlossene Stunden werden deshalb
    einmal verdichtet (Database.refresh_hourly) - der Tarif lässt sich damit
    weiterhin stundengenau zuordnen.
    """
    conn.execute("""
        CREATE TABLE hourly_energy (
            hour TEXT PRIMARY KEY,
            samples INTEGER NOT NULL,
            seconds REAL NOT NULL,
            pv_wh REAL NOT NULL,
            load_wh REAL NOT NULL,
            feed_in_wh REAL NOT NULL,
            grid_wh REAL NOT NULL,
            battery_charge_wh REAL NOT NULL,
            battery_discharge_wh REAL NOT NULL,
            self_consumption_wh REAL NOT NULL,
            pv_max INTEGER,
            load_max INTEGER,
            soc_min REAL,
            soc_max REAL
        ) WITHOUT ROWID
    """)


Migration = Callable[[sqlite3.Connection, MigrationContext], None]

MIGRATIONS: List[Tuple[int, str, Migration]] = [
    (1, "baseline schema", _migration_001_baseline),
    (2, "wal journal mode", _migration_002_wal),
    (3, "compact solar_data to the measured values", _migration_003_compact_solar_data),
    (4, "merge legacy csv logs", _migration_004_import_legacy_csv),
    (5, "replace device_status snapshots by events", _migration_005_events_from_status),
    (6, "hourly energy cache", _migration_006_hourly_energy),
]


def backup_database(conn: sqlite3.Connection, db_path: Path, from_version: int) -> Optional[Path]:
    """
    Legt eine Kopie neben die Datenbank, bevor migriert wird.

    Über die Backup-API statt per Dateikopie, damit auch noch nicht
    zurückgeschriebene Inhalte der WAL-Datei mitgesichert werden.
    """
    if from_version == 0 and not conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
        return None  # frische Datenbank - nichts zu sichern

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.v{from_version}.{stamp}.bak")
    target = sqlite3.connect(backup_path)
    try:
        conn.backup(target)
    finally:
        target.close()

    logger.info(f"Backup vor der Migration angelegt: {backup_path}")
    return backup_path


def apply_migrations(conn: sqlite3.Connection, db_path: Path, ctx: MigrationContext) -> int:
    """
    Bringt die Datenbank auf den neuesten Stand.

    Jeder Schritt läuft in einer eigenen Transaktion und setzt danach
    user_version. Scheitert ein Schritt, wird er vollständig zurückgerollt und
    die Exception weitergereicht - halb migriert startet die Anwendung nicht,
    ein erneuter Start versucht den Schritt noch einmal.

    Returns:
        Die Version nach dem Lauf
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    target = MIGRATIONS[-1][0]
    if current >= target:
        return current

    logger.info(f"Migriere Datenbank von Version {current} auf {target} - das kann einige Minuten dauern")
    backup_database(conn, db_path, current)

    for version, description, migrate in MIGRATIONS:
        if version <= current:
            continue

        logger.info(f"Migration {version}: {description}")
        # journal_mode lässt sich nicht innerhalb einer Transaktion umstellen
        transactional = migrate is not _migration_002_wal
        try:
            if transactional:
                conn.execute("BEGIN")
            migrate(conn, ctx)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        except Exception:
            conn.rollback()
            logger.error(f"Migration {version} ({description}) fehlgeschlagen - Abbruch")
            raise
        current = version

    _reclaim_space(conn)
    logger.info(f"Datenbank auf Version {current}")
    return current


def _reclaim_space(conn: sqlite3.Connection) -> None:
    """Gibt den Platz gelöschter Tabellen an das Dateisystem zurück."""
    free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    if free_pages * page_size < 16 * 1024 * 1024:
        return

    logger.info(f"Gebe {free_pages * page_size / 1024 / 1024:.0f} MB ungenutzten Platz frei (VACUUM)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def cleanup_legacy_files(conn: sqlite3.Connection, db_path: Path, legacy_log_dir: Optional[Path],
                         started_at: datetime) -> None:
    """
    Löscht übernommene CSV-Logs und Backups aus einem früheren Start.

    Was in diesem Lauf importiert oder gesichert wurde, bleibt liegen: erst
    wenn die migrierte Datenbank einen Start überstanden hat, ist die alte
    Ablage entbehrlich.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'legacy_files'").fetchone():
        cutoff = started_at.isoformat()
        stale = conn.execute("SELECT path FROM legacy_files WHERE imported_at < ?", (cutoff,)).fetchall()
        for (path,) in stale:
            Path(path).unlink(missing_ok=True)
        if stale:
            conn.execute("DELETE FROM legacy_files WHERE imported_at < ?", (cutoff,))
            conn.commit()
            logger.info(f"{len(stale)} übernommene CSV-Logs gelöscht")

    if legacy_log_dir and legacy_log_dir.is_dir():
        for folder in [legacy_log_dir / sub for sub in LEGACY_SUBDIRS] + [legacy_log_dir]:
            _remove_if_empty(folder)

    for backup in db_path.parent.glob(f"{db_path.name}.v*.bak"):
        if datetime.fromtimestamp(backup.stat().st_mtime) < started_at:
            backup.unlink(missing_ok=True)
            logger.info(f"Backup {backup.name} gelöscht - die migrierte Datenbank läuft")


def _remove_if_empty(folder: Path) -> None:
    """Entfernt einen Ordner, in dem höchstens noch Finder-Metadaten liegen."""
    if not folder.is_dir():
        return
    entries = list(folder.iterdir())
    if any(entry.name != ".DS_Store" for entry in entries):
        return
    for entry in entries:
        entry.unlink(missing_ok=True)
    folder.rmdir()
