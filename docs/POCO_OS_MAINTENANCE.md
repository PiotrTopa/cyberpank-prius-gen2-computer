# POCO F1 car computer: OS audit and upgrade plan

Lane L23, phase 1 (read-only audit), 2026-11-18 19:44-21:30 CET. The phone sat on the bench, out of
the car. No camera, gateway or powerbox was attached and the LTE antenna was off, so the uplink was down.
Nothing on the phone was installed, upgraded, removed, restarted or edited.
The package index used is the one already in `/var/cache/apk`, fetched 2026-11-17 01:43 CET. A fresh
`apk update` was declined in phase 1, so the upgradable list below may have grown since that date.

Phase 2 (section 6) runs only after the owner approves it.

---

## 0. Summary

| Topic | Finding | Weight |
|---|---|---|
| Upgrade state | Never fully upgraded since the install on 2026-10-21. A few packages were upgraded as side effects of `apk add` (mesa 26.2.5→26.2.6 on 11-04, openssh p1→p2 on 11-13): a **partial upgrade on edge**. 395 packages are behind the 11-17 index. | high |
| Kernel | `linux-postmarketos-qcom-sdm845` **7.1_rc1-r4** (built 2026-07-14), stock pmOS package from `mirror.postmarketos.org/postmarketos/main`. Variant **tianma** (`device-xiaomi-beryllium-kernel-tianma` 23-r0). It is **not** in the upgradable list: the 11-17 index has no newer kernel. Not held. | info |
| Owner patches | No local package and no pmbootstrap build. The patches live as **in-place edits of the package-owned DTB** `/boot/dtbs/qcom/sdm845-xiaomi-beryllium-tianma.dtb` (`apk audit`: `U`): `dr_mode=host`, `usb-vbus-regulator@1100` and the FT8719 touch node. Any reinstall or upgrade of the kernel package silently reverts them. `deviceinfo_flash_kernel_on_update=true` then flashes the pristine DTB to the boot partition, so the next boot comes up in device mode with no VBUS: hub, powerbox, gateway and MFD are all gone. | high |
| Holds | None. `/etc/apk/world` has no `=` pins and no local repository. | high |
| Risky upgrades pending | mesa 26.2.6 → **26.3.0** (turnip/ncnn), libcamera 0.7.2 → 0.8.0, linux-firmware-qcom/ath10k 20260916 → 20261019 (WCN3990 Wi-Fi = the AP lifeline), soc-qcom 14 → 16 and tqftpserv 1.2 → 1.3 (modem/remoteproc plumbing), postmarketos-base 78 → 79 / base-ui 62 → 65, systemd 262 → 262.2, python3 3.14.8 → 3.14.10, musl r5 → r6, openssl/libssl/libcrypto 3.5.9-r0 → r1, NetworkManager r1 → r2, nftables r0 → r1. | high |
| Security | sshd listens on all interfaces. **Password login is enabled, and the password is a 6-digit PIN.** LTE (`qmapmux*`/`wwan*`) is dropped by nftables before the port-22 accept, so ssh is reachable over Wi-Fi AP, WireGuard and USB. Root login: key only (no keys for root). sudo-rs: `%wheel ALL=(ALL) ALL` with a password. AP: WPA2-PSK, 24 characters, 3 character classes, PMF at the default. openssh 10.6p2 and openssl 3.5.9 are current in the 11-17 index (only a `-r1` openssl rebuild is pending). | medium |
| Health | One failed unit (`usb-signaller`: expected in host mode, harmless). Journal 996 MB with no configured limit. ext4 is clean, with 396 GB lifetime writes. UFS life-time estimate A/B = 0x01 (0-10 % used), EOL info 0x01 (normal). zram 8.1 GB zstd, swappiness 180. Boot takes 79 s, 48.5 s of it in `prius-backend`. Watchdog 30 s is active. | ok |
| Bench reboot loop | With the uplink down and the power profile `low`, `prius-netwatch` reboots the phone about 21 min after each boot. The 20-min key-off grace arms it, and it reboots on its **first** check because the failure counter in `/run` already stood at 8. It happened during this audit at 21:11:02 CET. This must be suppressed during phase 2. | high (for phase 2) |
| Backup | No image or rootfs backup of the phone exists anywhere: none in the CMDB, on the laptop or on nokia1. On the phone: DTB/boot backups in `/root/boot-backup` (422 MB), app rollback tarballs in `/home/user`. The laptop has **19 GB free** (232 GB disk, 92 % used). | high |
| Rescue path | fastboot over USB from nokia1 or the laptop. Needs a hand on the phone: Vol-down + power; only the power key is wired to the powerbox. No serial access is known (`console=ttyMSM0` is on the cmdline, but no pads are wired). Headless: display removed. | medium |

**Recommendation: (b) selective upgrade in two windows.**
- **Window 1:** everything except mesa, libcamera, the kernel/device/firmware stack and the qcom plumbing, which are held by pins. This brings in openssl, musl, systemd, python, NetworkManager, nftables, busybox and apk-tools.
- **Window 2:** mesa 26.3 alone, behind the GPU inference gate. A firmware/soc-qcom step follows only once the AP lifeline can be tested with a second way in (LTE antenna on).

The kernel needs no action today, because no newer kernel is in the index. The owner's patches must still be made upgrade-proof (section 7, item M1) before any future kernel bump. A full upgrade (a) bundles four independent risks (GPU results, Wi-Fi firmware, modem plumbing, pmOS base config) into one reboot with no package-level rollback, because Alpine edge does not keep old versions. Leaving it as is (c) keeps a partial-upgrade edge system that drifts further every week. That drift makes any later forced `apk add` riskier, as the 11-04 mesa bump showed.

---

## 1. OS state

