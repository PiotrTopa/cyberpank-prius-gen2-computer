"""Fuelling log: maths, persistence, commands, service (docs/FUEL_LOG.md)."""

import time

import pytest

from cyberpunk_computer.api.commands import CommandError, build_command, command_catalog
from cyberpunk_computer.fuel_log import model
from cyberpunk_computer.fuel_log.actions import FuelLogCommandAction
from cyberpunk_computer.fuel_log.commands import parse_ts, resolve_amounts
from cyberpunk_computer.fuel_log.service import FuelLogService
from cyberpunk_computer.fuel_log.store import FuelLogStore
from cyberpunk_computer.metrics import MetricsDatabase
from cyberpunk_computer.state.actions import Action, ActionType
from cyberpunk_computer.state.store import Store


def e(i, fuel, km, litres, full=True, price=None, est=None, ts=None):
    return {"id": i, "ts": ts if ts is not None else 1000.0 + i, "fuel": fuel, "odometer_km": km,
            "litres": litres, "full": full, "price_per_l": price, "est_total_l": est}


# ── maths ────────────────────────────────────────────────────────────────────

def test_full_to_full_with_partial_fills():
    es = [e(1, "lpg", 1000, 30), e(2, "lpg", 1200, 10, full=False), e(3, "lpg", 1400, 35)]
    seg = model.segments(es, "lpg")["segments"]
    assert len(seg) == 1
    s = seg[0]
    assert s["distance_km"] == 400 and s["litres"] == 45 and s["fills"] == 2
    assert s["l_per_100km"] == pytest.approx(11.25)


def test_fills_before_first_full_and_pending_partials():
    es = [e(1, "lpg", 900, 12, full=False), e(2, "lpg", 1000, 30), e(3, "lpg", 1300, 20, full=False)]
    r = model.segments(es, "lpg")
    assert r["segments"] == [] and r["unattributed_l"] == 12
    assert r["pending_l"] == 20 and r["pending_fills"] == 1


def test_two_fuels_rates_over_total_distance_and_combined():
    k = 0.77
    es = [
        e(1, "lpg", 1000, 40, price=3.0, est=0.0), e(2, "petrol", 1010, 30, price=6.0, est=0.8),
        e(3, "lpg", 1500, 50, price=3.2, est=40.0),          # 500 km, 50 l -> 10 l/100 km
        e(4, "lpg", 2000, 50, price=3.0, est=80.0),          # 500 km, 50 l -> 10
        e(5, "petrol", 2010, 20, price=6.5, est=80.8),       # 1000 km, 20 l -> 2
    ]
    f = model.compute_figures(es, odometer_now=2110.0, est_total_now=90.0, k=k)
    lo, po = f["lpg"]["overall"], f["petrol"]["overall"]
    assert lo["l_per_100km"] == pytest.approx(10.0) and lo["distance_km"] == 1000
    assert lo["price_per_l"] == pytest.approx(3.1)
    assert po["l_per_100km"] == pytest.approx(2.0) and po["price_per_l"] == pytest.approx(6.5)
    c = f["combined"]["overall"]
    assert not c["lower_bound"]
    assert c["petrol_eq_l_per_100km"] == pytest.approx(2.0 + k * 10.0)
    assert c["cost_per_100km"] == pytest.approx(2.0 * 6.5 + 10.0 * 3.1)
    assert c["overlap_km"] == pytest.approx(990)
    # estimator: lpg window 80 l over 1000 km = 8.0 petrol-eq l/100 km
    assert lo["est_l_per_100km"] == pytest.approx(8.0)
    assert c["measured_over_est"] == pytest.approx((2.0 + k * 10.0) / 8.0, rel=1e-3)
    # since last fill
    assert f["lpg"]["since_last"]["km"] == pytest.approx(110)
    assert f["petrol"]["since_last"]["km"] == pytest.approx(100)
    assert f["lpg"]["since_last"]["est_l"] == pytest.approx(10)


def test_rolling_last_n_and_last():
    es = [e(i, "lpg", 1000 + 500 * i, 40 + i) for i in range(6)]
    f = model.compute_figures(es, k=0.77, rolling_n=3)
    assert f["lpg"]["last"]["l_per_100km"] == pytest.approx(45 / 5)
    assert f["lpg"]["rolling"]["litres"] == pytest.approx(43 + 44 + 45)
    assert f["lpg"]["rolling"]["distance_km"] == 1500
    assert f["lpg"]["overall"]["segments"] == 5


def test_lower_bound_when_only_lpg_measured():
    es = [e(1, "lpg", 1000, 40, price=3.0), e(2, "lpg", 1400, 40, price=3.0)]
    c = model.compute_figures(es, k=0.8)["combined"]["overall"]
    assert c["lower_bound"] and c["petrol_eq_l_per_100km"] == pytest.approx(8.0)
    assert c["cost_per_100km"] == pytest.approx(30.0)
    assert model.compute_figures([], k=0.8)["combined"]["overall"]["petrol_eq_l_per_100km"] is None


