"""Diagnostics engine against the ECU/gateway simulator: full scans, the
one-request-at-a-time scheduler, silent ECUs, host-side ISO-TP on a
sub-addressed ECU, clear gating, reports."""

import json

import pytest

from cyberpunk_computer.diag import report as reportmod
from cyberpunk_computer.diag.ecus import ECUS, Ecu
from cyberpunk_computer.diag.engine import DiagConfig
from cyberpunk_computer.diag.sim import SimEcu, SimObdEcu, make_prius

from diag_harness import Rig


def prius_rig(scenario="buttons_unplugged", **kw):
    rig = Rig(**kw)
    src = make_prius(lambda d: None, scenario=scenario)
    for e in src.ecus:
        rig.gw.add(e)
    return rig


def ecu_of(rig, tx):
    return next(e for e in rig.gw.ecus if e.tx_id == tx)


# ── full scan ────────────────────────────────────────────────────────────────

def test_full_scan_end_to_end():
    rig = prius_rig()
    ok, _ = rig.eng.start_scan()
    assert ok and rig.eng.scanning
    rig.run()
    res = rig.eng.results
    assert rig.eng.scan["state"] == "done" and rig.eng.scan["done"] == rig.eng.scan["total"] == len(ECUS)

    eng = res["engine"]
    assert eng["status"] == "ok" and eng["responded"] and eng["mil"] is True and eng["dtc_count"] == 1
    (p0420,) = eng["dtcs"]
    assert p0420["code"] == "P0420" and set(p0420["status"]) == {"stored", "freeze_frame"}
    assert p0420["manual_page"] == "05-139" and p0420["description"].startswith("Catalyst")
    assert eng["freeze_frame"]["dtc"] == "P0420"
    assert eng["freeze_frame"]["values"]["rpm"] == {"value": 1200.0, "unit": "rpm"}

    # The HV ECU's mode 03 is empty; the master-warning codes come from 0x13.
    hv = res["hv"]
    assert [d["code"] for d in hv["dtcs"]] == ["P0852", "P3102"]
    assert hv["dtcs"][0]["sources"] == ["toyota 13"] and hv["dtcs"][0]["master_warning"] == "all"
    assert hv["mil"] is False

    assert res["battery"]["status"] == "ok" and res["battery"]["dtcs"] == []
    # K-line / BEAN ECUs are listed, never addressed in a default scan.
    for key in ("brake", "eps", "airbag", "transmission", "body", "power_source"):
        assert res[key]["status"] == "unreachable" and not res[key]["responded"]
    sent_ids = {int(p["i"], 16) for p in rig.gw.sent}
    assert sent_ids == {0x7E0, 0x7E2, 0x7E3}


def test_one_request_at_a_time_and_bus_load_cap():
    cfg = DiagConfig(min_gap_s=0.05, max_frames_per_s=5)
    rig = prius_rig(config=cfg)
    rig.eng.start_scan(kinds=["stored", "pending", "freeze", "ident"])
    rig.run()
    assert rig.max_outstanding == 1
    assert len(rig.tx_times) > 15
    # never more than 5 frames in any one-second window, never closer than the gap
    for i, t in enumerate(rig.tx_times):
        assert sum(1 for u in rig.tx_times if t <= u < t + 1.0) <= 5
        if i:
            assert t - rig.tx_times[i - 1] >= 0.05 - 1e-9


def test_every_frame_is_a_gateway_req_with_one_response_id():
    rig = prius_rig()
    rig.eng.start_scan()
    rig.run()
    for p in rig.gw.sent:
        assert p["a"] == "req" and len(p["d"]) == 8 and len(p["r"]) == 1
        assert int(p["r"][0], 16) == int(p["i"], 16) + 8
        assert p["isotp"] is True and p["t"] >= 300


def test_non_answering_ecu_gets_one_request():
    rig = prius_rig()
    ecu_of(rig, 0x7E3).alive = False
    rig.eng.start_scan()
    rig.run()
    batt = rig.eng.results["battery"]
    assert batt["status"] == "no_answer" and not batt["responded"] and "0x7E3" in batt["error"]
    assert len(rig.requests(0x7E3)) == 1
    assert rig.eng.results["engine"]["status"] == "ok"        # the scan went on