| Item | Value |
|---|---|
| Distribution | postmarketOS edge, systemd flavour. `/etc/os-release` reads `NAME="Nura"`, `ID=nura`, `ID_LIKE=alpine`, `VERSION_ID=edge` (the pmOS rebrand string), support URLs on gitlab.postmarketos.org |
| Repositories | pmOS `extra-repos/systemd/main`, pmOS `main`, Alpine `edge/main`, `edge/community`, **`edge/testing`** |
| Installed packages | 1389 (`apk info`), 2.94 GiB |
| Install | 2026-10-21 09:40 CET by pmbootstrap on the laptop (`~/pmbootstrap`, rootfs chroot still in `~/.local/var/pmbootstrap/chroot_rootfs_xiaomi-beryllium`); ext4 created 2026-10-21 11:43 |
| Root storage | `userdata` (sda21, 113 GB UFS) holds a loop image: `loop0p1` ext2 480 MB `/boot` (18 % used), `loop0p2` ext4 109 GB `/` (32.1 GB used, 71.3 GB free) |
| Boot chain | `deviceinfo_generate_bootimg=true`, `append_dtb=true`, `flash_kernel_on_update=true`, flash method fastboot, A-only. `mkinitfs` builds `/boot/boot.img` (kernel + appended DTB + initramfs) and writes it to the `boot` partition (sde45) itself. Last rebuilt 2026-10-22 21:47 (poco-usb/poco-vbus) |
| Kernel | `Linux 7.1.0-rc1-sdm845 #5-postmarketos-qcom-sdm845 SMP PREEMPT Tue Jul 14 03:51:45 UTC`, package `linux-postmarketos-qcom-sdm845-7.1_rc1-r4`, pulled in by `device-xiaomi-beryllium-kernel-tianma-23-r0`. `linux-headers` is 7.2.1 (unrelated, for builds) |
| Device/firmware | `device-xiaomi-beryllium-23-r0`, `firmware-xiaomi-beryllium-13-r0`, `firmware-qcom-adreno-a630-20260110-r1`, `linux-firmware-{ath10k,qcom,qca,ath11k}-20260916-r0` |
| Owner patches | DTB edits in place (see Summary). `apk audit --system` lists as modified: both beryllium DTBs (U), the kernel `modules.*` index files (U, normal after depmod) and `gst-ptp-helper` (x, capabilities). The scripts `poco-usb`/`poco-vbus` (github.com/PiotrTopa/poco-f1-usb-host) are in `/usr/local/sbin`, with backups of every DTB edit in `/root/boot-backup/{usb,vbus,touch,i2c}-*` |
| Live DTB check | `/boot/dtbs/qcom/…-tianma.dtb` and `/boot/…-tianma.dtb`: `dr_mode = host`, node `usb-vbus-regulator@1100` present |
| `/etc/apk/world` | 118 entries, no version pins, no tags (`@…`), no local repository. Includes the whole phosh UI meta (`postmarketos-ui-phosh`, firefox-esr, gnome apps, flatpak) although the phone is headless with greetd disabled |
| Upgrade history (`/var/log/apk.log`) | Initial install 1116 packages (10-21). Later only `apk add` transactions: 10-21 (dev tools, onnxruntime, opencv, mesa-vulkan-freedreno, libcamera-dev), 11-04 (backend deps; this **upgraded mesa 26.2.5 → 26.2.6**, 7 packages), 11-09 bluez-btmon, 11-13 py3-pillow and openssh-client-default (**upgraded openssh 10.6p1 → 10.6p2**, 3 packages, `openssh-nftrules` left at p1), 11-17 android-tools/curl/jq. **No `apk upgrade` was ever run.** |

### Upgradable packages (index of 2026-11-17)

395 packages are behind. About 66 are GNOME/phosh/GTK/Qt/flatpak/pipewire UI packages that the
headless car computer never runs, 20 are gstreamer, 16 are py3-*, 64 are other lib*. The ones that matter:

| Package (installed → available) | Why it matters | Risk |
|---|---|---|
| mesa, mesa-vulkan-freedreno, -egl, -gbm, -gl, -gles, -dri-gallium, -rusticl 26.2.6 → **26.3.0** | turnip is roadsense's Vulkan driver via ncnn (modeld, signd detector). It needs `TU_DEBUG=flushall` (set by modeld/signd themselves). A minor-version change can change results (fp16 paths) or speed | **high** |
| libcamera, -ipa, -tools, -dev, py3-libcamera 0.7.2 → 0.8.0 | Not in the production camera path (`camerad-dvr` reads the USB DVR through libjpeg; `roadsense-camerad` (libcamera) is masked). Only used for bench tools | low (production), medium (if the rear camera returns) |
| linux-firmware-ath10k/-qcom/-qca/-ath11k 20260916 → 20261019 | WCN3990 Wi-Fi firmware is the AP lifeline, already fragile (`ath10k-retry.service`, crash on first probe). Bluetooth (qca) carries the BLE satellites. linux-firmware-qcom also has the venus/adsp/cdsp blobs | **high** |
| soc-qcom, soc-qcom-modem, soc-qcom-vulkan, … 14 → 16; tqftpserv 1.2 → 1.3 | Modem bring-up, remoteproc firmware serving (tqftpserv serves the modem/Wi-Fi firmware files) and the default Vulkan settings | **high** (LTE/WireGuard and Wi-Fi) |
| postmarketos-base* 78 → 79, -systemd 92 → 93, base-ui* 62 → 65, baselayout 78 → 79 | Ship the default nftables files, sshd/sudo/zram/resolved configuration, udev rules | medium (config drift, `.apk-new` files) |
| systemd 262-r7 → 262.2-r0 | Point release. A `daemon-reload` re-arms the transient key-off timers (CMDB) | medium |
| python3 3.14.8 → 3.14.10, py3-pillow 12.3 → 13.0, py3-gobject3, py3-websockets, py3-multidict, py3-certifi | Backend venv `/home/user/cyberpunk/.venv` is `--system-site-packages` on the system python (symlink `python3 → /usr/sbin/python3`); prius-bled uses py3-gobject3/py3-dbus; roadsense api/mapd use system python3 | medium (pillow major) |
| musl 1.2.6-r5 → r6 | Everything links it; rebuild/patch | low-medium |
| openssl, libssl3, libcrypto3 3.5.9-r0 → r1 | Security-relevant rebuild (content of r1 not inspected) | low, **wanted** |
| networkmanager* 99991.58.0-r1 → r2 (pmOS fork) | AP, LTE bearer, WireGuard profiles | medium |
| nftables 1.1.7-r0 → r1 | Firewall | low |
| busybox r7 → r8, apk-tools 3.0.8 → 3.0.9, device-mapper 2.03.35 → 2.03.44, kmod, iproute2, zlib, libxml2, nss, ffmpeg | Base tools | low |
| opencv 4.13.0-r0 → r1 | Rebuild; used by bench tools | low |
| **Not pending:** linux-postmarketos-qcom-sdm845, device-xiaomi-beryllium*, firmware-xiaomi-beryllium, firmware-qcom-adreno-a630, onnxruntime 1.30.0, openssh 10.6p2 (server/client), wireguard-tools 1.0.20260223, sudo-rs 0.2.15, ModemManager 1.25.95_git20261005, libqmi, bluez 5.87, gcc/libstdc++/libgomp | | |

### Locally built / local software (not managed by apk)

