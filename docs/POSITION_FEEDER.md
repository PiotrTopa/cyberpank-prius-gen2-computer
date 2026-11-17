# Position feeder (backend -> roadsense mapd)

The backend delivers raw GNSS fixes and vehicle motion to roadsense's map daemon `mapd` over its
position input API v1 (roadsense docs/ARCHITECTURE.md, "mapd"). roadsense owns all position
filtering, dead reckoning and map matching; the backend only feeds. Status: built 2026-11-15 on the
dev box, not deployed; no real GNSS source exists yet. The phone's own receiver (`phone`, section 11)
is built and runs as a fallback behind the head unit, but in the car (phone in its box) it tracked no
satellite in 20 min on 2026-11-15.

## 1. Parts

| Part | File |
|---|---|
| position API client (copy of roadsense `tools/position_client.py`, MIT, with a partial-send fix) | `cyberpunk_computer/backend/position_client.py` |
| CAN motion tap, GNSS sources, source selector, feeder thread | `cyberpunk_computer/backend/position_feeder.py` |
| phone GNSS source (ModemManager location, NMEA parser) | `cyberpunk_computer/backend/phone_gnss.py` |
| REST: status (+ raw fix, pack file), head-unit ingest, synthetic session | `cyberpunk_computer/api/position_routes.py` |
| synthetic route (5.6 km loop in Opole, OSM geometry, ODbL) | `cyberpunk_computer/backend/data/position_route_opole.json` |
| wiring (panda tee, gating, status mirror) | `backend/service.py` (`_wire_position_feeder`, `_position_feeder_tick`) |
| env | `backend/__main__.py` (`_build_position`) |
| map / fused consumption (scene key `map`) | `backend/roadsense_scene.py`, contract [MFD_MAIN_SCREEN.md](MFD_MAIN_SCREEN.md) §7.2 |
| dashboard MAP tab (section 10) | `dashboard/src/tabs/MapTab.tsx`, `components/MiniMap.tsx`, `lib/mapLayer.ts` |
| tests | `tests/test_position_feeder.py`, `tests/test_position_synthetic_control.py`, `tests/test_position_mapd_e2e.py`, `tests/test_phone_gnss.py` |

Nothing is imported from roadsense; the e2e test runs the roadsense checkout as external processes.

## 2. Data flow and rules

