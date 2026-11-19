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
