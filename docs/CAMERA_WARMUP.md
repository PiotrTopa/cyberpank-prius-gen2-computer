# Camera warm-up: early camera power on the BEAN entry signal

The fvision USB DVR (`1902:3241`, behind the VIA hub `2109:2817` on powerbox relay ch4, USB
`1-1.4`, camera on its port 4, head unit on its port 2) leaves the USB bus once after every
power-up and comes back; on the first drive after hours parked it stayed away about two minutes.
The road picture therefore came 45 s to 3 min into the drive. The backend now powers the camera
hub as soon as somebody gets in, so that sequence runs before the key.

## What the camera does after power-up (phone journals)

| power-up | enumerates after hub power | leaves the bus after | away for |
|---|---|---|---|
| 2026-11-16 13:34 preview | n/a | 27 s | 5 s |
| 2026-11-16 17:39 drive, first after 4 h | n/a | 28 s | **122 s** (+2 s, +4 s) |
| 2026-11-16 20:11 preview | n/a | 25 s | 5 s |
| 2026-11-16 20:38 / 21:14 / 21:32 drives | n/a | 39 / 37 / 31 s | 4 s |
| 2026-11-16 23:37 preview | n/a | 24 s | 4 s |
| 2026-11-17 01:28 preview | n/a | never in 164 s | - |
| 2026-11-17 10:10 drive, first of the day | n/a | 35 s | **123 s** (+2 s, +4 s) |
| 2026-11-17 12:31 preview | 3 s | 28 s | 4 s |
| 2026-11-17 13:20 drive | 2 s | 34 s | 4 s |
| 2026-11-17 13:31 / 13:34 previews | 4 / 3 s | 27 / 29 s | 5 / 4 s |

## The early start signal and how far ahead of the key it comes

