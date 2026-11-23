"""Host statistics of both POCOs (backend.host_stats), the shared fan
(fan_policy.SharedFan) and their metrics signals."""

import json

from cyberpunk_computer.backend import host_stats as hs
from cyberpunk_computer.backend.fan_policy import SharedFan
from cyberpunk_computer.metrics.signals import SIGNALS_BY_NAME
from cyberpunk_computer.state.actions import SetHostStatsAction, SetPoco2StateAction
from cyberpunk_computer.state.store import Store

STAT_A = """cpu  1000 0 500 8000 100 0 0 0 0 0
cpu0 100 0 100 1000 0 0 0 0 0 0
cpu4 400 0 100 500 0 0 0 0 0 0
intr 5
"""
STAT_B = """cpu  1300 0 600 8500 100 0 0 0 0 0
cpu0 110 0 110 1180 0 0 0 0 0 0
cpu4 580 0 110 510 0 0 0 0 0 0
intr 9
"""


def test_cpu_percent_from_proc_stat():
    a, b = hs.parse_proc_stat(STAT_A), hs.parse_proc_stat(STAT_B)
    assert set(a) == {"cpu", "cpu0", "cpu4"}
    assert hs.cpu_percent(a, b, ["cpu"]) == 44.4          # 400 busy of 900
    assert hs.cpu_percent(a, b, ["cpu0"]) == 10.0
    assert hs.cpu_percent(a, b, ["cpu4"]) == 95.0
    assert hs.cpu_percent({}, b, ["cpu"]) is None          # first record: no delta yet
    assert hs.cpu_percent(a, a, ["cpu"]) is None


def test_meminfo_and_zone_kinds():
    m = hs.parse_meminfo("MemTotal: 5675000 kB\nMemFree: 1 kB\nMemAvailable: 4540000 kB\n")
    assert m == {"mem_total_mb": 5542, "mem_avail_mb": 4434, "mem_used_pct": 20.0}
    assert hs.parse_meminfo("")["mem_used_pct"] is None
    assert hs.zone_kind("cpu5-thermal") == "cpu" and hs.zone_kind("cluster1-thermal") == "cpu"
    assert hs.zone_kind("gpu-top-thermal") == "gpu" and hs.zone_kind("qcom-battery") == "battery"
    assert hs.zone_kind("wlan-thermal") is None


def test_peak_hold_window():
    p = hs._Peak(10.0)
    p.add(0.0, 60.0)
    p.add(1.0, 95.0)
    p.add(2.0, 70.0)
    assert p.value() == 95.0
    p.add(11.5, 71.0)
    assert p.value() == 71.0
    p.add(30.0, None)
    assert p.value() is None


def test_record_on_this_machine_has_the_schema():
    stats = hs.HostStats(host="here")
    stats.record()
    rec = stats.record()
    assert rec["host"] == "here" and isinstance(rec["zones"], dict)
    for key in ("t", "cpu_c", "gpu_c", "cpu_pct", "load1", "mem_used_pct", "power_w"):
        assert key in rec
    json.dumps(rec)


def test_agent_file_is_self_contained():
    src = hs.RemoteHostStats.agent_source().decode()
    assert "from ." not in src and "import cyberpunk_computer" not in src
    assert 'if __name__ == "__main__":' in src


class FakeProc:
    def __init__(self, lines):
        self.stdin = self
        self.stdout = iter(lines)
        self.written = b""
        self.terminated = False

    def write(self, data):
        self.written += data

    def close(self):
        pass

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_remote_reader_command_and_parsing():
    t = [100.0]
    procs = []

    def popen(argv, **kw):
        procs.append((argv, FakeProc([b'noise\n', b'{"host":"poco2","t":1,"cpu_c":61.5}\n'])))
        return procs[-1][1]

    r = hs.RemoteHostStats(["ssh", "-i", "/k", "user@10.42.0.94"], period_s=2.0, host="poco2",
                           popen=popen, clock=lambda: t[0])
    assert r.latest() == (None, None)
    r._session()
    argv, proc = procs[0]
    assert argv[:3] == ["ssh", "-i", "/k"]
    assert argv[-7:] == ["user@10.42.0.94", "python3", "-u", "-", "--period", "2.0", "--host"] + [] \
        or argv[-8:] == ["user@10.42.0.94", "python3", "-u", "-", "--period", "2.0", "--host", "poco2"]
    assert "ServerAliveInterval=5" in argv
    assert proc.written == hs.RemoteHostStats.agent_source() and proc.terminated
    rec, age = r.latest()
    assert rec["cpu_c"] == 61.5 and age == 0.0 and r.lines == 1
    t[0] += 20
    assert r.latest()[1] == 20.0
    assert r.feed_line("[1,2]") is False


