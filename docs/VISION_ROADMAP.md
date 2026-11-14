# Vision in this repository: lights, environment, signs, map speed limit

Scope of the in-repo `cyberpunk_computer/vision/` package and where each vision function lives.

Standing decisions:

- The 70mai dashcam stream (640×360) is used **only** for lights and the environment model (day,
  dusk, night, enclosed, weather) at 1 fps. It is not a sign-reading camera: digits are unreadable at
  640×360 (measured, [VISION_DETECTOR_EXPERIMENT_2026-10-19.md](VISION_DETECTOR_EXPERIMENT_2026-10-19.md) §7).
- **Single car computer (2026-11-04):** the POCO F1 also runs the roadsense perception stack
  (repos `roadsense`, `roadsense-signs`; camera, road model, signs, environment) under
  `roadsense.target` while ACC is on ([POCO/README.md](../POCO/README.md)). There is no second phone;
  the earlier plan of a flowpilot (openpilot on Android) phone with its own camera is superseded.
- One grabber process owns a camera and fans frames out to every consumer (the dashcam's RTSP server
  accepts one client).
- A detector never touches the store; the frontend only reads state.

## 1. Camera sources

| Source | Status | Use |
|---|---|---|
| 70mai dashcam, RTSP 640×360 @ 30 fps over its hotspot ([DASHCAM_70MAI.md](DASHCAM_70MAI.md)) | readable; one client at a time | environment model / lights at 1 fps (one long ffmpeg session) |
| roadsense camera on the POCO's camera hub (powerbox relay ch4) | owned by roadsense | roadsense perception; the backend sees its health and proxies its API (`api/roadsense_proxy.py`) |
| head unit reverse/front analog cameras ([HEADUNIT_INTEGRATION.md](HEADUNIT_INTEGRATION.md)) | hardware video plane, not capturable | none |

## 2. Package layout

```
cyberpunk_computer/vision/
    ffmpeg.py, frames.py   ffmpeg/ffprobe wrapper; FrameSource: DashcamStreamSource, DashcamRtspSource, DirectorySource
    light_features.py      per-frame statistics (sky band, road band, chroma, clip)
    scene_features.py      superset for the environment model (bands, percentiles, shares, edges)
    environment.py         EnvironmentModel: day / dusk_dawn / night / enclosed + weather hint
    sun.py                 solar elevation from time + position
    auto_lights.py         AutoLightModel: hysteresis state machine -> LightDecision
    detectors/             detector slot: Detection, Detector protocol, NullDetector, SubprocessDetector,
                           SubprocessYoloDetector, filter_detections, TfliteDetector (stub)
    maps/                  osm_speed_limit.py (OSM extract loader, grid index, map matching,
                           Polish implicit limits); data/gdansk_15km.json.gz + replay fixtures
    fusion/                speed_limit.py (SpeedLimitFusion: map prior + camera sightings, replay CLI);
                           service.py (SpeedLimitService.update(position) -> SetVisionStateAction)
```

State: `AppState.vision` — lights and environment fields (written by `io/dashcam_ingest.py`) and
`speed_limit_kmh`, `speed_limit_source`, `speed_limit_confidence`, `road_name`, `position_ok`
(written by `SpeedLimitService`, which nothing calls yet).

## 3. Lights

Data and rule: [DASHCAM_70MAI.md](DASHCAM_70MAI.md). Environment model (decision tree + rules
fitted on BDD100K val, DAWN and the first own drive; pure-Python runtime, 1 fps, 10 s vote) feeding
`AutoLightModel`: [VISION_ENVIRONMENT_MODEL_2026-10-19.md](VISION_ENVIRONMENT_MODEL_2026-10-19.md).
Output: `lights_recommended` with a reason. Actuation (AVC-LAN / CAN / BEAN or the head unit) is a
separate step and stays off until thresholds are confirmed on dusk, night and rain drives.

## 4. Road signs

Upstream openpilot does not read traffic signs or traffic lights (lanes, road edges, lead vehicles,
path). Speed-limit control exists only in forks (sunnypilot, FrogPilot), mostly from OpenStreetMap
or from the car's own sign recognition over CAN, which a 2004–2009 Prius does not have (checked
2026-10-19). So the cheapest first source of a speed limit is the map; a camera only confirms it.

