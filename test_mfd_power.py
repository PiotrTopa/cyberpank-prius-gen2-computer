#!/usr/bin/env python3
"""Self-running tests for the MFD video-board power manager state machine.

    python3 test_mfd_power.py

Uses a fake clock, fake command runner and fake iface presence — no hardware.
"""

from __future__ import annotations

import importlib.util
import os
import sys

# Load mfd_power directly by path: cyberpunk_computer.backend's __init__ pulls
# in the API server (uvicorn/fastapi), which isn't available on dev machines.
_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "mfd_power", os.path.join(_here, "cyberpunk_computer", "backend", "mfd_power.py")
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["mfd_power"] = _mod
_spec.loader.exec_module(_mod)

MfdPowerConfig = _mod.MfdPowerConfig
MfdPowerManager = _mod.MfdPowerManager
STATE_BOOTING = _mod.STATE_BOOTING
STATE_GRACE = _mod.STATE_GRACE
STATE_OFF = _mod.STATE_OFF
STATE_ON = _mod.STATE_ON
STATE_SHUTTING_DOWN = _mod.STATE_SHUTTING_DOWN

FAILURES = []


def check(name: str, ok: bool) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        FAILURES.append(name)


class Fake:
    """Fake environment: clock, commands, iface, ping result."""

    def __init__(self):
        self.now = 1000.0
        self.cmds: list[list[str]] = []
        self.iface_present = False
        self.ping_ok = False
        self.published: list[dict] = []

    def clock(self):
        return self.now

    def runner(self, cmd, timeout=10):
        self.cmds.append(list(cmd))

        class R:
            returncode = 0
        r = R()
        if cmd[0] == "ping":
            r.returncode = 0 if self.ping_ok else 1
        return r

    def spawner(self, cmd):
        self.cmds.append(list(cmd))

    def iface_exists(self, iface):
        return self.iface_present

    def publish(self, status):
        self.published.append(status)

    def power_cmds(self):
        return [c for c in self.cmds if c[:2] == ["sudo", "prius-usb-power"]]


def make(fake: Fake, **cfg_kw) -> MfdPowerManager:
    kw = dict(tick_s=1.0, grace_s=600.0, dark_grace=False, boot_timeout_s=120.0, shutdown_wait_s=45.0)
    kw.update(cfg_kw)
    cfg = MfdPowerConfig(**kw)
    return MfdPowerManager(
        cfg, publish=fake.publish, runner=fake.runner, spawner=fake.spawner,
        iface_exists=fake.iface_exists, clock=fake.clock,
    )


def step(mgr: MfdPowerManager, fake: Fake, acc: bool, dt: float = 2.0):
    fake.now += dt
    mgr.tick(acc)


def test_full_cycle():
    print("full ACC cycle")
    fake = Fake()
    mgr = make(fake)

    # Idle with ACC off: stays off (startup reconciliation enforces port off).
    step(mgr, fake, acc=False)
    check("stays-off", mgr.status()["state"] == STATE_OFF)
    check("startup-enforce-off",
          fake.power_cmds() == [["sudo", "prius-usb-power", "off", "1-1 5"]])

    # ACC on: port powered, BOOTING.
    step(mgr, fake, acc=True)
    check("acc-on-powers-port",
          fake.power_cmds()[-1] == ["sudo", "prius-usb-power", "on", "1-1 5"])
    check("booting", mgr.status()["state"] == STATE_BOOTING)

    # Iface appears + ping ok -> ON, host iface configured.
    fake.iface_present = True
    fake.ping_ok = True
    step(mgr, fake, acc=True)
    check("on-when-reachable", mgr.status()["state"] == STATE_ON)
    check("host-ip-configured",
          any(c[:3] == ["sudo", "ip", "addr"] for c in fake.cmds))
    check("reachable-reported", mgr.status()["reachable"] is True)

    # ACC off -> GRACE; ACC back within grace -> ON again, no shutdown.
    step(mgr, fake, acc=False)
    check("grace-on-acc-off", mgr.status()["state"] == STATE_GRACE)
    step(mgr, fake, acc=True)
    check("acc-back-cancels-grace", mgr.status()["state"] == STATE_ON)
    check("no-ssh-yet", not any("ssh" in c for c in fake.cmds))

    # ACC off, grace expires -> SSH shutdown -> wait -> port power off -> OFF.
    step(mgr, fake, acc=False)
    check("grace-again", mgr.status()["state"] == STATE_GRACE)
    fake.now += 601
    step(mgr, fake, acc=False)
    check("shutting-down-after-grace",
          mgr.status()["state"] == STATE_SHUTTING_DOWN)
    check("ssh-shutdown-sent",
          any(c[:2] == ["sudo", "ssh"] and c[-2:] == ["sudo", "poweroff"]
              for c in fake.cmds))
    # Pi halts: gadget disappears -> immediate power cut, back to OFF.
    fake.iface_present = False
    step(mgr, fake, acc=False)
    check("power-cut-after-halt",
          fake.power_cmds()[-1] == ["sudo", "prius-usb-power", "off", "1-1 5"])
    check("back-to-off", mgr.status()["state"] == STATE_OFF)


