from datetime import date, datetime, timedelta

import pytest

from conftest import sample
from solarflow.stats import build_statistics, device_usage, resolve_period

DAY = datetime(2026, 6, 1)


def fill(db, start: datetime, minutes: int, step: int = 5, **values):
    for i in range(minutes * 60 // step):
        db.insert_sample(sample(start + timedelta(seconds=i * step), **values))


def test_hourly_energy_integrates_power(db):
    fill(db, DAY.replace(hour=12), 60, pv=1000, grid=-600, load=400, battery=0, soc=50)

    [hour] = db.hourly(DAY.replace(hour=12), DAY.replace(hour=13))

    assert hour["samples"] == 720
    assert hour["seconds"] == pytest.approx(3600)
    assert hour["pv_wh"] == pytest.approx(1000)
    assert hour["load_wh"] == pytest.approx(400)
    assert hour["feed_in_wh"] == pytest.approx(600)
    assert hour["grid_wh"] == pytest.approx(0)
    assert hour["self_consumption_wh"] == pytest.approx(400)


def test_cached_hours_equal_live_calculation(db):
    fill(db, DAY.replace(hour=10), 150, step=30, pv=2000, grid=-500, load=1500, battery=0)
    live = db.hourly(DAY, DAY + timedelta(days=1))

    assert db.refresh_hourly(DAY.replace(hour=13)) == 2  # 10 und 11 Uhr sind abgeschlossen
    cached = db.hourly(DAY, DAY + timedelta(days=1))

    assert [round(h["pv_wh"], 6) for h in cached] == [round(h["pv_wh"], 6) for h in live]
    assert db.refresh_hourly(DAY.replace(hour=13)) == 0


def test_sample_before_a_gap_counts_one_interval(db):
    db.insert_sample(sample(DAY.replace(hour=12), pv=3600, load=0))
    db.insert_sample(sample(DAY.replace(hour=12, minute=30), pv=0, load=0))

    [hour] = db.hourly(DAY.replace(hour=12), DAY.replace(hour=13))

    assert hour["pv_wh"] == pytest.approx(db.update_interval)


def test_slow_samples_within_max_gap_count_fully(db):
    db.insert_sample(sample(DAY.replace(hour=12), pv=3600, load=0))
    db.insert_sample(sample(DAY.replace(hour=12, second=40), pv=0, load=0))

    [hour] = db.hourly(DAY.replace(hour=12), DAY.replace(hour=13))

    assert hour["pv_wh"] == pytest.approx(40)


def test_last_sample_of_an_hour_uses_its_successor(db):
    db.insert_sample(sample(DAY.replace(hour=12, minute=59, second=50), pv=3600))
    db.insert_sample(sample(DAY.replace(hour=13, minute=0, second=10), pv=0))

    first, second = db.hourly(DAY.replace(hour=12), DAY.replace(hour=14))

    assert first["seconds"] == pytest.approx(20)
    assert first["pv_wh"] == pytest.approx(20)


def test_costs_use_night_tariff(config, db):
    config.electricity_price, config.electricity_price_night, config.feed_in_tariff = 0.40, 0.30, 0.10
    fill(db, DAY.replace(hour=23), 60, grid=1000, load=1000)      # 1 kWh Nachtbezug
    fill(db, DAY.replace(hour=12), 60, pv=3000, grid=-1000, load=2000)  # 1 kWh Einspeisung

    stats = build_statistics(db, config, "day", date(2026, 6, 1), now=DAY + timedelta(days=2))

    assert stats["energy"]["grid"] == pytest.approx(1.0)
    assert stats["energy"]["load"] == pytest.approx(3.0)
    assert stats["costs"]["grid_cost"] == pytest.approx(0.30)
    assert stats["costs"]["cost_without_solar"] == pytest.approx(2 * 0.40 + 1 * 0.30)
    assert stats["costs"]["saved"] == pytest.approx(0.80)
    assert stats["costs"]["feed_in_revenue"] == pytest.approx(0.10)
    assert stats["costs"]["benefit"] == pytest.approx(0.90)
    assert stats["autarky"] == pytest.approx(66.7, abs=0.1)
    assert stats["self_consumption_rate"] == pytest.approx(66.7, abs=0.1)


def test_day_series_has_24_hours_and_hides_the_future(config, db):
    fill(db, DAY.replace(hour=8), 30, pv=500, load=500)
    stats = build_statistics(db, config, "day", now=DAY.replace(hour=9, minute=15))

    assert len(stats["series"]) == 24
    assert stats["series"][8]["pv"] == pytest.approx(0.25)
    assert stats["series"][9]["pv"] == 0
    assert stats["series"][10]["pv"] is None


def test_statistics_survive_a_restart(config, db, tmp_path):
    """Fehler 2: die Statistik kommt aus der Datenbank und nicht aus dem Speicher."""
    from solarflow.database import Database
    from solarflow.migrations import MigrationContext

    fill(db, DAY.replace(hour=12), 60, pv=1000, load=1000)
    db.close()

    reopened = Database(config.database_file, config.update_interval)
    reopened.open(MigrationContext(), datetime.now())
    stats = build_statistics(reopened, config, "all", now=DAY.replace(hour=18))

    assert stats["energy"]["pv"] == pytest.approx(1.0)
    assert stats["first_sample"] == DAY.replace(hour=12).isoformat()


@pytest.mark.parametrize("kind, ref, label, start, end, previous, following", [
    ("day", date(2026, 6, 3), "Mittwoch, 3. Juni 2026", date(2026, 6, 3), date(2026, 6, 4),
     date(2026, 6, 2), date(2026, 6, 4)),
    ("week", date(2026, 6, 3), "KW 23 · 01.06. – 07.06.2026", date(2026, 6, 1), date(2026, 6, 8),
     date(2026, 5, 25), date(2026, 6, 8)),
    ("month", date(2026, 6, 3), "Juni 2026", date(2026, 6, 1), date(2026, 7, 1),
     date(2026, 5, 1), None),
    ("year", date(2026, 6, 3), "2026", date(2026, 1, 1), date(2027, 1, 1), date(2025, 1, 1), None),
])
def test_periods(kind, ref, label, start, end, previous, following):
    period = resolve_period(kind, ref, today=date(2026, 6, 20), first=date(2025, 11, 18))

    assert period.label == label
    assert (period.start.date(), period.end.date()) == (start, end)
    assert (period.previous, period.next) == (previous, following)


def test_no_navigation_before_the_first_sample():
    period = resolve_period("day", date(2026, 6, 1), today=date(2026, 6, 1), first=date(2026, 6, 1))
    assert period.previous is None and period.next is None


def test_all_time_uses_months_then_years():
    assert resolve_period("all", date(2026, 6, 1), date(2026, 6, 1), date(2025, 11, 18)).bucket == "month"
    assert resolve_period("all", date(2026, 6, 1), date(2026, 6, 1), date(2022, 1, 1)).bucket == "year"


def test_device_usage_clips_to_the_period():
    def event(ts, state, power=2000):
        return {"timestamp": ts, "device_name": "Heizung", "new_state": state, "device_power": power}

    events = [event("2026-05-31 23:00:00", "on"),   # läuft über Mitternacht
              event("2026-06-01 01:00:00", "off"),
              event("2026-06-01 12:00:00", "on"),
              event("2026-06-01 12:30:00", "unreachable"),
              event("2026-06-01 23:30:00", "on")]  # läuft bis Periodenende

    [usage] = device_usage(events, DAY, DAY + timedelta(days=1), now=DAY + timedelta(days=2))

    assert usage["runtime_hours"] == pytest.approx(2.0)
    assert usage["energy"] == pytest.approx(4.0)
    assert usage["switches"] == 2


def test_all_time_series_includes_the_first_partial_month(config, db):
    fill(db, datetime(2025, 11, 18, 12), 60, pv=1000, load=500)
    fill(db, datetime(2025, 12, 5, 12), 60, pv=2000, load=500)

    stats = build_statistics(db, config, "all", now=datetime(2026, 1, 10))

    assert [s["label"] for s in stats["series"]] == ["Nov 25", "Dez 25", "Jan 26"]
    assert [s["pv"] for s in stats["series"]] == pytest.approx([1.0, 2.0, 0.0])
    assert sum(s["pv"] for s in stats["series"]) == pytest.approx(stats["energy"]["pv"])
