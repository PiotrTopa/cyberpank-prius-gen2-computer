# Head unit integration — facts and design (2026-10-19)

The Chinese Android head unit in the Prius becomes a managed peripheral of the car computer
(backend on the Poco F1), in the same way the Toyota MFD is one. This document records what was
measured on the unit on 2026-10-19 and the design that follows from it. Evidence lives in
`cyberpunk-prius-gateway/captures/2026-10-19/` (`CAPTURES.md`, `headunit/`) and the decompiled vendor
framework (scratch copy from the dumped system image; images in `~/backups/headunit-9520-2026-10-19/`).

## 1. Facts

| Item | Fact | Evidence |
|---|---|---|
| SoC / OS | MediaTek Autochips AC8227L, Android 8.1 (SDK 27; `ro.build.version.release` claims 10.0), build `9520_00033_V001`, kernel 3.18.79 | `headunit/getprop.txt` |
| Access | Second USB port is a dual-role OTG port in ADB device mode (0e8d:201c, ADB only). `adb devices` authorises without a prompt. `su 0` gives uid 0 (su is 4750 root:shell), SELinux permissive, dm-verity disabled, `/system` remounts rw. `adb root` is refused, but `service.adb.root=1` + adbd restart makes adbd root | CAPTURES.md "Root inventory" |
| Screen | 1280×720, 160 dpi | `wm size` |
| Vendor stack | "xy"/"xyauto"/"acloud" framework inside `system_server` (`com.mediatek.server.NewkeypadMonitor`, `CanbusMonitor`, `CanbusService`), Autochips `AudioSettings`/`Audio1701Settings`, launcher `com.android.launcher` (xyauto), ZLink `com.zjinnova.zlink` (CarPlay / Android Auto), CAN-box apps `com.xygala.canbus`, `com.xygala.pvcanset` | decompiled `mediatek-services`, `framework`, APKs |
| MCU link | `/dev/ttyMT2`, 115200 baud, opened by `NewkeypadMonitor` (native `libSerial_key.so`); reopened on every QuickBoot power cycle (`autochips.intent.action.QB_POWEROFF/QB_POWERON`) | decompiled `NewkeypadMonitor`, strace |
| CAN box link | `/dev/ttyMT3`, 38400 baud (`canbus_bandrate`), `CanbusMonitor`; profile set to a "BNR-Toyota Prado … --disconnect" entry; nothing ever answers; the unit writes `2E C4 01 vv chk` (volume) on each volume change | decompiled `CanbusMonitor`, `settings_system.txt`, strace |
| Frame format | `5A A5 LEN CMD DATA… CHK`, LEN = bytes after it incl. CHK, CHK = (−Σ(CMD..DATA)) mod 256 | `headunit/mcu_trace.txt` |
| MCU → Android | `10 SS KK` key (SS 01 press / 02 release, press repeated every ≈100 ms while held); `12 xx` device state (1/2 headlamp off/on, 3/4 ACC off/on, 5/6 brake, 7/8 reverse video off/on, 13 ACC pre-on); `05` power-key; `21`/`3A` time; `66` reverse display; `69 0E ll rr` every 5 s ("B_vibration_voltage" left/right); `82` battery (→ `sys.xy.mcu.battery`); version/upgrade/key-learning/SN | decompiled `key_dev_read`, strace |
| Android → MCU | `05` power (06 power off, 15 ACC-off ack, 16 sleep, 0C reboot, 13/14 lock/unlock, A2/A3 amplifier); `06` mute; `07 05/06` force reverse view in/out; `31` time (BCD); `0B 01 RR GG BB 05 64` key backlight colour (the 1 Hz rainbow comes from `com.xygala.pvcanset` via `action.mcukey.set`) | decompiled, `headunit/mcu_rw_trace.txt` |
| Wheel keys (verified with the user) | 06 PLAY_PAUSE (labelled MODE on the wheel), 0A FORWARD (NEXT), 0B BACKWARD (PREV), 0C VOL+, 0D VOL−, 0F PHONE (answer), 10 HANGUP (end call), 50 VOICE (SPEAK; no release frame). The MFD bezel buttons MAP/INFO never reach the unit (they stay on the Toyota AVC-LAN side) | CAPTURES.md wheel map |
| Key routing in the framework | `key_dev_read` → `doforKeyInfo`. VOL± are applied there directly (never reach `onKey`), everything else goes through the vendor key table → Android `KEYCODE_MEDIA_*` injection and broadcasts: NEXT/PREV/PLAY → `xy.android.nextmedia` / `xy.android.previousmedia` / `xy.android.playpause`; PHONE/HANGUP/VOICE → broadcast `com.zjinnova.zlink` with `command=REQ_SPEC_FUNC_CMD`, `specFuncCode` 5 / 6 / 1500; VOL → `xy.android.volume.change` extra `volume` (0–63) | decompiled `NewkeypadMonitor` |
| Volume chain | `AudioManager.setStreamVolume(STREAM_MUSIC, 0..63)` → `AudioService.autosetTDA7729MasterVolume` → `vendor.autochips.hardware.audio@1.0` HAL → ALSA mixer `TDA7729*_Switch`; `ro.config.media_vol_steps=63`. The ADAU1701 DSP is driven through `/dev/adau1701` (`libadau.so`); AudioService also broadcasts `com.autochips.adau1701set` extra `vol`. A plain `media volume --set` from the shell did not change the level on 2026-10-19; the vendor setter is the broadcast `xy.android.gtpkey.volset` | decompiled `AudioService`, `AudioSettings`; CAPTURES.md volume test |
| Source | `action_get_cur_mediasource` → answered by `xy.notify.cur.mediasource` extra `mediaSourcepkg`; related: `xy.kill.mediaapp`, `xy.release.mediaappres`, `xy.android.radio.isplay`, `xy.fmradio.quit`, `xy.android.hw.fmpower.enable`, `xy.android.4735.fm`, `xy.btmusic.stop` | broadcast history |
| Control channels needing no modification | Receivers registered in `system_server` without permission: `canbus.intent.action.cmd` extra `mcu_cmd` (send any MCU frame ≤16 bytes); `action.mcukey.set` (LED colour); `canbus.intent.action.key` and `xy.android.gtpkey.*` (inject keys into the vendor routing); `xy.android.gtpkey.volset` (set volume); `xy.android.lightscreen`, `xy.android.set.atcmaxbright` (backlight); `action_get_cur_mediasource` | decompiled `NewkeypadMonitor` |
| Observation without touching the port | `settings put system newkeypad_debug 1` logs raw MCU bytes and "key :K state:S" to logcat | decompiled |
| ZLink | Reports `status` (CONNECTED / DISCONNECT / MAIN_AUDIO_START / MAIN_AUDIO_STOP / PHONE_CALL_ON / PHONE_CALL_OFF / MAIN_PAGE_SHOW …) and `phoneMode` (auto / carplay / …) in broadcast `com.zjinnova.zlink`; receives `command` ACTION_ENTER / ACTION_EXIT (on ACC) and REQ_SPEC_FUNC_CMD on the same action. Its window has no FLAG_SECURE and it already owns a `TYPE_APPLICATION_OVERLAY` window, so overlays work on this build. `resizeableActivity=false` (no split screen). Its code is packed; behaviour under an overlay must be tested live | decompiled manifest, `dumpsys window` |
| Launcher | No widget host. Its music tile is fed by `update.widget.*` broadcasts (`curplaysong`, `PlayState`, `proBarvalue` / `proBarmax`) | decompiled launcher |
| Wi-Fi | `NewkeypadMonitor` calls `setWifiEnabled(false)` on `xy.android.acc.on` / `acc.off`; ZLink uses Wi-Fi P2P for wireless projection | decompiled |
| USB | RNDIS gadget configurations exist in the init rc, so the OTG port can be a network device as well as ADB | `headunit/init_rc.txt` |
| JBL amplifier | Not on the AVC-LAN: with the unit's amplifier switch on, the display's roll call (14:25:21) was answered only by our nav emulator and the gateway node | `captures/2026-10-19/emu2_amp_on.ndjson` |

