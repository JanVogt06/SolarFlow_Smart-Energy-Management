import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from solarflow import migrations
from solarflow.database import Database
from solarflow.migrations import MigrationContext

# So hat das alte DatabaseWriter-Modul die Tabellen angelegt
LEGACY_SCHEMA = """
CREATE TABLE solar_data (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
    pv_power REAL NOT NULL, grid_power REAL NOT NULL, battery_power REAL DEFAULT 0,
    load_power REAL NOT NULL, battery_soc REAL, feed_in_power REAL NOT NULL,
    grid_consumption REAL NOT NULL, self_consumption REAL NOT NULL, autarky_rate REAL NOT NULL,
    surplus_power REAL NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX idx_solar_timestamp ON solar_data (timestamp);
CREATE TABLE daily_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL UNIQUE,
    runtime_hours REAL NOT NULL, pv_energy REAL NOT NULL);
CREATE TABLE device_events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
    device_name TEXT NOT NULL, action TEXT NOT NULL, old_state TEXT, new_state TEXT NOT NULL,
    reason TEXT, surplus_power REAL, device_power REAL NOT NULL, priority INTEGER NOT NULL,
    runtime_today INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE device_status (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
    total_devices_on INTEGER NOT NULL DEFAULT 0, total_consumption REAL NOT NULL DEFAULT 0,
    surplus_power REAL NOT NULL DEFAULT 0, used_surplus REAL NOT NULL DEFAULT 0,
    device_states TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX idx_device_status_timestamp ON device_status (timestamp);
"""

SOLAR_HEADER = ("timestamp;pv_power;grid_power;battery_power;load_power;battery_soc;total_production;"
                "feed_in_power;grid_consumption;self_consumption;autarky_rate;surplus_power")
EVENT_HEADER = ("timestamp;device_name;action;from_state;to_state;reason;surplus_power;device_power;"
                "on_threshold;off_threshold;runtime_today;priority")


def legacy_solar(ts, pv, grid, battery, load, soc):
    return (ts, pv, grid, battery, load, soc, max(-grid, 0), max(grid, 0), load - max(grid, 0), 90.0, max(-grid, 0))


def status(heater: str, dryer: str) -> str:
    return json.dumps({"heizkörper_jan_state": heater, "heizkörper_jan_runtime": "10",
                       "entfeuchter_(neu)_state": dryer, "entfeuchter_(neu)_runtime": "5"})


