# To-do (owner, hardware and on-site)

Things only the owner can do at the car or the bench. Software follow-ups live in the documents
linked from each line.

| # | What | Details | Added |
|---|---|---|---|
| 1 | **MFD: reconnect the two red MSB lanes.** Pi header **pin 38 = GPIO20** (red bit 4) and **pin 40 = GPIO21** (red bit 5, MSB), the last two pins of the 40-pin header, do not reach the VGA666. Reseat the VGA666 at that end / reflow both pins on the Pi header and the VGA666 socket. Check: on the Pi `sudo pinctrl set 20 ip pu; pinctrl get 20` must read `lo` (same for 21), then `sudo pinctrl set 20 a2` (and 21); then a ramp photo and the fine colour curve | [MFD_VIDEO_TIMINGS.md](MFD_VIDEO_TIMINGS.md) §3 | 2026-11-17 |
| 2 | Turn-signal satellite (two indicator lamp lines) | [FEATURES.md](FEATURES.md) §7 gap 3 | 2026-11-17 |
| 3 | Camera hub USB branch: protocol errors (`error -71`) and re-enumerations of the head unit and the DVR camera behind hub `1-1.4`; check cable, hub power, connectors | [HEADUNIT_INTEGRATION.md](HEADUNIT_INTEGRATION.md) §7.4 | 2026-11-17 |
| 4 | Phone supply hold-up against ms dips (hard reset on IP2368 inrush, 2026-11-17 10:39); scope on the buck output | [FINDINGS_LOG.md](FINDINGS_LOG.md) | 2026-11-17 |
| 5 | Yaw-rate calibration: one slow 360° circle; cruise stalk wiring measurement on the bench; hazards with IG on (BEAN) | [POSITION_FEEDER.md](POSITION_FEEDER.md) §3, [CRUISE_CONTROL.md](CRUISE_CONTROL.md) | 2026-11-17 |
