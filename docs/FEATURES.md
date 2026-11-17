# Feature list

Every function of the smart-functions catalogue (2026-11-15) with its status in this repository.
Framework and alert details: [ALERTS.md](ALERTS.md). Status values: `have`, `built, shadow` (runs,
logs only until the owner switches it on), `built, disabled: waiting for X`, `in progress (separate
lane)`, `planned` (with the first step), `not planned` (with the reason), `waiting: sensor not
installed`. Scope rule: information, warnings and comfort control only; nothing steers, brakes or
accelerates.

## 1. Driver information and warnings

| Function | Status | Needs | Code | Note / first step |
|---|---|---|---|---|
| Alert framework (banner, arbitration, shadow mode, log, dashboard) | have | — | `alerts/`, `ui/scene/alerts.py`, dashboard ALERTS | every new type ships in shadow |
| Audible alert channel | built, disabled: waiting for a sound output | head-unit agent or piezo | `alerts/sinks.py` (null sink) | alerts carry a sound id; first step: head-unit agent with `SoundPool` |
| Speed-limit warning (ISA-like, true speed) | built, shadow | fused / camera limit, CAN 0x0B4 | `alerts/functions/speed_limit.py` | map limit needs GNSS; vision-only today |
| True-speed chip near the limit | built, shadow | as above | `speed_limit.py`, `ui/scene/alerts.py` | shown with the limit notice only |
| Speed-limit preview ("90 in 650 m") | planned | GNSS (map `ahead.limit`) | — | first step: chip in `render.py` from scene `map.ahead.limit` (MFD_MAIN_SCREEN 8.3) |
| Forward collision / closing-speed warning (advisory) | built, shadow | lead `d`, `v_rel`, true speed, brake, steering | `alerts/functions/headway.py` (`distance_warning`) | not a certified FCW |
| Headway / tailgating coach | built, shadow | lead, true speed | `headway.py` (`headway_coach`) | ≥ 50 km/h only |
| Lead-vehicle departure | built, shadow | lead (tracked or vision), true speed, gear | `alerts/functions/stop_go.py` | uses roadsense's tracked lead (topic `lead`, else `fused.lead`) and its `departed` cue when present |
| Traffic-light green reminder | built, disabled: waiting for validated traffic-light reliability | ego light with relevance | `stop_go.py` (`green_light`, shadow only) | first step: review its shadow log against dashcam |
| Red-light / stop ahead highlight | planned | ego light, `stop_ahead` | — | first step: alert type on approach speed vs distance (visual only) |
| Lane departure warning | built, disabled: waiting for turn-signal state | turn signal, lanes | `alerts/functions/lane.py` | refuses `on`; shadow for data |
| Curve speed notice | built, shadow | GNSS + map look-ahead | `alerts/functions/curve.py` | inactive without a matched map position |
| Ice / frost risk | built, shadow | outside temperature (AVC E0 5D F7 / BEAN FE:CD) | `alerts/functions/winter.py` | OBD 0146 not used (not usably connected) |
| Rear-defogger hint (cold start) | built, shadow | outside temp, defogger state (AVC F3) | `winter.py` | hint only, no press |
| Wet-road / rain awareness | waiting: sensor not installed | RLS rain level in the car | — | bench decode exists (vag-rls-lin) |
| Fogging / dew assist | waiting: sensor not installed | RLS dew point / glass temperature | — | — |
| Drowsiness: time-based break reminder | planned | drive time, distance | — | first step: alert type on `drive_started_at` (2 h / 200 km) |
| Drowsiness: steering entropy / lane variance | not planned | — | — | 1.5° steering resolution, no validation data |
| Driver distraction (driver camera) | not planned | IR camera + model | — | CPU budget, privacy |
| Wrong-way warning | planned | GNSS + oneway | — | first step: shadow type on motorway ramps only, very high match confidence |
| Front cross-traffic | not planned | corner radars | — | no sensors |
| Blind-spot monitoring | not planned | rear radars | — | camera-only BSM unreliable |
| Rear cross-traffic | not planned | rear radars | — | no sensors |
| Door exit warning (cyclists) | not planned | rear sensing | — | no sensors |
| Parking sensors / distance arcs | planned | ultrasonic kit on a satellite | — | first step: sensor choice; drawing slot in MFD §9 |
| Tyre pressure monitoring | planned | BLE TPMS sensors | — | first step: BLE TPMS sensors via `prius-bled` |

## 2. Navigation and map

| Function | Status | Needs | Code | Note / first step |
|---|---|---|---|---|
| Map layer (mapd, fused limit, look-ahead) | have (roadsense) | GNSS for real use | roadsense mapd; `backend/position_feeder.py` | verified on synthetic drives only |
| Predictive eco coach (lift-off hint) | built, shadow | map look-ahead or camera warning signs, true speed, accelerator | `alerts/functions/eco.py` | built-up boundary ahead not in the scene yet |
| Predictive battery use (hints) | planned | GNSS, SOC | — | first step: info type "EV-friendly stretch" on built-up ahead + SOC |
| Range estimate | built, shadow | fuel level 0x5A4, consumption | `alerts/functions/maintenance.py` (`range_low`) | petrol only; LPG not visible |
| Geofenced behaviours | planned | GNSS | — | first step: geofence input in `alerts/inputs.py` |
| Navigation | have | head unit (ZLink) | — | — |
| Traffic / hazard info | not planned | — | — | via projection apps only |
| Speed-camera POI | planned | GNSS + open POI list | — | first step: licence check of a POI list |
| Tunnel detection | have (roadsense fused fact) | map or envd | scene `map.tunnel` | used by the tunnel automation |

