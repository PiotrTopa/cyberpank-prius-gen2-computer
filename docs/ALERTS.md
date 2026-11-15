# Driver alerts and smart functions

Code: `cyberpunk_computer/alerts/` (backend), `ui/scene/alerts.py` (MFD), `dashboard/src/tabs/AlertsTab.tsx`.
Feature list with status and data gaps: [FEATURES.md](FEATURES.md).

The system informs the driver and controls comfort functions only. Nothing in this framework acts
on steering, brakes or the drivetrain. The only actuation is the AVC-LAN climate recirculation set
(tunnel automation, whitelisted in `alerts/service.py::ACTIONS`). Nothing transmits on BEAN or CAN.

## 1. Framework

```
store (engine thread) ──middleware──▶ markers (fuel level seen, 21C3 updates), MFD acknowledge
roadsense scene feed (own thread) ──on_scene──▶ latest scene (the mapping the MFD gets)
engine loop ── AlertService.tick() ≤ 10 Hz ──▶ build_inputs ─▶ AlertManager.tick ─▶ functions
       │
       ├─ event "alert" (ZMQ + websocket) → MFD banner
       ├─ alerts_log.jsonl (rotated 4 MB × 3) + metrics `events` rows `alert.<event>.<id>`
       ├─ AVC-LAN climate set (automation, mode on only)
       ├─ SoundSink.play(sound_id) (null implementation)
       └─ NotificationSink.notify(entry) (null implementation; parked-car events)
```

| Part | File | Role |
|---|---|---|
| types | `alerts/model.py` | `AlertSpec` (id, text, severity, priority, icon, sound id, kind, timing, gating, `enable_check`), `Candidate` (text, event key, evidence), `AlertFunction`, `EvalContext` (`mode`, `actuate`, `note`, `stat`) |
| inputs | `alerts/inputs.py` | `AlertInputs`: the only place that names state fields and scene keys; source table in its docstring |
| manager | `alerts/manager.py` | modes, gating, debounce, latch/re-arm, cooldown, max per drive, acknowledge, arbitration, drives, publishing |
| persistence | `alerts/persist.py` | `alerts_settings.json` (modes, counts, function memory), `alerts_log.jsonl` + writer thread |
| sinks | `alerts/sinks.py` | `SoundSink` / `NotificationSink` interfaces, null implementations, sound ids |
| tunables | `alerts/tuning.py` | every threshold of every function; JSON override `alerts_tuning.json` |
| backend glue | `alerts/service.py` | wiring, markers, actuator whitelist, metrics events |
| REST | `alerts/routes.py` | `/api/v1/alerts` |
| replay | `alerts/replay.py` | the alert set over a recorded trip |

### 1.1 Modes

| Mode | Evaluated | Logged (with evidence) | MFD | Sound | Actuation / push |
|---|---|---|---|---|---|
| `off` | no | no | no | no | no |
| `shadow` (default for every new type) | yes | yes | no | no | no (automation: "would press") |
| `on` | yes | yes | yes (kind `alert`) | yes, when a sink exists | yes (automation / event) |

`enable_check`: a type whose required input is unavailable refuses `on` (the REST call answers 409
with the reason; a stored `on` acts as `shadow`). Used by `lane_departure` (turn signal) and
`green_light` (traffic-light reliability).

### 1.2 Rules per type (manager)

1. gating: true speed range, gears (`DRIVE`, `B`), READY / IG, or car off (parked events);
2. debounce: the condition holds `debounce_s`;
3. latch: after a fire the type waits for the condition to be absent `clear_after_s`, or for a new
   event key (e.g. a new limit segment);
4. cooldown after a fire, `max_per_drive`;
5. acknowledge: tap = that event key is done; long press = the type is muted for the rest of the
   drive (a drive starts when READY/IG returns after ≥ 60 s);
6. arbitration (mode `on`): one alert shown: highest severity, then priority, then newest; a shown
   alert stays `min_show_s` unless a higher severity pre-empts it; clears when the condition is gone,
   after `max_show_s` or on acknowledge; a pre-empted alert comes back while its condition holds.

### 1.3 Log entry (shadow review)

