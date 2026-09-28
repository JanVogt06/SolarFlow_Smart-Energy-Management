import json
from pathlib import Path

import pytest
import requests

from solarflow.config import Config, SettingsStore
from solarflow.fronius import FroniusClient, parse_power_flow
from solarflow.hue import HueBridge


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.payload


class FakeHue:
    """Simuliert die REST-API der Bridge."""

    def __init__(self):
        self.online = True
        self.button_pressed = False
        self.users = {"known-user"}
        self.lights = {"1": {"name": "Heizung", "state": {"on": False, "reachable": True}},
                       "2": {"name": "Entfeuchter", "state": {"on": True, "reachable": False}},
                       "3": {"name": "Kaputt"}}
        self.calls = []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        if not self.online:
            raise requests.ConnectionError("no route to host")
        path = url.split("10.0.0.2", 1)[1]
        if method == "POST" and path == "/api":
            if not self.button_pressed:
                return Response([{"error": {"type": 101, "description": "link button not pressed"}}])
            self.users.add("new-user")
            return Response([{"success": {"username": "new-user"}}])
        user = path.split("/")[2]
        if user not in self.users:
            return Response([{"error": {"type": 1, "description": "unauthorized user"}}])
        if method == "GET" and path.endswith("/lights"):
            return Response(self.lights)
        if method == "PUT":
            light_id = path.split("/")[4]
            self.lights[light_id]["state"]["on"] = json["on"]
            return Response([{"success": {f"/lights/{light_id}/state/on": json["on"]}}])
        return Response({}, 404)


@pytest.fixture
def hue(tmp_path, monkeypatch):
    fake = FakeHue()
    token = tmp_path / ".python_hue"
    token.write_text(json.dumps({"10.0.0.2": {"username": "known-user"}}))
    bridge = HueBridge("10.0.0.2", token)
    monkeypatch.setattr(bridge._session, "request", fake.request)
    return bridge, fake


def test_hue_reads_lights_and_reachability(hue):
    bridge, _ = hue
    assert bridge.refresh()
    assert bridge.connected
    assert bridge.light_names() == ["Entfeuchter", "Heizung"]
    assert bridge.light("Entfeuchter").reachable is False


def test_hue_switches_and_reports_rejections(hue):
    bridge, fake = hue
    bridge.refresh()
    assert bridge.set_on("Heizung", True)
    assert fake.lights["1"]["state"]["on"] is True
    assert bridge.light("Heizung").on
    assert not bridge.set_on("Gibt es nicht", True)


def test_hue_offline_is_not_connected_and_cannot_switch(hue):
    bridge, fake = hue
    bridge.refresh()
    fake.online = False

    assert not bridge.refresh()
    assert not bridge.connected
    assert "nicht erreichbar" in bridge.error
    assert not bridge.set_on("Heizung", True)
    assert fake.calls[-1][0] == "GET"  # kein PUT an eine tote Bridge


def test_hue_pairing_waits_for_the_link_button(tmp_path, monkeypatch):
    fake = FakeHue()
    token = tmp_path / ".python_hue"
    bridge = HueBridge("10.0.0.2", token)
    monkeypatch.setattr(bridge._session, "request", fake.request)

    assert not bridge.refresh()
    assert "Link-Button" in bridge.error

    fake.button_pressed = True
    assert bridge.refresh()
    assert json.loads(token.read_text()) == {"10.0.0.2": {"username": "new-user"}}


def test_hue_forgets_a_revoked_user(hue):
    bridge, fake = hue
    fake.users = set()
    assert not bridge.refresh()
    assert bridge.username is None


def test_hue_reads_the_phue_token_from_home(tmp_path):
    home_token = tmp_path / "home" / ".python_hue"
    home_token.parent.mkdir()
    home_token.write_text(json.dumps({"10.0.0.2": {"username": "from-phue"}}))
    bridge = HueBridge("10.0.0.2", tmp_path / "data" / ".python_hue", [home_token])
    assert bridge.username == "from-phue"


POWER_FLOW = {
    "Body": {"Data": {
        "Site": {"P_PV": 5234.5, "P_Grid": -3000.1, "P_Akku": -1000, "P_Load": -1234.4},
        "Inverters": {"1": {"SOC": 87.3}},
    }}
}


def test_parse_power_flow():
    data = parse_power_flow(POWER_FLOW)
    assert (data.pv_power, data.grid_power, data.battery_power, data.load_power) == (5234.5, -3000.1, -1000, 1234.4)
    assert data.battery_soc == 87.3
    assert data.feed_in_power == pytest.approx(3000.1)
    assert data.autarky_rate == 100


def test_parse_power_flow_without_battery_at_night():
    data = parse_power_flow({"Body": {"Data": {"Site": {"P_PV": None, "P_Grid": 300, "P_Akku": None,
                                                         "P_Load": -300}}}})
    assert (data.pv_power, data.battery_power, data.battery_soc) == (0, 0, None)
    assert data.grid_consumption == 300
    assert data.autarky_rate == 0


def test_parse_power_flow_rejects_garbage():
    with pytest.raises(ValueError):
        parse_power_flow({"Head": {}})


def test_fronius_client_returns_none_when_offline(monkeypatch):
    client = FroniusClient("10.0.0.9")

    def fail(*args, **kwargs):
        raise requests.ConnectTimeout("timeout")

    monkeypatch.setattr(client.session, "get", fail)
    assert client.fetch() is None


def test_config_from_env():
    config = Config.from_env({
        "DATA_DIR": "/data", "FRONIUS_IP": "1.2.3.4", "UPDATE_INTERVAL": "10", "ENABLE_HUE": "True",
        "NIGHT_TARIFF_START": "21:00", "API_PORT": "9000", "ELECTRICITY_PRICE": "kaputt",
    })
    assert config.data_dir == Path("/data")
    assert (config.fronius_ip, config.update_interval, config.enable_hue, config.port) == ("1.2.3.4", 10, True, 9000)
    assert config.night_tariff_start == 21
    assert config.electricity_price == 0.40  # ungültig -> Standard


def test_night_tariff_hours():
    config = Config(night_tariff_start=22, night_tariff_end=6)
    assert [h for h in range(24) if config.is_night(h)] == [0, 1, 2, 3, 4, 5, 22, 23]
    config.night_tariff_start, config.night_tariff_end = 0, 0
    assert not any(config.is_night(h) for h in range(24))


def test_settings_persist_only_changed_values(tmp_path):
    config = Config(data_dir=tmp_path)
    store = SettingsStore(config)
    assert store.save({"fronius_ip": "5.6.7.8", "unknown": 1, "update_interval": None})
    assert json.loads(config.settings_file.read_text()) == {"fronius_ip": "5.6.7.8"}

    fresh = Config(data_dir=tmp_path)
    SettingsStore(fresh).load()
    assert fresh.fronius_ip == "5.6.7.8"


def test_empty_or_broken_settings_file_is_ignored(tmp_path):
    config = Config(data_dir=tmp_path)
    config.settings_file.write_text("")
    SettingsStore(config).load()
    config.settings_file.write_text("{kaputt")
    SettingsStore(config).load()
    assert config.fronius_ip == "192.168.178.90"
