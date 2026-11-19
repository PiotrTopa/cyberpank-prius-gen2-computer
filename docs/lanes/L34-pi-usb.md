# L34: MFD Pi unreachable / "USB tree STORM" (2026-11-19)

Read-only investigation, 14:19-14:30 CET, phone boot of 12:17 (kernel journal, backend journal,
Prometheus on the P52, one ssh to the Pi). Times CET.

## Findings

1. **The Pi does not drop alone: the top hub `1-1` leaves the bus and takes everything with it.**
   61 `usb 1-1: USB disconnect` between 12:43:54 and 14:20 (65 by 14:27). Each time: `-71` on all hubs
   (`clear tt`, `1-1.4-port2 cannot reset`), root port sees the hub go and return in 0.3-0.5 s, every
   device re-enumerates. No over-current, no xhci/dwc3 error, no `device descriptor read` error.
2. **The drops happen only while relay ch3 (CAN hub `1-1.3`: gateway + panda) and ch5 (RS485 rail,
   switched in the same second) are on.**

   | ch3/ch5 | Window | Tree drops | Phone state |
   |---|---|---|---|
   | off | 12:19-12:42:37 | 0 | idle, camera previews |
   | ON (ACC) | 12:42:37-12:56:39 | 15 | roadsense running |
   | off | 12:56:39-13:13:11 | 0 | incl. roadsense 13:11:53-13:13 |
   | ON (manual `usbtest`, ACC off) | 13:13:11-13:19:41 | 11 | idle 0.31 A, 4.07 V, 33 C, USB 5 V 5.00-5.04 V |
   | off | 13:19:41-13:27:45 | 0 | roadsense 13:23:44-13:27:25: 1.7 A, 4.00 V, 68-76 C |
   | ON (manual, ACC off) | 13:27:45-13:35:40 | 10 | idle 0.32 A, 33 C, camera hub off |
   | off | 13:35:40-13:56:19 | 0 | Pi off 13:39-13:50, back 13:51 |
   | ON (ACC, parked, READY 13.8 V) | 13:56:19- | 25 by 14:20 | roadsense, 1.6 A, 68 C |

   So phone load, heat, phone supply sag, ACC, the camera hub, the head unit and roadsense are not
   needed and not sufficient. First drop 12-77 s after ch3 on. Every drop is seen first by the panda
   reader (`Panda link lost: bulk read 0x81 LIBUSB_ERROR_IO` ~20 ms before the kernel line), 4-50 s
   after `Panda connected`.
3. No supply collapse: powerbox uptime 9349 s (no reset since 11:47, `reset_cause 1`,
   `relay_fix_count 0`, `relay_err_count 0`), gateway uptime 1868 s at 14:27 (no reset since ch3 on at
   13:56:19, through 25 drops). Relay read-back `[null,1,1,1,1,0,0,0]`. No PCF8574 reset today.
4. **The Pi is healthy.** `throttled=0x0`, 32 C, `mfd.service` active, `NRestarts=0`, old frontend
   connects to the new backend (`Backend link UP`). All its reboots today are the backend's power cuts
   (10 in this phone boot: 12:44:39, 12:49:25, 12:54:10, 14:01:11, 14:06:00, 14:12:59, 14:15:59,
   14:19:00, 14:21:50, 14:27:11). Gadget binds 46 s after power-on; on a quiet tree `usb0` + ping at
   55 s (13:50:35 -> 13:51:30).
5. After a tree drop the Pi usually re-enumerates by itself in 2 s (21 of 21 in the 13:15-13:35
   windows). In the ACC windows its port sometimes flaps for 1-2 min (`new high-speed USB device
   number N` repeated without descriptors or an error; on the Pi `new device is high-speed` at 46 s and
   108 s, `new address` only at 109 s). Then the 45 s watchdog cuts its power, the boot is hit by more
   drops, and the 180 s boot timeout cuts again (14:15:59, 14:19:00).
6. The storm guard deployed at 14:10 (6df6820) still power-cycles: it only waits for 45 s without a
   tree reset, and gaps of 45 s are common at 1-1.5 drops/min; the doubling resets on any good ping.
   Timers are unchanged against 0567a3a (45 s unreachable, 180 s boot).
