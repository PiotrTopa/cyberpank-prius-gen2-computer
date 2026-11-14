"""Climate view-model (state/climate_model.py): reading every climate field from
the store, optimistic values that reconcile with the next AVC status, and the
end-to-end command path

    ClimateController -> store action -> RemoteStore._translate (frontend)
      -> api.commands.build_command (backend) -> egress -> ClimateRemote
      -> AVC press frame 110->1C6 00 25 E0 84 ...

checked against the remote's own frame builder (climate_remote.press_frame).
"""

from dataclasses import replace

import pytest

try:  # importing the frontend package blocks numpy unless it is already loaded
    import numpy  # noqa: F401
except ImportError:
    pass

from cyberpunk_computer.api.commands import build_command
from cyberpunk_computer.comm.avc import AvcDecoder, AvcFrame, ManualScheduler
from cyberpunk_computer.comm.avc.climate_remote import RELEASE, press_frame
from cyberpunk_computer.frontend.remote_store import RemoteStore, _translate
from cyberpunk_computer.state.actions import (
    BatchAction, ClimateButtonAction, SetAvcClimateAction, SetAvcStateAction,
    SetOutsideTempAction,
)
from cyberpunk_computer.state.store import Store
from cyberpunk_computer.state.climate_model import (
    ClimateController, ClimateView, format_temp, snap_temp,
)

dec = AvcDecoder()


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class RecStore:
    """Store stand-in: fixed state, records dispatched actions."""

    def __init__(self, state):
        self.state = state
        self.actions = []

    def dispatch(self, action):
        self.actions.append(action)


def live_store(**climate):
    st = Store()
    fields = dict(fan_speed=3, ac_on=True, auto_mode=True, recirculation=False,
                  fresh_air=True, defrost=False, rear_defogger=False,
                  air_direction="face", system_on=True, target_temp=21.5)
    fields.update(climate)
    st.dispatch(SetAvcClimateAction(**fields))
    return st


# ── reading ─────────────────────────────────────────────────────────────────

def test_format_and_snap():
    assert format_temp(21.5) == "21.5"
    assert format_temp(22.0) == "22"
    assert format_temp(None) == "--"
    assert snap_temp(21.74) == 21.5 and snap_temp(21.76) == 22.0


def test_view_before_any_status_is_unknown():
    v = ClimateView.from_state(Store().state)
    assert not v.live and v.stale
    assert v.mode == "unknown" and v.mode_text == "--"
    assert v.fan_text == "--" and v.intake is None
    assert v.inside_temp is None and v.outside_temp is None
    assert not v.target_settable


def test_view_reads_every_field():
    st = live_store(fan_speed=5, ac_on=False, auto_mode=False, recirculation=True,
                    fresh_air=False, rear_defogger=True, air_direction="windscreen_foot",
                    target_temp=19.5)
    st.dispatch(SetOutsideTempAction(7.0))
    v = ClimateView.from_state(st.state)
    assert v.live and v.status_source == "avc"
    assert v.system_on is True and v.mode == "manual"
    assert v.target_temp == 19.5 and v.target_text == "19.5"
    assert v.fan == 5 and not v.fan_auto and v.fan_text == "5"
    assert not v.ac and v.recirculation and v.intake == "recirc"
    assert v.air_direction == "windscreen_foot" and v.air_direction_text == "FEET+WS"
    assert v.rear_defogger and not v.front_defrost
    assert v.outside_temp == 7.0 and v.inside_temp is None


def test_view_auto_fan_defrost_and_off():
    v = ClimateView.from_state(live_store(fan_speed=2).state)
    assert v.mode == "auto" and v.fan_auto and v.fan_text == "AUTO 2"
    # front defrost (F3 b1 0x10) overrides the vent as air direction
    v = ClimateView.from_state(live_store(auto_mode=False, defrost=True, air_direction=None).state)
    assert v.front_defrost and v.air_direction == "defrost" and v.air_direction_text == "DEFROST"
    st = live_store()
    st.dispatch(SetAvcClimateAction(system_on=False, fan_speed=0))   # F5 = 39 / OFF button
    v = ClimateView.from_state(st.state)
    assert v.mode == "off" and v.mode_text == "OFF"
    assert v.target_text == "OFF" and v.fan_text == "OFF"
    assert not v.target_settable


