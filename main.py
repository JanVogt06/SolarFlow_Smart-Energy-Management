"""
SolarFlow - Einstiegspunkt.

Alle Einstellungen lassen sich auch über Umgebungsvariablen setzen (siehe README)
und im Dashboard unter "Einstellungen" ändern.
"""

import argparse
import logging
import signal
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional

from solarflow import __version__
from solarflow.api import APIServer
from solarflow.config import Config, SettingsStore
from solarflow.controller import EnergyController
from solarflow.database import Database
from solarflow.devices import DeviceStore
from solarflow.fronius import FroniusClient
from solarflow.migrations import MigrationContext
from solarflow.monitor import Monitor

logger = logging.getLogger("solarflow")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SolarFlow - schaltet Hue-Geräte nach dem Solarüberschuss eines Fronius Wechselrichters",
        epilog="Beispiel: python main.py --ip 192.168.178.90 --hue-ip 192.168.178.26",
    )
    parser.add_argument("--ip", help="IP-Adresse des Fronius Wechselrichters")
    parser.add_argument("--hue-ip", help="IP-Adresse der Hue Bridge (aktiviert die Hue-Steuerung)")
    parser.add_argument("--interval", type=int, help="Abfrageintervall in Sekunden (Standard: 5)")
    parser.add_argument("--port", type=int, help="Port des Dashboards (Standard: 8000)")
    parser.add_argument("--data-dir", help="Ordner für Einstellungen, Geräte, Datenbank und Log (Standard: .)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"SolarFlow {__version__}")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    """Umgebung < settings.json < Kommandozeile."""
    config = Config.from_env()
    if args.data_dir:
        config.data_dir = Path(args.data_dir)

    SettingsStore(config).load()

    if args.ip:
        config.fronius_ip = args.ip
    if args.hue_ip:
        config.enable_hue, config.hue_bridge_ip = True, args.hue_ip
    if args.interval:
        config.update_interval = max(args.interval, 1)
    if args.port:
        config.port = args.port
    if args.log_level:
        config.log_level = args.log_level
    return config


def setup_logging(config: Config, console_handler: Optional[logging.Handler] = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handlers: List[logging.Handler] = [console_handler or logging.StreamHandler()]
    try:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(config.log_file, maxBytes=5 * 1024 * 1024,
                                            backupCount=2, encoding="utf-8"))
    except OSError as e:
        print(f"Warnung: Logdatei {config.log_file} nicht beschreibbar: {e}", file=sys.stderr)

    for handler in handlers:
        if handler is not console_handler:
            handler.setFormatter(formatter)
        root.addHandler(handler)

    # uvicorn und requests reden sonst bei jeder Anfrage mit
    for noisy in ("urllib3", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: Optional[List[str]] = None) -> int:
    started_at = datetime.now()
    logging.basicConfig(level=logging.INFO)
    config = build_config(parse_args(argv))

    live = None
    if sys.stdout.isatty():
        from rich.logging import RichHandler
        from solarflow.live_display import LiveDisplay
        live = LiveDisplay()
    setup_logging(config, RichHandler(console=live.console, show_path=False) if live else None)
    logger.info(f"SolarFlow {__version__} - Daten in {config.data_dir.resolve()}")

    store = DeviceStore(config.devices_file)
    store.load()

    legacy_dir = config.data_dir / "Datalogs"
    db = Database(config.database_file, config.update_interval)
    try:
        db.open(
            MigrationContext(legacy_log_dir=legacy_dir,
                             devices={d.name: (d.power_consumption, d.priority) for d in store.all()}),
            started_at,
            legacy_db=legacy_dir / "solar_energy.db",
        )
        db.refresh_hourly()
    except Exception:
        logger.exception("Datenbank konnte nicht geöffnet werden")
        return 1

    controller = EnergyController(config, store, db)
    controller.restore(datetime.now())
    monitor = Monitor(config, SettingsStore(config), db, controller, FroniusClient(config.fronius_ip))

    server = APIServer(monitor, config.port)
    try:
        server.start()
    except RuntimeError as e:
        logger.error(f"{e} - läuft SolarFlow schon oder ist der Port belegt?")
        db.close()
        return 1
    logger.info(f"Dashboard: http://localhost:{config.port}  ·  API: http://localhost:{config.port}/docs")

    def request_stop(signum, frame):
        logger.info("Beenden angefordert")
        monitor.stop()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    if live:
        monitor.on_update = lambda data: live.update(monitor, data)
        live.start()
    try:
        monitor.run()
    finally:
        if live:
            live.stop()
        monitor.shutdown()
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
