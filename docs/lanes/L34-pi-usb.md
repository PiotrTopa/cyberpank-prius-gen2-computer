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

## Part 5 (17:07-17:17 CET): relay 3 only, and "is it CAN-side software?"

### Test: only relay 3 on, Pi off, relays 4/5 off, stack off, ignition off
17:11:33-17:16:33 (Pi's grace had ended at 17:11, so no Pi on the bus and no power cuts). Top hub with
only the powerbox and the CAN hub (gateway + panda). Phone 20-22 C, 12.64 V, 0.26 A (max 0.36 A).
**2 tree drops in 5 min (17:11:49, 17:15:19).** First errors 6-32 ms before the disconnect:
`clear tt` on `1-1.3` for the gateway's serial IN endpoint and the panda's 0x81, and on `1-1` for the
powerbox's serial IN: every full-speed device at once. 12 V flat through both (12.63-12.65 V).
Restored and verified 17:17:13: all relays off, holders `[]`, `roadsense_mode auto`.
`usbmon` is not available on this kernel (no module in `/lib/modules/7.1.0-rc1-sdm845`, kernel
lockdown `integrity`), so there is no transfer-level capture.

### History against changes
Tree drops per minute of relay 3 on (boot clocks jump, so only boots with a sane clock are rated):

| Boot | Gateway fw | Backend CAN code | Relay 3 on | Tree drops | Rate |
|---|---|---|---|---|---|
| 17 Nov 10:38-16:10 | 3.2.11 | 16-17 Nov commits | n/a | 1 | low |
| 17 Nov 16:10-19:21 (two drives) | 3.2.11 | same | n/a | 7 | low |
| 17 Nov 19:21 - 18 Nov 10:30 (3.3.0 flashed 21:47, parked test 18 Nov 09:52-10:30) | 3.2.11 -> 3.3.0 | + 95ae1e5/956d408 (17 Nov 20:49-21:22) | 38 min | 3 | 0.08/min |
| 18 Nov 21:11 - 19 Nov 09:35 (bench) | not connected | - | 0 | 12 (all with relay 3 off) | - |
| 19 Nov 10:39-11:46 | 3.3.0 | v2 deploy of 06:50 (no CAN change) | 53 min | 49 (0 in the first 14 min) | 0.92/min |
| 19 Nov 12:19-14:50 | 3.3.0 | same (+ `usb_tree.py` 14:10) | 82 min | 88 | 1.07/min |
| 19 Nov 14:51-15:34 (soft reboot) | 3.3.0 | same | 33 min | 12 | 0.36/min |
| 19 Nov 15:34- (power cycle) | 3.3.0 | same | drive 15:36-15:45: 0; tests: 0.5/min | | |

Car repo: no commit touching the panda reader, the gateway link, CAN setup, discovery or port power
after 95ae1e5 (17 Nov 21:22); gateway repo: last firmware change b6f60d3/e8556a8 (17 Nov, 3.3.0).
That code and firmware ran on 18 Nov at 0.08/min and for the first 14 min of 19 Nov 10:41 without a
drop. No software change lines up with 10:55 on 19 Nov.

### What the host does on that branch (car asleep)
* Panda (`io/panda_can.py`, libusb): on connect `set_safety_mode` (silent), `set_power_save 0`,
  heartbeat, reset comms, three identity reads; then continuously a bulk IN on 0x81 with a 20 ms
  timeout (about 50 timed-out and cancelled transfers per second while the bus is quiet), a heartbeat
  control transfer every 1 s, a health read every 5 s. Reconnect 2 s after an error; no USB reset.
* Gateway (CDC serial): CAN setup (16 commands) and nav personality check on every connect; the
  solicited watchdog re-sends the setup when no reply comes (120 s spacing seen while asleep); RS485
  discovery job every 5 min; the gateway's own heartbeat 1/s.
* Port power: `uhubctl` data-enable + relay only on relay 3 edges.

Last logged host action on the branch before each drop (part 4 and 5, relay 3 on):

| Drop | Last action | Age |
|---|---|---|
| 16:16:51 | CAN setup re-send (watchdog) | 27 s |
| 16:17:46 | panda connect | 49 s |
| 16:19:18 | RS485 discovery job | 11 s |
| 16:24:15 | CAN setup re-send | 2 s (12 s after panda connect, relay 3 just on) |
| 16:28:15 | panda connect | 235 s |
| 16:30:22 | CAN setup re-send | 69 s |
| 16:32:08 | panda connect | 100 s |
| 16:42:20 | gateway discovery | 0.1 s (relay 3 on 4 s before) |
| 16:42:49 | CAN setup re-send | 10 s |
| 16:43:56 | Pi relay cut | 18 s |
| 16:44:53 | panda connect | 50 s |
| 16:46:32 | Pi relay cut | 4 s |
| 17:11:49 | CAN setup re-send | 12 s |
| 17:15:19 | panda connect | 204 s |

No fixed delay (0.1-235 s). Two drops came more than 200 s after any logged action, with only the
continuous polling running. Same result as the 78 drops of the afternoon (part 2).

### Verdict on "CAN-side software jams USB"
A recent CAN-side software CHANGE as the cause: refuted by the history. The continuous host traffic to
the two controllers (above all the 50 cancelled bulk reads per second on the panda while the bus is
quiet) as the load under which a marginal hub fails: undecided; there is no runtime switch to pause
the panda reader or the gateway link (only `panda_bench`, a counter reset), so it needs either the
panda unplugged or the backend started once with the panda reader disabled.

## Part 6 (17:25-17:28 CET): panda-reader-off variant NOT run, the drop-in cannot switch it off
A runtime drop-in `/run/systemd/system/prius-backend.service.d/l34-nopanda.conf` with
`Environment=BACKEND_PANDA_ENABLED=0` was installed and the backend restarted once (17:26:18; stop to
start about 12 s, no power-button press, relays re-asserted off, MFD off). The reader stayed ON
(`Panda broadcast CAN source enabled`, `panda_enabled true`): `/usr/local/sbin/prius-backend` assigns
`BACKEND_PANDA_ENABLED=1` (and `BACKEND_EXTRA_ARGS=`) as its own defaults before sourcing
`/etc/prius/backend.env`, and `run` builds the argument list itself, so neither a unit `Environment=`
nor extra `ExecStart` arguments reach the backend; only the installed env file does. No relay test
was run. The drop-in was removed and systemd reloaded (17:28); no second restart was needed because
the running backend is the normal configuration. Ways to do it: (a) one line
`BACKEND_PANDA_ENABLED=0` in `/etc/prius/backend.env` for the test, removed after (two restarts);
(b) a `/run` drop-in with `BindReadOnlyPaths=<copy of the env file with that line>:/etc/prius/backend.env`
(installed file untouched, two restarts); (c) the panda unplugged by hand (no restart).

## Part 6b (17:29-17:46 CET): panda reader off versus on, relay 3 only, back to back
One detached root unit on the phone (`l34seq`, script `/tmp/l34_seq.sh`, restore trap): backup
`/etc/prius/backend.env.l34-bak`, one appended line `BACKEND_PANDA_ENABLED=0`, backend restart,
run A; file restored from the backup (sha256 `0f98c2fb...7601` before = after, backup removed),
backend restart, run B. Ignition off, Pi off, relays 4/5 off, stack off, phone 20-22 C, 12.6-12.7 V.

| Run | Panda reader | Window | Tree drops | `-71` lines | CAN hub / gateway / panda on the bus |
|---|---|---|---|---|---|
| A | OFF (`panda_enabled false`, no panda line in the backend log; gateway link up, `Gateway identified: v3.3.0`) | 17:30:29-17:38:30 (8 min) | **0** | 0 | 100 % |
| B (control) | ON (2 panda connects) | 17:39:58-17:44:59 (5 min) | **1** (17:40:02) | 3 | 99 % |

First error of the drop in B: `usb 1-1.3: clear tt 1 (90b1)` = the panda's bulk IN endpoint 0x81, 4 ms
before the disconnect, then the powerbox's serial endpoint on `1-1`. 12 V flat (12.63-12.65 V).

Relay 3 only, all runs of the session: reader ON 5 drops in 14 min (2 in 4, 2 in 5, 1 in 5 =
0.36/min); reader OFF 0 in 8 min, where 2.9 were expected at that rate (chance of 0: about 6 %).

Reading: the host's panda reader is the load under which the tree falls (supported, one run, not yet
proven); the reader is unchanged since 16-17 Nov and ran at 0.08/min on 18 Nov, so the hub tree's
new sensitivity since 19 Nov 10:55 is still a second, unexplained ingredient. What the reader does
while the bus is quiet: a bulk read on 0x81 with a 20 ms timeout, i.e. about 50 cancelled transfers
per second on a full-speed device behind the CAN hub's transaction translator, plus a control
transfer every second.
State verified at 17:45:38: env file sha256 as recorded, no backup file, backend with
`panda_enabled true`, all relays off, holders `[]`, `roadsense_mode auto`, units `l34seq`/`l34test*`
gone, 12.68 V.

