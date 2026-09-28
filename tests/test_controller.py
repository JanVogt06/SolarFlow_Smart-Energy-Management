from datetime import datetime, time, timedelta

import pytest

from conftest import FakeBridge, make_device, sample
from solarflow.devices import DeviceState

T0 = datetime(2026, 6, 1, 12, 0)


def setup_devices(controller, store, bridge, *devices, hw_on=()):
    for device in devices:
        store.add(device)
        bridge.add(device.name, on=device.name in hw_on)
    controller.restore(T0 - timedelta(hours=1))


def export(watts: float, when: datetime, **extra):
    """Messwert mit `watts` Einspeisung."""
    return sample(when, pv=watts + 500, grid=-watts, load=500, **extra)


def states(store):
    return {d.name: d.state for d in store.all()}


# --- Fehler 1: keine Verbindung zur Bridge -> kein virtuelles Schalten ---------

def test_unreachable_bridge_never_switches_virtually(controller, store, bridge, db):
    setup_devices(controller, store, bridge, make_device("Heizung"))
    bridge.online = False

    for i in range(5):
        controller.cycle(export(5000, T0 + timedelta(seconds=5 * i)))

    assert bridge.commands == []
    assert store.get("Heizung").state == DeviceState.UNREACHABLE
    assert "nicht erreichbar" in controller.unreachable_reason(store.get("Heizung"))
    assert [e["new_state"] for e in db.events(until=T0 + timedelta(hours=1))] == ["unreachable"]


def test_unreachable_bridge_at_start_marks_running_device_unreachable(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("Heizung"), hw_on=["Heizung"])
    controller.cycle(export(5000, T0))
    assert store.get("Heizung").state == DeviceState.ON

    bridge.online = False
    for i in range(1, 4):
        controller.cycle(export(0, T0 + timedelta(seconds=5 * i)))

    assert store.get("Heizung").state == DeviceState.UNREACHABLE
    assert bridge.commands == []


def test_short_bridge_hiccup_holds_state_without_switching(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("Heizung"))
    controller.cycle(export(5000, T0))
    assert store.get("Heizung").state == DeviceState.ON

    bridge.online = False
    controller.cycle(export(0, T0 + timedelta(minutes=10)))

    assert store.get("Heizung").state == DeviceState.ON
    assert bridge.commands == [("Heizung", True)]


def test_reconnected_bridge_state_is_adopted_without_manual_override(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("Heizung"))
    bridge.online = False
    for i in range(3):
        controller.cycle(export(0, T0 + timedelta(seconds=5 * i)))

    bridge.online = True
    bridge.add("Heizung", on=True)  # wurde währenddessen in der Hue-App eingeschaltet
    controller.cycle(export(5000, T0 + timedelta(minutes=1)))

    heater = store.get("Heizung")
    assert heater.state == DeviceState.ON
    assert heater.manual_until is None


def test_hue_disabled_never_switches(config, controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("Heizung"))
    config.enable_hue = False
    controller.configure_bridge()

    controller.cycle(export(5000, T0))

    assert controller.bridge is None
    assert store.get("Heizung").state != DeviceState.ON
    assert bridge.commands == []
    assert controller.unreachable_reason(store.get("Heizung")) == "Hue-Steuerung ist deaktiviert"


def test_device_missing_in_hue_is_unreachable(controller, store, bridge):
    store.add(make_device("Tippfehler"))
    controller.restore(T0)
    controller.cycle(export(5000, T0))

    assert store.get("Tippfehler").state == DeviceState.UNREACHABLE
    assert "kein Gerät" in controller.unreachable_reason(store.get("Tippfehler"))


