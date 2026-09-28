from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import make_device, sample
from solarflow.api import create_app
from solarflow.config import SettingsStore
from solarflow.monitor import Monitor


class FakeFronius:
    def __init__(self):
        self.ip_address = "10.0.0.1"
        self.next = None

    def fetch(self):
        return self.next


@pytest.fixture
def app(config, db, store, controller, bridge):
    for device in (make_device("Heizung", power=2000, priority=1, on=2200, off=1800),
                   make_device("Entfeuchter", power=200, priority=3, on=250, off=150)):
        store.add(device)
        bridge.add(device.name)
    store.save()
    controller.restore(datetime.now())
    fronius = FakeFronius()
    monitor = Monitor(config, SettingsStore(config), db, controller, fronius)
    client = TestClient(create_app(monitor))
    client.monitor, client.fronius = monitor, fronius
    return client


def tick(app, **values):
    now = datetime.now().replace(microsecond=0)
    app.fronius.next = sample(now, **values)
    app.monitor.tick(now)


def test_current_needs_data_first(app):
    assert app.get("/api/current").status_code == 503
    tick(app, pv=3000, grid=-1000, load=2000)
    data = app.get("/api/current").json()
    assert data["feed_in_power"] == 1000 and data["stale"] is False


def test_devices_and_hue_status(app, bridge):
    tick(app, pv=5000, grid=-2500, load=2500)
    body = app.get("/api/devices").json()

    assert body["hue"]["connected"] is True
    assert [(d["name"], d["state"]) for d in body["devices"]] == [("Heizung", "on"), ("Entfeuchter", "on")]
    assert body["total_consumption"] == 2200

    bridge.online = False
    for _ in range(3):
        tick(app, pv=0, grid=300, load=300)
    body = app.get("/api/devices").json()
    assert body["hue"]["connected"] is False
    assert {d["state"] for d in body["devices"]} == {"unreachable"}
    assert "nicht erreichbar" in body["devices"][0]["hint"]
    assert app.post("/api/devices/Heizung/switch", json={"on": True}).status_code == 409


def test_manual_switch_and_release(app, bridge):
    tick(app, pv=0, grid=300, load=300)
    response = app.post("/api/devices/Entfeuchter/switch", json={"on": True})
    assert response.status_code == 200, response.text
    device = app.get("/api/devices").json()["devices"][1]
    assert device["state"] == "on" and device["manual_remaining"] > 0

    assert app.delete("/api/devices/Entfeuchter/manual").status_code == 200
    assert app.get("/api/devices").json()["devices"][1]["manual_remaining"] is None


def test_create_update_delete_device(app, store):
    new = {"name": "Pool", "power_consumption": 750, "priority": 6, "switch_on_threshold": 1000,
           "switch_off_threshold": 500, "allowed_time_ranges": [["10:00", "18:00"]]}
    assert app.post("/api/devices", json=new).status_code == 201
    assert app.post("/api/devices", json=new).status_code == 409

    assert app.put("/api/devices/Pool", json={**new, "priority": 2}).status_code == 200
    assert store.get("Pool").priority == 2

    bad = {**new, "switch_off_threshold": 2000}
    assert app.put("/api/devices/Pool", json=bad).status_code == 422
    assert app.post("/api/devices", json={**new, "name": "X", "allowed_time_ranges": [["25:00", "1"]]}).status_code == 400

    assert app.delete("/api/devices/Pool").status_code == 200
    assert store.get("Pool") is None
    assert app.delete("/api/devices/Pool").status_code == 404


def test_settings_roundtrip(app, config):
    assert app.get("/api/settings").json()["fronius_ip"] == config.fronius_ip

    response = app.put("/api/settings", json={"fronius_ip": "10.0.0.5", "night_tariff_start": 21})
    assert response.status_code == 200
    assert config.fronius_ip == "10.0.0.5" and config.night_tariff_start == 21
    assert app.fronius.ip_address == "10.0.0.5"

    assert app.put("/api/settings", json={"fronius_ip": "http://x/"}).status_code == 422
    assert app.put("/api/settings", json={"min_battery_soc_on": 10, "min_battery_soc_off": 50}).status_code == 422
    assert app.put("/api/settings", json={}).status_code == 400


