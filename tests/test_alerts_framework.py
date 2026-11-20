"""Alert framework: manager rules (modes, gating, debounce, latch, cooldown,
arbitration, acknowledge, drives), persistence, the backend service glue and
the REST routes. The functions on top have their own test files."""

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from cyberpunk_computer.alerts.inputs import AlertInputs, build_inputs
from cyberpunk_computer.alerts.manager import AlertManager
from cyberpunk_computer.alerts.model import DRIVE_GEARS, AlertFunction, AlertSpec, Candidate
from cyberpunk_computer.alerts.persist import AlertLog, AlertSettings
from cyberpunk_computer.alerts.sinks import NullNotificationSink, NullSoundSink
from cyberpunk_computer.alerts.tuning import AlertTuning, apply_overrides


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Switch(AlertFunction):
    """Test function: each type fires while its switch is set."""

    name = "switch"

    def __init__(self, *specs):
        self.specs = tuple(specs)
        self.on = {}
        self.keys = {}
        self.drives = 0

    def evaluate(self, inp, ctx):
        return {aid: Candidate(key=self.keys.get(aid), evidence={"x": 1}) if v else None
                for aid, v in self.on.items()}

    def on_drive_start(self, inp, ctx):
        self.drives += 1


def spec(aid="a", **kw):
    base = dict(id=aid, title=aid, text=aid.upper(), min_show_s=0.0, max_show_s=None,
                cooldown_s=0.0, clear_after_s=0.5)
    base.update(kw)
    return AlertSpec(**base)


def inp(clk, **kw):
    base = dict(now=clk.t, wall=clk.t, real_kmh=50.0, gear="DRIVE", ready=True)
    base.update(kw)
    return AlertInputs(**base)


def make(fn, tmp_path=None, **kw):
    clk = Clock()
    pub = []
    settings = AlertSettings(str(tmp_path) if tmp_path else None)
    snd, ntf = NullSoundSink(), NullNotificationSink()
    acts = []
    m = AlertManager([fn], settings, AlertLog(None), snd, ntf, publish=pub.append,
                     actuator=lambda a, b, c: acts.append((a, b)) or True, clock=clk, wall=clk, **kw)
    return m, clk, pub, snd, ntf, acts


def run(m, clk, secs, step=0.1, **kw):
    end = clk.t + secs
    while clk.t < end - 1e-9:
        m.tick(inp(clk, **kw))
        clk.t += step


def fires(m, aid=None):
    return [e for e in m.log.recent_entries(500) if e.get("event") == "fire"
            and (aid is None or e["id"] == aid)][::-1]


# ── modes ────────────────────────────────────────────────────────────────────

def test_default_mode_is_shadow_and_shadow_shows_nothing():
    fn = Switch(spec("a", sound="notice"))
    m, clk, pub, snd, _, _ = make(fn)
    assert m.mode("a") == "shadow"
    fn.on["a"] = True
    run(m, clk, 1.0)
    assert len(fires(m)) == 1
    e = fires(m)[0]
    assert e["mode"] == "shadow" and e["evidence"] == {"x": 1} and e["inputs"]["real_kmh"] == 50.0
    assert m.shown is None and snd.count == 0 and all(p["alert"] is None for p in pub)


def test_on_shows_one_alert_and_sounds_once(tmp_path):
    fn = Switch(spec("a", sound="notice"))
    m, clk, pub, snd, _, _ = make(fn, tmp_path)
    assert m.set_mode("a", "on") == (True, None)
    fn.on["a"] = True
    run(m, clk, 3.0)
    assert m.shown == "a" and snd.count == 1
    assert pub[-1]["alert"]["id"] == "a" and pub[-1]["alert"]["text"] == "A"
    fn.on["a"] = False
    run(m, clk, 1.0)
    assert m.shown is None and pub[-1]["alert"] is None
    # mode persisted
    assert json.loads((tmp_path / "alerts_settings.json").read_text())["modes"]["a"] == "on"
    assert AlertSettings(str(tmp_path)).mode("a") == "on"


