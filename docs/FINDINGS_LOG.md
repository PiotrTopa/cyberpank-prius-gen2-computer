# Findings log

One line per finding about the car, its buses and our hardware, newest first, with a link to where it
is documented. Times CET. Add a line whenever a doc gains a confirmed fact or loses a wrong one.

## 2026-11-17 (body signals)

* Owner's body-signal list against BEAN and CAN ([BEAN_SIGNALS.md](BEAN_SIGNALS.md) §4.1, §4.2, §6,
  §10; recording `docs/data/bean/2026-11-17_body_signals.ndjson.gz`):
  * turn signals, hazards (car off), wipers/washer and seat belt are on neither BEAN nor broadcast
    CAN; `vehicle.turn_signal` stays None until a lamp source exists (`io/turn_signal.py`);
  * headlamp flash with the lamps off = `FE:7F` b0 `28` (both lamp bits); the high-beam switch alone
    sets nothing then;
  * each door: driver `FE:B6` b1 `80`, passenger `40`, both rear doors and the back door share `04`;
    the back door is the one preceded by the opener pulse `FE:83` b3 `04` (decoded,
    `vehicle.back_door_open`, P); CAN 0x5B6 decoded as the door fallback
    ([prius_can.md](prius_can.md) §3);
  * rear defogger `FE:FC` b0 `08`, front defrost `FE:FC` b0 `10` + `52:ED` b1 `20` now C; the
    steering-pad temperature keys only step `52:FD` (0.5 °C per 1.3–1.6 s held), no `FE:FB`;
  * brake hold: `FE:29` b2 `08` = 0x529 D `08` = 0x230 D bit 2 again.

## 2026-11-17

* Backend decodes the 17 Nov findings: yaw rate (`dynamics.yaw_rate_dps`, standstill offsets learnt),
  accelerations (m/s²), MG2 rpm from 0x244, 0x230 distance and our own odometer (`vehicle.odometer_km`,
  for service intervals, the meter is frozen), 0x348/0x3C8 rpm, 0x3CA speed, 0x3CD voltage, 0x484
  coolant, 0x03A accelerator, 0x038 ICE power request, 0x0B4 moving, 0x230 brake; cruise engaged and
  main switch now first from 0x120 / 0x5C8, `21D3` E switches in the state; replay of the drive
  through the ingress: yaw r 0.957, MG2 r 0.995, 0x348 rpm r 0.997, distance 0.6 % below integrated
  0x0B4 ([prius_can.md](prius_can.md) §2, [CRUISE_CONTROL.md](CRUISE_CONTROL.md) §5.1).
* 0x230 distance counter: 102.85 counts/m over 10.8 km, counts up in reverse too
  ([prius_can.md](prius_can.md) §2).
* The "trip log appended to a file of 16 Nov" after the 10:39 reboot was a new file named by a stale
  wall clock: the phone booted with the clock at 16 Nov 11:50, the recorder opened
  `trip_20261116_115052_965`, and NTP stepped the clock by +82,173 s 36 s later. The recorder now
  closes and re-dates a trip at a clock step and never reuses an existing file name; readers skip
  NUL-filled tails ([ARCHITECTURE.md](ARCHITECTURE.md) §3).
* `21D3` E = the HV ECU's switch inputs: `10` SET/COAST, `08` RES/ACC, bits 0–2 brake pedal; the stalk
  is visible nowhere else ([CRUISE_CONTROL.md](CRUISE_CONTROL.md) §3.1).
* Cruise resume memory = vehicle speed at the cancel (6/6), cleared below ~33–35 km/h and by the main
  switch; RES with no memory does nothing ([CRUISE_CONTROL.md](CRUISE_CONTROL.md) §5).
* Cruise tap = 1 mph (1.609 km/h); `21D3` B = internal set speed floored to km/h; ±5 km/h steps = 3 taps
  ([CRUISE_CONTROL.md](CRUISE_CONTROL.md) §5).
* 0x5C8 `A4` = the gateway's immediate-copy header, not a cruise bit; 0x120 byte H = checksum
  ([CRUISE_CONTROL.md](CRUISE_CONTROL.md) §3.2).
