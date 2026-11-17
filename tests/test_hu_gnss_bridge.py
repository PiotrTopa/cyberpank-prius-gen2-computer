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
