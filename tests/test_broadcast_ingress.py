"""Broadcast CAN fast path: panda reader thread -> IngressController -> Store.

Also measures the host-side latency of that path (decode + dispatch through
the real rules engine) so the "millisecond latency" claim has a number.
"""

import statistics
import threading
import time

from cyberpunk_computer.io.factory import create_test_twin
from cyberpunk_computer.state.actions import SetVolumeAction


def frame_03b(current_tenths=-128, voltage=0xE5):
    raw = current_tenths & 0xFFF
    return bytes([(raw >> 8) & 0x0F, raw & 0xFF, 0x00, voltage, 0x00])


def frame_0b4(speed_kph):
    raw = int(round(speed_kph * 100))
    return bytes([0, 0, 0, 0, 0, (raw >> 8) & 0xFF, raw & 0xFF, 0])


def test_frame_reaches_state_without_engine_tick():
    twin = create_test_twin()
    ing = twin.ingress
    t0 = time.monotonic()
    assert ing.ingest_broadcast_can(0x03B, frame_03b(-128, 0xE5), 0, 1000.0, t0)
    energy = twin.store.state.energy
    assert abs(energy.hv_battery_current + 12.8) < 1e-6
    assert energy.hv_battery_voltage == 229
    # 0x3CB: SOC byte 3 in 0.5 % steps.
    assert ing.ingest_broadcast_can(0x3CB, bytes([100, 80, 0, 124, 20, 21, 0]), 0, 1000.1, t0 + 0.1)
    assert abs(twin.store.state.energy.battery_soc - 0.62) < 1e-6
    st = ing.stats
    assert st.broadcast_frames == 2 and st.broadcast_decoded == 2 and st.broadcast_dispatched == 2
    assert st.can_messages == 2 and st.last_can_mono == t0 + 0.1 and st.last_can_time == 1000.1


def test_identical_payload_is_deduped_within_window_only():
    twin = create_test_twin()
    ing = twin.ingress
    t0 = time.monotonic()
    assert ing.ingest_broadcast_can(0x0B4, frame_0b4(10), 0, 1.0, t0)
    assert not ing.ingest_broadcast_can(0x0B4, frame_0b4(10), 0, 1.1, t0 + 0.1)
    assert ing.stats.broadcast_deduped == 1
    # Changed payload: never deduped.
    assert ing.ingest_broadcast_can(0x0B4, frame_0b4(11), 0, 1.2, t0 + 0.2)
    # Same payload again but past the window: accepted (freshness).
    assert ing.ingest_broadcast_can(0x0B4, frame_0b4(11), 0, 1.6, t0 + 0.6)
    ing.set_broadcast_dedupe(0)
    assert ing.ingest_broadcast_can(0x0B4, frame_0b4(11), 0, 1.61, t0 + 0.61)


def test_unknown_ids_are_skipped_after_misses_and_retried():
    twin = create_test_twin()
    ing = twin.ingress
    ing.set_broadcast_dedupe(0)
    t0 = time.monotonic()
    for i in range(3):
        assert not ing.ingest_broadcast_can(0x7FF, bytes([i]), 0, 1.0, t0 + i * 0.01)
    assert ing.stats.broadcast_skipped_unknown == 0
    assert not ing.ingest_broadcast_can(0x7FF, bytes([9]), 0, 1.0, t0 + 0.05)
    assert ing.stats.broadcast_skipped_unknown == 1
    # After the TTL the id is tried again (decoder may have learnt it).
    assert not ing.ingest_broadcast_can(0x7FF, bytes([9]), 0, 1.0, t0 + 61.0)
    assert ing.stats.broadcast_skipped_unknown == 1
    # A known id is never poisoned by the negative cache.
    assert ing.ingest_broadcast_can(0x0B4, frame_0b4(5), 0, 1.0, t0 + 61.1)


def test_concurrent_dispatch_from_panda_thread_and_engine_thread():
    """The Store serialises dispatches: a reader thread feeding frames while
    the engine thread dispatches must not corrupt state or raise."""
    twin = create_test_twin()
    ing = twin.ingress
    ing.set_broadcast_dedupe(0)
    ing.set_broadcast_dispatch_cap(0)     # every frame must dispatch here
    errors = []
    n = 2000

    def panda():
        try:
            for i in range(n):
                ing.ingest_broadcast_can(0x0B4, frame_0b4((i % 700) / 10.0), 0, time.time(), time.monotonic())
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def engine():
        try:
            for i in range(n):
                twin.store.dispatch(SetVolumeAction(i % 60))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=panda), threading.Thread(target=engine)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    assert ing.stats.broadcast_dispatched == n
    assert twin.store.state.audio.volume == (n - 1) % 60
    assert twin.store._dispatching is False and twin.store._pending_actions == []


