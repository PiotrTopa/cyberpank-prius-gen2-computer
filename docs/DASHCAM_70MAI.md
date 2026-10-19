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
