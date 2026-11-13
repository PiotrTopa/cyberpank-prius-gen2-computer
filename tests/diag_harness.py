"""Shared harness for the diagnostics tests: engine + simulated gateway/ECUs
on a fake clock."""

from cyberpunk_computer.diag.engine import DiagConfig, DiagEngine
from cyberpunk_computer.diag.sim import SimGateway


class Rig:
    def __init__(self, ecus=None, speed=0.0, bus_tap=False, config=None, publish=None,
                 on_scan_done=None):
        self.t = 0.0
        self.speed = speed
        self.max_outstanding = 0
        self.outstanding = 0
        self.tx_times = []
        self.gw = SimGateway(self._deliver, bus_tap=self._tap if bus_tap else None)
        self.eng = DiagEngine(self._send, publish=publish, vehicle_speed=lambda: self.speed,
                              ecus=ecus, config=config or DiagConfig(), clock=lambda: self.t,
                              wall=lambda: 1_700_000_000.0 + self.t, on_scan_done=on_scan_done)

    def _send(self, payload):
        ok = self.gw.send(payload)
        if ok:
            self.outstanding += 1
            self.max_outstanding = max(self.max_outstanding, self.outstanding)
            self.tx_times.append(self.t)
        return ok

    def _deliver(self, data):
        self.outstanding -= 1
        self.eng.on_gateway_message(data)

    def _tap(self, can_id, frame):
        self.eng.on_bus_frame(can_id, frame, self.t)

    def run(self, max_s=120.0, step=0.01, extra=None):
        """Tick until the engine is idle."""
        n = int(max_s / step)
        for _ in range(n):
            self.eng.tick()
            self.gw.step()
            if extra is not None:
                extra()
            self.t += step
            if not self.eng.busy:
                self.eng.tick()
                return
        raise AssertionError("engine still busy after %.0f s" % max_s)

    def requests(self, tx_id=None):
        """Request payload bytes (first byte(s) after the PCI) of every frame sent."""
        out = []
        for p in self.gw.sent:
            if tx_id is None or int(p["i"], 16) == tx_id:
                out.append(bytes(p["d"]))
        return out
