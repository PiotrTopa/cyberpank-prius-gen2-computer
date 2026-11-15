"""
Screenshots of the MFD alert banner on the demo drive (ui/scene/demo.py).

    python -m cyberpunk_computer.ui.scene.alerts_demo --shots OUT_DIR [--copy-to DIR] [--prefix alerts_]

Each shot runs the demo timeline to a moment, pushes an ``alert`` event the
way the backend sends it (docs/ALERTS.md) and renders the whole main screen.
The texts are the ones the alert functions produce.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from .alerts import EVENT, AlertFeed
from .demo import SIZE, Sim

# (name, t, alert payload, demo mods, page)
SHOTS: List[tuple] = [
    ("speed_limit", 24.0, {"id": "speed_limit", "text": "LIMIT 50  REAL 62", "severity": "notice",
                           "icon": "limit", "chip": {"real_kmh": 62}},
     {"limit": {"kmh": 50, "src": "sign", "conf": 0.8}}, 0),
    ("distance_warning", 21.0, {"id": "distance_warning", "text": "CLOSING 1.6s",
                                "severity": "warning", "icon": "lead", "advisory": True}, {}, 0),
    ("headway_coach", 26.0, {"id": "headway_coach", "text": "GAP 0.8s", "severity": "notice", "advisory": True,
                             "icon": "lead"}, {}, 0),
    ("lead_departure", 39.0, {"id": "lead_departure", "text": "LEAD MOVED OFF", "severity": "notice",
                              "icon": "go"}, {}, 0),
    ("curve", 23.0, {"id": "curve_speed", "text": "CURVE 250m  MAX 70", "severity": "notice",
                     "icon": "curve"}, {}, 0),
    ("eco_lift", 20.0, {"id": "eco_lift", "text": "LIFT OFF  RBT 320m", "severity": "info",
                        "icon": "lift"}, {}, 0),
    ("ice_risk", 12.0, {"id": "ice_risk", "text": "ICE RISK  OUT 1C", "severity": "notice",
                        "icon": "snow"}, {"env": {"place": "rural_road", "light": "night", "weather": "snow"}}, 0),
    ("service_due", 4.0, {"id": "service_due", "text": "OIL SERVICE IN 350km", "severity": "info",
                          "icon": "service"}, {}, 0),
    ("new_dtc", 4.0, {"id": "new_dtc", "text": "NEW FAULT P0A80", "severity": "notice",
                      "icon": "fault"}, {}, 0),
    ("range_low", 22.0, {"id": "range_low", "text": "RANGE ~45km (PETROL)", "severity": "notice",
                         "icon": "fuel"}, {}, 1),
]


def render_shots(out_dir: str, scale: int = 4, copy_to: Optional[str] = None,
                 prefix: str = "alerts_") -> List[str]:
    import pygame
    os.makedirs(out_dir, exist_ok=True)
    pygame.init()
    pygame.display.set_mode(SIZE)
    surf = pygame.Surface(SIZE)
    paths = []
    for i, (name, t, alert, mods, page) in enumerate(SHOTS):
        sim = Sim("live", mods=mods)
        feed = AlertFeed(clock=lambda s=sim: s.t)
        sim.screen.scene_page.alerts = feed
        sim.run_to(t - 0.5)
        payload: Dict[str, Any] = dict({"seq": 1, "ttl": 5.0, "ack": True}, **alert)
        feed.push(EVENT, {"alert": payload})
        if page:
            sim.screen.show_page(page)
        sim.run_to(t)
        scene = sim.screen.scene_page.scene
        if scene is not None:
            scene.invalidate()
        sim.frame(surf)
        base = "%02d_%s" % (i + 1, name)
        p = os.path.join(out_dir, base + ".png")
        pygame.image.save(surf, p)
        big = pygame.transform.scale(surf, (SIZE[0] * scale, SIZE[1] * scale))
        pygame.image.save(big, os.path.join(out_dir, base + "@%dx.png" % scale))
        if copy_to:
            os.makedirs(copy_to, exist_ok=True)
            pygame.image.save(big, os.path.join(copy_to, "%s%s.png" % (prefix, base)))
        paths.append(p)
    return paths


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shots", required=True, help="output directory")
    ap.add_argument("--copy-to", default=None)
    ap.add_argument("--prefix", default="alerts_")
    ap.add_argument("--scale", type=int, default=4)
    a = ap.parse_args(argv)
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    for p in render_shots(a.shots, a.scale, a.copy_to, a.prefix):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