def test_host_side_latency_budget():
    """Decode + dispatch (rules engine, subscribers) per hot frame on this
    machine. Printed with -s; the assertion is a loose regression guard."""
    twin = create_test_twin()
    ing = twin.ingress
    ing.set_broadcast_dedupe(0)
    samples = []
    for i in range(3000):
        if i % 2:
            cid, data = 0x03B, frame_03b(-200 + (i % 400), 0xC0 + (i % 60))
        else:
            cid, data = 0x0B4, frame_0b4((i % 1200) / 10.0)
        t0 = time.perf_counter()
        ing.ingest_broadcast_can(cid, data, 0, time.time(), time.monotonic())
        samples.append(time.perf_counter() - t0)
    samples = samples[200:]           # warm-up
    mean = statistics.fmean(samples) * 1e6
    p99 = sorted(samples)[int(len(samples) * 0.99)] * 1e6
    print("\nbroadcast ingest per frame: mean %.0f us, p99 %.0f us, max %.0f us"
          % (mean, p99, max(samples) * 1e6))
    assert mean < 2000, "decode+dispatch should take well under 2 ms per frame"


def test_bench_correlation_times_gateway_tx_to_panda_rx():
    twin = create_test_twin()
    ing = twin.ingress
    t0 = time.monotonic()
    ing.bench_mark_tx(0x0B4, frame_0b4(12.3), t0)
    ing.bench_mark_tx(0x0B4, frame_0b4(45.6), t0)
    assert ing.ingest_broadcast_can(0x0B4, frame_0b4(12.3), 0, 1.0, t0 + 0.0021)
    r = ing.bench_results()
    assert r["n"] == 1 and abs(r["last_ms"] - 2.1) < 0.01 and r["pending"] == 1
    ing.bench_reset()
    assert ing.bench_results()["n"] == 0 and ing.bench_results()["pending"] == 0


def test_gateway_can_tx_command_builds_action():
    from cyberpunk_computer.api.commands import build_command, CommandError
    a = build_command("gateway_can_tx", {"id": "0x0B4", "data": [0, 0, 0, 0, 0, "0x10", 9, 0]})
    assert a.can_id == 0x0B4 and a.data == bytes([0, 0, 0, 0, 0, 0x10, 9, 0]) and a.bench
    import pytest
    with pytest.raises(CommandError):
        build_command("gateway_can_tx", {"id": 1, "data": [300]})
    assert build_command("panda_bench", {}).reset


def test_hv_voltage_above_255_is_not_wrapped():
    """Hard regeneration: the pack is at ~260 V (0x0104). Byte 3 alone read
    as 4 V and the power came out ~0 kW (car metrics, 2026-11)."""
    twin = create_test_twin()
    raw = (-679) & 0xFFF
    frame = bytes([(raw >> 8) & 0x0F, raw & 0xFF, 0x01, 0x04, 0x00])
    assert twin.ingress.ingest_broadcast_can(0x03B, frame, 0, 1000.0, time.monotonic())
    energy = twin.store.state.energy
    assert energy.hv_battery_voltage == 260
    assert abs(energy.battery_power_kw + 17.654) < 1e-3


def test_steering_angle_is_steering_wheel_degrees():
    """0x025: 12-bit signed (low nibble of A, B) x 1.5 deg, + = left; same
    decoder as the position feeder; the high nibble of A is not angle."""
    from cyberpunk_computer.backend.position_feeder import decode_steering_deg as feeder_decode
    from cyberpunk_computer.comm.can_decoder import decode_steering_deg

    twin = create_test_twin()
    ing = twin.ingress
    ing.set_broadcast_dedupe(0)
    t0 = time.monotonic()
    cases = (
        (bytes([0x0F, 0xF9, 0, 0, 0x79, 0x79, 0x79, 0xA0]), -10.5, -7),   # 0xFF9 = -7
        (bytes([0x00, 0x23, 0, 0, 0x09, 0x6A, 0x79, 0x3C]), 52.5, 35),
        (bytes([0x01, 0xB8, 0, 0, 0, 0, 0, 0]), 660.0, 440),               # left stop
        (bytes([0x0E, 0x48, 0, 0, 0, 0, 0, 0]), -660.0, -440),             # right stop
        (bytes([0x70, 0x23, 0, 0, 0, 0, 0, 0]), 52.5, 35),                 # high nibble ignored
    )
    for i, (data, deg, raw) in enumerate(cases):
        assert decode_steering_deg(data) == deg
        assert feeder_decode(data) == deg
        assert ing.ingest_broadcast_can(0x025, data, 0, 1.0 + i, t0 + i)
        dyn = twin.store.state.dynamics
        assert dyn.steering_angle == deg and dyn.steering_angle_raw == raw
    assert decode_steering_deg(b"\x00") is None


# ── 2026-11-17: log cadence of undecoded ids, dispatch cap, 0x3CF ────────────

