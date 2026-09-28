"""
Steuerbare Geräte und ihre Konfiguration in devices.json.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .jsonfile import read_json, write_json

logger = logging.getLogger(__name__)

TimeRange = Tuple[time, time]


class DeviceState(Enum):
    OFF = "off"
    ON = "on"
    BLOCKED = "blocked"  # außerhalb der erlaubten Zeit oder Tageslaufzeit erreicht
    UNREACHABLE = "unreachable"  # Hue meldet das Gerät nicht erreichbar


def parse_time(value: Any) -> time:
    """Akzeptiert "HH:MM" und "HH:MM:SS"."""
    if isinstance(value, time):
        return value
    try:
        return time.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"Ungültige Uhrzeit '{value}' (erwartet HH:MM)") from None


@dataclass
class Device:
    """Ein über Hue schaltbares Gerät samt Regeln und Laufzeitzustand."""

    name: str
    power_consumption: float  # Watt
    priority: int  # 1 = höchste
    switch_on_threshold: float  # Mindestüberschuss zum Einschalten
    switch_off_threshold: float  # darunter wird ausgeschaltet
    description: str = ""
    min_runtime: int = 0  # Minuten
    max_runtime_per_day: int = 0  # Minuten, 0 = unbegrenzt
    allowed_time_ranges: List[TimeRange] = field(default_factory=list)

    state: DeviceState = DeviceState.OFF
    on_since: Optional[datetime] = None
    runtime_today_seconds: float = 0.0  # abgeschlossene Laufzeit heute
    last_switch_off: Optional[datetime] = None
    manual_until: Optional[datetime] = None

    def __post_init__(self) -> None:
        errors = self.validation_errors()
        if errors:
            raise ValueError(f"Gerät '{self.name}': " + "; ".join(errors))

    def validation_errors(self) -> List[str]:
        errors = []
        if not isinstance(self.name, str) or not self.name.strip():
            errors.append("Name darf nicht leer sein")
        if self.power_consumption <= 0:
            errors.append("Leistung muss größer als 0 sein")
        if not 1 <= self.priority <= 10:
            errors.append("Priorität muss zwischen 1 und 10 liegen")
        if self.switch_on_threshold < 0 or self.switch_off_threshold < 0:
            errors.append("Schwellwerte dürfen nicht negativ sein")
        if self.switch_off_threshold > self.switch_on_threshold:
            errors.append("Ausschalt-Schwellwert darf nicht höher als der Einschalt-Schwellwert sein")
        if not 0 <= self.min_runtime <= 1440 or not 0 <= self.max_runtime_per_day <= 1440:
            errors.append("Laufzeiten müssen zwischen 0 und 1440 Minuten liegen")
        for start, end in self.allowed_time_ranges:
            if start == end:
                errors.append(f"Zeitbereich {start:%H:%M}-{end:%H:%M} ist leer")
        return errors

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Device":
        """
        Erstellt ein Gerät aus devices.json oder einer API-Anfrage.

        Raises:
            ValueError: bei fehlenden oder ungültigen Angaben
        """
        try:
            ranges = [(parse_time(start), parse_time(end))
                      for start, end in data.get("allowed_time_ranges") or []]
            return cls(
                name=str(data["name"]).strip(),
                description=str(data.get("description") or ""),
                power_consumption=float(data["power_consumption"]),
                priority=int(data["priority"]),
                switch_on_threshold=float(data["switch_on_threshold"]),
                switch_off_threshold=float(data["switch_off_threshold"]),
                min_runtime=int(data.get("min_runtime") or 0),
                max_runtime_per_day=int(data.get("max_runtime_per_day") or 0),
                allowed_time_ranges=ranges,
            )
        except KeyError as e:
            raise ValueError(f"Pflichtfeld {e} fehlt") from None
        except TypeError as e:
            raise ValueError(f"Ungültige Angabe: {e}") from None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "power_consumption": self.power_consumption,
            "priority": self.priority,
            "min_runtime": self.min_runtime,
            "max_runtime_per_day": self.max_runtime_per_day,
            "switch_on_threshold": self.switch_on_threshold,
            "switch_off_threshold": self.switch_off_threshold,
            "allowed_time_ranges": [[s.strftime("%H:%M"), e.strftime("%H:%M")]
                                    for s, e in self.allowed_time_ranges],
        }

    # --- Regeln ------------------------------------------------------------

    def is_time_allowed(self, now: datetime) -> bool:
        """Liegt `now` in einem erlaubten Zeitbereich? Bereiche über Mitternacht sind erlaubt."""
        if not self.allowed_time_ranges:
            return True
        current = now.time()
        for start, end in self.allowed_time_ranges:
            if start < end and start <= current < end:
                return True
            if start > end and (current >= start or current < end):
                return True
        return False

    def runtime_today(self, now: datetime) -> float:
        """Laufzeit heute in Sekunden, einschließlich der laufenden Session ab Mitternacht."""
        if self.on_since is None:
            return self.runtime_today_seconds
        session_start = max(self.on_since, datetime.combine(now.date(), time.min))
        return self.runtime_today_seconds + max((now - session_start).total_seconds(), 0.0)

    def can_run_today(self, now: datetime) -> bool:
        return self.max_runtime_per_day == 0 or self.runtime_today(now) < self.max_runtime_per_day * 60

    def min_runtime_reached(self, now: datetime) -> bool:
        if self.min_runtime == 0 or self.on_since is None:
            return True
        return now - self.on_since >= timedelta(minutes=self.min_runtime)

    def is_manual(self, now: datetime) -> bool:
        return self.manual_until is not None and now < self.manual_until

    def format_time_ranges(self) -> str:
        if not self.allowed_time_ranges:
            return "immer"
        return ", ".join(f"{s:%H:%M}-{e:%H:%M}" for s, e in self.allowed_time_ranges)


class DeviceStore:
    """Geräteliste mit Persistenz in devices.json. Thread-sicher."""

    def __init__(self, path: Path):
        self.path = path
        self._devices: List[Device] = []
        self.lock = threading.RLock()

    def load(self) -> None:
        """Lädt die Geräte; ungültige Einträge werden gemeldet und übersprungen."""
        try:
            raw = read_json(self.path, [])
        except ValueError as e:
            logger.error(f"{self.path} ist kein gültiges JSON - starte ohne Geräte: {e}")
            raw = []

        devices: List[Device] = []
        for index, entry in enumerate(raw if isinstance(raw, list) else []):
            try:
                device = Device.from_dict(entry)
            except ValueError as e:
                logger.error(f"Gerät {index + 1} in {self.path} übersprungen: {e}")
                continue
            if any(d.name == device.name for d in devices):
                logger.error(f"Gerät '{device.name}' ist doppelt in {self.path} - zweiter Eintrag übersprungen")
                continue
            devices.append(device)

        with self.lock:
            self._devices = devices
        logger.info(f"{len(devices)} Geräte aus {self.path} geladen")

    def save(self) -> bool:
        with self.lock:
            data = [device.to_dict() for device in self._devices]
        if not write_json(self.path, data):
            logger.error(f"Geräte konnten nicht nach {self.path} geschrieben werden")
            return False
        return True

    def all(self) -> List[Device]:
        """Alle Geräte nach Priorität (1 zuerst) - als neue Liste."""
        with self.lock:
            return sorted(self._devices, key=lambda d: d.priority)

    def get(self, name: str) -> Optional[Device]:
        with self.lock:
            return next((d for d in self._devices if d.name == name), None)

    def add(self, device: Device) -> None:
        with self.lock:
            if self.get(device.name):
                raise ValueError(f"Gerät '{device.name}' existiert bereits")
            self._devices.append(device)

    def replace(self, name: str, device: Device) -> None:
        """Ersetzt die Konfiguration eines Geräts; der Laufzeitzustand bleibt erhalten."""
        with self.lock:
            old = self.get(name)
            if old is None:
                raise KeyError(name)
            if device.name != name and self.get(device.name):
                raise ValueError(f"Gerät '{device.name}' existiert bereits")
            for attr in ("state", "on_since", "runtime_today_seconds", "last_switch_off", "manual_until"):
                setattr(device, attr, getattr(old, attr))
            self._devices[self._devices.index(old)] = device

    def remove(self, name: str) -> Optional[Device]:
        with self.lock:
            device = self.get(name)
            if device:
                self._devices.remove(device)
            return device