## Part 7 (18:02-18:32 CET): reader off / on / off, relay 3 only: the part 6b result does not hold
Same method (detached root unit `l34seq2`, env file backup + sha256, restore trap; four backend
restarts, all clean). Ignition off, Pi off, relays 4/5 off, stack off, phone 20-22 C, 12.55-12.69 V.

| Block | Panda reader | Window | Tree drops | Rate |
|---|---|---|---|---|
| 6b A | off | 17:30:29-17:38:30 (8 min) | 0 | 0 |
| 6b B | on | 17:39:58-17:44:59 (5 min) | 1 | 0.20/min |
| 7 OFF1 | off (`panda_enabled false`, 0 panda connects) | 18:02:54-18:12:55 (10 min) | 1 (18:05:35) | 0.10/min |
| 7 ON1 | on (1 panda connect) | 18:14:24-18:24:24 (10 min) | 0 | 0 |
| 7 OFF2 | off (0 panda connects) | 18:25:52-18:30:52 (5 min) | 2 (18:26:37, 18:29:00) | 0.40/min |
| Reader OFF, 6b + 7 | | 23 min | 3 | 0.13/min |
| Reader ON, 6b + 7 | | 15 min | 1 | 0.07/min |
| Reader ON, all relay-3-only runs of the session (parts 4, 5, 6b, 7) | | 24 min | 5 | 0.21/min |

