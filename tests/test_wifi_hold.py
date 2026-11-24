"""backend.wifi_hold: keep the car AP up while poco2 needs it."""

from cyberpunk_computer.backend.wifi_hold import WifiHold


class Rig:
    def __init__(self, ap=False):
        self.ap = ap
        self.t = 0.0
        self.cmds = []
        self.h = WifiHold(ap_up=lambda: self.ap, spawn=self.cmds.append,
                          clock=lambda: self.t, resume_retry_s=60.0)

    def verbs(self):
        return [" ".join(c[-2:]) if c[0] == "timeout" else " ".join(c[:2]) for c in self.cmds]


def test_hold_cancels_the_suspend_and_resumes_a_down_ap_rate_limited():
    r = Rig(ap=False)
    r.h.tick("low")
    assert r.cmds == []                       # no owner, nothing done
    r.h.hold("poco2")
    r.h.tick("low")
    assert r.verbs() == ["systemctl stop", "/usr/local/sbin/prius-wifi resume"]
    r.t = 30.0
    r.h.tick("low")
    assert len(r.cmds) == 2                   # retry only after resume_retry_s
    r.t = 61.0
    r.h.tick("low")
    assert r.verbs()[-1] == "/usr/local/sbin/prius-wifi resume"
    r.ap = True
    r.t = 200.0
    r.h.tick("low")
    assert len(r.cmds) == 3                   # AP up: nothing more


def test_release_rearms_the_key_off_grace_only_in_low():
    r = Rig(ap=True)
    r.h.hold("poco2")
    r.h.tick("low")
    r.h.release("poco2")
    r.h.tick("low")
    assert r.verbs()[-1] == "/usr/local/sbin/prius-power apply"
    r.h.tick("low")
    assert r.verbs().count("/usr/local/sbin/prius-power apply") == 1   # once

    r2 = Rig(ap=True)
    r2.h.hold("poco2")
    r2.h.tick("full")
    r2.h.release("poco2")
    r2.h.tick("full")
    assert "/usr/local/sbin/prius-power apply" not in r2.verbs()
