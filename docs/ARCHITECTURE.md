# Architecture

System topology, processes, transports and the UI input model. The state store, actions, rules
and IO ports are in [VIRTUAL_TWIN_ARCHITECTURE.md](VIRTUAL_TWIN_ARCHITECTURE.md); the POCO's OS
side in [POCO/ARCHITECTURE.md](../POCO/ARCHITECTURE.md).

## 1. Processes and transports

| Process | Runs on | Entry point | Role |
|---|---|---|---|
| backend | POCO F1 (`prius-backend.service`) | `python -m cyberpunk_computer.backend` | owns the hardware links and the virtual twin; no pygame |
| frontend | MFD Pi Zero v1.3 (`mfd.service`) | `python -m cyberpunk_computer.frontend --host 192.168.100.1` | pygame UI; `RemoteTwin` stands in for the local twin |
| standalone app | any desktop | `python -m cyberpunk_computer` | same UI with a local `VirtualTwin` (file replay, mock, or a gateway on a local serial port) |
| dashboard | browser | `dashboard/dist`, served by the backend at `/` | web UI over REST + WebSocket ([dashboard/README.md](../dashboard/README.md)) |

The UI code is identical in the frontend and the standalone app; only the data source differs.

### Backend threads (`backend/service.py`)

| Thread | Work |
|---|---|
| main (engine) | `vt.update()` + `bridge.drain_commands()` at `BACKEND_TICK_HZ` (default 50); the only thread that dispatches UI/API commands |
| metrics | `MetricsSink`: samples store snapshots into SQLite (`BACKEND_DB`) |
| battery-health | `battery/service.py`: per-ride and per-rest battery records into `battery.db`, nice +10 ([BATTERY_HEALTH.md](BATTERY_HEALTH.md)) |
| api-server | uvicorn/FastAPI: REST + WebSocket |
| zmq | `ZmqServer` socket IO |
| panda reader | broadcast CAN frames → `IngressController.ingest_broadcast_can` → `Store.dispatch` (the store holds a re-entrant lock for this path); with the position feeder on, `CanMotionTap.on_frame` sees each frame first |
| roadsense-scene | roadsense `/ws` reader → ZMQ event `roadsense_scene` (`backend/roadsense_scene.py`) |
| position-feeder | only with `BACKEND_POSITION_FEEDER=1`: CAN motion, driver controls, body signals + GNSS → chris-integrator `/run/chris/position.sock` (legacy mapd `/run/roadsense/position.sock` as the fallback; `backend/position_feeder.py`, [POSITION_FEEDER.md](POSITION_FEEDER.md)) |
| headunit-adb | only with `BACKEND_HEADUNIT=1`: head unit volume / mute / media keys / alert sounds over the phone's adb server (smart-socket client, never the `adb` binary; `backend/headunit.py`, [HEADUNIT_INTEGRATION.md](HEADUNIT_INTEGRATION.md) §8.3) |

`api/bridge.py::StoreBridge` is the boundary: API/ZMQ commands are queued and dispatched on the
engine thread; snapshots go out latest-only so a slow client never blocks the engine.

### Network interfaces of the backend

| Interface | Port | Content |
|---|---|---|
| REST | 8080 | `GET /health`; under `/api/v1`: `state`, `metrics/catalog`, `metrics`, `events`, `commands` (list), `POST commands/{name}`, `bean/frames`, `diag/*`, `battery/*` ([BATTERY_HEALTH.md](BATTERY_HEALTH.md)), `camera/preview*`, `position` + `POST position/gnss` (feeder on only), `alerts` ([ALERTS.md](ALERTS.md)), roadsense proxy; `/roadsense/` (roadsense status page), `/login`, `/logout`, `/auth/*` (sessions, pairing). Auth with `BACKEND_AUTH_TOKEN` set: loopback trusted, else bearer token or session cookie ([AUTH.md](AUTH.md)) |
| WebSocket | 8080 `/api/v1/stream` | state snapshots (dashboard); same auth as REST |
| ZMQ PUB | 8081 | `{"type":"state","ts","state"}` (latest-only, throttled) and `{"type":"event","ts","name","data"}` (FIFO, never displaced by state) |
| ZMQ REP | 8082 (no auth; firewalled to lo/usb*/wg*) | `{"command": name, "params": {...}}` → `{"status":"ok"}` on acceptance (execution is asynchronous) or `{"status":"error","reason"}` |

The frontend (`frontend/client.py`) subscribes to PUB, sends commands over REQ (socket rebuilt after a
lost reply) and treats the link as up only while envelopes arrive within 5 s (state is published at
about 1 Hz).

