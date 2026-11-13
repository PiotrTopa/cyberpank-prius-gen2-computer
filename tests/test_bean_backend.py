"""Backend side of the powerbox BEAN receiver (fw 2.3.0, device 203):
parser, store (message table, known values, fps, powerbox.bean_rx), capture
log + marks (backend.bean_monitor), REST commands and the frames endpoint."""

import asyncio
import json
import os

import pytest

from cyberpunk_computer.api.commands import CommandError, build_command
from cyberpunk_computer.backend.bean_monitor import BeanMonitor
from cyberpunk_computer.comm.bean import decode_known, frame_from_wire, update_messages
from cyberpunk_computer.io.ports import DEVICE_POWERBOX_BEAN
from cyberpunk_computer.io.powerbox import (
    PowerboxCommander,
    build_bean_rx_command,
    build_bean_tx_enable_command,
    parse_bean_message,
    parse_powerbox_message,
    parse_powerbox_system,
)
from cyberpunk_computer.state.actions import (
    BeanCaptureAction,
    BeanMarkAction,
    PowerboxBeanRxAction,
    PowerboxBeanTxEnableAction,
    SetBeanRxAction,
    SetPowerboxBeanAction,
)
from cyberpunk_computer.state.store import Store

T0 = 1_763_000_000.0


def _line(t, frames, st=None):
    d = {"t": t, "bf": frames}
    if st is not None:
        d["st"] = st
    return d


class TestParser:
    def test_device_id(self):
        assert DEVICE_POWERBOX_BEAN == 203

    def test_frames_get_wall_time_from_firmware_age(self):
        (a,) = parse_bean_message(_line(10_500, [[10_000, 6, "FE", "2C", "64", 1, 0],
                                                 [10_400, 2, "FE", "7F", "00108000", None, 1]]),
                                  recv_ts=T0)
        assert isinstance(a, SetBeanRxAction)
        f0, f1 = a.frames
        assert f0 == {"ts": T0 - 0.5, "t_ms": 10_000, "pri": 6, "dst": "FE", "mes": "2C",
                      "data": "64", "rsp": 1, "ok": True, "err": None}
        assert f1["ok"] is False and f1["err"] == "crc" and f1["ts"] == T0 - 0.1

    def test_tick_wrap(self):
        f = frame_from_wire([(1 << 30) - 100, 1, "FE", "24", "00", 1, 0], 50, T0)
        assert f["ts"] == pytest.approx(T0 - 0.15)

    def test_stats_only_and_empty(self):
        (a,) = parse_bean_message({"t": 1, "st": {"ok": 5, "on": 1}}, recv_ts=T0)
        assert a.frames == [] and a.stats["ok"] == 5
        assert parse_bean_message({"t": 1}) == []

    def test_routed_by_device(self):
        acts = parse_powerbox_message(203, _line(5, [[5, 1, "FE", "24", "00", 1, 0]]))
        assert isinstance(acts[0], SetBeanRxAction)

    def test_raw_chunks_joined(self):
        assert parse_bean_message({"raw": [-2000, 100], "ri": 0, "rn": 3}) == []
        (a,) = parse_bean_message({"raw": [-300], "ri": 2, "rn": 3})
        assert a.raw == [-2000, 100, -300]

    def test_system_acks_and_errors(self):
        (a,) = parse_powerbox_system({"ack": "bean_rx", "on": 1, "mode": "dma"})
        assert isinstance(a, SetBeanRxAction) and a.ack == {"on": 1, "mode": "dma"}
        assert parse_powerbox_system({"ack": "bean_rx", "op": "stats"}) == []
        (e,) = parse_powerbox_system({"msg": "ERROR", "code": "BEAN_RX", "detail": "start: no rp2"})
        assert isinstance(e, SetBeanRxAction) and e.error == "start: no rp2"
        (g,) = parse_powerbox_system({"msg": "ERROR", "code": "BEAN_TX_DISABLED", "detail": "op=tx"})
        assert isinstance(g, SetPowerboxBeanAction) and g.result["error"] == "BEAN_TX_DISABLED"
        (t,) = parse_powerbox_system({"ack": "bean_tx_enable", "v": 1, "s": 60})
        assert t.result == {"tx_enabled": True, "s": 60}


