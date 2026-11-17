"""Peak-hold thermal sampler: the fan policy must see burst peaks (2026-11-17)."""
import math

from cyberpunk_computer.backend.thermal_sampler import PeakHold, ThermalSampler, classify


def test_classify():
    assert classify("cpu4-thermal") == "cpu"
    assert classify("cluster1-thermal") == "cpu"
    assert classify("gpu-top-thermal") == "gpu"
    assert classify("qcom-battery") == "battery"
    assert classify("wlan-thermal") is None


def test_peak_hold_keeps_max_then_expires():
    h = PeakHold(hold_s=2.5)
    h.add(0.0, 70.0)
    h.add(0.2, 95.0)
    h.add(0.4, 68.0)
    assert h.value(0.4) == 95.0
    assert h.value(2.6) == 95.0
    assert h.value(2.8) == 68.0          # the 95 at t=0.2 is older than 2.5 s
    assert h.value(10.0) is None
    h.add(11.0, None)
    assert h.value(11.0) is None


def burst(t):
    """The measured load: 0.3 s at 95 °C once per second, 68 °C otherwise."""
    return 95.0 if (t % 1.0) < 0.3 else 68.0


def test_one_hz_reader_is_blind_but_sampler_is_not(tmp_path):
    # a 1 Hz reader at phase 0.5 s never sees the burst
    assert all(burst(k + 0.5) == 68.0 for k in range(60))
    clock = {"t": 0.0}
    zone = tmp_path / "temp"
    s = ThermalSampler(zones={"cpu": [str(zone)], "gpu": [], "battery": []},
                       clock=lambda: clock["t"])
    seen = []
    t = 0.0
    while t < 60.0:
        clock["t"] = t
        zone.write_text(str(int(burst(t) * 1000)))
        s.sample_once()
        if t > 3.0 and math.isclose((t % 1.0), 0.5, abs_tol=0.1):
            seen.append(s.read()[0])
        t += s.period_s
    assert seen and min(seen) == 95.0


def test_missing_zones_give_none():
    s = ThermalSampler(zones={"cpu": ["/nonexistent/temp"], "gpu": [], "battery": []})
    s.sample_once()
    assert s.read() == (None, None, None)