| Item | Where | Upgrade coupling |
|---|---|---|
| ncnn | `~user/vulkan-work/ncnn`, git `7e36d25` = tag 20261020 (+ `ncnn-blob-barrier.patch` alongside) | **Statically linked** into modeld/signd/lights_replay; reaches the GPU through `dlopen` of the Vulkan loader. So mesa changes reach it, ncnn itself does not change |
| ONNX Runtime | **apk package** `onnxruntime 1.30.0-r0` (Alpine community), dynamically linked by modeld/signd/envd. Not a local build (the brief assumed one) | Not in the pending list; a future bump changes ORT results and speed (signd classifier, envd) |
| roadsense binaries | `/usr/local/bin/{camerad-dvr,modeld,signd,envd,…}` (+ `.pre-l*` copies), built on the phone (`~user/roadsense/build-ncnn`, `~user/roadsense-signs/build-l15`) | Link libstdc++, libgomp, libonnxruntime, libjpeg (libjpeg-turbo): ABI-stable across the pending upgrade |
| roadsense api/mapd | `/usr/local/lib/roadsense/{api,mapd}` on system `python3` (+ `api.old`, `api.pre-l6..l11`) | python 3.14.x point release: no rebuild needed |
| Backend venv | `/home/user/cyberpunk/.venv`, pip: fastapi 0.162, pydantic 2.14 (pydantic_core binary), numpy 2.5.4, cryptography 50, aiohttp 3.14, … | Same minor python (3.14) means compiled wheels keep working |
| poco-usb / poco-vbus | `/usr/local/sbin` | See M1 |

---

## 2. Security exposure

### Listening sockets (`ss -tulpn`) and who can reach them

| Port | Process | Bound | Reachable from (nftables) |
|---|---|---|---|
| tcp 22 | sshd (`sshd.pam`) | 0.0.0.0, [::] | Wi-Fi (AP or client), WireGuard, USB. **Not** LTE (`qmapmux*`/`wwan*` dropped earlier in the chain) |
| tcp 8080 | prius-backend (API + dashboard) | 0.0.0.0 | lo, wg*, usb*; on wlan* only from 10.42.0.0/24 to 10.42.0.1 (`prius_guard`); token/session auth (docs/AUTH.md) |
| tcp 8081, 8082 | prius-backend ZMQ (no auth) | 0.0.0.0 | lo, wg*, usb* only |
| tcp 8090 | roadsense api (when running) | 0.0.0.0 | as 8080 |
| tcp 8788 / udp 8787 | backend Wi-Fi satellites | wlan0 / 0.0.0.0 | wlan* only |
| udp 53, 67 / tcp 53 | NM dnsmasq (AP) | 10.42.0.1 / 0.0.0.0 | wlan* (DHCP, DNS) |
| udp 123 | prius-backend (python) | 0.0.0.0 | no accept rule, so dropped from outside |
| tcp 5037, udp 5353 | adb server (head-unit GNSS bridge) | 127.0.0.1 / mDNS | 5037 loopback only; 5353 no accept rule |
| udp 5353 | avahi | all | no accept rule |
| tcp/udp 5355 | systemd-resolved LLMNR | all | no accept rule |
| udp 42038 | WireGuard `wg-homelab` | all | no accept rule; the tunnel works by outbound + conntrack (keepalive 25 s) |
| tcp 24727, tcp/udp 53317 | (nothing listens) | | **accepted on every non-LTE interface** by pmOS defaults `50_ausweisapp2.nft`, `50_localsend.nft` |

Interfaces: `wlan0` (AP 10.42.0.1/24), `wg-homelab` (10.200.0.14/32), `qmapmux0.0` (LTE, CGNAT 10.x),
`rmnet_ipa0` (link-local only, no drop rule: no services reachable in practice), no `usb0` (host mode).
IPv6: link-local only. **From the LTE side nothing is reachable.**

### nftables (loaded ruleset)

`inet filter` input policy **drop**. It accepts: lo, established, ICMP/ICMPv6 basics, DHCP on usb/wlan/p2p,
24727, 53317, 8080-8082, 8090, 22, DNS on wlan, everything from usb*. It drops wwan*/qmapmux*.
Forward policy drop, except from wlan* and usb* (AP tether). `inet prius_guard` (priority -10) confines
8080-8082/8090 as above and also drops 192.168.100.0/24 (MFD link) on those ports.
`ip nm-shared-wlan0` handles AP masquerade. Files: `/etc/nftables.d/{01_wwan,10_dhcp,50_ausweisapp2,50_localsend,50_prius_backend,50_roadsense,50_sshd→/usr/share,51_hotspot,51_usb_inet}.nft`.
The ruleset matches the CMDB description.

### ssh, users, sudo

| Item | State |
|---|---|
| sshd (effective `sshd -T`) | `PasswordAuthentication yes`, `KbdInteractiveAuthentication yes`, `PubkeyAuthentication yes`, `PermitRootLogin prohibit-password`, `AllowTcpForwarding no`, `X11Forwarding no`, `MaxAuthTries 6`, `UsePAM yes`. Drop-in `50-postmarketos-ui-policy.conf` |
| Keys | `user`: 2 keys (ed25519 + rsa, both `piotr@ai`). root: no `authorized_keys`. root has its own client key `/root/.ssh/id_ed25519` for the MFD Pi |
| Accounts with a login shell | `root` (locked: no password hash), `user` (uid 10000, password hash = the 6-digit PIN; groups wheel, audio, video, netdev, plugdev, feedbackd) |
| sudo | sudo-rs 0.2.15, `/etc/sudoers`: `root ALL`, `%wheel ALL=(ALL) ALL` (password required), `Defaults pwfeedback`; `/etc/sudoers.d/` empty |
| Exposure | A 6-digit PIN with password ssh enabled can be brute-forced. MaxAuthTries 6 and no rate limiting allow on the order of 10^6 tries from anyone **on the AP or the WireGuard net**. The AP is WPA2-PSK with a strong passphrase (24 characters, 3 classes), so the practical exposure is the WireGuard peers and AP clients |
| Wi-Fi profiles | `prius-ap` (AP, WPA-PSK, 24 chars, 3 classes); client profiles `home`/`prius-home-wifi` (14 chars, 2 classes) and `prius-dev-wifi` (22 chars, 3 classes), all autoconnect off |
| Secrets present on the phone (values not read) | WireGuard private key (NM profile), AP PSK, SIM PIN (sim-unlock service), backend auth token `/etc/prius/backend-auth.env`, car Ed25519 key `/etc/prius/satellites/`, adb key, root ssh key. All have vault copies per CMDB except the root ssh key and the car Ed25519 key |
| CVE posture | openssh 10.6p2 and openssl 3.5.9 are the newest builds in the 11-17 index; nothing older than the repository is pending except openssl `-r1` and `openssh-nftrules`. No serious unpatched openssh/openssl CVE is known for these versions as of the audit; re-check after `apk update` |

---

## 3. Health

