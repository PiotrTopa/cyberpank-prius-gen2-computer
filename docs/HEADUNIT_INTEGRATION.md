# Head unit integration

The Chinese Android head unit in the Prius as a managed peripheral of the car computer (backend on
the POCO F1), like the Toyota MFD. Status: facts measured on the unit on 2026-10-19; the design in
sections 2–5 is not built (parked). Evidence: `cyberpunk-prius-gateway/captures/2026-10-19/`
(`CAPTURES.md`, `headunit/`) and the decompiled vendor framework from the dumped system image (the
image backups are kept outside the repo).

## 1. Facts

| Item | Fact | Evidence |
|---|---|---|
| SoC / OS | MediaTek Autochips AC8227L, Android 8.1 (SDK 27; `ro.build.version.release` claims 10.0), build `9520_00033_V001`, kernel 3.18.79 | `headunit/getprop.txt` |
| Access | Second USB port is a dual-role OTG port in ADB device mode (0e8d:201c, product "9520", serial `MJU8DY59WS8LMB85`, ADB only). On the car computer it enumerates at USB `1-1.4.2` behind the camera hub (`1-1.4`, VIA 2109:2817, powerbox relay ch4), ~3 s after relay 4 on and only with ACC on (seen at every ACC start on 2026-11-16). From the P52 on 2026-10-19 `adb devices` authorised without a prompt (`ro.adb.secure=1`, so the phone's own key may still get one). `su 0` gives uid 0 (su is 4750 root:shell), SELinux permissive, dm-verity disabled, `/system` remounts rw. `adb root` is refused, but `service.adb.root=1` + adbd restart makes adbd root | CAPTURES.md "Root inventory" |
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

## 2. Use cases (design)

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
8. **GNSS for the map layer.** The unit's GPS is the car's position source: the agent posts every
   raw fix to `POST /api/v1/position/gnss` on the backend, which feeds roadsense mapd. The backend
   side is built (off by default); contract, fields and clock handling:
   [POSITION_FEEDER.md](POSITION_FEEDER.md) §4. Not measured yet: whether the unit has a GNSS
   receiver with a usable antenna, its fix rate and accuracy fields. The unit runs MediaTek's GNSS
   stack (`ro.mtk_gps_support=1`, `mnld`, `/dev/gps`, `/dev/stpgps`, YGPS test app). Interim
   sender without an app: `hu-gnss-bridge` on the car computer (§7.4).

## 3. Architecture (design)

- **Agent app on the unit** (Android, API 27), installed as a privileged system app on the writable
  `/system` so it can register the vendor broadcasts, hold the overlay permission and run a foreground
  service. It needs no exploit: the unit ships root and a writable system partition; the dumped
  images restore it. Responsibilities: subscribe to the vendor broadcasts and MCU state, forward
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

## 5. Order of work

1. Offline: agent app skeleton (receivers, overlay service, ZMQ client), backend `headunit` state.
2. On the car (unit on the cable): `newkeypad_debug 1`; confirm every broadcast listed above fires;
   test `xy.android.gtpkey.volset`, `canbus.intent.action.cmd` with the LED frame, `lightscreen`;
   overlay over ZLink with Android Auto running; pull the amp-button trace left on the unit.
3. Install the agent as a system app; verify it survives reboot and QuickBoot.
4. USB RNDIS link to the Poco; backend integration; wheel-key policy.
5. Only if suppression is needed: the ttyMT2 splitter.

## 6. Open gaps

Decisions (owner):

1. Which keys the unit keeps and which the computer owns (decides whether the splitter is needed).
2. USB or Wi-Fi link to the POCO.
3. Whether the head unit is the car's permanent audio amplifier (then it owns volume and the
   computer mirrors), or the JBL amplifier returns on the AVC-LAN.
4. How much of our content on the unit: overlays only, or a full screen when Android Auto is idle.

Measurements (step 2 of section 5): that every listed broadcast fires; that `xy.android.gtpkey.volset`,
the `canbus.intent.action.cmd` LED frame and `xy.android.lightscreen` act; overlay behaviour over ZLink
with Android Auto running; the amplifier-button trace left on the unit.

## 7. ADB from the car computer (tooling, 2026-11-17)

Everything needed to work on the unit from the POCO over its USB cable, installed and tested with no
unit attached (the unit was off): the "absent" paths on the phone, the "present" paths on the P52
against a fake `adb`. Nothing has talked to the real unit from the phone yet.

### 7.1 Installed on the POCO

