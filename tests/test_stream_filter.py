"""Per-client stream shaping (api.stream_filter) and the power summary."""

from cyberpunk_computer.api.power_summary import compute_power_summary
from cyberpunk_computer.api.stream_filter import (
    StreamSubscription, apply_merge_patch, merge_patch,
)
from cyberpunk_computer.metrics import MetricsDatabase


def env(state, ts=1.0):
    return {"type": "state", "ts": ts, "state": state}


def test_no_params_is_the_legacy_full_1hz_feed():
    sub = StreamSubscription.from_params({})
    s = {"a": 1, "b": {"c": 2}}
    assert sub.shape(env(s), 0.0) == env(s)
    assert sub.shape(env(s, 2.0), 1.0) == env(s, 2.0)


def test_keys_and_interval():
    sub = StreamSubscription.from_params({"keys": "powerbox,hosts", "interval": "5", "token": "x"})
    full = {"powerbox": {"v": 12}, "hosts": {}, "diagnostics": {"big": "x" * 100}}
    assert sub.shape(env(full), 0.0)["state"] == {"powerbox": {"v": 12}, "hosts": {}}
    assert sub.shape(env(full), 3.0) is None
    assert sub.shape(env(full), 5.0) is not None


def test_interval_is_clamped():
    assert StreamSubscription.from_params({"interval": "0.01"}).interval_s == 1.0
    assert StreamSubscription.from_params({"interval": "900"}).interval_s == 60.0
    assert StreamSubscription.from_params({"interval": "nan"}).interval_s == 1.0


def test_delta_patches_rebuild_the_selected_state():
    sub = StreamSubscription.from_params({"delta": "1"})
    states = [
        {"pb": {"v": 12.0, "relays": [1, 0], "x": None}, "hosts": {"poco": {"t": 1}}},
        {"pb": {"v": 12.1, "relays": [1, 1], "x": None}, "hosts": {"poco": {"t": 1}}},
        {"pb": {"v": 12.1, "relays": [1, 1], "x": 3}, "hosts": {"poco": None}},
        {"pb": {}, "hosts": {"poco": {"t": 2}}, "new": [1]},
    ]
    client = None
    for i, s in enumerate(states):
        msg = sub.shape(env(s), float(i))
        if "state" in msg:
            client = msg["state"]
        else:
            client = apply_merge_patch(client, msg["patch"])
        expect = {k: v for k, v in s.items()}
        # null leaves are deleted on the client
        assert apply_merge_patch({}, client) == apply_merge_patch({}, expect)
    assert sub.shape(env(states[-1]), 9.0)["patch"] == {}


def test_resubscribe_resends_full_state():
    sub = StreamSubscription.from_params({"delta": "1", "interval": "10"})
    s = {"a": 1, "b": 2}
    sub.shape(env(s), 0.0)
    sub.update({"keys": ["b"]})
    assert sub.shape(env(s), 0.5) == {"type": "state", "ts": 1.0, "state": {"b": 2}}


def test_events_always_pass():
    sub = StreamSubscription.from_params({"keys": "a", "interval": "60"})
    sub.shape(env({"a": 1}), 0.0)
    e = {"type": "event", "ts": 1.0, "name": "x", "data": {}}
    assert sub.shape(e, 0.1) == e


def test_merge_patch_minimal():
    assert merge_patch({"a": {"b": 1, "c": 2}}, {"a": {"b": 1, "c": 3}}) == {"a": {"c": 3}}
    assert merge_patch({"a": 1}, {"a": 1}) == {}


def test_power_summary(tmp_path):
    db = MetricsDatabase(str(tmp_path / "m.db"))
    conn = db.connect()
    now = 1_800_000_000.0
    samples = []
    for i in range(7200):  # 2 h at 1 Hz, stack -6 W (sign of the shunt), poco 4 W, 12.5 V
        t = now - 7200 + i
        samples += [("powerbox_power", t, -6.0), ("poco_power", t, 4.0), ("powerbox_voltage", t, 12.5)]
    db.write_samples(conn, samples)
    db.rollup(conn, now=now + 120)
    out = compute_power_summary(db, conn, now)
    assert out["stack"]["avg_1m"] == 6.0 and out["stack"]["avg_1h"] == 6.0
    assert out["poco"]["avg_5m"] == 4.0
    assert 11.5 < out["energy_24h_wh"] < 12.5
    assert out["voltage_min_24h"] == 12.5
    assert 8 <= len(out["voltage_24h"]) <= 9 and out["voltage_24h"][0][1] == 12.5