## 3. Comfort automation

| Function | Status | Needs | Code | Note / first step |
|---|---|---|---|---|
| Automatic climate | have (factory) | — | — | — |
| Tunnel auto-recirculation | built, shadow | fused tunnel fact, AVC climate status + remote | `alerts/functions/tunnel.py` | the only actuation; steps back on any driver touch |
| Air-quality recirculation | planned | PM/VOC/NOx sensor at the cowl | — | first step: sensor on a satellite; same automation pattern |
| Remote pre-conditioning | not planned | — | — | climate needs READY; not reachable remotely |
| Rain-sensing wipers | not planned | — | — | visibility-critical actuation |
| Automatic headlights / high-beam assist | not planned | — | — | owner rule: lights left alone; reminder possible |
| Lights reminder (tunnel/dark, low beam off) | planned | envd light, lamp bits | — | first step: info alert type, visual only |
| Welcome / leaving lighting | not planned | — | — | owner rule |
| Auto lock while driving / walk-away lock | not planned | — | — | no BEAN lock command; hard-wiring locks is a safety risk |
| Seat / steering memory | not planned | — | — | manual seats |
| Driver profiles | planned | phone BLE presence | — | first step: BLE presence in `prius-bled` |
| Cabin over-temperature / rear-seat reminder | planned | BMP280 cabin temp or BEAN `52:6D` (`climate.inside_temp`, IG on only, offset G), rear-door bit | — | first step: parked event type on `powerbox.bmp2_t` |

## 4. Energy and hybrid

| Function | Status | Needs | Code | Note / first step |
|---|---|---|---|---|
| Eco score / energy displays | have (energy flow, HV kW strip, history, fuel figures) | — | `ui/scene/powerbox.py` | — |
| Regen-vs-friction braking score | built (statistic, always logged) | 21C3 regen + master-cylinder torque, brake | `eco.py` (`stat eco_stop`), dashboard ALERTS | units unverified: relative figure |
| EV-mode hints | planned | EV state, map | — | first step: info type |
| Warm-up awareness | planned | coolant temperature | — | first step: info type at READY below 40 °C |
| Fuel and trip statistics | have | — | metrics, trip fuel rules | LPG gap |
| Fuelling log (LPG + petrol fills, full-to-full l/100 km, energy-equivalent combined figure, cost/100 km) | have | backend odometer | `fuel_log/`, `ui/screens/fuel_log_screen.py` | [FUEL_LOG.md](FUEL_LOG.md); fills entered by the driver (MFD or `fuel_log_add`) |
| HV battery health trends | in progress (separate lane) | 21CE/21D0 | — | `feat/battery-health` |
| Inverter cooling health | planned | 21C3 temperatures, MG2 power | — | first step: trend metric in the battery-health style |
| 12 V battery health (trend) | in progress (separate lane) / partly built | INA219 | `guardian.py` (`low_12v` resting event) | trend belongs to the health lane |

## 5. Connected services

| Function | Status | Needs | Code | Note / first step |
|---|---|---|---|---|
| Remote status | have | — | dashboard over LTE + WireGuard | — |
| Push notifications | built, disabled: waiting for a push channel | notification sink | `alerts/sinks.py` (null sink) | recommended: phone → LTE → self-hosted push topic (ALERTS.md 1.5) |
| Parked-car guardian (unlocked, door, lamps, 12 V, intrusion) | built, shadow | BEAN car-off frames, INA219 | `alerts/functions/guardian.py` | events only; dashboard list |
| Theft / tow alert (moved) | planned | GNSS or phone IMU | — | first step: position delta while parked |
| Vehicle finder | planned | GNSS | — | — |
| Trip logbook | planned | GNSS | — | — |
| Maintenance reminders | built, shadow | odometer (BEAN 52:3F), owner-entered intervals | `maintenance.py` (`service_due`), dashboard "done now" | intervals in `alerts_tuning.json` |
| DTC notice | built, shadow | existing DTC scans | `maintenance.py` (`new_dtc`) | no new polling |
| Over-the-air updates | have (own software) | — | deploy tooling | car ECUs: not planned |
| Dashcam / sentry recording | not planned (sentry) / planned (driving recorder in roadsense) | — | — | sentry drains the 12 V battery |
| eCall-like crash detection | not planned | — | — | no airbag signal; false 112 calls are harmful |

## 6. HMI