def test_gateway_not_connected():
    rig = prius_rig()
    rig.gw.connected = False
    rig.eng.start_scan(["engine"])
    rig.run()
    r = rig.eng.results["engine"]
    assert r["status"] == "no_answer" and "NOT_CONNECTED" in r["error"]


def test_dead_gateway_times_out_on_the_host():
    rig = prius_rig()
    rig.gw.drop_replies = True
    rig.eng.start_scan(["engine"])
    rig.run()
    r = rig.eng.results["engine"]
    assert r["status"] == "no_answer" and "no_gateway_reply" in r["error"]
    assert len(rig.gw.sent) == 1


def test_probe_scan_sends_one_frame_per_unverified_address():
    rig = prius_rig()
    rig.eng.start_scan(kinds=["stored", "probe"])
    rig.run()
    for key in ("brake", "eps", "airbag", "transmission", "meter", "ac", "body", "gateway"):
        r = rig.eng.results[key]
        assert r["status"] == "no_answer" and r["error"].startswith("probe: no answer")
    assert rig.requests(0x7B0) == [bytes([0x01, 0x3E, 0, 0, 0, 0, 0, 0])]
    # sub-addressed probe: sub-address, PCI, tester present; raw (no gateway ISO-TP)
    body = [p for p in rig.gw.sent if int(p["i"], 16) == 0x750]
    assert [bytes(p["d"])[:3] for p in body] == [bytes([0x40, 0x01, 0x3E]), bytes([0x5F, 0x01, 0x3E])]
    assert all(p["isotp"] is False and p["t"] < 300 and p["r"] == ["0x758"] for p in body)
    assert rig.eng.results["power_source"]["status"] == "unreachable"   # no address at all


def test_probe_that_answers_is_read():
    rig = prius_rig()
    rig.gw.add(SimEcu(0x7B0, 0x7B8, responses={b"\x3E": b"\x7E", b"\x13": bytes([0x53, 0x52, 0x71, 0x00, 0x00])}))
    rig.eng.start_scan(["brake"], kinds=["probe"])
    rig.run()
    r = rig.eng.results["brake"]
    assert r["status"] == "ok" and [d["code"] for d in r["dtcs"]] == ["C1271"]


def test_toyota_read_alternative_form_after_format_rejection():
    rig = prius_rig()
    hv = ecu_of(rig, 0x7E2)
    hv.responses[b"\x13"] = b"\x7F\x13\x12"
    hv.responses[b"\x13\xFF\x00"] = bytes([0x53, 0x08, 0x52, 0x00, 0x00])
    rig.eng.start_scan(["hv"])
    rig.run()
    assert [d["code"] for d in rig.eng.results["hv"]["dtcs"]] == ["P0852"]
    assert b"\x03\x13\xFF\x00" in [r[:4] for r in rig.requests(0x7E2)]


def test_negative_response_is_an_answer_not_a_failure():
    rig = prius_rig()
    ecu_of(rig, 0x7E3).toyota = None             # 0x13 -> 7F 13 11
    rig.eng.start_scan(["battery"])
    rig.run()
    r = rig.eng.results["battery"]
    assert r["status"] == "ok" and any("service not supported" in line for line in r["log"])


def test_scan_refused_while_running_and_bad_kinds():
    rig = prius_rig()
    assert rig.eng.start_scan()[0]
    assert rig.eng.start_scan() == (False, "a scan is already running")
    rig.run()
    assert rig.eng.start_scan(kinds=["bogus"])[0] is False
    assert rig.eng.start_scan(ecus=["nope"])[0] is False


def test_cancel_stops_transmitting():
    rig = prius_rig()
    rig.eng.start_scan()
    for _ in range(5):
        rig.eng.tick(); rig.gw.step(); rig.t += 0.01
    rig.eng.cancel()
    n = len(rig.gw.sent)
    rig.run()
    assert len(rig.gw.sent) == n and rig.eng.scan["state"] == "cancelled" and not rig.eng.busy


def test_identify_reads_vin():
    rig = prius_rig()
    rig.eng.start_identify(["engine"])
    rig.run()
    assert rig.eng.vehicle["vin"] == "JTDKB20U040000000"
    assert rig.eng.results["engine"]["ident"]["vin"] == "JTDKB20U040000000"


