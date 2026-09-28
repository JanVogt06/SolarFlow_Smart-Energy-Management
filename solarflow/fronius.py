"""
Abfrage des Fronius Wechselrichters über die Solar API.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

POWER_FLOW_ENDPOINT = "/solar_api/v1/GetPowerFlowRealtimeData.fcgi"


@dataclass(frozen=True)
class SolarData:
    """Momentaufnahme der Leistungsflüsse in Watt."""

    pv_power: float = 0.0  # Erzeugung
    grid_power: float = 0.0  # + Bezug, - Einspeisung
    battery_power: float = 0.0  # + Entladen, - Laden
    load_power: float = 0.0  # Hausverbrauch
    battery_soc: Optional[float] = None  # Ladestand in %
    timestamp: datetime = field(default_factory=lambda: datetime.now().replace(microsecond=0))

    @property
    def feed_in_power(self) -> float:
        """Einspeisung ins Netz (positiv) - der Überschuss, den die Steuerung verteilt."""
        return max(-self.grid_power, 0.0)

    @property
    def grid_consumption(self) -> float:
        """Netzbezug (positiv)."""
        return max(self.grid_power, 0.0)

    @property
    def battery_charging(self) -> bool:
        return self.battery_power < 0

    @property
    def has_battery(self) -> bool:
        return self.battery_soc is not None

    @property
    def self_consumption(self) -> float:
        """Selbst gedeckter Verbrauch (aus PV und Batterie)."""
        return min(max(self.load_power - self.grid_consumption, 0.0), self.load_power)

    @property
    def autarky_rate(self) -> float:
        """Anteil des Verbrauchs, der nicht aus dem Netz kommt, in Prozent."""
        if self.load_power <= 10:
            return 0.0
        return self.self_consumption / self.load_power * 100


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_power_flow(payload: Dict[str, Any]) -> SolarData:
    """
    Wandelt die Antwort von GetPowerFlowRealtimeData in SolarData um.

    Fronius liefert null für nicht vorhandene Komponenten (z.B. P_Akku ohne
    Batterie oder P_PV nachts) - das zählt als 0 W.

    Raises:
        ValueError: wenn die Antwort nicht die erwartete Struktur hat
    """
    try:
        data = payload["Body"]["Data"]
        site = data["Site"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"Unerwartete Antwort des Wechselrichters: {e}") from None

    soc = None
    candidates = [(entry, "SOC") for entry in (data.get("Inverters") or {}).values()]
    candidates += [(entry, "StateOfCharge_Relative") for entry in (data.get("Storage") or {}).values()]
    for entry, key in candidates:
        if isinstance(entry, dict) and entry.get(key) is not None:
            soc = _to_float(entry[key])
            break

    return SolarData(
        # Nachts meldet manche Firmware wenige Watt negativ - Erzeugung ist nie negativ
        pv_power=max(_to_float(site.get("P_PV")), 0.0),
        grid_power=_to_float(site.get("P_Grid")),
        battery_power=_to_float(site.get("P_Akku")),
        load_power=abs(_to_float(site.get("P_Load"))),
        battery_soc=soc,
    )


class FroniusClient:
    """Liest die aktuellen Leistungsdaten vom Wechselrichter."""

    def __init__(self, ip_address: str, timeout: float = 5.0):
        self.ip_address = ip_address
        self.timeout = timeout
        self.session = requests.Session()

    def fetch(self) -> Optional[SolarData]:
        """
        Holt die aktuellen Leistungsdaten.

        Returns:
            SolarData oder None, wenn der Wechselrichter nicht antwortet
        """
        url = f"http://{self.ip_address}{POWER_FLOW_ENDPOINT}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return parse_power_flow(response.json())
        except requests.RequestException as e:
            logger.warning(f"Wechselrichter {self.ip_address} nicht erreichbar: {e}")
        except ValueError as e:
            logger.warning(f"Antwort des Wechselrichters unbrauchbar: {e}")
        return None