| What | Where |
|---|---|
| `adb` 1.0.41 (Alpine `android-tools` 37.0.0), `curl`, `jq` | apk (`POCO/packages.txt`) |
| udev rule: vendor 0e8d → group `plugdev` 0660, `/dev/prius-headunit`, ModemManager ignore | `/etc/udev/rules.d/51-prius-headunit.rules` (repo `POCO/rootfs/etc/udev/rules.d/`) |
| ADB key of the phone (user `user`) | `~user/.android/adbkey{,.pub}`; backup vault key `poco_adbkey_b64` (base64 of the PEM); public key also in `tools/headunit/keys/poco_adbkey.pub`; prompt fingerprint `C1:77:87:D1:4F:28:72:89:7F:DE:E4:01:70:70:0D:52` |
| tools `hu-*` | `/usr/local/lib/prius-headunit/`, symlinks in `/usr/local/bin`; source `tools/headunit/`, reinstall from the P52 with `dotinfra vault exec poco_user_password -- tools/headunit/install.sh --sudo-stdin` |
| captures | `~user/headunit/{info,logcat,pull}/<timestamp>` |

Restore the key on a reflashed phone: `dotinfra vault exec poco_adbkey_b64 -- sh -c 'base64 -d | ssh
user@10.200.0.14 "umask 077; mkdir -p .android; cat > .android/adbkey"'` then `ssh user@10.200.0.14
'adb pubkey .android/adbkey > .android/adbkey.pub'`.

### 7.2 Commands

All tools are read-only on the unit unless a `--write` flag is given; exit 3 = unit absent, 4 =
unauthorised, 5 = adb offline, 6 = write refused. `-h` prints the usage.

| Command | Does |
|---|---|
| `hu-status` | backend ACC / relay ch4 / roadsense camera power, camera hub `1-1.4`, unit on USB, adb state, build, uptime, USB config, SELinux, `su 0 id`, location providers, foreground app, port forwards |
| `hu-shell [--root] [CMD]` | `adb shell` / `su 0` (not guarded: whatever you type runs) |
| `hu-pull [--root] REMOTE [DIR]` | copy from the unit (`--root` reads one file through `su 0 cat`) |
| `hu-push --write LOCAL [REMOTE]` | copy to the unit, default `/data/local/tmp/`; `/system`, `/vendor`, `/data/misc`, `/data/system` also need `--system` |
| `hu-info` | facts dump: getprop, packages, `dumpsys location`, GNSS HAL, `mnld`/`gnss` processes, `/dev/ttyMT*` `/dev/gps*` and which process holds them, MTK GPS config (`mnl.prop`), init rc rndis/gps lines, `adb_keys` fingerprints, toolbox (`nc`?), USB/network/display, GPS-filtered and tail logcat, dmesg, screenshot; `SUMMARY.txt` + tarball |
| `hu-logcat [-t S] [--dump] [-- ARGS]` | logcat of all buffers to a file |
| `hu-fwd reverse [8080]` / `forward L R` / `list` / `clear` / `test` | adb port forwarding; `test` makes the unit GET `/login` through the tunnel and prints the HTTP status line |
| `hu-rndis` / `--enable --write` / `--disable --write` | RNDIS status (unit's `sys.usb.config`, init rc entries, car-side interfaces on `1-1.4.2`); enable = `setprop sys.usb.config rndis,adb` (not persistent) |
| `hu-adbkey` / `install --write [PUB]` | key fingerprint; whether it is in the unit's `adb_keys`; append it through `su` |
| `hu-gnss-bridge` | GNSS sender running on the car computer (§7.4); dry run unless `--post` |

Security: the backend trusts loopback without a token (`BACKEND_AUTH_TRUSTED`, [AUTH.md](AUTH.md)).
`hu-fwd reverse 8080` therefore gives every app on the unit the full API, commands included (relays).
Open it only for a session; a permanent agent on the unit gets a token or a narrower route.

### 7.3 Authorisation (the RSA prompt)

`ro.adb.secure=1`. The phone's key is new, so the unit may show "Allow USB debugging?" with the
fingerprint above on its first connection: tick "Always allow from this computer", OK. If no prompt
appears and `hu-status` stays `unauthorized`:

1. Unit Settings → Developer options → "Revoke USB debugging authorisations", replug (camera hub
   relay or ACC cycle), wait for the prompt.
2. From the P52 (its key was accepted on 2026-10-19): move the unit's cable to the P52, then
   `tools/headunit/hu-adbkey install --write tools/headunit/keys/poco_adbkey.pub` (appends the phone's
   public key to `/data/misc/adb/adb_keys` through `su`, backup `adb_keys.bak-<ts>`), cable back.
3. Last resort: copy the P52's authorised key to the phone (`~/.android/adbkey` on the P52 →
   phone `~/.android/adbkey`, then `adb kill-server`); two machines then share one key.

