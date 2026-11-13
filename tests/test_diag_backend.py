"""Diagnostics in the backend: REST command validation, store/ingress wiring,
persisted auto-clear settings, HTTP routes, the CLI dry run."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cyberpunk_computer.api.commands import CommandError, build_command, command_catalog
from cyberpunk_computer.diag import report as reportmod
from cyberpunk_computer.diag.actions import DiagCommandAction, SetDiagStateAction
from cyberpunk_computer.diag.sim import make_prius
from cyberpunk_computer.diag.wiring import wire_diagnostics
from cyberpunk_computer.io.factory import ExecutionMode, VirtualTwinConfig, create_virtual_twin
from cyberpunk_computer.io.ports import DEVICE_CAN, RawMessage

REPO = Path(__file__).resolve().parent.parent


# ── REST validation ──────────────────────────────────────────────────────────

def test_commands_are_registered():
    names = {c["name"] for c in command_catalog()}
    assert {"diag_scan", "diag_identify", "diag_read", "diag_clear", "diag_cancel",
            "diag_autoclear_set", "dtc"} <= names


def test_scan_command():
    a = build_command("diag_scan", {})
    assert isinstance(a, DiagCommandAction) and a.op == "scan" and a.params == {}
    a = build_command("diag_scan", {"ecus": ["hv", "engine"], "kinds": ["stored"], "probe": True})
    assert a.params == {"ecus": ["hv", "engine"], "kinds": ["stored"], "probe": True}
    for bad in ({"ecus": ["nope"]}, {"ecus": []}, {"kinds": ["all"]}, {"kinds": []}, {"probe": "yes"},
                {"ecus": 5}):
        with pytest.raises(CommandError):
            build_command("diag_scan", bad)


def test_clear_command_needs_explicit_confirmation():
    for bad in ({"ecu": "engine"}, {"ecu": "engine", "confirm": False}, {"ecu": "engine", "confirm": "true"},
                {"ecu": "engine", "confirm": 1}, {"confirm": True}, {"ecu": "nope", "confirm": True},
                {"ecu": "transmission", "confirm": True}, {"ecu": "engine", "confirm": True, "allow_moving": "y"}):
        with pytest.raises(CommandError):
            build_command("diag_clear", bad)
    a = build_command("diag_clear", {"ecu": "engine", "confirm": True})
    assert a.op == "clear" and a.params == {"ecu": "engine", "confirm": True, "allow_moving": False}


def test_read_command_is_read_only():
    assert build_command("diag_read", {"ecu": "hv", "item": "hybrid_system"}).params["item"] == "hybrid_system"
    assert build_command("diag_read", {"ecu": "hv", "item": "13 FF 00"}).op == "read"
    for bad in ({"ecu": "hv"}, {"ecu": "hv", "item": ""}, {"ecu": "hv", "item": "1101"},
                {"ecu": "hv", "item": "04"}, {"ecu": "hv", "item": "14"}, {"ecu": "hv", "item": "3021"},
                {"ecu": "hv", "item": "2701"}, {"ecu": "hv", "item": "xyz"}, {"ecu": "nope", "item": "13"},
                {"ecu": "power_source", "item": "13"}, {"ecu": "hv", "item": "21" + "00" * 7}):
        with pytest.raises(CommandError):
            build_command("diag_read", bad)


def test_autoclear_command():
    a = build_command("diag_autoclear_set", {"enabled": True, "codes": [{"ecu": "engine", "code": "P0420"}]})
    assert a.op == "autoclear_set"
    for bad in ({}, {"enabled": 1}, {"codes": [{"ecu": "engine", "code": "nope"}]}, {"bogus": 1},
                {"codes": [{"ecu": "brake", "code": "C1241"}]}, {"min_interval_s": 1}):
        with pytest.raises(CommandError):
            build_command("diag_autoclear_set", bad)


def test_state_action_fields():
    with pytest.raises(ValueError):
        SetDiagStateAction({"stored_dtcs": ()})
    with pytest.raises(ValueError):
        DiagCommandAction("explode")


# ── wiring: store + ingress + simulated car ──────────────────────────────────

class SimOut:
    """Output port that feeds the simulated gateway."""

    def __init__(self):
        self.gw = None
        self.connected = True
        self.other = []

    def send(self, cmd):
        if cmd.device_id == DEVICE_CAN and cmd.payload.get("a") == "req":
            return self.gw.send(cmd.payload)
        self.other.append(cmd)
        return True

    def is_connected(self):
        return self.connected


@pytest.fixture
def wired(tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr("cyberpunk_computer.diag.wiring._save_autoclear", lambda cfg: saved.update(cfg))
    monkeypatch.setattr("cyberpunk_computer.diag.wiring._load_autoclear",
                        lambda known: __import__("cyberpunk_computer.diag.autoclear", fromlist=["x"]).AutoClearConfig())
    twin = create_virtual_twin(VirtualTwinConfig(mode=ExecutionMode.TEST, log_commands=False))
    out = SimOut()
    twin.output_port = out
    replies = []
    out.gw = make_prius(replies.append)
    diag = wire_diagnostics(twin, report_dir=str(tmp_path / "diag"))
    twin.ingress.stats.last_can_mono = time.monotonic()

    def run(max_iter=4000):
        for _ in range(max_iter):
            diag.tick()
            out.gw.step()
            while replies:
                # through the real ingress path, as a gateway CAN-channel line
                twin.ingress._process_message(RawMessage(device_id=DEVICE_CAN, timestamp=time.time(),
                                                         data=replies.pop(0)))
            if not diag.engine.busy:
                diag.tick()
                return
            time.sleep(0.0005)
        raise AssertionError("still busy")

    diag.engine.config.min_gap_s = 0.0
    diag.engine.config.publish_min_s = 0.0
    return twin, diag, out, run, saved


def test_scan_through_store_and_ingress(wired, tmp_path):
    twin, diag, out, run, _ = wired
    twin.store.dispatch(build_command("diag_scan", {"kinds": ["stored", "pending", "freeze", "ident"]}))
    assert twin.store.state.diagnostics.scan["state"] == "running" or diag.engine.scanning
    run()
    d = twin.store.state.diagnostics
    assert d.scan["state"] == "done" and d.scan["report_file"].startswith("diag_report_")
    assert [x["code"] for x in d.ecus["hv"]["dtcs"]] == ["P0852", "P3102"]
    assert d.ecus["engine"]["dtcs"][0]["manual_page"] == "05-139"
    assert d.vehicle["vin"] == "JTDKB20U040000000"
    assert d.ecus["transmission"]["status"] == "unreachable"
    # the MFD DTC screen's fields carry every ECU's codes
    assert set(d.stored_dtcs) == {("P0420", "ENGINE"), ("P0852", "HYBRID"), ("P3102", "HYBRID")}
    assert d.mil_on is True and d.scan_in_progress is False and d.last_scan_time is not None
    # state is serialisable for the API / dashboard
    from cyberpunk_computer.api.serialization import serialize_state
    text = json.dumps(serialize_state(twin.store.state))
    assert "P0852" in text and "autoclear" in text
    # and the remote frontend can rebuild it
    from cyberpunk_computer.frontend.deserialize import deserialize_state
    back = deserialize_state(json.loads(text))
    assert back.diagnostics.ecus["hv"]["dtcs"][0]["code"] == "P0852"
    assert reportmod.read_report(str(tmp_path / "diag"))["vin"] == "JTDKB20U040000000"


def test_diag_replies_do_not_reach_the_legacy_decoder(wired):
    twin, diag, out, run, _ = wired
    twin.store.dispatch(build_command("diag_scan", {"ecus": ["engine"]}))
    run()
    # legacy scan state untouched by raw 0x43 replies (only the final summary dispatch)
    assert twin.ingress._dtc_scan_results == []


def test_clear_through_the_store(wired, tmp_path):
    twin, diag, out, run, _ = wired
    twin.store.dispatch(build_command("diag_clear", {"ecu": "engine", "confirm": True}))
    run()
    lc = twin.store.state.diagnostics.ecus["engine"]["last_clear"]
    assert lc["ok"] and lc["cleared"] == ["P0420"]
    (rec,) = reportmod.read_clears(str(tmp_path / "diag"))
    assert rec["origin"] == "manual" and rec["before"]["freeze_frame"]["dtc"] == "P0420"
    assert "speed_kmh" in rec["context"]


def test_read_and_error_feedback(wired):
    twin, diag, out, run, _ = wired
    twin.store.dispatch(build_command("diag_read", {"ecu": "engine", "item": "monitor_status"}))
    run()
    lr = twin.store.state.diagnostics.last_read
    assert lr["values"]["mil"] is True and lr["values"]["dtc_count"] == 1
    twin.store.dispatch(build_command("diag_scan", {}))
    twin.store.dispatch(build_command("diag_scan", {}))        # refused: already running
    diag.tick()
    assert "already running" in twin.store.state.diagnostics.scan["error"]
    twin.store.dispatch(build_command("diag_cancel", {}))
    run()
    assert twin.store.state.diagnostics.scan["state"] == "cancelled"


def test_autoclear_through_the_store(wired, tmp_path):
    twin, diag, out, run, saved = wired
    assert twin.store.state.diagnostics.autoclear["enabled"] is False
    twin.store.dispatch(build_command("diag_autoclear_set", {
        "enabled": True, "codes": [{"ecu": "engine", "code": "P0420"}]}))
    assert saved["enabled"] is True and saved["codes"] == [{"ecu": "engine", "code": "P0420", "enabled": True}]
    run()
    ac = twin.store.state.diagnostics.autoclear
    assert ac["counters"]["clears"] == 1 and ac["last_action"]["codes"] == ["P0420"]
    (rec,) = reportmod.read_clears(str(tmp_path / "diag"))
    assert rec["origin"] == "autoclear" and rec["before"]["freeze_frame"]["dtc"] == "P0420"


def test_bus_tap_reaches_the_engine(wired):
    twin, diag, out, run, _ = wired
    twin.ingress.ingest_broadcast_can(0x7EA, bytes([1, 2, 3, 4, 5, 6, 7, 8]))
    twin.ingress.ingest_broadcast_can(0x3CB, bytes(7))
    assert len(diag.engine._bus_frames) == 1


def test_persisted_settings_roundtrip(tmp_path):
    from cyberpunk_computer.persistence import SettingsManager
    sm = SettingsManager(config_dir=tmp_path)
    assert sm.settings.diagnostics.autoclear == {}
    sm.settings.diagnostics.autoclear = {"enabled": True, "codes": [{"ecu": "engine", "code": "P0420", "enabled": True}]}
    assert sm.save()
    again = SettingsManager(config_dir=tmp_path)
    assert again.settings.diagnostics.autoclear["codes"][0]["code"] == "P0420"
    assert again.settings.power.undervoltage_threshold is None       # other sections intact


# ── HTTP routes ──────────────────────────────────────────────────────────────

class _Bridge:
    def attach_loop(self, loop):
        pass

    def latest_snapshot(self):
        return None


def _endpoint(app, path):
    (r,) = [r for r in app.routes if getattr(r, "path", "") == path]
    return r.endpoint


def test_http_routes(tmp_path):
    from fastapi import HTTPException
    from cyberpunk_computer.api import app as app_mod
    d = str(tmp_path / "diag")
    reportmod.set_active_dir(d)
    try:
        app = app_mod.create_app(_Bridge(), db=object())
        with pytest.raises(HTTPException) as e:
            asyncio.run(_endpoint(app, "/api/v1/diag/report")(name=None))
        assert e.value.status_code == 404
        name = reportmod.write_report(d, {"ts": 1_700_000_000, "vin": "X", "ecus": {}})
        reportmod.append_clear(d, {"ts": 1, "ecu": "engine", "ok": True})
        assert asyncio.run(_endpoint(app, "/api/v1/diag/report")(name=None))["vin"] == "X"
        assert asyncio.run(_endpoint(app, "/api/v1/diag/report")(name=name))["vin"] == "X"
        with pytest.raises(HTTPException):
            asyncio.run(_endpoint(app, "/api/v1/diag/report")(name="../secret.json"))
        assert asyncio.run(_endpoint(app, "/api/v1/diag/reports")())["reports"][0]["name"] == name
        assert asyncio.run(_endpoint(app, "/api/v1/diag/clears")(limit=10))["clears"][0]["ecu"] == "engine"
        ecus = asyncio.run(_endpoint(app, "/api/v1/diag/ecus")())["ecus"]
        assert {e["key"] for e in ecus} >= {"engine", "hv", "battery", "transmission", "power_source"}
        assert next(e for e in ecus if e["key"] == "transmission")["reachable"] is False
    finally:
        reportmod.set_active_dir(None)


# ── CLI dry run ──────────────────────────────────────────────────────────────

def test_cli_simulator_run(tmp_path):
    out = subprocess.run([sys.executable, str(REPO / "scripts" / "diag_cli.py"), "--sim",
                          "--report", str(tmp_path / "r.json"), "scan"],
                         capture_output=True, text=True, cwd=str(REPO), timeout=60)
    assert out.returncode == 0, out.stderr
    assert "P0852" in out.stdout and "P position switch" in out.stdout
    assert "Hybrid vehicle control ECU" in out.stdout and "05-306" in out.stdout
    assert json.loads((tmp_path / "r.json").read_text())["ecus"]["engine"]["dtcs"][0]["code"] == "P0420"