def test_staleness_follows_avc_link():
    clock = Clock()
    st = live_store()
    ctl = ClimateController(st, clock=clock)
    assert ctl.view().stale                      # live status but no AVC frame time yet
    st.dispatch(SetAvcStateAction(last_frame_time=5.0))
    assert not ctl.view().stale
    clock.t += 11
    assert not ctl.view().stale
    clock.t += 2                                 # 13 s without a new frame
    assert ctl.view().stale
    st.dispatch(SetAvcStateAction(last_frame_time=18.0))
    assert not ctl.view().stale


# ── commands + optimistic reconciliation ────────────────────────────────────

def test_target_temp_half_steps_optimistic_until_status():
    clock = Clock()
    st = RecStore(live_store(target_temp=21.5).state)
    ctl = ClimateController(st, clock=clock)
    assert ctl.step_target_temp(+1)
    assert [a.temp for a in st.actions] == [22.0]
    assert ctl.view().target_temp == 22.0 and "target_temp" in ctl.view().pending
    ctl.step_target_temp(+1)
    assert st.actions[-1].temp == 22.5           # steps from the shown value
    # the ECU confirms 22.5 -> pending cleared
    st.state = replace(st.state, climate=replace(st.state.climate, target_temp=22.5))
    v = ctl.view()
    assert v.target_temp == 22.5 and not v.pending
    # clamped to the command range, no dispatch at the limit
    st.state = replace(st.state, climate=replace(st.state.climate, target_temp=18.0))
    n = len(st.actions)
    assert ctl.step_target_temp(-1) and len(st.actions) == n


def test_optimistic_value_expires_and_car_wins():
    clock = Clock()
    st = RecStore(live_store(ac_on=True).state)
    ctl = ClimateController(st, clock=clock)
    ctl.set_ac(False)
    assert ctl.view().ac is False
    clock.t += 3.1                                # no confirming status
    assert ctl.view().ac is True and not ctl.view().pending


def test_target_temp_refused_when_off_or_unknown():
    st = RecStore(Store().state)
    assert not ClimateController(st).set_target_temp(22.0)
    off = live_store()
    off.dispatch(SetAvcClimateAction(system_on=False))
    st = RecStore(off.state)
    assert not ClimateController(st).step_target_temp(1)
    assert st.actions == []


def test_mode_semantics():
    st = RecStore(live_store(fan_speed=4).state)          # AUTO, fan 4
    ctl = ClimateController(st)
    assert ctl.set_mode("auto") and st.actions == []      # already AUTO
    ctl.set_mode("manual")                                # leave AUTO = press current fan
    a = st.actions[-1]
    assert type(a).__name__ == "SetFanSpeedAction" and a.speed == 4
    assert ctl.view().mode == "manual"
    ctl.set_mode("off")
    a = st.actions[-1]
    assert isinstance(a, ClimateButtonAction) and a.button == "off"
    v = ctl.view()
    assert v.mode == "off" and v.system_on is False
    ctl.set_mode("auto")
    assert type(st.actions[-1]).__name__ == "SetAutoModeAction" and st.actions[-1].auto_mode


def test_blind_toggles_guarded():
    st = RecStore(live_store().state)
    ctl = ClimateController(st)
    assert ctl.set_rear_defogger(False) and st.actions == []    # already off
    assert ctl.toggle_rear_defogger()
    assert ctl.toggle_rear_defogger() is False                  # first press unconfirmed
    assert [a.button for a in st.actions] == ["rear_defogger"]
    assert ctl.set_air_direction("defrost")
    assert st.actions[-1].button == "front_defrost"
    assert ctl.view().air_direction == "defrost"
    # no status yet -> never toggle blind
    st2 = RecStore(Store().state)
    assert ClimateController(st2).toggle_front_defrost() is False and st2.actions == []