@pytest.fixture
def legacy(tmp_path: Path):
    """Datenordner, wie ihn die Version vor dem Umbau hinterlassen hat."""
    logs = tmp_path / "Datalogs"
    for sub in ("Solardata", "Devicelogs", "Dailystats"):
        (logs / sub).mkdir(parents=True)

    conn = sqlite3.connect(logs / "solar_energy.db")
    conn.executescript(LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO solar_data (timestamp, pv_power, grid_power, battery_power, load_power, battery_soc, "
        "feed_in_power, grid_consumption, self_consumption, autarky_rate, surplus_power) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [legacy_solar("2026-01-10 12:00:00", 3000.0, -1000.0, -500.0, 1500.0, 60.0),
         legacy_solar("2026-01-10 12:00:05", 3010.4, -1010.0, -500.0, 1500.0, 60.1)])
    conn.execute("INSERT INTO daily_stats (date, runtime_hours, pv_energy) VALUES ('2026-01-10', 1, 0.3)")
    conn.execute("INSERT INTO device_events (timestamp, device_name, action, old_state, new_state, reason, "
                 "surplus_power, device_power, priority, runtime_today) VALUES "
                 "('2026-01-10 12:00:30', 'Heizkörper Jan', 'eingeschaltet', 'off', 'on', 'x', 2500, 2000, 1, 0)")
    conn.executemany(
        "INSERT INTO device_status (timestamp, surplus_power, device_states) VALUES (?, ?, ?)",
        [("2026-01-10 12:00:25", 2500, status("0", "0")),
         ("2026-01-10 12:00:35", 500, status("1", "0")),   # Heizung ein - Ereignis vorhanden
         ("2026-01-10 12:10:00", 700, status("1", "1")),   # Entfeuchter ein - kein Ereignis (Hue-App)
         ("2026-01-10 12:10:30", 700, status("1", "1")),   # letzter Snapshot vor der Lücke
         ("2026-01-10 18:00:00", 0, status("0", "0"))])    # beide aus nach Lücke - kein Ereignis
    conn.commit()
    conn.close()

    (logs / "Solardata" / "solar_data_20260110.csv").write_text("\n".join([
        SOLAR_HEADER,
        "# SOLAR Log",
        "# Erstellt: 2026-01-10 00:00:00",
        '"# CSV-Format: Delimiter=\';\', Encoding=\'utf-8\'"',
        "",
        "2026-01-10 12:00:00;3000;-1000;-500;1500;60,0;3000;1000;0;1500;100,0;1000",  # schon in der DB
        "2026-01-10 12:00:10;3020;-1020;-500;1500;60,1;3020;1020;0;1500;100,0;1020",  # fehlt in der DB
        "2026-01-10 12:00:15;0;+30;+200;230;-;200;0;30;200;87,0;0",                   # fehlt, ohne Akku
        "\0\0\0\0\0\0\0\0\0\0",                                                        # Absturzrest
    ]) + "\n", encoding="utf-8")
    (logs / "Devicelogs" / "device_events_20260110.csv").write_text("\n".join([
        EVENT_HEADER,
        "2026-01-10 12:00:30;Heizkörper Jan;eingeschaltet;off;on;x;2500;2000;2200;1800;0;1",  # doppelt
        "2026-01-10 13:00:00;Heizkörper Jan;ausgeschaltet;on;off;y;0;2000;2200;1800;60;1",   # fehlt
    ]) + "\n", encoding="utf-8")
    (logs / "Devicelogs" / "device_status_20260110.csv").write_text("Zeitstempel;x\n", encoding="utf-8")
    (logs / "Devicelogs" / "device_summary_20260110.txt").write_text("Zusammenfassung\n", encoding="utf-8")
    (logs / "Dailystats" / "daily_stats_20260110.csv").write_text("date;x\n", encoding="utf-8")
    (logs / "Dailystats" / ".DS_Store").write_bytes(b"\0")
    return tmp_path


def open_db(data_dir: Path, started_at: datetime) -> Database:
    legacy_db = data_dir / "Datalogs" / "solar_energy.db"
    db = Database(legacy_db if legacy_db.exists() else data_dir / "solarflow.db")
    db.open(MigrationContext(legacy_log_dir=data_dir / "Datalogs",
                             devices={"Heizkörper Jan": (2000, 1), "Entfeuchter (neu)": (200, 3)}),
            started_at)
    return db


