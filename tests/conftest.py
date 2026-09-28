import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solarflow.config import Config  # noqa: E402
from solarflow.controller import EnergyController  # noqa: E402
from solarflow.database import Database  # noqa: E402
from solarflow.devices import Device, DeviceStore  # noqa: E402
from solarflow.fronius import SolarData  # noqa: E402
from solarflow.hue import HueBridge, HueLight  # noqa: E402
from solarflow.migrations import MigrationContext  # noqa: E402


class FakeBridge:
    """Verhält sich wie HueBridge, ohne Netzwerk."""

    DOWN_AFTER = HueBridge.DOWN_AFTER

    def __init__(self, ip: str = "10.0.0.2"):
        self.ip = ip
        self.online = True
        self.connected = False
        self.failures = 0
        self.error: Optional[str] = None
        self.lights: Dict[str, HueLight] = {}
        self.commands: List[tuple] = []
        self.accept = True

    def add(self, name: str, on: bool = False, reachable: bool = True) -> None:
        self.lights[name] = HueLight(str(len(self.lights) + 1), name, on, reachable)

    def refresh(self) -> bool:
        if not self.online:
            self.failures += 1
            self.connected = False
            self.error = f"Hue Bridge {self.ip} nicht erreichbar"
            return False
        self.failures, self.connected, self.error = 0, True, None
        return True

    def light(self, name: str) -> Optional[HueLight]:
        return self.lights.get(name)

    def light_names(self) -> List[str]:
        return sorted(self.lights)

    def set_on(self, name: str, on: bool) -> bool:
        light = self.lights.get(name)
        if not self.connected or light is None or not self.accept:
            return False
        self.commands.append((name, on))
        self.lights[name] = light._replace(on=on)
        return True


def make_device(name: str, power: float = 1000, priority: int = 5, on: float = 1100,
                off: float = 900, **extra) -> Device:
    return Device(name=name, power_consumption=power, priority=priority,
                  switch_on_threshold=on, switch_off_threshold=off, **extra)


def sample(when: datetime, pv: float = 0, grid: float = 0, battery: float = 0,
           load: float = 0, soc: Optional[float] = None) -> SolarData:
    return SolarData(pv_power=pv, grid_power=grid, battery_power=battery, load_power=load,
                     battery_soc=soc, timestamp=when)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path, enable_hue=True, hue_bridge_ip="10.0.0.2",
                  hysteresis_minutes=5, manual_override_minutes=30,
                  min_battery_soc_on=0, min_battery_soc_off=0)


@pytest.fixture
def db(config: Config) -> Database:
    database = Database(config.database_file, config.update_interval)
    database.open(MigrationContext(), datetime.now())
    yield database
    database.close()


@pytest.fixture
def bridge() -> FakeBridge:
    return FakeBridge()


@pytest.fixture
def store(config: Config) -> DeviceStore:
    return DeviceStore(config.devices_file)


@pytest.fixture
def controller(config: Config, store: DeviceStore, db: Database, bridge: FakeBridge) -> EnergyController:
    ctrl = EnergyController(config, store, db, bridge_factory=lambda ip: bridge)
    ctrl.restore(datetime(2026, 6, 1, 8, 0))
    return ctrl
