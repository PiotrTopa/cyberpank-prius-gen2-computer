# Position feeder (backend -> roadsense mapd)

The backend delivers raw GNSS fixes and vehicle motion to roadsense's map daemon `mapd` over its
position input API v1 (roadsense docs/ARCHITECTURE.md, "mapd"). roadsense owns all position
filtering, dead reckoning and map matching; the backend only feeds. Status: built 2026-11-15 on the
dev box, not deployed; no real GNSS source exists yet.

## 1. Parts

| Part | File |
|---|---|
| position API client (copy of roadsense `tools/position_client.py`, MIT, with a partial-send fix) | `cyberpunk_computer/backend/position_client.py` |
| CAN motion tap, GNSS sources, feeder thread | `cyberpunk_computer/backend/position_feeder.py` |
| REST: status, head-unit ingest | `cyberpunk_computer/api/position_routes.py` |
| synthetic route (5.6 km loop in Opole, OSM geometry, ODbL) | `cyberpunk_computer/backend/data/position_route_opole.json` |
| wiring (panda tee, gating, status mirror) | `backend/service.py` (`_wire_position_feeder`, `_position_feeder_tick`) |
| env | `backend/__main__.py` (`_build_position`) |
| map / fused consumption (scene key `map`) | `backend/roadsense_scene.py`, contract [MFD_MAIN_SCREEN.md](MFD_MAIN_SCREEN.md) §7.2 |
| tests | `tests/test_position_feeder.py`, `tests/test_position_mapd_e2e.py` |

Nothing is imported from roadsense; the e2e test runs the roadsense checkout as external processes.

## 2. Data flow and rules

```
panda reader thread ─on_frame tee─▶ CanMotionTap (latest 0x0B4, 0x120, 0x0B1, 0x0B3, 0x025 + receipt time)
                    └────────────▶ ingress (unchanged)
head unit ─POST /api/v1/position/gnss─▶ HeadunitGnssSource (validate, time -> CLOCK_MONOTONIC, queue)
                                              │
PositionFeeder thread, motion_hz (10) ────────┴──▶ PositionFeed ──▶ /run/roadsense/position.sock (mapd)
```

* Off unless `BACKEND_POSITION_FEEDER=1`. Off = no thread, no tap on the panda callback, no routes.
* Runs only while the roadsense manager is `starting`, `running` or `grace` (mapd is a unit of
  `roadsense.target`); without a roadsense manager (dev) always. Inactive = socket closed, thread
  asleep.
* mapd absent: every message is dropped, a reconnect is tried at most every 2 s (`connect` on a
  missing path), no log line per attempt; connect / disconnect edges are logged once.
* Never blocks: non-blocking socket; a full socket buffer drops the message; a short write closes the
  connection (no half line in the stream).
* One motion message per new 0x0B4 frame, at most `motion_hz` per second.
* No motion while the newest 0x0B4 is older than 0.3 s (car off, CAN dark, panda gone). No GNSS
  source and no CAN = nothing sent at all.
* Without a usable fix (`headunit` silent for 3 s, or it reported `fix: none`; synthetic dropout)
  the feeder sends `{"type":"gnss","fix":"none","source":...}` at once and every 5 s; mapd then has no
  position (`no_source` / `unknown`) or dead-reckons, and roadsense's output is vision only.
* BEAN is not used for motion (its copies of CAN lag 45..110 ms and more, [prius_can.md](prius_can.md)
  0x120).

## 3. Motion fields and their signals

Message: `{"type":"motion","ts_mono_ns","speed_mps","gear","wheel_speeds_mps","source":"prius-can"}`
(`steering_wheel_deg` too with `BACKEND_POSITION_YAW=wheels,steer`).

| Field | Signal | Decode | State |
|---|---|---|---|
| `ts_mono_ns` | panda transfer receipt time of the 0x0B4 frame | `time.monotonic()` (CLOCK_MONOTONIC, the clock mapd uses) | measurement time to within the panda read timeout (20 ms) plus USB buffering; never the send time |
| `speed_mps` | CAN 0x0B4 | (256F + G) × 0.01 km/h / 3.6 | verified: true speed (meter trip distance agrees; speedometer +12 %). Not the display speed |
| `gear` | CAN 0x120 | byte F low nibble 0 P, 1 R, 2 N, 3 D, 4 B; other values: left out; left out when older than 1 s | P/R/N/D verified on 20 shifts; B never observed |
| `wheel_speeds_mps` `[FL, FR, RL, RR]` | CAN 0x0B1 (front), 0x0B3 (rear): right = A,B, left = C,D | × 0.01 km/h / 3.6; only when both frames are within 0.1 s of the speed frame | unit verified on the 2026-11-13 drive (wheel mean 0.978 × 0x0B4; front/rear (right − left) r = 0.97); left/right labels third-party |
| `steering_wheel_deg` (opt-in) | CAN 0x025 | 12-bit signed (low nibble of A, B) × 1.5°, positive = left | scale verified by range (±650–660° at the stops); r = 0.95 with the rear wheel-speed yaw, sign consistent with the wheel labels; absolute sign third-party (Toyota convention) |
| `yaw_rate_rps` | none | — | not sent: no yaw-rate sensor frame is decoded; 0x03A (named "yaw rate" in `can_decoder.py`) correlates 0.01 with the wheel-speed yaw |