def test_read_item_named_and_hex():
    rig = prius_rig()
    ecu_of(rig, 0x7E0).responses[b"\x01\x05"] = bytes([0x41, 0x05, 0x7D])
    assert rig.eng.start_read("engine", "coolant_temp")[0]
    rig.run()
    assert rig.eng.last_read["values"] == {"coolant_temp_c": 85}
    assert rig.eng.last_read["response"] == "41 05 7D"
    assert rig.eng.start_read("engine", "0105")[0]
    rig.run()
    assert rig.eng.last_read["request"] == "01 05" and "values" not in rig.eng.last_read


def test_multi_frame_data_list_through_the_gateway():
    rig = prius_rig()
    data = bytes([0x61, 0xC3]) + bytes(range(37))
    ecu_of(rig, 0x7E2).responses[b"\x21\xC3"] = data
    rig.eng.start_read("hv", "21C3")
    rig.run()
    assert bytes.fromhex(rig.eng.last_read["response"].replace(" ", "")) == data


def test_forbidden_services_never_reach_the_gateway():
    rig = prius_rig()
    for item in ("1101", "2701", "3021", "04", "14", "3B00"):
        ok, msg = rig.eng.start_read("engine", item)
        assert not ok
    rig.run()
    assert rig.gw.sent == []


# ── host-side ISO-TP on a sub-addressed ECU ──────────────────────────────────

LONG = bytes([0x53, 0x04, 0xA2, 0x78, 0xA2, 0x81, 0x92, 0x10, 0x92, 0x60])     # 10 bytes -> FF + 1 CF
VERY_LONG = bytes([0x61, 0x01]) + bytes(range(30))                             # FF + 5 CF


def sub_rig(responses, honour_bs=True, bus_tap=False):
    ecus = {"body": Ecu(key="body", name="Body", tx_id=0x750, rx_id=0x758, sub_addr=0x40,
                        toyota_dtc=True, dict_key="power_source", legacy_name="BODY")}
    rig = Rig(ecus=ecus, bus_tap=bus_tap)
    rig.gw.add(SimEcu(0x750, 0x758, sub_addr=0x40, responses=responses, honour_bs=honour_bs))
    rig.gw.add(SimEcu(0x750, 0x758, sub_addr=0x5F, responses={b"\x13": b"\x53\x00"}))
    return rig


def test_extended_addressing_single_frame():
    rig = sub_rig({b"\x13": bytes([0x53, 0x01, 0xA2, 0x78])})
    rig.eng.start_scan()
    rig.run()
    assert [d["code"] for d in rig.eng.results["body"]["dtcs"]] == ["B2278"]
    (p,) = rig.gw.sent
    assert bytes(p["d"]) == bytes([0x40, 0x01, 0x13, 0, 0, 0, 0, 0]) and p["isotp"] is False


def test_extended_addressing_multi_frame_with_block_size_one():
    rig = sub_rig({b"\x13": LONG})
    rig.eng.start_scan()
    rig.run()
    assert [d["code"] for d in rig.eng.results["body"]["dtcs"]] == ["B1210", "B1260", "B2278", "B2281"]
    frames = [bytes(p["d"]) for p in rig.gw.sent]
    assert frames[1] == bytes([0x40, 0x30, 0x01, 0x00, 0, 0, 0, 0])      # flow control: sub, 30, BS=1, STmin
    assert len(frames) == 2 and rig.max_outstanding == 1


def test_extended_addressing_long_message_ping_pong():
    rig = sub_rig({b"\x21\x01": VERY_LONG})
    rig.eng.start_read("body", "2101")
    rig.run()
    assert bytes.fromhex(rig.eng.last_read["response"].replace(" ", "")) == VERY_LONG
    assert len(rig.gw.sent) == 1 + 5 and rig.max_outstanding == 1      # one flow control per consecutive frame


def test_ecu_ignoring_block_size_without_a_bus_tap_reports_partial():
    rig = sub_rig({b"\x13": bytes([0x53, 0x08]) + bytes([0xA2, 0x78] * 8)}, honour_bs=False)
    rig.eng.start_scan()
    rig.run()
    r = rig.eng.results["body"]
    # The gateway returns one frame per job: the rest is lost, the first codes are kept.
    assert r["partial"] is True and r["dtcs"][0]["code"] == "B2278"
    assert any("incomplete multi-frame" in line for line in r["log"])


