# BEAN findings on the owner's car (NHW20, EU LHD, smart entry)

Capture `docs/data/bean/2026-11-13_session30_doors_locks.ndjson.gz` (16:29–16:37 CET, 4,934 frames,
0 errors), matched against the owner's timed action list. Confidence: each item was seen at every
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
| `FE:E1` byte 2 | 0x10 / 0x20 | **unlock / lock event**, one or two frames at priority 10, then back to 000000 | remote unlock, handle touch unlock, auto re-lock, handle-button lock; NOT at the interior lock switch |
| `FE:83` byte 3 | 0x08 | interior (dome) lamp on timer, ~15–25 s after unlock/door | every wake with unlock or door |
| `FE:83` byte 3 | 0x04 | back door open / luggage lamp | 36:01 only (single occurrence) |
| `FE:D8` byte 1 | 0x01 | driver-door-opened timer (~35 s) | driver door only, not other doors |
| `B5:D9`, `C1:E2` | 2–3 bytes | key authentication exchange (values differ every time) | only when the driver door opens |
| `FE:C2` | node + status | wake announcements; the first one after sleep is not ACKed (rsp 0) | every wake |

Not on the bus: the map lamps switched by hand (bus stayed asleep).

`FE:E1` is the lock/unlock candidate for transmission: it appears whenever the lock action does not
come from the body ECU's own interior switch, which fits a request sent to the body ECU (or an
answer-back/hazard-flash request tied to it). Which of the two it is can only be settled by sending it.
Handle touch, handle button, remote and auto re-lock are indistinguishable on the bus.
