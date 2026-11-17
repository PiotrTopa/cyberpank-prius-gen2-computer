"""tools/headunit/hu-gnss-bridge: parsing of `dumpsys location` (Android 8.1 layout)
into backend GNSS fixes (docs/POSITION_FEEDER.md §4). The input lines follow
Location.toString() of API 26/27; the real unit's output is still to be captured
(docs/HEADUNIT_INTEGRATION.md §7)."""

import importlib.machinery
import importlib.util
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "tools", "headunit", "hu-gnss-bridge")
_loader = importlib.machinery.SourceFileLoader("hu_gnss_bridge", _PATH)
_spec = importlib.util.spec_from_loader("hu_gnss_bridge", _loader)
bridge = importlib.util.module_from_spec(_spec)
_loader.exec_module(bridge)

BLOCK = """\
@@HU_LOC_BEGIN
  Last Known Locations:
    gps: Location[gps 50.061234,19.937654 hAcc=4 et=+1h2m3s400ms alt=231.5 vel=13.4 bear=87.5 vAcc=6 sAcc=1 bAcc=3 {Bundle[{satellites=9, maxCn0=41, meanCn0=33}]}]
    passive: Location[network 50.060000,19.930000 hAcc=1200 et=+50m1s5ms vAcc=??? sAcc=??? bAcc=???]
  Last Known Locations Coarse Intervals:
    gps: Location[gps 50.061000,19.937000 hAcc=2000 et=+1h2m3s400ms vAcc=??? sAcc=??? bAcc=???]
@@HU_LOC_END 3723.90 1000.00
"""


def test_duration():
    assert bridge.parse_duration_ms("+1h2m3s400ms") == 3_723_400
    assert bridge.parse_duration_ms("+1d0h0m0s1ms") == 86_400_001
    assert bridge.parse_duration_ms("+45s12ms") == 45_012
    assert bridge.parse_duration_ms("+3m0s0ms") == 180_000
    assert bridge.parse_duration_ms("0") == 0
    assert bridge.parse_duration_ms("?!?") is None


def test_block_to_fix():
    (locs, up, t), = list(bridge.samples(BLOCK.splitlines(True)))
    assert up == 3723.90
    gps = locs["gps"]
    assert gps["lat"] == 50.061234 and gps["lon"] == 19.937654
    assert gps["hacc_m"] == 4 and gps["sats"] == 9 and gps["et_ms"] == 3_723_400
    assert gps["speed_mps"] == 13.4 and gps["course_deg"] == 87.5
    assert locs["passive"]["provider"] == "network" and "vacc_m" not in locs["passive"]
    fix = bridge.to_fix(gps, up, t, now=t + 0.1)
    assert fix["age_ms"] == 600                      # 3723900 - 3723400 + 100
    assert fix["fix"] == "3d" and fix["sats"] == 9 and "mock" not in fix


def test_unknown_accuracy_and_mock():
    loc = bridge.parse_location("gps 1.000000,2.000000 hAcc=??? t=?!? et=?!? vAcc=??? sAcc=??? bAcc=??? mock")
    assert loc["mock"] and loc["et_ms"] is None and "hacc_m" not in loc
    fix = bridge.to_fix(loc, 10.0, 0.0, now=0.0)
    assert "age_ms" not in fix