### 4.1 Phase A: speed limit from the map (implemented, not wired)

Pure Python, no numpy, no network at runtime.

- `tools/osm_extract.py` — one Overpass query for a box (default 15×15 km around Gdańsk,
  `54.28263,18.5344,54.41737,18.7656`): drivable `highway=*` ways (motorway … service; no
  footways/cycleways/tracks) with geometry and the tags `maxspeed`, `maxspeed:forward/backward`,
  `maxspeed:type`, `source:maxspeed`, `zone:traffic`, `zone:maxspeed`, `maxspeed:conditional`,
  `name`, `ref`, `oneway`, `junction`, `lanes`, `motorroad`, `dual_carriageway`, `service`, plus
  `place=*` nodes. Compact JSON (gzipped if `.gz`), coordinates as 1e-5° integers; one request, own
  User-Agent, retry with 30/60/120 s backoff on 429/5xx/timeouts.

      python3 tools/osm_extract.py -o cyberpunk_computer/vision/maps/data/gdansk_15km.json.gz
      python3 tools/osm_extract.py --center 54.52,18.53 --size-km 20 -o gdynia.json.gz
      python3 tools/osm_extract.py --bbox S,W,N,E -o box.json.gz --save-raw raw.json
      python3 tools/osm_extract.py --clip S,W,N,E --in big.json.gz -o small.json.gz

  Bundled extract (OSM base 2026-10-19 15:22 UTC, © OpenStreetMap contributors, ODbL): 28 512 ways,
  8 582 with an explicit `maxspeed[:forward|:backward]`, 178 place nodes, 1.0 MB gzipped; 18 982 of
  the ways are `highway=service`. A whole-Pomorskie box is ~80× the area (size not measured).
- `vision/maps/osm_speed_limit.py` — `OsmSpeedMap.load(path)` (0.6 s, ~140 MB RSS for the 15 km box
  on the laptop), `match(Position) -> MapMatch`, `speed_limit(match) -> SpeedLimit`. Matching: local
  metric projection, 64 m grid buckets, nearest segment within 25 m with cost
  `distance + 0.25 m/° · heading error (+40 m wrong way on a oneway, +5 m for service roads)`; heading
  ignored below 5 km/h; `oneway`, `-1`, roundabouts, motorways honoured; the direction picks
  `maxspeed:forward/backward` (no heading: the lower one). 0.01–0.05 ms per lookup. Junctions =
  vertices shared by two non-service ways; `MapMatch.at_junction` = within 30 m.
- Limits (Polish defaults): explicit `maxspeed` (also `PL:zone30`-style values) → basis
  `explicit maxspeed`; else motorway 140, expressway (`motorroad=yes`) 120 dual / 100 single, living
  street 20, built-up 50 → `implicit built-up`; outside 100 on a 2-lane dual carriageway, else 90 →
  `implicit road class`. **Built-up is heuristic** (OSM has no D-42/D-43 polygons), per way: own
  `PL:urban`/`PL:rural`/`PL:zone*` tags → `residential`/`living_street` → majority of neighbouring
  ways (tags, `maxspeed=50`/`90`) → within a radius of a `place` node → not built-up. Confidence =
  basis confidence × match confidence; service roads and links are capped low.
- `vision/fusion/speed_limit.py` — `SpeedLimitFusion`: priority camera override > end-of-limit
  default > map explicit > built-up sign default > map implicit. A disagreeing `Sighting(value_kmh,
  score, side, t, kind)` counts only on our side (`right`/`overhead`; `left` on a oneway), not at a
  junction, score ≥ 0.5; two consistent ones within 30 s override. Agreeing sightings raise the
  confidence (+0.25·score) and refresh an override. Overrides end at the next junction (> 50 m after
  the sign: the Polish B-33 rule), after 3 km or 10 min. `end_limit` → the road's default;
  `built_up_begin`/`end` → 50 / road-class default until the opposite sign (20 km cap). A new map
  value needs 2 consecutive fixes; a lost match holds 10 s.