def test_short_segment_has_no_rate_but_counts_in_sums():
    es = [e(1, "lpg", 1000, 40), e(2, "lpg", 1010, 1.5), e(3, "lpg", 1410, 40)]
    segs = model.segments(es, "lpg")["segments"]
    assert segs[0]["l_per_100km"] is None
    assert model.aggregate(segs)["l_per_100km"] == pytest.approx(41.5 / 410 * 100)


def test_energy_factor_env(monkeypatch):
    monkeypatch.setenv("PRIUS_LPG_PETROL_ENERGY", "0.75")
    assert model.energy_factor() == 0.75
    monkeypatch.setenv("PRIUS_LPG_PETROL_ENERGY", "7")
    assert model.energy_factor() == model.LPG_TO_PETROL_ENERGY


def test_defaults_last_price_per_fuel():
    es = [e(1, "lpg", 1000, 30, price=3.5), e(2, "lpg", 1400, 23.11, price=3.99), e(3, "petrol", 1410, 20)]
    d = model.defaults(es)
    assert d["lpg"]["price_per_l"] == 3.99 and d["lpg"]["litres"] == 23.11
    assert d["lpg"]["total"] == pytest.approx(92.21)
    assert d["petrol"]["price_per_l"] is None


# ── amounts / timestamps ─────────────────────────────────────────────────────

def test_resolve_amounts():
    assert resolve_amounts(None, 92.21, 3.99) == (23.11, 3.99, 92.21)
    assert resolve_amounts(20.0, None, 4.0) == (20.0, 4.0, 80.0)
    assert resolve_amounts(20.0, 80.0, None) == (20.0, 4.0, 80.0)
    assert resolve_amounts(20.0, None, None) == (20.0, None, None)
    for bad in ((None, 90.0, None), (None, None, 3.0), (20.0, 90.0, 4.0), (500.0, None, None)):
        with pytest.raises(ValueError):
            resolve_amounts(*bad)


def test_parse_ts_local_and_offset():
    # Warsaw is CET (UTC+1) in November, CEST (UTC+2) in summer
    assert parse_ts("2026-11-10 16:30") == parse_ts("2026-11-10T15:30:00Z")
    assert parse_ts("2026-08-10T16:30") == parse_ts("2026-08-10T16:30:00+02:00")
    with pytest.raises(ValueError):
        parse_ts(time.time() + 3600)
    with pytest.raises(ValueError):
        parse_ts("yesterday")


# ── commands ─────────────────────────────────────────────────────────────────

def test_commands_registered_and_validated():
    names = {c["name"] for c in command_catalog()}
    assert {"fuel_log_add", "fuel_log_undo", "fuel_log_edit_last"} <= names
    a = build_command("fuel_log_add", {"fuel": "LPG", "total": 92.21, "price_per_l": 3.99})
    assert isinstance(a, FuelLogCommandAction) and a.op == "add"
    assert a.params["litres"] == 23.11 and a.params["full"] is True and a.params["fuel"] == "lpg"
    a = build_command("fuel_log_add", {"fuel": "petrol", "litres": "30.5", "full": "false"})
    assert a.params["full"] is False and a.params["price_per_l"] is None
    for bad in ({}, {"fuel": "diesel", "litres": 3}, {"fuel": "lpg"}, {"fuel": "lpg", "litres": 0},
                {"fuel": "lpg", "litres": 20, "colour": "red"}, {"fuel": "lpg", "litres": 20, "ts": "soon"},
                {"fuel": "lpg", "litres": True}):
        with pytest.raises(CommandError):
            build_command("fuel_log_add", bad)
    assert build_command("fuel_log_undo", {}).op == "undo"
    with pytest.raises(CommandError):
        build_command("fuel_log_edit_last", {})
    assert build_command("fuel_log_edit_last", {"price_per_l": 4.09}).params == {"price_per_l": 4.09}


# ── persistence + service ────────────────────────────────────────────────────

def _set_vehicle(store, **fields):
    from dataclasses import replace
    store._state = replace(store._state, vehicle=replace(store._state.vehicle, **fields))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def svc(tmp_path):
    store = Store()
    _set_vehicle(store, odometer_km=300012.75)
    clock = Clock(time.time())
    s = FuelLogService(FuelLogStore(str(tmp_path / "fuel_log.db")), store, clock=clock)
    store.add_middleware(s.middleware)
    s.publish()
    return s


