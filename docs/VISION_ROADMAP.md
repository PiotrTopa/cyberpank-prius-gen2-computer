# Vision roadmap: lights now, signs later, next to a flowpilot port

Decision 2026-10-19 (user): keep the automatic-headlight logic simple (no machine learning); do
scene understanding later. flowpilot (openpilot on Android) will run on its **own, separate Poco F1
with its own camera**; the car computer (the other Poco F1) shares nothing with it. This document
fixes *where* each vision function lives so that the later work drops into a prepared slot instead
of a rewrite.

## 1. Camera sources in the car

| Source | Today | Later |
|---|---|---|
| 70mai dashcam, RTSP 640×360 @ 30 fps over its hotspot (`docs/DASHCAM_70MAI.md`) | the only camera the computer can read; one client at a time | stays the "cheap" source for brightness/weather sampling at 0.2 fps |
| flowpilot Poco F1 (separate device, own camera) | not present | fully owned by flowpilot; the car computer may *receive* its outputs (lead car, lane lines, path) over the car network as an optional feed, never its camera |
| car computer Poco F1 own camera | not used | optional second source if it can be mounted facing the road; otherwise the dashcam stays the only road view |
| Head unit reverse/front analog cameras (`docs/HEADUNIT_INTEGRATION.md`) | hardware video plane, not capturable | n/a for vision |

Rule: one grabber process owns a camera and fans frames out to every consumer (the dashcam's RTSP
server accepts one client, so the brightness sampler and a sign detector must share one grab).
flowpilot's camera is on another phone and is not ours.

## 2. Package layout (host repo)

```
cyberpunk_computer/vision/
    frames.py          FrameSource protocol: DashcamRtspSource, DirectorySource (replay)
                       later: a shared grabber feeding several consumers from one RTSP session;
                       optional FlowpilotFeed (network subscriber for flowpilot *outputs*, not frames)
    light_features.py  per-frame statistics via ffmpeg/ffprobe (sky band, road band, chroma, clip)
    sun.py             solar elevation from time + position
    auto_lights.py     AutoLightModel: hysteresis state machine -> LightDecision
    detectors/         (empty slot) one module per detector, same interface:
                       Detector.process(frame, t) -> list[Detection]; Detection = class, box,
                       score, t. A detector never touches the store directly.
    fusion/            (empty slot) temporal voting + state rules per topic
                       (e.g. speed_limit.py keeps the current limit from votes + map prior)
```

State: `VehicleState.vision` (lights fields now; later `speed_limit`, `signs_recent`,
`traffic_light`) published like every other ingest; the frontend only reads state.

## 3. Lights (now)

See `docs/DASHCAM_70MAI.md` for the data and the rule. Inputs: sun elevation (clock + position),
sky-band luma, sky chroma, clipped fraction; outputs `lights_recommended` with a reason. The
actuation (AVC-LAN / CAN or the head unit) is a separate step and is not switched automatically
until the thresholds have been confirmed on dusk, night and rain drives.

## 4. Road signs (later): what openpilot gives and what it does not

- Upstream openpilot does not read traffic signs or traffic lights; its model gives lanes, road
  edges, lead vehicles and a driving path. Speed-limit control exists only in forks (sunnypilot,
  FrogPilot) and there it comes mostly from map data (OpenStreetMap through a local map daemon)
  or from the car's own CAN when the car already has sign recognition; a 2004–2009 Prius has none.
  flowpilot follows upstream and has no sign reading either. (Checked 2026-10-19; a web search
  returned nothing newer, so verify against the forks' current docs when the port starts.)
- Consequence: sign reading is our job if we want it, and the cheapest first source of a speed
  limit is not a camera at all.

### 4.1 Phase A: speed limit from the map (no camera)

- Offline OpenStreetMap extract for the region (Pomorskie, a few hundred MB as a routable graph,
  or just `maxspeed` tags around the current position).
- Map-match the GPS fix to the nearest way with heading, read `maxspeed` (+ implicit defaults:
  50 in built-up areas, 90/120/140 by road class in Poland), publish `speed_limit` with source
  `map` and a confidence.
- This gives a usable limit on every road instantly and is what the forks do. It also provides the
  *prior* for the camera phase: a sign reader only has to confirm or override a known candidate.

### 4.2 Phase B: camera confirmation of signs

- **Where:** `vision/detectors/signs.py` on the car computer Poco, fed by the dashcam stream
  through the shared grabber (2–5 fps), or by the computer's own camera if one is mounted facing
  the road. flowpilot's camera on the other phone is not involved.
- **Model:** a nano-size detector (YOLO v8n/11n/12n family) at 320–416 px input, exported to
  TensorFlow Lite / LiteRT with the GPU delegate (Snapdragon 845: Adreno 630) or ONNX Runtime with
  NNAPI; expect a few frames per second, which is enough for signs that stay in view for seconds.
  Classes: speed limits (20…140, end of limit), no-entry, stop, give-way, built-up area begin/end,
  priority road; later traffic lights (red/green/arrow). European training data: Mapillary Traffic
  Sign Dataset (includes Polish signs), GTSRB/GTSDB as a warm start (German signs are near-identical
  to Polish ones). Fine-tune on frames from our own drives; label them with a large vision model
  offline first, then correct by hand.
- **Two-stage reading:** detector finds the round sign, a tiny classifier (or the detector's
  classes) reads the number; a track across 3+ frames with consistent class is one "sighting".
- **Fusion:** `vision/fusion/speed_limit.py` holds the current limit: map prior + sightings; a
  sighting that agrees raises confidence; one that disagrees overrides after two consistent frames
  if the sign is on our side of the road and we are not passing a junction; limits expire by
  distance or by the next sighting; end-of-limit signs and built-up-area signs reset to defaults.
- **Resolution budget:** at 640×360 a 60 cm sign is readable from about 15–20 m; the Poco camera
  at 1080p doubles that. Good enough for confirmation, not for reading signs across a dual
  carriageway.
- **Latency budget:** 5 fps detector, 3-frame track, so a confirmed sighting about 1 s after the
  sign enters the readable zone.
- **Compute budget on the Poco:** the whole Snapdragon 845 is ours (flowpilot runs elsewhere);
  a nano detector at 2–5 fps with the GPU delegate leaves room for the backend. Measure before
  promising.

### 4.3 Phase C: traffic lights and other scene state

- Same detector slot; red/green state of the light ahead with the same track-and-vote fusion;
  useful for the dashboard (not for control).
- Weather and wet road from the dashcam statistics (wiper period, road-band texture) stay in the
  cheap path; a small image classifier can replace them later if the statistics prove weak.

## 5. Order of work

1. Lights model + tests + optional backend ingest (in progress 2026-10-19).
2. Dusk, night, rain, overcast and tunnel drives sampled; thresholds fixed; actuation enabled.
3. Phase A map speed limit (independent of any camera; can start any time).
4. flowpilot port (user) on its own Poco. The only touch point is an optional network feed of its
   outputs into the car computer's state; keep the vision package's frame interface stable so a
   shared grabber or a second camera is a new class, not a change.
5. Phase B sign detector, first offline on recorded drives, then on the Poco.
6. Phase C.
