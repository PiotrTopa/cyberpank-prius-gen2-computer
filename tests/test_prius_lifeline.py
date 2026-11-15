"""prius-lifeline + prius-wifi (POCO/rootfs/usr/local/sbin), run for real
against fake nmcli/ping/ip/systemctl: uplink down => car AP up.

The fakes keep NetworkManager state in files (radio, active Wi-Fi connection)
so the real prius-wifi verbs (lifeline, suspend, apply) act on it.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SBIN = Path(os.environ.get("PRIUS_TEST_SBIN") or
            Path(__file__).resolve().parents[1] / "POCO" / "rootfs" / "usr" / "local" / "sbin")

FAKES = {
    "nmcli": r"""#!/bin/sh
F=$FAKE_DIR
echo "nmcli $*" >> $F/calls
case "$*" in
  "-t -f NAME,TYPE con show --active")
    echo "prius-wan:gsm"; a=$(cat $F/active 2>/dev/null); [ -n "$a" ] && echo "$a:802-11-wireless"; exit 0 ;;
  "radio wifi") cat $F/radio; exit 0 ;;
  "radio wifi on") echo enabled > $F/radio; exit 0 ;;
  "radio wifi off") echo disabled > $F/radio; : > $F/active; exit 0 ;;
  "con up "*) [ "$(cat $F/radio)" = enabled ] || exit 4; echo "$3" > $F/active; exit 0 ;;
  "con down "*) [ "$(cat $F/active 2>/dev/null)" = "$3" ] && : > $F/active; exit 0 ;;
esac
exit 0
""",
    "ping": "#!/bin/sh\nexit $(cat $FAKE_DIR/ping_rc 2>/dev/null || echo 0)\n",
    "ip": """#!/bin/sh
case "$*" in
  "route show default dev wlan0") [ -e $FAKE_DIR/client_route ] && echo "default via 192.168.0.1 dev wlan0" ;;
esac
exit 0
""",
    "systemctl": """#!/bin/sh