- `vision/fusion/service.py` — `SpeedLimitService.update(position) -> SetVisionStateAction` (GPS
  ingest hook) and `add_sighting(s)` (detector hook); `position_ok` = valid fix inside the extract box.
- Replay (CSV `t,lat,lon[,heading_deg,speed_kmh]` or GPX; sightings CSV `t,value_kmh,score,side[,kind]`):

      python -m cyberpunk_computer.vision.fusion.speed_limit \
          --track cyberpunk_computer/vision/maps/data/track_old_town_to_armii_krajowej.csv \
          --sightings cyberpunk_computer/vision/maps/data/sightings_armii_krajowej.csv [--map X.json.gz] [--all]

  The fixture is a synthetic 7.7 km shortest-path drive (60 km/h, 1 Hz, 1.5 m noise) from Podwale
  Przedmiejskie along Aleja Armii Krajowej: 30 → 50 → 70 → 80 → 50 → 100, 0.12 ms/step.

### 4.2 Phase B: camera confirmation of signs (design)

- **Where:** `vision/detectors/` on the car computer, fed by a shared grabber at 2–5 fps from a
  road-facing camera with enough resolution (not the dashcam stream, see below).
- **Model:** a nano detector (YOLO v8n/11n/12n family) at 320–416 px input, on the Adreno 630 through
  LiteRT with the GPU delegate or ncnn Vulkan, as a native helper speaking the `SubprocessDetector`
  JSON-lines protocol (the backend has no numpy). Classes: speed limits (20…140, end of limit),
  no-entry, stop, give-way, built-up area begin/end, priority road; later traffic lights. Training
  data: Mapillary Traffic Sign Dataset (includes Polish signs), GTSRB/GTSDB as a warm start; fine-tune
  on own frames labelled offline by a large vision model, then corrected by hand.
- **Two-stage reading:** the detector finds the sign, a classifier (or the detector's classes) reads
  the number; a track of 3+ frames with a consistent class is one `Sighting` for the fusion in §4.1.
- **Resolution budget (measured, experiment §7):** at 640×360 a 60 cm sign is 17–25 px only when
  nearly level with the car (about 5–8 m); shape/colour classes work at that size, digits do not
  (3–4 px per digit). The map prior carries the number; a 640×360 camera can only confirm the sign type.
- **Latency budget:** 5 fps detector, 3-frame track → a confirmed sighting about 1 s after the sign
  enters the readable zone. 10 s sampling misses most signs.
- **Measured on the laptop (experiment §4):** yolo11n at 416, ONNX Runtime 14–23 ms/frame on 4 cores,
  ncnn Vulkan 7–9 ms on a Quadro; TFLite int8 3.1 MB but loses small objects (traffic lights 33→20),
  so GPU fp16 is the first choice on the phone. Phone estimate 10–25 fps at 416 (±2×, not measured).

### 4.3 Phase C: traffic lights and other scene state (design)

Same detector slot; red/green state of the light ahead with the same track-and-vote fusion, for
display only. Weather and wet road stay in the cheap statistics path unless they prove weak.

## Open gaps

- No GPS source exists in the backend, so `SpeedLimitService` is not wired and the speed-limit fields
  stay empty.
- Overlap with roadsense (`envd` environment, `signd` signs): which of the dashcam ingest, the
  environment model, the detector slot and the map fusion remain in use next to roadsense is an
  owner decision not recorded here. The dashcam ingest is off in the provisioned configuration
  ([DASHCAM_70MAI.md](DASHCAM_70MAI.md)).
- Map gaps: time-dependent limits (`maxspeed:conditional` kept but ignored), temporary and
  construction limits, zone signs (`strefa tempo 30`, `strefa zamieszkania` known only where each way
  is tagged), vehicle-class limits, expressway/motorway rules inside built-up areas, the built-up
  heuristic at town edges (the place-radius rule calls the whole Gdańsk box built-up), service-road
  limits, map age (re-run the extractor).
- Thresholds for lights need dusk, night, rain, overcast and tunnel drives.
- Phase B needs a road-facing camera with more than 640×360 and a phone measurement of the detector.
