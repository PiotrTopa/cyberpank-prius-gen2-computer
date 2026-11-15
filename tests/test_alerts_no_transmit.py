"""Safety: the alert framework never transmits except the tunnel automation's
AVC-LAN recirculation press in mode ``on``.

* ``tunnel_recirc`` in ``shadow`` and ``off``: through a whole tunnel (entry,
  hold, exit, restore) the real AlertService dispatches no action into the
  store at all -- so the egress (AVC-LAN climate remote) and the BEAN /
  powerbox / CAN transmit paths, which are all driven by store actions, get
  nothing. ``on`` is the positive control: exactly the recirculation set.
* Statically: the only ``dispatch(`` in the alerts package is the
  SetRecirculationAction in ``AlertService._actuate``, and no alerts module
  imports a transmit path (egress, BEAN / CAN / gateway tx, serial, satellite
  transports, powerbox commander).
"""

import ast
from pathlib import Path

import pytest

from cyberpunk_computer.alerts.functions.tunnel import TunnelRecircFunction
from cyberpunk_computer.alerts.inputs import AlertInputs
from cyberpunk_computer.alerts.service import AlertService
from cyberpunk_computer.alerts.tuning import AlertTuning
from cyberpunk_computer.state.store import Store

ALERTS = Path(__file__).resolve().parent.parent / "cyberpunk_computer" / "alerts"

FRESH = {"system_on": True, "recirculation": False, "fresh_air": True, "ac_on": True, "auto_mode": True,
         "defrost": False, "rear_defogger": False, "fan_speed": 3, "air_direction": "face",
         "target_temp": 21.0, "status_source": "avc"}
RECIRC = dict(FRESH, recirculation=True, fresh_air=False)
IN_MAP = {"in": True, "src": "map", "map_usable": True}
OUT = {"in": False, "src": "none", "map_usable": True}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _tunnel_drive(tmp_path, mode):
    clk = Clock()
    store = Store()
    seen = []
    store.add_middleware(lambda a, s: seen.append(a))
    svc = AlertService(store, str(tmp_path), clock=clk, wall=clk,
                       functions=[TunnelRecircFunction(AlertTuning())])
    svc.attach()
    if mode != "shadow":                       # shadow is the shipped default
        assert svc.manager.set_mode("tunnel_recirc", mode) == (True, None)
    assert svc.manager.mode("tunnel_recirc") == mode
    base = dict(map_ok=True, map_conf=0.95, climate_live=True, real_kmh=60.0, gear="DRIVE", ready=True,
                scene_age=0.1)

    def run(secs, **kw):
        end = clk.t + secs
        while clk.t < end:
            svc.manager.tick(AlertInputs(now=clk.t, wall=clk.t, **dict(base, **kw)))
            clk.t += 0.1

    seen.clear()
    run(5.0, tunnel=OUT, climate=FRESH)
    run(2.0, tunnel=IN_MAP, climate=FRESH)
    # in "on" the car confirms the press; shadow / off never pressed, the status stays FRESH
    run(40.0, tunnel=IN_MAP, climate=RECIRC if mode == "on" else FRESH)
    run(40.0, tunnel=OUT, climate=RECIRC if mode == "on" else FRESH)
    actions = [e for e in svc.log.recent_entries(100) if e.get("event") == "action"]
    svc.stop()
    return seen, actions, svc


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_tunnel_recirc_shadow_and_off_send_no_climate_command(tmp_path, mode):
    seen, actions, svc = _tunnel_drive(tmp_path, mode)
    assert seen == []                                   # nothing reaches the store: no AVC key, no BEAN, no CAN
    assert svc.actions_executed == 0
    if mode == "shadow":                                # it logs what it would have pressed, unexecuted
        assert sorted((a["action"], a["executed"]) for a in actions) == [("recirc_off", False),
                                                                        ("recirc_on", False)]
    else:
        assert actions == []


def test_tunnel_recirc_on_dispatches_only_the_recirculation_set(tmp_path):
    seen, actions, svc = _tunnel_drive(tmp_path, "on")
    assert [(type(a).__name__, a.recirculation) for a in seen] == [("SetRecirculationAction", True),
                                                                  ("SetRecirculationAction", False)]
    assert svc.actions_executed == 2


def _modules():
    return sorted(ALERTS.rglob("*.py"))


def test_only_dispatch_in_the_alerts_package_is_the_recirculation_set():
    sites = []
    for p in _modules():
        tree = ast.parse(p.read_text(), str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "dispatch":
                arg = node.args[0] if node.args else None
                name = arg.func.id if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) else None
                sites.append((p.name, name))
    assert sites == [("service.py", "SetRecirculationAction")]


FORBIDDEN = ("egress", "serial_io", "panda_can", "udp_output", "vfd_output", "rs485_transport",
             "satellite_transport", "ble_transport", "wifi_transport", "bean_monitor", "commands")
FORBIDDEN_NAMES = ("PowerboxCommander", "PriusPowerController", "build_power_off_command")


def test_no_alerts_module_imports_a_transmit_path():
    for p in _modules():
        tree = ast.parse(p.read_text(), str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not any(mod.split(".")[-1] == f for f in FORBIDDEN), (p.name, mod)
                for a in node.names:
                    assert a.name not in FORBIDDEN_NAMES, (p.name, a.name)
                    assert "Tx" not in a.name and "Bean" not in a.name or a.name.endswith("Ingress"), \
                        (p.name, a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert not any(a.name.split(".")[-1] == f for f in FORBIDDEN), (p.name, a.name)
