# BEAN findings on the owner's car (NHW20, EU LHD, smart entry)

Captures `docs/data/bean/2026-11-13_session30_doors_locks.ndjson.gz` (16:29–16:37 CET, 4,934 frames,
0 errors; handle/smart-touch operations) and `docs/data/bean/2026-11-13_remote_test.ndjson.gz`
(key-fob unlock/lock), matched against the owner's timed action list. Confidence: each item was seen at every
occurrence of the action unless noted. BEAN frames carry no source address, so senders are inferred.

| Message | Field | Meaning | Evidence |
|---|---|---|---|
| `52:3F` | 3 bytes | odometer, km (0x0493DF = 299,999) | owner confirmed exact |
| `FE:B6` byte 0 | 0x01 | driver door unlocked | set first at every unlock, cleared at lock |
| `FE:B6` byte 0 | 0x40, 0x80 | other doors unlocked (two groups) | C1 after unlock; C0 → 40 → 00 while locking |
| `FE:B6` byte 1 | 0x80 | driver door open | 31:21–31:26, 32:01–32:11 |
| `FE:B6` byte 1 | 0x40 | passenger door open | 34:01–34:11 |
| `FE:B6` byte 1 | 0x04 | rear door or back door open (one bit for all three) | 35:40–35:52, 36:01–36:16, 36:31–36:41 |
| `FE:7F` byte 2 | 0x80 | any door open (meter indicator) | every door event |
| `FE:E1` byte 2 | **0x04 / 0x08** | **remote (fob) unlock / lock**, one or two frames at priority 10, then back to 000000 | `remote_test` capture: 000004 at key-fob unlock, 000008 at key-fob lock |
| `FE:E1` byte 2 | **0x10 / 0x20** | **handle / smart-touch unlock / lock**, one or two frames at priority 10, then back to 000000 | session30: 000010 at handle-touch unlock, 000020 at handle-touch lock; NOT at the interior lock switch |
| `FE:E1` byte 2 | 0x20 | **30 s auto re-lock** (after a handle unlock with no door opened) uses the lock code | session30: unlock 000010 @ t_ms 4482887 → lock 000020 @ 4512862, Δ≈30.0 s |
| `FE:83` byte 3 | 0x08 | interior (dome) lamp on timer, ~15–25 s after unlock/door | every wake with unlock or door |
| `FE:83` byte 3 | 0x04 | back door open / luggage lamp | 36:01 only (single occurrence) |
| `FE:D8` byte 1 | 0x01 | driver-door-opened timer (~35 s) | driver door only, not other doors |
| `B5:D9`, `C1:E2` | 2–3 bytes | key authentication exchange (values differ every time) | only when the driver door opens |
| `FE:C2` | node + status | wake announcements; the first one after sleep is not ACKed (rsp 0) | every wake |

Not on the bus: the map lamps switched by hand (bus stayed asleep).

`FE:E1` is the lock/unlock candidate for transmission: it appears whenever the lock action does not
come from the body ECU's own interior switch, which fits a request sent to the body ECU (or an
answer-back/hazard-flash request tied to it). Which of the two it is can only be settled by sending it.
The method **is** distinguishable on the bus: remote/fob uses 0x04 (unlock) / 0x08 (lock); handle or
smart-touch uses 0x10 / 0x20; the 30 s auto re-lock reuses the lock code 0x20. (Corrected 2026-11-13
against `docs/data/bean/2026-11-13_remote_test.ndjson.gz`; an earlier note here wrongly called the
methods indistinguishable, having seen only the handle-touch capture.)

## TX replay result — FE:E1 is a report, not a command (2026-11-13, ~18:20 CET, car parked)

Settled by transmitting. We injected the exact recorded remote-unlock frame `FE:E1 pri=10 data=000004`
on the car's bus, both as a cold frame (our SOF woke the bus) and as a two-stage test (wake first, 2 s
settle so the body ECU 0x40 was fully awake, then inject). Both times the frame was **ACKed** (a node
received it with valid CRC) but:

- `FE:B6` stayed `0000` — **the doors did not unlock**;
- the body ECU's own `FE:E1` stayed `000000` (it never echoed or acted);
- **no 30 s auto re-lock followed** (the behaviour the owner's replay test was designed to detect).

So `FE:E1` is a **status broadcast the body ECU emits *after* it acts**, not a command it listens to.
The `FE:E1`→`FE:B6` 8 ms ordering seen in recordings is report-then-report.

**There is no door lock/unlock command on this BEAN bus at all.** Across both captures the only directed
(non-`FE`) frames are `40:59 00C0` (body-ECU heartbeat, *never varies*), `52:3F` (odometer), `62:6E`
(meter), and `B5:D9`/`C1:E2` (rolling smart-key crypto challenge/response = key authentication). The
body ECU (0x40) owns the lock motors and decides to actuate from its *direct* inputs — key RF/LF and the
interior lock switch (which is wired to the ECU, not on the bus) — then only reports the result. A fob
press wakes the bus with the `FE:C2` announcement cascade; the actual unlock command never travels on
BEAN. **Conclusion: door lock/unlock cannot be achieved by BEAN injection on this car.** Reading lock
state and door-open state from the bus works perfectly; actuation would need either body-ECU message
fuzzing (`40:xx`, risky on a safety ECU) or a physical relay tap on the lock-actuator / lock-switch
wiring (the standard aftermarket approach).

### Corroborated by the factory manual (research lane, 2026-11-13)

The official 2004 NHW20 repair manual (RM1075U) states the architecture explicitly and settles it:

- Smart-entry "Function of Main Components", Multiplex Network Body ECU: it *transmits* body signals
  (courtesy switch, door key lock/unlock switch, door lock position switch, …) to the smart ECU **via
  BEAN**, but *receives* the **door lock/unlock request signal from the smart ECU via a DEDICATED SERIAL
  COMMUNICATION LINE with a local protocol** (body ECU ↔ smart ECU ↔ wireless door-lock receiver). So
  BEAN carries body-ECU→smart-ECU **status only**; the lock/unlock **command** rides a separate
  proprietary serial line, not BEAN.
- The body ECU then **drives the lock motors over hardwired outputs** ("outputs a door lock/unlock
  control signal to each door lock assy"); the 30 s auto re-lock and repeat-lock are body-ECU-internal
  logic — matching our `FE:E1 0x20` auto re-lock being a report, not a command.
- Body-ECU terminals page: the dedicated link is pin **RDA (B7-23) "Door control receiver input"**
  (~6–7 V idle); the lock-motor drive outputs live in the separate "Power Door Lock Control System"
  section, not the multiplex section.

Independent hobbyist corroboration: the only public Toyota BEAN code table (`specadmin/toyota-mpx`,
`MPX-codes.md`) documents door frames `FE:DB/DC/DD/DE/96` etc. — **all status/event reports, no command
packet**. Toyota diagnostic lock control *does* exist but over the **diagnostic channel** (K-line/CAN,
Techstream actuator test), reachable at CAN id 0x750 on *later* (2010+, CAN-body) Prii — not on the
NHW20's BEAN body bus.

**Routes to actual lock control, ranked:** (1) relay/driver on the body ECU's **hardwired lock-motor
outputs** — "Power Door Lock Control System" section of the EWD — bypasses the bus and crypto, highest
confidence; (2) reverse-engineer and inject on the **dedicated serial line** at body-ECU pin RDA (B7-23)
— carries the real request but is an undocumented proprietary protocol, high effort; (3) body-ECU
**diagnostic actuator test** over K-line (NHW20 is pre-CAN-body), not over BEAN.
