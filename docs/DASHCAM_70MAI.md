# 70mai dashcam as a camera source (verified 2026-10-19)

The 70mai dashcam in the Prius exposes a live H.264 stream over its own Wi-Fi hotspot. The car
computer can use it as a forward-looking camera (scene brightness for automatic lights, weather
hints, later maybe more). Facts measured on 2026-10-19 (evidence:
`cyberpunk-prius-gateway/captures/2026-10-19/CAPTURES.md`, section "70mai dashcam").

## Access

| Item | Value |
|---|---|
| Hotspot | SSID `70mai_S410_814b`, WPA2, 2.4 GHz channel 10. The camera must be powered on (it is switched on by hand). |
| Addresses | Camera `192.168.0.1`, DHCP client gets `192.168.0.2/24`. The hotspot offers a default route with no internet; the client should refuse it (`ipv4.never-default yes` in NetworkManager, or on Android simply keep mobile data). |
| Live stream | `rtsp://192.168.0.1/liveRTSP/av4`, RTSP over TCP, no authentication. H.264 High, 640×360, 30 fps, ≈0.7 Mbit/s. Novatek firmware, LIVE555 server. |
| Sessions | The RTSP server serves one client at a time: a second DESCRIBE while a session is open (or right after one was torn down) fails for a few seconds. One consumer in the car, with retries. |
| Other ports | 80 (HTTP; the `Config.cgi` endpoint rejects everything with `ResultCode -4444`, so no usable HTTP API without the app's signing), 3333 (the 70mai app's control channel, not needed). |

## Grabbing frames

```bash
# one frame
ffmpeg -rtsp_transport tcp -i rtsp://192.168.0.1/liveRTSP/av4 -frames:v 1 -y frame.jpg
# scene brightness once per second (Y average, 0..255), for an auto-light signal
ffmpeg -rtsp_transport tcp -i rtsp://192.168.0.1/liveRTSP/av4 \
  -vf "fps=1,signalstats,metadata=print:key=lavfi.signalstats.YAVG" -f null -
```

Helper: `tools/dashcam_brightness.py` prints one line per second with the Y average, min and max,
and keeps a running median. The 2026-10-19 reference inside an underground garage with lights on
was Y average ≈ 43 (frame `captures/2026-10-19/70mai/frame1_live.jpg`).

## Use in the computer

- The backend (on the Poco) joins the hotspot as a secondary Wi-Fi network while mobile data stays
  the internet route; the camera is only reachable while the car is on and the camera is awake.
- A `dashcam` ingest samples one frame every few seconds, publishes `scene_luma` (and later
  derived flags: dark, tunnel, rain streaks, wipers) into the vehicle state; the auto-light logic
  consumes it together with the AVC-LAN lights state.
- Thresholds have to be learned on the road: record Y average together with time of day and the
  headlamp state for a few drives before switching anything automatically.

## First drive data (2026-10-19, garage → sunny afternoon → home)

174 frames, 10 s apart, in `cyberpunk-prius-gateway/captures/2026-10-19/70mai/drive_home/` with
`samples.csv` (per frame: whole-frame Y avg/min/max, U/V avg; top third "sky" Y/U/V; bottom half
"road" Y) and `annotations.txt` (the driver's notes with laptop time). The camera's burned-in clock
runs ≈ 6 min ahead of the laptop.

| Segment (driver's note) | n | sky Y min/med/max | road Y min/med/max | sky U / V (median) |
|---|---|---|---|---|
| garage, lights on | 13 | 98 / 108 / 178 | 51 / 61 / 80 | 128 / 127 (neutral) |
| outside, sunny | 29 | 150 / 192 / 227 | 62 / 70 / 94 | 136 / 119 (blue) |
| driving into the sun | 33 | 139 / 176 / 183 | 64 / 83 / 98 | 151 / 105 (blue) |
| sun straight in the frame | 23 | 180 / 183 / 187 | 73 / 79 / 83 | 150 / 106 |
| after wipers | 53 | 163 / 183 / 193 | 38 / 77 / 86 | 147 / 109 |
| shade | 23 | 162 / 185 / 209 | 62 / 86 / 99 | 146 / 106 |

What the numbers say:

- **The camera's auto exposure flattens absolute brightness.** Whole-frame Y sits at 112–123 in
  every outdoor segment, sun or shade. Average brightness is therefore not a light meter; the
  *distribution* is: sky band versus road band, chroma, and the amount of clipping.
- **Sky band Y separates "enclosed" from "daylight" with margin:** garage median 108 (max 178 under
  ceiling lights) against daylight minimum 139 (median 176–192). Road band alone does not (garage
  51–80 overlaps daylight 62–99).
- **Sky chroma is the cleanest daylight flag:** garage sky U/V ≈ 128/127 (grey), daylight U 136–153 and
  V 97–120 (blue), chroma distance 10–30 from neutral. Overcast days will sit between; not sampled yet.
- **The sun in the frame does not raise the band averages** (clipped disc plus flare), so a
  "clipped fraction" of the sky band (share of pixels above 250) is needed to recognise it and to
  hold the previous decision instead of reacting.
- Not yet sampled: dusk, night, rain, overcast, tunnels. Those decide the thresholds; sample them
  with the same tool and annotate the same way.

### Cheap auto-light rule, first proposal

Inputs every 5 s, each median-filtered over the last 30 s: `sky_y`, `road_y`, `sky_chroma`
(distance of sky U/V from 128), `sky_clip` (fraction > 250). Plus the sun elevation from clock and
GPS, which is free and robust.

1. Sun below −6° (civil twilight): lights on, camera ignored.
2. `sky_clip` > 5 %: hold the current state (sun or strong flare in the picture).
3. Lights on when `sky_y` < 120 **and** `sky_chroma` < 8 for 5 s (garage, tunnel, night, dense
   overcast at dusk); lights off when `sky_y` > 150 **or** `sky_chroma` > 12 for 20 s.
4. Rain hint (later): periodic frame-to-frame difference spikes from the wipers, or a wetness
   texture on the road band.

The garage/daylight split above passes with these numbers; dusk and overcast values will move
the 120/150 pair, so treat them as placeholders until those drives exist.

### Beyond brightness: reading the scene

- **Classic first.** Brightness, chroma and clipping from ffmpeg cost nothing and run anywhere.
- **Small detectors on the Poco.** A nano-size YOLO (v8n/11n/12n class) at 320–416 px runs at a
  few frames per second on a Snapdragon 845 through LiteRT (TensorFlow Lite) with the GPU
  delegate or ONNX Runtime with NNAPI; the COCO classes already include `traffic light` and `stop
  sign`. For signs, fine-tuned nano models exist (a YOLO12n with 63 sign classes on Hugging Face,
  "StreetSignSenseY12n"); for traffic lights, the LISA and Bosch Small Traffic Lights datasets.
  At the stream's 640×360 a sign becomes readable only close up, so expect recognition, not
  long-range reading.
- **Labeling with a big model, running a small one.** The cheapest path to a car-specific model:
  let a vision model label a few thousand sampled frames offline (weather, tunnel, wet road,
  lights of other cars), then train a tiny classifier for the Poco.
- References found while looking: a 2025 lightweight YOLO traffic-sign detector at 50 FPS on a
  phone (Optoelectronics Letters), pruned+quantised YOLOv12-N for signs and lights (University of
  Peradeniya), Jetson benchmarks for YOLOv7-tiny (BEEI). None benchmarks the Snapdragon 845; that
  number has to be measured on the Poco.