Sign chain: mapd derives yaw from the rear pair as (RR − RL) / track, positive = counter-clockwise
(left). Right wheel faster = left turn under the third-party labels. Steering positive (left) has the
same sign as (right − left) on the drive. Indirect check of the absolute sign: tight turns (|curvature|
> 0.08 /m at 2–8 m/s) are right-handed 131 times against 87 left (right turns are tighter in
right-hand traffic). Not verified directly.

Analysis basis: `trip_20261113_214804_935.ndjson` on the phone (16 min drive, max 52.5 km/h; panda
frames, deduplicated by the ingress at 0.25 s), interpolated to 10 Hz.

Why wheels and not steering by default: mapd uses `steering_wheel_deg` only without wheel speeds,
with a kinematic model (ratio 19.1, wheelbase 2.70 m). Fitted to the rear-wheel yaw of the drive, the
effective ratio is 21 over all speeds and 29 above 8 m/s, so the steering input over-predicts the
yaw by ~10 % to ~50 %. The rear wheels are not driven (no drive slip). Rear-wheel yaw while driving
straight (> 8 m/s, |steering| < 3°): mean 0.87 °/s, standard deviation 0.84 °/s on the deduplicated
log (mapd estimates a yaw bias).

Not corrected: the wheel speeds read 2.2 % below 0x0B4 (the yaw scales with it); the backend's own
`dynamics.steering_angle` still uses × 0.1.

## 4. Head-unit GNSS contract (to build on the head unit)

Transport: HTTP `POST http://<phone>:8080/api/v1/position/gnss`, `Content-Type: application/json`,
`Authorization: Bearer <BACKEND_AUTH_TOKEN>` when the backend has a token. Port 8080 is open on every
interface (`POCO/rootfs/etc/nftables.d/50_prius_backend.nft`), so it works over the planned USB RNDIS
link or the phone's Wi-Fi AP ([HEADUNIT_INTEGRATION.md](HEADUNIT_INTEGRATION.md) §3).

Why REST: the backend already serves authenticated REST to every other client; one request per fix
at 1..10 Hz is cheap; each request is acknowledged with per-fix validation errors; no new port,
firewall rule or protocol. roadsense's own `POST /v1/position` is not used: it listens on loopback
only, and the backend has to convert the clock and own the source policy.

Body: one fix object, or an array of up to 20 (oldest first). Rate: every fix as it comes (1..10 Hz),
no batching needed. Fields (Android `Location` names in brackets):

| Field | Required | Unit / range | Source on Android |
|---|---|---|---|
| `lat`, `lon` | yes (unless `fix` is `none`) | degrees WGS84; 0,0 is rejected | `getLatitude()`, `getLongitude()` |
| `age_ms` | strongly recommended | ms ≥ 0: age of the fix at the moment of sending | `(SystemClock.elapsedRealtimeNanos() - location.getElapsedRealtimeNanos()) / 1e6` |
| `utc_ms` | recommended | Unix ms (integer) | `getTime()` |
| `fix` | no (default `3d`) | `none`, `2d`, `3d`, `dgps`, `rtk`, `dr` | `none` when the provider reports no fix / is disabled |
| `hacc_m` | recommended | m, 68 % radius | `getAccuracy()` (if `hasAccuracy()`) |
| `speed_mps` | no | m/s 0..120 | `getSpeed()` (if `hasSpeed()`) |
| `speed_acc_mps` | no | m/s | `getSpeedAccuracyMetersPerSecond()` (API 26+) |
| `course_deg` | no | 0..360, clockwise from true north | `getBearing()` (if `hasBearing()`) |
| `course_acc_deg` | no | degrees | `getBearingAccuracyDegrees()` (API 26+) |
| `alt_m`, `vacc_m` | no | m | `getAltitude()`, `getVerticalAccuracyMeters()` (passed on, unused by mapd) |
| `sats` | no | integer 0..200 | `getExtras().getInt("satellites")` or `GnssStatus` |
| `hdop` | no | | NMEA GSA (passed on, unused by mapd) |