7. The "USB hub problem" report is the backend's new `USB tree STORM: hub 1-1 reset 3 times in 10 min`
   (ERROR, `backend/usb_tree.py`, at 14:07:01 and 14:12:10): top hub `devnum` changed 3 times in 10 min.
8. The phone reboot at 12:17 was a requested one (`sudo systemctl reboot` from an ssh session of
   `user` at 12:11:18; shutdown took 6 min), not a reset.
9. History: the morning boot was quiet for 14 min with ch3 on (10:41:16-10:55:46, panda 1685 frames/s),
   then 21 drops by 11:19 and 28 in 11:31-11:44. 2026-11-17/18: 8-9 tree drops per boot. So the branch
   got ~20x worse on 2026-11-19 at 10:55 and stayed so, parked or not.

## Not decided (needs a state change, owner's go)

Which part of the switched branch upsets the top hub: CAN hub `1-1.3` or its 12->5 V supply/VBUS
back-feed into port 3, its upstream cable, the panda (OBD 12 V and ground: second ground path), the
gateway (CAN / AVC-LAN / RS485 wiring), or the ch5 satellite rail. Tests, in order: ch5 alone; ch3
alone; ch3 with the panda unplugged; ch3 with the gateway unplugged; CAN hub on another top-hub port
/ other cable.

## Part 2 (14:39-15:13 CET): numbering, anatomy, relay test with the ignition on

### Relay numbering (one convention)
Board relay N = PCF8574 pin P(N-1) = firmware channel N = backend `ch` N (`RELAY_PIN_MAP = {n: n-1}`;
the removed `powerbox/RELAY-CHANGE-PLAN.md`, last in 15fd721^, says the same). `/api/v1/state`
`powerbox.relays` is a 0-based list (index = N-1).

| Relay | Load | Notes |
|---|---|---|
| 1 (NC) | the powerbox's own supply (hard restart) | never a load |
| 2 | MFD Pi Zero, 100-115 mA at 12 V | MFD power manager |
| 3 | CAN hub `1a40:0101` (`1-1.3`): gateway + panda, 50 -> 155 mA | gateway power path |
| 4 | camera hub VIA `2109:2817` (`1-1.4`): dashcam, head unit on its port 1; +84 mA | roadsense manager |
| 5 | RS485 satellite rail (12 V into the car) | "untested on this board"; nodes 106 DRL, 107 rain/light, 110 VFD all offline, `devices_seen 0` |
| 6-8 | unused | |

The fan is not on a relay (5 V buck, PWM GP29). The owner's "relay 4" = camera hub, "relay 5" = RS485 rail.

### Relay test, ignition on, parked (PARK, 0 km/h, 13.7-14.0 V), backend REST only
Mechanism: `roadsense_mode off` (stack and relay 4 off), `set_relay {channel:3,on:false}` (works with
ACC on: gateway power path, stays off), `set_relay {channel:4}`; relay 5 cannot be switched off with
ACC on by any supported command (`acc` holder). Script `/tmp/l34_test.py` on the phone, restores
`set_relay 3 on` + `roadsense_mode auto`. Each state 131 s after a 12 s settle.

| When | Relays on (2 always) | roadsense | Tree drops | 12 V current |
|---|---|---|---|---|
| 14:40:28-14:50:28 (before the reboot) | 3 4 5 | running | 10 | ~1.0 A |
| 14:51:47-15:00:50 (after the clean reboot) | 3 4 5 | running | 7 | ~1.0 A |
| 15:02:07-15:04:18 | 5 | off | 0 | 0.27 A |
| 15:04:32-15:06:43 | 3 5 | off | 0 | 0.41 A |
| 15:06:58-15:09:09 | 5 | off | 0 | 0.28 A |
| 15:09:24-15:11:35 | 4 5 | off | 0 | 0.38 A |
| 15:11:35- (restored) | 3 4 5 | starting/running | 1 at 15:11:55 (20 s after) | 1.0-1.2 A |