### Hardware links of the backend

| Device | Link | Code | Notes |
|---|---|---|---|
| Gateway (RP2040) | USB CDC NDJSON, 1 Mbaud | `io/serial_io.py` | AVC-LAN, solicited CAN, RS485; [PROTOCOL.md](PROTOCOL.md) |
| Powerbox (RP2040) | USB CDC NDJSON | `io/powerbox.py`, `io/multi_input.py` | device ids offset into 200-203; [powerbox/README.md](../powerbox/README.md) |
| comma.ai panda | libusb-1.0 via ctypes (`bbaa:ddcc`) | `io/panda_can.py` | broadcast CAN only, see §3 |
| MFD Pi | USB gadget `usb0`, ZMQ | `backend/mfd_power.py` | the backend powers the Pi via the hub port (uhubctl) and manages its boot/shutdown (`BACKEND_MFD_*`) |
| roadsense + chris-integrator | systemd `roadsense.target` + HTTP :8090 (chris-integrator's consolidated API since L44; roadsense's debug face :8091) | `backend/roadsense_power.py`, `api/roadsense_proxy.py` | started/stopped on ACC; the camera hub (relay ch4) is powered already at the early start demand (BEAN: doors unlocked) for at most `BACKEND_ROADSENSE_EARLY_CAMERA_S` (180 s), without the stack: the dashcam leaves the USB bus once 23-39 s after power-up, for 4-5 s or, on the first drive after a long stand, for 122 s (journals of 2026-11-16 / 17: no road picture from 0:45 to 2:55 after ACC on), and that sequence should run before the drive. Sessions, tail, budget per parked period and the hand-over to ACC: [CAMERA_WARMUP.md](CAMERA_WARMUP.md) |
| chris-integrator (vehicle input) | Unix socket `/run/chris/position.sock` (vehicle input API 1.1; legacy mapd `/run/roadsense/position.sock` v1/1.1 after a rollback) | `backend/position_feeder.py`, `backend/position_client.py` | off by default; [POSITION_FEEDER.md](POSITION_FEEDER.md), [lanes/L44-chris-repoint.md](lanes/L44-chris-repoint.md) |
| satellites | RS485 (gateway), BLE (`prius-bled`), Wi-Fi | `backend/satellite_registry.py`, `io/*_transport.py` | [SATELLITES.md](SATELLITES.md) |

Gateway and powerbox are told apart by `{"id":0,"d":{"a":"whoami"}}` → `IDENT` with `role`
(`io/discovery.py`), because both use ids 1 and 2 for different buses.