Unknown fields are ignored. `{"fix":"none"}` reports "no fix now"; silence for 3 s means the same.

Clock (phone CLOCK_MONOTONIC), best first:

1. `age_ms`: t = arrival − age − 0.02 s link delay. Needs no clock sync between the devices.
2. `utc_ms` against the phone's wall clock, only when the difference is −0.5..2.5 s.
3. Otherwise the arrival time − 0.02 s (counted in the status as `time_arrival`).

Rejected: a fix older than 2.5 s on arrival, one not newer than the previous fix, out-of-range
values. Answer: `200 {"accepted": n, "rejected": [{"index": i, "reason": "..."}], "has_fix": bool}`;
`400` body not JSON / more than 20 fixes; `401` token; `413` body over 64 kB; `503`
`{"detail":{"reason":"source_not_headunit"}}` when the feeder runs another source. The route exists
only with the feeder on. Every fix goes to mapd with `"source": "headunit"`.

## 5. Synthetic source (test / demo only)

`BACKEND_POSITION_SOURCE=synthetic`; never a default. A vehicle drives the route (built-in: 5.6 km
loop in Opole; `BACKEND_POSITION_SYNTHETIC_ROUTE` = JSON `{"points": [[lat, lon, v_mps], ...]}` or a
bare list, open routes are driven there and back) at 0.9 × the map's speed limits, curve speed ≤ √(2
m/s² / curvature), 1.2 m/s² acceleration, 2 m/s² braking. It sends both the motion (speed, gear D,
wheel speeds from the yaw with track 1.48 m, steering angle) and GNSS (1 Hz, 2.5 m noise correlated
over 20 s + 0.5 m white, `hacc_m` 5, course from 1 m/s), all with `"source": "synthetic"`; mapd
reports it in `map.input` and the scene shows `map.in.gnss = "synthetic"`. Fix dropouts:
`BACKEND_POSITION_SYNTHETIC_DROPOUTS=t0:t1,...` (s since start). While the real car moves (CAN 0x0B4
fresh and > 0.5 m/s) the synthetic drive sends nothing (`suspended: "car_moving"`) and resumes from
standstill afterwards. A warning is logged at start.

## 6. Status (`state.roadsense.position`, `GET /api/v1/position`)

Mirrored on change, at most every 2 s; `null` while the feeder is off.

| Field | Meaning |
|---|---|
| `enabled`, `source`, `synthetic` | feeder on; `headunit` / `synthetic` / `none` |
| `active` | running now (roadsense up) |
| `suspended` | `car_moving` (synthetic only) or null |
| `connected`, `socket`, `connects` | connected to mapd's `position.sock` |
| `sent`, `dropped` | messages written / dropped (mapd away or socket full) |
| `motion_sent`, `gnss_sent`, `none_sent` | per kind |
| `errors`, `last_error` | error replies from mapd |
| `motion.yaw_input` | `wheels`, `steer`, `wheels+steer` or null in the last motion message |
| `motion.can_age_s`, `motion.can_frames` | age of the newest 0x0B4, frames seen by the tap |
| `gnss.has_fix`, `gnss.last_fix_age_s` | source has a usable fix; age of the newest fix |
| `gnss.*` (head unit) | `received`, `accepted`, `rejected`, `none`, `time_age` / `time_utc` / `time_arrival`, `last_reject`, `fix`, `hacc_m`, `sats` (no coordinates) |
| `gnss.*` (synthetic) | `fixes`, `route`, `route_m`, `along_m`, `laps`, `speed_mps`, `dropout` |

The roadsense manager's unit mirror (`state.roadsense.units`) now includes `roadsense-mapd.service`.

## 7. Configuration and deploy steps (not executed)

| Env (`/etc/prius/backend.env`) | Default | Meaning |
|---|---|---|
| `BACKEND_POSITION_FEEDER` | `0` | `1` turns the feeder on |
| `BACKEND_POSITION_SOURCE` | `headunit` | `headunit`, `synthetic`, `none`; an unknown value keeps the feeder off (logged) |
| `BACKEND_POSITION_RUNDIR` | `/run/roadsense` | where `position.sock` is |
| `BACKEND_POSITION_MOTION_HZ` | `10` | 5..50 |
| `BACKEND_POSITION_YAW` | `wheels` | comma list of `wheels`, `steer`; empty = speed and gear only |
| `BACKEND_POSITION_FIX_TIMEOUT_S` | `3` | head unit silent this long = no fix |
| `BACKEND_POSITION_SYNTHETIC_ROUTE` | built-in | route file |
| `BACKEND_POSITION_SYNTHETIC_DROPOUTS` | none | `t0:t1,...` |