| Item | Finding |
|---|---|
| Failed units | `usb-signaller.service` (pmOS default, exits 1: "No USB Device Controller", correct in host mode). Harmless; can be disabled |
| Journal | 995.6 MB on disk, no `journald.conf` (built-in defaults: up to 10 % of the fs, capped at 4 GB). 15 boots retained back to 2026-11-07. Boot timestamps are non-monotonic (clock fallback); `journald: Time jumped backwards, rotating` twice per boot |
| Log noise (this boot, ~3.4 h) | 432× each modeld/signd 10 s stats lines (roadsense on, L21), 377×2 prius-bled discovery on/off, 370×2 NetworkManager qrtr0 MTU failure (`Invalid argument`), 290× hexagonrpcd sensors `sns_reg_config` write to `/mnt/vendor/persist` (sensors stack unused headless), ~550 resolved "degraded feature set" (uplink down), ~150 ssh session open/close sets (lanes). Last 24 h by identifier: kernel 24.4 k, systemd 8.9 k, NetworkManager 5.7 k, prius-backend 3.2 k, sshd 3.1 k |
| Disk | `/` 32.1 GB of 109 GB used. See the cleanup table below |
| Filesystem | ext4 root `clean`, mount count 342, never fsck'd since creation, errors behaviour `continue`, lifetime writes **396 GB in 4 weeks** (~14 GB/day, mostly ride/replay copies, builds, recorder, journal). `/boot` ext2: `mounting unchecked fs, running e2fsck is recommended` at every boot. No I/O or ext4 errors in dmesg |
| UFS wear | `health_descriptor`: `life_time_estimation_a` 0x01, `_b` 0x01 (0-10 % of rated life used), `eol_info` 0x01 (normal) |
| Memory | 5.5 GB RAM, zram swap 8.1 GB zstd prio 300 (pmOS `postmarketos-zram-swap`), swappiness 180, swap unused at idle |
| Time | No persistent RTC (`rtc-pm8xxx` counts from 1970 at every boot). systemd-timesyncd active, saved clock `/var/lib/systemd/timesync/clock` updated live. `prius-clock-fallback.timer` (2 min after boot, then every 5 min) runs `htpdate` against two HTTPS hosts while `NTPSynchronized=no`. chrony is installed (`chrony-4.9`) but inactive; htpdate is in the world |
| Watchdog | `RuntimeWatchdogSec=30`, `RebootWatchdogSec=30` (`/etc/systemd/system.conf.d/watchdog.conf` and the overlay `10-watchdog.conf`), device `qcom_wdt` |
| Boot time | 10.2 s kernel + 68.7 s userspace = **79 s**, multi-user at 65 s. Blame: prius-backend 48.5 s (waits for the first ACC report ≤20 s plus device probing), ath10k-retry 11.2 s, prius-wifi 3.4 s. Critical chain: multi-user ← prius-gnss-bridge ← prius-backend ← network-online. The `local-fs.target` chain contains `run-user-10000-gvfs.mount`, so `user@10000` (gvfs, gnome-keyring: `gkr-pam: couldn't unlock the login keyring` on every ssh login) starts at boot for the UI user; unnecessary headless |
| Unit ordering | gnss-bridge is ordered after backend (fine). The backend's long start delays `multi-user.target` but nothing waits on it. No ordering cycles were logged |
| Uplink on the bench | LTE bearer has an address, but no data passes (antenna off). WireGuard has had **0 B received, no handshake** this boot. Lifeline ON since 18:45 CET |
| **netwatch reboot loop** | Power profile `low` (bench, no ACC). After the 20-min grace `prius-netwatch.timer` is armed and its first run at 21:10:56 already reported "consecutive failures: 8". It rebooted at 21:11:02 (journal of boot -1). The counter in `/run/prius-netwatch.fail` survives across arming within one boot (it had counted before L21 switched the profile to `full`). It is not reset when the timer is re-armed. Today's boots at 16:46, 17:17 and 17:46 CET fit the same pattern. Bug candidate: reset the counter when arming |

### Disk usage and cleanup candidates (list only; nothing was deleted)

| Path | Size | What | Safe to remove? |
|---|---|---|---|
| `/home/user/rides` | 19.0 GB | 21 replay rides; copies of the laptop's `~/datasets/rides` (28 GB, 22 rides) | Yes if replay on the phone is not needed; can be re-synced from the laptop. Largest single item |
| `/home/user/l21` | 1.5 GB | L21 lane files (sources, builds, profiles, `out/` results, `lf/` frames, `chk/` check outputs). Holds the GPU check inputs (section 6) | **Keep until phase 2 is verified**; then archive `out/` + `chk/` to the laptop and remove |
| `/var/lib/roadsense/rec` | 1.2 GB | 19 recorder sessions. The five L21 bench sessions `20261118-185123`, `-190340`, `-194014`, `-195616`, `-202334` total ~0.5 MB; three more 11-18 bench sessions (`-143107`, `-145015`, `-150942`) ~1.8 MB; older ones from 11-12 … 11-18 09:52 are up to 443 MB each | L21/bench sessions: yes. Older sessions: only after checking they are real drives already copied off (the recorder caps itself at 4 GB) |
| `/var/log/journal` | 996 MB | systemd journal | Cap instead (M3) |
| `/home/user/rs-replay` | 780 MB | replay inputs incl. `ljkr.nv12` (373 MB): **input of the GPU check**, keep | Keep `ljkr.nv12`; the `.xz` copy and old jsonl outputs are redundant |
| `/home/user/quant-work` | 779 MB | quantisation lane work | Yes after archiving to the laptop (check the roadsense QUANTIZATION notes first) |
| `/home/user/vulkan-work` | 542 MB | ncnn source + build used by the installed binaries (`NCNN_DIR`) | **Keep** (rebuilds link against it) |
| `/root/boot-backup` | 422 MB | 14 DTB/boot backups from 10-21/10-22 | Keep the newest `usb-*`/`vbus-*`/`touch-*`; also copy to the laptop in phase 2 |
| `/var/cache/apk` | 277 MB | indexes + 182 cached `.apk` files of earlier `apk add` runs, **including mesa 26.2.6 (all 8 subpackages)** and `device-xiaomi-beryllium-kernel-{tianma,ebbg}-23-r0` | **Keep**: the only offline downgrade material for mesa |
| Old lane dirs `l1` 24 MB, `l3` 37 MB, `l4` 76 MB, `l7` 176 MB, `l8` 47 MB, `l10`, `l11`, `l12` 138 MB, `pocopilot` 125 MB, `usbdbg` 13 MB, `thermal` 9 MB, `bench` | ~650 MB | finished lanes | Yes after a check that the reports are in the repos |
| Rollback tarballs in `/home/user` | ~330 MB | `roadsense-installed-20261117-pre-L16.tgz` and `-pre-L17.tgz` (73 MB each), `-20261116-pre-stability.tgz` (23 MB), `roadsense-rollback-20261115-pre-mapd.tar.gz` (19 MB), `-pre-L18.tgz` (0.9 MB), 13× `cyberpunk_computer-*-pre-*.tgz` (~3 MB each, 11-15 … 11-18), `cyberpunk-rollback-20261115.tgz`, `roadsense-src/lib/etc-*`, `backend-env-*`, dir `rollback-20261115-auth` | Everything older than the current deploy's direct predecessor: yes. Keep the newest of each kind (`cyberpunk_computer-20261118-pre-previewsync.tgz`, `roadsense-installed-20261117-pre-L18.tgz`) |
| `/usr/local/bin/*.pre-l*`, `/usr/local/lib/roadsense/api.{old,pre-l6..l11}` | small | old binaries/api copies | Yes, except the newest predecessor |
| `/var/tmp/roadsense-{,signs-}build` | 48 MB | older build trees (current: `~user/roadsense/build-ncnn`) | Yes |
| `/home/user/roadsense-maps` | 250 MB | the installed map pack (target of `current.rsmap`) | **Keep** |

