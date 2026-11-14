# RS485 Satellite Power Management & Twin Model

Multi-transport satellites (RS485 + BLE + Wi-Fi, keychain, auth,
`SatelliteRegistry`, REST commands): [SATELLITES.md](SATELLITES.md). This document
covers the RS485 rail, its wake-locks and the RS485 job queue.

How the backend manages the **RS485 satellite rail** (powerbox relay channel 5) and models each RS485
satellite in the virtual twin. Code: `state/rules/satellite_power.py`,
`backend/satellites.py`, `SatellitesState` in `state/app_state.py`.

## Hardware

```
Prius 12V ──► powerbox relay board ──relay ch5 (PCF8574)──► RS485 satellites
                                                        ├─ 106 DRL controller
POCO ◄─USB─► gateway RP2040 ◄────RS485 half-duplex────► ├─ 107 rain/light sensor
                                                        └─ 110 VFD display
```

* relay ch5 is switched by the powerbox firmware on host command
  (`{"a":"relay","ch":5,"on":b}`); the firmware never applies policy itself.
  The relay board's PCF8574 wakes with every coil released, so after any
  powerbox restart (including the firmware's `hard_restart` self-power-cut,
  which drops every NO relay) the rail is **OFF** until the rule re-asserts it
  through the mirror-disagreement retry below. Relay 5 is **untested** on this
  board (relays 1–4 verified on the bench; [powerbox/README.md](../powerbox/README.md#relay-board)).
* Satellites talk NDJSON through the **gateway** (device ids 100–199).
* The rail state is mirrored back in the powerbox `STATUS` heartbeat
  (`powerbox.rs485_power`, derived from the STATUS `rly` array, ~1 Hz).

## Power model: wake-locks

`SatellitesState.power_holders` is a set of named wake-locks. **The rail is on iff
the set is non-empty.** Holders:

| Holder      | Held by | Meaning |
|-------------|---------|---------|
| `acc`       | `SatelliteAccHoldRule` | Ignition on → satellites always ready. |
| `queue`     | `SatelliteJobQueue`    | Jobs pending/running (key-off work). |
| `manual:*`  | API `satellite_power_hold`, UI rail toggle / `set_relay` ch5 | Operator holds. |

`SatellitePowerRule` (watches SATELLITES + POWERBOX) turns the holder set into
the physical rail via `PowerboxCommander.set_rs485_power(…)` (relay ch5):

* ON is applied immediately;
* OFF only after a **linger** (default 10 s) of the set staying empty —
  absorbs key-off bounce and back-to-back jobs (no rail flapping);
* while the `powerbox.rs485_power` mirror disagrees, the command is re-sent every
  `satellite_retry_s` (recovers lost serial commands / powerbox reboots).

### Gateway USB power is bonded to the same wake-locks

The gateway board (CAN/AVC-LAN bridge **and** RS485 master) has exactly the
same logical requirement as the rail: needed when ACC is on (CAN/AVC traffic) and
whenever satellite jobs run (RS485). So `SatellitePowerRule` also drives the
gateway's power (via `SetGatewayUsbPowerAction` → `backend.port_power`
`RelayPortPower`, powerbox relay **ch3** = the USB hub that carries the gateway
**and** the comma.ai panda, so both switch together) on the same ON/OFF edges;
there is **no separate manual gateway power control**. Convergence/retry for the gateway port is owned by
the backend's `_port_power_enforce_tick` (desired vs the powerbox `rly`
read-back, ~15 s) plus an unconditional re-assert ~6 s after a
`POWERBOX_READY` boot banner.

## Job queue: one FIFO-within-priority queue for all satellite work

Every satellite interaction is a `SatelliteJob` submitted to the single
`SatelliteJobQueue` — scheduled sensor reads, event commands (e.g. remote
start prep), config re-pushes, API requests. Priorities (lower first, FIFO
within a level): config 10, event 30, normal 50, scheduled 70.

The queue:

1. acquires the `queue` wake-lock while non-empty (one shared power-up for
   overlapping jobs — a temperature read finishing while a light-control job
   arrives never cycles the rail);