def test_unplugged_device_is_unreachable_while_others_work(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A", priority=1), make_device("B", priority=2))
    bridge.add("B", reachable=False)
    bridge.lights["B"] = bridge.lights["B"]._replace(light_id="2")

    controller.cycle(export(5000, T0))

    assert states(store) == {"A": DeviceState.ON, "B": DeviceState.UNREACHABLE}


# --- Verteilung des Überschusses --------------------------------------------

def test_running_device_power_is_not_offered_to_others(controller, store, bridge):
    """Regression: früher galt der Verbrauch laufender Geräte als frei verfügbar."""
    jan = make_device("Jan", power=2000, priority=1, on=2200, off=1800)
    wohnzimmer = make_device("Wohnzimmer", power=2000, priority=2, on=2200, off=1800)
    setup_devices(controller, store, bridge, jan, wohnzimmer, hw_on=["Jan"])

    controller.cycle(export(300, T0))

    assert states(store) == {"Jan": DeviceState.ON, "Wohnzimmer": DeviceState.OFF}
    assert bridge.commands == []


def test_surplus_is_shared_by_priority(controller, store, bridge):
    setup_devices(controller, store, bridge,
                  make_device("A", power=2000, priority=1, on=2200, off=1800),
                  make_device("B", power=2000, priority=2, on=2200, off=1800),
                  make_device("C", power=200, priority=3, on=250, off=150))

    controller.cycle(export(4500, T0))

    # A: 4500 -> 2500 übrig, B: 2500 -> 500 übrig, C: 500 -> 300 übrig
    assert set(states(store).values()) == {DeviceState.ON}


def test_switch_off_below_threshold_lowest_priority_first(controller, store, bridge):
    setup_devices(controller, store, bridge,
                  make_device("A", power=1000, priority=1, on=1100, off=900),
                  make_device("B", power=1000, priority=2, on=1100, off=900),
                  hw_on=["A", "B"])

    # 200 W Netzbezug: ohne B blieben 800 W < 900 W -> B aus;
    # danach A: 800 W + 1000 W = 1800 W >= 900 W -> bleibt
    controller.cycle(sample(T0, pv=1800, grid=200, load=2000))

    assert states(store) == {"A": DeviceState.ON, "B": DeviceState.OFF}


def test_battery_discharge_is_no_surplus(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A", power=1000, on=1100, off=900), hw_on=["A"])

    controller.cycle(sample(T0, pv=0, grid=0, battery=1000, load=1000, soc=80))

    assert store.get("A").state == DeviceState.OFF


def test_preemption_of_lower_priority_device(controller, store, bridge):
    setup_devices(controller, store, bridge,
                  make_device("Wichtig", power=2000, priority=1, on=2200, off=1800),
                  make_device("Unwichtig", power=2000, priority=5, on=2200, off=100),
                  hw_on=["Unwichtig"])

    controller.cycle(export(300, T0))

    assert states(store) == {"Wichtig": DeviceState.ON, "Unwichtig": DeviceState.OFF}
    assert bridge.commands == [("Unwichtig", False), ("Wichtig", True)]


def test_no_preemption_when_it_would_not_suffice(controller, store, bridge):
    setup_devices(controller, store, bridge,
                  make_device("Wichtig", power=2000, priority=1, on=2200, off=1800),
                  make_device("Klein", power=200, priority=5, on=250, off=0),
                  hw_on=["Klein"])

    controller.cycle(export(300, T0))

    assert states(store) == {"Wichtig": DeviceState.OFF, "Klein": DeviceState.ON}
    assert "Überschuss" in controller.hints["Wichtig"]


def test_min_runtime_delays_switch_off(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A", min_runtime=30))
    controller.cycle(export(2000, T0))
    assert store.get("A").state == DeviceState.ON

    controller.cycle(sample(T0 + timedelta(minutes=10), pv=0, grid=500, load=1500))
    assert store.get("A").state == DeviceState.ON
    assert "Mindestlaufzeit" in controller.hints["A"]

    controller.cycle(sample(T0 + timedelta(minutes=31), pv=0, grid=500, load=1500))
    assert store.get("A").state == DeviceState.OFF


def test_hysteresis_prevents_quick_restart(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A"), hw_on=["A"])
    controller.cycle(sample(T0, grid=500, load=1500))
    assert store.get("A").state == DeviceState.OFF

    controller.cycle(export(2000, T0 + timedelta(minutes=2)))
    assert store.get("A").state == DeviceState.OFF

    controller.cycle(export(2000, T0 + timedelta(minutes=6)))
    assert store.get("A").state == DeviceState.ON


def test_time_window_blocks_and_forces_off(controller, store, bridge):
    device = make_device("A", allowed_time_ranges=[(time(6), time(12, 30))], min_runtime=120)
    setup_devices(controller, store, bridge, device)

    controller.cycle(export(2000, T0))
    assert store.get("A").state == DeviceState.ON

    # Zeitfenster zu Ende: aus, obwohl die Mindestlaufzeit noch nicht erreicht ist
    controller.cycle(export(2000, T0 + timedelta(minutes=31)))
    assert store.get("A").state == DeviceState.BLOCKED


def test_time_window_over_midnight():
    device = make_device("A", allowed_time_ranges=[(time(22), time(2))])
    assert device.is_time_allowed(datetime(2026, 6, 1, 23, 0))
    assert device.is_time_allowed(datetime(2026, 6, 2, 1, 59))
    assert not device.is_time_allowed(datetime(2026, 6, 2, 2, 0))
    assert not device.is_time_allowed(datetime(2026, 6, 2, 12, 0))


def test_max_runtime_per_day(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A", max_runtime_per_day=60))
    controller.cycle(export(2000, T0))
    controller.cycle(export(1000, T0 + timedelta(minutes=61)))

    assert store.get("A").state == DeviceState.BLOCKED
    assert controller.hints["A"] == "Tageslaufzeit erreicht"


def test_battery_soc_thresholds(config, controller, store, bridge):
    config.min_battery_soc_on, config.min_battery_soc_off = 95, 20
    setup_devices(controller, store, bridge, make_device("A"))

    controller.cycle(export(3000, T0, soc=90))
    assert store.get("A").state == DeviceState.OFF
    assert "wartet auf Akku" in controller.hints["A"]

    controller.cycle(export(3000, T0 + timedelta(seconds=5), soc=96))
    assert store.get("A").state == DeviceState.ON

    controller.cycle(export(3000, T0 + timedelta(seconds=10), soc=19))
    assert store.get("A").state == DeviceState.OFF


# --- Manuell und extern ------------------------------------------------------

def test_external_switch_pauses_automation(config, controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A"))
    controller.cycle(export(0, T0))

    bridge.add("A", on=True)  # in der Hue-App eingeschaltet
    bridge.lights["A"] = bridge.lights["A"]._replace(light_id="1")
    controller.cycle(export(0, T0 + timedelta(minutes=1)))

    device = store.get("A")
    assert device.state == DeviceState.ON
    assert device.is_manual(T0 + timedelta(minutes=2))

    controller.cycle(export(0, T0 + timedelta(minutes=5)))
    assert device.state == DeviceState.ON  # Automatik pausiert trotz fehlendem Überschuss

    controller.cycle(sample(T0 + timedelta(minutes=32), grid=300, load=1300))
    assert device.state == DeviceState.OFF


def test_manual_switch_and_release(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A"))
    controller.cycle(export(0, T0))

    assert controller.switch_manually("A", True, T0 + timedelta(seconds=1))
    controller.cycle(export(0, T0 + timedelta(seconds=30)))
    assert store.get("A").state == DeviceState.ON

    controller.release_manual("A")
    controller.cycle(sample(T0 + timedelta(seconds=35), grid=300, load=1300))
    assert store.get("A").state == DeviceState.OFF


def test_rejected_command_changes_nothing(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A"))
    bridge.accept = False

    controller.cycle(export(5000, T0))
    assert store.get("A").state == DeviceState.OFF
    assert not controller.switch_manually("A", True, T0)
    assert store.get("A").state == DeviceState.OFF


def test_bridge_latency_is_not_taken_as_external_switch(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A"))
    controller.cycle(export(2000, T0))
    bridge.lights["A"] = bridge.lights["A"]._replace(on=False)  # Bridge zeigt noch den alten Zustand

    controller.cycle(export(2000, T0 + timedelta(seconds=5)))
    assert store.get("A").state == DeviceState.ON
    assert store.get("A").manual_until is None


# --- Laufzeit und Neustart ---------------------------------------------------

def test_restart_restores_runtime_and_state(controller, store, bridge, db):
    setup_devices(controller, store, bridge, make_device("A"), hw_on=["A"])
    day = datetime(2026, 6, 1)
    for hour, minute, state in ((8, 0, "on"), (9, 30, "off"), (10, 0, "on")):
        db.insert_event(day.replace(hour=hour, minute=minute), "A", "x", "", state, "", 0, 1000, 5, 0)

    controller.restore(day.replace(hour=10, minute=30))
    device = store.get("A")

    assert device.state == DeviceState.ON
    assert device.on_since == day.replace(hour=10)
    assert device.runtime_today(day.replace(hour=10, minute=30)) == pytest.approx(2 * 3600)
    assert device.last_switch_off == day.replace(hour=9, minute=30)

    controller.cycle(export(2000, day.replace(hour=10, minute=31)))
    assert device.state == DeviceState.ON and bridge.commands == []


def test_runtime_is_split_at_midnight(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A"))
    evening = datetime(2026, 6, 1, 23, 0)
    controller.cycle(export(2000, evening))
    controller.cycle(export(2000, evening + timedelta(hours=1, minutes=30)))

    assert store.get("A").runtime_today(evening + timedelta(hours=1, minutes=30)) == pytest.approx(30 * 60)


def test_shutdown_switches_everything_off_and_logs(controller, store, bridge, db):
    setup_devices(controller, store, bridge, make_device("A"), make_device("B"))
    controller.cycle(export(5000, T0))
    controller.switch_all_off(T0 + timedelta(minutes=10), "Programmende")

    assert set(states(store).values()) == {DeviceState.OFF}
    last = db.recent_events(2)
    assert {e["reason"] for e in last} == {"Programmende"}


def test_data_loss_switches_automatic_devices_off(config, controller, store, bridge, db):
    from solarflow.monitor import Monitor

    class Silent:
        ip_address = "x"

        def fetch(self):
            return None

    setup_devices(controller, store, bridge, make_device("Auto"), make_device("Hand"))
    controller.cycle(export(5000, T0))
    controller.switch_manually("Hand", True, T0)
    monitor = Monitor(config, None, db, controller, Silent())
    monitor._last_data_at = T0

    monitor.tick(T0 + timedelta(minutes=1))
    assert store.get("Auto").state == DeviceState.ON
    monitor.tick(T0 + timedelta(minutes=3))
    assert store.get("Auto").state == DeviceState.OFF
    assert store.get("Hand").state == DeviceState.ON


def test_day_rolls_over_even_without_solar_data(controller, store, bridge):
    setup_devices(controller, store, bridge, make_device("A"))
    controller.cycle(export(2000, T0))
    controller.cycle(sample(T0 + timedelta(hours=1), grid=500, load=1500))  # aus nach 1 h
    assert store.get("A").runtime_today_seconds == pytest.approx(3600)

    with controller.lock:
        controller.sync(T0 + timedelta(days=1))
    assert store.get("A").runtime_today_seconds == 0