echo "systemctl $*" >> $FAKE_DIR/calls
[ "$1" = is-active ] && { [ -e "$FAKE_DIR/unit_$3" ]; exit $?; }
exit 0
""",
    "sleep": "#!/bin/sh\nexit 0\n",
}


class Phone:
    def __init__(self, tmp):
        self.d = tmp / "fake"
        self.etc = tmp / "etc"
        self.run_dir = tmp / "run"
        bindir = tmp / "bin"
        for p in (self.d, self.etc, bindir):
            p.mkdir()
        for name, body in FAKES.items():
            f = bindir / name
            f.write_text(body)
            f.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH="%s:%s" % (bindir, os.environ["PATH"]),
            FAKE_DIR=str(self.d),
            PRIUS_ETC=str(self.etc),
            LIFELINE_RUN=str(self.run_dir),
            PRIUS_WIFI=str(SBIN / "prius-wifi"),
            PRIUS_WIFI_FLAG=str(self.etc / "wifi-mode"),
            PRIUS_WIFI_LOCK=str(tmp / "wifi.lock"),
        )
        self.t = 1_000_000
        self.set(radio="disabled", active="", ping_rc="0")
        self.flag(**{"power-mode": "low", "wifi-mode": "ap"})

    def set(self, **kw):
        for k, v in kw.items():
            (self.d / k).write_text(v + "\n" if v else "")

    def flag(self, **kw):
        for k, v in kw.items():
            (self.etc / k).write_text(v + "\n")

    def get(self, k):
        return (self.d / k).read_text().strip()

    def sh(self, prog, *args):
        env = dict(self.env, LIFELINE_NOW=str(self.t))
        return subprocess.run([str(SBIN / prog), *args], env=env, capture_output=True,
                              text=True, timeout=30)

    def tick(self, dt=30):
        self.t += dt
        r = self.sh("prius-lifeline", "tick")
        assert r.returncode == 0, r.stderr
        return r.stdout

    @property
    def state(self):
        return self.sh("prius-lifeline", "state").stdout.strip()

    @property
    def ap_up(self):
        return self.get("radio") == "enabled" and self.get("active") == "prius-ap"


@pytest.fixture
def phone(tmp_path):
    if not shutil.which("flock") or not shutil.which("timeout"):
        pytest.skip("needs util-linux flock + coreutils timeout")
    return Phone(tmp_path)


def go_down(phone, n=3):
    phone.set(ping_rc="1")
    out = "".join(phone.tick() for _ in range(n))
    return out


def test_uplink_fine_never_touches_wifi(phone):
    for _ in range(5):
        phone.tick()
    assert phone.state == "off"
    calls = phone.d / "calls"
    assert not calls.exists() or "radio wifi on" not in calls.read_text()
    assert phone.get("radio") == "disabled"


def test_suspended_wifi_comes_up_after_three_misses(phone):
    phone.set(ping_rc="1")
    phone.tick(); phone.tick()
    assert phone.state == "off" and not phone.ap_up
    out = phone.tick()
    assert "lifeline ON" in out
    assert phone.state == "on" and phone.ap_up
    assert (phone.etc / "wifi-mode").read_text().strip() == "ap"   # flag untouched


def test_fail_threshold_is_tunable(phone):
    phone.flag(**{"lifeline-fails": "1"})
    phone.set(ping_rc="1")
    phone.tick()
    assert phone.state == "on" and phone.ap_up


def test_suspend_is_blocked_while_lifeline_on(phone):
    go_down(phone)
    r = phone.sh("prius-wifi", "suspend")
    assert "suspend skipped" in r.stdout
    assert phone.ap_up


def test_ap_reasserted_if_something_drops_it(phone):
    go_down(phone)
    phone.set(radio="disabled", active="")
    phone.tick()
    assert phone.ap_up


def test_release_after_hold_low_profile_suspends(phone):
    go_down(phone)
    phone.set(ping_rc="0")
    out = phone.tick()
    assert "holding the AP" in out and phone.state == "on"
    phone.tick(300)
    assert phone.state == "on" and phone.ap_up
    out = phone.tick(300)
    assert "lifeline OFF" in out
    assert phone.state == "off" and phone.get("radio") == "disabled"


def test_hold_is_tunable_and_restarts_on_a_miss(phone):
    phone.flag(**{"lifeline-hold": "2min"})
    go_down(phone)
    phone.set(ping_rc="0"); phone.tick()
    phone.tick(90)
    phone.set(ping_rc="1"); assert "lost again" in phone.tick()
    phone.set(ping_rc="0"); phone.tick(); phone.tick(90)
    assert phone.state == "on"
    phone.tick(40)
    assert phone.state == "off"


def test_release_leaves_a_pending_key_off_grace_alone(phone):
    go_down(phone)
    phone.set(ping_rc="0", **{"unit_prius-wifi-suspend.timer": "1"})
    phone.tick(); phone.tick(600)
    assert phone.state == "off" and phone.ap_up   # the grace timer will suspend later


def test_release_full_profile_restores_flag_mode(phone):
    phone.flag(**{"power-mode": "full", "wifi-mode": "client"})
    go_down(phone)
    assert phone.ap_up                          # client not associated -> AP
    assert (phone.etc / "wifi-mode").read_text().strip() == "client"
    phone.set(ping_rc="0"); phone.tick(); phone.tick(600)
    assert phone.state == "off" and phone.get("active") == "prius-home-wifi"


def test_associated_client_with_route_is_left_alone(phone):
    phone.flag(**{"power-mode": "full", "wifi-mode": "client"})
    phone.set(radio="enabled", active="prius-home-wifi", client_route="1")
    out = go_down(phone)
    assert phone.state == "on" and phone.get("active") == "prius-home-wifi"
    assert "client mode left alone" in out
    (phone.d / "client_route").unlink()          # client loses its network
    phone.tick()
    assert phone.ap_up


def test_flag_off_keeps_ap_while_lifeline_on(phone):
    phone.flag(**{"wifi-mode": "off"})
    go_down(phone)
    assert phone.ap_up
    phone.sh("prius-wifi", "apply")
    assert phone.ap_up
    phone.set(ping_rc="0"); phone.tick(); phone.tick(600)
    assert phone.state == "off" and phone.get("radio") == "disabled"


def test_reboot_without_uplink_comes_back_on(phone):
    go_down(phone)
    shutil.rmtree(phone.run_dir)                 # /run is tmpfs
    phone.set(radio="disabled", active="")
    phone.tick(); phone.tick()
    assert phone.state == "off"
    phone.tick()
    assert phone.state == "on" and phone.ap_up


def test_simulate_down_hook_expires(phone):
    r = phone.sh("prius-lifeline", "simulate-down", "100")
    assert r.returncode == 0
    go_down_ticks = [phone.tick() for _ in range(3)]
    assert phone.state == "on" and phone.ap_up and "lifeline ON" in go_down_ticks[-1]
    phone.tick(20)                               # t = +110: simulation over, ping OK
    assert not (phone.run_dir / "simulate-down").exists()
    assert phone.sh("prius-lifeline", "simulate-down", "5000").returncode == 2


def test_lifeline_never_bounces_links_or_reboots(phone):
    go_down(phone, 12)
    calls = (phone.d / "calls").read_text()
    for bad in ("prius-wan", "wg-homelab", "reboot"):
        assert bad not in calls.replace("prius-wan:gsm", "")
