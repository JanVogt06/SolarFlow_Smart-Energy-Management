"""
Statistik über beliebige Zeiträume - berechnet aus den gespeicherten Messwerten.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import Config
from .database import TIME_FORMAT, Database

PERIODS = ("day", "week", "month", "year", "all")

_MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
           "August", "September", "Oktober", "November", "Dezember")
_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

_ENERGY_KEYS = ("pv", "load", "feed_in", "grid", "battery_charge", "battery_discharge", "self_consumption")


@dataclass(frozen=True)
class Period:
    """Ein Auswertungszeitraum und wie er in Balken aufgeteilt wird."""

    kind: str
    start: datetime
    end: datetime
    label: str
    bucket: str  # "hour", "day", "month" oder "year"
    previous: Optional[date]
    next: Optional[date]


def _month_start(day: date, offset: int = 0) -> date:
    month = day.month - 1 + offset
    return date(day.year + month // 12, month % 12 + 1, 1)


def _as_datetime(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def resolve_period(kind: str, ref: date, today: date, first: Optional[date]) -> Period:
    """
    Bestimmt Beginn, Ende und Beschriftung eines Zeitraums.

    Args:
        kind: day, week, month, year oder all
        ref: Ein Tag innerhalb des gewünschten Zeitraums
        today: Heutiges Datum (begrenzt das Blättern in die Zukunft)
        first: Tag des ersten Messwerts (begrenzt das Blättern in die Vergangenheit)
    """
    if kind not in PERIODS:
        raise ValueError(f"Unbekannter Zeitraum: {kind}")

    if kind == "all":
        start = first or today
        months = (today.year - start.year) * 12 + today.month - start.month
        return Period(kind, _as_datetime(start), _as_datetime(today + timedelta(days=1)),
                      f"Gesamt seit {start:%d.%m.%Y}", "month" if months < 24 else "year", None, None)

    if kind == "day":
        start, end = ref, ref + timedelta(days=1)
        label = f"{_WEEKDAYS[ref.weekday()]}, {ref.day}. {_MONTHS[ref.month - 1]} {ref.year}"
        bucket, prev_ref = "hour", ref - timedelta(days=1)
    elif kind == "week":
        start = ref - timedelta(days=ref.weekday())
        end = start + timedelta(days=7)
        last = end - timedelta(days=1)
        label = f"KW {start.isocalendar()[1]} · {start:%d.%m.} – {last:%d.%m.%Y}"
        bucket, prev_ref = "day", start - timedelta(days=7)
    elif kind == "month":
        start, end = _month_start(ref), _month_start(ref, 1)
        label = f"{_MONTHS[start.month - 1]} {start.year}"
        bucket, prev_ref = "day", _month_start(ref, -1)
    else:
        start, end = date(ref.year, 1, 1), date(ref.year + 1, 1, 1)
        label = str(ref.year)
        bucket, prev_ref = "month", date(ref.year - 1, 1, 1)

    previous = prev_ref if first is not None and prev_ref < start and first < start else None
    following = end if end <= today else None
    return Period(kind, _as_datetime(start), _as_datetime(end), label, bucket, previous, following)


def _bucket_starts(period: Period) -> List[datetime]:
    starts, cursor = [], period.start
    while cursor < period.end:
        starts.append(cursor)
        if period.bucket == "hour":
            cursor += timedelta(hours=1)
        elif period.bucket == "day":
            cursor += timedelta(days=1)
        elif period.bucket == "month":
            cursor = _as_datetime(_month_start(cursor.date(), 1))
        else:
            cursor = datetime(cursor.year + 1, 1, 1)
    return starts


def _bucket_key(period: Period, hour: datetime) -> datetime:
    if period.bucket == "hour":
        return hour
    if period.bucket == "day":
        return datetime(hour.year, hour.month, hour.day)
    if period.bucket == "month":
        return datetime(hour.year, hour.month, 1)
    return datetime(hour.year, 1, 1)


def _bucket_label(period: Period, start: datetime) -> str:
    if period.bucket == "hour":
        return f"{start:%H}"
    if period.bucket == "day":
        return f"{start.day}." if period.kind == "month" else _WEEKDAYS[start.weekday()][:2]
    if period.bucket == "month":
        return _MONTHS[start.month - 1][:3] + ("" if period.kind == "year" else f" {start:%y}")
    return str(start.year)


class _Totals:
    """Summiert Stundenwerte und rechnet sie in kWh und Euro um."""

    def __init__(self) -> None:
        self.wh = {key: 0.0 for key in _ENERGY_KEYS}
        self.grid_night_wh = 0.0
        self.load_night_wh = 0.0
        self.seconds = 0.0
        self.pv_max: Optional[float] = None
        self.load_max: Optional[float] = None
        self.soc_min: Optional[float] = None
        self.soc_max: Optional[float] = None

    def add(self, row: Dict[str, Any], night: bool) -> None:
        for key in _ENERGY_KEYS:
            self.wh[key] += row[f"{key}_wh"] or 0.0
        if night:
            self.grid_night_wh += row["grid_wh"] or 0.0
            self.load_night_wh += row["load_wh"] or 0.0
        self.seconds += row["seconds"] or 0.0
        self.pv_max = _extreme(max, self.pv_max, row["pv_max"])
        self.load_max = _extreme(max, self.load_max, row["load_max"])
        self.soc_min = _extreme(min, self.soc_min, row["soc_min"])
        self.soc_max = _extreme(max, self.soc_max, row["soc_max"])

    def energy(self) -> Dict[str, float]:
        return {key: round(value / 1000, 3) for key, value in self.wh.items()}

    def costs(self, config: Config) -> Dict[str, float]:
        grid_day = (self.wh["grid"] - self.grid_night_wh) / 1000
        load_day = (self.wh["load"] - self.load_night_wh) / 1000
        grid_cost = grid_day * config.electricity_price + self.grid_night_wh / 1000 * config.electricity_price_night
        without_solar = load_day * config.electricity_price + self.load_night_wh / 1000 * config.electricity_price_night
        revenue = self.wh["feed_in"] / 1000 * config.feed_in_tariff
        saved = without_solar - grid_cost
        return {
            "grid_cost": round(grid_cost, 2),
            "cost_without_solar": round(without_solar, 2),
            "saved": round(saved, 2),
            "feed_in_revenue": round(revenue, 2),
            "benefit": round(saved + revenue, 2),
        }

    def summary(self, config: Config) -> Dict[str, Any]:
        wh = self.wh
        return {
            "energy": self.energy(),
            "costs": self.costs(config),
            "autarky": _percent(wh["self_consumption"], wh["load"]),
            "self_consumption_rate": _percent(wh["pv"] - wh["feed_in"], wh["pv"]),
            "pv_max": self.pv_max,
            "load_max": self.load_max,
            "soc_min": self.soc_min,
            "soc_max": self.soc_max,
            "covered_hours": round(self.seconds / 3600, 1),
        }


def _extreme(pick, current: Optional[float], value: Optional[float]) -> Optional[float]:
    if value is None:
        return current
    return value if current is None else pick(current, value)


def _percent(part: float, whole: float) -> Optional[float]:
    if whole <= 0:
        return None
    return round(min(max(part / whole * 100, 0.0), 100.0), 1)


def device_usage(events: Iterable[Dict[str, Any]], start: datetime, end: datetime,
                 now: datetime) -> List[Dict[str, Any]]:
    """
    Laufzeit, Energie und Einschaltvorgänge je Gerät im Zeitraum [start, end).

    Args:
        events: Alle Schaltereignisse vor `end`, zeitlich sortiert
    """
    stop = min(end, now)
    usage: Dict[str, Dict[str, float]] = {}
    running: Dict[str, Tuple[datetime, float]] = {}

    def close(name: str, until: datetime) -> None:
        since, power = running.pop(name)
        seconds = (min(until, stop) - max(since, start)).total_seconds()
        if seconds > 0:
            entry = usage.setdefault(name, {"seconds": 0.0, "wh": 0.0, "switches": 0})
            entry["seconds"] += seconds
            entry["wh"] += power * seconds / 3600

    for event in events:
        moment = datetime.strptime(event["timestamp"], TIME_FORMAT)
        name = event["device_name"]
        on = event["new_state"] == "on"

        if name in running and not on:
            close(name, moment)
        elif on and name not in running:
            running[name] = (moment, event["device_power"] or 0.0)
            if start <= moment < end:
                usage.setdefault(name, {"seconds": 0.0, "wh": 0.0, "switches": 0})["switches"] += 1

    for name in list(running):
        close(name, stop)

    return sorted(
        ({"name": name, "runtime_hours": round(v["seconds"] / 3600, 2),
          "energy": round(v["wh"] / 1000, 3), "switches": int(v["switches"])}
         for name, v in usage.items()),
        key=lambda item: -item["energy"]
    )


def build_statistics(db: Database, config: Config, kind: str, ref: Optional[date] = None,
                     now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Stellt die Statistik eines Zeitraums zusammen.

    Returns:
        Zeitraum, Summen, Kosten, Balken für das Diagramm und Gerätenutzung
    """
    now = now or datetime.now()
    first = db.first_sample_time()
    period = resolve_period(kind, ref or now.date(), now.date(), first.date() if first else None)

    hours = db.hourly(period.start, period.end)
    starts = _bucket_starts(period)
    buckets = {start: _Totals() for start in starts}
    totals = _Totals()
    for row in hours:
        hour = datetime.strptime(row["hour"], TIME_FORMAT)
        night = config.is_night(hour.hour)
        totals.add(row, night)
        key = _bucket_key(period, hour)
        if key in buckets:
            buckets[key].add(row, night)

    series = []
    for start in starts:
        bucket = buckets[start]
        future = start > now
        entry: Dict[str, Any] = {"start": start.isoformat(), "label": _bucket_label(period, start)}
        entry.update({key: None if future else value for key, value in bucket.energy().items()})
        entry["benefit"] = None if future else bucket.costs(config)["benefit"]
        series.append(entry)

    return {
        "period": {
            "kind": period.kind,
            "label": period.label,
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "bucket": period.bucket,
            "previous": period.previous.isoformat() if period.previous else None,
            "next": period.next.isoformat() if period.next else None,
        },
        "first_sample": first.isoformat() if first else None,
        **totals.summary(config),
        "series": series,
        "devices": device_usage(db.events(until=period.end), period.start, period.end, now),
    }