* Cruise latencies with the panda fix: 0x5C8 leads BEAN `62:C8` by 40–60 ms; 0x120 engaged leads the
  `21D3` poll by 0.04–1.1 s ([CRUISE_CONTROL.md](CRUISE_CONTROL.md) §4).
* 0x022 = yaw rate (two channels, ≈ 0.25 °/s per count), 0x023 = acceleration sensor with two channels
  at ±45° ([prius_can.md](prius_can.md) §2).
* 0x244 E,F = MG2 rpm, signed (direction), at broadcast rate ([prius_can.md](prius_can.md) §2).
* Last byte of every powertrain broadcast frame 0x022–0x3CF is a Toyota checksum
  ([prius_can.md](prius_can.md) §1).
* 0x230 A,B distance counter (~103 counts/m), D bit 2 brake; 0x348 ICE-running bits; 0x03A E bit 4
  accelerator pressed; 0x038 B ICE power request; 0x3C8 C ≈ rpm/32; 0x0B4 E bit 0 moving
  ([prius_can.md](prius_can.md) §2, §3).
* Gateway CAN transmit errors were the firmware's SPI polling during its own frame, not hardware; fw
  3.2.11 fixed it; first drive 5379/5379 answered, TEC 0, no bus-off ([prius_can.md](prius_can.md) §9;
  gateway repo `gateway-rp2350/README.md`, `captures/2026-11-17/`).
* Panda reader keeps up on the car: ~1690 frames/s at ~20 % busy; passive frames no longer late
  ([prius_can.md](prius_can.md) §8, [ARCHITECTURE.md](ARCHITECTURE.md) §3).
* Powerbox fw 2.4.2 on the car: BEAN receiver 44 % cpu, 1 overflow in 27 min, 51.6 frames/s complete
  ([powerbox/README.md](../powerbox/README.md) "Cost"; [BEAN_SIGNALS.md](BEAN_SIGNALS.md) §9).
* Undecoded CAN ids reach the trip log once a second only; nothing faster than 1 Hz is visible in them
  ([prius_can.md](prius_can.md) §8).
* Analysis report of the day: `filedrop/uploads/prius_data_compare_2026-11-17.md` (owner's drop folder).

## 2026-11-16

* Cruise decode: `62:C8`/0x5C8 main switch, 0x120 E bit 7 engaged, `21D3` A speed, B set speed, C drive
  request, D state ([BEAN_SIGNALS.md](BEAN_SIGNALS.md) §1).
* Passive CAN reached the phone 0–5 s late (panda reader overloaded); halved poll traffic, 0x3CF air
  temperatures replace OBD 0146/010F ([prius_can.md](prius_can.md) §2, §8).
* Powerbox fw 2.4.1 lost 7–9 % of BEAN frames (attribute-table regression), fixed in 2.4.2
  ([powerbox/README.md](../powerbox/README.md)).
* Cabin temperature = BEAN `52:6D` b0, 0.25 °C/count; `52:6D` b1 `20` blower on; `98:CC` raw outside
  sensor; meter odometer `52:3F` frozen at 299,999 km ([BEAN_SIGNALS.md](BEAN_SIGNALS.md) §4).
* High beam = `FE:7F` b0 `08` / 0x57F B `38` ([BEAN_SIGNALS.md](BEAN_SIGNALS.md) §4.1).
* Power-up sequence ACC → IG → READY on BEAN, experiment E1 done ([BEAN_SIGNALS.md](BEAN_SIGNALS.md)
  §4.5).

## 2026-11-15

* 0x025 steering angle × 1.5° verified ([prius_can.md](prius_can.md) §2).
* BEAN recording time base and delivery latency ([BEAN_SIGNALS.md](BEAN_SIGNALS.md) §9).
* Battery health collector and analysis ([BATTERY_HEALTH.md](BATTERY_HEALTH.md)).

## 2026-11-13 / 14

* BEAN ID map, gateway BEAN ↔ CAN copies, no BEAN lock command on this car
  ([BEAN_SIGNALS.md](BEAN_SIGNALS.md) §4, §5, §7).
* `62:20`/0x520 = fuel per injection, flow = K × word × rpm; meter works on true speed, speedometer
  12 % high ([BEAN_SIGNALS.md](BEAN_SIGNALS.md) §4.3, §4.4, §4.6).