def test_off_ignores_and_bad_mode_refused():
    fn = Switch(spec("a"))
    m, clk, *_ = make(fn)
    assert m.set_mode("a", "loud")[0] is False
    assert m.set_mode("zzz", "on") == (False, "unknown alert id")
    m.set_mode("a", "off")
    fn.on["a"] = True
    run(m, clk, 1.0)
    assert fires(m) == []


def test_enable_check_refuses_on_but_allows_shadow():
    blocked = {"why": "turn signal not decoded"}
    fn = Switch(spec("lane", enable_check=lambda: blocked["why"]))
    m, clk, *_ = make(fn)
    ok, why = m.set_mode("lane", "on")
    assert not ok and why == "turn signal not decoded"
    assert m.set_mode("lane", "shadow") == (True, None)
    # a stored "on" never takes effect while blocked
    m.settings.set_mode("lane", "on")
    assert m.mode("lane") == "shadow"
    blocked["why"] = None
    assert m.mode("lane") == "on"


# ── timing ───────────────────────────────────────────────────────────────────

def test_debounce_latch_and_rearm():
    fn = Switch(spec("a", debounce_s=1.0))
    m, clk, *_ = make(fn)
    fn.on["a"] = True
    run(m, clk, 0.8)
    assert fires(m) == []
    run(m, clk, 0.5)
    assert len(fires(m)) == 1
    run(m, clk, 5.0)                       # still true: latched, no repeat
    assert len(fires(m)) == 1
    fn.on["a"] = False
    run(m, clk, 1.0)                       # absent > clear_after_s: re-armed
    fn.on["a"] = True
    run(m, clk, 1.2)
    assert len(fires(m)) == 2


def test_new_key_rearms_while_condition_holds():
    fn = Switch(spec("a"))
    m, clk, *_ = make(fn)
    fn.on["a"] = True
    fn.keys["a"] = "seg1"
    run(m, clk, 1.0)
    fn.keys["a"] = "seg2"
    run(m, clk, 1.0)
    assert [e["key"] for e in fires(m)] == ["seg1", "seg2"]


def test_cooldown_and_max_per_drive():
    fn = Switch(spec("a", cooldown_s=10.0, max_per_drive=2))
    m, clk, *_ = make(fn)
    for _ in range(6):
        fn.on["a"] = True
        run(m, clk, 0.5)
        fn.on["a"] = False
        run(m, clk, 1.0)
    # 9 s: one fire (cooldown)
    assert len(fires(m)) == 1
    for _ in range(30):
        fn.on["a"] = True
        run(m, clk, 0.5)
        fn.on["a"] = False
        run(m, clk, 1.0)
    assert len(fires(m)) == 2              # max per drive


def test_gating_speed_gear_ready_and_off():
    fn = Switch(spec("a", min_kmh=30, gears=DRIVE_GEARS, require_ready=True),
                spec("p", require_off=True))
    m, clk, *_ = make(fn)
    fn.on.update(a=True, p=True)
    run(m, clk, 1.0, real_kmh=20.0)
    run(m, clk, 1.0, gear="REVERSE")
    run(m, clk, 1.0, ready=False, acc=True)
    assert fires(m, "a") == [] and fires(m, "p") == []
    run(m, clk, 1.0, ready=False, real_kmh=0.0, gear="PARK")
    assert len(fires(m, "p")) == 1
    run(m, clk, 1.0)
    assert len(fires(m, "a")) == 1


# ── display ──────────────────────────────────────────────────────────────────

def test_arbitration_warning_preempts_notice_and_notice_returns():
    fn = Switch(spec("n", severity="notice", priority=80, min_show_s=3.0),
                spec("w", severity="warning", priority=10))
    m, clk, pub, *_ = make(fn)
    m.set_mode("n", "on")
    m.set_mode("w", "on")
    fn.on["n"] = True
    run(m, clk, 0.5)
    assert m.shown == "n"
    fn.on["w"] = True
    run(m, clk, 0.3)
    assert m.shown == "w"                  # pre-empts although n is within min_show_s
    fn.on["w"] = False
    run(m, clk, 1.0)
    assert m.shown == "n"                  # still holds: shown again


