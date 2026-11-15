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
| api-server | uvicorn/FastAPI: REST + WebSocket |
| zmq | `ZmqServer` socket IO |
| panda reader | broadcast CAN frames → `IngressController.ingest_broadcast_can` → `Store.dispatch` (the store holds a re-entrant lock for this path); with the position feeder on, `CanMotionTap.on_frame` sees each frame first |
| roadsense-scene | roadsense `/ws` reader → ZMQ event `roadsense_scene` (`backend/roadsense_scene.py`) |
| position-feeder | only with `BACKEND_POSITION_FEEDER=1`: CAN motion + GNSS → roadsense mapd `position.sock` (`backend/position_feeder.py`, [POSITION_FEEDER.md](POSITION_FEEDER.md)) |

`api/bridge.py::StoreBridge` is the boundary: API/ZMQ commands are queued and dispatched on the
engine thread; snapshots go out latest-only so a slow client never blocks the engine.

### Network interfaces of the backend

| Interface | Port | Content |
|---|---|---|
| REST | 8080 | `GET /health`; under `/api/v1`: `state`, `metrics/catalog`, `metrics`, `events`, `commands` (list), `POST commands/{name}`, `bean/frames`, `diag/*`, `camera/preview*`, `position` + `POST position/gnss` (feeder on only), roadsense proxy. Auth: `BACKEND_AUTH_TOKEN` when set |
| WebSocket | 8080 `/api/v1/stream` | state snapshots (dashboard) |
| ZMQ PUB | 8081 | `{"type":"state","ts","state"}` (latest-only, throttled) and `{"type":"event","ts","name","data"}` (FIFO, never displaced by state) |
| ZMQ REP | 8082 | `{"command": name, "params": {...}}` → `{"status":"ok"}` on acceptance (execution is asynchronous) or `{"status":"error","reason"}` |

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
| roadsense | systemd + HTTP :8090 | `backend/roadsense_power.py`, `api/roadsense_proxy.py` | started/stopped on ACC |
| roadsense mapd | Unix socket `/run/roadsense/position.sock` (position input API v1) | `backend/position_feeder.py`, `backend/position_client.py` | off by default; [POSITION_FEEDER.md](POSITION_FEEDER.md) |
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
| Broadcast | comma.ai panda (`io/panda_can.py`) | every broadcast frame (0x03B battery, 0x3CB SOC, 0x0B4 speed, 0x120 gear, steering, wheel speeds, …; ids and scaling in [prius_can.md](prius_can.md)) | panda reader thread → `IngressController.ingest_broadcast_can` → `CANDecoder` → `Store.dispatch`; no input queue, no engine tick (≈0.5 ms decode+dispatch on the POCO) |
| Solicited | RP2040 gateway (`io/serial_io.py`) | periodic OBD/Toyota subscriptions, one-shot requests, DTC scans | serial reader → `RawMessage` queue → engine tick → `IngressController._handle_can_message` |

Gateway firmware 3.x forwards no passive frames unless `sniff` is on, and `sniff` disables
`req`/`sub`/`tx` ([PROTOCOL.md](PROTOCOL.md)), so one device cannot do both. Rules for the panda:

- always **SAFETY_SILENT** (bus monitor: no ACK, no TX); the backend never writes to its CAN TX endpoint;
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

| Group | Slot | Item | ECU | Request | Interval ms | Timeout ms / ISO-TP |
|---|---|---|---|---|---|---|
| core | 0 | engine RPM | 0x7E0 | `01 0C` | 500 | 200 |
| core | 1 | engine load | 0x7E0 | `01 04` | 2000 | 200 |
| core | 2 | hybrid comprehensive | 0x7E2 | `21 C3` | 1000 | 500 / yes |
| core | 6 | cruise control | 0x7E2 | `21 D3` | 1000 | 200 |
| battery_cells | 3 | block voltages | 0x7E3 | `21 CE` | 2000 | 500 / yes |
| battery_cells | 4 | battery temps / fan / delta SOC | 0x7E3 | `21 CF` | 2000 | 200 |
| battery_cells | 5 | block resistances | 0x7E3 | `21 D0` | 5000 | 500 / yes |
| hybrid_extended | 8 | hybrid additional | 0x7E2 | `21 C4` | 2000 | 500 / yes |
| engine_sensors | 9 / 10 / 11 | intake air temp / MAF / lambda | 0x7E0 | `01 0F` / `01 10` / `01 24` | 5000 / 2000 / 2000 | 200 |
| aux_battery | 7 | aux voltage | 0x7E0 | `01 42` | 5000 | 200 |
| environment | 13 / 14 | barometric pressure / ambient air temp | 0x7E0 | `01 33` / `01 46` | 10000 | 200 |
| odometer | 12 | distance since DTC clear | 0x7E0 | `01 31` | 10000 | 200 |

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

- `battery_cells` slot 4 (`21 CF`) is subscribed with a 200 ms timeout and without ISO-TP, while the
  21CF answer is longer than one frame (`PIDDefinition.byte_count` 12); the gateway then returns only
  the first frame (probable, from the firmware's ISO-TP rules in [PROTOCOL.md](PROTOCOL.md)). Whether
  `delta_soc` ever reaches the state on the car is unverified; check a trip recording for slot-4
  responses and their length.
