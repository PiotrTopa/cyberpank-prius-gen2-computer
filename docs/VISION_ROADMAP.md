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
    maps/              offline map data (2026-10-19): osm_speed_limit.py = OSM extract loader,
                       grid index, map matching, Polish implicit limits; data/gdansk_15km.json.gz
    fusion/            temporal voting + state rules per topic (2026-10-19):
                       speed_limit.py = SpeedLimitFusion (map prior + camera sightings) + replay CLI;
                       service.py = SpeedLimitService.update(position) -> SetVisionStateAction
```

State: `VehicleState.vision` (lights fields; `speed_limit_kmh`, `speed_limit_source`,
`speed_limit_confidence`, `road_name`, `position_ok` since 2026-10-19; later `signs_recent`,
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

#### Implemented 2026-10-19: map prior + fusion (no GPS ingest yet)

Pure Python (no numpy, no network at runtime). Pieces:

- `tools/osm_extract.py` — one Overpass query for a box (default 15×15 km around Gdańsk,
  `54.28263,18.5344,54.41737,18.7656`): drivable `highway=*` ways (motorway … service; no
  footways/cycleways/tracks) with geometry and the tags `maxspeed`, `maxspeed:forward/backward`,
  `maxspeed:type`, `source:maxspeed`, `zone:traffic`, `zone:maxspeed`, `maxspeed:conditional`,
  `name`, `ref`, `oneway`, `junction`, `lanes`, `motorroad`, `dual_carriageway`, `service`, plus
  `place=*` nodes. Writes compact JSON (gzipped if `.gz`), coordinates as 1e-5° integers. Polite:
  one request, own User-Agent, retry with 30/60/120 s backoff on 429/5xx/timeouts (the public
  server answered 502 three times on 2026-10-19 before serving the box).

      python3 tools/osm_extract.py -o cyberpunk_computer/vision/maps/data/gdansk_15km.json.gz
      python3 tools/osm_extract.py --center 54.52,18.53 --size-km 20 -o gdynia.json.gz
      python3 tools/osm_extract.py --bbox S,W,N,E -o box.json.gz --save-raw raw.json
      python3 tools/osm_extract.py --clip S,W,N,E --in big.json.gz -o small.json.gz

  The bundled extract (OSM base 2026-10-19 15:22 UTC, © OpenStreetMap contributors, ODbL):
  28 512 ways, 8 582 with an explicit `maxspeed[:forward|:backward]`, 178 place nodes, 1.0 MB
  gzipped; 18 982 of the ways are `highway=service` (driveways, parking aisles). A whole-
  Pomorskie box is ~80× the area (size not measured): produce it with `--bbox` when needed.
- `vision/maps/osm_speed_limit.py` — `OsmSpeedMap.load(path)` (0.6 s, ~140 MB RSS for the
  15 km box on the P52), `match(Position) -> MapMatch`, `speed_limit(match) -> SpeedLimit`.
  Matching: local metric projection, 64 m grid buckets, nearest segment within 25 m with cost
  `distance + 0.25 m/° · heading error (+40 m wrong way on a oneway, +5 m for service roads)`;
  heading ignored below 5 km/h; `oneway`, `-1`, roundabouts, motorways honoured; the direction
  picks `maxspeed:forward/backward` (no heading: the lower one). 0.01–0.05 ms per lookup.
  Junctions = vertices shared by two non-service ways; `MapMatch.at_junction` = within 30 m.
- Limits (Polish defaults): explicit `maxspeed` (also `PL:zone30`-style values) → basis
  `explicit maxspeed`; else motorway 140, expressway (`motorroad=yes`) 120 dual / 100 single,
  living street 20, built-up 50 → `implicit built-up`; outside 100 on a 2-lane dual carriageway,
  else 90 → `implicit road class`. **Built-up is heuristic** (OSM has no D-42/D-43 polygons),
  resolved per way: own `PL:urban`/`PL:rural`/`PL:zone*` tags (reliable) → `residential`/
  `living_street` → majority of neighbouring ways (tags, `maxspeed=50`/`90`) → within a radius
  of a `place` node → not built-up. Confidence = basis confidence × match confidence; service
  roads and links are capped low.
- `vision/fusion/speed_limit.py` — `SpeedLimitFusion`: priority camera override > end-of-limit
  default > map explicit > built-up sign default > map implicit. A disagreeing `Sighting(value_kmh,
  score, side, t, kind)` counts only on our side (`right`/`overhead`; `left` on a oneway), not at
  a junction, score ≥ 0.5; two consistent ones within 30 s override. Agreeing sightings raise the
  confidence (+0.25·score) and refresh an override. Overrides end at the next junction (> 50 m
  after the sign: the Polish B-33 rule), after 3 km or 10 min. `end_limit` → the road's default;
  `built_up_begin`/`end` → 50 / road-class default until the opposite sign (20 km cap). A new map
  value needs 2 consecutive fixes (no flicker on crossing streets); a lost match holds 10 s.
- `vision/fusion/service.py` — `SpeedLimitService.update(position) -> SetVisionStateAction`
  (**the GPS ingest hook**) and `add_sighting(s)` (the detector hook); publishes
  `vision.speed_limit_kmh/_source/_confidence`, `road_name`, `position_ok` (valid fix inside the
  extract box). Not wired into the backend: no GPS source exists yet.
- Replay a drive (CSV `t,lat,lon[,heading_deg,speed_kmh]` or GPX; sightings CSV
  `t,value_kmh,score,side[,kind]`):

      python -m cyberpunk_computer.vision.fusion.speed_limit \
          --track tests/fixtures/osm/track_old_town_to_armii_krajowej.csv \
          --sightings tests/fixtures/osm/sightings_armii_krajowej.csv [--map X.json.gz] [--all]

  The fixture is a synthetic 7.7 km shortest-path drive (60 km/h, 1 Hz, 1.5 m noise) from
  Podwale Przedmiejskie along Aleja Armii Krajowej: 30 → 50 → 70 → 80 → 50 → 100, 0.12 ms/step.

Known gaps: time-dependent limits (`maxspeed:conditional` is kept but ignored), temporary and
construction limits, zone signs (`strefa tempo 30`, `strefa zamieszkania` areas are only known
where each way is tagged), vehicle-class limits, expressway/motorway rules inside built-up
areas, the built-up heuristic at town edges (the place-radius rule calls the whole Gdańsk box
built-up), service-road limits (parking lots are not public roads), map age (re-run the
extractor), and GPS: no ingest exists, so nothing publishes these fields yet.

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
- **Fusion** (implemented 2026-10-19, see 4.1 above; the detector only has to emit
  `Sighting`s): `vision/fusion/speed_limit.py` holds the current limit: map prior + sightings; a
  sighting that agrees raises confidence; one that disagrees overrides after two consistent frames
  if the sign is on our side of the road and we are not passing a junction; limits expire by
  distance or by the next sighting; end-of-limit signs and built-up-area signs reset to defaults.
- **Resolution budget (measured 2026-10-19, `docs/VISION_DETECTOR_EXPERIMENT_2026-10-19.md`):** at
  640×360 a sign is 17–25 px only when nearly level with the car (about 5–8 m for a 60 cm disc);
  shape/colour classes work at that size, digits of a speed-limit disc do not (3–4 px per digit).
  So the dashcam stream confirms sign *type* close up; reading numbers needs a higher-resolution
  road-facing camera (the computer's own phone camera at 1080p, or a dashcam with a higher-res
  stream). The map prior therefore carries the number; the camera confirms the sign.
- **Latency budget:** 5 fps detector, 3-frame track, so a confirmed sighting about 1 s after the
  sign enters the readable zone; the 10 s sampler misses most signs, so the shared grabber must
  run at 2–5 fps for this phase.
- **Measured on the laptop (same doc):** yolo11n at 416 input, ONNX Runtime 14–23 ms/frame on 4
  cores, ncnn Vulkan 7–9 ms on the Quadro; TFLite int8 3.1 MB but loses small objects (traffic
  lights 33→20), so prefer GPU fp16 on the phone. Phone estimate 10–25 fps at 416 (±2×, not
  measured). COCO classes already give cars (157/174 frames) and traffic lights (30 frames).
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
