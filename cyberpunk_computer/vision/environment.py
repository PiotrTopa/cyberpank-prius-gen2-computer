"""Environment model: day / dusk-dawn / night / enclosed + a weather hint, no ML runtime.

Input: one :class:`~.scene_features.SceneFeatures` per frame (the dashcam at
1 fps). Output: :class:`Environment`::

    time_of_day  "day" | "dusk_dawn" | "night"
    enclosed     True in a garage / tunnel / covered car park
    weather      "clear" (blue sky) | "overcast" (bright grey sky) | None (no call);
                 rain / snow / fog are not separable from frame statistics
    confidence   0..1 (probability of the published label, from the tree leaves)
    label        "enclosed" if enclosed else time_of_day

Pure Python. The three decision trees below are *data*: they were fitted
offline by ``tools/vision/env_fit.py`` (scikit-learn, experiment venv only)
and pasted here as nested tuples. A node is ``(feature, threshold, left,
right)`` (``feature <= threshold`` goes left), a leaf is ``(class,
probabilities)`` with the probabilities in the order of the tree's
``*_CLASSES``. Origin, data and metrics:
docs/VISION_ENVIRONMENT_MODEL_2026-10-19.md.

Two layers:

* :meth:`EnvironmentModel.classify` — one frame, stateless;
* :meth:`EnvironmentModel.update` — the same plus a sliding-window soft vote
  (mean of the per-frame class probabilities over ``window_s`` = 10 s), which
  is what the car publishes; a single odd frame (a bridge, a lorry filling the
  windscreen) does not change the label.

Sun elevation, when known, is a *prior* on the time of day only: below −6°
(civil twilight) "day" is scaled by 0.05; above +10° "night" and
"dusk_dawn" are scaled by 0.05 / 0.2. The camera can still win with strong
evidence (a dark underpass at noon stays "night"). The headlight override by
the sun stays in :mod:`auto_lights`.

Replay a drive::

    python -m cyberpunk_computer.vision.environment --frames DIR   # JPEGs (ffprobe)
    python -m cyberpunk_computer.vision.environment --csv FEATURES.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from .scene_features import SceneFeatures, from_row

# ── fitted data (2026-10-19) ─────────────────────────────────────────────
# Data: BDD100K val (10 000 dashcam frames, labels timeofday / weather / scene;
# FiftyOne export dgural/bdd100k on Hugging Face), DAWN (704 fog/rain/snow frames),
# our 70mai drive of 2026-10-19 (174 frames, garage + sunny afternoon).
# Features: scene_features.extract (frames scaled to 640x360 first).

#: Time of day. sklearn DecisionTreeClassifier(max_depth=3, min_samples_leaf=40,
#: class_weight="balanced", random_state=0) on all 9 965 BDD val frames with a
#: day / dawn-dusk / night label (env_fit.py fit). Leaf probabilities are the
#: class-balanced training fractions. Held-out 30 %: accuracy 0.874, recall
#: day 0.87 / dusk 0.52 / night 0.96, precision 0.92 / 0.36 / 0.96.
TOD_CLASSES = ("day", "dusk_dawn", "night")
TOD_TREE: tuple = (
    "sky_y", 56.324,
    ("sky_blue", 5.391,
     ("sky_high", 90.5, ("night", (0.008, 0.014, 0.978)), ("night", (0.05, 0.237, 0.713))),
     ("dusk_dawn", (0.086, 0.646, 0.268))),
    ("edge_full", 0.038,
     ("full_dark", 0.053, ("day", (0.507, 0.465, 0.028)), ("dusk_dawn", (0.174, 0.768, 0.058))),
     ("edge_full", 0.051, ("day", (0.584, 0.402, 0.014)), ("day", (0.816, 0.17, 0.013)))),
)

#: Enclosed (garage, tunnel, covered car park): one conjunction, written as a
#: tree — lit (sky_y > 60), no bright sky above (top_drop <= 10, sky_high <=
#: 200) and no blue (sky_blue <= 2). Grid search (env_fit.py fit) over 27
#: hand-labelled BDD tunnel / car-park interiors + our 11 garage frames
#: against BDD outdoor frames and our sunny drive. Per frame: BDD enclosed
#: 0.56, our garage 0.82, BDD daytime false positives 0.028, our outdoor 0.
#: Leaf values are votes (1/0), not calibrated probabilities.
ENC_CLASSES = (False, True)
_NO, _YES = (False, (1.0, 0.0)), (True, (0.0, 1.0))
ENC_TREE: tuple = (
    "sky_y", 60.0, _NO,
    ("top_drop", 10.0,
     ("sky_high", 200.0, ("sky_blue", 2.0, _YES, _NO), _NO),
     _NO),
)

#: Weather: only what frame statistics support (daytime BDD): a blue sky
#: ("clear": sky_blue > 8 and sky_sat > 15; precision 0.82 for clear / partly
#: cloudy, recall 0.60) and a bright grey sky ("overcast": sky_blue <= 1,
#: sky_high >= 200, sky_sat <= 8; precision 0.90 for overcast / rain / snow /
#: fog, recall 0.32). Rain, snow and fog as separate classes are not
#: separable from these statistics (tree precision 0.30 / 0.17); never output.
WX_CLASSES = ("clear", "overcast", None)
_WX_NONE = (None, (0.0, 0.0, 1.0))
WX_TREE: tuple = (
    "sky_blue", 1.0,
    ("sky_high", 199.5, _WX_NONE, ("sky_sat", 8.0, ("overcast", (0.0, 1.0, 0.0)), _WX_NONE)),
    ("sky_blue", 8.0, _WX_NONE, ("sky_sat", 15.0, _WX_NONE, ("clear", (1.0, 0.0, 0.0)))),
)

#: A weather call is published when its window share reaches this.
WX_MIN_P = 0.6
#: Enclosed when the (window-averaged) enclosed vote exceeds this.
ENC_MIN_P = 0.5

TIMES_OF_DAY = TOD_CLASSES


def derived(f: Mapping[str, float]) -> Dict[str, float]:
    """Derived features from a SceneFeatures dict (or a CSV row of floats)."""
    return {
        "sky_blue": (f["sky_u"] - f["sky_v"]) / 2.0,    # >0 blue sky, <0 sodium/orange
        "top_mid": f["b0_y"] - f["b2_y"],               # top fifth vs middle fifth
        "top_drop": f["b0_y"] - f["b1_y"],              # top fifth vs second fifth
        "sky_spread": f["sky_high"] - f["sky_low"],     # 90 % - 10 % Y of the sky band
    }


def _walk(node: tuple, x: Mapping[str, float]) -> Tuple[object, Tuple[float, ...]]:
    while len(node) == 4:
        feat, thr, left, right = node
        node = left if x[feat] <= thr else right
    return node[0], node[1]


@dataclass(frozen=True)
class Environment:
    t: float
    time_of_day: str
    enclosed: bool
    weather: Optional[str]
    confidence: float
    label: str
    p_time: Tuple[float, ...] = ()      # probabilities in TOD_CLASSES order
    p_enclosed: float = 0.0

    @property
    def lights_wanted(self) -> bool:
        """Headlights wanted by the scene: night, dusk/dawn or enclosed."""
        return self.enclosed or self.time_of_day != "day"

    def as_dict(self) -> dict:
        return asdict(self)


def _features(features) -> Dict[str, float]:
    x = features.as_dict() if isinstance(features, SceneFeatures) else dict(features)
    x.update(derived(x))
    return x


def sun_prior(sun_elevation: Optional[float]) -> Tuple[float, float, float]:
    """Multipliers for (day, dusk_dawn, night) from the sun elevation."""
    if sun_elevation is None:
        return (1.0, 1.0, 1.0)
    if sun_elevation < -6.0:
        return (0.05, 1.0, 1.0)
    if sun_elevation > 10.0:
        return (1.0, 0.2, 0.05)
    return (1.0, 1.0, 1.0)


class EnvironmentModel:
    """Stateless :meth:`classify` + windowed :meth:`update`."""

    def __init__(self, window_s: float = 10.0) -> None:
        self.window_s = window_s
        self._win: Deque[Tuple[float, Tuple[float, ...], float, Tuple[float, ...]]] = deque()
        self.last: Optional[Environment] = None

    # ── one frame ─────────────────────────────────────────────────────────

    @staticmethod
    def probabilities(features) -> Tuple[Tuple[float, ...], float, Tuple[float, ...]]:
        """(time-of-day probabilities, P(enclosed), weather probabilities) of one frame."""
        x = _features(features)
        _, p_tod = _walk(TOD_TREE, x)
        _, p_enc = _walk(ENC_TREE, x)
        _, p_wx = _walk(WX_TREE, x)
        return tuple(p_tod), float(p_enc[1]), tuple(p_wx)

    @staticmethod
    def _decide(t: float, p_tod: Sequence[float], p_enc: float, p_wx: Sequence[float],
                sun_elevation: Optional[float]) -> Environment:
        prior = sun_prior(sun_elevation)
        w = [p * q for p, q in zip(p_tod, prior)]
        s = sum(w) or 1.0
        p_time = tuple(round(v / s, 3) for v in w)
        i = max(range(len(p_time)), key=lambda k: p_time[k])
        tod = TOD_CLASSES[i]
        enclosed = p_enc > ENC_MIN_P
        weather = None
        if tod == "day" and not enclosed and p_wx:
            j = max(range(len(p_wx)), key=lambda k: p_wx[k])
            if p_wx[j] >= WX_MIN_P:
                weather = WX_CLASSES[j]     # None for the "no call" class
        conf = p_enc if enclosed else p_time[i]
        return Environment(t=t, time_of_day=tod, enclosed=enclosed, weather=weather,
                           confidence=round(conf, 3), label="enclosed" if enclosed else tod,
                           p_time=p_time, p_enclosed=round(p_enc, 3))

    def classify(self, features, sun_elevation: Optional[float] = None) -> Environment:
        """One frame (``SceneFeatures`` or a dict with its fields), no state."""
        p_tod, p_enc, p_wx = self.probabilities(features)
        t = float(features.t if isinstance(features, SceneFeatures) else features.get("t", 0.0))
        return self._decide(t, p_tod, p_enc, p_wx, sun_elevation)

    # ── windowed ──────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._win.clear()
        self.last = None

    def update(self, features: Optional[SceneFeatures],
               sun_elevation: Optional[float] = None,
               t: Optional[float] = None) -> Optional[Environment]:
        """Add a frame (``None`` = no frame) and return the window's environment.

        ``None`` when the window holds no frame (camera absent for > window_s).
        """
        if t is None:
            t = features.t if features is not None else time.time()
        if features is not None:
            self._win.append((t, *self.probabilities(features)))
        while self._win and self._win[0][0] <= t - self.window_s:
            self._win.popleft()
        if not self._win:
            self.last = None
            return None
        n = len(self._win)
        p_tod = [sum(w[1][k] for w in self._win) / n for k in range(len(TOD_CLASSES))]
        p_enc = sum(w[2] for w in self._win) / n
        p_wx = [sum(w[3][k] for w in self._win) / n for k in range(len(WX_CLASSES))]
        self.last = self._decide(t, p_tod, p_enc, p_wx, sun_elevation)
        return self.last


# ── CLI ───────────────────────────────────────────────────────────────────

def _load(args) -> List[SceneFeatures]:
    if args.csv:
        with open(args.csv, newline="") as f:
            rows = [from_row(r) for r in csv.DictReader(f)]
        return [r for r in rows if r is not None]
    from .frames import DirectorySource
    from .scene_features import extract
    out = []
    for fr in DirectorySource(args.frames):
        f = extract(fr.path, t=fr.t)
        if f is not None:
            out.append(f)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cyberpunk_computer.vision.environment",
                                 description="Replay the environment model over a recorded drive.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="feature CSV with the SceneFeatures columns (tools/vision/env_features.py)")
    src.add_argument("--frames", help="folder of YYYYmmdd_HHMMSS.jpg frames (needs ffprobe)")
    ap.add_argument("--latlon", help="lat,lon for the sun prior (default: no sun prior)")
    ap.add_argument("--frame", action="store_true", help="per-frame labels instead of the 10 s window")
    ap.add_argument("--all", action="store_true", help="print every sample, not only label changes")
    args = ap.parse_args(argv)
    feats = _load(args)
    if not feats:
        print("no usable rows/frames (ffprobe missing?)", file=sys.stderr)
        return 1
    sun_fn = None
    if args.latlon:
        from .sun import solar_elevation
        lat, lon = (float(v) for v in args.latlon.split(","))
        sun_fn = lambda t: solar_elevation(t, lat, lon)   # noqa: E731
    model = EnvironmentModel()
    print("time                 label      tod        enc  weather   conf")
    prev, counts = None, {}
    for f in feats:
        sun = sun_fn(f.t) if sun_fn else None
        e = model.classify(f, sun) if args.frame else model.update(f, sun, t=f.t)
        counts[e.label] = counts.get(e.label, 0) + 1
        if args.all or e.label != prev:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.t))
            print(f"{ts}  {e.label:<10} {e.time_of_day:<10} {'yes' if e.enclosed else 'no ':<4}"
                  f" {e.weather or '-':<9} {e.confidence:4.2f}")
        prev = e.label
    print(f"{len(feats)} samples: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