With equal rates, 3 or more of the 4 drops of parts 6b + 7 falling in the reader-off time (23 of 38
min) has a probability of 0.48; over the whole session (3 in 23 min off, 5 in 24 min on) the two
rates do not differ either. The zero of part 6b run A was chance.

Drops with the reader off: first errors 5-7 ms before the disconnect on the powerbox's serial IN
endpoint (`1-1: clear tt (90d2)`, `(9152)`, `(9202)`) and the gateway's serial IN endpoint
(`1-1.3: clear tt (9122)`, `(91d2)`, `(9222)`), no panda endpoint (it is never opened). 12 V flat
(12.59-12.63 V, 0.22-0.25 A).

Verdict: the panda reader's polling is NOT the stressor (refuted: the tree drops at the same rate
with the panda never opened). What remains with relay 3 on and the reader off: the powered CAN hub
on top-hub port 3, the gateway's serial link (1 heartbeat/s, CAN setup re-sends) and the plugged,
idle panda. The relay-3-only rate over the evening was bursty and lower than in the afternoon
(8 drops in 47 min, 0.17/min, against 0.5-1/min with everything on).
State verified 18:32:07: env file sha256 `0f98c2fb...7601` as before, no backup file, backend active
with `panda_enabled true`, all relays off, holders `[]`, `roadsense_mode auto`, no `l34` units, 12.64 V.

## Part 8 + the owner's two blocks (18:35-18:55 CET), ignition off, stack off, phone 21 C
The 90-minute relay-3-on/off session of part 9 was called off before it started; nothing of it ran.

### Two blocks, back to back
| Block | Relays on | Window | Tree drops | `-71` lines | Pi | 12 V |
|---|---|---|---|---|---|---|
| 1: relay 3 only (gateway + panda), reader on | 3 | 18:39:48-18:46:48 (7 min) | **2** (18:43:47, 18:46:33); 1 in the first 5 min | 8 | off | 12.58 V, 0.245 A |
| 2: all the others | 2 (Pi), 4 (camera hub), 5 (RS485) | 18:49:18-18:54:18 (5 min) | **0** | 0 | on, attached 18:49:01 (first try), no watchdog cut | 12.52 V, 0.354 A |