Zero drops in the whole 10.5 min with roadsense off (15:01:03-15:11:35), in every state; the first drop
came 20 s after the stack and all relays were back. The `3 5` state (0 in 143 s) is NOT decisive: at
the 13:15-13:35 rate (1.3/min, same relays, roadsense off, ACC off) about 3 were expected (p ~ 5 %).
So the earlier "relay 3/5 on is enough" (ACC-off windows) and this run disagree; states not yet run:
`3 4 5` with roadsense off, and roadsense running with relay 3 off.

Side findings of the run:
* With relay 4 "off" (15:01:03) the camera hub stayed on the bus (`Port camera: finishing OFF (no
  disconnect seen within 3.0s)`), threw `-71` on its port 2 and re-enumerated 5 times by 15:11: it is
  still fed from somewhere (top-hub VBUS / back-feed). Earlier today it left within 0.2-0.9 s.
* 12 V at the box falls with its own current: 13.98 V at 0.27 A, 13.89 V at 0.41 A, 13.65 V at 1.0 A,
  13.6 V at 1.6 A, 12.4 V at >= 3.2 A (14:04:28 and 14:04:46-50, sensor clipped): about 0.45 ohm in
  the box's 12 V feed or ground return. Unexplained 3.2 A events at 14:04.
* First launch of the script died with its ssh session (15:01:21); roadsense off / relay 3 off stood
  for 30 s until the rerun.

### Anatomy (read-only)
* Per drop (78 in the 12:17 boot): the first `-71` line is on `1-1` TT (27), `1-1.4-port2` camera
  (26), `1-1.3` TT (15), none (10); errors lead the disconnect by 0-118 ms (median 20). All hubs fail
  together: no downstream device complains first. Same picture in the morning boot and on 17 Nov.
* No periodicity: intervals median 37 s, widest bin 10-30 s, tail to 220 s; no fixed delay after
  `Panda connected` (0.9-60+ s, flat), CAN setup, ssh to the Pi, uhubctl, satellite jobs, relay
  commands. Rate with the fan on 0.97/min, off 0.93/min: the fan is not it. (The "0 rpm" of TODO 3h(c)
  is the tach value in the FAN ack at the instant of the command, before spin-up.)
* Isolated `-71` on `1-1.3` without a tree drop are only the 17-line bursts at each relay 3 off.
* Hubs: `power/control auto`, autosuspend 0, `runtime_status active`, suspended time 0, no LPM
  attribute: autosuspend is not involved. No over-current / xhci messages.
* Morning: relays 2 3 4 5 all on from 10:41:16, 12 min of driving up to 62 km/h (10:43-10:55) without
  a tree drop (one Pi-only blip 10:52:09); first drop 10:55:46 at walking speed at the end of the
  drive, then continuous. Nothing in the relay/satellite/panda state changed at 10:55.
* RS485 traffic with ACC on: ~3 frames/s to node 110 (VFD) through the gateway; no node answers.
* 12 V around drops (1 Hz): no dip before a drop (min 13.64 V); current falls ~0.36 A after one.

## Part 3 (15:15): not run
The script found the car in DRIVE at 11.3 km/h before switching anything and stopped.

## Part 4 (16:13-16:51 CET): ignition off, parked, on the battery (12.84 -> 12.63 V), after the 15:34 power cycle
Script `/tmp/l34_test4.py` on the phone as a user unit (`systemd-run --user`), backend REST only:
`mfd_hold` (Pi as canary), `satellite_power_hold l34` (relay 5), `set_relay 3`, `roadsense_mode on/off`
(stack + relay 4). 1 Hz log `/tmp/l34_test4.log`. Restored and verified at 16:51:28: holders `[]`,
`roadsense_mode auto`, stack off, relays 3/4/5 off, MFD in its normal grace.