```json
{"ts": 1794665144.2, "seq": 17, "event": "fire", "id": "speed_limit", "kind": "alert", "mode": "shadow",
 "drive": 3, "text": "LIMIT 50  REAL 62", "key": "seg4", "severity": "notice",
 "evidence": {"limit": 50, "src": "sign", "conf": 0.8, "agree": "agree", "map_layer": true,
              "real_kmh": 62.1, "display_kmh": 69, "since_change_m": 412, "tolerance_kmh": 6.0,
              "delay_s": 5.0, "bar": "strong", "over_s": 5.0},
 "inputs": {"real_kmh": 62.1, "display_kmh": 69, "gear": "DRIVE", "ready": true, "brake": false,
            "steer": 1.5, "outside_c": 7.0, "scene_age": 0.08, "limit": {...}, "lead": {...}}}
```

Events: `fire`, `show`, `clear` (why: condition / timeout / ack / mode / gated), `ack`, `action`
(automation: `executed` true/false), `note` (e.g. "stepped back"), `stat` (braking score per stop),
`mode`, `drive`.

### 1.4 MFD (event `alert`)

Contract in `ui/scene/alerts.py`. The banner takes the status-strip row (194 × 21, page dots stay):
1 px frame and text in the severity colour (info cyan, notice amber, warning red), a drawn 1-bit
15 × 15 icon, Terminus 14, `ADVISORY` tag for advisory types. No blinking. A `REAL nn` chip under the
limit sign while the speed-limit notice is shown. The backend re-sends the shown alert every 2 s; the
frontend drops it after 5 s without a refresh. Tap = acknowledge, long press = mute for the drive
(`input_event` `mfd`/`alert_ack`). Frame pacing: `ScenePage.needs_frame` reports banner and chip
changes. Shown on the main screen (all pages); other screens do not show it yet.

Screenshots: `python -m cyberpunk_computer.ui.scene.alerts_demo --shots OUT --copy-to /home/piotr/filedrop/uploads`
(prefix `alerts_`).

### 1.5 Sound and notifications

`SoundSink.play(sound_id, severity, alert_id) -> bool` and `stop(alert_id)`, non-blocking, engine
thread. Sound ids: `soft`, `notice`, `warning` (abstract cues). Only `NullSoundSink` exists: the MFD
Pi has no audio, the head-unit agent is not built, the meter buzzer (BEAN 62:6E) is on the deny list.
Candidates for a real sink: the head-unit agent (Android `SoundPool`/TTS with music ducking), or a
piezo on a satellite / spare powerbox output.

`NotificationSink.notify(entry) -> bool`, same rules. Only `NullNotificationSink` exists.
Recommended channel: an outbound HTTP push from the phone over LTE to a self-hosted topic (ntfy-type
server on the WireGuard hub) that the owner's phone subscribes to; it works with the laptop off. The
laptop's WhatsApp bridge is reachable only while the laptop runs: at most a secondary sink.

### 1.6 REST (authenticated like every `/api/v1` route)

| Route | Content |
|---|---|
| `GET /api/v1/alerts` | types (mode, effective mode, `on_blocked`, counts, last entry), shown alert, function status, sinks |
| `GET /api/v1/alerts/log?limit&id&event` | recent entries, newest first |
| `GET /api/v1/alerts/log/export?since=` | the whole log as JSON lines (download) |
| `POST /api/v1/alerts/{id}/mode` | `{"mode": "off|shadow|on"}`; 409 with the reason when `on` is refused |
| `POST /api/v1/alerts/ack` | `{"id"?, "long"?}` |
| `POST /api/v1/alerts/service/{item}/done` | `{"km"?}` (default: odometer now) |

Dashboard: tab ALERTS.

### 1.7 Environment

`BACKEND_ALERTS=0` disables it; `BACKEND_ALERTS_DIR` (default `alerts/` next to the metrics
database); `BACKEND_ALERTS_TUNING` (a JSON file); `BACKEND_ALERTS_HZ` (1..20, default 10).

### 1.8 Replay

```
python -m cyberpunk_computer.alerts.replay TRIP [--roadsense WS.jsonl.gz --rs-offset S] [--modes configured --settings DIR] [--json OUT]
```

TRIP = a backend trip recording (`*.ndjson[.gz]`) or a decoded-frame CSV (`t,bus,id,data`). Frames
go through the real ingress into a Store; a roadsense `/ws` recording goes through the backend's
`SceneBuilder`. All types are evaluated as shadow by default. Output: one line per would-be fire,
action, note and stat, then a summary. On `tests/fixtures/fuel_drive_2026-11-13.csv.gz`: 5 stops
found, nothing fires (no limit, lead, map or outside temperature in that recording), braking share
null (21C3 not in it).

