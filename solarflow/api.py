"""
REST-API und Auslieferung des Web-Dashboards.
"""

import logging
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import List, Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import __version__
from .devices import Device, DeviceState
from .monitor import Monitor
from .stats import build_statistics

logger = logging.getLogger(__name__)


def _frontend_dir() -> Path:
    # Als PyInstaller-Bundle liegt das Frontend im entpackten Temp-Verzeichnis
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "frontend"


class SettingsUpdate(BaseModel):
    fronius_ip: Optional[str] = None
    update_interval: Optional[int] = Field(None, ge=1, le=3600)
    enable_hue: Optional[bool] = None
    hue_bridge_ip: Optional[str] = None
    hysteresis_minutes: Optional[int] = Field(None, ge=0, le=1440)
    manual_override_minutes: Optional[int] = Field(None, ge=0, le=1440)
    min_battery_soc_on: Optional[float] = Field(None, ge=0, le=100)
    min_battery_soc_off: Optional[float] = Field(None, ge=0, le=100)
    electricity_price: Optional[float] = Field(None, ge=0, le=10)
    electricity_price_night: Optional[float] = Field(None, ge=0, le=10)
    feed_in_tariff: Optional[float] = Field(None, ge=0, le=10)
    night_tariff_start: Optional[int] = Field(None, ge=0, le=23)
    night_tariff_end: Optional[int] = Field(None, ge=0, le=23)

    @field_validator("fronius_ip", "hue_bridge_ip")
    @classmethod
    def host_is_plausible(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        host = value.strip()
        if not host or "/" in host or " " in host:
            raise ValueError("Adresse muss ein Hostname oder eine IP ohne Protokoll sein")
        return host


class DeviceIn(BaseModel):
    name: str
    description: str = ""
    power_consumption: float = Field(gt=0)
    priority: int = Field(ge=1, le=10)
    switch_on_threshold: float = Field(ge=0)
    switch_off_threshold: float = Field(ge=0)
    min_runtime: int = Field(0, ge=0, le=1440)
    max_runtime_per_day: int = Field(0, ge=0, le=1440)
    allowed_time_ranges: List[List[str]] = []

    @model_validator(mode="after")
    def thresholds_ordered(self) -> "DeviceIn":
        if self.switch_off_threshold > self.switch_on_threshold:
            raise ValueError("Ausschalt-Schwellwert darf nicht höher als der Einschalt-Schwellwert sein")
        return self

    def to_device(self) -> Device:
        try:
            return Device.from_dict(self.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None


class SwitchRequest(BaseModel):
    on: bool


def create_app(monitor: Monitor) -> FastAPI:
    app = FastAPI(title="SolarFlow API", version=__version__,
                  description="Smart Energy Management für Fronius und Philips Hue")
    controller = monitor.controller
    store = controller.store
    config = monitor.config

    @app.middleware("http")
    async def revalidate_frontend(request, call_next):
        """Nach einem Update soll der Browser nicht die alte Oberfläche aus dem Cache zeigen."""
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/status")
    def status():
        return {"status": "online", "version": __version__, "timestamp": datetime.now().isoformat()}

    @app.get("/api/current")
    def current():
        data = monitor.current()
        if data is None:
            raise HTTPException(status_code=503, detail="Noch keine Daten vom Wechselrichter")
        return data

    @app.get("/api/stats")
    def stats(period: Literal["day", "week", "month", "year", "all"] = "day", ref: Optional[date] = None):
        return build_statistics(monitor.db, config, period, ref)

    @app.get("/api/stats/export")
    def export_stats(period: Literal["day", "week", "month", "year", "all"] = "day", ref: Optional[date] = None):
        """Die Balken eines Zeitraums als CSV (Semikolon, deutsches Dezimalkomma)."""
        data = build_statistics(monitor.db, config, period, ref)
        columns = ("pv", "load", "self_consumption", "feed_in", "grid", "battery_charge", "battery_discharge")
        lines = ["Beginn;PV (kWh);Verbrauch (kWh);Selbst gedeckt (kWh);Einspeisung (kWh);Netzbezug (kWh);"
                 "Akku geladen (kWh);Akku entladen (kWh);Nutzen (EUR)"]
        for row in data["series"]:
            if row["pv"] is None:
                continue
            values = [row[c] for c in columns] + [row["benefit"]]
            lines.append(";".join([row["start"]] + [f"{v:.3f}".replace(".", ",") for v in values]))
        filename = f"solarflow_{period}_{data['period']['start'][:10]}.csv"
        return Response("\n".join(lines) + "\n", media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/settings")
    def get_settings():
        return config.editable()

    @app.put("/api/settings")
    def update_settings(update: SettingsUpdate):
        changes = update.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(status_code=400, detail="Keine Änderungen übergeben")
        soc_on = changes.get("min_battery_soc_on", config.min_battery_soc_on)
        soc_off = changes.get("min_battery_soc_off", config.min_battery_soc_off)
        if soc_off > soc_on:
            raise HTTPException(status_code=422,
                                detail="Ausschalt-Ladestand darf nicht höher als der Einschalt-Ladestand sein")
        if not monitor.apply_settings(changes):
            raise HTTPException(status_code=500, detail="Einstellungen konnten nicht gespeichert werden")
        return {"message": "Einstellungen gespeichert", "settings": config.editable()}

    @app.get("/api/hue")
    def hue():
        bridge = controller.bridge
        return {**controller.hue_status(), "lights": bridge.light_names() if bridge else []}

    def device_view(device: Device, now: datetime) -> dict:
        hysteresis = config.hysteresis_minutes * 60
        waiting = None
        if device.state == DeviceState.OFF and device.last_switch_off:
            remaining = hysteresis - (now - device.last_switch_off).total_seconds()
            waiting = int(remaining) if remaining > 0 else None
        return {
            **device.to_dict(),
            "state": device.state.value,
            "runtime_today": int(device.runtime_today(now) // 60),
            "manual_remaining": int((device.manual_until - now).total_seconds()) if device.is_manual(now) else None,
            "hysteresis_remaining": waiting,
            "hint": controller.unreachable_reason(device) or controller.hints.get(device.name),
        }

    @app.get("/api/devices")
    def devices():
        now = datetime.now()
        with controller.lock:
            items = [device_view(d, now) for d in store.all()]
        return {
            "hue": controller.hue_status(),
            "devices": items,
            "total_consumption": sum(d["power_consumption"] for d in items if d["state"] == "on"),
        }

    @app.get("/api/devices/events")
    def events(limit: int = Query(30, ge=1, le=500)):
        return monitor.db.recent_events(limit)

    def require(name: str) -> Device:
        device = store.get(name)
        if device is None:
            raise HTTPException(status_code=404, detail=f"Gerät '{name}' nicht gefunden")
        return device

    @app.post("/api/devices/{name:path}/switch")
    def switch(name: str, request: SwitchRequest):
        device = require(name)
        reason = controller.unreachable_reason(device)
        if reason:
            raise HTTPException(status_code=409, detail=reason)
        if not controller.switch_manually(name, request.on):
            raise HTTPException(status_code=502, detail="Die Hue Bridge hat den Schaltbefehl nicht angenommen")
        return {"message": f"'{name}' {'eingeschaltet' if request.on else 'ausgeschaltet'}",
                "state": device.state.value}

    @app.delete("/api/devices/{name:path}/manual")
    def release_manual(name: str):
        require(name)
        controller.release_manual(name)
        return {"message": f"'{name}' wird wieder automatisch gesteuert"}

    @app.post("/api/devices", status_code=201)
    def create_device(payload: DeviceIn):
        device = payload.to_device()
        with controller.lock:
            try:
                store.add(device)
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
            if not store.save():
                store.remove(device.name)
                raise HTTPException(status_code=500, detail="devices.json konnte nicht geschrieben werden")
            controller.track(device.name)
        return {"message": f"Gerät '{device.name}' angelegt"}

    @app.put("/api/devices/{name:path}")
    def update_device(name: str, payload: DeviceIn):
        old = require(name)
        device = payload.to_device()
        with controller.lock:
            if device.name != name and old.state == DeviceState.ON:
                raise HTTPException(status_code=409, detail="Zum Umbenennen muss das Gerät ausgeschaltet sein")
            try:
                store.replace(name, device)
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
            if not store.save():
                store.replace(device.name, old)
                raise HTTPException(status_code=500, detail="devices.json konnte nicht geschrieben werden")
            if device.name != name:
                controller.track(device.name)
        return {"message": f"Gerät '{device.name}' gespeichert"}

    @app.delete("/api/devices/{name:path}")
    def delete_device(name: str):
        device = require(name)
        with controller.lock:
            if device.state == DeviceState.ON and not controller.switch_manually(name, False):
                raise HTTPException(status_code=409, detail="Gerät läuft und ließ sich nicht ausschalten")
            store.remove(name)
            if not store.save():
                store.add(device)
                raise HTTPException(status_code=500, detail="devices.json konnte nicht geschrieben werden")
        return {"message": f"Gerät '{name}' entfernt"}

    frontend = _frontend_dir()
    if (frontend / "index.html").is_file():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    else:
        logger.warning(f"Kein Frontend unter {frontend} gefunden - nur die API ist verfügbar")

    return app


class APIServer:
    """Betreibt uvicorn in einem Hintergrund-Thread."""

    def __init__(self, monitor: Monitor, port: int):
        self.server = uvicorn.Server(uvicorn.Config(
            create_app(monitor), host="0.0.0.0", port=port, log_level="warning", access_log=False
        ))
        self.thread = threading.Thread(target=self.server.run, name="api", daemon=True)

    def start(self, timeout: float = 10.0) -> None:
        """
        Startet den Server und wartet, bis er Anfragen annimmt.

        Raises:
            RuntimeError: wenn der Server nicht hochkommt (z.B. Port belegt)
        """
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"Dashboard-Server konnte Port {self.server.config.port} nicht öffnen")
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
