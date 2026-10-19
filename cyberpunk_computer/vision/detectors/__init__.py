"""Object-detector slot of the vision package (docs/VISION_ROADMAP.md §4.2).

The stable interface is small on purpose:

* :class:`Detection` — one box: class name, score, ``(x0, y0, x1, y1)`` in
  pixels of the original frame, capture time ``t`` (epoch seconds);
* :class:`Detector` — anything with ``process(frame_path, t) -> list[Detection]``.
  A detector never raises on a bad frame or a dead model; it returns ``[]``.

Implementations:

* :class:`NullDetector` — always ``[]`` (the default when no model is set up);
* :class:`SubprocessDetector` — runs a helper process that speaks the
  JSON-lines protocol of ``tools/vision/yolo_detect.py --stdin`` (one frame
  path in, one JSON line out) and parses its answers. The backend imports
  nothing heavier than :mod:`json`/:mod:`subprocess`;
* :class:`SubprocessYoloDetector` — the above, wired to the experiment venv
  (``.venv-vision``: torch + ultralytics) for offline evaluation on the laptop;
* :class:`TfliteDetector` — placeholder for the on-car (Poco F1) model; it
  documents the intended LiteRT path and raises :class:`NotImplementedError`.

Like the rest of :mod:`cyberpunk_computer.vision`, this module is pure Python
(no numpy, PIL, OpenCV or torch). Results and timings of the first experiment
are in docs/VISION_DETECTOR_EXPERIMENT_2026-10-19.md.
"""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import time
from dataclasses import dataclass
from typing import (Callable, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple,
                    runtime_checkable)

logger = logging.getLogger(__name__)

Box = Tuple[float, float, float, float]

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
#: Experiment venv (torch + ultralytics); never imported, only executed.
DEFAULT_VENV = os.path.join(_REPO, ".venv-vision")
DEFAULT_PYTHON = os.environ.get("VISION_YOLO_PYTHON", os.path.join(DEFAULT_VENV, "bin", "python"))
DEFAULT_SCRIPT = os.path.join(_REPO, "tools", "vision", "yolo_detect.py")
DEFAULT_MODEL = os.environ.get("VISION_YOLO_MODEL",
                               os.path.join(DEFAULT_VENV, "models", "yolo11n.pt"))

#: COCO classes that matter on the road (traffic-light state comes later).
ROAD_CLASSES = ("car", "truck", "bus", "motorcycle", "bicycle", "person",
                "traffic light", "stop sign")

#: The suction-cup phone mount seen by the 70mai at 640x360 (2026-10-19 drive):
#: COCO nano calls it person/cow/dog/bicycle/bird in ~20 % of the frames.
#: Valid only for the current mounting; re-measure if the mount moves.
DASHCAM_MOUNT_REGION: Box = (495.0, 155.0, 640.0, 310.0)


@dataclass(frozen=True)
class Detection:
    cls: str
    score: float
    box: Box          # x0, y0, x1, y1 in pixels of the original frame
    t: float          # capture time of the frame, epoch seconds

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.box[0] + self.box[2]) / 2.0, (self.box[1] + self.box[3]) / 2.0)

    def to_dict(self) -> dict:
        return {"cls": self.cls, "score": self.score, "box": list(self.box), "t": self.t}

    @classmethod
    def from_dict(cls, d: Mapping, t: Optional[float] = None) -> "Detection":
        """From a ``{"cls", "score", "box"[, "t"]}`` mapping; ``t`` overrides."""
        x0, y0, x1, y1 = (float(v) for v in d["box"])
        return cls(cls=str(d["cls"]), score=float(d["score"]), box=(x0, y0, x1, y1),
                   t=float(t if t is not None else d.get("t", 0.0)))


@runtime_checkable
class Detector(Protocol):
    def process(self, frame_path: str, t: float) -> List[Detection]:
        """Detections in one frame (``[]`` if none or on any failure)."""
        ...


class NullDetector:
    """Sees nothing. The default detector when no model is installed."""

    def process(self, frame_path: str, t: float) -> List[Detection]:
        return []

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# Parsing and filtering (pure functions, shared by every implementation)
# --------------------------------------------------------------------------

def parse_result(obj: Mapping, t: float) -> Optional[List[Detection]]:
    """One ``yolo_detect.py`` answer → detections; ``None`` for an error,
    a ``ready`` banner or a malformed object."""
    if not isinstance(obj, Mapping) or "error" in obj or "detections" not in obj:
        return None
    try:
        return [Detection.from_dict(d, t) for d in obj["detections"]]
    except (KeyError, TypeError, ValueError):
        return None


def parse_line(line: str, t: float) -> Optional[List[Detection]]:
    """:func:`parse_result` of one JSON line (``None`` if it is not JSON)."""
    try:
        return parse_result(json.loads(line), t)
    except ValueError:
        return None


def _inside(point: Tuple[float, float], region: Box) -> bool:
    return region[0] <= point[0] <= region[2] and region[1] <= point[1] <= region[3]


