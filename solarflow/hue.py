"""
Philips Hue Bridge über die lokale REST-API (v1).

Der Zugangsschlüssel liegt im Format von phue in `.python_hue`
({"<bridge-ip>": {"username": "..."}}), damit bestehende Kopplungen erhalten bleiben.
"""

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

import requests

from .jsonfile import read_json, write_json

logger = logging.getLogger(__name__)

_LINK_BUTTON_NOT_PRESSED = 101
_UNAUTHORIZED_USER = 1


class HueLight(NamedTuple):
    light_id: str
    name: str
    on: bool
    reachable: bool


class HueError(Exception):
    """Die Bridge hat nicht oder mit einem Fehler geantwortet."""


class HueBridge:
    """Liest und schaltet die Geräte einer Hue Bridge."""

    # Erst nach so vielen Fehlversuchen in Folge gilt die Bridge als weg
    DOWN_AFTER = 3

    def __init__(self, ip: str, token_file: Path, fallback_token_files: Sequence[Path] = (),
                 timeout: float = 4.0):
        """
        Args:
            ip: Adresse der Bridge
            token_file: Hier wird ein neuer Zugangsschlüssel gespeichert
            fallback_token_files: Weitere Orte, an denen ein Schlüssel liegen kann
            timeout: Timeout je Anfrage in Sekunden
        """
        self.ip = ip
        self.token_file = token_file
        self.timeout = timeout
        self.username = self._load_username([token_file, *fallback_token_files])
        self.connected = False
        self.failures = 0
        self.error: Optional[str] = None
        self._lights: Dict[str, HueLight] = {}
        self._session = requests.Session()
        self._lock = threading.RLock()

    # --- Kopplung ----------------------------------------------------------

    def _load_username(self, paths: Sequence[Path]) -> Optional[str]:
        for path in paths:
            try:
                entry = (read_json(path, {}) or {}).get(self.ip)
            except (ValueError, AttributeError):
                continue
            if isinstance(entry, dict) and entry.get("username"):
                return str(entry["username"])
        return None

    def _save_username(self, username: str) -> None:
        try:
            stored = read_json(self.token_file, {}) or {}
        except ValueError:
            stored = {}
        stored[self.ip] = {"username": username}
        if not write_json(self.token_file, stored):
            logger.error(f"Hue-Zugangsschlüssel konnte nicht in {self.token_file} gespeichert werden")

    def _register(self) -> None:
        """
        Versucht sich an der Bridge anzumelden. Klappt nur innerhalb von 30
        Sekunden nach einem Druck auf den Link-Button - bis dahin bleibt die
        Bridge getrennt und der nächste Zyklus probiert es erneut.
        """
        result = self._request("POST", "/api", {"devicetype": "solarflow#server"})
        entry = result[0] if isinstance(result, list) and result else {}
        username = (entry.get("success") or {}).get("username")
        if username:
            self.username = username
            self._save_username(username)
            logger.info("Mit der Hue Bridge gekoppelt")
            return

        error = entry.get("error") or {}
        if error.get("type") == _LINK_BUTTON_NOT_PRESSED:
            raise HueError("Nicht gekoppelt - bitte den Link-Button auf der Hue Bridge drücken")
        raise HueError(f"Kopplung fehlgeschlagen: {error.get('description', result)}")

    # --- Kommunikation -----------------------------------------------------

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        try:
            response = self._session.request(method, f"http://{self.ip}{path}", json=body,
                                             timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise HueError(f"Hue Bridge {self.ip} nicht erreichbar") from e
        except ValueError as e:
            raise HueError(f"Unverständliche Antwort der Hue Bridge: {e}") from e

    @staticmethod
    def _first_error(result: Any) -> Optional[dict]:
        entries = result if isinstance(result, list) else [result]
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("error"), dict):
                return entry["error"]
        return None

    def _fetch_lights(self) -> Dict[str, HueLight]:
        if not self.username:
            self._register()

        result = self._request("GET", f"/api/{self.username}/lights")
        error = self._first_error(result)
        if error:
            if error.get("type") == _UNAUTHORIZED_USER:
                logger.warning("Hue Bridge kennt den gespeicherten Zugangsschlüssel nicht mehr")
                self.username = None
            raise HueError(f"Hue meldet: {error.get('description', error)}")
        if not isinstance(result, dict):
            raise HueError("Unerwartete Antwort der Hue Bridge")

        lights = {}
        for light_id, entry in result.items():
            try:
                state = entry["state"]
                lights[entry["name"]] = HueLight(
                    light_id=str(light_id),
                    name=entry["name"],
                    on=bool(state.get("on", False)),
                    reachable=bool(state.get("reachable", True)),
                )
            except (KeyError, TypeError, AttributeError):
                logger.debug(f"Hue-Gerät {light_id} ohne verwertbaren Zustand übersprungen")
        return lights

    def refresh(self) -> bool:
        """
        Liest Namen, Schaltzustand und Erreichbarkeit aller Geräte.

        Returns:
            True wenn die Bridge geantwortet hat
        """
        with self._lock:
            try:
                lights = self._fetch_lights()
            except HueError as e:
                self.failures += 1
                if self.failures == self.DOWN_AFTER or (self.failures > self.DOWN_AFTER and self.error != str(e)):
                    logger.warning(str(e))
                self.connected = False
                self.error = str(e)
                return False

            if self.failures >= self.DOWN_AFTER or not self._lights:
                logger.info(f"Hue Bridge {self.ip} verbunden ({len(lights)} Geräte)")
            self.connected = True
            self.failures = 0
            self.error = None
            self._lights = lights
            return True

    def light(self, name: str) -> Optional[HueLight]:
        """Zustand eines Geräts aus der letzten Abfrage."""
        with self._lock:
            return self._lights.get(name)

    def light_names(self) -> List[str]:
        with self._lock:
            return sorted(self._lights)

    def set_on(self, name: str, on: bool) -> bool:
        """
        Schaltet ein Gerät.

        Returns:
            True wenn die Bridge den Befehl ohne Fehler angenommen hat
        """
        with self._lock:
            light = self._lights.get(name)
            if not self.connected or light is None:
                logger.warning(f"'{name}' kann nicht geschaltet werden - "
                               f"{'nicht in Hue gefunden' if self.connected else 'Bridge nicht verbunden'}")
                return False
            try:
                result = self._request("PUT", f"/api/{self.username}/lights/{light.light_id}/state", {"on": on})
            except HueError as e:
                logger.error(f"Schalten von '{name}' fehlgeschlagen: {e}")
                return False

            error = self._first_error(result)
            if error:
                logger.error(f"Hue lehnt das Schalten von '{name}' ab: {error.get('description', error)}")
                return False

            self._lights[name] = light._replace(on=on)
            return True