class Bean:
    def __init__(self):
        self.frames_total = 0
        self.decoded = {}

    def rx(self, **vals):
        self.frames_total += 5
        for k, v in vals.items():
            self.decoded[k] = {"value": v, "ts": 0, "src": "FE:B6"}


def test_bean_early_start():
    print("test_bean_early_start")
    T = _mod.BeanWakeTrigger
    # entry mode: locked wake (our own or a fob lock) does not trigger
    b, t = Bean(), T("entry", 15.0, 30.0)
    check("idle bus: no demand", t.update(b, None, 0.0, 5000.0) is False)
    b.rx(doors_locked=True, any_door_open=False)
    check("bus active but locked: no demand", t.update(b, None, 1.0, 5001.0) is False)
    b.rx(doors_locked=False)
    check("unlocked: demand", t.update(b, None, 2.0, 5002.0) is True)
    check("still inside the active window", t.update(b, None, 16.0, 5016.0) is True)
    check("bus quiet > 15 s: demand gone", t.update(b, None, 18.0, 5018.0) is False)
    b.rx(doors_locked=True, any_door_open=True)
    check("door open: demand", t.update(b, None, 30.0, 5030.0) is True)
    # our own transmit woke the bus: muted
    b, t = Bean(), T("entry", 15.0, 30.0)
    t.update(b, None, 0.0, 5000.0)
    b.rx(doors_locked=False)
    check("own TX 3 s ago: muted", t.update(b, 4998.0, 1.0, 5001.0) is False)
    # any / off modes
    b, t = Bean(), T("any")
    t.update(b, None, 0.0, 5000.0)
    b.rx(doors_locked=True)
    check("any: activity is enough", t.update(b, None, 1.0, 5001.0) is True)
    b, t = Bean(), T("off")
    b.rx(doors_locked=False)
    check("off: never", t.update(b, None, 1.0, 5001.0) is False)

    # manager: early demand starts from OFF at once and then follows the grace
    f = Fake()
    m = make(f, tick_s=5.0, grace_s=1200.0, dark_grace=True)
    step(m, f, False)                      # reconcile, OFF
    check("off before", m.status()["state"] == STATE_OFF)
    f.now += 0.5                           # well inside the 5 s tick limit
    m.tick(False, early=True, why="BEAN: doors unlocked")
    check("early start not delayed by the slow tick", m.status()["state"] == STATE_BOOTING)
    f.iface_present = f.ping_ok = True
    f.now += 6; m.tick(False, early=True)
    check("on", m.status()["state"] == STATE_ON)
    f.now += 6; m.tick(False, early=False)
    check("bus quiet, no ACC: grace", m.status()["state"] == STATE_GRACE)
    ssh = lambda needle: [c for c in f.cmds if c[:2] == ["sudo", "ssh"] and needle in c[-1]]
    check("grace: dark standby sent", len(ssh("SIGSTOP")) == 1 and m.status()["dark"])
    f.now += 6; m.tick(False)
    f.now += 6; m.tick(False)
    f.now += 6; m.tick(False)
    check("dark standby repeated 3 times, no more", len(ssh("SIGSTOP")) == 3)
    check("no restore yet", len(ssh("SIGCONT")) == 0)
    f.now += 6; m.tick(True)
    check("ACC during grace: on", m.status()["state"] == STATE_ON)
    check("restore sent at once", len(ssh("SIGCONT")) == 1 and not m.status()["dark"])
    f.now += 6; m.tick(True)
    f.now += 6; m.tick(True)
    f.now += 6; m.tick(True)
    check("restore repeated 3 times", len(ssh("SIGCONT")) == 3)
    f.now += 6; m.tick(True)
    check("no resend inside 30 s", len(ssh("SIGCONT")) == 3)
    for _ in range(5):
        f.now += 6; m.tick(True)
    check("restore re-sent once after 30 s in ON", len(ssh("SIGCONT")) == 4)
    f.ping_ok = False
    for _ in range(6):
        f.now += 6; m.tick(True)
    check("no resend while unreachable", len(ssh("SIGCONT")) == 4)
    f.ping_ok = True
    f.now += 6; m.tick(True)
    f.now += 6; m.tick(False)
    f.now += 1201; m.tick(False)
    check("grace expired: shutting down", m.status()["state"] == STATE_SHUTTING_DOWN)