### 7.4 GNSS sender design

Three routes; the first needs nothing on the unit and runs today:

| Route | How | State |
|---|---|---|
| A. `hu-gnss-bridge` (default `--source dumpsys`) | one long `adb shell` loop prints the "Last Known Locations" block of `dumpsys location` and `/proc/uptime` every second; the bridge parses `Location[gps lat,lon hAcc= et= vel= bear= …]`, sends a fix when `et` changes, `age_ms` = unit uptime − `et` (both CLOCK_BOOTTIME on the unit, no clock sync); POSTs to `127.0.0.1:8080` (loopback, no token); reconnects after USB drops | built, parser unit-tested on Android 8.1's `Location.toString` layout (`tests/test_hu_gnss_bridge.py`); not run against the unit |
| B. PriusGnss app + `hu-gnss-bridge --source logcat` | a foreground service on the unit requests GPS updates and logs each fix as backend-format JSON (tag `PriusGnss`); the bridge reads `adb logcat` and POSTs; full fix rate, exact `age_ms`, keeps GPS on with no UI app | skeleton in `tools/headunit/apk/` (manifest, service, boot receiver, Gradle-free `build.sh`); not built: the P52 has a JRE and `adb` only. Needs (apt, a few hundred MB, owner's go): `openjdk-21-jdk-headless aapt dalvik-exchange apksigner google-android-platform-27-installer` |
| C. App posting directly | the app POSTs to `http://127.0.0.1:8080` over `adb reverse` or to the phone over RNDIS / Wi-Fi with the bearer token | later; puts the token on the unit |

Limit of A: `dumpsys` only shows fresh GPS fixes while an app on the unit requests GPS (a navigation
app, or MediaTek's YGPS: `hu-gnss-bridge --start-ygps --write`), and it samples at ~1 Hz. The
backend's feeder must run the `headunit` source (`BACKEND_POSITION_FEEDER=1`, default source) for
the POST to be accepted.

### 7.5 On-site session checklist

1. Key on (ACC). The backend turns relay ch4 on with the roadsense stack (or PREVIEW); the unit
   appears ~3 s later.
2. `ssh user@10.200.0.14` (or over the car AP) → `hu-status`. Expect `camera hub: 2109:2817`,
   `head unit : USB 1-1.4.2 id=0e8d:201c`, `adb state : device`.
   - `absent` with the hub absent: relay ch4 is off (stack not started yet; wait / check the
     dashboard). Do not toggle relay 4 by hand while roadsense owns it.
   - `unauthorized`: accept the prompt on the unit's screen (fingerprint `C1:77:87:D1:…:0D:52`,
     "Always allow"); no prompt → §7.3.
3. `hu-info` → read `~/headunit/info/<ts>/SUMMARY.txt`: GPS providers and last fix, `mnld`, who
   holds `/dev/gps`/`ttyMT*`, location apps, whether the unit has `nc`.
4. GPS check: start a navigation app or `hu-gnss-bridge --start-ygps --write` (YGPS on screen),
   then `hu-gnss-bridge` (dry run): fixes with `age_ms` < 1500 and sane `hacc_m` mean route A works.
   Parked under a roof the fix may never come; drive or park in the open.
5. `hu-gnss-bridge --post` and watch `curl -s http://127.0.0.1:8080/api/v1/position | jq` (or the dashboard)
   for the accepted-fix count rising; Ctrl-C to stop. `--log ~/headunit/gnss-<date>.ndjson` keeps raw samples.
6. `hu-logcat -t 120` while pressing wheel keys / changing volume (with `settings put system
   newkeypad_debug 1` via `hu-shell` if the MCU bytes are wanted) — the §5 step 2 measurements.
7. Optional: `hu-fwd reverse 8080` + `hu-fwd test` (expect an HTTP status line), then `hu-fwd clear`.
8. Optional: `hu-rndis` (status); `hu-rndis --enable --write` only when trying the USB network
   (adb drops and comes back with a new product id; a power cycle restores plain adb).
9. Copy captures to the laptop: `scp -r user@10.200.0.14:headunit/info/<ts> …`.

Open issue: the camera hub dropped and re-enumerated mid-drive twice on 2026-11-16 (17:47:17 and
21:16:50 local time); the unit disappears with it. `hu-gnss-bridge` reconnects; interactive sessions must be
restarted. Cause not known (hub power / relay ch4 / cable).