def _logged(ing):
    seen = []
    ing.add_message_log_callback(lambda msg, direction: seen.append(msg.data))
    return seen


def test_undecoded_id_reaches_the_log_once_a_second():
    """Until 2026-11-16 an id the decoder does not know reached the trip log
    3 times a minute (0x3CA, 0x3CF, 0x484 ...: 0.05 /s for 100 ms frames)."""
    twin = create_test_twin()
    ing = twin.ingress
    seen = _logged(ing)
    t0 = time.monotonic()
    n = 0
    t = 0.0
    while t < 20.0:                       # a 100 ms frame with a changing byte
        ing.ingest_broadcast_can(0x3CA, bytes([0, 0, n & 0xFF, 0, 0]), 0, 1000.0 + t, t0 + t)
        n += 1
        t += 0.1
    logged = [d for d in seen if d["i"] == "0x3CA"]
    assert 18 <= len(logged) <= 24        # 3 probes + one per second
    assert ing.stats.broadcast_unknown_logged >= 17
    assert twin.store.state is not None and ing.stats.broadcast_dispatched == 0


def test_fast_id_updates_state_at_the_cap_and_logs_every_change():
    twin = create_test_twin()
    ing = twin.ingress
    seen = _logged(ing)
    t0 = time.monotonic()
    dispatched = 0
    for k in range(125):                  # 0x03B every 8 ms for 1 s, value changing
        dispatched += bool(ing.ingest_broadcast_can(
            0x03B, frame_03b(-100 - k, 0xE5), 0, 1000.0 + k * 0.008, t0 + k * 0.008))
    assert 9 <= dispatched <= 13          # 0.1 s cap
    assert len([d for d in seen if d["i"] == "0x03B"]) == 125
    assert ing.stats.broadcast_rate_limited == 125 - dispatched
    # The stream stops changing: the next frame carries the deferred value
    # into the state without a second log line.
    last = frame_03b(-100 - 124, 0xE5)
    t = t0 + 125 * 0.008
    for k in range(20):
        ing.ingest_broadcast_can(0x03B, last, 0, 1001.0 + k * 0.008, t + k * 0.008)
    assert abs(twin.store.state.energy.hv_battery_current + 22.4) < 1e-6
    assert len([d for d in seen if d["i"] == "0x03B"]) == 125


def test_event_frame_after_a_periodic_one_is_not_deferred():
    """0x529 comes once a second and immediately on an event: the event frame
    50 ms after a periodic one must reach the state at once."""
    twin = create_test_twin()
    ing = twin.ingress
    t0 = time.monotonic()
    a = bytes([0x25, 0, 0, 0x03, 0, 0])
    b = bytes([0xA5, 0, 0, 0x83, 0, 0])
    for k in range(3):
        ing.ingest_broadcast_can(0x529, a, 0, 1000.0 + k, t0 + k)
    before = ing.stats.broadcast_rate_limited
    ing.ingest_broadcast_can(0x529, b, 0, 1002.05, t0 + 2.05)
    assert ing.stats.broadcast_rate_limited == before


def test_dispatch_cap_off():
    twin = create_test_twin()
    ing = twin.ingress
    ing.set_broadcast_dispatch_cap(0)
    t0 = time.monotonic()
    n = sum(bool(ing.ingest_broadcast_can(0x03B, frame_03b(-100 - k, 0xE5), 0, 1.0, t0 + k * 0.008))
            for k in range(50))
    assert n == 50


def test_0x3cf_feeds_air_temperatures_and_marks_the_can_source():
    twin = create_test_twin()
    ing = twin.ingress
    t0 = time.monotonic()
    assert ing.ingest_broadcast_can(0x3CF, bytes([0x10, 8, 21, 0, 0x33]), 0, 1000.0, t0)
    v = twin.store.state.vehicle
    assert v.ambient_air_temp == 8.0 and v.intake_air_temp == 21.0
    assert ing.sources.fresh("ambient_air", "can") and ing.sources.fresh("intake_temp", "can")


def test_0x3cf_at_or_below_zero_does_not_cover_the_obd_poll():
    """0 °C and below are not verified on the car: the value is shown while
    no OBD reply is fresh, but the CAN source is not marked alive, so the
    OBD 0146 slot is not suspended and both values reach the log."""
    twin = create_test_twin()
    ing = twin.ingress
    t0 = time.monotonic()
    assert ing.ingest_broadcast_can(0x3CF, bytes([0x10, 0xFD, 12, 0, 0]), 0, 1000.0, t0)
    v = twin.store.state.vehicle
    assert v.ambient_air_temp == -3.0 and v.intake_air_temp == 12.0
    assert not ing.sources.fresh("ambient_air", "can")
    assert ing.sources.fresh("intake_temp", "can")