(A 60 s relay-3-only step at 18:36:41 gave 0 drops. Switching block 2 on makes relay 3 close for
5 s, 18:48:11-18:48:16: the rail hold drives the gateway relay on its edge.) Caveat: 2 against 0 in
7 and 5 minutes is not significant alone; it agrees with every earlier comparison (relay-3-only this
evening 8 drops in 51 min; relay 3 off in quiet states 0 in 13 min; afternoon 21 in 14.5 min against
0 in 22 min).

### A. The phone's VBUS (proven / not exposed)
* The "USB 5 V" of part 1 is the PMI8998 RRADC channel `usbin_v` (`iio:device1/in_voltage0_raw` x
  19.53 mV): the voltage on the phone's own USB-C VBUS pin at the PMIC, the node its OTG boost
  drives. It is not the hub's own 5 V and not a powerbox reading (the powerbox measures only 12 V).
* Sampled at 49 Hz for 12 min (block 1 and the steps, max gap 27 ms): `usbin_v` 5.547-5.566 V
  (raw 284/285) throughout; through both drops it only moved by the same one count (19.5 mV) that
  follows the load. Boost status register 0x1109 = 0x0a in all 35 250 samples (0x0a = healthy per
  the CMDB; the latched-off value is 0x04), interrupt status 0x1110 = 0x00 (no OTG fault / over-
  current), 0x110a = 0x00, 0x1310 = 0x04 constant. Regulator `usb_vbus`: enabled, 1 user, limit 3 A.
  So: no VBUS loss of 20 ms or longer and no boost fault at a drop. Shorter dips cannot be seen.
* Second by second over the day (Prometheus), `usbin_v` does not dip at any of 21 sampled drops.
* The reading sits at 4.94-5.06 V in the 12:17 boot and at 5.43-5.57 V in the 14:51 and 15:34 boots
  (5.49 V also in the 18 Nov test, TODO 3e). Drops occurred in both regimes. Why the level differs by
  boot is not known (boost setting, or the powered hub lifting the pin: the phone does not tell).
* Not exposed by this phone: VBUS current in either direction (`usbin_i` returns "no data"), so
  neither the boost's load nor a back-feed from the powered hub can be read; charger `online 0`,
  `voltage_now 0` (it reports nothing in host mode); no typec/extcon/role nodes; no kernel message
  from the charger, boost or dwc3 drivers at any drop today (only the hub lines).
  `usbmon` absent.

### B. What the gateway + panda branch draws (powerbox INA219 on the 12 V input, 1 Hz, clips at 3.2 A)
| State | 12 V current | Step over all-off | Power step |
|---|---|---|---|
| all off (phone idle, top hub, powerbox) | 0.126 A at 12.65 V (120 s) | - | - |
| relay 3 on, panda reader ON | 0.245 A at 12.59 V (459 s; also 0.244 A in two earlier blocks) | +0.119 A | +1.50 W |
| relay 3 on, panda reader OFF (panda in power save) | 0.234 A at 12.62 V (1348 s, parts 6b/7) | +0.108 A | +1.36 W |
| relay 3 on, CAN traffic flowing (15:04, 13.9 V; against relay 5 only) | 0.406 A against 0.274 A | +0.132 A | +1.83 W |
| relays 2 + 4 + 5 on (block 2) | 0.354 A at 12.52 V | +0.228 A | +2.85 W |

The step includes the relay coil (12 V coil of the opto board, about 30-35 mA, 0.4 W; not measured
here). Without it the branch takes about 1.1 W asleep with the reader on; at an assumed 85 %
converter efficiency that is about 0.92 W = 185 mA at 5 V for the hub chip, the RP2040 gateway with
its CAN/AVC-LAN/RS485 parts and the panda together; about 23 mA of it is the panda being kept awake;
about 225 mA with CAN traffic. Typical figures: RP2040 board with USB about 25-30 mA, a 4-port
USB 2.0 hub chip about 30-70 mA, a panda about 80-150 mA awake: sum 135-250 mA. Nothing is out of
line, and the current does not change in the seconds before a drop (0.22-0.24 A flat). The panda's
hardware type is not in the backend state (USB: `bbaa:ddcc`, bcdDevice 0a00, fw `DEV-f120999e-DEBUG`,
health v9). The INA cannot see anything shorter than about a second.

State verified 18:55:36: holders `[]`, `roadsense_mode auto`, stack off, relays 3/4/5 off, the Pi in
its normal 20 min grace after the hold was released, `panda_enabled true`, 12.59 V.
