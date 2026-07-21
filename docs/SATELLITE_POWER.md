# RS485 Satellite Power Management & Twin Model

How the backend manages the **OUT2 satellite rail** and models each RS485
satellite in the virtual twin. Code: `state/rules/satellite_power.py`,
`backend/satellites.py`, `SatellitesState` in `state/app_state.py`.

## Hardware recap

```
Prius 12V ──► powerbox RP2040 ──OUT2 (GP28, MOSFET)──► RS485 satellites
                                                        ├─ 106 DRL controller
POCO ◄─USB─► gateway RP2040 ◄────RS485 half-duplex────► ├─ 107 rain/light sensor
                                                        └─ 110 VFD display
```

* OUT2 is switched by the powerbox firmware on host command
  (`{"a":"out","ch":2,"on":…}`); firmware boots it **ON** as a fail-safe and
  never applies policy itself.
* Satellites talk NDJSON through the **gateway** (device ids 100–199).
* The rail state is mirrored back in the powerbox `STATUS` heartbeat
  (`powerbox.out2`, ~1 Hz).

## Power model: wake-locks

`SatellitesState.power_holders` is a set of named wake-locks. **OUT2 is on iff
the set is non-empty.** Holders:

| Holder      | Held by | Meaning |
|-------------|---------|---------|
| `acc`       | `SatelliteAccHoldRule` | Ignition on → satellites always ready. |
| `queue`     | `SatelliteJobQueue`    | Jobs pending/running (key-off work). |
| `manual:*`  | API `satellite_power_hold`, UI OUT2 toggle | Operator holds. |

`SatellitePowerRule` (watches SATELLITES + POWERBOX) turns the holder set into
the physical rail via `PowerboxCommander.set_out(2, …)`:

* ON is applied immediately;
* OFF only after a **linger** (default 10 s) of the set staying empty —
  absorbs key-off bounce and back-to-back jobs (no rail flapping);
* while the `powerbox.out2` mirror disagrees, the command is re-sent every
  `satellite_retry_s` (recovers lost serial commands / powerbox reboots).

### Gateway USB power is bonded to the same wake-locks

The gateway board (CAN/AVC-LAN bridge **and** RS485 master) has exactly the
same logical requirement as OUT2: needed when ACC is on (CAN/AVC traffic) and
whenever satellite jobs run (RS485). So `SatellitePowerRule` also drives the
gateway's USB hub-port power (via `SetGatewayUsbPowerAction` →
`prius-usb-power`) on the same ON/OFF edges — there is **no separate manual
gateway power control** (the old `gateway_power` API command was removed).
Convergence/retry for the gateway port is owned by the backend's
`_gateway_usb_power_tick` desired-state poll (uhubctl ground truth, ~10 s).

## Job queue: one FIFO-within-priority queue for all satellite work

Every satellite interaction is a `SatelliteJob` submitted to the single
`SatelliteJobQueue` — scheduled sensor reads, event commands (e.g. remote
start prep), config re-pushes, API requests. Priorities (lower first, FIFO
within a level): config 10, event 30, normal 50, scheduled 70.

The queue:

1. acquires the `queue` wake-lock while non-empty (one shared power-up for
   overlapping jobs — a temperature read finishing while a light-control job
   arrives never cycles the rail);
2. per job waits for the rail (`powerbox.out2`) and the job's
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
state snapshot): `power_holders`, `power_requested` (desired OUT2 —
ground truth stays `powerbox.out2`), `queue_depth`, `active_job`, and the
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

## REST commands

* `satellite_send {device_id, payload, priority?}` — enqueue a command
  (powers the rail if needed, serialized).
* `satellite_power_hold {name, on}` — manual `manual:<name>` wake-lock.

Note: the UI `set_out` command with `channel=2` is translated into the
`manual:ui` wake-lock so the operator and the rule never fight over the rail.
