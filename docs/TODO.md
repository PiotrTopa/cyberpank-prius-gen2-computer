# To-do (owner, hardware and on-site)

Things only the owner can do at the car or the bench. Software follow-ups live in the documents
linked from each line.

| # | What | Details | Added |
|---|---|---|---|
| 1 | **MFD: reconnect the two red MSB lanes.** Pi header **pin 38 = GPIO20** (red bit 4) and **pin 40 = GPIO21** (red bit 5, MSB), the last two pins of the 40-pin header, do not reach the VGA666. Reseat the VGA666 at that end / reflow both pins on the Pi header and the VGA666 socket. Check: on the Pi `sudo pinctrl set 20 ip pu; pinctrl get 20` must read `lo` (same for 21), then `sudo pinctrl set 20 a2` (and 21); then a ramp photo and the fine colour curve | [MFD_VIDEO_TIMINGS.md](MFD_VIDEO_TIMINGS.md) §3 | 2026-11-17 |
| 2 | Turn-signal satellite (two indicator lamp lines) | [FEATURES.md](FEATURES.md) §7 gap 3 | 2026-11-17 |
| 3 | **Whole USB tree collapses** (not only the camera hub): on the 2026-11-17 evening drive `error -71` hit hubs `1-1`, `1-1.3` and `1-1.4` in the same second three times (16:03:13, 16:15:49, 16:26:33), each with a 6–7 s CAN gap and the panda, gateway, powerbox and MFD link dropping; the DVR port (`1-1.4` port 4) usually complains first. Check the 5 V supply and ground of the hubs, the DVR camera port and cable, connectors. The head unit additionally dropped off USB ~35 times in 15 min | [HEADUNIT_INTEGRATION.md](HEADUNIT_INTEGRATION.md) §7.4, drive report 2026-11-17 | 2026-11-17 |
| 3a | **Head-unit GPS reception is weak**: fixes for only 31 % of the driving time, 3–5 satellites, first fix after an ACC cycle 4 min, an 18.8 min stretch without a fix with the link up; all 88 assistance (EPO) downloads failed (the unit has no internet). Check the GPS antenna placement and connector; consider giving the unit internet (phone AP) for assistance data | drive report 2026-11-17 | 2026-11-17 |
| 4 | Phone supply hold-up against ms dips (hard reset on IP2368 inrush, 2026-11-17 10:39); scope on the buck output | [FINDINGS_LOG.md](FINDINGS_LOG.md) | 2026-11-17 |
| 5 | Yaw-rate calibration: one slow 360° circle; cruise stalk wiring measurement on the bench; hazards with IG on (BEAN) | [POSITION_FEEDER.md](POSITION_FEEDER.md) §3, [CRUISE_CONTROL.md](CRUISE_CONTROL.md) | 2026-11-17 |