def filter_detections(dets: Iterable[Detection], min_score: float = 0.0,
                      classes: Optional[Iterable[str]] = None,
                      ignore_regions: Sequence[Box] = (),
                      max_area_frac: Optional[float] = None,
                      frame_size: Tuple[int, int] = (640, 360)) -> List[Detection]:
    """Drop low scores, unwanted classes, boxes centred in an ignore region
    (e.g. :data:`DASHCAM_MOUNT_REGION`) and boxes covering more than
    ``max_area_frac`` of the frame (the nano model's whole-bonnet "car"/"boat")."""
    keep = set(classes) if classes is not None else None
    limit = max_area_frac * frame_size[0] * frame_size[1] if max_area_frac else None
    out = []
    for d in dets:
        if d.score < min_score or (keep is not None and d.cls not in keep):
            continue
        if limit is not None and d.area > limit:
            continue
        if any(_inside(d.center, r) for r in ignore_regions):
            continue
        out.append(d)
    return out


# --------------------------------------------------------------------------
# Subprocess detectors
# --------------------------------------------------------------------------

class SubprocessDetector:
    """A long-lived helper process speaking JSON lines on stdin/stdout.

    Protocol (``tools/vision/yolo_detect.py --stdin``): the helper prints one
    ``{"ready": true, ...}`` line once its model is loaded, then answers every
    frame path written to its stdin with exactly one JSON line. The helper is
    started lazily on the first :meth:`process`; if it dies or stalls it is
    killed, the frame gives ``[]``, and the next call restarts it after
    ``restart_delay_s``. Nothing here raises.

    The same protocol is the intended contract for the on-car helper (a native
    LiteRT/ncnn binary or an Android service), see :class:`TfliteDetector`.
    """

    def __init__(self, cmd: Sequence[str], timeout_s: float = 10.0,
                 start_timeout_s: float = 120.0, restart_delay_s: float = 30.0,
                 env: Optional[Mapping[str, str]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cmd = list(cmd)
        self.timeout_s = timeout_s
        self.start_timeout_s = start_timeout_s
        self.restart_delay_s = restart_delay_s
        self.env = dict(env) if env is not None else None
        self._clock = clock
        self._proc: Optional[subprocess.Popen] = None
        self._failed_at: Optional[float] = None
        self._buf = b""
        self.last_result: Optional[dict] = None   # raw answer for the last frame

    # -- process management -------------------------------------------------

    def _readline(self, timeout: float) -> Optional[str]:
        """One line from the helper within ``timeout`` s (own buffer: select()
        and a buffered file object do not mix)."""
        p = self._proc
        if p is None or p.stdout is None:
            return None
        deadline = self._clock() + timeout
        while b"\n" not in self._buf:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([p.stdout], [], [], remaining)
                if not ready:
                    return None
                chunk = os.read(p.stdout.fileno(), 65536)
            except (OSError, ValueError):
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", "replace")

    def _start(self) -> bool:
        if self._failed_at is not None and self._clock() - self._failed_at < self.restart_delay_s:
            return False
        env = None
        if self.env is not None:
            env = dict(os.environ)
            env.update(self.env)
        try:
            self._proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, bufsize=0, env=env)
            self._buf = b""
        except OSError as e:
            logger.warning("detector: cannot start %s: %s", self.cmd[0], e)
            self._fail()
            return False
        deadline = self._clock() + self.start_timeout_s
        while True:
            line = self._readline(max(0.0, deadline - self._clock()))
            if line is None:
                logger.warning("detector: %s gave no ready line", self.cmd[0])
                self._fail()
                return False
            try:
                if json.loads(line).get("ready"):
                    self._failed_at = None
                    return True
            except (ValueError, AttributeError):
                pass   # stray output before the banner

    def _fail(self) -> None:
        self.close()
        self._failed_at = self._clock()

    def close(self) -> None:
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            p.kill()
            try:
                p.wait(timeout=2)
            except subprocess.SubprocessError:
                pass

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    # -- Detector -----------------------------------------------------------

    def process(self, frame_path: str, t: float) -> List[Detection]:
        self.last_result = None
        if self._proc is None or self._proc.poll() is not None:
            self._proc = None
            if not self._start():
                return []
        try:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write((os.path.abspath(frame_path) + "\n").encode())
            self._proc.stdin.flush()
        except (OSError, ValueError, AssertionError):
            self._fail()
            return []
        line = self._readline(self.timeout_s)
        if line is None:
            logger.warning("detector: no answer for %s", frame_path)
            self._fail()
            return []
        try:
            self.last_result = json.loads(line)
        except ValueError:
            return []
        return parse_result(self.last_result, t) or []


class SubprocessYoloDetector(SubprocessDetector):
    """Ultralytics YOLO in the experiment venv, via ``tools/vision/yolo_detect.py``.

    For offline evaluation on the laptop (``.venv-vision`` with torch +
    ultralytics, see docs/VISION_DETECTOR_EXPERIMENT_2026-10-19.md). ``model``
    can be a ``.pt``, ``.onnx``, ``.tflite`` or ``*_ncnn_model`` path;
    ``device`` is ``cpu``, ``vulkan:N`` (ncnn) or a CUDA index.
    """

    def __init__(self, model: str = DEFAULT_MODEL, imgsz: int = 416, conf: float = 0.25,
                 classes: Optional[Iterable[str]] = ROAD_CLASSES, device: str = "cpu",
                 threads: int = 0, python: str = DEFAULT_PYTHON, script: str = DEFAULT_SCRIPT,
                 **kw) -> None:
        cmd = [python, "-I", script, "--stdin", "--model", model, "--imgsz", str(imgsz),
               "--conf", str(conf), "--device", device]
        if classes:
            cmd += ["--classes", ",".join(classes)]
        if threads:
            cmd += ["--threads", str(threads)]
        super().__init__(cmd, **kw)
        self.model = model

    @staticmethod
    def available(python: str = DEFAULT_PYTHON, script: str = DEFAULT_SCRIPT,
                  model: str = DEFAULT_MODEL) -> bool:
        """Are the experiment venv, the helper script and the model present?"""
        return os.access(python, os.X_OK) and os.path.isfile(script) and os.path.exists(model)


# --------------------------------------------------------------------------
# On-car detector (not implemented yet)
# --------------------------------------------------------------------------

class TfliteDetector:
    """Placeholder for the on-car model (Poco F1, Snapdragon 845, Adreno 630).

    Not implemented: :meth:`process` raises :class:`NotImplementedError`.

    Why it is not a few lines of Python: the LiteRT Python API
    (``ai_edge_litert`` / the older ``tflite_runtime``) takes and returns
    numpy arrays, and the backend runs without numpy. Two ways forward,
    both keeping this module dependency-free:

    1. **Helper process (preferred).** A small native binary on the phone
       (LiteRT C++ with the GPU delegate, or ncnn with Vulkan) that speaks the
       JSON-lines protocol of ``tools/vision/yolo_detect.py --stdin``; the
       backend then uses :class:`SubprocessDetector` unchanged with
       ``cmd=[helper, "--model", model_path, ...]``.
    2. **In-process with numpy**, if numpy ever becomes acceptable on the car.
       The intended code path (not executed)::

           # from ai_edge_litert.interpreter import Interpreter, load_delegate
           # delegates = [load_delegate("libtensorflowlite_gpu_delegate.so")] if use_gpu else []
           # it = Interpreter(model_path=model_path, num_threads=threads,
           #                  experimental_delegates=delegates)
           # it.allocate_tensors()
           # inp, out = it.get_input_details()[0], it.get_output_details()[0]
           # # frame -> letterboxed imgsz x imgsz RGB, no PIL: ffmpeg does it
           # #   ffmpeg -i frame.jpg -vf "scale=416:-2,pad=416:416:0:(oh-ih)/2"
           # #          -f rawvideo -pix_fmt rgb24 -
           # x = np.frombuffer(rgb, np.uint8).reshape(1, imgsz, imgsz, 3)
           # x = x.astype(np.float32) / 255.0          # (int8 model: quantise with inp["quantization"])
           # it.set_tensor(inp["index"], x); it.invoke()
           # y = it.get_tensor(out["index"])[0]        # (84, N): cx, cy, w, h, 80 class scores
           # # -> score threshold, per-class NMS, undo letterbox -> Detection(...)

    Exported models from the 2026-10-19 experiment: ``yolo11n_416_fp32.tflite``
    (10.7 MB) and ``yolo11n_416_int8.tflite`` (3.1 MB); the exporter offers no
    fp16 TFLite any more (LiteRT format: fp32, int8, w8a16, w8a32).
    """

    def __init__(self, model_path: str, labels: Sequence[str] = (), imgsz: int = 416,
                 threads: int = 4, use_gpu: bool = True, conf: float = 0.25) -> None:
        self.model_path = model_path
        self.labels = list(labels)
        self.imgsz = imgsz
        self.threads = threads
        self.use_gpu = use_gpu
        self.conf = conf

    def process(self, frame_path: str, t: float) -> List[Detection]:
        raise NotImplementedError(
            "TfliteDetector is a placeholder: the LiteRT Python API needs numpy, which the "
            "backend does not have. Run the model in a helper process speaking the "
            "yolo_detect.py JSON-lines protocol and use SubprocessDetector "
            "(see docs/VISION_DETECTOR_EXPERIMENT_2026-10-19.md).")

    def close(self) -> None:
        pass


__all__ = [
    "Box", "Detection", "Detector", "NullDetector", "SubprocessDetector",
    "SubprocessYoloDetector", "TfliteDetector", "filter_detections", "parse_line",
    "parse_result", "DASHCAM_MOUNT_REGION", "ROAD_CLASSES",
]