def test_boot_timeout_recycles():
    print("boot timeout power-cycles")
    fake = Fake()
    mgr = make(fake)
    step(mgr, fake, acc=True)
    check("booting", mgr.status()["state"] == STATE_BOOTING)
    fake.now += 121  # exceed boot_timeout_s with no iface
    step(mgr, fake, acc=True)
    pc = fake.power_cmds()
    check("recycled",
          pc[-2:] == [["sudo", "prius-usb-power", "off", "1-1 5"],
                      ["sudo", "prius-usb-power", "on", "1-1 5"]])
    check("still-booting", mgr.status()["state"] == STATE_BOOTING)


def test_shutdown_wait_timeout():
    print("shutdown wait timeout (iface never disappears)")
    fake = Fake()
    mgr = make(fake)
    step(mgr, fake, acc=True)
    fake.iface_present = True
    fake.ping_ok = True
    step(mgr, fake, acc=True)
    step(mgr, fake, acc=False)          # -> GRACE
    fake.now += 601
    step(mgr, fake, acc=False)          # -> SHUTTING_DOWN
    fake.now += 46                       # exceed shutdown_wait_s
    step(mgr, fake, acc=False)
    check("power-cut-after-wait",
          fake.power_cmds()[-1] == ["sudo", "prius-usb-power", "off", "1-1 5"])
    check("off", mgr.status()["state"] == STATE_OFF)


def test_reconfigures_ip_while_on():
    print("host IP re-applied while ON (gadget re-enumeration)")
    fake = Fake()
    mgr = make(fake)
    step(mgr, fake, acc=True)
    fake.iface_present = True
    fake.ping_ok = True
    step(mgr, fake, acc=True)
    n_ip = sum(1 for c in fake.cmds if c[:3] == ["sudo", "ip", "addr"])
    step(mgr, fake, acc=True)
    n_ip2 = sum(1 for c in fake.cmds if c[:3] == ["sudo", "ip", "addr"])
    check("ip-reapplied-every-tick", n_ip2 > n_ip)
    # Ping loss is reflected in status.
    fake.ping_ok = False
    step(mgr, fake, acc=True)
    check("unreachable-reported", mgr.status()["reachable"] is False)
    check("stays-on-despite-ping-loss", mgr.status()["state"] == STATE_ON)


def test_publish_on_change_only():
    print("publish only on observable change")
    fake = Fake()
    mgr = make(fake)
    step(mgr, fake, acc=False)
    step(mgr, fake, acc=False)
    step(mgr, fake, acc=False)
    check("single-initial-publish", len(fake.published) == 1)
    step(mgr, fake, acc=True)
    check("publish-on-transition", len(fake.published) == 2)


def test_startup_reconciliation():
    print("startup reconciliation")
    # Board already running, ACC off -> adopt into GRACE (clean shutdown later).
    fake = Fake()
    mgr = make(fake)
    fake.iface_present = True
    fake.ping_ok = True
    step(mgr, fake, acc=False)
    check("adopt-running-board-to-grace", mgr.status()["state"] == STATE_GRACE)
    check("no-power-cmd-on-adopt", not fake.power_cmds())
    # Board absent, ACC off -> enforce port off once.
    fake2 = Fake()
    mgr2 = make(fake2)
    step(mgr2, fake2, acc=False)
    check("enforce-off-at-startup",
          fake2.power_cmds() == [["sudo", "prius-usb-power", "off", "1-1 5"]])
    # Board already running, ACC on -> adopt into ON.
    fake3 = Fake()
    mgr3 = make(fake3)
    fake3.iface_present = True
    fake3.ping_ok = True
    step(mgr3, fake3, acc=True)
    check("adopt-running-board-to-on", mgr3.status()["state"] == STATE_ON)


def main() -> None:
    test_full_cycle()
    test_bean_early_start()
    test_boot_timeout_recycles()
    test_shutdown_wait_timeout()
    test_reconfigures_ip_while_on()
    test_publish_on_change_only()
    test_startup_reconciliation()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASSED")


if __name__ == "__main__":
    main()