def test_same_severity_waits_for_min_show():
    fn = Switch(spec("lo", priority=10, min_show_s=3.0), spec("hi", priority=90))
    m, clk, *_ = make(fn)
    m.set_mode("lo", "on")
    m.set_mode("hi", "on")
    fn.on["lo"] = True
    run(m, clk, 1.0)
    fn.on["hi"] = True
    run(m, clk, 1.0)
    assert m.shown == "lo"
    run(m, clk, 1.5)
    assert m.shown == "hi"


def test_max_show_timeout_and_min_show_hold():
    fn = Switch(spec("a", max_show_s=4.0, min_show_s=2.0))
    m, clk, pub, *_ = make(fn)
    m.set_mode("a", "on")
    fn.on["a"] = True
    run(m, clk, 0.3)
    fn.on["a"] = False
    run(m, clk, 1.0)
    assert m.shown == "a"                  # condition gone, but held 2 s
    run(m, clk, 1.5)
    assert m.shown is None
    fn.on["a"] = True
    run(m, clk, 4.5)
    assert m.shown is None                 # timed out, condition still true, latched
    assert pub[-1]["alert"] is None


def test_ack_is_per_event_and_long_press_mutes_the_drive():
    fn = Switch(spec("a", cooldown_s=0.0))
    m, clk, *_ = make(fn)
    m.set_mode("a", "on")
    fn.keys["a"] = "k1"
    fn.on["a"] = True
    run(m, clk, 0.5)
    assert m.acknowledge() is True and m.shown is None
    fn.on["a"] = False
    run(m, clk, 1.0)
    fn.on["a"] = True
    run(m, clk, 1.0)
    assert len(fires(m)) == 1              # same event key: acknowledged
    fn.keys["a"] = "k2"
    run(m, clk, 0.5)
    assert len(fires(m)) == 2 and m.shown == "a"
    assert m.acknowledge("a", long=True)
    fn.keys["a"] = "k3"
    run(m, clk, 1.0)
    assert len(fires(m)) == 2              # muted for the drive
    # next drive: READY off for > 60 s
    run(m, clk, 61.0, step=1.0, ready=False)
    run(m, clk, 1.0)
    assert len(fires(m)) == 3 and fn.drives == 2


def test_not_ackable_alert_ignores_ack():
    fn = Switch(spec("a", ackable=False))
    m, clk, *_ = make(fn)
    m.set_mode("a", "on")
    fn.on["a"] = True
    run(m, clk, 0.5)
    assert m.acknowledge() is False and m.shown == "a"


def test_republish_while_shown():
    fn = Switch(spec("a"))
    m, clk, pub, *_ = make(fn)
    m.set_mode("a", "on")
    fn.on["a"] = True
    run(m, clk, 7.0)
    shown = [p for p in pub if p["alert"]]
    assert len(shown) >= 3 and len({p["alert"]["seq"] for p in shown}) == 1


# ── kinds ────────────────────────────────────────────────────────────────────

class Auto(AlertFunction):
    name = "auto"
    specs = (AlertSpec(id="auto", title="auto", text="", kind="automation"),)

    def __init__(self):
        self.go = False
        self.results = []

    def evaluate(self, inp, ctx):
        if self.go:
            self.go = False
            self.results.append(ctx.actuate("auto", "recirc_on", {"why": "test"}))
        return {}