def test_air_direction_cycle_covers_every_mode():
    st = RecStore(live_store(air_direction="face").state)
    ctl = ClimateController(st)
    seen = []
    for _ in range(5):
        ctl.step_air_direction(+1)
        seen.append(ctl.view().air_direction)
    assert seen == ["face_foot", "foot", "windscreen_foot", "defrost", "face"]


# ── end to end: UI action -> backend command -> AVC frame ──────────────────

def hexf(frame):
    return " ".join(f"{b:02X}" for b in frame)


@pytest.fixture
def twin():
    from cyberpunk_computer.io.factory import create_test_twin
    tw = create_test_twin()
    tw.climate_remote._sched = ManualScheduler()
    yield tw
    tw.avc_scheduler.stop()


def feed_status(remote, b1, b2=0x00, fan=3, setpoint_nn=0x0C):
    """ECU status + setpoint as the remote sees them (default: AUTO, A/C,
    fresh, face, fan 3, 21.5 C)."""
    remote.on_update(dec.decode(AvcFrame.make(
        0x1C6, 0x110, f"00 E0 5D F3 {b1:02X} {b2:02X} 00 00 00 {fan << 5:02X}")))
    remote.on_update(dec.decode(AvcFrame.make(0x1C6, 0x110, f"00 E0 5D F5 {setpoint_nn:02X} 38")))


def presses(tw):
    out = []
    for c in tw.output_port._sent_commands:
        if c.device_id != 2:
            continue
        d = tuple(int(x, 16) for x in c.to_gateway_json()["d"]["d"])
        if d != RELEASE:
            out.append(hexf(d))
    tw.output_port._sent_commands.clear()
    return out


def run(tw, action, via_frontend):
    """Send ``action`` the way the UI does: through RemoteStore translation +
    the backend command registry (Pi frontend), or straight into the backend
    store (local twin)."""
    if via_frontend:
        tr = _translate(action)
        assert tr is not None, f"RemoteStore drops {type(action).__name__}"
        name, params = tr
        built = build_command(name, params)
        tw.store.dispatch(built)
    else:
        tw.store.dispatch(action)
    tw.climate_remote._sched.advance(20.0)
    return presses(tw)


CASES = [
    # (description, controller call, ECU b1, b2, expected presses)
    ("temp +0.5", lambda c: c.step_target_temp(+1), 0xC3, 0x00, ["temp_up"]),
    ("temp -0.5", lambda c: c.step_target_temp(-1), 0xC3, 0x00, ["temp_down"]),
    ("fan 6", lambda c: c.set_fan(6), 0xC3, 0x00, ["fan_6"]),
    ("fan 1", lambda c: c.set_fan(1), 0xC3, 0x00, ["fan_1"]),
    ("fan auto", lambda c: c.set_fan_auto(), 0x43, 0x00, ["auto"]),
    ("mode auto", lambda c: c.set_mode("auto"), 0x43, 0x00, ["auto"]),
    ("mode manual", lambda c: c.set_mode("manual"), 0xC3, 0x00, ["fan_3"]),
    ("mode off", lambda c: c.set_mode("off"), 0xC3, 0x00, ["off"]),
    ("A/C off", lambda c: c.set_ac(False), 0xC3, 0x00, ["ac"]),
    ("A/C on", lambda c: c.set_ac(True), 0xC1, 0x00, ["ac"]),
    ("recirc on", lambda c: c.set_recirculation(True), 0xC3, 0x00, ["recirc"]),
    ("fresh", lambda c: c.set_recirculation(False), 0xA3, 0x00, ["recirc"]),
    ("vent face+feet", lambda c: c.set_air_direction("face_foot"), 0x43, 0x00, ["vent_face_foot"]),
    ("vent feet", lambda c: c.set_air_direction("foot"), 0x43, 0x00, ["vent_foot"]),
    ("vent feet+ws", lambda c: c.set_air_direction("windscreen_foot"), 0x43, 0x00,
     ["vent_windscreen_foot"]),
    ("vent face", lambda c: c.set_air_direction("face"), 0x42, 0x80, ["vent_face"]),
    ("air defrost", lambda c: c.set_air_direction("defrost"), 0x43, 0x00, ["front_defrost"]),
    ("front defrost on", lambda c: c.set_front_defrost(True), 0x43, 0x00, ["front_defrost"]),
    ("front defrost off", lambda c: c.set_front_defrost(False), 0x52, 0x00, ["front_defrost"]),
    ("rear defogger on", lambda c: c.set_rear_defogger(True), 0x43, 0x00, ["rear_defogger"]),
    ("rear defogger off", lambda c: c.set_rear_defogger(False), 0x4B, 0x00, ["rear_defogger"]),
]