class TestKnownIds:
    """All UNVERIFIED on the Prius (other models / bean_tools dashboard)."""

    def test_values(self):
        assert decode_known("2C", "64") == {"coolant_c": 50.0}
        assert decode_known("40", "800000") == {"shift": "P", "shift_raw": 0x80}
        assert decode_known("40", "100000")["shift"] == "D"
        assert decode_known("24", "23") == {"speed_kmh": 35}
        assert decode_known("CD", "47") == {"ambient_c": 23}
        assert decode_known("7F", "001080000000") == {"lamp_tail": False, "lamp_high_beam": False,
                                                      "door_open": True}
        assert decode_known("7F", "18") == {"lamp_tail": True, "lamp_high_beam": True}
        assert decode_known("3F", "0287E980") == {"odometer_km": 165865}
        assert decode_known("AB", "4080") == {} and decode_known("2C", "") == {}


def _frames(*recs, t=10_000, ts=T0):
    return [frame_from_wire([t] + list(r) + [1, 0] if len(r) == 4 else [t] + list(r), t, ts)
            for r in recs]


class TestStore:
    def test_table_decoded_counters_and_fps(self):
        s = Store()
        s.dispatch(SetBeanRxAction(frames=_frames((6, "FE", "2C", "64"), (2, "FE", "24", "00")),
                                   recv_ts=T0))
        s.dispatch(SetBeanRxAction(frames=_frames((6, "FE", "2C", "63"), ts=T0 + 2.0),
                                   recv_ts=T0 + 2.0))
        s.dispatch(SetBeanRxAction(
            frames=[frame_from_wire([1, 6, "FE", "2C", "6399", 1, 1], 1, T0 + 2.1)],
            stats={"on": 1, "mode": "dma", "ok": 3, "crc": 1, "cpu": 12},
            recv_ts=T0 + 2.1))
        b = s.state.bean
        e = b.messages["FE:2C"]
        assert e["count"] == 2 and e["data"] == "63" and e["changes"] == 1
        assert e["period_ms"] == pytest.approx(2000.0) and e["pri"] == 6
        assert b.messages["FE:24"]["count"] == 1
        assert b.decoded["coolant_c"] == {"value": 49.5, "ts": T0 + 2.0, "src": "FE:2C"}
        assert b.decoded["speed_kmh"]["value"] == 0
        assert b.frames_total == 3 and b.errors_total == 1 and b.last_frame_ts == T0 + 2.0
        rx = s.state.powerbox.bean_rx
        assert rx["enabled"] is True and rx["mode"] == "dma" and rx["crc"] == 1
        assert rx["fps"] == pytest.approx(3 / 5.0) and rx["last_frame_ts"] == T0 + 2.0
        # 10 s of silence: the window empties on the next stats line
        s.dispatch(SetBeanRxAction(stats={"on": 1, "ok": 3}, recv_ts=T0 + 12))
        assert s.state.powerbox.bean_rx["fps"] == 0

    def test_ack_error_raw(self):
        s = Store()
        s.dispatch(SetBeanRxAction(ack={"on": 0, "mode": None}, recv_ts=T0))
        assert s.state.powerbox.bean_rx["enabled"] is False
        s.dispatch(SetBeanRxAction(error="start: no rp2", recv_ts=T0))
        assert s.state.powerbox.bean_rx["error"] == "start: no rp2"
        s.dispatch(SetBeanRxAction(raw=[-2000, 100], recv_ts=T0))
        assert s.state.powerbox.bean_rx["raw"] == [-2000, 100]

    def test_table_capped(self):
        frames = [frame_from_wire([0, 1, "%02X" % (i // 256), "%02X" % (i % 256), "00", 1, 0],
                                  0, T0 + i) for i in range(300)]
        table = update_messages({}, frames)
        assert len(table) == 256 and "00:00" not in table and "01:2B" in table

    def test_mark_and_capture_status(self):
        from cyberpunk_computer.state.actions import SetBeanCaptureAction
        s = Store()
        s.dispatch(BeanMarkAction("driver door opened"))
        s.dispatch(SetBeanCaptureAction({"on": True, "file": "x"}))
        assert s.state.bean.last_mark == "driver door opened"
        assert s.state.bean.capture == {"on": True, "file": "x"}

    def test_state_serializes_small(self):
        from cyberpunk_computer.api.serialization import serialize_state
        s = Store()
        s.dispatch(SetBeanRxAction(frames=_frames(*[(2, "FE", "%02X" % m, "00") for m in range(40)]),
                                   recv_ts=T0))
        out = serialize_state(s.state)
        assert "frames" not in out["bean"]
        assert len(json.dumps(out["bean"])) < 8000


class TestMonitor:
    def test_ring_since(self, tmp_path):
        m = BeanMonitor(str(tmp_path), ring=5)
        m.ingest(_frames(*[(1, "FE", "%02X" % i, "00") for i in range(8)]))
        r = m.frames_since(0)
        assert [f["mes"] for f in r["frames"]] == ["03", "04", "05", "06", "07"]
        assert r["next"] == 8 and r["last_seq"] == 8
        assert m.frames_since(6)["frames"][0]["seq"] == 7
        assert m.frames_since(8)["frames"] == [] and m.frames_since(8)["next"] == 8

    def test_capture_file_marks_and_rotation(self, tmp_path):
        clock = [T0]
        m = BeanMonitor(str(tmp_path / "bean"), rotate_bytes=2000, clock=lambda: clock[0])
        assert m.start("door test!")
        m.ingest(_frames((6, "FE", "2C", "64")))
        m.mark("driver door opened")
        m.ingest([frame_from_wire([0, 6, "FE", "2C", "65AA", 1, 1], 0, T0)])
        for _ in range(30):
            m.ingest(_frames((2, "FE", "7F", "001000000000")))
        st = m.status()
        m.stop()
        files = sorted(os.listdir(tmp_path / "bean"))
        assert len(files) >= 2 and files[0].endswith("_door_test.ndjson")
        assert "_part2" in files[1] and st["part"] == len(files) and st["frames"] == 32
        lines = [json.loads(l) for l in open(tmp_path / "bean" / files[0])]
        assert lines[0]["type"] == "header" and lines[0]["format"] == "bean-capture/1"
        assert lines[1] == {"type": "frame", "ts": T0, "t_ms": 10_000, "pri": 6, "dst": "FE",
                            "mes": "2C", "data": "64", "rsp": 1, "ok": 1}
        assert lines[2] == {"type": "mark", "ts": T0, "text": "driver door opened"}
        assert lines[3]["ok"] == 0 and lines[3]["err"] == "crc"
        for f in files:
            assert os.path.getsize(tmp_path / "bean" / f) <= 2000
        assert m.status()["on"] is False

    def test_mark_without_capture_is_kept(self, tmp_path):
        m = BeanMonitor(str(tmp_path))
        m.mark("x")
        assert m.frames_since(0)["marks"][-1]["text"] == "x"
        assert not os.listdir(tmp_path)

    def test_unwritable_dir_reports_error(self, tmp_path):
        f = tmp_path / "file"
        f.write_text("")
        m = BeanMonitor(str(f / "sub"))
        assert m.start("a") is False and m.status()["error"]


class TestCommands:
    def test_bean_rx(self):
        assert build_command("powerbox_bean_rx", {"on": True}).on is True
        assert build_command("powerbox_bean_rx", {"op": "stats"}).op == "stats"
        assert build_command("powerbox_bean_rx", {"raw": 200}).raw == 200
        for bad in ({}, {"on": 1, "op": "stats"}, {"op": "x"}, {"raw": 0}, {"raw": 513}):
            with pytest.raises(CommandError):
                build_command("powerbox_bean_rx", bad)

    def test_capture_mark_tx_enable(self):
        a = build_command("bean_capture", {"on": True, "name": "doors 1"})
        assert isinstance(a, BeanCaptureAction) and a.on and a.name == "doors 1"
        assert build_command("bean_capture", {"on": False}).on is False
        with pytest.raises(CommandError):
            build_command("bean_capture", {"on": True, "name": "../x"})
        assert build_command("bean_mark", {"text": "driver door opened"}).text == "driver door opened"
        for bad in ({}, {"text": ""}, {"text": "x" * 201}):
            with pytest.raises(CommandError):
                build_command("bean_mark", bad)
        assert isinstance(build_command("powerbox_bean_tx_enable", {"on": True}),
                          PowerboxBeanTxEnableAction)

    def test_wire_commands(self):
        assert build_bean_rx_command(on=False).payload == {"a": "bean_rx", "on": 0}
        assert build_bean_rx_command(op="stats").payload == {"a": "bean_rx", "op": "stats"}
        assert build_bean_rx_command(raw=64).payload == {"a": "bean_rx", "raw": 64}
        assert build_bean_rx_command(on=True).device_id == 0
        with pytest.raises(ValueError):
            build_bean_rx_command(on=True, raw=5)
        assert build_bean_tx_enable_command(True).payload == {"a": "bean_tx_enable", "v": 1}

    def test_commander(self):
        sent = []

        class Port:
            def send(self, cmd):
                sent.append(cmd.payload)
                return True
        c = PowerboxCommander(Port())
        assert c.bean_rx(on=True) and c.bean_rx(raw=10) and c.bean_tx_enable(False)
        assert not c.bean_rx(raw=1000)
        assert sent == [{"a": "bean_rx", "on": 1}, {"a": "bean_rx", "raw": 10},
                        {"a": "bean_tx_enable", "v": 0}]


class TestApi:
    def _app(self, provider):
        from cyberpunk_computer.api import app as app_mod

        class Bridge:
            def attach_loop(self, loop):
                pass

            def latest_snapshot(self):
                return None
        return app_mod.create_app(Bridge(), db=object(), bean_frames=provider)

    def _endpoint(self, app):
        (r,) = [r for r in app.routes if getattr(r, "path", "") == "/api/v1/bean/frames"]
        return r.endpoint

    def test_frames_endpoint(self, tmp_path):
        m = BeanMonitor(str(tmp_path))
        m.ingest(_frames((6, "FE", "2C", "64"), (1, "FE", "24", "00")))
        ep = self._endpoint(self._app(m.frames_since))
        out = asyncio.run(ep(since=1, limit=200))
        assert [f["mes"] for f in out["frames"]] == ["24"] and out["next"] == 2

    def test_frames_endpoint_unwired(self):
        from fastapi import HTTPException
        ep = self._endpoint(self._app(None))
        with pytest.raises(HTTPException) as e:
            asyncio.run(ep(since=0, limit=10))
        assert e.value.status_code == 503


class TestServiceWiring:
    def test_middleware_feeds_monitor_and_commander(self, tmp_path):
        from cyberpunk_computer.backend.service import BackendConfig, BackendService
        svc = BackendService(BackendConfig(bean_capture_dir=str(tmp_path / "bean")))

        class Twin:
            store = Store()
        twin = Twin()
        svc.twin = twin
        svc._wire_bean(twin)
        twin.store.dispatch(BeanCaptureAction(True, "t"))
        twin.store.dispatch(SetBeanRxAction(frames=_frames((6, "FE", "2C", "64")), recv_ts=T0))
        twin.store.dispatch(BeanMarkAction("door"))
        svc._bean_tick()
        cap = twin.store.state.bean.capture
        assert cap["on"] is True and cap["frames"] == 1 and cap["file"].endswith("_t.ndjson")
        assert svc.bean_monitor.frames_since(0)["frames"][0]["mes"] == "2C"
        twin.store.dispatch(BeanCaptureAction(False))
        svc._bean_tick()
        assert twin.store.state.bean.capture["on"] is False
        kinds = [json.loads(l)["type"] for l in open(cap["file"])]
        assert kinds == ["header", "frame", "mark"]

    def test_bean_rx_action_reaches_wire(self):
        a = PowerboxBeanRxAction(on=False)
        assert (a.on, a.op, a.raw) == (False, None, None)