def test_automation_logs_in_shadow_and_executes_in_on():
    fn = Auto()
    m, clk, pub, snd, ntf, acts = make(fn)
    fn.go = True
    run(m, clk, 0.2)
    assert fn.results == [False] and acts == []
    m.set_mode("auto", "on")
    fn.go = True
    run(m, clk, 0.2)
    assert fn.results == [False, True] and acts == [("auto", "recirc_on")]
    ent = [e for e in m.log.recent_entries(50) if e.get("event") == "action"]
    assert [e["executed"] for e in ent] == [True, False]       # newest first
    assert m.shown is None


def test_event_kind_goes_to_notification_sink_only_in_on():
    fn = Switch(spec("ev", kind="event", require_off=True))
    m, clk, pub, snd, ntf, _ = make(fn)
    fn.on["ev"] = True
    run(m, clk, 1.0, ready=False, real_kmh=0.0)
    assert ntf.count == 0 and len(fires(m)) == 1
    m.set_mode("ev", "on")
    fn.on["ev"] = False
    run(m, clk, 1.0, ready=False)
    fn.on["ev"] = True
    run(m, clk, 1.0, ready=False)
    assert ntf.count == 1 and m.shown is None


def test_failing_function_does_not_stop_others():
    class Bad(AlertFunction):
        name = "bad"
        specs = (spec("b"),)

        def evaluate(self, inp, ctx):
            raise RuntimeError("boom")

    good = Switch(spec("g"))
    clk = Clock()
    m = AlertManager([Bad(), good], clock=clk, wall=clk)
    good.on["g"] = True
    run(m, clk, 1.0)
    assert m.errors >= 1 and len(fires(m, "g")) == 1


def test_duplicate_ids_rejected():
    with pytest.raises(ValueError):
        AlertManager([Switch(spec("a")), Switch(spec("a"))])


def test_status_lists_types_counts_and_last():
    fn = Switch(spec("a"))
    m, clk, *_ = make(fn)
    fn.on["a"] = True
    run(m, clk, 1.0)
    st = m.status()
    t = st["types"][0]
    assert t["id"] == "a" and t["mode"] == "shadow" and t["fires_total"] == 1
    assert t["last"]["event"] == "fire" and st["sound"]["sink"] == "null"


# ── persistence ──────────────────────────────────────────────────────────────

def test_log_file_rotation_and_export(tmp_path):
    log = AlertLog(str(tmp_path), max_bytes=400, keep_files=3)
    log.start()
    for i in range(40):
        log.append({"ts": 1000.0 + i, "event": "fire", "id": "a", "n": i})
    log.stop()
    files = log.files()
    assert 1 < len(files) <= 3
    lines = list(log.iter_export())
    nums = [json.loads(x)["n"] for x in lines]
    assert nums == sorted(nums) and nums[-1] == 39
    assert all(json.loads(x)["ts"] >= 1030 for x in log.iter_export(since_wall=1030))


def test_log_export_from_memory_without_directory():
    log = AlertLog(None)
    log.append({"ts": 1.0, "event": "fire", "id": "a"})
    assert len(list(log.iter_export())) == 1


def test_settings_survive_a_corrupt_file(tmp_path):
    (tmp_path / "alerts_settings.json").write_text("{not json")
    s = AlertSettings(str(tmp_path))
    assert s.mode("a") is None
    s.set_mode("a", "on")
    assert s.save() and AlertSettings(str(tmp_path)).mode("a") == "on"


def test_tuning_overrides_validate_types():
    t = AlertTuning()
    probs = apply_overrides(t, {"speed_limit": {"tolerance_kmh": 8, "weak_policy": "off",
                                                "delay_s": "x", "nope": 1},
                                "bogus": {}})
    assert t.speed_limit.tolerance_kmh == 8.0 and t.speed_limit.weak_policy == "off"
    assert t.speed_limit.delay_s == 5.0 and len(probs) == 3


# ── inputs ───────────────────────────────────────────────────────────────────

