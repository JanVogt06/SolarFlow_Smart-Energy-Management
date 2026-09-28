"""
Gerätesteuerung: gleicht die Geräte mit der Hue Bridge ab und verteilt den Überschuss.

Geschaltet wird nur, was die Bridge bestätigt. Ist sie nicht erreichbar, gelten
alle Geräte als nicht erreichbar und die Automatik ruht - es gibt keinen
virtuellen Betrieb.
"""

import logging
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .config import Config
from .database import TIME_FORMAT, Database
from .devices import Device, DeviceState, DeviceStore
from .fronius import SolarData
from .hue import HueBridge

logger = logging.getLogger(__name__)

# So lange zeigt die Bridge nach einem Schaltbefehl gelegentlich noch den alten Zustand
SETTLE_TIME = timedelta(seconds=15)

_ACTIONS = {
    DeviceState.ON: "eingeschaltet",
    DeviceState.OFF: "ausgeschaltet",
    DeviceState.UNREACHABLE: "nicht erreichbar",
}


def _day_start(moment: datetime) -> datetime:
    return datetime.combine(moment.date(), time.min)


class EnergyController:
    """Schaltet Geräte nach verfügbarem Überschuss, Priorität und Regeln."""

    def __init__(self, config: Config, store: DeviceStore, db: Database,
                 bridge_factory: Optional[Callable[[str], HueBridge]] = None):
        """
        Args:
            bridge_factory: Erzeugt die Bridge zu einer IP (austauschbar für Tests)
        """
        self.config = config
        self.store = store
        self.db = db
        # phue legte den Schlüssel im Home-Verzeichnis ab
        self._bridge_factory = bridge_factory or (lambda ip: HueBridge(
            ip, config.hue_token_file, [Path.home() / ".python_hue"]))
        self.bridge: Optional[HueBridge] = None
        self.lock: threading.RLock = store.lock

        # Warum ein Gerät gerade nicht läuft - für das Dashboard
        self.hints: Dict[str, str] = {}
        self._pending: Dict[str, Tuple[datetime, bool]] = {}
        self._unconfirmed: Set[str] = set()
        self._last_seen: Optional[datetime] = None
        self._day = datetime.now().date()
        self._last_surplus: Optional[float] = None

        self.configure_bridge()

    # --- Hue ---------------------------------------------------------------

    def configure_bridge(self) -> None:
        """Legt die Bridge nach der aktuellen Konfiguration an (oder entfernt sie)."""
        with self.lock:
            wanted = self.config.hue_bridge_ip if self.config.enable_hue else None
            if self.bridge is not None and self.bridge.ip == wanted:
                return

            if self.bridge is not None:
                self.switch_all_off(datetime.now(), "Hue-Steuerung geändert")

            self.bridge = self._bridge_factory(wanted) if wanted else None
            self._pending.clear()
            self._unconfirmed = {d.name for d in self.store.all()}
            logger.info(f"Hue-Steuerung {'über ' + wanted if wanted else 'deaktiviert'}")

    def hue_status(self) -> Dict[str, object]:
        bridge = self.bridge
        # Ein einzelner Aussetzer ist noch keine Trennung (siehe HueBridge.DOWN_AFTER)
        connected = bridge is not None and (bridge.connected or 0 < bridge.failures < bridge.DOWN_AFTER)
        return {
            "enabled": bridge is not None,
            "bridge_ip": self.config.hue_bridge_ip,
            "connected": connected,
            "error": bridge.error if bridge is not None and not connected else None,
        }

    def unreachable_reason(self, device: Device) -> Optional[str]:
        """Warum ein Gerät nicht gesteuert werden kann - oder None."""
        bridge = self.bridge
        if bridge is None:
            return "Hue-Steuerung ist deaktiviert"
        if not bridge.connected:
            return bridge.error or "Hue Bridge nicht erreichbar"
        light = bridge.light(device.name)
        if light is None:
            return "In Hue gibt es kein Gerät mit diesem Namen"
        if not light.reachable:
            return "Gerät antwortet nicht – steckt es in der Steckdose?"
        return None

    # --- Zustand -----------------------------------------------------------

    def restore(self, now: datetime) -> None:
        """
        Stellt Zustand und heutige Laufzeit aus den gespeicherten Schaltereignissen
        wieder her, damit ein Neustart weder Laufzeit noch Hysterese vergisst.
        Den echten Schaltzustand bestätigt der erste Abgleich mit der Bridge.
        """
        day_start = _day_start(now)
        last: Dict[str, Tuple[datetime, str]] = {}
        closed: Dict[str, float] = {}
        last_off: Dict[str, datetime] = {}

        for event in self.db.events(until=now + timedelta(seconds=1)):
            name, state = event["device_name"], event["new_state"]
            moment = datetime.strptime(event["timestamp"], TIME_FORMAT)
            previous = last.get(name)
            if previous and previous[1] == "on" and state != "on":
                seconds = (moment - max(previous[0], day_start)).total_seconds()
                closed[name] = closed.get(name, 0.0) + max(seconds, 0.0)
            # "wieder erreichbar" ist kein Ausschalten und startet keine Wartezeit
            if state == "off" and previous and previous[1] == "on":
                last_off[name] = moment
            if not (previous and previous[1] == "on" and state == "on"):
                last[name] = (moment, state)

        with self.lock:
            self._day = now.date()
            # Bis hierhin lief das Programm zuletzt nachweislich
            self._last_seen = self.db.last_sample_time()
            for device in self.store.all():
                moment, state = last.get(device.name, (None, "off"))
                device.runtime_today_seconds = closed.get(device.name, 0.0)
                device.last_switch_off = last_off.get(device.name)
                device.state = DeviceState.ON if state == "on" else DeviceState.OFF
                device.on_since = moment if state == "on" else None
            self._unconfirmed = {d.name for d in self.store.all()}

    def _set_state(self, device: Device, new: DeviceState, now: datetime, reason: str = "") -> None:
        """Wechselt den Zustand, führt die Laufzeit nach und protokolliert den Wechsel."""
        old = device.state
        if old == new:
            return

        if old == DeviceState.ON:
            session_start = max(device.on_since or now, _day_start(now))
            device.runtime_today_seconds += max((now - session_start).total_seconds(), 0.0)
            device.on_since = None
        if new == DeviceState.ON:
            device.on_since = now
        if new == DeviceState.OFF and old == DeviceState.ON:
            device.last_switch_off = now
        device.state = new

        # Wechsel zwischen aus und blockiert sind keine Schaltvorgänge
        if {old, new} <= {DeviceState.OFF, DeviceState.BLOCKED}:
            return

        action = _ACTIONS.get(new, new.value)
        if old == DeviceState.UNREACHABLE:
            action = "wieder erreichbar"
        logger.info(f"'{device.name}' {action}{f' ({reason})' if reason else ''}")
        try:
            self.db.insert_event(
                now, device.name, action, old.value, new.value, reason, self._last_surplus,
                device.power_consumption, device.priority, int(device.runtime_today(now) // 60)
            )
        except Exception as e:
            logger.error(f"Schaltereignis für '{device.name}' nicht gespeichert: {e}")

    def track(self, name: str) -> None:
        """Ein neu angelegtes Gerät übernimmt beim ersten Abgleich still den Hue-Zustand."""
        with self.lock:
            self._unconfirmed.add(name)

    def _roll_day(self, now: datetime) -> None:
        if now.date() == self._day:
            return
        self._day = now.date()
        for device in self.store.all():
            device.runtime_today_seconds = 0.0
        logger.info("Neuer Tag - Tageslaufzeiten zurückgesetzt")

    # --- Abgleich mit der Bridge -------------------------------------------

    def sync(self, now: datetime) -> None:
        """Übernimmt Schaltzustand und Erreichbarkeit von der Bridge."""
        bridge = self.bridge
        # Netzwerkzugriff ohne die Sperre - das Dashboard soll nicht auf die Bridge warten
        answered = bridge.refresh() if bridge is not None else False
        with self.lock:
            self._apply_sync(now, bridge, answered)

    def _apply_sync(self, now: datetime, bridge: Optional[HueBridge], answered: bool) -> None:
        self._roll_day(now)
        if bridge is not self.bridge:
            return  # Bridge wurde währenddessen umkonfiguriert

        if bridge is None:
            # Ohne Steuerung weiß niemand, ob ein Gerät noch läuft
            for device in self.store.all():
                if device.state == DeviceState.ON:
                    self._set_state(device, DeviceState.UNREACHABLE, now, "Hue-Steuerung deaktiviert")
            return

        if not answered:
            if bridge.failures < HueBridge.DOWN_AFTER:
                return  # kurzer Aussetzer: Zustände halten, aber nicht schalten
            for device in self.store.all():
                self._set_state(device, DeviceState.UNREACHABLE, now, bridge.error or "Hue Bridge nicht erreichbar")
            return

        for device in self.store.all():
            light = bridge.light(device.name)
            if light is None or not light.reachable:
                self._set_state(device, DeviceState.UNREACHABLE, now, self.unreachable_reason(device) or "")
                self._pending.pop(device.name, None)
                continue

            if self._settling(device.name, light.on, now):
                continue

            is_on = device.state == DeviceState.ON
            unconfirmed = device.name in self._unconfirmed
            silent = device.state == DeviceState.UNREACHABLE or unconfirmed
            self._unconfirmed.discard(device.name)
            if light.on == is_on and device.state != DeviceState.UNREACHABLE:
                continue

            target = DeviceState.ON if light.on else DeviceState.OFF
            if not silent:
                self._set_state(device, target, now, "extern geschaltet (z.B. Hue-App)")
                self._start_manual(device, now)
            elif unconfirmed and is_on and self._last_seen and device.on_since and self._last_seen > device.on_since:
                # Nach einem Absturz: wann das Gerät ausging, weiß niemand - spätestens
                # beim letzten Messwert des vorigen Laufs lief das Programm noch
                self._set_state(device, target, min(self._last_seen, now), "beim Neustart aus vorgefunden")
            else:
                self._set_state(device, target, now, "Zustand von Hue übernommen")

    def _settling(self, name: str, hw_on: bool, now: datetime) -> bool:
        """True solange die Bridge einen eigenen Schaltbefehl noch nicht anzeigt."""
        pending = self._pending.get(name)
        if pending is None:
            return False
        switched_at, expected = pending
        if hw_on != expected and now - switched_at < SETTLE_TIME:
            return True
        del self._pending[name]
        return False

    # --- Schalten ----------------------------------------------------------

    def _switch(self, device: Device, on: bool, now: datetime, reason: str) -> bool:
        if self.bridge is None or not self.bridge.set_on(device.name, on):
            return False
        self._pending[device.name] = (now, on)
        self._set_state(device, DeviceState.ON if on else DeviceState.OFF, now, reason)
        return True

    def _start_manual(self, device: Device, now: datetime) -> None:
        minutes = self.config.manual_override_minutes
        if minutes > 0:
            device.manual_until = now + timedelta(minutes=minutes)

    def switch_manually(self, name: str, on: bool, now: Optional[datetime] = None) -> bool:
        """
        Schaltet ein Gerät von Hand und pausiert dafür die Automatik.

        Returns:
            True wenn die Bridge den Befehl angenommen hat
        """
        now = now or datetime.now()
        with self.lock:
            device = self.store.get(name)
            if device is None or self.unreachable_reason(device):
                return False
            if not self._switch(device, on, now, "manuell im Dashboard"):
                return False
            self._start_manual(device, now)
            return True

    def release_manual(self, name: str) -> None:
        with self.lock:
            device = self.store.get(name)
            if device is not None:
                device.manual_until = None

    def switch_all_off(self, now: datetime, reason: str, include_manual: bool = True) -> None:
        """
        Schaltet laufende Geräte aus - damit nichts unbeaufsichtigt weiterläuft.

        Args:
            include_manual: Auch von Hand eingeschaltete Geräte ausschalten
        """
        with self.lock:
            for device in self.store.all():
                if device.state != DeviceState.ON or (not include_manual and device.is_manual(now)):
                    continue
                if not self._switch(device, False, now, reason):
                    logger.warning(f"'{device.name}' konnte nicht ausgeschaltet werden")

    # --- Automatik ---------------------------------------------------------

    def cycle(self, data: SolarData) -> None:
        """Ein Steuerzyklus: Tageswechsel, Abgleich mit der Bridge, Entscheidung."""
        now = data.timestamp
        bridge = self.bridge
        answered = bridge.refresh() if bridge is not None else False
        with self.lock:
            self._last_surplus = -data.grid_power
            self._apply_sync(now, bridge, answered)
            self.decide(data, now)

    def decide(self, data: SolarData, now: datetime) -> None:
        """
        Verteilt den Überschuss (= aktuelle Einspeisung) auf die Geräte.

        1. Laufende Geräte prüfen, niedrigste Priorität zuerst: aus, wenn der
           Überschuss ohne sie unter ihren Ausschalt-Schwellwert fiele, der
           Akku zu leer ist oder Zeitfenster/Tageslaufzeit es verlangen.
        2. Freie Geräte, höchste Priorität zuerst: ein, wenn der Überschuss den
           Einschalt-Schwellwert erreicht - notfalls durch Verdrängen
           niedriger priorisierter Geräte.
        """
        self.hints = {}
        if self.bridge is None or not self.bridge.connected:
            return

        cfg = self.config
        # Vorzeichenbehaftet: bei Netzbezug negativ, damit ein Gerät nicht weiterläuft,
        # obwohl auch ohne es noch Strom aus dem Netz käme
        surplus = -data.grid_power
        discharge = max(data.battery_power, 0.0)
        soc = data.battery_soc if data.battery_soc is not None else 100.0
        hysteresis = timedelta(minutes=cfg.hysteresis_minutes)

        devices = [d for d in self.store.all()
                   if d.state != DeviceState.UNREACHABLE and not d.is_manual(now)]
        switched: Set[str] = set()

        for device in reversed(devices):
            if device.state != DeviceState.ON:
                continue
            forced, reason = False, None
            if not device.is_time_allowed(now):
                forced, reason = True, "außerhalb der erlaubten Zeit"
            elif not device.can_run_today(now):
                forced, reason = True, "maximale Tageslaufzeit erreicht"
            elif soc < cfg.min_battery_soc_off:
                reason = f"Akku {soc:.0f} % < {cfg.min_battery_soc_off:.0f} %"
            else:
                # Entladestrom aus dem Akku ist kein Solarüberschuss
                available = surplus + device.power_consumption - discharge
                if available < device.switch_off_threshold:
                    reason = f"Überschuss {available:.0f} W < {device.switch_off_threshold:.0f} W"

            if reason is None:
                continue
            if not forced and not device.min_runtime_reached(now):
                self.hints[device.name] = f"Mindestlaufzeit noch nicht erreicht ({reason})"
                continue
            if self._switch(device, False, now, reason):
                surplus += device.power_consumption
                switched.add(device.name)

        for device in devices:
            if device.state == DeviceState.ON:
                continue

            if not device.is_time_allowed(now):
                self._set_state(device, DeviceState.BLOCKED, now)
                self.hints[device.name] = f"nur {device.format_time_ranges()}"
                continue
            if not device.can_run_today(now):
                self._set_state(device, DeviceState.BLOCKED, now)
                self.hints[device.name] = "Tageslaufzeit erreicht"
                continue
            self._set_state(device, DeviceState.OFF, now)
            if device.name in switched:
                continue

            if soc < cfg.min_battery_soc_on:
                self.hints[device.name] = f"wartet auf Akku ({soc:.0f} % < {cfg.min_battery_soc_on:.0f} %)"
                continue
            if device.last_switch_off and now - device.last_switch_off < hysteresis:
                continue

            if surplus < device.switch_on_threshold:
                victims = self._preemption_victims(device, devices, surplus, now)
                if victims is None:
                    self.hints[device.name] = (f"Überschuss {surplus:.0f} W < {device.switch_on_threshold:.0f} W")
                    continue
                for victim in victims:
                    if self._switch(victim, False, now, f"verdrängt von '{device.name}'"):
                        surplus += victim.power_consumption
                        switched.add(victim.name)
                if surplus < device.switch_on_threshold:
                    continue

            if self._switch(device, True, now, f"Überschuss {surplus:.0f} W ≥ {device.switch_on_threshold:.0f} W"):
                surplus -= device.power_consumption
                switched.add(device.name)

    @staticmethod
    def _preemption_victims(device: Device, devices: List[Device], surplus: float,
                            now: datetime) -> Optional[List[Device]]:
        """
        Niedriger priorisierte laufende Geräte, deren Abschalten für `device`
        genug Überschuss freigibt - oder None, wenn das nicht reicht.
        """
        victims, freed = [], 0.0
        for other in reversed(devices):
            if other.priority <= device.priority:
                break
            if other.state != DeviceState.ON or not other.min_runtime_reached(now):
                continue
            victims.append(other)
            freed += other.power_consumption
            if surplus + freed >= device.switch_on_threshold:
                return victims
        return None
