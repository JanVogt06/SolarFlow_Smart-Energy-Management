"""
Konfiguration des Smart Energy Managers.

Jeder Wert kommt aus drei Quellen, die spätere gewinnt:
Umgebungsvariable < im Dashboard gespeicherte Einstellung (settings.json) < Kommandozeile.
"""

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .jsonfile import read_json, write_json

logger = logging.getLogger(__name__)


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "ja", "on")


def _parse_hour(raw: str) -> int:
    """Akzeptiert "22", "22:00" und "22:00:00" - der Tarif wechselt zur vollen Stunde."""
    hour, _, rest = raw.strip().partition(":")
    if rest.strip(":0"):
        raise ValueError(f"Tarifwechsel nur zur vollen Stunde möglich: {raw}")
    return int(hour)


# Feld -> (Umgebungsvariable, Parser). Ältere Variablennamen bleiben gültig.
_ENV: Dict[str, tuple] = {
    "fronius_ip": ("FRONIUS_IP", str),
    "update_interval": ("UPDATE_INTERVAL", int),
    "port": ("API_PORT", int),
    "log_level": ("LOG_LEVEL", str),
    "enable_hue": ("ENABLE_HUE", _parse_bool),
    "hue_bridge_ip": ("HUE_BRIDGE_IP", str),
    "hysteresis_minutes": ("DEVICE_HYSTERESIS_MINUTES", int),
    "manual_override_minutes": ("DEVICE_MANUAL_OVERRIDE_MINUTES", int),
    "min_battery_soc_on": ("DEVICE_MIN_BATTERY_SOC_ON", float),
    "min_battery_soc_off": ("DEVICE_MIN_BATTERY_SOC_OFF", float),
    "electricity_price": ("ELECTRICITY_PRICE", float),
    "electricity_price_night": ("ELECTRICITY_PRICE_NIGHT", float),
    "feed_in_tariff": ("FEED_IN_TARIFF", float),
    "night_tariff_start": ("NIGHT_TARIFF_START", _parse_hour),
    "night_tariff_end": ("NIGHT_TARIFF_END", _parse_hour),
}

# Über das Dashboard änderbar und in settings.json gespeichert
EDITABLE = (
    "fronius_ip", "update_interval",
    "enable_hue", "hue_bridge_ip",
    "hysteresis_minutes", "manual_override_minutes",
    "min_battery_soc_on", "min_battery_soc_off",
    "electricity_price", "electricity_price_night", "feed_in_tariff",
    "night_tariff_start", "night_tariff_end",
)


@dataclass
class Config:
    """Alle Einstellungen an einem Ort."""

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", ".")))

    fronius_ip: str = "192.168.178.90"
    update_interval: int = 5  # Sekunden
    port: int = 8000
    log_level: str = "INFO"

    enable_hue: bool = False
    hue_bridge_ip: str = "192.168.178.26"

    hysteresis_minutes: int = 5
    manual_override_minutes: int = 30
    min_battery_soc_on: float = 95.0
    min_battery_soc_off: float = 20.0

    electricity_price: float = 0.40  # €/kWh
    electricity_price_night: float = 0.30  # €/kWh
    feed_in_tariff: float = 0.082  # €/kWh
    night_tariff_start: int = 22  # volle Stunde
    night_tariff_end: int = 6

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "Config":
        """Erstellt die Konfiguration aus Umgebungsvariablen."""
        environ = os.environ if environ is None else environ
        config = cls(data_dir=Path(environ.get("DATA_DIR", ".")))

        for name, (variable, parse) in _ENV.items():
            raw = environ.get(variable)
            if raw is None or raw.strip() == "":
                continue
            try:
                setattr(config, name, parse(raw))
            except ValueError:
                logger.error(f"{variable}={raw!r} ist ungültig - verwende {getattr(config, name)!r}")

        return config

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def devices_file(self) -> Path:
        return self.data_dir / "devices.json"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "solarflow.db"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "solar_monitor.log"

    @property
    def hue_token_file(self) -> Path:
        return self.data_dir / ".python_hue"

    def is_night(self, hour: int) -> bool:
        """Prüft ob eine Stunde (0-23) im Nachttarif liegt."""
        start, end = self.night_tariff_start, self.night_tariff_end
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def editable(self) -> Dict[str, Any]:
        """Gibt die über das Dashboard änderbaren Einstellungen zurück."""
        return {name: getattr(self, name) for name in EDITABLE}

    def apply(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """
        Übernimmt bekannte, änderbare Einstellungen.

        Args:
            values: Neue Werte (unbekannte Schlüssel und None werden ignoriert)

        Returns:
            Tatsächlich übernommene Werte
        """
        types: Dict[str, Callable] = {f.name: f.type for f in fields(self)}
        applied = {}
        for name, value in values.items():
            if name not in EDITABLE or value is None:
                continue
            setattr(self, name, types[name](value))
            applied[name] = getattr(self, name)
        return applied


class SettingsStore:
    """Hält die im Dashboard geänderten Einstellungen in settings.json fest.

    Gespeichert werden nur tatsächlich geänderte Werte; alles andere bleibt
    über die Umgebung steuerbar.
    """

    def __init__(self, config: Config):
        self.config = config
        self.path = config.settings_file

    def load(self) -> None:
        """Wendet die gespeicherten Einstellungen auf die Konfiguration an."""
        try:
            stored = read_json(self.path, {})
        except ValueError as e:
            logger.error(f"{self.path} ist kein gültiges JSON und wird ignoriert: {e}")
            return

        if isinstance(stored, dict) and stored:
            applied = self.config.apply(stored)
            logger.info(f"{len(applied)} gespeicherte Einstellungen aus {self.path} übernommen")

    def save(self, changes: Dict[str, Any]) -> bool:
        """
        Übernimmt Änderungen in die Konfiguration und schreibt sie in die Datei.

        Returns:
            True wenn die Datei geschrieben werden konnte
        """
        applied = self.config.apply(changes)
        if not applied:
            return True

        try:
            stored = read_json(self.path, {})
        except ValueError:
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored.update(applied)

        if not write_json(self.path, stored):
            return False

        logger.info(f"Einstellungen gespeichert: {', '.join(sorted(applied))}")
        return True