All ages in the backend use `time.monotonic()`; wall-clock stamps in the state are for display only
(the POCO's RTC is not persisted and NTP can step the clock by hours at boot).

## 2. Input abstraction

The UI responds to five logical events (`input/manager.py::InputEvent`): `ROTATE_LEFT`,
`ROTATE_RIGHT`, `PRESS_LIGHT`, `PRESS_STRONG`, `BACK`. Dev keyboard: ← / → (and mouse wheel) rotate,
Enter light press, Space strong press, Escape back.

### Pointer model (mouse and touch)

Mouse (dev) and the MFD touch panel (production) drive one gesture recogniser,
`ui/pointer.py::PointerController`, in native 480×240 coordinates. It only knows
`down(x, y)` / `move(x, y)` / `up(x, y)` plus a per-frame `update()`:

| Gesture | Rule | Default semantics |
|---------|------|-------------------|
| click | down + up on the same target, ≤ 10 px movement, release within 8 px of its rect | focus + `PRESS_LIGHT` |
| long press | held still ≥ 600 ms (fires while still down; the release then does nothing) | focus + `PRESS_STRONG` |
| drag | target is `draggable` (slider) or claims the move (`on_pointer_move` → True) | target-specific; never clicks |
| slide off | moved beyond the slop on a non-drag target | cancelled, no click |

Targets:

- **Widgets** (`ui/widgets/base.py`): `pointer_enabled` (default = focusable),
  `draggable`, `hit_test`, `pointer_target_at` (containers return the deepest
  pointer-enabled child — `Frame` lets a slider child win), and hooks
  `on_pointer_down/move/up`, `on_click`, `on_long_press`. Returning False from
  `on_click`/`on_long_press` selects the default semantics above.
- **`PointerZone`**: rect + callbacks for custom-drawn screens (list rows,
  footer buttons, swipe areas, the `< BACK` chip). Zones are hit-tested before
  widgets; `Screen.setup_pointer()` registers them lazily on first use.
- **`Screen`** (`ui/screens/base.py`): `pointer_target_at`, `pointer_click`,
  `pointer_long_press`; the defaults focus the widget and call
  `handle_input(PRESS_LIGHT/STRONG)`, i.e. the same code path as the keyboard/encoder,
  so edit modes, timeouts and activity tracking behave identically.
  `pointer_helpers.MenuListZone` maps rows onto `_selected_index` + input
  events (tap row = select + PRESS_LIGHT; on the row being edited: `>` end =
  ROTATE_RIGHT, value area = ROTATE_LEFT, label = done; sideways drag = one
  rotate per 16 px). A `< BACK` chip is added only where a screen had no
  tappable way back; BACK goes through `Application.inject_input` like Escape.

Pressed feedback is a 1 px outline in `COLORS["active"]` drawn by the app on a
single pre-allocated `pygame.Rect`; the idle per-frame cost is a few
comparisons (the Pi budget is ~21 fps on one core).

Sources:

- *Dev mouse* — `core/app.py::_process_events` maps left-button
  `MOUSEBUTTONDOWN/MOTION/UP` from window to native coordinates (scale-aware:
  `--scale 2/4`, fullscreen).
- *Production touch* — the backend publishes on the ZMQ event channel
  (`frontend/client.py` → `RemoteTwin.add_event_callback`). The callback only
  appends to `TouchEventQueue` (a deque) on the client thread; the main loop
  drains it once per frame and feeds the same controller. Nothing depends on
  SDL mouse (`SDL_VIDEODRIVER=dummy`, `SDL_NOMOUSE=1`).

Touch contract (backend → frontend; origin and calibration in [AVC_BACKEND.md](AVC_BACKEND.md)):

```text
"touch"  {"x": 0..479, "y": 0..239, "pressed": bool, "raw_x": int, "raw_y": int, "t": float}
         pressed=true streamed at 10–15 Hz while the finger is down (first = down,
         then moves); pressed=false = release with the last known x/y.
         Coordinates are calibrated to the native surface by the backend.
"button" {"key": "menu"|"dest"|"map"|"maintenance"|"calendar"|"language",
          "pressed": bool, "t": float}
```

A touch press whose frames stop for 1 s is cancelled (never clicked); release
frames without a preceding press are ignored. Buttons act on the press edge
via the table in `ui/hw_buttons.py` (each target first unwinds to the home
dashboard):

| key | target |
|-----|--------|
| `menu` | home dashboard, page 1, edits cancelled |
| `map` | dashboard DYNAMICS page (pressed again there → page 1) |
| `dest` | ENGINE / DIAGNOSTICS menu |
| `maintenance` | DIAGNOSTICS / DTC |
| `calendar`, `language` | logged, no-op |

### Encoder configuration (not wired)

`input/manager.py::EncoderConfig` (`mode` smooth/stepped, `min`, `max`, `step`, `detents`, `current`)
and presets per widget kind (`volume`, `menu`, `mode_selector`, `temperature`) are kept by the
`InputManager`, but nothing sends them to a device: no rotary encoder is connected.

## 3. CAN ingress: two flows

The vehicle CAN reaches the backend through two devices on the CAN hub (powerbox relay ch3):

| Flow | Device | Traffic | Path into the state |
|---|---|---|---|
| Broadcast | comma.ai panda (`io/panda_can.py`) | every broadcast frame (0x03B battery, 0x3CB SOC, 0x0B4 speed, 0x120 gear, steering, wheel speeds, 0x3CF air temperatures, …; ids and scaling in [prius_can.md](prius_can.md)) | panda reader thread → `IngressController.ingest_broadcast_can` → `CANDecoder` → `Store.dispatch`; no input queue, no engine tick |
| Solicited | RP2040 gateway (`io/serial_io.py`) | periodic OBD/Toyota subscriptions, one-shot requests, DTC scans | serial reader → `RawMessage` queue → engine tick → `IngressController._handle_can_message` |

Gateway firmware 3.x forwards no passive frames unless `sniff` is on, and `sniff` disables
`req`/`sub`/`tx` ([PROTOCOL.md](PROTOCOL.md)), so one device cannot do both. Rules for the panda:

- always **SAFETY_SILENT** (bus monitor: no ACK, no TX); the backend never writes to its CAN TX endpoint.
  The gateway is the only device that transmits on the car's CAN, so polling belongs to it alone;
- filters on the reader thread (the bus carries about 1650 frames/s; a state update costs about
  0.07 ms on the laptop, and the phone managed about 500 a second before it fell behind): an identical payload repeated within 0.25 s is dropped; a fast id updates the state at
  most every 0.1 s (`panda_dispatch_min_s`; every changed frame still reaches the trip log and the
  battery collector); an id the decoder does not know is logged once a second and not decoded; a bulk
  transfer of more than 64 frames is a backlog and is reduced to the newest frame per id (ids from
  0x700 up are all kept). The position feeder's tap sees every frame. Journal line
  `Panda reader: …` once a minute: frames/s, busy %, largest batch, coalesced, dispatched/s (first
  drive with these filters, 2026-11-17: ~1690 frames/s at ~20 % busy);
- heartbeat every second (the panda firmware power-saves its transceivers 2 s after the heartbeat stops);
- without the panda: no broadcast values, solicited data unaffected; `connection.panda_*` in the state
  shows link, firmware, frame rate and per-bus counts. `--no-panda` / `BACKEND_PANDA_ENABLED=0` disables it.

Solicited setup lifecycle (`io/factory.py::VirtualTwin`): the CAN mode and subscriptions are
(re)sent on every gateway serial (re)connect, on the `GATEWAY_READY` banner, on a `GW_HB` uptime
regression (reboot with the banner missed), and by a watchdog while the gateway heartbeats but no
subscription answers (15 s, 30 s → 300 s backoff).

### Solicited subscriptions (`comm/subscription_groups.py`)

Single source of the backend's periodic PIDs. All groups are enabled by default; all except `core`
can be switched off on the MFD Data Sources screen. Requests go to physical ECU ids (response id =
request + 8).

| Group | Slot | Item | ECU | Request | Interval ms | Timeout ms / ISO-TP | Why this rate |
|---|---|---|---|---|---|---|---|
| core | 0 | engine RPM | 0x7E0 | `01 0C` | 500 | 200 | suspended while BEAN `FE:26` or CAN 0x348 / 0x3C8 is fresh |
| core | 1 | engine load | 0x7E0 | `01 04` | 5000 | 200 | no broadcast source |
| core | 2 | hybrid comprehensive | 0x7E2 | `21 C3` | 2000 | 500 / yes | inverter and motor temperatures, torques (its MG2 rpm, HV voltage and current are used only while CAN 0x244 / 0x03B are stale) |
| core | 6 | cruise control | 0x7E2 | `21 D3` | 1000 | 200 | cruise set speed, resume memory and stalk switches exist nowhere else; engaged and main switch come first from CAN 0x120 / 0x5C8 |
| battery_cells | 3 | block voltages | 0x7E3 | `21 CE` | 2000 | 500 / yes | block-resistance pairs must be ≤ 3.5 s apart ([BATTERY_HEALTH.md](BATTERY_HEALTH.md)) |
| battery_cells | 4 | battery temps / fan / delta SOC | 0x7E3 | `21 CF` | 10000 | 500 / yes | slow values; 0x3CB carries two temperatures at 0.1 s |
| battery_cells | 5 | block resistances | 0x7E3 | `21 D0` | 30000 | 500 / yes | ECU's own slow figures |
| hybrid_extended | 8 | hybrid additional | 0x7E2 | `21 C4` | 5000 | 500 / yes | converter temperature, A/C power |
| engine_sensors | 9 | intake air temp | 0x7E0 | `01 0F` | 30000 | 200 | suspended while CAN 0x3CF C is fresh |
| engine_sensors | 10 / 11 | MAF / lambda | 0x7E0 | `01 10` / `01 24` | 5000 | 200 | no broadcast source |
| aux_battery | 7 | aux voltage | 0x7E0 | `01 42` | 30000 | 200 | the powerbox measures the 12 V rail every second |
| environment | 13 | barometric pressure | 0x7E0 | `01 33` | 60000 | 200 | |
| environment | 14 | ambient air temp | 0x7E0 | `01 46` | 60000 | 200 | suspended while CAN 0x3CF B is fresh (above 0 °C) |
| odometer | 12 | distance since DTC clear | 0x7E0 | `01 31` | 60000 | 200 | |

Transmit budget: each request is one frame from the gateway onto the powertrain CAN, each ISO-TP
reply costs one more (flow control). With the broadcast sources alive: 3.0 requests/s + 1.3 flow
controls/s = 4.3 frames/s (8.6 until 2026-11-16). `tests/test_poll_budget.py` holds the budget.
A slot that exists for one broadcast-covered value is unsubscribed after the broadcast has been fresh
for 10 s and re-subscribed when it goes stale (`comm/source_priority.py`, `SlotSuspender`).
`BACKEND_OBD_PAD` (unset by default) sets the padding byte of the requests
([prius_can.md](prius_can.md) §9).

Audit against the 2026-11-17 findings (gateway fw 3.2.11): request ids, response ids, ISO-TP flags
(every multi-frame PID sets `isotp`; `21D3`, `01xx` are single frames) and rates match the table;
no slot polls a value that is broadcast except as a fallback. Two backend fixes came out of it:
single-frame replies are now checked for an ISO-TP single-frame PCI (the panda also delivers the
first and consecutive frames of every multi-frame answer; a consecutive frame starting `21 61 C3`
used to decode as a 21C3 reply), and the 21C3 MG2 rpm, HV voltage and current no longer overwrite
the broadcast values every 2 s (source policies `mg2_rpm`, `hv_voltage`, `hv_current`).

Source order of the broadcast signals with more than one source (`comm/source_priority.py`):
speed 0x0B4 → 0x3CA → wheel mean (2.2 % low) → OBD; rpm `FE:26` → 0x348 → 0x3C8 → OBD 010C;
coolant 0x039 → `FE:2C` → 0x484 → OBD; brake switch 0x230 D → `FE:29`; cruise main 0x5C8 → `62:C8`
→ `21D3`; cruise engaged 0x120 → `21D3`. Yaw rate, accelerations, distance and our odometer:
`io/dynamics_ingest.py` ([prius_can.md](prius_can.md) §2).

Trip recording (`io/trip_recorder.py`, `io/comm_logger.py`): a trip file is always a new file (opened
exclusively; a taken name gets a suffix) whose header carries the kernel boot id and the monotonic
clock. The phone has no RTC: after a boot the wall clock can be a day old until NTP steps it. A step
of more than 5 s while a trip is open closes it, rewrites its pre-step wall times by the step and
renames it after its corrected start; the next traffic opens a correctly named file. A hard reset can
leave a NUL-filled tail and no `end` record: the readers (replay, battery backfill, alert replay, BEAN
tools) skip NUL bytes and lines that are not JSON.

Responses are decoded by `comm/can_decoder.py` (21C3 → `mg1/mg2_inverter_temp`, `mg1/mg2_motor_temp`,
`inverter_temp` = MG2; 21CE → `battery_soc_21ce`, `battery_current_21ce`, 14 `block_voltages` with
min/max/delta; 21CF → `delta_soc`, `battery_air_intake_temp`, charge/discharge limits, fan speed).
PID scaling: [prius_can.md](prius_can.md) sections 5–7. `comm/solicited_can.py` holds the
`PIDDefinition`s and `ECUAddress` table; its `PROFILE_*` lists are not used by the backend.

## 4. UI

- Renders at 480×240 into one surface; `--scale 2/4` upscales for the desktop window
  (`core/renderer.py`). On the Pi the surface is written to the firmware framebuffer `/dev/fb0`
  (`core/framebuffer.py`, SDL dummy driver).
- Screen stack in `core/app.py` (`push_screen` / `pop_screen`); focus in `ui/focus.py::FocusManager`.
  Screens in `ui/screens/`, widgets in `ui/widgets/`; the home dashboard layout is documented in
  `ui/screens/main_screen.py`.
- Colours: themes in `ui/colors.py` (VFD, amber, synthwave palettes). Fonts and the 1-bit rendering
  rule for the analog MFD link: `ui/fonts.py`, [assets/fonts/README.md](../assets/fonts/README.md).

## 5. VFD satellite (device 110)

The VFD (Noritake, 256×48) is a separate application (`vfd_satellite/`) fed by `VFDDisplayRule`
(normalises vehicle state into `VFDSatelliteState`) and the VFD output handlers (`io/vfd_output.py`).
Messages: `E` energy (20 Hz), `S` state (on change), `C` config (on change), `R` reset; values
normalised to 0..1 (or −1..+1). Output goes over UDP :5110 in development
(`python -m vfd_satellite --udp`) and over RS485 through the gateway in the car.
Protocol: [VFD_SATELLITE_PROTOCOL.md](VFD_SATELLITE_PROTOCOL.md).

## Open gaps

- `battery_cells` slot 4 (`21 CF`): until 2026-11-15 it was subscribed without ISO-TP and the gateway
  returned only the first frame of the 18-byte answer (confirmed on the 2026-11-13 recording: 550 of
  550 answers `10 12 61 CF …`; `delta_soc`, fan speed and intake temperature never reached the state).
  The subscription now sets `isotp` with a 500 ms timeout; not yet verified on the car
  ([BATTERY_HEALTH.md](BATTERY_HEALTH.md) §11).
