"""
SQLite-Datenbank: Messwerte, Schaltereignisse und Stundenwerte.

Geschrieben wird nur vom Monitor-Thread über eine eigene Verbindung; jede
Leseabfrage öffnet eine kurze eigene Verbindung. Im WAL-Modus blockieren sich
beide nicht.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .fronius import SolarData
from .migrations import MigrationContext, apply_migrations, cleanup_legacy_files

logger = logging.getLogger(__name__)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

HOURLY_COLUMNS = (
    "hour", "samples", "seconds", "pv_wh", "load_wh", "feed_in_wh", "grid_wh",
    "battery_charge_wh", "battery_discharge_wh", "self_consumption_wh",
    "pv_max", "load_max", "soc_min", "soc_max",
)

# Energie je Stunde: jeder Messwert gilt bis zum nächsten. Ist der Abstand
# größer als :max_gap, fehlen Daten (Programm aus, Wechselrichter weg) - dann
# zählt der Messwert nur ein normales Abfrageintervall. Die Abfrage liest bis
# :lookahead weiter, damit auch der letzte Messwert einer Stunde seinen
# Nachfolger kennt.
_HOURLY_SQL = """
WITH g AS (
    SELECT timestamp, pv_power AS pv, load_power AS load, grid_power AS grid,
           battery_power AS bat, battery_soc AS soc,
           (julianday(LEAD(timestamp) OVER (ORDER BY timestamp)) - julianday(timestamp)) * 86400.0 AS gap
    FROM solar_data
    WHERE timestamp >= :start AND timestamp < :lookahead
), s AS (
    SELECT *, CASE WHEN gap IS NULL OR gap > :max_gap THEN :nominal ELSE gap END AS dt FROM g
)
SELECT substr(timestamp, 1, 13) || ':00:00' AS hour,
       COUNT(*),
       SUM(dt),
       SUM(pv * dt) / 3600.0,
       SUM(load * dt) / 3600.0,
       SUM(MAX(-grid, 0) * dt) / 3600.0,
       SUM(MAX(grid, 0) * dt) / 3600.0,
       SUM(MAX(-bat, 0) * dt) / 3600.0,
       SUM(MAX(bat, 0) * dt) / 3600.0,
       SUM(MIN(MAX(load - MAX(grid, 0), 0), load) * dt) / 3600.0,
       MAX(pv), MAX(load), MIN(soc), MAX(soc)