def _state():
    from cyberpunk_computer.state.app_state import AppState, GearPosition
    st = AppState()
    v = replace(st.vehicle, real_speed_kmh=48.0, display_speed_kmh=54.0, gear=GearPosition.DRIVE,
                ready_mode=True, brake_pressed=12)
    bean = replace(st.bean, decoded={"doors_locked": {"value": True, "ts": 5.0, "src": "FE:B6"},
                                     "odometer_km": {"value": 251234, "ts": 5.0, "src": "52:3F"}})
    return replace(st, vehicle=v, bean=bean)


def test_build_inputs_uses_true_speed_and_bean():
    i = build_inputs(_state(), None, None, 10.0, 10.0, {})
    assert i.real_kmh == 48.0 and i.display_kmh == 54.0 and i.gear == "DRIVE"
    assert i.brake is True and i.doors_locked is True and i.odometer_km == 251234
    assert i.fuel_l is None                       # placeholder 30 L is not a value
    assert build_inputs(_state(), None, None, 10.0, 10.0, {"fuel": True}).fuel_l == 30.0
    assert i.turn_signal == "unknown"


def test_build_inputs_limit_prefers_usable_map_layer():
    scene = {"limit": {"kmh": 50, "src": "sign", "conf": 0.7},
             "map": {"ok": True, "conf": 0.95, "limit": {"kmh": 70, "src": "map", "conf": 0.9,
                                                         "agree": "map_only"}}}
    i = build_inputs(_state(), scene, 0.2, 10.0, 10.0, {})
    assert i.limit.kmh == 70 and i.limit.src == "map" and i.limit.from_map_layer and i.map_ok
    syn = dict(scene, map=dict(scene["map"], syn=True))
    i2 = build_inputs(_state(), syn, 0.2, 10.0, 10.0, {})
    assert i2.limit.kmh == 50 and not i2.limit.from_map_layer and not i2.map_ok
    stale = build_inputs(_state(), scene, 5.0, 10.0, 10.0, {})
    assert stale.limit is None and stale.lead is None


def test_build_inputs_lead_tracked_first():
    scene = {"lead": {"d": 20.0, "y": 0.1, "v_rel": -1.0, "prob": 0.9},
             "map": {"ok": False, "lead": {"present": True, "d": 19.5, "v_rel": -0.8,
                                           "standing": False, "departed": False}}}
    i = build_inputs(_state(), scene, 0.1, 10.0, 10.0, {})
    assert i.lead.src == "tracked" and i.lead.d == 19.5 and i.lead_tracked_available
    del scene["map"]
    i = build_inputs(_state(), scene, 0.1, 10.0, 10.0, {})
    assert i.lead.src == "vision" and i.lead.prob == 0.9 and not i.lead_tracked_available


def test_build_inputs_reads_the_lead_topic_first():
    # roadsense's lead topic (scene lead + lead_src) beats fused's slower copy in map.lead
    scene = {"lead": {"d": 5.4, "y": 0.1, "v_rel": 0.0, "a": 0.0, "src": "tracked", "standing": True,
                      "gap": None, "id": 7},
             "lead_src": "tracked",
             "map": {"ok": False, "lead": {"present": True, "d": 6.0, "standing": False}}}
    i = build_inputs(_state(), scene, 0.1, 10.0, 10.0, {})
    assert i.lead.src == "tracked" and i.lead.d == 5.4 and i.lead.standing and i.lead.track_id == 7
    assert i.lead_tracked_available
    # the tracker has no lead: none, even with a vision-looking value elsewhere
    gone = dict(scene, lead=None)
    i = build_inputs(_state(), gone, 0.1, 10.0, 10.0, {})
    assert i.lead is None and i.lead_tracked_available
    # lost after pulling away: the departed cue survives without a distance
    i = build_inputs(_state(), dict(gone, lead_departed=True), 0.1, 10.0, 10.0, {})
    assert i.lead.departed and i.lead.src == "tracked" and i.lead.d == 0.0


