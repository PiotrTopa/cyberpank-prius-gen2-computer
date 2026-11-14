# Virtual twin: state store, actions, rules, IO ports

The backend (and the standalone app) keeps a model of the car in one immutable `AppState`. Hardware
input becomes actions, actions change the state, rules derive state, and UI actions become hardware
commands. Neither the UI nor the rules talk to hardware directly. Processes and transports around
it: [ARCHITECTURE.md](ARCHITECTURE.md). Runnable walk-through: `examples/virtual_twin_example.py`.

```
InputPort ──► IngressController ──► Store.dispatch(Action) ──► reducer ──► new AppState
 (serial,      (decode AVC/CAN/          │                                    │ notify(slices)
  file, mock,   satellite → actions)     │ middleware (after the update)       ├──► RulesEngine ──► more actions
  panda)                                 └──► EgressController (UI actions)    ├──► UI / API / ZMQ / metrics
                                                  │                            └──► EgressController state handlers
                                                  ▼
                                             OutputPort (serial, log, mock, UDP)
```

## 1. State (`state/app_state.py`, `state/store.py`)

`AppState` is a frozen dataclass; every update creates a new object. Slices and their subscription
keys (`StateSlice`):

| `AppState` field | `StateSlice` | Content / writer |
|---|---|---|
| `audio` | `AUDIO` | volume, bass, mid, treble, balance, fader, mute |
| `climate` | `CLIMATE` | AVC-LAN climate status/setpoint, outside temp ([AVC_BACKEND.md](AVC_BACKEND.md)) |
| `vehicle` | `VEHICLE` | READY, gear, speed, engine, solicited PIDs |
| `energy` | `ENERGY` | HV battery, power flow, fuel |
| `dynamics` | `DYNAMICS` | steering, acceleration, wheel pulses, headlight state |
| `connection` | `CONNECTION` | gateway / panda link status |
| `debug` | `DEBUG` | debug counters |
| `input` | `INPUT` | last AVC button / touch |
| `diagnostics` | `DIAGNOSTICS` | DTCs and the diagnostics engine ([TOYOTA_DIAGNOSTICS.md](TOYOTA_DIAGNOSTICS.md) §9) |
| `display` | `DISPLAY` | display settings (chart time base, …) |
| `vfd_satellite` | `VFD_SATELLITE` | computed VFD content (device 110) |
| `powerbox` | `POWERBOX` | ignition (ACC/BATT), INA219 telemetry (devices 200-202) |
| `satellites` | `SATELLITES` | per-node twin, OUT2 power wake-locks |
| `avc` | `AVC` | AVC-LAN link, display screen, lights, touch, nav emulation |
| `vision` | `VISION` | dashcam auto-light / environment ([DASHCAM_70MAI.md](DASHCAM_70MAI.md)) |
| `roadsense` | `ROADSENSE` | roadsense stack health, phone thermal zones |
| `bean` | `BEAN` | BEAN message table, known values, capture status (device 203) |
| `screen_brightness`, `ambient_*` | — | UI-only values |

`StateSlice.ALL` subscribes to every change.

## 2. Actions (`state/actions.py`)

An action has a `type` (`ActionType`), a `source` and its data (`SetVolumeAction(35, source=…)`).

| `ActionSource` | Meaning | Effect |
|---|---|---|
| `GATEWAY` | value from the car | update state only (never echoed back) |
| `UI` | user input (MFD, dashboard, REST/ZMQ command) | update state; the egress middleware turns it into a hardware command |
| `INTERNAL` | app logic, timers, rules | update state only |

Rules dispatch with `INTERNAL`. There is no `RULE` source (see Open gaps).

## 3. Store

```python
store.subscribe(StateSlice.AUDIO, callback)   # returns an unsubscribe function
store.add_middleware(fn)                       # fn(action, store), runs after the update
store.dispatch(SetVolumeAction(50, source=ActionSource.UI))
```

`dispatch` runs the reducer (`Store._reduce`, returns the changed slices), notifies the subscribers
of those slices, then runs middleware. An action dispatched while another is being processed
(by a subscriber, rule or middleware) is queued and processed afterwards. `BatchAction` applies
several actions with one notification. The store holds a re-entrant lock, because the panda reader thread
dispatches broadcast CAN directly; everything else dispatches on the engine thread.

## 4. IO ports (`io/ports.py`)

| Interface | Methods |
|---|---|
| `InputPort` | `start()`, `stop()`, `poll() -> RawMessage | None` (non-blocking), `is_connected()` |
| `OutputPort` | `send(OutgoingCommand) -> bool`, `is_connected()` |
| `BidirectionalPort` | both (serial) |

| Implementation | File | Use |
|---|---|---|
| `SerialPort` (+ `SerialInputPort`/`SerialOutputPort` views) | `io/serial_io.py` | gateway / powerbox, auto-reconnect every 2 s |
| `MultiInputPort` | `io/multi_input.py` | merges gateway + powerbox; offsets powerbox ids into 200+ |
| `FileInputPort` | `io/file_io.py` | `--replay` (backend logs and gateway-v3 captures) |
| `MockInputPort`, `MockOutputPort`, `LogOutputPort` | `io/mock_io.py` | tests; dev output (`[WOULD SEND]`) |
| `UDPOutputPort`, `MultiOutputPort` | `io/udp_output.py` | VFD satellite over UDP :5110 in development |

