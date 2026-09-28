"""
Live-Ansicht im Terminal - nur, wenn SolarFlow in einem echten Terminal läuft.
"""

from datetime import datetime
from typing import Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .devices import DeviceState
from .fronius import SolarData
from .monitor import Monitor
from .stats import build_statistics

_STATE = {
    DeviceState.ON: "[bold green]EIN[/]",
    DeviceState.OFF: "[dim]AUS[/]",
    DeviceState.BLOCKED: "[yellow]GESPERRT[/]",
    DeviceState.UNREACHABLE: "[red]NICHT ERREICHBAR[/]",
}


class LiveDisplay:
    """Zeigt Leistung, Tageswerte und Geräte; Logmeldungen laufen darüber durch."""

    def __init__(self) -> None:
        self.console = Console()
        self._live = Live(Panel("Warte auf den ersten Messwert ..."), console=self.console,
                          refresh_per_second=2)

    def start(self) -> None:
        self._live.start()

    def stop(self) -> None:
        self._live.stop()

    def update(self, monitor: Monitor, data: Optional[SolarData]) -> None:
        data = data or monitor.latest
        if data is None:
            return
        self._live.update(self._render(monitor, data))

    def _render(self, monitor: Monitor, data: SolarData) -> Panel:
        power = Table.grid(padding=(0, 2))
        power.add_column(style="cyan")
        power.add_column(justify="right")
        power.add_row("PV-Erzeugung", f"{data.pv_power:,.0f} W")
        power.add_row("Hausverbrauch", f"{data.load_power:,.0f} W")
        if data.grid_power < 0:
            power.add_row("Einspeisung", f"[green]{data.feed_in_power:,.0f} W[/]")
        else:
            power.add_row("Netzbezug", f"[red]{data.grid_consumption:,.0f} W[/]")
        if data.has_battery:
            direction = "lädt" if data.battery_charging else "entlädt"
            power.add_row("Akku", f"{data.battery_soc:.0f} % ({direction} {abs(data.battery_power):,.0f} W)")
        power.add_row("Autarkie", f"{data.autarky_rate:.0f} %")

        today = build_statistics(monitor.db, monitor.config, "day")
        energy = today["energy"]
        totals = Table.grid(padding=(0, 2))
        totals.add_column(style="cyan")
        totals.add_column(justify="right")
        totals.add_row("PV heute", f"{energy['pv']:.1f} kWh")
        totals.add_row("Verbrauch", f"{energy['load']:.1f} kWh")
        totals.add_row("Einspeisung", f"{energy['feed_in']:.1f} kWh")
        totals.add_row("Netzbezug", f"{energy['grid']:.1f} kWh")
        totals.add_row("Ersparnis", f"{today['costs']['benefit']:.2f} €")

        devices = Table(box=None, pad_edge=False)
        for column in ("Gerät", "Prio", "Leistung", "Status", "Laufzeit"):
            devices.add_column(column, justify="right" if column in ("Leistung", "Laufzeit") else "left")
        now = datetime.now()
        with monitor.controller.lock:
            for device in monitor.controller.store.all():
                minutes = int(device.runtime_today(now) // 60)
                devices.add_row(device.name, str(device.priority), f"{device.power_consumption:.0f} W",
                                _STATE[device.state], f"{minutes // 60}h {minutes % 60:02d}m")

        hue = monitor.controller.hue_status()
        hue_line = ("[green]Hue verbunden[/]" if hue["connected"]
                    else f"[red]{hue['error'] or 'Hue nicht verbunden'}[/]" if hue["enabled"]
                    else "[dim]Hue-Steuerung deaktiviert[/]")

        return Panel(
            Group(_side_by_side(power, totals), "", hue_line, devices),
            title=f"[bold]SolarFlow[/] · {data.timestamp:%H:%M:%S}",
            subtitle=f"Dashboard: http://localhost:{monitor.config.port}",
            border_style="blue",
        )


def _side_by_side(left: Table, right: Table) -> Table:
    grid = Table.grid(padding=(0, 6))
    grid.add_row(left, right)
    return grid