FROM s
WHERE timestamp < :end
GROUP BY hour
ORDER BY hour
"""


def _format(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)


def _hour_floor(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


class Database:
    """Zugriff auf solarflow.db."""

    def __init__(self, path: Path, update_interval: int = 5):
        self.path = path
        self.update_interval = update_interval
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    @property
    def max_gap(self) -> float:
        """Längste Zeit, die ein einzelner Messwert gelten darf."""
        return max(60.0, 3.0 * self.update_interval)

    def open(self, ctx: MigrationContext, started_at: datetime,
             legacy_db: Optional[Path] = None) -> None:
        """
        Öffnet die Datenbank, migriert sie und räumt Altlasten früherer Starts weg.

        Args:
            ctx: Kontext für die Migrationen
            started_at: Startzeit dieses Programmlaufs
            legacy_db: Frühere Datenbankdatei, die hierher umzieht
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if legacy_db is not None:
            self._adopt_legacy_database(legacy_db)

        self._conn = self._connect()
        apply_migrations(self._conn, self.path, ctx)
        cleanup_legacy_files(self._conn, self.path, ctx.legacy_log_dir, started_at)

    def _adopt_legacy_database(self, legacy_db: Path) -> None:
        """Zieht Datalogs/solar_energy.db samt WAL-Dateien an den neuen Ort um."""
        if self.path.exists() or not legacy_db.is_file():
            return
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{legacy_db}{suffix}")
            if source.exists():
                os.replace(source, f"{self.path}{suffix}")
        logger.info(f"Datenbank von {legacy_db} nach {self.path} verschoben")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute(sql, params)
            self._conn.commit()

    def _read(self, sql: str, params: Any = ()) -> List[tuple]:
        conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # --- Messwerte ---------------------------------------------------------

    def insert_sample(self, data: SolarData) -> None:
        self._write(
            "INSERT OR REPLACE INTO solar_data VALUES (?, ?, ?, ?, ?, ?)",
            (_format(data.timestamp), round(data.pv_power), round(data.grid_power),
             round(data.battery_power), round(data.load_power),
             None if data.battery_soc is None else round(data.battery_soc, 1))
        )

    def first_sample_time(self) -> Optional[datetime]:
        rows = self._read("SELECT MIN(timestamp) FROM solar_data")
        return datetime.strptime(rows[0][0], TIME_FORMAT) if rows and rows[0][0] else None

    # --- Stundenwerte ------------------------------------------------------

    def _aggregate(self, start: datetime, end: datetime, conn: Optional[sqlite3.Connection] = None) -> List[tuple]:
        params = {
            "start": _format(start),
            "end": _format(end),
            "lookahead": _format(end + timedelta(seconds=self.max_gap)),
            "nominal": float(self.update_interval),
            "max_gap": self.max_gap,
        }
        if conn is not None:
            return conn.execute(_HOURLY_SQL, params).fetchall()
        return self._read(_HOURLY_SQL, params)

    def _cache_end(self) -> Optional[datetime]:
        """Beginn der ersten Stunde, die noch nicht im Cache liegt."""
        rows = self._read("SELECT MAX(hour) FROM hourly_energy")
        if not rows or rows[0][0] is None:
            return None
        return datetime.strptime(rows[0][0], TIME_FORMAT) + timedelta(hours=1)

    def refresh_hourly(self, now: Optional[datetime] = None) -> int:
        """
        Verdichtet alle abgeschlossenen, noch nicht gespeicherten Stunden.

        Eine Stunde gilt als abgeschlossen, wenn auch ihr letzter Messwert
        seinen Nachfolger haben kann.

        Returns:
            Anzahl neu gespeicherter Stunden
        """
        now = now or datetime.now()
        end = _hour_floor(now - timedelta(seconds=self.max_gap))
        start = self._cache_end()
        if start is None:
            first = self.first_sample_time()
            if first is None:
                return 0
            start = _hour_floor(first)
        if start >= end:
            return 0

        with self._lock:
            if self._conn is None:
                return 0
            rows = self._aggregate(start, end, self._conn)
            self._conn.executemany(
                f"INSERT OR REPLACE INTO hourly_energy VALUES ({', '.join('?' * len(HOURLY_COLUMNS))})",
                rows
            )
            self._conn.commit()

        if len(rows) > 24:
            logger.info(f"Statistik: {len(rows)} Stunden aus den Messwerten berechnet")
        return len(rows)

    def hourly(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        """
        Liefert die Stundenwerte im Zeitraum [start, end).

        Abgeschlossene Stunden kommen aus dem Cache, der Rest wird live aus
        den Messwerten berechnet.
        """
        cache_end = self._cache_end() or start
        rows: List[tuple] = []
        if start < cache_end:
            rows += self._read(
                "SELECT * FROM hourly_energy WHERE hour >= ? AND hour < ? ORDER BY hour",
                (_format(start), _format(min(end, cache_end)))
            )
        live_start = max(start, cache_end)
        if live_start < end:
            rows += self._aggregate(live_start, end)
        return [dict(zip(HOURLY_COLUMNS, row)) for row in rows]

    # --- Schaltereignisse --------------------------------------------------

    def insert_event(self, timestamp: datetime, device_name: str, action: str,
                     old_state: str, new_state: str, reason: str,
                     surplus_power: Optional[float], device_power: float,
                     priority: int, runtime_today_minutes: int) -> None:
        self._write(
            "INSERT INTO device_events (timestamp, device_name, action, old_state, new_state, reason, "
            "surplus_power, device_power, priority, runtime_today) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_format(timestamp), device_name, action, old_state, new_state, reason,
             None if surplus_power is None else round(surplus_power), device_power,
             priority, runtime_today_minutes)
        )

    def events(self, until: datetime, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Schaltereignisse vor `until` (optional ab `since`), zeitlich sortiert."""
        sql = ("SELECT timestamp, device_name, action, old_state, new_state, reason, "
               "surplus_power, device_power FROM device_events WHERE timestamp < ?")
        params: List[Any] = [_format(until)]
        if since is not None:
            sql += " AND timestamp >= ?"
            params.append(_format(since))
        rows = self._read(sql + " ORDER BY timestamp, id", params)
        keys = ("timestamp", "device_name", "action", "old_state", "new_state", "reason",
                "surplus_power", "device_power")
        return [dict(zip(keys, row)) for row in rows]

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._read(
            "SELECT timestamp, device_name, action, new_state, reason FROM device_events "
            "ORDER BY timestamp DESC, id DESC LIMIT ?", (limit,)
        )
        keys = ("timestamp", "device_name", "action", "new_state", "reason")
        return [dict(zip(keys, row)) for row in rows]