def test_build_inputs_lead_unknown_is_not_no_lead():
    """L44: camera not believable (vis.ok false) or the backend's lead_unknown: lead None AND lead_unknown,
    and no fallback to the fused copy or the raw value is taken."""
    raw = {"d": 20.0, "y": 0.1, "v_rel": -1.0, "prob": 0.9}
    fused = {"ok": False, "lead": {"present": True, "d": 19.5}}
    for scene in ({"lead": raw, "map": fused, "vis": {"ok": False, "why": "vision: fault: x"}},
                  {"lead": None, "lead_src": "tracked", "lead_unknown": True, "map": fused},
                  {"lead": raw, "map": {"ok": False, "lead": {"present": True, "d": 19.5, "valid": False}}}):
        i = build_inputs(_state(), scene, 0.1, 10.0, 10.0, {})
        assert i.lead is None and i.lead_unknown, scene
        assert i.compact()["lead"] == "unknown"
    i = build_inputs(_state(), {"lead": None, "lead_src": "tracked", "vis": {"ok": True}}, 0.1, 10.0, 10.0, {})
    assert i.lead is None and not i.lead_unknown and i.vision_ok is True       # a known "no lead"
    i = build_inputs(_state(), {"lead": dict(raw, src="tracked", srcs=["cam", "radar:0"]), "lead_src": "tracked"},
                     0.1, 10.0, 10.0, {})
    assert i.lead.sources == ("cam", "radar:0") and i.vision_ok is None


def test_build_inputs_on_the_recorded_roadsense_scene():
    """Scenes built by the backend feed from the recorded roadsense messages."""
    import gzip
    from pathlib import Path
    from cyberpunk_computer.backend.roadsense_scene import SceneBuilder, SceneFeedConfig
    b = SceneBuilder(SceneFeedConfig())
    t = 0.0
    scenes = []
    with gzip.open(Path(__file__).parent / "fixtures" / "roadsense_ws_2026-11-14.jsonl.gz", "rt") as fh:
        for line in fh:
            t += 0.1
            b.feed(line.strip(), t)
            sc = b.build(t)
            if sc:
                scenes.append(sc)
    assert scenes
    i = build_inputs(_state(), scenes[-1], 0.1, 10.0, 10.0, {})
    assert i.scene_fresh and len(i.lanes) == 4
    assert i.limit is None or i.limit.kmh > 0


# ── service + routes ─────────────────────────────────────────────────────────

def _service(tmp_path, fn):
    from cyberpunk_computer.alerts.service import AlertService
    from cyberpunk_computer.state.store import Store
    store = Store()
    events = []
    clk = Clock()
    svc = AlertService(store, str(tmp_path), publish_event=lambda n, d: events.append((n, d)),
                       clock=clk, wall=clk, functions=[fn])
    svc.attach()
    return svc, store, events, clk


def test_service_ack_from_the_mfd_input_event(tmp_path):
    from cyberpunk_computer.state.actions import ActionSource, InputEventAction, SetReadyModeAction
    fn = Switch(spec("a"))
    svc, store, events, clk = _service(tmp_path, fn)
    svc.manager.set_mode("a", "on")
    store.dispatch(SetReadyModeAction(True))
    fn.on["a"] = True
    svc.tick(force=True)
    assert events[-1] == ("alert", {"alert": svc.manager.current()}) and svc.manager.shown == "a"
    store.dispatch(InputEventAction("mfd", "alert_ack", {"id": "a"}, source=ActionSource.UI))
    assert svc.manager.shown is None and events[-1][1]["alert"] is None
    svc.stop()


def test_service_actuator_only_dispatches_recirculation(tmp_path):
    from cyberpunk_computer.state.actions import ActionSource
    fn = Auto()
    svc, store, events, clk = _service(tmp_path, fn)
    seen = []
    store.add_middleware(lambda a, s: seen.append(a))
    assert svc._actuate("auto", "recirc_on", {}) is True
    assert svc._actuate("auto", "brake", {}) is False
    names = [type(a).__name__ for a in seen]
    assert names == ["SetRecirculationAction"] and seen[0].source == ActionSource.UI
    assert seen[0].recirculation is True
    svc.stop()