def test_dry_run_from_stdin(monkeypatch, capsys):
    import io
    import sys
    stream = BLOCK + BLOCK                             # same fix twice: sent once
    monkeypatch.setattr(sys, "stdin", io.StringIO(stream))
    assert bridge.main(["--stdin"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("DRY ")


def test_logcat_source():
    lines = ['--------- beginning of main\n', '{"event":"started"}\n',
             '{"lat":50.1,"lon":19.9,"age_ms":40,"utc_ms":1763340000000,"et_ms":5000,"hacc_m":3.5,"sats":11}\n',
             'garbage\n']
    (obj, t), = list(bridge.logcat_fixes(lines))
    fix = bridge.logcat_to_fix(obj, t, now=t + 0.01)
    assert fix["age_ms"] == 50 and fix["fix"] == "3d" and "et_ms" not in fix and fix["sats"] == 11


REAL_8227L = """\
@@HU_LOC_BEGIN
  Last Known Locations:
    fused: Location[fused 50.061000,19.937000 hAcc=5 et=+1m14s92ms alt=45.1 vel=0.04478083 vAcc=10 sAcc=??? bAcc=??? {Bundle[{noGPSLocation=Location[fused 50.061000,19.937000 hAcc=5 et=+1m14s92ms alt=45.1 vel=0.04478083 vAcc=10 sAcc=??? bAcc=???]}]}]
    gps: Location[gps 50.061234,19.937654 hAcc=2 et=+4d3h38m36s513ms alt=10.1 vel=0.0 bear=109.59 vAcc=??? sAcc=??? bAcc=??? {Bundle[{satellites=9, maxCn0=35, meanCn0=26}]}]
    passive: Location[gps 50.061234,19.937654 hAcc=2 et=+4d3h38m36s513ms alt=10.1 vel=0.0 bear=109.59 vAcc=??? sAcc=??? bAcc=??? {Bundle[{satellites=9, maxCn0=35, meanCn0=26}]}]
  Last Known Locations Coarse Intervals:
    gps: Location[gps 50.061000,19.937000 hAcc=7 et=+4d3h33m56s474ms alt=7.3 vel=0.14772528 bear=240.49 vAcc=??? sAcc=??? bAcc=??? {Bundle[{satellites=0, maxCn0=0, meanCn0=0}]}]
@@HU_LOC_END 358717.01 1000.00
"""


def test_real_unit_layout():
    """Line layout captured from the car's unit on 2026-11-17 (coordinates replaced)."""
    (locs, up, t), = list(bridge.samples(REAL_8227L.splitlines(True)))
    gps = locs["gps"]
    assert gps["et_ms"] == ((4 * 24 + 3) * 3600 + 38 * 60 + 36) * 1000 + 513
    assert gps["hacc_m"] == 2 and gps["sats"] == 9 and gps["course_deg"] == 109.59
    assert "vacc_m" not in gps and "speed_acc_mps" not in gps      # "???" = not reported
    fix = bridge.to_fix(gps, up, t, now=t)
    assert fix["age_ms"] == 497 and fix["speed_mps"] == 0.0


def test_stats_counts_accepts_and_failures(capsys):
    st = bridge.Stats()
    st.count(200, '{"accepted":1,"rejected":[],"has_fix":true}', {"hacc_m": 2.0})
    st.count(200, '{"accepted":0,"rejected":[{"index":0,"reason":"old"}]}', {"hacc_m": 3.0})
    st.count(0, "connection refused", {})
    assert (st.sent, st.ok, st.failed) == (3, 1, 2)
    st.t0 -= 61
    st.tick(True)
    assert "sent 3 accepted 1 failed 2" in capsys.readouterr().err
    assert st.sent == 0


def test_recovery_ladder():
    r = bridge.Recovery(wait_s=20, max_wait_s=300)
    assert r.due(True, 0) is None                 # first sighting starts the clock
    assert r.due(True, 19) is None
    assert r.due(True, 21) == "usb_reset"
    assert r.due(True, 30) is None
    assert r.due(True, 42) == "adb_restart"       # then the wait doubles
    assert r.due(True, 70) is None
    assert r.due(True, 83) == "usb_reset"
    assert r.due(False, 90) is None               # unit left USB (ACC off): clock stops
    assert r.due(True, 500) is None
    r.ok()
    assert r.wait == 20 and r.step == 0


def test_usb_node(tmp_path):
    d = tmp_path / "1-1.4.2"
    d.mkdir()
    (d / "idVendor").write_text("0e8d\n"); (d / "busnum").write_text("1\n"); (d / "devnum").write_text("50\n")
    (tmp_path / "usb1").mkdir()
    assert bridge.usb_node(sysfs=str(tmp_path)) == "/dev/bus/usb/001/050"
    assert bridge.usb_node(vendor="ffff", sysfs=str(tmp_path)) is None