## 2. Functions (all `shadow` by default; `lane_departure` `off` and not enableable)

| Type | Function file | Kind, severity | Trigger (defaults) | Inputs (verified in code) | Gating / suppression |
|---|---|---|---|---|---|
| `speed_limit` | `speed_limit.py` | alert, notice, sound `notice` | true speed > limit + 6 km/h (10 % above 100) for 5 s | `vehicle.real_speed_kmh` (CAN 0x0B4); scene `map.limit` (fused, map usable and not synthetic) else scene `limit` (camera) | ≥ 20 km/h, D/B; weak sources (`map_implicit`, `vision_default`, `default`): +15 km/h for 10 s, conf ≥ 0.6; `conflict` / unknown: never; first 150 m after a change: never; re-arm after 3 s at/below the limit |
| `lead_departure` | `stop_go.py` | alert, notice | car stands ≥ 2 s behind a standing lead ≤ 10 m that moves off | roadsense's tracked lead (`departed` cue): scene `lead` with `lead_src: "tracked"` (topic `lead`; `lead_departed` when the lead was lost after pulling away), else `map.lead` (its copy in `fused`), else the vision scene `lead` (prob ≥ 0.6, +3.5 m for 1 s) | READY, D/B, once per stop; a vanishing lead never fires |
| `green_light` | `stop_go.py` | alert, info | ego light red → green while standing, no lead ≤ 15 m for 2 s | scene `light` (`state`, `rel`, `agree`) | **cannot be on** (traffic-light reliability unvalidated) |
| `headway_coach` | `headway.py` | alert, notice, advisory | time gap < 1.0 s for 5 s | lead `d` (minus 1.8 m camera-to-bumper), true speed, steering | ≥ 50 km/h, lead ≤ 1.3 m lateral, steering ≤ 25°; 2 min cooldown, 6 per drive |
| `distance_warning` | `headway.py` | alert, warning, advisory | TTC < 2.0 s with ≥ 15 km/h closing | lead `d`, `v_rel`, true speed, brake (`brake_switch` / `brake_pressed`), steering | 30..150 km/h, lead prob ≥ 0.8 (vision), ≤ 1 m lateral, not braking, steering ≤ 25°, scene ≤ 0.6 s old; 10 s cooldown |
| `curve_speed` | `curve.py` | alert, notice | v > sqrt(2.5 m/s² · r) + 5 km/h, curve start in 3..9 s | scene `map.ahead.curve` (`m`, `r`), map conf ≥ 0.9, envd weather (2.0 m/s² wet) | inactive without a matched map; not built-up; not when a junction is within 40 m of the curve start; once per curve |
| `tunnel_recirc` | `tunnel.py` | automation | tunnel entry (map: in / portal ≤ 120 m, conf ≥ 0.8, 1 s; vision: 3 s) | scene `map.tunnel`; `climate.*` (AVC E0 5D F3); outside temp; BEAN FE:FB | only from FRESH with the climate on; not with front defrost, not cold with A/C off; restore 20 s after the exit if untouched; steps back on any driver touch; no feedback in 6 s → step back; 30 s between presses, 12/h, 10 min cap |
| `eco_lift` | `eco.py` | alert, info, silent | target in [glide, glide + v·4.5 s], glide = (v² − vt²)/(2·0.7) | scene `map.ahead` (limit, roundabout, stop/give-way), camera `signs.ahead` without the map, throttle (CAN 0x244) | ≥ 35 km/h, ≥ 15 km/h above the target, accelerator pressed; once per target |
| braking score (stat) | `eco.py` | `stat eco_stop` | per stop from > 20 to < 3 km/h | 21C3 regen torque actual, master-cylinder torque (1 Hz), brake | relative figure (units unverified) |
| `ice_risk` | `winter.py` | alert, notice, sound `soft` | ≤ 3 °C at drive start (after 30 s), or falling to ≤ 3 °C for 60 s after ≥ 5 °C | `climate.outside_temp` (AVC E0 5D F7 / BEAN FE:CD, passive) | READY; 2 per drive |
| `rear_defog_hint` | `winter.py` | alert, info, silent | first 5 min, ≤ 3 °C, rear defogger off | outside temp, `climate.rear_defogger` (AVC F3) | once per drive |
| `service_due` | `maintenance.py` | alert, info | an item within 500 km / 14 days or overdue | BEAN `odometer_km` (52:3F); done records from the dashboard | drive start, < 10 km/h, once per drive; items never recorded are "not set" |
| `range_low` | `maintenance.py` | alert, notice | range < 60 km | `vehicle.fuel_level` (0x5A4, only once a real value arrived); own L/100 km over the last 300 km, else meter 52:A2 | petrol only (LPG not visible); once per drive |
| `new_dtc` | `maintenance.py` | alert, notice | a stored DTC not known before | `diagnostics.stored_dtcs`, `diagnostics.ecus[*].dtcs` (existing scans only) | first observation = baseline; shown at < 10 km/h; known after 30 s at a standstill |
| `left_unlocked`, `door_open_parked`, `lamps_left_on`, `low_12v`, `intrusion` | `guardian.py` | event | 5 min unlocked / 2 min door open / 1 min lamps / < 12.2 V for 10 min from 30 min / door opened while locked with no unlock in 60 s | BEAN FE:B6, FE:7F, FE:E1; INA219 | car off; once per parking; no polling |
| `lane_departure` | `lane.py` | alert, warning | wheel < 0.15 m from an ego line, approaching ≥ 0.25 m/s | scene `lanes[1]`, `lanes[2]`; `turn_signal` (unknown) | ≥ 60 km/h, steering ≤ 30°; **off, cannot be on** until the turn signal is available |