`mfd_power.BeanWakeTrigger` (the MFD's early boot) is true while the BEAN body bus has been active
within `BACKEND_MFD_BEAN_ACTIVE_S` (15 s) and, in mode `entry` (default), the doors are unlocked
or a door is open; in mode `any`, on any bus activity. Our own BEAN transmissions are muted. The
camera uses exactly this value (every condition, every mode), gated by ACC off. The operator MFD
hold (`mfd_hold`) is applied after it and never powers the camera.

From the backend journal (rising edge of the trigger -> powerbox `acc_on` event):

| drive | entry signal -> ACC on |
|---|---|
| 2026-11-13 21:47 | 16 s |
| 2026-11-16 17:39 (first after 4 h) | 32 s |
| 2026-11-16 20:38 | 9 s |
| 2026-11-16 21:14 | 19 s |
| 2026-11-16 21:32 | 22 s |
| 2026-11-17 10:09 (first of the day) | 13 s |
| 2026-11-17 13:19 | 28 s (the demand dropped again 8-12 s before ACC) |

Median about 19 s. The signal also fires without a drive (2026-11-15 13:29 unlock, about 60 s;
2026-11-16 20:48 and 20:52, 5 s and 40 s; 2026-11-17 13:13, about 60 s, six minutes before the
drive), and it is true at once when the driver opens the door after a key-off.

## Rules (`backend/roadsense_power.py`, `RoadsenseManager.set_early_camera`)

One mechanism: the BEAN trigger in `BackendService._mfd_power_tick` feeds both the MFD and
`set_early_camera(early and not ACC, inhibit=undervoltage or shutdown_requested)`; the manager
turns a rising edge into one *early session*, which is the existing PREVIEW state (hub on, no
units, low CPU profile allowed).

| event | camera hub |
|---|---|
| rising edge of the trigger, ACC off, mode `auto` | on at the next loop pass (session starts) |
| ... within `EARLY_CAMERA_HOLDOFF_S` (120 s) of a key-off | ignored (getting out is not getting in); a new edge after that counts |
| ... parked-period budget used up | ignored |
| ... undervoltage or powerbox shutdown request | ignored |
| demand gone (bus quiet 15 s, or locked and closed) | stays on for `EARLY_CAMERA_TAIL_S` (30 s), then off |
| demand back within the tail | same session, no off/on |
| session reaches `EARLY_CAMERA_S` (180 s) or the budget left | off; the same continuous demand does not restart it |
| ACC on | session ends, PREVIEW -> STARTING with the hub left on: no blip, no 8 s settle |
| ACC on for `EARLY_CAMERA_RESET_ACC_S` (60 s) | budget refilled (a real drive) |
| undervoltage / shutdown request during a session | off at the next tick |
| operator mode `off` (or `on`) | no session; a live one ends |
| a stack stop that finishes during a session | the hub is kept (no off/on) |
| backend shutdown | session ends, a preview-only hub is cut |
| backend start | no session before the first ACC report (reconcile) |
| operator MFD hold | never powers the camera |

Environment (`/etc/prius/backend.env`, all optional):

| variable | default | meaning |
|---|---|---|
| `BACKEND_ROADSENSE_EARLY_CAMERA_S` | 180 | longest single session (0 = feature off) |
| `BACKEND_ROADSENSE_EARLY_CAMERA_TAIL_S` | 30 | hub kept after the demand ends |
| `BACKEND_ROADSENSE_EARLY_CAMERA_BUDGET_S` | 900 | all sessions of one parked period together (0 = no cap) |
| `BACKEND_ROADSENSE_EARLY_CAMERA_RESET_ACC_S` | 60 | ACC on this long refills the budget |
| `BACKEND_ROADSENSE_EARLY_CAMERA_HOLDOFF_S` | 120 | no session this long after a key-off |

The budget lives in memory: a backend restart refills it. The feature needs the MFD power
manager (`BACKEND_MFD_ENABLED=1`), because the trigger is evaluated in its tick.
`/api/v1/state` `roadsense.early_camera` shows `active`, `demand`, `session_s`, `used_s`,
`budget_s`, `sessions` and `last_end`; the journal logs every session start, end (with reason
and budget) and every ignored demand.

## Timing budget on the recorded drives

| | before | with early power |
|---|---|---|
| camera hub power | ACC + 3-5 s | ACC - 9 to -32 s (median -19 s) |
| stack start | ACC + 13-15 s (8 s settle) | ACC + 0-5 s (next manager tick, no settle) |
| warm drop-out (power + 25-40 s, 4-5 s away) | ACC + 28-45 s, ends by + 50 s | ACC - 7 to + 31 s, ends by about + 36 s; with the longer leads it is over before the stack is up |
| cold drop-out (122 s away) | road picture back at about ACC + 2:50 | back about 12-37 s earlier, i.e. still about ACC + 2:15 to 2:40 |

So the warm case moves mostly or entirely ahead of the drive; the cold case is only shortened by
the lead. Nothing in this repository can shorten the camera's own 122 s.

## USB port power cycle: tested, not shipped

Idea (lane L16): when the camera does not return within 10 s, cycle its hub port
(`uhubctl -l 1-1.4 -p 4 -a cycle`). Findings, 2026-11-17, parked, two preview sessions:

* The VIA hub advertises per-port power switching (`wHubCharacteristic 0x00e9`, `ppps`,
  `PortPwrCtrlMask 0xff`); `uhubctl` 2.5.0 is installed on the phone.
* `uhubctl -a off` on port 4: the status read `0000 off`, but one second later the port was
  `0503 power ... connect` again (the kernel re-powers a port switched off through usbfs).
* The kernel's own switch (`/sys/bus/usb/devices/1-1.4:1.0/1-1.4-port4/disable` = 1) held
  `0000 off` for 4 s. After `disable` = 0 the camera was back on the bus within 1 s and did NOT
  leave it again in the following 54 s. After a real power-up it needs 2-4 s to enumerate and
  always leaves the bus 24-39 s later. The camera therefore kept running through the port
  switch: either the hub does not really switch VBUS or the camera bridges a 4 s cut (dashcams
  usually carry a hold-up capacitor). Which of the two was not separated.
* During the long absence the camera has disconnected itself from the bus; a port cycle that
  does not restart it cannot bring it back. No watchdog was shipped.

Alternative: the backend cuts the whole camera hub (relay ch4) for a few seconds. That surely
restarts the camera (every preview / drive power-up above), and `RoadsenseManager` already owns
ch4 through `port_power.RelayPortPower` (VBUS off first, data port off after the disconnect,
on in the reverse order), so a "cut ch4 for N s while the camera has been away 10 s" step would
be a small addition. Not done, because it is not clearly sound: the head unit's USB link (port 2
of the same hub) drops with it; the restarted camera begins its own sequence again (enumerate
2-4 s, drop-out 25-40 s later, 4 s or possibly 122 s); and nothing shows that a restart shortens
the 122 s. Decide it after the cold start below.

## Power budget

Documented: relay ch4 (VIA hub + dashcam) draws +84 mA at 12 V, about 1 W
(`powerbox/README.md` relay table, `POCO/POWER.md` camera preview). The head unit is fed by the
car, not by this hub. Nothing in this repository or the CMDB gives the draw during the camera's
power-up or while it writes to its TF card, or the hub's share.

| case | camera hub on-time | energy at 12 V |
|---|---|---|
| one session (cap) | 180 s | 4.2 mAh (0.05 Wh) |
| typical unlock-and-drive | lead (9-32 s) | under 1 mAh |
| unlock, take something, lock | demand + 15 s bus timeout + 30 s tail, about 60-90 s | 1.4-2.1 mAh |
| worst case per parked period (budget) | 900 s | 21 mAh (0.25 Wh) |

The MFD board is woken by the same trigger and has its own 20 min grace; its draw is not part of
this table. To measure: relay ch4 current during the first 40 s after power-up (camera boot and
TF card) and steady, with the camera recording and not.

## What the next cold start in the car must show

After at least 4 h parked, unlock, wait about 30 s, then the key. In the journals:

1. backend: `roadsense: early camera session 1: hub on for at most 180 s` at the unlock, then
   `off -> preview`;
2. kernel: `usb 1-1.4.4` enumerates 2-4 s later and leaves the bus 25-40 s after the hub power;
3. backend: `early camera session ends (ACC on)` and `preview -> starting` with no
   `camera hub power -> OFF` between them;
4. how long the camera stays away. The decisive point is whether the 122 s now start at the
   camera's own drop-out (time since power) or at ACC / READY (supply at engine start): with
   the hub powered before the key, these two moments are now apart.