def query(db: Database, sql: str):
    conn = sqlite3.connect(db.path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_legacy_data_is_merged_into_compact_database(legacy):
    db = open_db(legacy, datetime.now())

    assert db.path == legacy / "Datalogs" / "solar_energy.db"  # bleibt, wo sie war
    assert query(db, "PRAGMA user_version")[0][0] == migrations.MIGRATIONS[-1][0]
    assert query(db, "PRAGMA journal_mode")[0][0] == "wal"

    tables = {row[0] for row in query(db, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"device_status", "daily_stats"}.isdisjoint(tables)
    assert {"solar_data", "device_events", "hourly_energy", "legacy_files"} <= tables

    assert query(db, "SELECT * FROM solar_data ORDER BY timestamp") == [
        ("2026-01-10 12:00:00", 3000, -1000, -500, 1500, 60.0),
        ("2026-01-10 12:00:05", 3010, -1010, -500, 1500, 60.1),
        ("2026-01-10 12:00:10", 3020, -1020, -500, 1500, 60.1),
        ("2026-01-10 12:00:15", 0, 30, 200, 230, None),
    ]


def test_events_are_merged_and_completed_from_status_snapshots(legacy):
    db = open_db(legacy, datetime.now())
    events = [(e["timestamp"], e["device_name"], e["new_state"])
              for e in db.events(until=datetime(2026, 1, 11))]

    assert events == [
        ("2026-01-10 12:00:30", "Heizkörper Jan", "on"),      # bestand schon
        ("2026-01-10 12:10:00", "Entfeuchter (neu)", "on"),   # aus device_status nachgetragen
        # nachgetragen mit dem letzten Snapshot vor der Lücke, nicht dem Neustart um 18 Uhr
        ("2026-01-10 12:10:30", "Entfeuchter (neu)", "off"),
        ("2026-01-10 13:00:00", "Heizkörper Jan", "off"),     # aus der CSV; Heizung braucht nichts
    ]
    reconstructed = db.events(until=datetime(2026, 1, 11), since=datetime(2026, 1, 10, 12, 10))[0]
    assert reconstructed["device_power"] == 200
    assert "Statusprotokoll" in reconstructed["reason"]


def test_legacy_files_are_removed_only_after_a_confirmed_run(legacy):
    first_start = datetime.now()
    db = open_db(legacy, first_start)
    db.close()
    backups = list((legacy / "Datalogs").glob("solar_energy.db.v0.*.bak"))
    assert len(backups) == 1

    # Der erste Lauf stürzt ab, bevor er bestätigt wurde: nichts wird gelöscht
    db = open_db(legacy, first_start + timedelta(minutes=1))
    assert (legacy / "Datalogs" / "Solardata" / "solar_data_20260110.csv").exists()
    assert backups[0].exists()

    # Dieser Lauf läuft fehlerfrei und gibt frei - gelöscht wird erst beim nächsten Start
    assert db.confirm_legacy_cleanup() == 6
    assert (legacy / "Datalogs" / "Solardata" / "solar_data_20260110.csv").exists()
    db.close()

    db = open_db(legacy, datetime.now() + timedelta(minutes=2))
    assert sorted(p.name for p in (legacy / "Datalogs").iterdir() if not p.name.endswith(("-wal", "-shm"))) \
        == ["solar_energy.db"]
    assert not backups[0].exists()
    assert query(db, "SELECT COUNT(*) FROM legacy_files")[0][0] == 0
    assert query(db, "SELECT COUNT(*) FROM solar_data")[0][0] == 4


def test_unknown_files_keep_their_folder(legacy):
    (legacy / "Datalogs" / "notizen.md").write_text("meins")
    db = open_db(legacy, datetime.now())
    db.confirm_legacy_cleanup()
    db.close()
    open_db(legacy, datetime.now() + timedelta(seconds=1))

    assert (legacy / "Datalogs" / "notizen.md").exists()
    assert not (legacy / "Datalogs" / "Solardata").exists()


def test_fresh_install(tmp_path):
    db = open_db(tmp_path, datetime.now())
    assert db.path == tmp_path / "solarflow.db"
    assert query(db, "SELECT COUNT(*) FROM solar_data")[0][0] == 0
    assert list(tmp_path.glob("*.bak")) == []


def test_failed_migration_is_rolled_back(legacy, monkeypatch):
    def broken(conn, ctx):
        conn.execute("CREATE TABLE halb (x)")
        raise RuntimeError("kaputt")

    steps = list(migrations.MIGRATIONS)
    steps[3] = (4, "broken", broken)
    monkeypatch.setattr(migrations, "MIGRATIONS", steps)

    with pytest.raises(RuntimeError):
        open_db(legacy, datetime.now())

    db_path = legacy / "Datalogs" / "solar_energy.db"
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'halb'").fetchone()[0] == 0
    conn.close()

    monkeypatch.undo()
    db = open_db(legacy, datetime.now())
    assert query(db, "SELECT COUNT(*) FROM solar_data")[0][0] == 4


@pytest.fixture
def berlin(monkeypatch):
    import time as _time
    monkeypatch.setenv("TZ", "Europe/Berlin")
    _time.tzset()
    yield
    monkeypatch.undo()
    _time.tzset()


def test_utc_timestamps_of_the_old_docker_image_become_local_time(legacy, berlin):
    conn = sqlite3.connect(legacy / "Datalogs" / "solar_energy.db")
    conn.executemany(
        "INSERT INTO solar_data (timestamp, pv_power, grid_power, battery_power, load_power, battery_soc, "
        "feed_in_power, grid_consumption, self_consumption, autarky_rate, surplus_power) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [legacy_solar("2026-06-21 11:30:00", 8000.0, -5000.0, 0.0, 3000.0, 100.0),   # Sommerzeit +2
         legacy_solar("2026-10-25 00:30:00", 0.0, 100.0, 0.0, 100.0, 50.0),         # 02:30 Sommerzeit
         legacy_solar("2026-10-25 01:30:00", 0.0, 200.0, 0.0, 200.0, 50.0)])        # 02:30 Winterzeit
    conn.commit()
    conn.close()

    db = Database(legacy / "Datalogs" / "solar_energy.db")
    db.open(MigrationContext(legacy_log_dir=legacy / "Datalogs", legacy_timestamps_utc=True), datetime.now())

    rows = dict(query(db, "SELECT timestamp, grid_power FROM solar_data"))
    assert "2026-01-10 13:00:00" in rows and "2026-01-10 12:00:00" not in rows  # Winterzeit +1
    assert rows["2026-06-21 13:30:00"] == -5000
    assert rows["2026-10-25 02:30:00"] == 100  # doppelte Stunde: der erste Messwert bleibt
    assert db.events(until=datetime(2026, 1, 11))[0]["timestamp"] == "2026-01-10 13:00:30"


def test_local_timestamps_stay_untouched(legacy, berlin):
    db = open_db(legacy, datetime.now())
    assert query(db, "SELECT MIN(timestamp) FROM solar_data")[0][0] == "2026-01-10 12:00:00"


def test_existing_database_moves_out_of_datalogs(tmp_path):
    from solarflow.database import adopt_legacy_database

    (tmp_path / "Datalogs").mkdir()
    legacy = tmp_path / "Datalogs" / "solar_energy.db"
    sqlite3.connect(legacy).execute("CREATE TABLE x (y)").connection.close()
    Path(f"{legacy}-wal").write_bytes(b"")

    target = adopt_legacy_database(legacy, tmp_path / "solarflow.db")

    assert target == tmp_path / "solarflow.db" and target.exists()
    assert Path(f"{target}-wal").exists()
    assert list((tmp_path / "Datalogs").iterdir()) == []


def test_database_stays_when_it_cannot_move(tmp_path, monkeypatch):
    from solarflow import database

    (tmp_path / "Datalogs").mkdir()
    legacy = tmp_path / "Datalogs" / "solar_energy.db"
    legacy.write_bytes(b"x")

    def cross_device(*args):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(database.os, "replace", cross_device)
    assert database.adopt_legacy_database(legacy, tmp_path / "solarflow.db") == legacy
    assert legacy.exists()


def test_migrated_database_in_datalogs_moves_and_datalogs_disappears(legacy):
    """Der Weg einer Installation, die schon mit der Datenbank in Datalogs migriert wurde."""
    from solarflow.database import adopt_legacy_database

    db = open_db(legacy, datetime.now())  # migriert noch in Datalogs/solar_energy.db
    db.confirm_legacy_cleanup()
    db.close()

    target = adopt_legacy_database(legacy / "Datalogs" / "solar_energy.db", legacy / "solarflow.db")
    db = Database(target)
    db.open(MigrationContext(legacy_log_dir=legacy / "Datalogs"), datetime.now() + timedelta(minutes=1))

    assert not (legacy / "Datalogs").exists()
    assert query(db, "SELECT COUNT(*) FROM solar_data")[0][0] == 4