### Not validated without real data

* Speed limit: the fused limit's real accuracy (no GNSS on the car yet; camera engine only); the
  owner's tolerance preference.
* Lead departure / distance: modeld lead behaviour at a standstill and in cut-ins on this car; the
  1.8 m camera-to-bumper offset is an estimate; roadsense's tracked lead (topic `lead`, roadsense
  2026-11-15) is consumed when roadsense sends it (headway reads its distance and closing speed; time gap
  and TTC are still computed here from the true speed and the camera-to-bumper offset); not checked on the car.
* Curve and eco: only on the synthetic Opole drive (real mapd): every curve seen there starts at a
  junction, so no curve notice fired; eco targets were too close at the test speed. Needs a GNSS
  source and real drives.
* Tunnel: whether our AVC press shows up on BEAN FE:FB (the 3 s grace assumes it may); whether the
  ECU in AUTO switches the intake itself (would look like a driver touch: the automation steps back,
  the safe side).
* Braking score: 21C3 units and the meaning of the master-cylinder value on this car.
* Winter: heat soak of the bumper sensor after parking.
* Guardian: BEAN frames while parked for a long time (the bus sleeps; last values are kept);
  INA219 includes the computer's own draw.

## 3. Deploy (prepared, not executed)

Everything arrives in shadow (no settings file → every type at its default: shadow, `lane_departure`
off). Nothing is shown, sounded or pressed until the owner switches a type on.

1. Merge `feat/alerts` into `main` (conflict-prone files: `backend/service.py`, `api/app.py`,
   `api/server.py`, `ui/scene/page.py`, `ui/screens/main_screen.py`, `core/app.py`,
   `backend/roadsense_scene.py`, `dashboard/src/App.tsx`, `dashboard/src/types.ts`,
   `dashboard/package.json`: each change is a small separated block).
2. Backend (phone): the usual sync/deploy of `cyberpunk_computer/` (DEPLOYMENT.md), then restart
   `prius-backend.service` at a parked moment. Check: journal line `alert framework: 19 types (...)`,
   `GET /api/v1/alerts` (all `shadow`, `lane_departure` `off`), `alerts/` next to the metrics DB.
3. Dashboard: `npm run build` and deploy `dashboard/dist` with the backend.
4. MFD Pi: deploy the frontend (`ui/scene/alerts.py`, `page.py`, `main_screen.py`, `core/app.py`)
   and restart `mfd.service` when parked; nothing changes on the screen while every type is shadow.
5. After drives: review in the ALERTS tab or `GET /api/v1/alerts/log/export`; offline:
   `python -m cyberpunk_computer.alerts.replay <trip recording> --roadsense <ws recording>`;
   tune `alerts_tuning.json`; switch types on one by one.