Deploy (none of this was done):

1. Phone: install mapd and a pack from the roadsense checkout (`scripts/install-phone.sh` installs
   `/usr/local/lib/roadsense/mapd` and `roadsense-mapd.service`; `roadsense-maps install` sets
   `/var/lib/roadsense/maps/current.rsmap`, roadsense docs/MAPS.md). On 2026-11-15 the phone had
   neither `roadsense-mapd.service` nor `/var/lib/roadsense/maps` (read-only check), and its installed
   `roadsense.target` does not want mapd yet.
2. Backend: sync this branch; add `BACKEND_POSITION_FEEDER=1` (source `headunit`) to
   `/etc/prius/backend.env`; restart `prius-backend.service`.
3. Check: `GET /api/v1/position` → `active` true with the stack running, `connected` true,
   `none_sent` > 0, `motion.can_age_s` < 0.3 with the car on; roadsense `GET /v1/map` →
   `input.motion.tag` `prius-can`, `input.gnss.fix` `none`, `state` `no_source`.
4. Demo only, car parked: `BACKEND_POSITION_SOURCE=synthetic` (map output shows `synthetic`).

## 8. Tests (2026-11-15, dev box)

| Test | Result |
|---|---|
| `tests/test_position_feeder.py`: decoders on real frames, tap freshness / pairing / field order, head-unit validation and clock, feeder cycle (no fix → `none`, repeats, fix forwarded, silent with CAN dark, quiet drop without mapd, synthetic suspension), synthetic drive consistency, PositionFeed on a Unix socket (hello, error replies, reconnect), `fused` → scene, REST routes with a token, env parsing, service wiring / gating | 23 passed |
| `tests/test_position_mapd_e2e.py` with roadsense `.venv-models` (fake camera daemons `--no-mapd` + real mapd + real api) and `dev-opolskie.rsmap`: no fix | mapd `no_source`, `available` false, `reason` `no_position`, `input.gnss` `headunit`/`none`, motion `vehicle`/`prius-can`; api `fused.map.available` false, limit source `sign` (vision); scene `map.ok` false, `why` `no_position`, `map.limit` = scene `limit` (50 km/h B-33 from the fake signs) |
| same, synthetic drive with a fix dropout at 15–20 s | `good` + matched; position error against the synthetic truth after 8 s: max 4.1 m, mean 2.5 m; `dead_reckoning` exactly in the dropout, `available` throughout (13/13 samples), `good` again after it; `input.gnss.source` `synthetic`, motion tag `synthetic`; scene `map.ok` true, `map.in.gnss` `synthetic`, road `Mały Rynek` (living street) |
| full suite `pytest tests --ignore=tests/test_pointer_ui.py` | 1155 passed, 9 skipped, 5 failed: the 5 (`test_bean_rx`, `test_bean_tx`) need `tests/fixtures/bean_nhw20_frames.txt.gz`, which exists only untracked in the main checkout; with that file copied in, both files pass (162) |

## 9. Open gaps

* No real GNSS source: the head-unit side (agent app sending §4) does not exist; its fix latency,
  `age_ms` availability and accuracy fields are untested.
* Motion is untested against a running mapd on the car; timestamps are panda receipt times (bus
  delay assumed a few ms, not measured).
* Absolute sign of 0x025 and the left/right labels of 0x0B1/0x0B3 rest on third-party conventions
  (consistent with each other and with the turn-tightness asymmetry; see §3). One logged deliberate
  left turn, or a GNSS course, settles it.
* 0x0B1/0x0B3 read 2.2 % below 0x0B4; 0x0B1/0x0B3 bytes E,F and the 0x025 fraction nibble are not
  decoded.
* No yaw-rate sensor decoded; 0x03A is not one in the current reading.
* `dynamics.steering_angle` in the state still uses × 0.1 (15× too small); nothing reads it for
  motion.
* Replay mode (`--replay`) has no panda, so no motion; the feeder is a live-car feature.
* Steering ratio / wheelbase in mapd are nominal (19.1 / 2.70 m); this car's effective ratio is
  speed-dependent (21–29, §3).
* The MFD screens do not draw the scene `map` key yet.
* roadsense reference client gap (reported, roadsense not changed): `tools/position_client.py`
  counts a short non-blocking `send` as sent and leaves a partial line in the stream; the copy here
  closes the connection instead.