def test_disabling_hue_switches_devices_off_first(app, config, bridge):
    tick(app, pv=5000, grid=-2500, load=2500)
    assert app.put("/api/settings", json={"enable_hue": False}).status_code == 200
    assert ("Heizung", False) in bridge.commands
    assert app.get("/api/devices").json()["hue"]["enabled"] is False


def test_stats_periods(app):
    tick(app, pv=3000, grid=-1000, load=2000)
    for period in ("day", "week", "month", "year", "all"):
        body = app.get(f"/api/stats?period={period}").json()
        assert body["period"]["kind"] == period
        assert "energy" in body and "costs" in body and body["series"]

    yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
    assert app.get(f"/api/stats?period=day&ref={yesterday}").json()["energy"]["pv"] == 0
    assert app.get("/api/stats?period=decade").status_code == 422


def test_events_endpoint(app):
    tick(app, pv=5000, grid=-2500, load=2500)
    events = app.get("/api/devices/events?limit=5").json()
    assert {e["device_name"] for e in events} == {"Heizung", "Entfeuchter"}


def test_frontend_is_served(app):
    response = app.get("/")
    assert response.status_code == 200
    assert "SolarFlow" in response.text
    assert response.headers["cache-control"] == "no-cache"


def test_stats_csv_export(app):
    tick(app, pv=3000, grid=-1000, load=2000)
    response = app.get("/api/stats/export?period=day")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.strip().split("\n")
    assert lines[0].startswith("Beginn;PV (kWh)")
    assert all(line.count(";") == 8 for line in lines)


def test_switching_is_refused_while_hue_is_disabled(app, config):
    assert app.put("/api/settings", json={"enable_hue": False}).status_code == 200
    response = app.post("/api/devices/Heizung/switch", json={"on": True})
    assert response.status_code == 409
    assert response.json()["detail"] == "Hue-Steuerung ist deaktiviert"
    assert app.get("/api/devices").json()["devices"][0]["hint"] == "Hue-Steuerung ist deaktiviert"


def test_live_display_renders(app):
    from rich.console import Console
    from solarflow.live_display import LiveDisplay

    tick(app, pv=5000, grid=-2500, load=2500, battery=-100, soc=80)
    display = LiveDisplay()
    console = Console(record=True, width=120)
    console.print(display._render(app.monitor, app.monitor.latest))
    text = console.export_text()

    assert "PV-Erzeugung" in text and "5.000 W" in text
    assert "Heizung" in text and "EIN" in text
    assert "Hue verbunden" in text


def test_device_names_with_a_slash(app, bridge):
    device = {"name": "Licht 1/2", "power_consumption": 50, "priority": 5,
              "switch_on_threshold": 100, "switch_off_threshold": 50}
    assert app.post("/api/devices", json=device).status_code == 201
    bridge.add("Licht 1/2")
    tick(app, pv=0, grid=300, load=300)

    assert app.post("/api/devices/Licht%201%2F2/switch", json={"on": True}).status_code == 200
    assert app.delete("/api/devices/Licht%201%2F2/manual").status_code == 200
    assert app.put("/api/devices/Licht%201%2F2", json={**device, "priority": 3}).status_code == 200
    assert app.delete("/api/devices/Licht%201%2F2").status_code == 200


def test_healthy_run_confirms_legacy_cleanup(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.monitor.db, "confirm_legacy_cleanup", lambda: calls.append(1) or 0)
    start = datetime.now().replace(microsecond=0)
    for minutes in (0, 5, 11, 12):
        app.fronius.next = sample(start + timedelta(minutes=minutes), pv=100, load=100)
        app.monitor.tick(start + timedelta(minutes=minutes))
    assert calls == [1]