def _step(fan, now, poco, poco2, mode="full", override=None):
    return fan.step(now, {"poco": poco, "poco2": poco2}, ambient_c=22.0, box_in_c=None,
                    box_out_c=22.0, power_mode=mode, override_pct=override)


def R(cpu=None, gpu=None, present=True, fresh=True):
    return {"cpu_c": cpu, "gpu_c": gpu, "present": present, "fresh": fresh}


def test_shared_fan_follows_the_hotter_phone():
    fan = SharedFan()
    duty = 0.0
    for i in range(40):                       # car computer cool, secondary hot
        duty, dec, det = _step(fan, i * 10.0, R(55, 45), R(96, 88))
    assert duty >= 90.0
    assert det["source"] == "poco2" and det["poco2_pct"] >= 90.0 and det["poco_pct"] == 0.0
    assert det["poco2_ctrl_c"] is not None

    fan = SharedFan()
    for i in range(40):                       # the other way round
        duty, dec, det = _step(fan, i * 10.0, R(96, 88), R(50, 42))
    assert duty >= 90.0 and det["source"] == "poco" and det["poco2_pct"] == 0.0

    fan = SharedFan()
    for i in range(40):                       # both cool: fan off
        duty, dec, det = _step(fan, i * 10.0, R(50, 42), R(50, 42))
    assert duty == 0.0 and det["source"] == "idle"


def test_shared_fan_ignores_a_phone_that_is_off():
    fan = SharedFan()
    for i in range(20):
        duty, dec, det = _step(fan, i * 10.0, R(50, 42), R(present=False, fresh=False))
    assert duty == 0.0 and det["poco2_stale"] is False


def test_shared_fan_holds_a_floor_for_a_running_phone_without_data():
    fan = SharedFan(stale_floor_pct=30.0)
    for i in range(5):
        duty, dec, det = _step(fan, i * 10.0, R(50, 42), R(present=True, fresh=False))
    assert duty == 30.0 and det["poco2_stale"] is True and det["poco2_reason"] == "stale"
    assert det["source"] == "poco2"
    fan = SharedFan(stale_floor_pct=30.0)     # key off: no floor
    for i in range(5):
        duty, dec, det = _step(fan, i * 10.0, R(50, 42), R(present=True, fresh=False), mode="low")
    assert duty == 0.0


def test_shared_fan_override_wins():
    fan = SharedFan()
    duty, dec, det = _step(fan, 0.0, R(96, 88), R(96, 88), override=25.0)
    assert duty == 25.0 and det["source"] == "override"


def test_state_and_signals():
    store = Store()
    store.dispatch(SetHostStatsAction(
        poco={"cpu_c": 70.5, "gpu_c": 60.0, "cpu_pct": 33.0, "big_pct": 50.0, "load1": 1.5,
              "mem_used_pct": 40.0, "power_w": 2.5},
        thermal={"duty_pct": 35.0, "poco_pct": 35.0, "poco2_pct": 0.0, "source": "poco"}))
    s = store.state
    assert s.hosts.poco2 is None
    assert SIGNALS_BY_NAME["poco_cpu_temp"].get(s) == 70.5
    assert SIGNALS_BY_NAME["poco_cpu_usage"].get(s) == 33.0
    assert SIGNALS_BY_NAME["poco2_cpu_temp"].get(s) is None
    assert SIGNALS_BY_NAME["fan_poco_pct"].get(s) == 35.0
    assert SIGNALS_BY_NAME["poco2_on"].get(s) is None        # manager disabled
    store.dispatch(SetPoco2StateAction(enabled=True, state="on"))
    store.dispatch(SetHostStatsAction(poco2={"cpu_c": 44.0, "power_w": 0.8}))
    s = store.state
    assert SIGNALS_BY_NAME["poco2_on"].get(s) == 1.0
    assert SIGNALS_BY_NAME["poco2_cpu_temp"].get(s) == 44.0
    assert SIGNALS_BY_NAME["poco2_host_power"].get(s) == 0.8
    assert s.hosts.poco["cpu_c"] == 70.5                      # partial update keeps the rest
