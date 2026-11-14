# 70mai dashcam as a camera source

The 70mai dashcam in the Prius serves a live H.264 stream over its own Wi-Fi hotspot. The backend
can use it as a forward camera for scene brightness and the environment model (automatic-lights
recommendation). Facts measured on 2026-10-19 (evidence: `cyberpunk-prius-gateway/captures/2026-10-19/CAPTURES.md`,
section "70mai dashcam").

## Access

| Item | Value |
|---|---|
| Hotspot | SSID `70mai_S410_814b`, WPA2, 2.4 GHz channel 10. The camera must be powered on (switched on by hand). |
| Addresses | Camera `192.168.0.1`, DHCP client gets `192.168.0.2/24`. The hotspot offers a default route with no internet; the client must refuse it (`ipv4.never-default yes` in NetworkManager). |
| Live stream | `rtsp://192.168.0.1/liveRTSP/av4`, RTSP over TCP, no authentication. H.264 High, 640×360, 30 fps, ≈0.7 Mbit/s. Novatek firmware, LIVE555 server. |
| Sessions | One client at a time: a second DESCRIBE while a session is open (or right after one was torn down) fails for a few seconds. Session setup ≈ 1 s. |
| Other ports | 80 (HTTP; `Config.cgi` rejects everything with `ResultCode -4444` without the app's signing), 3333 (the 70mai app's control channel, not needed). |
| Clock | The camera's burned-in clock ran ≈ 6 min ahead of the laptop on 2026-10-19. |

## Grabbing frames

```bash
# one frame
ffmpeg -rtsp_transport tcp -i rtsp://192.168.0.1/liveRTSP/av4 -frames:v 1 -y frame.jpg
# scene brightness once per second (Y average, 0..255)
ffmpeg -rtsp_transport tcp -i rtsp://192.168.0.1/liveRTSP/av4 \
  -vf "fps=1,signalstats,metadata=print:key=lavfi.signalstats.YAVG" -f null -
```

Helpers: `tools/dashcam_brightness.py` (one line per second: Y average/min/max, running median),
`tools/dashcam_sampler.py` (session per frame, a few seconds apart; writes `samples.csv`).

## First drive data (2026-10-19, garage → sunny afternoon → home)