def test_service_scene_reaches_inputs(tmp_path):
    fn = Switch(spec("a"))
    svc, store, events, clk = _service(tmp_path, fn)
    svc.on_scene({"seq": 1, "lead": {"d": 12.0, "prob": 0.9}})
    clk.t += 0.2
    svc.tick(force=True)
    assert svc.manager.last_inputs.lead.d == 12.0
    assert svc.status()["scene"]["received"] == 1
    svc.stop()


def test_routes_over_http(tmp_path):
    from test_api_auth import _Bridge, _serve
    from cyberpunk_computer.api import app as app_mod
    from cyberpunk_computer.alerts.routes import register

    fn = Switch(spec("a"), spec("lane", enable_check=lambda: "turn signal not decoded"))
    svc, store, events, clk = _service(tmp_path, fn)
    fn.on["a"] = True
    svc.tick(force=True)
    app = app_mod.create_app(_Bridge(), db=object(), auth_token="tok",
                             extra_routes=[lambda a, p, auth: register(a, svc, p + "/alerts", auth)])
    server, t, port = _serve(app)
    hdr = {"Authorization": "Bearer tok", "Content-Type": "application/json"}

    def call(path, method="GET", body=None, headers=hdr):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        assert call("/api/v1/alerts", headers={})[0] == 401
        st, body = call("/api/v1/alerts")
        data = json.loads(body)
        assert st == 200 and {x["id"] for x in data["types"]} == {"a", "lane"}
        assert call("/api/v1/alerts/a/mode", "POST", {"mode": "on"})[0] == 200
        assert svc.manager.mode("a") == "on"
        st, body = call("/api/v1/alerts/lane/mode", "POST", {"mode": "on"})
        assert st == 409 and b"turn signal" in body
        assert call("/api/v1/alerts/a/mode", "POST", {"mode": "x"})[0] == 400
        assert call("/api/v1/alerts/nope/mode", "POST", {"mode": "on"})[0] == 404
        st, body = call("/api/v1/alerts/log?limit=5")
        assert st == 200 and json.loads(body)["entries"][0]["event"] in ("mode", "fire")
        st, body = call("/api/v1/alerts/log/export")
        assert st == 200 and all(json.loads(x) for x in body.decode().splitlines())
        assert json.loads(call("/api/v1/alerts/ack", "POST", {})[1])["acknowledged"] is False
    finally:
        server.should_exit = True
        t.join(5)
        svc.stop()


# ── scene keys added for the alert functions (backend/roadsense_scene.py) ────

def test_scene_carries_tracked_lead_and_light_relevance():
    from cyberpunk_computer.backend.roadsense_scene import fused_to_scene, lights_to_scene
    fused = {"map": {"available": False, "reason": "no_position"},
             "lead": {"present": True, "mode": "hold", "track_id": 7, "dist_m": 5.43, "lateral_m": 0.12,
                      "v_rel_mps": 0.0, "v_lead_mps": 0.0, "a_lead_mps2": 0.0, "time_gap_s": None,
                      "ttc_s": None, "standing": True, "departed": False, "source": "vision+vehicle"}}
    sc = fused_to_scene(fused)
    assert sc["lead"] == {"present": True, "d": 5.4, "y": 0.12, "v_rel": 0.0, "v": 0.0, "a": 0.0,
                          "standing": True, "mode": "hold", "id": 7}
    assert "lead" not in fused_to_scene({"map": {"available": False}, "lead": None})
    lt = lights_to_scene({"ego": {"state": "green", "dist_m": 22.4, "lateral_m": -0.2, "relevance": 0.644,
                                  "agree": 1}})
    assert lt["rel"] == 0.64 and lt["agree"] == 1
    i = build_inputs(_state(), {"map": sc}, 0.1, 10.0, 10.0, {})
    assert i.lead.src == "tracked" and i.lead.standing and i.lead.d == 5.4