def test_add_undo_edit_through_the_store(svc, tmp_path):
    store = svc.store
    store.dispatch(build_command("fuel_log_add", {"fuel": "lpg", "total": 92.21, "price_per_l": 3.99}))
    fl = store.state.fuel_log
    assert fl.enabled and fl.count == 1 and fl.last_command["ok"]
    ent = fl.entries[0]
    assert ent["odometer_km"] == 300012.75 and ent["odometer_source"] == "live"
    assert ent["car"]["odometer_km"] == 300012.75 and ent["litres"] == 23.11
    assert fl.defaults["lpg"]["price_per_l"] == 3.99

    store.dispatch(build_command("fuel_log_edit_last", {"total": 100.0}))
    ent = store.state.fuel_log.entries[0]
    assert ent["total"] == 100.0 and ent["litres"] == pytest.approx(25.06)

    store.dispatch(build_command("fuel_log_edit_last", {"litres": 20.0}))
    ent = store.state.fuel_log.entries[0]
    assert ent["litres"] == 20.0 and ent["total"] == pytest.approx(79.8)

    store.dispatch(build_command("fuel_log_undo", {"id": 99}))
    assert not store.state.fuel_log.last_command["ok"] and store.state.fuel_log.count == 1
    store.dispatch(build_command("fuel_log_undo", {}))
    assert store.state.fuel_log.count == 0
    store.dispatch(build_command("fuel_log_undo", {}))
    assert store.state.fuel_log.last_command["error"] == "the log is empty"

    # persisted: a new service on the same file sees the (undone) state
    store.dispatch(build_command("fuel_log_add", {"fuel": "petrol", "litres": 30}))
    again = FuelLogStore(str(tmp_path / "fuel_log.db"))
    assert [x["fuel"] for x in again.entries()] == ["petrol"]


def test_add_without_odometer_is_refused(tmp_path):
    store = Store()
    s = FuelLogService(FuelLogStore(":memory:"), store)
    store.add_middleware(s.middleware)
    store.dispatch(build_command("fuel_log_add", {"fuel": "lpg", "litres": 20}))
    assert not store.state.fuel_log.last_command["ok"]
    store.dispatch(build_command("fuel_log_add", {"fuel": "lpg", "litres": 20, "odometer_km": 300000}))
    assert store.state.fuel_log.entries[0]["odometer_source"] == "given"


def test_back_dated_fill_uses_metrics_speed_record(tmp_path):
    now = time.time()
    mdb = MetricsDatabase(str(tmp_path / "metrics.db"))
    # 600 s at 60 km/h ending 100 s ago = 10 km; then a gap (car parked)
    conn = mdb.connect()
    mdb.write_samples(conn, [("speed", now - 1100, 0.0)]
                      + [("speed", now - 900 + i, 60.0) for i in range(600)]
                      + [("speed", now - 100 + i, 0.0) for i in range(50)])
    conn.commit()
    conn.close()
    store = Store()
    _set_vehicle(store, odometer_km=300012.75)
    s = FuelLogService(FuelLogStore(":memory:"), store, metrics_db=mdb)
    store.add_middleware(s.middleware)
    store.dispatch(build_command("fuel_log_add", {"fuel": "lpg", "total": 92.21, "price_per_l": 3.99,
                                                  "ts": now - 1000}))
    r = store.state.fuel_log.last_command
    assert r["ok"] and r["odometer_source"] == "metrics_speed"
    assert r["odometer_km"] == pytest.approx(300012.75 - 599 / 60, abs=0.01)
    ent = store.state.fuel_log.entries[0]
    assert ent["car"] is None and ent["est_total_l"] is None
    # older than the raw record -> refused unless odometer_km is given
    store.dispatch(build_command("fuel_log_add", {"fuel": "lpg", "litres": 20, "ts": now - 5 * 86400}))
    assert not store.state.fuel_log.last_command["ok"]


def test_estimator_counter_integrates_across_resets(svc):
    class S:
        class vehicle:
            trip_fuel_consumed = 0.0
            odometer_km = 300012.75
    for v in (0.0, 0.5, 1.2, 0.1, 0.4):
        S.vehicle.trip_fuel_consumed = v
        svc.observe(S)
    assert svc.est_total_l == pytest.approx(1.2 + 0.4)
    svc._clock.t += 60
    svc.observe(S)
    assert float(svc.db.kv_get("est_total_l")) == pytest.approx(1.6)


def test_state_serializes_and_roundtrips(svc):
    from cyberpunk_computer.api.serialization import serialize_state
    from cyberpunk_computer.frontend.deserialize import deserialize_state
    import json
    svc.store.dispatch(build_command("fuel_log_add", {"fuel": "lpg", "litres": 20, "price_per_l": 4}))
    d = serialize_state(svc.store.state)
    json.dumps(d)
    back = deserialize_state(d)
    assert back.fuel_log.count == 1 and back.fuel_log.entries[0]["fuel"] == "lpg"
    assert back.fuel_log.figures["energy_factor"] == model.LPG_TO_PETROL_ENERGY


def test_remote_store_translates_the_action():
    from cyberpunk_computer.frontend.remote_store import _translate
    name, params = _translate(FuelLogCommandAction("add", {"fuel": "lpg", "litres": 20.0}))
    assert name == "fuel_log_add" and build_command(name, params).params["litres"] == 20.0
    assert _translate(FuelLogCommandAction("undo"))[0] == "fuel_log_undo"
