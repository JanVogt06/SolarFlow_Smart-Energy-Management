"""
Hauptschleife: Messwert holen, speichern, Geräte steuern.
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from .config import Config, SettingsStore
from .controller import EnergyController
from .database import Database
from .fronius import FroniusClient, SolarData

logger = logging.getLogger(__name__)

# Ohne frische Messwerte weiß die Automatik nicht, ob noch Überschuss da ist
DATA_LOSS_TIMEOUT = timedelta(minutes=2)


class Monitor:
    """Verbindet Wechselrichter, Datenbank und Gerätesteuerung."""

    def __init__(self, config: Config, settings: SettingsStore, db: Database,
                 controller: EnergyController, fronius: FroniusClient):
        self.config = config
        self.settings = settings
        self.db = db
        self.controller = controller
        self.fronius = fronius

        self.latest: Optional[SolarData] = None
        self.on_update: Optional[Callable[[Optional[SolarData]], None]] = None
        self._last_data_at: Optional[datetime] = None
        self._data_lost = False
        self._stop = threading.Event()

    def run(self) -> None:
        """Läuft bis stop() aufgerufen wird."""
        logger.info(f"Frage den Wechselrichter {self.config.fronius_ip} alle "
                    f"{self.config.update_interval} s ab")
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick(datetime.now())
            except Exception:
                logger.exception("Fehler im Messzyklus")
            elapsed = time.monotonic() - started
            self._stop.wait(max(self.config.update_interval - elapsed, 0.5))

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now: datetime) -> None:
        data = self.fronius.fetch()

        if data is None:
            # Geräteansicht trotzdem aktuell halten (z.B. extern geschaltet)
            with self.controller.lock:
                self.controller.sync(now)
            self._handle_data_loss(now)
        else:
            self.latest = data
            self._last_data_at = now
            if self._data_lost:
                logger.info("Wechselrichter liefert wieder Daten")
                self._data_lost = False
            self.db.insert_sample(data)
            self.controller.cycle(data)

        self.db.refresh_hourly(now)
        if self.on_update:
            self.on_update(data)

    def _handle_data_loss(self, now: datetime) -> None:
        if self._data_lost or self._last_data_at is None or now - self._last_data_at < DATA_LOSS_TIMEOUT:
            return
        self._data_lost = True
        logger.warning("Seit zwei Minuten keine Daten vom Wechselrichter - schalte automatisch "
                       "gesteuerte Geräte aus")
        self.controller.switch_all_off(now, "keine Daten vom Wechselrichter", include_manual=False)

    def current(self) -> Optional[Dict[str, Any]]:
        """Letzter Messwert samt Alter - für Dashboard und API."""
        data = self.latest
        if data is None:
            return None
        age = (datetime.now() - data.timestamp).total_seconds()
        return {
            "timestamp": data.timestamp.isoformat(),
            "age_seconds": round(age),
            "stale": age > max(3 * self.config.update_interval, 30),
            "pv_power": data.pv_power,
            "load_power": data.load_power,
            "grid_power": data.grid_power,
            "battery_power": data.battery_power,
            "battery_soc": data.battery_soc,
            "feed_in_power": data.feed_in_power,
            "grid_consumption": data.grid_consumption,
            "self_consumption": data.self_consumption,
            "autarky_rate": data.autarky_rate,
            "has_battery": data.has_battery,
        }

    def apply_settings(self, changes: Dict[str, Any]) -> bool:
        """Übernimmt Einstellungen zur Laufzeit und speichert sie."""
        saved = self.settings.save(changes)
        self.fronius.ip_address = self.config.fronius_ip
        self.db.update_interval = self.config.update_interval
        self.controller.configure_bridge()
        return saved

    def shutdown(self) -> None:
        """Schaltet alle Geräte aus und schließt die Datenbank."""
        logger.info("Beende - schalte alle Geräte aus")
        self.controller.switch_all_off(datetime.now(), "Programmende")
        self.db.close()