Total realistically freeable: ~23 GB, 19 GB of it the ride copies. The disk is at 31 %, so cleanup is housekeeping, not urgent. It does shrink the backup (section 4).

---

## 4. Backup state and restore paths

**Existing backups:** none of the OS. The CMDB names no image or rootfs backup. The laptop holds the
pmbootstrap checkout/chroot, which could rebuild a **fresh** install, not this one. nokia1 holds the
original install images in `~/pmos/` (not checked: nokia1 did not resolve by name from the laptop during the audit).
On the phone: `/root/boot-backup` (DTBs), the app rollback tarballs, `/root/lifeline-rollback-20261115`, `/root/nft-ruleset-pre-auth-20261115.nft`.

**Size of a useful backup:** the system without bulk data (`/usr` 3.1 GB, `/var` without rec/journal/cache
~1.1 GB, `/etc`, `/root` 0.45 GB, `/home/user` without rides/l21/quant-work/rs-replay/roadsense-maps ~3.7 GB) is
about **8.5 GB raw, an estimated 4-5 GB as zstd**. `/boot` is 78 MB, the `boot` partition 64 MB.

**Link speed:** measured over the AP with the laptop's USB Wi-Fi card: 60 MB in 39 s = **1.5 MB/s**. So a 4.5 GB archive takes
**about 50 min**. The ath10k AP drops now and then, so the transfer must be resumable (rsync `--partial`).
A whole-image `dd` of the 113 GB userdata (or even the 32 GB used) is not feasible over this link and does not fit on the laptop (19 GB free).

**Failure modes and restore:**