Recording: 174 frames, 10 s apart, in `cyberpunk-prius-gateway/captures/2026-10-19/70mai/drive_home/`
with `samples.csv` (per frame: whole-frame Y avg/min/max, U/V avg; top third "sky" Y/U/V; bottom half
"road" Y) and `annotations.txt` (the driver's notes with laptop time). Garage reference with lights on:
whole-frame Y average ≈ 43 (`captures/2026-10-19/70mai/frame1_live.jpg`).

| Segment (driver's note) | n | sky Y min/med/max | road Y min/med/max | sky U / V (median) |
|---|---|---|---|---|
| garage, lights on | 13 | 98 / 108 / 178 | 51 / 61 / 80 | 128 / 127 (neutral) |
| outside, sunny | 29 | 150 / 192 / 227 | 62 / 70 / 94 | 136 / 119 (blue) |
| driving into the sun | 33 | 139 / 176 / 183 | 64 / 83 / 98 | 151 / 105 (blue) |
| sun straight in the frame | 23 | 180 / 183 / 187 | 73 / 79 / 83 | 150 / 106 |
| after wipers | 53 | 163 / 183 / 193 | 38 / 77 / 86 | 147 / 109 |
| shade | 23 | 162 / 185 / 209 | 62 / 86 / 99 | 146 / 106 |

What the data shows:

- **Auto exposure flattens absolute brightness.** Whole-frame Y is 112–123 in every outdoor segment,
  sun or shade, so average brightness is not a light meter. The distribution is: sky band versus
  road band, chroma, clipping.
- **Sky band Y separates enclosed from daylight:** garage median 108 (max 178 under ceiling lights)
  against a daylight minimum of 139 (median 176–192). The road band alone does not (garage 51–80
  overlaps daylight 62–99).
- **Sky chroma is the cleanest daylight flag:** garage sky U/V ≈ 128/127 (grey), daylight U 136–153
  and V 97–120 (blue), chroma distance 10–30 from neutral.
- **The sun in the frame does not raise the band averages** (clipped disc plus flare). Open sunny sky
  is 10–40 % clipped (share of sky pixels with Y > 250) almost all the time; the garage ≤ 4 %.

### Cheap auto-light rule, first proposal

Inputs, each median-filtered over the last 30 s: `sky_y`, `road_y`, `sky_chroma` (distance of sky U/V
from 128), `sky_clip` (fraction > 250), plus the sun elevation from clock and position.

1. Sun below −6° (civil twilight): lights on, camera ignored.
2. `sky_clip` > 5 %: do not switch ON (a darkened picture next to the sun is not trusted). As
   implemented, switching OFF stays allowed: with a full "hold while clipped" the garage's ON state
   was kept through the whole sunny drive. `LightThresholds(clip_blocks_off=True)` restores the
   full hold.
3. Lights on when `sky_y` < 120 **and** `sky_chroma` < 8 for 5 s (garage, tunnel, night, dense
   overcast at dusk); lights off when `sky_y` > 150 **or** `sky_chroma` > 12 for 20 s.
4. Rain hint (not implemented): frame-to-frame difference spikes from the wipers, or wetness texture
   on the road band.

The thresholds (120/150, 8/12) are placeholders from this one sunny drive. They are the fallback of
the environment model ([VISION_ENVIRONMENT_MODEL_2026-10-19.md](VISION_ENVIRONMENT_MODEL_2026-10-19.md)),
which replaced them as the primary input. Replay on this drive: ON from the first garage frame, OFF at
15:25:21 (13 s after the "outside" note at 15:25:08), no further switch.

## Implementation

Package `cyberpunk_computer/vision/` (pure Python + the ffmpeg/ffprobe binaries; no numpy, PIL or
OpenCV):

| Module | Content |
|---|---|
| `ffmpeg.py` | the only place ffmpeg/ffprobe run; `grab_jpeg`, `signalstats` (several crops in one ffprobe run); never raises |
| `frames.py` | `FrameSource` protocol; `DashcamStreamSource` (one long-running ffmpeg writing 1 fps to `latest.jpg` atomically, restarted with back-off), `DashcamRtspSource` (session per frame), `DirectorySource` (replay of `YYYYmmdd_HHMMSS.jpg` folders) |
| `light_features.py` | `LightFeatures` (`sky_y, sky_u, sky_v, sky_chroma, sky_clip, road_y, y_avg, t`); `sky_clip` = sky crop → `lutyuv=y='if(gt(val,250),255,0)'` → YAVG/255 |
| `scene_features.py`, `environment.py` | environment model (day / dusk_dawn / night / enclosed + weather) |
| `sun.py` | `solar_elevation(t, lat, lon)`, NOAA approximation, no refraction |
| `auto_lights.py` | `AutoLightModel` state machine, `LightThresholds`, `LightDecision`, `replay()`, CLI |

Backend: `io/dashcam_ingest.py` (`DashcamLightIngest`) runs source → features → `EnvironmentModel` →
`AutoLightModel` on its own thread and publishes `state.vision` (`enabled, camera_ok,
lights_recommended, reason, confidence, sky_y, sky_chroma, sky_clip, sun_elevation, environment,
time_of_day, enclosed, weather, environment_confidence, updated_at`). Configuration:

| Variable | Default | Meaning |
|---|---|---|
| `BACKEND_DASHCAM_URL` | empty = off | e.g. `rtsp://192.168.0.1/liveRTSP/av4` |
| `BACKEND_DASHCAM_INTERVAL_S` | 1 | sampling interval |
| `BACKEND_DASHCAM_SOURCE` | `stream` | `stream` (one ffmpeg) or `grab` (session per frame; cannot sustain 1 fps on this camera) |
| `BACKEND_HOME_LATLON` | `54.35,18.65` | sun position until a GPS fix exists in the state |

The output is a recommendation only; nothing switches the lamps. Tests: `tests/test_vision_*.py`
(fixtures: this drive's CSV and one frame with the sun in the picture).

Offline evaluation:

```bash
python -m cyberpunk_computer.vision.auto_lights --csv DIR/samples.csv        # sampler CSV
python -m cyberpunk_computer.vision.auto_lights --frames DIR                 # JPEGs, adds sky_clip
#   --latlon LAT,LON  --no-sun (camera only)  --all (every decision)
```

## Open gaps

- `/etc/prius/backend.env` as provisioned (`POCO/provision.sh`) does not set `BACKEND_DASHCAM_URL`,
  so the ingest is off in the car; the POCO joining the camera's hotspot as a secondary network is
  not configured either. The stream source has not run against the real camera.
- Not sampled yet: dusk, night, rain, overcast, tunnels. They decide the thresholds and the
  environment model's weak classes; sample with the same tools and annotate the same way.
- Which of these functions the roadsense stack (separate repos, same POCO, its own camera) covers
  is not recorded here; see [VISION_ROADMAP.md](VISION_ROADMAP.md).