`RawMessage(device_id, timestamp, data, sequence, category)` is built from the gateway line
`{"id","d","seq","ts"}` (`RawMessage.from_gateway_json`, `ts` ms → s).
`OutgoingCommand(device_id, command_type, payload, priority, timestamp, include_cmd)` serialises to
`{"id","cmd","d"}`, or to `{"id","d"}` when `include_cmd=False` (AVC-LAN TX).

### Device ids

| Id | Device | Constant |
|---|---|---|
| 0 | gateway system | `DEVICE_SYSTEM` |
| 1 | CAN | `DEVICE_CAN` |
| 2 | AVC-LAN | `DEVICE_AVC` |
| 100-199 | RS485 satellites: 106 DRL controller, 107 rain/light sensor, 110 VFD | `DEVICE_SATELLITE_*`, `DEVICE_VFD` |
| 200 / 201 / 202 / 203 | powerbox system / power telemetry / power events / BEAN receiver | `DEVICE_POWERBOX_*` |
| 300-399, 400-499 | BLE and Wi-Fi satellites (registry ids, not gateway ids) | [SATELLITES.md](SATELLITES.md) |

## 5. Ingress and egress (`io/ingress.py`, `io/egress.py`)

- `IngressController.update()` drains the input port each engine tick and routes by device id:
  AVC-LAN to `io/avc_ingest.py::AvcIngest` (comm.avc decoder; AVC values take priority over CAN for
  15 s, see [AVC_BACKEND.md](AVC_BACKEND.md)), CAN to `comm/can_decoder.py::CANDecoder`, satellites
  and powerbox to their handlers. `ingest_broadcast_can()` is the panda entry point.
- `EgressController` registers itself as store middleware and turns `UI` actions into commands
  (climate actions → AVC-LAN `ClimateRemote` presses; audio actions; satellite commands). It also
  carries state-change handlers, e.g. the VFD output (`io/vfd_output.py`).

## 6. Rules (`state/rules/engine.py`)

| Element | Contract |
|---|---|
| `StateRule` | `name`, `watches: Set[StateSlice]`, `priority` (`RulePriority.HIGH` 0 / `NORMAL` 50 / `LOW` 100), `enabled`, `evaluate(old_state, new_state, store)` dispatching any derived actions |
| `FunctionalRule`, `create_computed_rule()` | a rule from a function |
| `RulesEngine(store, max_cascades=10)` | subscribes to `ALL`, detects changed slices, evaluates the rules that watch them in priority order; actions dispatched by rules may trigger further rules up to the cascade limit |

Registered rules:

| Rule | File | Registered by |
|---|---|---|
| `ParkSpeedRule`, `FuelConsumptionRule`, `TripFuelConsumptionRule`, `ActiveFuelRule`, `ChartDataRule` | `state/rules/` | `io/factory.py` (every mode) |
| `VFDDisplayRule` | `state/rules/vfd_display.py` | `io/factory.py` when `enable_vfd_satellite` |
| `PowerModeRule`, `UndervoltageProtectionRule` | `state/rules/power_management.py` | `backend/service.py` |
| `SatelliteAccHoldRule`, `SatellitePowerRule` | `state/rules/satellite_power.py` | `backend/service.py` |
| `DRLControlRule` | `state/rules/drl_control.py` | not registered: an example (its `DRLUserMode` and `SetDRLOutputAction` are placeholders, not in `app_state.py`/`actions.py`) |

Rules compute state; hardware side effects belong in the egress controller.

## 7. Execution modes (`io/factory.py`)

`create_virtual_twin(VirtualTwinConfig)` builds store, ports, controllers and rules for an
`ExecutionMode`:

| Mode | Input | Output |
|---|---|---|
| `PRODUCTION` | `SerialPort` (`serial_port` `/dev/ttyACM0`, 1 000 000 baud, auto-reconnect 2 s) | same serial port (+ UDP to the VFD) |
| `DEVELOPMENT` | `FileInputPort` (`replay_file`, `playback_speed`, `playback_loop`) | `LogOutputPort` (+ UDP to the VFD) |
| `TEST` | `MockInputPort` | `MockOutputPort` |

The backend builds on the same factory and adds the powerbox, panda, satellite registry, metrics and
the API.

## 8. Data flow examples

Outside temperature from the car:

```
{"id":2,"d":{"m":"1C6","s":"110","d":["00","E0","5D","F7","46"]}}
 → IngressController → AvcIngest (OutsideTemp 0x46 − 48 = 22 °C)
 → SetOutsideTempAction(22.0, source=GATEWAY) → climate.outside_temp → UI redraw
```

User changes the fan speed:

```
ClimateScreen → store.dispatch(SetFanSpeedAction(3, source=UI)) → state updated
 → EgressController middleware → ClimateRemote → press/release frames via the gateway
 → the climate ECU's status frame (E0 5D F3) confirms the new value
```

## 9. Adding a feature

1. State: fields in the right `*State` dataclass; a new `StateSlice` only for a new area.
2. Actions: `ActionType` entry + action class; reducer branch in `Store._reduce` returning the changed slices.
3. Derived state: a `StateRule` registered in `io/factory.py` (all modes) or `backend/service.py` (backend only).
4. IO: decoder in the ingress path; command encoding in the egress controller; device id in `io/ports.py`.
5. UI: subscribe to the slice; dispatch `UI` actions on input. The API serialises the state
   automatically (`api/serialization.py`); the frontend deserialises it (`frontend/deserialize.py`).

## Open gaps

- `state/rules/drl_control.py` refers to `ActionSource.RULE`, which does not exist in
  `ActionSource`; the example would fail if registered. No DRL state exists in `AppState`; the DRL
  satellite (106) and rain/light satellite (107) have ids but no state fields or rules.
