# Cruise control on this car (NHW20): engineering reference

Everything measured about the factory cruise control of this Prius NHW20 (EU, plain cruise, no
radar), written down for a possible future project of the owner: a man-in-the-middle (interposer) on
the cruise stalk. This file records facts and open measurements. It is not a build guide, and the car
computer does not act on the cruise: it only shows the state on the MFD.

Confidence as in [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md): **C** confirmed (two independent pieces of
evidence, or every occurrence), **P** probable, **G** guess, **?** unknown. Times are CET (local).

Sources: three drives with cruise use (§9), the owner's step lists for 2026-11-16 and 2026-11-17, and
the repair-manual references already in the repository. Related: [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md)
§1 (BEAN side, owner timeline of 2026-11-16), [`prius_can.md`](prius_can.md) §2 and §6 (CAN rows, 21D3
layout), [`FEATURES.md`](FEATURES.md) (MFD cruise slot).

---

## 1. Summary

| Question | Answer | Conf. |
|---|---|---|
| Which ECU runs the cruise? | The hybrid (HV) ECU: the set speed, the engaged state and the stalk switch states are in its diagnostic reply `0x7E2` mode 21 PID `D3`; RM1076E 05-1678 routes the cruise indicator HV ECU → gateway → meter | C |
| Which ECU reads the stalk? | The HV ECU: the stalk switch states appear in its own `21D3` reply (byte E, §3) | P (C that the HV ECU sees them; that it reads the wire itself and not via another ECU: P, no other ECU on the buses shows them) |
| Stalk switches on BEAN or broadcast CAN? | No (bit search over three sessions) | C |
| Set speed on BEAN or broadcast CAN? | No; only `21D3` B | C |
| Main switch | BEAN `62:C8` b1 `10` = CAN 0x5C8 C bit 4 (gateway copy) | C |
| Engaged | CAN 0x120 E bit 7 (fast); `21D3` D bit 6 (1 Hz) | C |
| Tap step | 1 mph = 1.609 km/h; `21D3` B shows the internal set speed floored to whole km/h | C |
| Hold rate | about 1 mph per second (1.4–1.6 km/h/s) | P |
| Minimum set speed | 40 km/h (owner's manual); SET at 37 km/h refused | C |
| Resume memory | kept after a cancel; equals the vehicle speed at the moment of the cancel; cleared when the car falls below about 35 km/h and by the main switch | C (cleared), P (= speed at cancel, 6 of 6) |
| Brake cancel | the HV ECU sees the brake on three separate inputs (`21D3` E bits 0–2), independent of the stalk | C |

---

## 2. Architecture as far as it is known

```
 stalk (RES/ACC, SET/COAST, CANCEL, main ON-OFF)
   │  wiring: not measured (§7)
   ▼
 HV ECU (0x7E2) ── cruise logic, set speed, drive request
   │  CAN (powertrain)                         │ diagnostic reply 21D3 (polled, 1 Hz)
   ├── 0x120 E bit 7 "engaged" (broadcast)     └── A speed, B set speed, C request, D state, E switches
   └── main-switch state ──► gateway ── CAN 0x5C8 ──► BEAN 62:C8 ──► meter (CRUISE indicator)
 brake pedal ──► HV ECU (three inputs, 21D3 E bits 0–2) ──► cancel
```

* The ECM is not involved on the buses: no cruise frame comes from it, and no ECM PID changes with
  the cruise.
* The gateway (BEAN node `52`) only copies the main-switch state to the meter (`62:C8`, §3). The set
  speed is not shown on the meter of this car and goes nowhere on the buses.
* RM1076E 05-1678 (meter): "HV ECU → combination meter: cruise indicator" via the gateway on BEAN, in
  agreement with the above. No repair-manual page on the stalk wiring is in this repository (§7).

---

## 3. Signals

### 3.1 Solicited `0x7E2` `21 D3` → `0x7EA 07 61 D3 A B C D E`

Polled once a second by the backend (`comm/subscription_groups.py` slot 6; decoder
`comm/solicited_can.py`). The third-party bit list for this PID does not match this car.

| Byte | Content | Conf. | Evidence |
|---|---|---|---|
| A | vehicle speed, km/h, integer | C | follows 0x0B4 |
| B | **set speed**, km/h, floored from an mph-based internal value (§5); 0 = no set speed and no resume memory | C | every SET, tap, hold, cancel of 2026-11-16/17 |
| C | analog 0..107: rises while the cruise pulls the car, 0 when it coasts, after a cancel and with the main switch off. Steady cruise 45–90, accelerating 85–107, falling towards 0 while the set speed is below the car's speed | C (behaviour), P (= drive torque request) | |
| D | bit 7 = shift in D (cleared in P/N with the main switch on: `0F`); bits 0–3 all set while the main switch is on; bit 6 = cruise engaged (controlling); bits 4–5 never seen | C | `80` off, `8F` standby, `CF` engaged |
| E | **switch inputs as the HV ECU sees them** at the moment of the reply (table below) | C for `10`/`08`/bits 0–2; ? for the rest | 22 switch samples, 1,500+ brake samples |

E bits:

| Bit | Meaning | Evidence |
|---|---|---|
| `10` | SET/COAST pressed | 11 of 11 samples at a SET (from standby) or a SET/COAST tap or hold; never otherwise |
| `08` | RES/ACC pressed | 11 of 11 samples at a RES/ACC tap or hold (incl. 6 presses with no resume memory, which did nothing); never otherwise |
| `01`, `02`, `04` | brake pedal: all three set while the pedal is pressed (1,442 of 1,500 polls with 0x030 > 8 and `FE:29` brake set show `07`); `05`, `04`, `01` at the moment of press or release (the three inputs switch at different pedal travel) | C (pedal), P (three separate brake inputs, e.g. stop-lamp switch and brake switch) |
| `02` alone | for two polls at every power-off (7 of 7 power-offs) | ? |
| `01` alone | twice while driving at 25 and 49 km/h with no brake on 0x030 (2026-11-16 17:43:19, 17:50:13) | ? (a light brake touch below the 0x030 threshold?) |
| CANCEL | not caught yet: every stalk cancel fell between two polls | open (§8) |

E is a sample, not a latch: a tap shorter than the poll gap is seen only when a poll falls inside it
(at 1 Hz most taps are missed; the set-speed change in B is still seen). SET/COAST and RES/ACC were
never seen together.

All E samples with a stalk bit (`21D3` A/B are km/h; ΔB is the change since the previous poll):

| Time | A | B | E | ΔB | What it was |
|---|---|---|---|---|---|
| 11-16 17:52:31.2 | 44 | 0 | `10` | 0 | SET in standby (earlier drive) |
| 11-16 21:17:01.6 | 53 | 0 | `10` | 0 | SET → B 52 on the next poll |
| 11-16 21:17:21.6 | 60 | 58 | `10` | −5 | hold SET/COAST (owner: hold down) |
| 11-16 21:17:26.6 | 59 | 55 | `10` | 0 | same hold, still pressed |
| 11-16 21:17:37.6 | 51 | 53 | `08` | +1 | RES/ACC tap |
| 11-16 21:17:45.6 | 53 | 55 | `08` | +2 | RES/ACC |
| 11-16 21:17:49.6 | 55 | 60 | `08` | +5 | RES/ACC (three taps or a hold within 1 s) |
| 11-16 21:20:02.2 | 37 | 0 | `10` | 0 | the refused SET at 37 km/h |
| 11-16 21:20:09.2 | 39 | 0 | `08` | 0 | RES, no memory (main had been switched off/on): nothing |
| 11-16 21:20:26.2 | 48 | 0 | `08` | 0 | RES, no memory: nothing |
| 11-16 21:21:15.2 | 45 | 45 | `10` | 0 | SET/COAST → 40 on the next poll |
| 11-17 10:22:53.9 | 50 | 0 | `10` | 0 | SET → 50 on the next poll |
| 11-17 10:25:58.7 | 58 | 57 | `10` | −1 | SET/COAST tap |
| 11-17 10:26:05.7 | 55 | 54 | `10` | −1 | SET/COAST tap |
| 11-17 10:27:16.7, :17.7 | 55 | 0 | `08` | 0 | RES, no memory (cleared at 10:26:13): nothing |
| 11-17 10:28:02.7, 10:28:46.7, :49.7 | 45, 46, 44 | 0 | `08` | 0 | RES, no memory: nothing |
| 11-17 10:28:59.7 | 49 | 0 | `10` | 0 | SET → 50 on the next poll |
| 11-17 10:29:01.7 | 47 | 53 | `08` | +3 | two RES/ACC taps within one poll |
| 11-17 10:29:10.7 | 53 | 51 | `10` | −2 | SET/COAST tap (floor effect, §5) |

### 3.2 Broadcast and BEAN

| Signal | Content | Conf. |
|---|---|---|
| BEAN `62:C8` (gateway → meter, PRI 6, 2 bytes, 1 s + on change, IG only) | b1 `10` = main switch on (the meter's CRUISE indicator); everything else `00` | C |
| CAN 0x5C8 `24 00 xx` (gateway copy of `62:C8`) | C bit 4 (`10`) = main switch. A `A4` instead of `24` = the gateway's **immediate-copy header** (bit 7 of the header, [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §5): on 2026-11-17 the first frame after each of the 7 main-switch changes carried `A4` (for about 1 s, until the next periodic `24` frame) and no other frame did. On 2026-11-16 the trip log showed the off/on at 21:19:59–21:20:01 as one late `A4` frame at 21:20:02.3, next to the refused SET (trip-log delivery delay of that day, §4). Resolves the former open item "0x5C8 b0 bit 7" | C |
| CAN 0x120 byte E bit 7 | **engaged** (`10` → `90`): set at every engage, cleared at every cancel (7 engages on 2026-11-16/17 incl. the refused one). Byte H (last byte) is the Toyota checksum, so its bit 7 changes with E; it is not a second engaged bit. At the refused SET (37 km/h) it was set for a single frame | C |
| Meter | CRUISE indicator only (from `62:C8`); no set speed | C |

Not found on BEAN or broadcast CAN (bit search over the 2026-11-16 and 2026-11-17 sessions, all IDs,
every byte and bit): set speed, resume memory, stalk switches, drive request.

---

## 4. Timing and latency

Measured on 2026-11-17 (panda path fixed, see below; times are the panda's frame time, the phone's
receive time of the `21D3` reply, and the phone's receive time of the BEAN batch):

| Event | First source | Others after it |
|---|---|---|
| main switch on/off (7 changes) | CAN 0x5C8 | BEAN `62:C8` +40–60 ms (gateway CAN → BEAN copy plus BEAN batching) |
| engage / cancel (6 changes) | CAN 0x120 E bit 7 | `21D3` D bit 6 +0.04–1.1 s (only the poll phase: the reply reflects the state at the poll) |
| set-speed change | `21D3` B (only source) | — |
| switch state | `21D3` E (only source, sampled) | — |

* `21D3` is the only set-speed source and is polled at 1 Hz, so a set-speed change is known 0–1 s
  late, and taps closer together than the poll gap merge (§5).
* **Passive CAN delivery delay (fixed 2026-11-17).** Until then the panda reader on the phone could not
  keep up with the bus and passive frames reached the state and the trip log 0–5 s late (median 0.6 s,
  [`prius_can.md`](prius_can.md) §8). The 2026-11-16 observations "0x120 about 2 s behind `21D3`" and
  "0x5C8 about 3 s behind `62:C8`" and the missed 1 s main off/on on 0x5C8 were that delay, not the
  car. Trip logs before 2026-11-17 must not be used for CAN-vs-poll timing.
* For an interposer the relevant timing (minimum tap length, debounce, the press time after which a
  press counts as a hold) is not measured: it needs `21D3` at a higher rate and marked presses (§8).

---

## 5. Behaviour (state machine as observed)

States: **OFF** (main switch off: D `80`, `62:C8` `00`), **STANDBY** (main on, not controlling: D
`8F`, `62:C8` `10`, 0x120 E `10`), **ENGAGED** (D `CF`, 0x120 E `90`, C > 0 while pulling).

| Transition | Observed | Conf. |
|---|---|---|
| OFF → STANDBY | main switch on; indicator at once (0x5C8, `62:C8`), D bits 0–3 | C |
| STANDBY → OFF | main switch off; clears the resume memory (B → 0; RES afterwards does nothing: 21:20:09, 21:20:26) | C |
| STANDBY → ENGAGED (SET) | at ≥ 40 km/h: B = current speed (floor), engaged. The B value appears on the poll after the one that saw E `10` (4 of 4: the ECU takes the set speed at or after the release) | C (SET), P (on release) |
| SET below 40 km/h | 2026-11-16 21:20:02 at 37 km/h: B = 37 and D bit 6 for exactly one poll, 0x120 engaged for one frame, then both cleared; the cruise did not hold | C (one occurrence) |
| ENGAGED: RES/ACC tap | +1 mph (internal), B +1 or +2 km/h (floor) | C |
| ENGAGED: SET/COAST tap | −1 mph, B −1 or −2 km/h | C |
| ENGAGED: hold RES/ACC | the set speed runs 2–4 km/h ahead of the car, +13 km/h in 9 s and +14 in 10 s (2026-11-16), i.e. about 1 mph per second; the car follows at ≈ 1.2 km/h/s | P (rate from 1 Hz B only, hold length not marked) |
| ENGAGED: hold SET/COAST | first poll −5, then −3, then flat for 4 s while the car slows (2026-11-16 21:17:21–29); no steady rate seen | P |
| ENGAGED → STANDBY (cancel) | C → 0, D bit 6 and 0x120 clear; B is kept as the resume memory, but **B becomes the vehicle speed at the moment of the cancel** where the two differed: 66 → 63 (A 63), 61 → 58 (A 57–59), 48 → 49 (A 49); unchanged where they were equal (50, 54, 42). 6 of 6 | P (= speed at cancel), C (memory kept) |
| cancel by brake | E `07` on the cancel poll (10:23:11.9, 10:29:23.7, 21:17:52.6, 21:23:08.2) | C |
| cancel without the brake | E `00` on the cancel poll and the poll before (10:26:08.7; 21:17:16.6 = owner's stalk cancel) | P (stalk CANCEL; the CANCEL bit was never sampled) |
| STANDBY: RES with memory | resumes to the memory (21:17:19: 63) | C |
| STANDBY: RES without memory | nothing; E shows `08`, B stays 0, D stays `8F` (6 presses) | C |
| memory cleared by speed | B → 0 on the first poll with A ≤ 32: kept at 36 (and 38), cleared at 32, 30, 30, 29, 22, 31 (6 of 6). Threshold between 33 and 35 km/h on `21D3` A | C |

**Set-speed quantum (settled 2026-11-17).** The ECU keeps the set speed in mph-based units; one tap
is 1 mph = 1.609 km/h; `21D3` B is that value floored to whole km/h, so the MFD may show it up to
1 km/h low. Single taps with long pauses, trip `trip_20261117_102419_216` (owner: "long pauses between
clicks"):

| Time | B | Step | Internal set speed if SET = 54.0–54.2 km/h and a tap = 1.609 km/h |
|---|---|---|---|
| 10:25:12.7 | 54 | SET | 54.0 |
| 10:25:19.7 | 55 | +1 | 55.6 |
| 10:25:35.7 | 57 | **+2** | 57.2 |
| 10:25:48.7 | 58 | +1 | 58.8 |
| 10:25:58.7 | 57 | −1 | 57.2 |
| 10:26:02.7 | 55 | **−2** | 55.6 |
| 10:26:05.7 | 54 | −1 | 54.0 |
| 10:26:08.7 | (54 kept) | cancel | |

Each change came alone, 3–16 s from the next, so each is one tap; the alternating +1/+2 and −1/−2
is exactly floor(54.0 + k × 1.609). Second session (10:29:00–10:29:23): SET at A 48–49 gave B 50
(SET ≈ 50.0–50.4), two taps up within one poll (+3 → 53), then single taps down 53 → 51 → 50 → 48,
which fits only an internal 53.2–53.6 = 50.0–50.4 + 2 × 1.609. The ±5 km/h steps of 2026-11-16 are
three taps (4.8 km/h). A first session the same morning (10:22:53–10:23:14) was SET 50 and a brake
cancel, no taps.

`21D3` C during these sessions: 45–90 in steady cruise at 50–58 km/h, a jump of 10–30 counts within
1–2 s after each +tap, a drop of 10–25 after each −tap, 0 at every cancel.

---

## 6. What the CAN/BEAN data say an interposer would have to respect

Facts only; the electrical side is unknown (§7).

* The stalk reaches the HV ECU as at least three distinct inputs as far as the ECU's own view goes
  (SET/COAST `10`, RES/ACC `08`, CANCEL not sampled yet) plus the main switch (D bits 0–3).
* The brake reaches the HV ECU on its own three inputs (E bits 0–2), not through the stalk. An
  interposer on the stalk does not touch brake cancel, and must not.
* Commands are relative and quantised: one tap = ±1 mph; a hold changes the set speed at about
  1 mph per second; SET takes the current speed (≥ 40 km/h only); RES returns to the memory, which is
  the speed at the last cancel and is lost below about 35 km/h and at main off.
* `21D3` E is a ready-made check: whatever an interposer presents, the HV ECU's own reading of it is
  visible in E (at the poll rate), and the result in B, C, D and 0x120.
* The set speed the driver sees on the MFD is B (floored). A controller that wants "set speed = X
  km/h" has to work in 1 mph steps from the current internal value, which B only gives to ±1 km/h.

---

## 7. Stalk wiring: known and unknown

* No page of the electrical wiring diagram or repair manual for the cruise control switch is in this
  repository, and nothing in the repository documents the stalk connector, its pins or its signal
  levels. RM1076E is referenced only for 05-1678 (meter: cruise indicator from the HV ECU).
* From the buses: the switch states end up in the HV ECU (§3.1). Whether the stalk is wired straight
  to the HV ECU (through the steering column spiral cable) or through another ECU is not visible; no
  other ECU on BEAN or CAN shows the switch states (P: direct to the HV ECU).
* Toyota cruise stalks of this period are commonly a resistor ladder on one signal wire plus ground,
  each switch giving a different resistance (G: general Toyota practice, not checked for the NHW20).
  This is the first thing to confirm or refute with the EWD and a multimeter.
* Not known: connector and pin numbers, number of wires, the voltage/resistance per switch, whether
  the main switch is on the same wire, whether the ECU detects an open/short or an implausible value
  and sets a DTC or locks the cruise out.

---

## 8. Open measurements

| # | Measurement | Settles | How |
|---|---|---|---|
| M1 | **Stalk connector and levels** (bench, key off, then IG on parked) | wiring, resistor ladder or not, values per switch incl. CANCEL and main | EWD page "Cruise Control System" for the NHW20 (RM1076E / EWD), then a multimeter at the spiral-cable connector; nothing connected in series |
| M2 | **Timed holds**: RES/ACC and SET/COAST held 1, 2, 5 s with marks, at a steady 60–80 km/h | hold threshold, hold rate in mph/s, coast behaviour | `bean_mark` at press and release; `21D3` at 5–10 Hz during the test (needs a temporary poll-rate change while the main switch is on) |
| M3 | **CANCEL bit** | which E bit CANCEL sets | stalk cancel held ~1 s so a poll falls inside it, three times |
| M4 | **Brake cancel vs stalk cancel**, marked | E bits 0–2 order, cancel latency | three of each |
| M5 | **Minimum tap length** | debounce for an emulated tap | only after M1, with an instrumented stalk; or `21D3` at 10 Hz and very short taps |
| M6 | E `02` at power-off, E `01` while driving | remaining E bits | from existing data at higher poll rate |
| M7 | **Memory = speed at cancel** | §5 cancel row (P) | cancel during a hold-up where the car is well below B, and at a steady speed with B ≠ A |
| M8 | SET at 38–41 km/h steps | exact minimum | four SETs while slowly accelerating |
| M9 | Main switch on/off while engaged | whether main off is an immediate cancel | once, at a safe speed |

Already done: tap step (1 mph), engaged and main sources, set speed layout, `21D3` E switch bits,
0x5C8 `A4` header, memory clear threshold.

---

## 9. Safety notes

* The car computer only reads: `21D3` is a read-only diagnostic request from the gateway; nothing is
  sent to BEAN, and no cruise frame is transmitted. The MFD cruise slot is information only.
* Any interposer would act on the car's longitudinal control. It is a future owner project, outside
  this repository's scope rule (information, warnings, comfort; nothing steers, brakes or
  accelerates), and needs its own safety case: the factory brake cancel (independent brake inputs)
  must stay untouched; a failed interposer must fall back to "no switch pressed" and to the original
  stalk; nothing may press SET or RES/ACC on its own without the driver.
* Tests with the cruise engaged are done by the owner, on a free road, with marks; never a test of an
  emulated switch on a public road before M1–M5.

---

## 10. Data and reproduction

| Drive | Trip log (phone `logs/trips/`) | BEAN | Content |
|---|---|---|---|
| 2026-11-16 17:39–18:02 | `trip_20261116_173944_316`, `_175709_621_p1` | capture `bean_20261116-175134_drive` (lossy, powerbox 2.4.1) | short cruise use 17:46–17:52, no owner timeline |
| 2026-11-16 21:14–21:28 | `trip_20261116_211424_086` | `docs/data/bean/2026-11-16_cruise_drive.ndjson.gz` (rebuilt from the trip log) | experiment E5 part 1, owner timeline in [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §1 |
| 2026-11-17 10:10–10:39 | `trip_20261117_100957_645`, `trip_20261117_102419_216_p1` | `bean_20261117-101230_drive` (complete, powerbox 2.4.2) | sessions 10:22:53–10:23:14 (SET, brake cancel), 10:25:11–10:26:13 (single taps), 10:28:59–10:29:25 (taps, brake cancel); first drive with the panda reader fix and gateway fw 3.2.11 |

The trip logs and the 2026-11-17 BEAN capture are on the phone, not in the repository. Every `21D3`
poll can be listed from a trip log with a few lines of Python: records with `id` 1, `d.a == "sub"`,
`d.i == "0x7EA"`, `d.d[1:3] == [0x61, 0xD3]`, bytes A–E = `d.d[3:8]`; the record `ts` is relative
after the first line and has to be put on the absolute clock of the surrounding records.