## 2. Use cases

1. **Wheel keys through the car computer.** Every wheel key is visible on the unit. Policy proposal:
   VOL± and PLAY/NEXT/PREV stay with the unit while it is the active audio device (it owns the amp
   chain); the computer receives a copy of every key for its own logic (e.g. long-press MODE
   switches the MFD page, VOICE while the computer has focus triggers the computer's function).
   PHONE/HANGUP go wherever the phone is (ZLink today).
2. **Volume and source sync.** The unit announces every level change (`xy.android.volume.change`)
   and the active source (`xy.notify.cur.mediasource`); the computer mirrors them into `AudioState`
   (already on a 0–63 scale) and sets them back through `xy.android.gtpkey.volset` and the media
   broadcasts. Mute via MCU cmd `06`.
3. **Our content on the head unit.** (a) An overlay window (`TYPE_APPLICATION_OVERLAY`, granted by
   appops) on top of Android Auto for alerts, climate, energy flow; (b) our own full activity when
   nothing is projected; (c) the launcher's music tile fed with our data via `update.widget.*`.
4. **Illumination, power, state.** Key backlight colour to the Prius theme (`0B` frame via
   `canbus.intent.action.cmd`), screen backlight (`xy.android.lightscreen`), ACC / reverse / headlamp
   state from MCU cmd `12` relayed to the backend, battery from cmd `82`, sleep/wake via cmd `05`.
5. **Vehicle data.** The CAN box is absent (ttyMT3 silent). The computer can feed the unit's
   vehicle-info consumers directly through the `CANBUSDISPINFO*` broadcasts if any screen of the unit
   is wanted for that; otherwise ignore.
6. **Phone/BT state and reverse camera.** ZLink status broadcasts give phone-call state; MCU `12 7/8`
   and `66` give reverse; `07 05/06` can force the reverse view from the computer.
7. **Health reporting.** Agent heartbeat, ZLink status, current source, volume and MCU state as a
   `headunit` section of the backend state.

## 3. Architecture

- **Agent app on the unit** (Android, API 27), installed as a privileged system app on the writable
  `/system` so it can register the vendor broadcasts, hold the overlay permission and run a foreground
  service. It needs no exploit: the unit ships root and a writable system partition; the images in
  `~/backups` restore it. Responsibilities: subscribe to the vendor broadcasts and MCU state, forward
  them to the backend, execute backend commands (volume, source, LED, backlight, overlay content,
  key injection), expose a local status page.
- **Optional key splitter.** Only if the policy needs to *suppress* a wheel key from the vendor
  handler: a small daemon started from init that owns `/dev/ttyMT2`, forwards frames to a pty that
  `NewkeypadMonitor` opens instead, and filters/forwards keys. It must survive the QuickBoot reopen
  and must never drop the ACC/sleep handshake (cmd `05`). This modifies `/system`; the no-modification
  route (observe + broadcasts) covers everything except suppression.
- **Transport.** Preferred: USB from the unit's OTG port into the Poco's hub as RNDIS (static
  address, like the Pi's gadget link), carrying ZMQ to the backend. Fallback: Wi-Fi to the Poco's
  access point, with the caveat that the vendor framework toggles Wi-Fi on ACC changes and ZLink uses
  Wi-Fi P2P.
- **Backend side.** First version needs no backend change: events arrive as `input_event` /
  `button` commands and state over the existing ZMQ / REST / WebSocket contracts; later a
  `headunit` state section and `headunit_*` commands.

## 4. Risks

- Suppressing VOL± is impossible by settings; it needs the splitter or a framework patch.
- A dead splitter breaks the ACC/sleep handshake; it must be supervised by init and fail open.
- ZLink is packed; overlay behaviour and key handling must be verified on the car.
- Wi-Fi is contested by the vendor framework and ZLink; prefer USB.
- Restore path: `ANDROID_system.img`, `VENDOR.img`, `BOOTIMG.img` etc. with SHA256 sums; there is no
  documented fastboot/SP-Flash route, so write only to `/system` and keep the originals.

## 5. Plan

1. Offline: agent app skeleton (receivers, overlay service, ZMQ client), backend `headunit` state.
2. On the car (unit on the cable): `newkeypad_debug 1`; confirm every broadcast listed above fires;
   test `xy.android.gtpkey.volset`, `canbus.intent.action.cmd` with the LED frame, `lightscreen`;
   overlay over ZLink with Android Auto running; pull the amp-button trace left on the unit.
3. Install the agent as a system app; verify it survives reboot and QuickBoot.
4. USB RNDIS link to the Poco; backend integration; wheel-key policy.
5. Only if suppression is needed: the ttyMT2 splitter.

## 6. Open questions for the user

1. Which keys should the unit keep, which should the computer own (decides whether the splitter is needed)?
2. USB or Wi-Fi to the Poco?
3. Is the head unit the car's permanent audio amplifier (then it owns volume and the computer mirrors), or will the JBL amp return on the AVC-LAN?
4. How much of our content on the unit: overlays only, or a full screen when Android Auto is idle?