```
panda reader thread ─on_frame tee─▶ CanMotionTap (latest 0x0B4, 0x120, 0x0B1, 0x0B3, 0x025 + receipt time)
                    └────────────▶ ingress (unchanged)
head unit ─POST /api/v1/position/gnss─▶ HeadunitGnssSource (validate, time -> CLOCK_MONOTONIC, queue) ─┐
ModemManager ─GetLocation (NMEA)─▶ phone-gnss thread ─▶ PhoneGnssSource (queue) ─────────────────────┤
                                                       SourceSelector (priority, hysteresis) ◀────────┘
                                                              │
PositionFeeder thread, motion_hz (10) ────────────────────────┴──▶ PositionFeed ──▶ /run/roadsense/position.sock (mapd)
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
| `yaw_rate_rps` (opt-in, `BACKEND_POSITION_YAW=wheels,gyro`) | CAN 0x022, two channels | mean of ((256A + B) − 0x200) and ((256C + D) − 0x200), each minus its standstill offset, × 0.0044 rad/s, positive = left; the offsets (−2 / −7 counts on 2026-11-17) follow the reading whenever speed and all wheels are exactly 0 for 1 s (time constant 3 s) | scale probable, not calibrated: ratio to the rear wheel-speed yaw 0.0042–0.0046 over 15–80 km/h (docs/prius_can.md). mapd uses `yaw_rate_rps` before the wheel speeds, so a 5 % scale error becomes a 5 % heading error per turn until a fix or the map corrects it: not the default until the 360° calibration is done. One count is 0.25 °/s (the wheel-speed difference resolves 0.11 °/s); the gain is at low speed, at standstill and where the rear wheels slip or differ. 0x03A is not a yaw rate |

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

Not corrected: the wheel speeds read 2.2 % below 0x0B4 (the yaw scales with it). The 0x025 decoder
(`decode_steering_deg` in `comm/can_decoder.py`) is the same for the feeder and the state's
`dynamics.steering_angle`.

## 4. Head-unit GNSS contract (to build on the head unit)

Transport: HTTP `POST http://<phone>:8080/api/v1/position/gnss`, `Content-Type: application/json`,
`Authorization: Bearer <BACKEND_AUTH_TOKEN>` (the car computer has a token since 2026-11-15; without it
the route answers 401). Port 8080 is firewalled to `lo`, `wg*`, `usb*` and the car AP address
10.42.0.1 (`POCO/rootfs/etc/nftables.d/50_prius_backend.nft`, [AUTH.md](AUTH.md)): the phone's Wi-Fi AP
works as is; the planned USB RNDIS link ([HEADUNIT_INTEGRATION.md](HEADUNIT_INTEGRATION.md) §3) needs its
interface named `usb*` (udev rule, like the MFD's `usb0`) or added to the guard.

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
`{"detail":{"reason":"source_not_headunit"}}` when the feeder runs no head-unit source (a list
`headunit,phone` accepts the POST; `phone` alone does not). The route exists
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

### 5.1 Synthetic session at run time (dashboard)

`POST /api/v1/position/synthetic` with `{"on": true}` / `{"on": false}` (bearer token or the dashboard
session, like every route) swaps the configured source for a synthetic drive while the backend runs
(`PositionFeeder.start_synthetic` / `stop_synthetic`). For testing without GNSS, car parked.

| Rule | Behaviour |
|---|---|
| start refused (409 `{"detail":{"reason":...}}`) | `roadsense_not_running` (feeder inactive), `car_moving` (fresh 0x0B4 above 0.5 m/s), `not_in_park` (fresh 0x120 gear other than P) |
| start refused (403) | `control_disabled` (`BACKEND_POSITION_SYNTHETIC_CONTROL=0`), `configured` (the env source is already `synthetic`; that one is not switchable) |
| CAN dark (car off, bench) | counts as parked: start allowed |
| session ends | the real car moves (the session ends instead of suspending; the real CAN motion goes out in the same cycle), roadsense stops (feeder inactive), after `BACKEND_POSITION_SYNTHETIC_SESSION_S` (1800 s; a second start renews), or `{"on": false}` |
| on the end | the configured source is back at once and sends `fix: none` (the head unit has no fix); `last_session_end` = `{reason, at}` |
| position reset | on the start and on every end (any reason above), the feeder owes mapd a `{"type":"reset","reason":"synthetic session started"}` / `"synthetic session ended: <reason>"` and sends it before the new source's first GNSS message, once its connection's hello reply lists `reset` in `accepts` (roadsense mapd since 2026-11-15). An older mapd (`accepts` without `reset`) is never sent one (`last_reset.sent` false, `why` `not_accepted`); with mapd away or the reply not yet read it stays owed (`reset_pending`) and goes out on the next connection. Renewing a running session is no source change and sends none |
| while it runs | `POST /gnss` answers 503 `source_not_headunit` |

Residue with an older roadsense (mapd before 2026-11-15, no `reset` in the position input API): after a synthetic
session mapd keeps dead-reckoning from the synthetic place (measured with the live harness: `dead_reckoning`,
`available` true, `input.gnss.source` `headunit` 4 s after the stop) until a real fix arrives or dead reckoning
runs out (vision odometry 120 s; vehicle motion up to 900 s / 25 km while CAN motion flows). The scene feed
therefore marks the scene `map` block `syn: true` from the first synthetic fix until mapd reports
`no_source` / `unknown` or a non-synthetic fix is in use (`roadsense_scene.SyntheticTaint`), and the MFD ignores
a `syn` map ([MFD_MAIN_SCREEN.md](MFD_MAIN_SCREEN.md) 7.2). A backend restart during the residue forgets the
taint. The same residue follows the env-configured synthetic source when it suspends for real motion (no
session ends there, so no reset is sent). With a mapd that accepts `reset` the feeder's reset ends the residue:
mapd answers `no_source` until the next real fix (and treats a synthetic <-> real change of the GNSS source tag as
an implicit reset of its own), which also clears the taint; the taint stays as the guard for the older mapd and for
a backend restart. Checked against the real mapd of the roadsense checkout (862bee3) on the dev box:
`tests/test_position_mapd_e2e.py::test_session_end_sends_reset_and_mapd_forgets_the_place` (the map is
`no_source` right after the stop, `input.last_reset.reason` is the feeder's); not yet on the phone.

## 6. Status (`state.roadsense.position`, `GET /api/v1/position`)

Mirrored on change, at most every 2 s; `null` while the feeder is off.

| Field | Meaning |
|---|---|
| `enabled`, `source`, `synthetic` | feeder on; the configured source (`headunit`, `phone`, `headunit,phone`, `synthetic`, `none`) |
| `gnss_source` | the source whose fixes go to mapd now (`headunit` / `phone` / `synthetic`), null without a fix |
| `active` | running now (roadsense up) |
| `suspended` | `car_moving` (synthetic only) or null |
| `connected`, `socket`, `connects` | connected to mapd's `position.sock` |
| `sent`, `dropped` | messages written / dropped (mapd away or socket full) |
| `motion_sent`, `gnss_sent`, `none_sent` | per kind |
| `errors`, `last_error` | error replies from mapd |
| `reset_sent`, `reset_pending`, `last_reset`, `mapd_accepts` | position resets sent to mapd; one is owed (5.1); `{reason, at, sent, why?}` of the last one handled; mapd's hello `accepts` on this connection (null before the reply) |
| `base_source`, `synthetic_control` | configured source; the run-time synthetic switch exists (5.1) |
| `session`, `last_session_end` | running synthetic session `{by, started, until}` (wall-clock s) or null; `{reason, at}` of the last one |
| `motion.yaw_input` | `wheels`, `steer`, `wheels+steer` or null in the last motion message |
| `motion.can_age_s`, `motion.can_frames` | age of the newest 0x0B4, frames seen by the tap |
| `gnss.has_fix`, `gnss.last_fix_age_s` | source has a usable fix; age of the newest fix |
| `gnss.*` (head unit) | `received`, `accepted`, `rejected`, `none`, `time_age` / `time_utc` / `time_arrival`, `last_reject`, `fix`, `hacc_m`, `sats` (no coordinates) |
| `gnss.*` (synthetic) | `fixes`, `route`, `route_m`, `along_m`, `laps`, `speed_mps`, `dropout` |
| `gnss.*` (phone) | `wanted`, `gnss_on`, `modem` (MM object path), `backend` (`dbus` / `mmcli`), `ttff_s`, `fix`, `searching`, `sats`, `sats_view`, `hdop`, `hacc_m`, `cn0_max`, `cn0_top4`, `tracked`, counts `polls` / `epochs` / `fixes` / `no_fix_epochs` / `searching` / `malformed` / `errors` / `setups` / `resetups` / `time_utc` / `time_rx`, `last_error` |
| `gnss.*` (list) | `selected`, `order`, `switches`, `last_switch` `{from, to}`, `last_fix_age_s`, `sources` = `{name: that source's fields}` |

`GET /api/v1/position` only (never in the state mirror) adds `raw_fix` (`lat`, `lon`, `hacc_m`, `age_s`,
`source` of the newest raw fix of the current source; null before one) and `pack_file` (`path`, `file`,
`size_bytes`, `mtime` of `BACKEND_ROADSENSE_MAP_PACK`, default `/var/lib/roadsense/maps/current.rsmap`, symlink
resolved; null when absent): mapd's `map.pack` has region and OSM date but no size.

The roadsense manager's unit mirror (`state.roadsense.units`) now includes `roadsense-mapd.service`.

## 7. Configuration and deploy steps (not executed)

| Env (`/etc/prius/backend.env`) | Default | Meaning |
|---|---|---|
| `BACKEND_POSITION_FEEDER` | `0` | `1` turns the feeder on |
| `BACKEND_POSITION_GNSS` | (unset) | GNSS sources in priority order: `headunit`, `phone` or a list `headunit,phone`; `synthetic` / `none` only alone. An invalid value keeps the feeder off (logged) |
| `BACKEND_POSITION_SOURCE` | `headunit` | older name, used when `BACKEND_POSITION_GNSS` is unset |
| `BACKEND_POSITION_SWITCH_UP_S` | `5` | a higher-priority source needs a fix this long before it takes over |
| `BACKEND_POSITION_PHONE_UERE_M` | `4` | phone: `hacc_m` = HDOP × this (not calibrated) |
| `BACKEND_POSITION_PHONE_LINGER_S` | `120` | phone: GNSS stays on this long after roadsense stops |
| `BACKEND_POSITION_PHONE_POLL_HZ` | `4` | phone: ModemManager `GetLocation` calls per second |
| `BACKEND_RECORD_INCLUDE` token `position` | not set | trip files also get the fixes sent to mapd (section 12); `all` does not include it |
| `BACKEND_POSITION_RUNDIR` | `/run/roadsense` | where `position.sock` is |
| `BACKEND_POSITION_MOTION_HZ` | `10` | 5..50 |
| `BACKEND_POSITION_YAW` | `wheels` | comma list of `wheels`, `steer`, `gyro` (0x022 as `yaw_rate_rps`); empty = speed and gear only |
| `BACKEND_POSITION_FIX_TIMEOUT_S` | `3` | head unit silent this long = no fix |
| `BACKEND_POSITION_SYNTHETIC_ROUTE` | built-in | route file |
| `BACKEND_POSITION_SYNTHETIC_DROPOUTS` | none | `t0:t1,...` |
| `BACKEND_POSITION_SYNTHETIC_CONTROL` | `1` | `0` removes the run-time synthetic switch (5.1) |
| `BACKEND_POSITION_SYNTHETIC_SESSION_S` | `1800` | 60..14400: a run-time session ends after this |
| `BACKEND_ROADSENSE_MAP_PACK` | `/var/lib/roadsense/maps/current.rsmap` | pack file whose size `GET /api/v1/position` reports |

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
5. Phone GNSS as fallback (section 11.5): add `BACKEND_POSITION_GNSS=headunit,phone`; restart
   `prius-backend.service`. Check with the stack running: `gnss.sources.phone.gnss_on` true,
   `mmcli -m any --location-status` shows `gps-nmea` enabled; 120 s after roadsense stops it is
   off again (`3gpp-lac-ci` only).

## 8. Tests (2026-11-15, dev box)

| Test | Result |
|---|---|
| `tests/test_position_feeder.py`: decoders on real frames, tap freshness / pairing / field order, head-unit validation and clock, feeder cycle (no fix → `none`, repeats, fix forwarded, silent with CAN dark, quiet drop without mapd, synthetic suspension), synthetic drive consistency, PositionFeed on a Unix socket (hello, error replies, reconnect), `fused` → scene, REST routes with a token, env parsing, service wiring / gating | 23 passed |
| `tests/test_position_mapd_e2e.py` with roadsense `.venv-models` (fake camera daemons `--no-mapd` + real mapd + real api) and `dev-opolskie.rsmap`: no fix | mapd `no_source`, `available` false, `reason` `no_position`, `input.gnss` `headunit`/`none`, motion `vehicle`/`prius-can`; api `fused.map.available` false, limit source `sign` (vision); scene `map.ok` false, `why` `no_position`, `map.limit` = scene `limit` (50 km/h B-33 from the fake signs) |
| same, synthetic drive with a fix dropout at 15–20 s | `good` + matched; position error against the synthetic truth after 8 s: max 4.1 m, mean 2.5 m; `dead_reckoning` exactly in the dropout, `available` throughout (13/13 samples), `good` again after it; `input.gnss.source` `synthetic`, motion tag `synthetic`; scene `map.ok` true, `map.in.gnss` `synthetic`, road `Mały Rynek` (living street) |
| `tests/test_phone_gnss.py` (2026-11-15): NMEA checksum, full epoch (position, speed, course, date, DOPs, GSV C/N0), searching trace as the modem sends it, malformed input (bad checksum, minutes ≥ 60, hemisphere, hour 25, 0,0, truncated, junk), GGA/RMC of different epochs, talker preference, `hacc_m`; phone lifecycle with a fake ModemManager (setup keeps other sources, linger, restore only what was added, owner-enabled GNSS left on, MM restart with a new path, silent setup loss, no capability), fix time from UTC / receipt, epoch dedupe, thread start / stop, mmcli fallback; selector fallback / hysteresis / old-fix drop / flicker; feeder with `headunit,phone` incl. REST ingest forwarding; env list parsing. `tests/test_trip_recorder.py`: position off by default, recorded with the token, skipped by replay | 18 + 3 passed |
| full suite after the phone source (worktree, fixture copied in) | 1266 passed, 9 skipped |
| position reset (2026-11-15): `tests/test_position_synthetic_control.py` (reset before the new source's first fix at start and at each kind of end, none to an older mapd, owed until connected and the hello reply is read), `tests/test_position_feeder.py` (hello `accepts` read on a Unix socket, per connection), `tests/test_position_mapd_e2e.py` (real mapd) | 4 + 1 + 1 new, passed |
| full suite `pytest tests --ignore=tests/test_pointer_ui.py` | 1155 passed, 9 skipped, 5 failed: the 5 (`test_bean_rx`, `test_bean_tx`) need `tests/fixtures/bean_nhw20_frames.txt.gz`, which exists only untracked in the main checkout; with that file copied in, both files pass (162) |

## 9. Open gaps

* No real GNSS source: the head-unit side (agent app sending §4) does not exist; its fix latency,
  `age_ms` availability and accuracy fields are untested. The phone's receiver got no fix in the car
  (section 11.3); a fix, its accuracy, latency and the UERE factor are unmeasured.
* Motion is untested against a running mapd on the car; timestamps are panda receipt times (bus
  delay assumed a few ms, not measured).
* Absolute sign of 0x025 and the left/right labels of 0x0B1/0x0B3 rest on third-party conventions
  (consistent with each other and with the turn-tightness asymmetry; see §3). One logged deliberate
  left turn, or a GNSS course, settles it.
* 0x0B1/0x0B3 read 2.2 % below 0x0B4; 0x0B1/0x0B3 bytes E,F and the 0x025 fraction nibble are not
  decoded.
* The yaw-rate sensor (0x022) is decoded but opt-in (`gyro`): its scale is not calibrated yet.
* Replay mode (`--replay`) has no panda, so no motion; the feeder is a live-car feature.
* Steering ratio / wheelbase in mapd are nominal (19.1 / 2.70 m); this car's effective ratio is
  speed-dependent (21–29, §3).
* The MFD draws only the fused speed limit from the scene `map` key ([MFD_MAIN_SCREEN.md](MFD_MAIN_SCREEN.md) 4).
* No raw-fix trail: the dashboard trail is of filtered positions, kept by the browser while the MAP tab is open.
* The reset (5.1) is checked against mapd on the dev box only; the phone's mapd gets it once roadsense
  862bee3 or later is deployed there (until then the feeder sends none, `why` `not_accepted`).
* roadsense reference client gap (reported, roadsense not changed): `tools/position_client.py`
  counts a short non-blocking `send` as sent and leaves a partial line in the stream; the copy here
  closes the connection instead.

## 10. Dashboard MAP tab (`dashboard/src/tabs/MapTab.tsx`)

Tab `#map` of the web dashboard, built 2026-11-15. Data: the roadsense front (`/api/v1/roadsense/map`,
`/fused`, `/health`) polled once a second, `GET /api/v1/position` every 2 s, only while the tab is open,
the page visible and the roadsense manager is `starting` / `running` / `grace`; otherwise nothing is
fetched and the tab says `roadsense not running` (feeder panel from the state stream only).

| Panel | Content |
|---|---|
| banners | `roadsense not running` / `api not answering`; `SYNTHETIC POSITION` (feeder synthetic or mapd hears `synthetic`); `synthetic residue` (dead reckoning within 15 min of a session end); `VISION ONLY` + roadsense's reason; `DEAD RECKONING` + distance; `MAP + CAMERA` + match confidence |
| Position & Match (MAP-02) | heading-up sketch drawn only from roadsense output (no tiles, no map data in the page): origin = matched point, up = matched bearing (the frame of `ahead.shape`); road shape ahead, curves, tunnel, junctions (dim when minor), features, limit changes, built-up edge; filtered position as a chevron with its accuracy circle, raw fix (REST) as a cross with its accuracy, this browser's trail; ranges 60 / 250 / 600 m. Readouts: filtered (source, accuracy, heading, speed, fix age, DR distance, odometer, lat/lon), matched (road, class, confidence, offset, heading diff, candidates, ambiguous), raw fix (source, age, accuracy, raw->filtered and filtered->matched distances) |
| Map Layer (MAP-01) | mapd unit state (manager mirror), mapd connected to the api, pack region + OSM date, pack file + size, availability + reason, match confidence; mapd input: GNSS source / fix / age / accuracy / sats, used / ignored / rejected, filter resets, motion source / tag / age, input errors, mapd CPU |
| Fused Facts (FUS-01) | speed limit, built-up, place, lanes, tunnel: fused value, source, vision and map inputs, agreement (conflict rows tinted red). Lanes agreement is the tab's own check (own-direction lanes implied by the camera's lane flags against the map's forward count). While a camera sign is in force with the map: `sign` / `sign_held` and what releases it, with distances: next non-minor junction (none on motorways / expressways), next tagged limit change, built-up boundary (landuse marked inferred), 5 km |
| Road Ahead (AHD-01) | current road (ref, name, class, flags, lane count, map limit), then junctions, curves (radius, angle, length), features, limit changes, tunnel entry / exit, built-up boundary, road change, nearest first, 12 shown, `+n more` |
| Position Feeder (POS-01) | the status fields of section 6; synthetic test drive box: state, time left, route progress, last end reason, START (two taps: arm, confirm) / STOP; START disabled when the switch is off, the feeder idle or the state speed above 1 km/h; the backend decides (409/403 reasons are shown) |

Overview tab: a `Map Layer` card (MAP-00) from the roadsense health mirror (`state.roadsense.health.map`
/ `.fused`, now mirrored by `roadsense_power`), no polling.

Checked on the dev box with the live harness (roadsense fake camera daemons + real mapd on
`dev-opolskie.rsmap` + real api, the backend's real `create_app` with this feeder, a run-time synthetic
session with a fix dropout, headless Chrome): states `no_source`, `good`, `dead_reckoning` (80 m DR in the
dropout), residue after the stop, roadsense off. Not checked: a real head-unit fix, the phone browser
itself, the car.


## 11. Phone GNSS source (`phone`, `backend/phone_gnss.py`)

Built 2026-11-15. The phone's own receiver as a stop-gap until the head unit sends fixes.

### 11.1 What the phone offers (read on the phone 2026-11-15, mainline kernel, ModemManager 1.25.95)

| Item | Finding |
|---|---|
| Mechanism | GNSS engine inside the modem, reached by QMI LOC; ModemManager (`qcom-soc` plugin, `qrtr0`) owns the LOC client and offers it as `Modem.Location`. No `/dev/gnss*`, no gpsd / geoclue / gnss-share installed, no `gps-unmanaged` capability |
| MM capabilities | `3gpp-lac-ci, gps-raw, gps-nmea, agps-msa, agps-msb`; enabled by default: `3gpp-lac-ci` only; GPS refresh rate 30 s (MM default) |
| Engine (qmicli, read-only gets) | engine lock `none`; operation mode `standalone`; NMEA types `gga rmc gsv gsa vtg pqxfi pstis glgsv gngsa gngns garmc gagsv gagsa gavtg gagga pqgsa pqgsv` (GPS, GLONASS, Galileo; no GST, so no receiver accuracy figure) |
| Assistance | XTRA: the modem knows three `xtra3grcej.bin` servers, its XTRA data is invalid (1980-01-06, 0 h); ModemManager reports `SupportedAssistanceData` = 0, so `--location-inject-assistance-data` is not available. SUPL server empty (MSB A-GPS would need one written into the modem: a modem configuration change, not made). Time injection (`qmicli --loc-inject-time`) works |
| Rate | the engine sends one NMEA burst per second (1 Hz) while searching; whether it can do more is unknown (no fix) |

### 11.2 Mechanism chosen

ModemManager over D-Bus (dbus-python, system package `py3-dbus`, already in `POCO/packages.txt`;
the backend venv has system site-packages). It is the only path that does not compete with
ModemManager for the modem that also carries LTE: gpsd or qmicli would need their own LOC client and
a session of their own. In-process D-Bus calls cost far less than forking `mmcli` per poll; `mmcli`
subprocesses are the fallback when dbus-python is missing (1 Hz).

| Rule | Behaviour |
|---|---|
| on | when the feeder becomes active (roadsense `starting`, ≈ 13 s after key-on, see [POCO/POWER.md](../POCO/POWER.md)): `Location.Setup(enabled \| gps-nmea)` keeping the other sources, `SetGpsRefreshRate(0)` |
| off | `BACKEND_POSITION_PHONE_LINGER_S` (120 s) after the feeder goes idle: only the bits this source added are cleared, the refresh rate is restored. A GNSS the owner enabled by hand stays on |
| poll | `GetLocation` at 4 Hz on the `phone-gnss` thread; one epoch per GGA time (repeats ignored) |
| time | fix time = receipt − (wall clock − NMEA UTC) when the clock is NTP-synced and the difference is −0.2..2.5 s (`time_utc`), else receipt − 0.5 s (`time_rx`) |
| `hacc_m` | GST sigmas when present (never on this modem), else HDOP × 4 m, floor 2 m |
| course | sent only from 1 m/s (receiver course at walking pace is noise) |
| no fix | `has_fix` false 3 s after the last fix; the feeder sends `fix: none`, roadsense runs on vision |
| MM restart / modem reset (netwatch) | every error resets the D-Bus connection, re-finds the modem (its path changes) after 5 s and sets up again (`resetups`); the setup is re-checked every 30 s |
| never | modem reset / enable / disable, bearers, SIM, restarting ModemManager or NetworkManager; positions in logs above DEBUG |
| backend never blocks | all D-Bus I/O on the `phone-gnss` thread; the feeder only reads a queue |

### 11.3 Measured in the car (2026-11-15, 19:18–19:46 CET, parked outdoors, phone in its box, car off)

GNSS on (gps-nmea + gps-raw, refresh rate 0) for 20 min, polled at 1 Hz, with 3 min off before and
after.

| Item | Result |
|---|---|
| satellites | none tracked in 20 min: no GSV sentence at all (GSV is in the enabled type list), GSA mode 1, GGA quality 0 |
| receiver time | no time from the sky; the GGA time appeared only after a UTC time injection (`qmicli --loc-inject-time`) at 9.6 min, then 1 Hz epochs (609 in 10.4 min), still quality 0 |
| C/N0, sats in view, accuracy, scatter, TTFF, latency | not measurable (no satellite) |
| LTE / tunnel | unaffected: 120/120 pings to the hub (every 10 s) with GNSS on, 18/18 before, 15/15 after; MM `connected` / `lte` throughout |
| power (`powerbox.poco_power_w`, profile `low`) | median 0.58 W before, 0.735 W with GNSS searching, 0.58 W after: ≈ +0.15 W (mean 0.65 → 0.79 W) |
| left as found | `gps-nmea` / `gps-raw` disabled, refresh rate 30 s, enabled `3gpp-lac-ci` only; the injected time is engine state only |

Unresolved: whether the box blocks the sky (the car is outdoors; LTE is fine) or the modem's GNSS RF
path does not work on this kernel (reports for SDM845 phones differ by device). One test settles
it: the phone out of the box with sky view, GNSS on for 5 min (`mmcli -m any
--location-enable-gps-nmea`, `mmcli -m any --location-get` shows `GSV` with C/N0 if anything is
tracked). Without any satellite, XTRA data would not help.

### 11.4 Source selection (`SourceSelector`)

`BACKEND_POSITION_GNSS=headunit,phone`: both run; every cycle both are drained and only the selected
one's fixes go to mapd, with `"source"` = `headunit` / `phone`.

* nothing selected or the selected source lost its fix (3 s silence, or head unit `fix: none`): the
  first source in the list with a fix, at once;
* a higher-priority source takes over a working one only after `BACKEND_POSITION_SWITCH_UP_S` (5 s)
  of continuous fix (a flickering head unit does not toggle);
* a fix not newer than the last one sent is dropped (receivers with different latencies);
* `synthetic` and `none` never take part; the run-time synthetic session replaces the whole list and
  gives it back on the end; `POST /gnss` keeps working with the list.

Status: `gnss_source` (section 6) and `gnss.selected`, `gnss.sources.<name>`; `GET /api/v1/position`
`raw_fix` is the selected source's.

### 11.5 With the head unit later

Keep `headunit,phone`: the head unit (better antenna) wins whenever it delivers fresh fixes; the phone
covers head-unit gaps (app restart, USB link) if it ever gets a fix in its mounting place. If the
phone never gets a sky view, `BACKEND_POSITION_GNSS=headunit` saves the ≈ 0.15 W of a GNSS that
searches for nothing. Other receivers (USB u-blox on the phone's hub, a GNSS satellite) would be one
more source in the list; `phone_gnss.parse_trace` parses their NMEA as is.

## 12. Location in recordings

What the trip recorder stores (`io/trip_recorder.py`): the bytes of the gateway, satellites,
powerbox and CAN through the ingress / egress callbacks (`BACKEND_RECORD_INCLUDE=all` on the car).
The feeder's GNSS (head-unit POSTs, phone, synthetic) never passed through the ingress, so before this
change no trip file held a position, and the state mirror holds none either (the feeder status has no
coordinates; `raw_fix` is REST only).

Now an explicit option: the token `position` in `BACKEND_RECORD_INCLUDE` (e.g. `all,position`) writes
every fix sent to mapd into the open trip file as `{"ts":…,"dir":"POS","id":0,"d":{<gnss message>}}`.
It is never part of `all`, never opens a trip and does not keep one alive; replay
(`FileInputPort`) skips `POS` lines. Default: off. Reason: `all` was chosen when trips held bus
traffic only, and a location track is personal data of a different kind; recording it should be a
decision, not a side effect of a new source.