### Onset (phase A)
Boot of 15:34 had 0 tree drops through the 15:36-15:45 drive and the parked hour. Everything on at
16:14:12 (relays 2-5, stack running). First drop at 16:16:51 (2 min 39 s later), then 16:17:46,
16:19:18: storm. At onset: 12.29-12.50 V, 0.7-1.4 A, SoC 82.7 C (72 C at 16:14, fan to 40 % at
16:16:22), first error lines 92 ms before the disconnect on `1-1.4-port2` (camera) and `1-1.3` TT
together, then `1-1` TT. In the journal that minute: fan on 16:16:22, CAN setup re-sent 16:16:24
(car asleep, panda 1 frame/s), the 30 s ssh to the Pi 16:16:44. No voltage dip in the 1 Hz samples.
A drop also happened at 16:14:01, before phase A, with relay 3 and 5 still OFF: 47 s after the Pi was
powered (16:13:14), i.e. at the moment its USB gadget attaches.

### Counts (4 min each, 12 s settle excluded; "present" = share of 1 Hz samples the device was on the bus)

| When | Relays on (2 always) | Stack | Tree drops | CAN hub / camera hub present | Phone | 12 V |
|---|---|---|---|---|---|---|
| 16:14:12-16:19:18 (A) | 3 4 5 | running | 3 | 99 % / 98 % | 72-85 C | 12.41 V, 1.01 A |
| 16:19:44-16:23:44 | 4 5 | running | 0 | 0 % / 100 % | 79-84 C | 12.43 V, 0.89 A |
| 16:24:33-16:28:33 | 3 5 (relay 4 stayed off: script slip) | off | 1 | 99 % / 0 % | 34-38 C | 12.64 V, 0.40 A |
| 16:29:04-16:33:04 | 3 | off | 2 | 98 % / 0 % | 32-33 C | 12.65 V, 0.37 A |
| 16:33:25-16:37:26 | 5 | off | 0 | 0 % / 0 % | 30 C | 12.69 V, 0.28 A |
| 16:37:55-16:41:55 | none | off | 0 | 0 % / 0 % | 29 C | 12.70 V, 0.26 A |
| 16:42:34-16:46:34 (control) | 3 4 5 | running | 4 | 97 % / 97 % | 69-85 C | 12.32 V, 1.06 A |
| 16:46:48-16:50:48 (repeat) | 4 5 | running | 1 (16:47:51) | 0 % / 99 % | 73-77 C | 12.37 V, 0.89 A |

Relay 3 on: 10 drops in 17.1 min (0.58/min). Relay 3 off: 1 drop in 16 min. Relays really switched:
with relay 3 off the CAN hub, gateway and panda were absent in every sample; with relay 4 off the
camera hub was absent in every sample (clean off this time).

Outside the measured windows: drops at 16:24:15 and 16:42:20, both within seconds of relay 3 being
switched ON (CAN hub attaching to top-hub port 3).

### What the Pi's power cuts do
MFD watchdog cuts at 16:18:34, 16:43:38, 16:46:28 (Pi on the bus only 8 % of the control state).
Tree drops followed at +44 s (16:19:18), +18 s (16:43:56), +4 s (16:46:32); the Pi's gadget attaches
about 46 s after power-on. Same in the afternoon logs: cut 12:44:39 -> drops +18 s, +47 s; cut
12:49:25 -> +53 s (Pi attach attempt 2 s before); cut 12:54:10 -> +16, +26, +45 s; cut 14:15:59 ->
+38, +55 s (attach attempts 1-4 s before, then `usb 1-1-port2: attempt power cycle`).

### Reading
Proven: relay 5 (RS485 rail) alone does nothing; the camera hub and the roadsense load are not
needed (relay 3 alone, phone at 32 C, 0.37 A, 12.65 V: 2 drops in 4 min); the CAN-hub branch on
top-hub port 3 multiplies the drop rate by about 10; CAN traffic is not needed (car asleep).
Suspected: the top hub `1-1` (or its 5 V feed / upstream link) cannot stand events on its downstream
ports: a self-powered device attaching (CAN hub on port 3, Pi on port 2) and whatever the powered
CAN hub does continuously (its own 5 V meeting the top hub's VBUS on port 3 is the candidate). The
Pi watchdog adds a loop: drop -> Pi unreachable -> power cut -> Pi attaches 46 s later -> drop.