| Failure after an upgrade | Way back in | Restore |
|---|---|---|
| Userspace regression, ssh still works (e.g. mesa result change, python package break) | ssh over AP | Pinned downgrade from the cached `.apk` files (mesa), or restore the affected paths from the local backup archive. Minutes |
| AP or Wi-Fi dead, system boots | WireGuard over LTE (needs the LTE antenna; on the bench without it, **none**) | ssh over WG, then downgrade the firmware from the local backup archive (`/lib/firmware`, `/usr/lib/firmware`). Without LTE: next row |
| No network at all / no boot | **fastboot** (hand on the phone: Vol-down + power while cabled to nokia1 or the laptop) | (1) `fastboot flash boot boot-pre-upgrade.img` (from the backup) and reboot: fixes kernel/initramfs/DTB breakage in ~5 min. (2) For rootfs breakage: `pmbootstrap flasher boot` with the pmOS initramfs debug shell (`pmbootstrap flasher boot --cmdline pmos.debug-shell`, which boots the laptop chroot's kernel with a **peripheral-mode** DTB, so USB networking works) gives a shell at `172.16.42.1` (telnet). From there, mount the rootfs and untar the local backup archive over it. ~30-60 min. Needs the laptop's pmbootstrap chroot to match the device (it is the install chroot) |
| Everything lost | fastboot | Fresh `pmbootstrap install` + flash userdata from nokia1, then `POCO/provision.sh`/`sync.sh`, roadsense install + ncnn build, map pack, SIM/WG/AP secrets from the vault, poco-usb/poco-vbus: **most of a day** |

Serial console: none in practice (no wired pads). Display: removed. The power key is on the powerbox
(GP29: 6 s from off boots, 12 s forces off); **the volume keys are not wired**, so fastboot needs a person at the phone.

---

## 5. Recommendation

**(b) selective upgrade in two windows**, with the kernel/device/firmware/soc-qcom stack and mesa held in the first window.
Reasons:
1. The security-relevant and base pieces (openssl -r1, musl, systemd, python point release, NetworkManager, nftables,
   busybox, apk-tools) are low-risk point updates and end the partial-upgrade state for most of the system.
2. mesa 26.3 can change GPU inference results. It deserves its own window and the bit-identical gate, with
   the cached 26.2.6 packages as a tested rollback.
3. Wi-Fi/BT firmware and the qcom modem plumbing can take away **both** remote paths at once. They should go only
   when a second path exists (LTE antenna connected, or in the car) and a person can reach the phone for fastboot.
4. The kernel has nothing pending. Before a kernel bump ever arrives, M1 (section 7) must make the DTB
   patches survive it; until then, an unattended `apk upgrade` is one pmaports bump away from a phone that
   boots without USB host mode.

(a) full upgrade: only acceptable in the car-side setting with a person present, after the backup, and with
the expectation of a fastboot recovery. (c) leave as is: acceptable for a few weeks, but each `apk add`
for a new tool will drag pieces forward uncontrolled (as on 11-04 and 11-13).

---

## 6. Phase 2 plan (to execute step by step after approval)

Preconditions (owner decisions, section 8):
- L21 is done (it is), and no lane is on the phone.
- Ideally the car hardware (hub, powerbox, gateway, camera) is attached and the LTE antenna is connected. Without them, steps V-USB and V-WG are deferred to the car.
- Person at the phone for windows 2/3.

Helper on the laptop (the password never appears on a command line; it goes to sudo on stdin):

```sh
P=user@10.42.0.1                      # fallback user@10.200.0.14 when WG is up
psu() { dotinfra vault exec poco_user_password -- ssh "$P" "sudo -S -p '' $*"; }
B=/home/piotr/backups/poco/$(date +%Y%m%d)   # laptop target (19 GB free: archive ~4-5 GB)
mkdir -p "$B"
```

### Step 0: stop the bench reboot loop for the session (rollback: reboot, or unmask)
```sh
psu systemctl mask --runtime prius-netwatch.service      # /run only, gone at next boot
psu systemctl stop prius-netwatch.timer prius-netwatch-arm.timer
ssh $P 'curl -s -X POST http://127.0.0.1:8080/api/v1/commands/roadsense_mode -H "Content-Type: application/json" -d "{\"mode\":\"off\"}"'
```
Verify: `psu systemctl is-enabled prius-netwatch.service` prints `masked-runtime`.
Note: the masked netwatch must be re-checked after every `daemon-reload` that apk triggers (systemd upgrade).

### Step 1: baseline capture (read-only, ~10 min + the L21 run)
```sh
ssh $P 'mkdir -p ~/l23 && cd ~/l23 && apk info -v | sort > pkgs-before.txt && cp /etc/apk/world world-before && uname -a > uname-before && lsusb -t > lsusb-before 2>&1; ip -br addr > ip-before; vulkaninfo --summary > vk-before 2>&1; systemctl --failed --no-legend > failed-before'
psu sh -c "'apk audit --system > /home/user/l23/audit-before; nft list ruleset > /home/user/l23/nft-before; ss -tulpn > /home/user/l23/ss-before; fdtget /boot/dtbs/qcom/sdm845-xiaomi-beryllium-tianma.dtb /soc@0/usb@a6f8800/usb@a600000 dr_mode > /home/user/l23/drmode-before'"
```
GPU inference baseline (the commands of L21's `checks.sh`/`envchk.sh`, installed binaries, stack off):
```sh
ssh $P 'cat > ~/l23/gpucheck.sh' <<'EOF'
# gpucheck.sh TAG : deterministic GPU/CPU inference outputs of the installed binaries (L21 inputs)
T=$1; L=/home/user/l21; M=/var/lib/roadsense/models; NV=/home/user/rs-replay/ljkr.nv12; O=/home/user/l23/gpu-$T
mkdir -p $O; cd $O
/usr/local/bin/modeld_replay $NV --width 1920 --height 1080 --backend ncnn --max-frames 40 --pitch -0.085 --yaw 0.025 --roll 0 --height-m 1.22 > md.jsonl 2> md.log
/usr/local/bin/signd_replay --models-dir $M --det-backend ncnn --cls-backend ort --threads 2 --raw --size 1920x1080 --every 2 --max-frames 60 $NV > sg.jsonl 2> sg.log
ROADSENSE_CONFIG_DIR=/etc/roadsense taskset -c 0-2 xargs -a $L/chk/envd.files /usr/local/bin/envd_replay --model $M/envd-image.onnx --classes $M/envd-classes-probe.json --threads 3 --no-timing > envd.jsonl 2> envd.log
xargs -a $L/chk/envd.files /usr/local/bin/lights_replay --backend ncnn-cpu --models-dir $M --threads 2 --cpus 0-2 --no-track > lights.jsonl 2> lights.log   # production lights path = ncnn on the CPU; cpus 0-2 because the low profile offlines 4-7
jq -c 'del(.ts_mono_ns,.ts_real_ns,.model_ms,.prep_ms)' md.jsonl | md5sum > md.sum
jq -c 'del(.detect_ms,.timing)' sg.jsonl | md5sum > sg.sum
jq -c 'del(.env_ms,.encode_ms)' envd.jsonl | md5sum > envd.sum
tail -1 md.log; tail -1 sg.log
EOF
psu sh /home/user/l23/gpucheck.sh before
```
Verified in phase 1 on the L21 outputs: modeld (timing fields removed) is byte-identical between two
builds, signd likewise, envd identical between big and little cores. So "identical sums" is a valid gate.
Speed and budget baseline with L21's tools (`/home/piotr/projects/wt/roadsense-v2/tools/budget/`, report
`docs/lanes/L21-budget-and-models.md` §1). This is the installed stack on ride `krakow-2024`, 240 s after 60 s, fan 100 %,
host roadsense mode `on` as L21 ran it, then back to `off`:
```sh
psu sh /home/user/l21/run.sh U0-before /home/user/l21/profiles/A0-installed.sh 240 60
# results: /home/user/l21/out/U0-before.{budget,ws,health,*.log}; table: python3 tools/budget/table.py (on the laptop, after rsync of out/)
```
Reference numbers from L21 §6 (installed stack): modeld 13.7 runs/s, model 54.0 / 57.3 ms mean/p90; signd detect 98.5 ms;
GPU busy 91 %; rail 6.67 W.

### Step 2: backup (≈15 min on the phone + ≈50 min transfer)
GNU tar keeps xattrs/capabilities (busybox tar does not). Install it first: it is tiny and the only package added before the backup.
```sh
psu apk add tar          # GNU tar; uses the current (11-17) index, no update yet
psu dd if=/dev/disk/by-partlabel/boot of=/home/user/l23/boot-partition-pre.img bs=1M
psu systemd-run --wait --unit l23-backup -p Nice=10 -p IOSchedulingClass=idle sh -c "'tar --numeric-owner --xattrs --xattrs-include=* --acls -cpf - --one-file-system -C / --exclude=./home/user/rides --exclude=./home/user/l21 --exclude=./home/user/quant-work --exclude=./home/user/rs-replay --exclude=./home/user/roadsense-maps --exclude=./home/user/l23 --exclude=./var/lib/roadsense/rec --exclude=./var/log/journal . | zstd -T0 -6 > /home/user/l23/rootfs-pre.tar.zst && tar -cpf - -C /boot . | zstd -6 > /home/user/l23/boot-fs-pre.tar.zst && cd /home/user/l23 && sha256sum *.zst *.img > SHA256SUMS'"
psu chown user:user /home/user/l23/rootfs-pre.tar.zst /home/user/l23/boot-fs-pre.tar.zst /home/user/l23/boot-partition-pre.img /home/user/l23/SHA256SUMS
rsync -av --partial --timeout=60 $P:l23/ "$B/"        # re-run until complete; resumable over AP drops
(cd "$B" && sha256sum -c SHA256SUMS)
ssh $P 'mkdir -p ~/l23/apk-keep' && psu sh -c "'cp /var/cache/apk/mesa*-26.2.6-r0.*.apk /var/cache/apk/device-xiaomi-beryllium-kernel-tianma-23-r0.*.apk /home/user/l23/apk-keep/'"
psu sh -c "'tar -C /root -cf /home/user/l23/root-boot-backup.tar boot-backup && chown -R user:user /home/user/l23'"
rsync -av --partial $P:l23/ "$B/"
```
Verify: the checksums match on the laptop; `zstd -t "$B/rootfs-pre.tar.zst"`. The local copy stays on the phone (71 GB free) as the fast restore source.
Rollback of this step: none needed (`apk del tar` if unwanted).

### Step 3: rescue image ready (laptop, before touching packages)
```sh
cd ~/pmbootstrap && ./pmbootstrap.py status     # device xiaomi-beryllium, kernel tianma, channel edge
# Test once with the phone in fastboot (hand on the phone: Vol-down + power, USB cable to the laptop):
./pmbootstrap.py flasher boot --cmdline pmos.debug-shell   # RAM boot only, flashes nothing; telnet 172.16.42.1
```
Optional but recommended (owner decision): keep the debug-shell boot path documented in the CMDB; the step needs a person.

### Window 1: base/security upgrade with holds (≈30-45 min)
Pins (world constraints) for everything that must not move now:
```sh
psu apk update
psu apk version -l "'<'" | tee /dev/stderr | wc -l          # new upgradable list; re-check the risky set against section 1
psu apk add \
  "'mesa=26.2.6-r0'" "'mesa-vulkan-freedreno=26.2.6-r0'" "'mesa-egl=26.2.6-r0'" "'mesa-gbm=26.2.6-r0'" "'mesa-gl=26.2.6-r0'" \
  "'mesa-gles=26.2.6-r0'" "'mesa-dri-gallium=26.2.6-r0'" "'mesa-rusticl=26.2.6-r0'" \
  "'linux-postmarketos-qcom-sdm845=7.1_rc1-r4'" "'device-xiaomi-beryllium=23-r0'" "'device-xiaomi-beryllium-kernel-tianma=23-r0'" \
  "'firmware-xiaomi-beryllium=13-r0'" "'firmware-qcom-adreno-a630=20260110-r1'" \
  "'linux-firmware-ath10k=20260916-r0'" "'linux-firmware-qcom=20260916-r0'" "'linux-firmware-qca=20260916-r0'" \
  "'soc-qcom=14-r0'" "'soc-qcom-modem=14-r0'" "'tqftpserv=1.2-r0'" \
  "'libcamera=99990.7.2-r4'" "'libcamera-ipa=99990.7.2-r4'" "'libcamera-tools=99990.7.2-r4'" "'libcamera-dev=99990.7.2-r4'" "'py3-libcamera=99990.7.2-r4'"
psu apk upgrade --simulate 2>&1 | tail -40     # read it: no kernel/mesa/firmware/soc-qcom lines; note any conflicts
```
If the simulation fails because a package needs a newer pinned one (likely candidates: soc-qcom-* subpackages,
postmarketos-base-* depending on soc-qcom 16, gst/gtk on newer mesa), add the named subpackage to the pins or drop that package from the run
(`apk upgrade --available` is not used). Do not force with `--force-broken-world`. Then:
```sh
psu systemd-run --wait --unit l23-upgrade sh -c "'apk upgrade 2>&1 | tee /home/user/l23/upgrade-w1.log'"
psu sh -c "'find /etc -name \"*.apk-new\"'"         # review each against the live file; keep ours unless the diff is a fix
psu systemctl is-enabled prius-netwatch.service     # still masked-runtime? (daemon-reload)
psu fdtget /boot/dtbs/qcom/sdm845-xiaomi-beryllium-tianma.dtb /soc@0/usb@a6f8800/usb@a600000 dr_mode   # must print host
psu fdtget -l /boot/dtbs/qcom/sdm845-xiaomi-beryllium-tianma.dtb /soc@0/spmi@c440000/pmic@2           # must list usb-vbus-regulator@1100
```
If `mkinitfs` ran (postmarketos-mkinitfs/base triggers), it rebuilt and **flashed** boot.img from the
patched DTB. That is fine as long as the two `fdtget` checks pass. **If either fails: do not reboot.** Run `sudo poco-vbus apply && sudo poco-usb host` (both re-run mkinitfs), check again, then reboot.

Reboot: `psu systemctl reboot` (works while the display is not blanked; `prius-blank` is opt-in). Wait up to 3 min for ssh over the AP.

Verification after the reboot (V-list; each must pass before window 2):

| Check | Command | Pass |
|---|---|---|
| V-BOOT | `ssh $P 'uname -r; systemctl --failed --no-legend; systemd-analyze'` | kernel `7.1.0-rc1-sdm845`; failed only `usb-signaller`; boot ≲ 90 s |
| V-DTB | `psu cat /sys/firmware/devicetree/base/soc@0/usb@a6f8800/usb@a600000/dr_mode` and `ls /sys/class/regulator/*/name \| xargs cat \| grep -i vbus` | `host`; vbus regulator present |
| V-USB (car/bench HW attached) | `lsusb -t`; `psu sh -c "'cat /sys/kernel/debug/regmap/0-02/registers \| grep ^1109'"`; backend state `connection.*` | hub 1-1, powerbox, gateway, panda, MFD link enumerate; `1109: 0a` |
| V-AP | from the laptop: `ping -c3 10.42.0.1`; `ssh $P prius-lifeline status` | AP up, lifeline loop running |
| V-WG (LTE antenna on) | `ssh $P 'sudo -n true; wg show wg-homelab latest-handshakes'`, `ping 10.200.0.1` from the phone; `ssh user@10.200.0.14` from the laptop | handshake < 2 min, hub reachable |
| V-SIM/modem | `journalctl -b -u prius-sim-unlock`; `mmcli -m any` | PIN verified, modem registered |
| V-NFT | `psu nft list ruleset > /tmp/n && diff /home/user/l23/nft-before /tmp/n` (as root) | no change except counters |
| V-SSH | `psu /usr/sbin/sshd.pam -T \| grep -E "passwordauth\|permitroot"` | unchanged (or as decided in M5) |
| V-BACKEND | `ssh $P 'curl -s http://127.0.0.1:8080/health; systemctl is-active prius-backend prius-bled prius-gnss-bridge prius-lifeline prius-power'` | all active, health ok |
| V-CLOCK | `timedatectl`; `systemctl list-timers prius-clock-fallback.timer` | synchronized (with uplink) / fallback timer scheduled |
| V-GPU | `psu sh /home/user/l23/gpucheck.sh w1` then `ssh $P 'cd ~/l23; for f in md sg envd; do cmp gpu-before/$f.sum gpu-w1/$f.sum && echo $f IDENTICAL; done; diff <(jq -c . gpu-before/lights.jsonl) <(jq -c . gpu-w1/lights.jsonl) \| head'` | all IDENTICAL (mesa did not move, so any difference points at ORT/libstdc++/musl and blocks) |
| V-SPEED | `psu sh /home/user/l21/run.sh U1-w1 /home/user/l21/profiles/A0-installed.sh 240 60`, compare `U0-before` vs `U1-w1` with `table.py` | modeld model ms within ±5 %, runs/s ±0.3, GPU busy ±3 points, hottest zone within L21's 3 C run-to-run spread |
| V-CAMERA (DVR attached) | `roadsense_mode on`, `curl -s http://127.0.0.1:8090/v1/health` → camera fps; `/preview` | ~20 fps, frames in the ring |

Rollback of window 1:
- Single package: `apk add <pkg>=<old version>` only works if the old `.apk` is still in `/var/cache/apk` (edge mirrors drop old versions).
- Otherwise restore from the local archive. For the whole system: boot the debug shell (step 3), mount the
  rootfs and `zstd -dc /home/user/l23/rootfs-pre.tar.zst | tar -xpf - --xattrs --xattrs-include=* --acls --numeric-owner -C /mnt`
  (this overwrites changed files; files added by the upgrade remain but are inert because `/lib/apk/db` comes back too).
- If the phone does not boot at all: `fastboot flash boot boot-partition-pre.img`.

### Window 2: mesa 26.3 alone (≈20 min + checks)
```sh
psu apk add mesa mesa-vulkan-freedreno mesa-egl mesa-gbm mesa-gl mesa-gles mesa-dri-gallium mesa-rusticl   # replaces the =26.2.6 constraints with plain names in world
psu apk upgrade --simulate | grep -i mesa      # only mesa lines
psu apk upgrade mesa mesa-vulkan-freedreno mesa-egl mesa-gbm mesa-gl mesa-gles mesa-dri-gallium mesa-rusticl
```
Adding the plain name replaces the pinned world entry. Never use `apk del` to lift a pin: it removes the package.
No reboot is needed for userspace Vulkan; reboot anyway to test the real start path.
Verify: `vulkaninfo --summary` (driver turnip, version 26.3.0), then **V-GPU** and **V-SPEED** against `before`:

| Result | Action |
|---|---|
| md/sg/envd sums identical, speed within the band | accept |
| sums differ | run `python3 ~/roadsense/tools/ncnn/compare_replay.py ~/l23/gpu-before/md.jsonl ~/l23/gpu-w2/md.jsonl` and the per-frame check of signd. Accept only within the roadsense accuracy gate (ROADMAP: lane lines < 0.11 m within 100 m, leads within the documented fp16 band, sign boxes within 2 px / scores within 0.01) **and** after a check without `TU_DEBUG=flushall` that the corruption mode has not changed (`modeld_replay … --ncnn-no-flushall` twice, outputs must differ only if they already did) |
| speed worse by > 5 % or new GPU faults in dmesg (`msm_dpu`/`adreno` hangs) | roll back |

Rollback of window 2 (tested material): `psu sh -c "'apk add /home/user/l23/apk-keep/mesa*-26.2.6-r0.*.apk && apk add mesa=26.2.6-r0 mesa-vulkan-freedreno=26.2.6-r0 …'"` (the same eight pins as in window 1), then V-GPU must return to the `before` sums.

### Window 3 (later, separate approval): firmware and qcom plumbing
Only with the LTE antenna connected (WireGuard as a second path) and a person at the phone.
Unpin `linux-firmware-*`, `soc-qcom*`, `tqftpserv` and upgrade them together, then reboot.
Verify V-AP (Wi-Fi firmware), BLE (`btmgmt info`, the esp1 satellite connects), V-SIM/modem, V-WG, `journalctl -b -u ath10k-retry`, and look for remoteproc crash lines in `dmesg`.
Rollback: restore `/lib/firmware` + `/usr/lib/firmware` + `/usr/share/…soc-qcom` files and `/lib/apk/db` from the local archive (`tar -xpf … ./usr/lib/firmware ./lib/apk/db …`), then reboot.
The kernel stays pinned until M1 is done; a kernel bump is its own window with `poco-usb host && poco-vbus apply` before the reboot.

### After each window
- `psu systemctl unmask --runtime prius-netwatch.service` (or simply reboot).
- `roadsense_mode auto`.
- Record the new package set (`apk info -v | sort > ~/l23/pkgs-after-wN.txt`, diff to the laptop).
- Update the CMDB (`os:` line, versions, held packages, backup location) and `dotinfra sync`.

---

## 7. Non-upgrade maintenance (each needs its own go)

| # | Item | Change | Risk |
|---|---|---|---|
| M1 | Make the DTB patches survive kernel updates | Either an apk hook or a `/etc/mkinitfs/hooks`-style re-apply (run `poco-usb host` / `poco-vbus apply` logic idempotently before boot.img generation), or a pmaports **local device-package fork** with the patched DTS (proper way, needs pmbootstrap build + local repo key on the phone). Until then: keep the kernel pinned in world | medium (wrong hook = boot without host mode; test with a RAM boot first) |
| M2 | Pin the kernel/device packages in world now | `apk add linux-postmarketos-qcom-sdm845=7.1_rc1-r4 device-xiaomi-beryllium-kernel-tianma=23-r0 device-xiaomi-beryllium=23-r0` | low (blocks unwanted bumps; must be lifted on purpose) |
| M3 | Journal limits | `/etc/systemd/journald.conf.d/10-prius.conf`: `SystemMaxUse=300M`, `MaxRetentionSec=1month`, `RateLimitBurst` default. Frees ~700 MB, cuts UFS writes | low |
| M4 | Bench reboot loop | Reset `/run/prius-netwatch.fail` when `prius-netwatch-arm` arms the timer (repo `POCO/rootfs` script). Optionally a bench flag that keeps netwatch off without LTE antenna | low |
| M5 | ssh hardening | `PasswordAuthentication no`, `KbdInteractiveAuthentication no` in a drop-in `/etc/ssh/sshd_config.d/60-prius.conf` (keys `piotr@ai` already in place; the PIN stays the sudo password). Keep a second session open while reloading sshd; armed rollback as for nft on 11-15. Optionally also add a second key (nokia1/P52) as a spare | low with the armed rollback; locking out = fastboot path |
| M6 | Firewall tidy | Remove the pmOS default accepts for AusweisApp2 (24727) and LocalSend (53317) (`50_ausweisapp2.nft`, `50_localsend.nft`; these are package files: override with an empty file of the same name in `/etc/nftables.d`). Nothing listens there today | low |
| M7 | Headless slimming | Disable unused services: `usb-signaller`, `hexagonrpcd` sensors / `iio-sensor-proxy`, user `gvfs`/gnome-keyring for uid 10000 (`gkr-pam` noise on every login), `avahi-daemon`, LLMNR (`resolved` `LLMNR=no`). Removing the phosh UI meta from world would drop ~70 of the 395 upgrades but is a large transaction: separate decision | low (services) / medium (package removal) |
| M8 | Cleanup per section 3 | rides copies (19 GB), L21 files after phase 2, bench recorder sessions, old rollback tarballs, old lane dirs, `.pre-l*` binaries | low (all re-creatable or archived first) |
| M9 | `/boot` fsck | ext2 `/boot` is never checked: run `e2fsck -n` read-only once, a real check only from the debug shell | low |
| M10 | Time sync | Fine as is (timesyncd + HTTPS fallback). chrony is installed but unused: leave it disabled or remove it from world, so it is never enabled by accident next to timesyncd | low |
| M11 | Permanent rescue | Flash a debug-shell boot image (peripheral-mode DTB) to the unused `recovery` partition (64 MB), bootable with Vol-up + power: a rescue path without the laptop's pmbootstrap | medium (check what `recovery` holds first; the bootloader must accept the image) |

---

## 8. Decisions for the owner

1. Option: **(b) selective, two windows** (recommended), (a) full, or (c) leave.
2. Where: on the bench (no hub/powerbox/camera/LTE: V-USB, V-WG and V-CAMERA are deferred to the car) or with the car hardware attached and the LTE antenna on.
3. Backup target: the laptop (19 GB free; the archive is ~4-5 GB) or node2. Keep the local copy on the phone until the next deploy.
4. Window 3 (Wi-Fi/BT/modem firmware) timing and presence for fastboot.
5. M1/M2 (kernel pin and making the DTB patches upgrade-proof), M5 (ssh keys only), M3 (journal cap), M8 (which cleanup).
6. Whether to fix the netwatch counter (M4) before more bench work, since the bench phone currently reboots about every 21 minutes while the uplink is down.