def test_ecu_ignoring_block_size_completes_through_the_bus_tap():
    rig = sub_rig({b"\x21\x01": VERY_LONG}, honour_bs=False, bus_tap=True)
    rig.eng.start_read("body", "2101")
    rig.run()
    assert bytes.fromhex(rig.eng.last_read["response"].replace(" ", "")) == VERY_LONG


def test_bus_tap_duplicates_do_not_corrupt():
    rig = sub_rig({b"\x21\x01": VERY_LONG}, bus_tap=True)
    rig.eng.start_read("body", "2101")
    rig.run()
    assert bytes.fromhex(rig.eng.last_read["response"].replace(" ", "")) == VERY_LONG


def test_response_pending_then_final_answer_on_the_bus_tap():
    rig = sub_rig({b"\x13": b"\x7F\x13\x78"}, bus_tap=True)
    rig.eng.start_scan()
    fired = []

    def late():
        if rig.t > 0.5 and not fired:
            fired.append(1)
            rig.eng.on_bus_frame(0x758, bytes([0x40, 0x04, 0x53, 0x01, 0xA2, 0x78, 0, 0]), rig.t)
    rig.run(extra=late)
    assert [d["code"] for d in rig.eng.results["body"]["dtcs"]] == ["B2278"]


def test_response_pending_without_final_answer():
    rig = sub_rig({b"\x13": b"\x7F\x13\x78"})
    rig.eng.start_scan()
    rig.run()
    r = rig.eng.results["body"]
    assert r["dtcs"] == [] and any("pending" in line for line in r["log"])


def test_other_sub_address_replies_are_not_mine():
    rig = sub_rig({})
    rig.gw.ecus[0].alive = False
    rig.eng.start_scan()
    # a frame of sub-address 0x5F on 0x758 while 0x40 is being asked
    rig.eng.tick()
    rig.eng.on_bus_frame(0x758, bytes([0x5F, 0x02, 0x53, 0x00, 0, 0, 0, 0]), 0.0)
    rig.run()
    assert rig.eng.results["body"]["status"] == "no_answer"


def test_garbage_gateway_lines_are_ignored():
    rig = prius_rig()
    rig.eng.start_scan(["engine"])
    rig.eng.tick()
    for junk in ({}, {"a": "sub", "slot": 1, "i": "0x7E8", "d": [0x43, 0]}, {"a": "resp", "i": "0x7EA", "d": [1]},
                 {"a": "resp", "i": "zz", "d": [1]}, {"a": "resp", "i": "0x7E8", "d": "nope"},
                 {"a": "resp", "i": "0x7E8", "d": ["x"]}, {"a": "resp", "err": "TIMEOUT", "i": "0x7EB", "req": "0x7E3"}):
        assert rig.eng.on_gateway_message(junk) is False
    rig.run()
    assert rig.eng.results["engine"]["status"] == "ok"


# ── clear gating ─────────────────────────────────────────────────────────────

def test_clear_needs_confirmation():
    rig = prius_rig()
    assert rig.eng.start_clear("engine")[0] is False
    assert rig.eng.start_clear("engine", confirm="yes")[0] is False
    assert rig.eng.start_clear("brake", confirm=True)[0] is False          # no clear service known
    assert rig.eng.start_clear("nope", confirm=True)[0] is False
    rig.run()
    assert rig.gw.sent == []


def test_clear_reads_first_then_clears_then_reads_back():
    logged = []
    rig = prius_rig()
    rig.eng.on_clear = logged.append
    assert rig.eng.start_clear("engine", confirm=True)[0]
    rig.run()
    reqs = [r[1] for r in rig.requests(0x7E0)]                 # service id of each request
    i_clear = reqs.index(0x04)
    assert 0x03 in reqs[:i_clear] and 0x02 in reqs[:i_clear]   # codes + freeze frame BEFORE the clear
    assert 0x03 in reqs[i_clear + 1:]                          # read back after
    assert reqs.count(0x04) == 1 and ecu_of(rig, 0x7E0).clears == 1
    (rec,) = logged
    assert rec["ok"] and rec["origin"] == "manual" and rec["after"] == []
    assert [d["code"] for d in rec["before"]["dtcs"]] == ["P0420"]
    assert rec["before"]["freeze_frame"]["dtc"] == "P0420"
    assert rig.eng.results["engine"]["last_clear"]["cleared"] == ["P0420"]
    assert rig.eng.results["engine"]["dtcs"] == []
    # only the engine ECU was cleared
    assert not any(r[1] == 0x04 for r in rig.requests(0x7E2))