def _state_from_status(b1, b2, fan=3, target=21.5):
    st = Store()
    vent = {0x80: "face_foot", 0x40: "foot", 0x20: "windscreen_foot"}.get(b2)
    if b2 == 0 and b1 & 0x01:
        vent = "face"
    st.dispatch(SetAvcClimateAction(
        fan_speed=fan, ac_on=bool(b1 & 0x02), auto_mode=bool(b1 & 0x80),
        recirculation=bool(b1 & 0x20), fresh_air=bool(b1 & 0x40),
        defrost=bool(b1 & 0x10), rear_defogger=bool(b1 & 0x08),
        air_direction=vent, system_on=True, target_temp=target))
    return st.state


@pytest.mark.parametrize("via_frontend", [True, False], ids=["frontend", "local"])
@pytest.mark.parametrize("desc,call,b1,b2,expected", CASES, ids=[c[0] for c in CASES])
def test_ui_command_produces_intended_avc_frame(twin, via_frontend, desc, call, b1, b2, expected):
    feed_status(twin.climate_remote, b1, b2)
    rec = RecStore(_state_from_status(b1, b2))
    ctl = ClimateController(rec)
    assert call(ctl)
    assert len(rec.actions) == 1, desc
    sent = []
    for a in rec.actions:
        sent += run(twin, a, via_frontend)
    # temperature loop: the fake ECU never moves the setpoint, so exactly the
    # first press is sent and the loop gives up (test_climate_remote covers
    # the confirmed multi-step case)
    assert sent == [hexf(press_frame(b)) for b in expected], desc


def test_remote_store_forwards_climate_buttons():
    sent = []

    class Client:
        def send_command(self, name, params):
            sent.append((name, params))
            return True

    rs = RemoteStore(Client())
    rs.dispatch(BatchAction([ClimateButtonAction("off"), ClimateButtonAction("rear_defogger")]))
    assert sent == [("climate_button", {"button": "off", "count": 1}),
                    ("climate_button", {"button": "rear_defogger", "count": 1})]
    for name, params in sent:
        assert build_command(name, params).button == params["button"]


def test_minimum_setpoint_and_leaving_auto_emit_frames(twin):
    """Point 3 of the climate brief: 18.0 C from 21.5 C walks down with
    temp_down presses; leaving AUTO goes out as a fan press (the remote has
    no AUTO-off button, so SetAutoModeAction(False) alone would send nothing)."""
    remote = twin.climate_remote
    feed_status(remote, 0xC3, setpoint_nn=0x0C)            # AUTO, 21.5 C

    nn = [0x0C]

    def ecu(dev, payload, _orig=remote._send):
        data = [int(x, 16) for x in payload["d"]]
        if data[4:] == [0x40, 0, 0, 0, 0, 0]:
            nn[0] -= 1
            remote._sched.call_later(0.05, lambda: remote.on_update(dec.decode(
                AvcFrame.make(0x1C6, 0x110, f"00 E0 5D F5 {nn[0]:02X} 38"))))
        return _orig(dev, payload)

    remote._send = ecu
    ctl = ClimateController(RecStore(_state_from_status(0xC3, 0x00)))
    ctl.set_target_temp(18.0)
    (a,) = ctl._store.actions
    sent = run(twin, a, via_frontend=True)
    assert sent == [hexf(press_frame("temp_down"))] * 7 and nn[0] == 0x05   # 15.5+2.5 = 18.0

    ctl.set_mode("manual")
    sent = run(twin, ctl._store.actions[-1], via_frontend=True)
    assert sent == [hexf(press_frame("fan_3"))]
    # the raw API "auto=false" is a documented no-op in the remote
    assert run(twin, build_command("set_climate", {"auto": False}), via_frontend=False) == []