2. per job waits for the rail (`powerbox.rs485_power`) and the job's
   `requires_online` satellites to report presence, then calls `start`; jobs
   with a `poll` callback run until done/timeout;
3. releases the lock when drained (the rule's linger provides the OFF delay);
4. on sustained 12 V **under-voltage** cancels everything and releases the
   lock — key-off satellite work can never drain the battery. The scheduler
   also skips cycles while under-voltage.

Serialized execution also matches the half-duplex RS485 bus.

### Producers

* **Scheduler** — `PeriodicJobSpec` (e.g. every 5 min wake + read sensors).
  Config: `satellite_poll_interval_s` + `satellite_poll_devices` (+ payload).
  Skips a cycle while the same-named job is still in flight.
* **Events / API** — dispatch `EnqueueSatelliteCommandAction` (exposed as the
  `satellite_send` REST command); backend middleware routes it into the queue.
* **Supervisor** — config re-push jobs (below).

## Satellite twin: presence + auto-reconfiguration

Each satellite is a `SatelliteNode` in `SatellitesState.nodes`:
`online`, `last_seen`, `boot_id`, `fw_version`, `desired_config` /
`reported_config` / `config_synced`.

* **Presence:** any ingress traffic from device 100–199 refreshes the node
  (throttled ~1/s). `SatelliteSupervisor` marks nodes offline after
  `satellite_offline_after_s` of silence, or immediately when the rail is
  known down. Output-only satellites that never transmit simply stay offline
  in the twin — don't gate jobs on them via `requires_online`.
* **Reboot detection:** the reducer bumps `boot_id` and clears
  `config_synced` on every offline→online edge.
* **Auto-reconfigure:** when a node is online with `config_synced == False`
  and a persisted `desired_config`, the supervisor submits a
  priority-10 config job that pushes the config and marks it synced. So a
  satellite that restarts (or gets rail-cycled) is reconfigured before any
  queued user job talks to it.
* **Persistence:** `user_settings.json → satellites.nodes` maps device id to
  the raw config payload, loaded at backend start.

```json
{ "satellites": { "nodes": { "110": { "t": "C", "bri": 80 } } } }
```

## Observability

`SatellitesState` mirrors everything for the UI/API (auto-serialized in the
state snapshot): `power_holders`, `power_requested` (desired the RS485 rail —
ground truth stays `powerbox.rs485_power`), `queue_depth`, `active_job`, and the
per-node twin.

## Backend config knobs (`BackendConfig`)

| Field | Default | |
|-------|---------|---|
| `satellites_enabled` | `True` | Master switch. |
| `satellite_linger_s` | `10.0` | OFF debounce after last wake-lock releases. |
| `satellite_retry_s` | `5.0` | Re-send cadence on mirror mismatch. |
| `satellite_offline_after_s` | `15.0` | Presence staleness. |
| `satellite_poll_interval_s` | `0` (off) | Periodic key-off wake interval. |
| `satellite_poll_devices` | `()` | Devices poked each poll cycle. |
| `satellite_poll_payload` | `{"a":"status"}` | Solicit payload. |

Multi-transport knobs (`satellite_transports`, `satellite_rediscover_s`,
`satellite_keychain_dir`, `satellite_wifi_*`, `satellite_ble_socket`) and
their `BACKEND_SATELLITE_*` env vars: see SATELLITES.md §8.

## REST commands

* `satellite_send {device_id, payload, priority?}` — enqueue a command
  (powers the rail if needed, serialized).
* `satellite_power_hold {name, on}` — manual `manual:<name>` wake-lock.

A manual `set_relay` ch5 (UI rail toggle) is translated into the `manual:ui`
wake-lock (`BackendService`), so the operator and the rule never fight over the rail.

## Open gaps

| Gap | What would resolve it |
|---|---|
| Relay ch5 (the rail) never switched on the real board. | Bench: `satellite_power_hold` on/off, check `powerbox.rs485_power` and the rail voltage. |
| Where the firmware of the RS485 nodes (DRL 106, rain/light 107, VFD 110) lives, and which nodes are fitted on the car, is not documented here (`vfd_satellite/` is a pygame simulator). | Inventory the RS485 bus (`satellite_discover`); link the firmware sources. |