| Function | Status | Needs | Code | Note / first step |
|---|---|---|---|---|
| Alert banner on the MFD | have | — | `ui/scene/alerts.py` | main screen; other screens: planned (app-level overlay) |
| Cruise state on the MFD (main switch, engaged, set / resume speed) | have | `21D3` poll, BEAN `62:C8`, CAN 0x5C8 / 0x120 | `io/cruise_fusion.py`, cruise slot in `ui/scene/powerbox.py`, `SET nn` chip | decoded 2026-11-16/17; reference incl. tap step (1 mph), state machine and the stalk switch bits in `21D3` E: [CRUISE_CONTROL.md](CRUISE_CONTROL.md); the switches are on no broadcast bus |
| Main screen: road view while stopped in traffic | have | gear, READY / ACC, doors | `ui/scene/director.py` | parked view only in P, with power off or a door open (2026-11-16; was: after 2.5 s standing) |
| HUD-style VFD display of alerts | planned | VFD satellite | — | first step: `alert` event to the VFD app |
| Voice control | have (head unit) | — | — | own STT not planned |
| Steering-wheel keys (acknowledge alerts) | planned | head-unit agent | — | first step: map a key to `input_event mfd/alert_ack` |
| Personalisation | have | — | — | — |

## 7. Data gaps (ranked by what they unlock)

| # | Gap | Unlocks | How to close it | Effort |
|---|---|---|---|---|
| 1 | GNSS position source (**closed 2026-11-17**: head-unit GPS over adb, `prius-gnss-bridge.service`; drive evaluation open) | map limit for ISA, curve notice, eco hints with the map, tunnel by map, limit preview, wrong-way, geofences, finder, logbook, tow alert, POI | phone GNSS on postmarketOS (lane `feat/phone-gnss`), USB u-blox on the hub, or the head-unit agent posting `/api/v1/position/gnss` (ADB tooling and the app-less `hu-gnss-bridge` ready since 2026-11-17, untested on the unit: [HEADUNIT_INTEGRATION.md](HEADUNIT_INTEGRATION.md) §7) | S–M |
| 2 | Sound output (**closed 2026-11-17**: head-unit agent app + `HeadunitSoundSink`, HEADUNIT_INTEGRATION.md 8; audibility in the cabin to confirm) | every alert type's sound id | head-unit agent (Android sound with ducking) or a piezo on a satellite / spare powerbox output, behind `SoundSink` | S–M |
| 3 | Turn-signal state | lane departure (enable), lane-change awareness, indicator-left-on reminder, roadsense junction intent | **planned (owner, 2026-11-17): a satellite reading the two indicator lamp lines (opto-isolated) and reporting left/right lamp levels** over the satellite transports (docs/SATELLITES.md). Not on CAN or BEAN (checked 2026-11-17; hazards with IG on still to check, BEAN_SIGNALS.md E14). The backend side is ready: `io/turn_signal.py` turns lamp levels into `vehicle.turn_signal` | S |
| 4 | Push channel | parked-car guardian delivery, DTC / maintenance push | notifier on the phone over LTE to a self-hosted topic, behind `NotificationSink` | S |
| 5 | Validated traffic-light ego state | green-light reminder (enable), red-light highlight | review `green_light` shadow log; roadsense L7 on own recordings | S–M |
| 6 | Object tracks in roadsense (adjacent / oncoming, lead class, pedestrians) | better distance warning, cut-in handling, truck-ahead recirc | roadsense `objects[]` (MFD_MAIN_SCREEN 8.2) | M–L |
| 7 | Rain / light / dew sensor in the car | rain awareness, fogging assist, lights reminder | VAG RLS on a LIN satellite (firmware exists from the bench) | M |
| 8 | Yaw rate / calibrated acceleration | live curve check, smoothness score | found 2026-11-17 (not decoded yet): 0x022 = yaw rate, 0x023 = two-channel acceleration sensor ([prius_can.md](prius_can.md) §2, scales P); next: decoder entries + a calibration drive (GNSS or a full circle) | S |
| 9 | Cruise stalk wiring and hold timing (state, set speed, engaged, tap step 1 mph and the SET/RES switch bits are verified 2026-11-16/17) | adaptive-cruise interposer design | timed holds and CANCEL with `21D3` at 5–10 Hz; stalk connector and levels with the EWD and a multimeter ([CRUISE_CONTROL.md](CRUISE_CONTROL.md) §8) | S |
| 10 | 21C3 torque units | absolute braking score | a braking test against 0x030 and deceleration | S |
| 11 | Fuel level scale / LPG level and fuel select | range on LPG, true fuel statistics | inputs from the LPG changeover line and level sender | S–M |
| 12 | Separate rear-door / hatch bits | door picture, rear-seat reminder | satellite input on the hatch switch or a further BEAN decode | S |
| 13 | Rear / side sensing | blind spot, cross traffic, exit warning, parking arcs | radar / ultrasonic kits on satellites | L |
| 14 | Seat belts / occupancy | rear-seat reminder | buckle switch taps or a seat mat | M |
| 15 | Outside air quality | AQS recirculation | PM/VOC/NOx sensor at the cowl | M |
| 16 | Tyre pressures | TPMS | BLE sensors via `prius-bled` | M |
| 17 | Wiper state | wet-road inference | tap the wiper motor line | S |
| 18 | Driver-facing camera | distraction / PERCLOS | IR camera + model | L |