def test_clear_refused_while_moving():
    logged = []
    rig = prius_rig()
    rig.eng.on_clear = logged.append
    ecu_of(rig, 0x7E0).speed = 42
    rig.eng.start_clear("hv", confirm=True)
    rig.run()
    assert not any(r[1] == 0x04 for r in rig.requests())
    assert logged[0]["ok"] is False and "42 km/h" in logged[0]["result"]
    assert rig.eng.results["hv"]["last_clear"]["ok"] is False


def test_clear_while_moving_only_with_the_override():
    rig = prius_rig()
    ecu_of(rig, 0x7E0).speed = 42
    rig.eng.start_clear("engine", confirm=True, allow_moving=True)
    rig.run()
    assert ecu_of(rig, 0x7E0).clears == 1


def test_clear_speed_falls_back_to_broadcast_speed():
    rig = prius_rig(speed=30.0)
    ecu_of(rig, 0x7E0).responses[b"\x01\x0D"] = lambda _r: None       # ECM does not report speed
    rig.eng.start_clear("hv", confirm=True)
    rig.run()
    assert not any(r[1] == 0x04 for r in rig.requests())
    rig2 = prius_rig(speed=None)
    ecu_of(rig2, 0x7E0).responses[b"\x01\x0D"] = lambda _r: None
    rig2.eng.start_clear("hv", confirm=True)
    rig2.run()
    assert not any(r[1] == 0x04 for r in rig2.requests())             # speed unknown -> no clear


def test_clear_refused_by_the_ecu_is_reported():
    rig = prius_rig()
    ecu_of(rig, 0x7E0).refuse_clear = True
    rig.eng.start_clear("engine", confirm=True)
    rig.run()
    lc = rig.eng.results["engine"]["last_clear"]
    assert lc["ok"] is False and "conditions not correct" in lc["result"] and lc["remaining"] == ["P0420"]


def test_no_clear_during_a_scan():
    rig = prius_rig()
    rig.eng.start_scan()
    assert rig.eng.start_clear("engine", confirm=True) == (False, "a scan is running")
    rig.run()
    assert ecu_of(rig, 0x7E0).clears == 0


# ── state / report ───────────────────────────────────────────────────────────

def test_state_is_json_and_published():
    seen = []
    rig = prius_rig(publish=seen.append)
    rig.eng.start_scan()
    rig.run()
    assert seen and seen[-1]["scan"]["state"] == "done"
    text = json.dumps(seen[-1])
    for key in ("name", "address", "responded", "protocol", "dtcs", "last_scan_ts", "error"):
        assert key in seen[-1]["ecus"]["engine"]
    assert "P0852" in text


def test_report_export(tmp_path):
    d = str(tmp_path / "diag")
    rig = prius_rig(on_scan_done=lambda rep: reportmod.write_report(d, rep))
    rig.eng.start_scan(kinds=["stored", "pending", "freeze", "ident"])
    rig.run()
    name = rig.eng.scan["report_file"]
    assert name.startswith("diag_report_") and name.endswith(".json")
    rep = reportmod.read_report(d)
    assert rep["vin"] == "JTDKB20U040000000" and rep["format"] == "prius-diag-report/1"
    assert [x["code"] for x in rep["ecus"]["hv"]["dtcs"]] == ["P0852", "P3102"]
    assert rep["trace"][0]["req"] == "01 01" and "resp" in rep["trace"][0]
    assert reportmod.list_reports(d)[0]["name"] == name
    assert reportmod.read_report(d, "../../etc/passwd") is None
    assert reportmod.read_report(d, "diag_report_19990101_000000.json") is None


def test_all_codes_for_the_mfd_screen():
    rig = prius_rig()
    rig.eng.start_scan()
    rig.run()
    assert sorted((c, e) for c, e, _ in rig.eng.all_codes()) == [
        ("P0420", "ENGINE"), ("P0852", "HYBRID"), ("P3102", "HYBRID")]
