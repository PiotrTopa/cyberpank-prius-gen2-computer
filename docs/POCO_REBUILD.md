# POCO F1 car computer: time-bomb fix, full backup, restore and rebuild

Lane L25, 2026-11-18 21:30-22:30 CET, phone on the bench (no camera, gateway, powerbox or LTE
antenna attached), reached over its access point from the laptop's USB Wi-Fi card.
Owner decision of 2026-11-18: the OS stays pinned as it is (no general upgrade); this lane makes
the device-tree patch upgrade-proof, takes a full restorable backup and writes the rebuild plan.
The audit this builds on is [POCO_OS_MAINTENANCE.md](POCO_OS_MAINTENANCE.md) (phase 1, lane L23).

| Section | What |
|---|---|
| [1](#1-time-bomb-device-tree-patch-and-package-pins-applied) | Device-tree patch and package pins: **applied on the phone** |
| [2](#2-full-backup-image) | Full backup: what it is, where, what it restores |
| [3A](#3a-restore-from-the-backup) | Restore from the backup (fastboot, step by step) |
| [3B](#3b-rebuild-from-scratch) | Rebuild from scratch when no backup is usable |
| [3C](#3c-gaps-state-that-exists-only-on-the-phone) | Gaps: state whose only copy is the phone (and now the backup) |
| [4](#4-rescue-boot-test-prepared-not-done) | Rescue-boot test: prepared, not done |
| [5](#5-prepared-not-applied-ssh-firewall-journal-netwatch) | ssh keys-only, firewall, journal cap, netwatch fix: prepared commands, **not applied** |

Shell helper used throughout (the password never appears on a command line; it goes to sudo on stdin):

```sh
P=user@10.42.0.1                 # over the phone's AP `prius`; via WireGuard: user@10.200.0.14
psu() { dotinfra vault exec poco_user_password -- ssh "$P" "sudo -S -p '' $*"; }
```

---

## 1. Time bomb: device-tree patch and package pins (applied)

### The problem
The phone runs the stock pmOS kernel package `linux-postmarketos-qcom-sdm845-7.1_rc1-r4` with the
Tianma DTB `/boot/dtbs/qcom/sdm845-xiaomi-beryllium-tianma.dtb`. That file belongs to the kernel
package and carries three owner edits made in place on 2026-10-21/22:

| # | Edit | Why |
|---|---|---|
| P1 | `usb@a600000` `dr_mode` `peripheral` → `host` | USB host role (hub, powerbox, gateway, panda, MFD link) |
| P2 | `pmic@2/usb-vbus-regulator@1100` (`qcom,pm8150b-vbus-reg`, always-on) | the phone sources 5 V VBUS itself ([POCO/dts/pmi8998-usb-vbus.dtso](../POCO/dts/pmi8998-usb-vbus.dtso)) |
| P3 | `i2c@a98000/touchscreen@1` novatek → `touchscreen@38` `focaltech,ft8719` | replacement panel (harmless headless; kept so the DTB stays the running one) |

With `deviceinfo_flash_kernel_on_update="true"` any kernel or device package bump re-runs
`mkinitfs` → `boot-deploy`, which appends the **pristine** DTB to the kernel and `dd`s the new
boot.img to the `boot` partition. The next boot would come up in device mode with no VBUS.

### What was changed on the phone (2026-11-18 21:33-21:45 CET)

| Change | Where | Undo |
|---|---|---|
| 27 world pins `name=version` (kernel, device, firmware, mesa, libcamera, boot-deploy, mkinitfs) | `/etc/apk/world`; old world kept in `/root/l25/world.pre-pins-20261118` | `cp /root/l25/world.pre-pins-20261118 /etc/apk/world` (nothing to reinstall: versions are the installed ones) |
| `prius-dtb-patch`: the patch as a script (pristine DTB → patched DTB, idempotent, fail-closed) + `check`, `check-live`, `check-bootimg` | `/usr/local/sbin/prius-dtb-patch` (repo `POCO/rootfs/usr/local/sbin/`) | remove the file |
| `prius-bootimg`: builds boot.img with the real boot-deploy code into a scratch dir, never flashes; `compare` with the boot partition; `dryrun`, `hooktest` | `/usr/local/sbin/prius-bootimg` | remove |
| boot-deploy hook: re-applies the patch inside every `mkinitfs` before the DTB is appended; aborts boot-deploy (no flash) if it cannot | `/etc/boot-deploy/hooks/50-prius-dtb` (repo `POCO/rootfs/etc/boot-deploy/hooks/`) | remove |
| boot check `prius-dt-check.service` (enabled): running DT + boot partition + /boot DTB; flag `/run/prius-dt-check` (`ok …` / `FAIL …`), journal `crit`, unit failed on error | `/usr/local/sbin/prius-dt-check`, `/etc/systemd/system/prius-dt-check.service` | `systemctl disable prius-dt-check`, remove |
| apk commit hook: after every apk transaction, warns loudly if a pinned package moved or the DT check fails | `/etc/apk/commit_hooks.d/prius-dtb`, pin list `/usr/local/share/prius/apk-pins.txt` | remove |
| patch sources on the phone | `/usr/local/share/prius/dts/` (`pmi8998-usb-vbus.dtso`, `tianma-prius.dts.diff`, the pristine DTB of 7.1_rc1-r4) | remove |
| `POCO/provision.sh`: hooks get mode 755; `prius-dt-check.service` is enabled by `--core` and full runs | repo | |

Nothing was flashed and the phone was not rebooted.

### Proofs (run on the phone, 2026-11-18 21:42 CET)

| Proof | Result |
|---|---|
| `prius-dtb-patch apply <pristine 7.1_rc1-r4 DTB> out.dtb` | `sha256 6164722f…f60710`, **byte-identical** to `/boot/dtbs/qcom/sdm845-xiaomi-beryllium-tianma.dtb`. Pristine = the DTB inside the cached package `linux-postmarketos-qcom-sdm845-7.1_rc1-r4.17d6ae79.apk` and in the laptop's install chroot, `sha256 44f744d1…a229c0d2` |
| `prius-bootimg dryrun`: boot-deploy (real functions, flash disabled) on vmlinuz + patched DTB + `/boot/initramfs` | `boot.img` 31481856 bytes, `sha256 86e8ac75…2a4987`, **identical to the first 31481856 bytes of the boot partition** `/dev/sde45` (the remaining 35.6 MB of the partition hold stale bytes of an older, larger image; `dd` never clears them and the bootloader reads only the sizes in the header) |
| `prius-bootimg hooktest`: the same build through the **real** `/etc/boot-deploy/hooks` starting from a **pristine** DTB copy | hook logged `re-applied the DTB patch`, image **identical** to the boot partition: a kernel bump's pristine DTB ends up patched |
| fail-closed: hook on a non-beryllium DTB | hook exits 1, boot-deploy stops before mkbootimg, no boot.img is written |
| `prius-dtb-patch check-bootimg` on a boot.img built from the pristine DTB | P1/P2/P3 `MISSING`, exit 1; on `/dev/sde45`: all ok |
| `prius-dt-check` | `ok 2026-11-18T20:42:15Z running-dt boot-partition disk-dtb` |

Pins (`apk upgrade --simulate`, same 2026-11-17 index, no `apk update`):

| | before pins | after pins |
|---|---|---|
| transactions | 396 (incl. mesa 26.3.0 ×8, libcamera 0.8.0 ×6, linux-firmware 20261019 ×4, postmarketos-mkinitfs 2.12.2) | 376; **no** pinned package listed (only `postmarketos-base-mesa 78→79`, a config package, not pinned) |

`apk add --simulate` checks after pinning: `mesa-dev` → refused (`breaks: mesa-dev-26.3.0-r0[mesa-gles=26.3.0-r0]` …);
`vulkan-tools`, `gst-plugin-pipewire`, `pipewire-spa-libcamera` → no change; `htop` → installs only htop + lsof.

### Rules that follow
- **Never `apk add <pinned name>` without `=version`**: an explicit add replaces the world pin
  (`apk add linux-postmarketos-qcom-sdm845` would silently unpin the kernel). `apk add foo` for any
  other package is safe: it cannot move a pinned package; it fails instead.
- Never `apk upgrade --available`, never `--force-broken-world`.
- A deliberate kernel bump (own window, person at the phone): `apk add linux-postmarketos-qcom-sdm845=<new> device-xiaomi-beryllium=<new> device-xiaomi-beryllium-kernel-tianma=<new>` → the
  boot-deploy hook patches the new DTB → `prius-dt-check` must print `ok` (it checks the boot
  partition, i.e. what the next boot gets) **before** the reboot. If the hook fails, boot-deploy did
  not flash; fix the anchors in `prius-dtb-patch` (the DTS diff is in `POCO/dts/tianma-prius.dts.diff`),
  then `sudo mkinitfs`. Update `POCO/rootfs/usr/local/share/prius/apk-pins.txt` and the phone copy.
- `prius-usb`/`prius-vbus`/`poco-usb`/`poco-vbus` still work (they edit the same DTB); the hook
  makes them unnecessary after a kernel bump. `prius-usb device` (dr_mode peripheral) is now
  undone by the hook at the next `mkinitfs`: to switch to device mode on purpose, also
  `chmod -x /etc/boot-deploy/hooks/50-prius-dtb`.
- Backend: nothing reads `/run/prius-dt-check` yet. Follow-up (owner decision): surface it in
  `/api/v1/state` (e.g. `os.dt_check`) and as an alert type.

---

## 2. Full backup image

**Location (laptop only):** `/mnt/win/linux-home/backups__poco/20261118/` on the laptop's second
disk; `/home/piotr/backups/poco` is a symlink to `/mnt/win/linux-home/backups__poco`, so the
`/home/piotr/backups/poco/20261118/` paths in this document keep working (owner decision
2026-11-19: the only copy on the laptop lives there). **That disk is NTFS** (ntfs-3g, mounted
owner-only on Linux with `umask=077`, in fstab with `nofail`): Unix permissions are **not stored**
and **Windows can read every file**, including the secrets below. Nothing in the restore relies on
the file modes there: owners, modes and xattrs of the phone's files live inside the archives
(`rootfs.tar.zst` pax headers, raw partition images), and the scripts are run with `sh …`. If the
disk is not mounted (`nofail`), the symlink dangles: `sudo mount /mnt/win`. The backup holds
WireGuard keys, the SIM PIN, the backend auth token, the car's satellite signing key, ssh host
keys and the root ssh key: never copy it elsewhere, never attach it, never commit it. A phone-side
copy of the same files stays in `/home/user/l25/img/` (8.5 GB on the phone's own disk; the owner
decides when to delete it).

**How it was made:** `POCO/backup/phone-backup.sh` run as root on the phone in a `systemd-run`
unit (Nice 19, idle I/O, CPUs 0-3, MemoryMax 1G); the rootfs is archived by
`POCO/backup/rootfs-tar.py` (Python tarfile: busybox tar cannot store xattrs and no package may be
installed); pulled with `rsync --partial` over the AP.

**State: complete and verified (2026-11-19 00:30 CET).** Runbook §2.1 was run on 2026-11-19
00:00-00:30 CET; the phone had not rebooted since 2026-11-18 21:12 CET. Afterwards the phone lock
`/run/phone-lane.lock` was handed to lane L24 (`L24`) and the netwatch runtime drop-in left in place. Archive run on the phone 2026-11-18
21:49-22:12 CET (rootfs tar 21:57-22:07 CET); pulled 22:43-00:20 CET in one rsync pass
(no link drop, ≈ 1.4 MB/s); the phone-side copy `/home/user/l25/img/` is kept for now (owner decides).

| Item | Size | sha256 (of the file as stored) |
|---|---|---|
| whole set `img/` (128 files) | 9.11 GB (8.48 GiB) | see `SHA256SUMS` below |
| `rootfs.tar.zst` (90 331 entries, 11.04 GB raw, 14 xattr records, 0 vanished, 0 changed while read) | 6.02 GB | `6e7ad2a5d87f4548ce88ecca0c380b7c68a5ca4dfa7823803e8e6d6d65923ec0` |
| `pmos-boot.img.zst` (/boot ext2, 480 MiB raw) | 200 MB | `249efa5da0bb2f40107788c848a5c302f45112935597018897b73ab16e1946ac` |
| `parts/` 86 partition images (all but userdata; 18.1 GB raw incl. /boot) | 2.6 GiB | per file in `SHA256SUMS`, raw images in `parts/SHA256SUMS.raw` |
| ↳ `sdf6-modemst1` / `sdf7-modemst2` | 8 MiB raw each | `6bb94012…ea706661` / `117998ff…98dda95f`: full values in `SHA256SUMS` |
| ↳ `sda14-persist`, `sde36-fsg`, `sdf1-fsc`, `sde45-boot` | 64 / 8 / 0.25 / 64 MiB raw | `86e746e4…`, `927e5551…`, `71a8ed4e…`, `48b23167…` |
| `sqlite/` metrics.db, battery.db, fuel_log.db (online copies) | 190 MB | in `SHA256SUMS` |
| `meta/` GPT dumps, blkid, tune2fs, cmdline, apk world/installed, deviceinfo, rides file list | small | in `SHA256SUMS` |

`SHA256SUMS` (sha256 `6bd172e40db3d519ae020775bbd402e65a0ee31d93ff954b0639a45aa4ddac2a`) lists every file.
Modem EFS partitions were re-read after the dump: modemst1, modemst2, fsg, fsc, persist all
**stable** (`parts/stability.txt`).

Verification:

| Check | Phone | Laptop |
|---|---|---|
| `sha256sum -c SHA256SUMS` | 127/127 OK | 127/127 OK |
| `zstd -t` (86 files, 18 122 993 664 bytes raw) | OK | OK |
| full `tar -tv` of the archive | — | OK: 90 331 entries, exit 0, no warnings (apart from GNU tar's `hdrcharset` notice) |
| raw partition hashes vs `SHA256SUMS.raw`, `/boot` raw hash | (dump-time) | OK: 84/84 partition images match their raw dump-time hashes; `/boot` raw hash OK |
| build tests: subset (`… 14 ./etc ./usr/local ./root`, tmpfs) and full size (16 GB, see below) | — | OK: GPT label-id `210CA09F-…`, PARTUUIDs `c11fa8a5-…`/`899bc788-…`, fs UUIDs `b56c1f02-…`/`398a493f-…` equal to `meta/`; e2fsck clean; owners/modes kept (`/etc/shadow` 640 0:42, `car.key` 0600 0:0); image deleted |
| excluded files vs laptop copies | — | `ljkr.nv12`, `ljkr.nv12.xz`, `poland-20261114.rsmap`: sha256 equal |

Laptop free space after the pull: 8.5 GB (8128 MiB); other jobs were writing to the disk at the same time.

| Exclusion (re-creatable bulk) | Size | Other copy |
|---|---|---|
| `/home/user/rides` | 19.0 GiB | laptop `~/datasets/rides` (22 rides, superset) |
| `/home/user/l21/rec`, `/home/user/l21/lf` | 0.6 + 0.73 GiB | L21 lane bench recordings / frames |
| `/home/user/quant-work/*.onnx` | ≈ 0.75 GiB | quantisation variants (roadsense QUANTIZATION notes) |
| `/home/user/rs-replay/ljkr.nv12` (+ `.xz`) | 356 MiB | laptop `~/datasets/roadsense-replay/` (sha256 `706cb417…f6a0` on the phone) |
| `/home/user/roadsense-maps/poland-20261114.rsmap` | 250 MiB | laptop `~/roadsense-maps/packs/` (laptop sha256 `1ffedd95…1150`; compare with `excluded.txt`) |
| `/var/log/journal` | ≈ 1 GiB | none (history only) |
| `/home/user/l24`, `/home/user/l25` | 0.1 GiB / staging | other lane's live scratch; this backup's staging |


### 2.1 Runbook: finish the backup

Run from the laptop once the USB Wi-Fi card is back and joined to the AP `prius`
(`nmcli con up prius-ap-usb`; never touch the internal Wi-Fi). `psu` as at the top.

| Step | Command | Expect / decide |
|---|---|---|
| R0 reach + reset check | `ssh -o ConnectTimeout=8 $P 'uptime; date -u; cat /run/phone-lane.lock; journalctl --list-boots \| tail -3'` | Uptime since ≤ 21:12 CET = no reboot. A boot after 22:00 = unexpected reset (netwatch or other): note its time (`journalctl -b -1 -n 30`). Clock in the past → `psu date -u -s @$(date +%s)` |
| R1 lock | if `/run/phone-lane.lock` is missing (reboot) or says L25: `psu sh -c "'[ -e /run/phone-lane.lock ] \|\| echo L25 > /run/phone-lane.lock'"`. If it says L24: wait | L25 holds it |
| R2 netwatch | `psu sh -c "'systemctl show -p DropInPaths,ConditionResult prius-netwatch.service; systemctl is-active prius-netwatch.timer'"`. After a reboot the runtime drop-in is gone: re-apply it (§5 "Bench note") | drop-in listed, timer inactive |
| R3 job result | `psu sh -c "'systemctl status l25-backup --no-pager; journalctl -u l25-backup -o cat \| tail -8; ls -la /home/user/l25/img; tail -3 /home/user/l25/img/rootfs-tar.log'"` | last line `phone-backup: done`, `SHA256SUMS` present, `rootfs-tar: done files=…`. If the job died (reboot mid-tar, no `done`, no SHA256SUMS): R3b |
| R3b redo | `psu sh -c "'rm -rf /home/user/l25/img; systemd-run --unit=l25-backup --collect -p Nice=19 -p IOSchedulingClass=idle -p MemoryMax=1G taskset -c 0-3 sh /home/user/l25/phone-backup.sh /home/user/l25/img'"` (scripts are still in `/home/user/l25/`; else `scp POCO/backup/{phone-backup.sh,rootfs-tar.py} $P:l25/`) | ≈ 20 min; wait with one detached waiter on `systemctl is-active l25-backup` |
| R4 checksums on the phone | `psu sh -c "'cd /home/user/l25/img && sha256sum -c SHA256SUMS \| grep -v OK\$; zstd -t rootfs.tar.zst'"` | no output from the first, `OK` from zstd |
| R5 pull | `B=/home/piotr/backups/poco/20261118; rm -f $B/img/pmos-boot.img.zst` (partial copy from 22:05) then `until rsync -a --partial --timeout=60 $P:l25/img/ $B/img/; do sleep 20; done` (priority order if time is short: add `--exclude 'parts/sde4[78]-*' --exclude 'parts/sda18-*'` first, then a second run without them) and `chmod -R go-rwx $B` | ≈ 1 h 45 min; `df -h /home` must stay ≥ 8 GB free |
| R6 verify on the laptop | `cd $B/img && sha256sum -c SHA256SUMS && zstd -t *.zst parts/*.zst && zstd -dc rootfs.tar.zst \| tar --xattrs --xattrs-include='*' -tvf - > ../rootfs.list` and the `SHA256SUMS.raw` loop of "Verify the backup"; build test: `sh POCO/backup/build-userdata-image.sh $B/img $B/test.img 14 ./etc ./usr/local ./root && rm $B/test.img*` | all OK; blkid/sfdisk match `meta/` |
| R7 phone cleanup | keep `/home/user/l25/img` until R6 passes, then `psu rm -rf /home/user/l25/img` (8 GB, secrets) — or keep it as the fast local restore source (owner decision) | |
| R8 release | `psu sh -c "'rm -r /run/systemd/system/prius-netwatch.service.d && systemctl daemon-reload'"` only if no other lane needs it masked (check `/run/phone-lane.lock`, L24's units); `psu rm -f /run/phone-lane.lock` | netwatch back to normal (it reboots the bench phone ~21 min after arming while the uplink is down) |
| R9 records | fill the table above with final sizes/checksums (`SHA256SUMS` lines of `rootfs.tar.zst`, `pmos-boot.img.zst`), CMDB `servers/poco.md` backup line → complete, `dotinfra event add --host poco --type maintenance "full backup completed …"` | |

### What it restores and what it does not

| Restores exactly | Notes |
|---|---|
| every partition except `userdata` (bootloader chain xbl/abl/tz/…, boot, dtbo, vbmeta, modem, dsp, bluetooth, system, vendor, cust, **persist, modemst1, modemst2, fsg, fsc** = device-unique calibration/EFS) | raw `dd` images; partition tables as `sfdisk` dumps and raw GPT head/tail per LUN |
| the pmOS `/boot` (loop0p1, ext2, same UUID `b56c1f02-…`) | raw image |
| the root filesystem content (owners numeric, modes, mtimes, symlinks, hard links, devices, all xattrs incl. file capabilities and ACLs) | a file archive, not a block image: the fs is re-created with the same UUID/label, so the existing boot.img cmdline (`pmos_root_uuid=398a493f-…`) matches |
| backend SQLite databases | in the tar (live copy) and as consistent online copies in `sqlite/` |

| Does not restore | Why / how to get it back |
|---|---|
| the excluded bulk (table above, `excluded.txt`) | rides: laptop `~/datasets/rides` (28 GB, superset); `ljkr.nv12`: laptop `~/datasets/roadsense-replay/`; map pack: laptop `~/roadsense-maps/packs/poland-20261114.rsmap` (sha256 equal, see table); L21 frames/recordings and quant-work ONNX variants: re-creatable lane material; journal: history only |
| state written after the archive run (21:57 CET on 2026-11-18, or the R3b redo) | the next backup |
| exact block layout of the root fs (inode numbers, fragmentation, free-space content) | not needed: nothing on the phone depends on inode numbers |
| socket files, `/run`, `/tmp` (tmpfs), `/proc`, `/sys`, `/dev` content | runtime only |
| hardware faults (UFS, flex cables, PMIC) | — |

**Consistency:** the archive is of the running system (backend, NetworkManager, journald live).
Files that changed while being read are listed in `rootfs-tar.log` (none expected to matter: the
SQLite databases have consistent copies; NetworkManager/systemd state files are rewritten at boot).
The modem EFS partitions were read and re-read: `parts/stability.txt`.

### Verify the backup (laptop)

```sh
B=/home/piotr/backups/poco/20261118
cd "$B" && sha256sum -c SHA256SUMS                        # transfer + storage
zstd -t rootfs.tar.zst pmos-boot.img.zst parts/*.zst        # compression intact
zstd -dc rootfs.tar.zst | tar --xattrs --xattrs-include='*' -tvf - > /dev/null   # whole archive readable
for f in parts/*.img.zst; do n=$(basename "$f" .zst); h=$(zstd -dc "$f" | sha256sum | cut -d' ' -f1); grep -q "^$h  $n\$" parts/SHA256SUMS.raw || echo "MISMATCH $n"; done
```

### Build the flashable userdata image from the backup (laptop)

The phone's `userdata` partition (sda21, 113 GB) holds a nested GPT ("subpartitions"):
p1 ext2 `/boot` (LBA 2048, 122880 × 4096 B), p2 ext4 `/`. The pmOS initramfs **always grows p2
and its ext4 to fill userdata** on boot when it finds subpartitions (`resize_root_partition`,
`init_functions_2nd.sh`), exactly like the 3.5 GB image of the original install. So the restore
image only needs room for the data.

Needs: about **2 × data** free on the laptop (≈ 11 GB image + ≈ 11 GB sparse output), or flash the
raw image directly (fastboot resparses, ≈ 11 GB free). On 2026-11-18 the laptop had 18 GB free with
the backup in place, so the full build was **not** run there (it would leave < 8 GB); the
procedure below was run end to end on a reduced root (see "Build test").

```sh
B=/home/piotr/backups/poco/20261118      # the backup (section 2)
W=/home/piotr/poco-restore               # needs ≥ 12 GB free (or node2, with the owner's OK)
mkdir -p "$W"
sh ~/projects/cyberpank-prius-gen2-computer/POCO/backup/build-userdata-image.sh "$B" "$W/userdata.img" 14
# 14 = GB; ≥ the archive's "bytes=" in rootfs-tar.log + 3 GB
# optional, as pmbootstrap does (fastboot can also take the raw image and resparse it):
sudo ~/.local/var/pmbootstrap/chroot_native/usr/bin/img2simg "$W/userdata.img" "$W/userdata.simg"
```
What the script does: a sparse file with 4096-byte sectors; the phone's nested GPT from
`meta/sfdisk-loop0.txt` (same label-id, PARTUUIDs, types and names; p2 shortened to the file
size); p1 = the raw `/boot` ext2 image (same UUID `b56c1f02-…`); p2 = `mkfs.ext4` with the phone's
root UUID `398a493f-…` and label `pmOS_root`; the archive extracted with
`tar --xattrs --xattrs-include='*' --numeric-owner -xp`; `e2fsck`. The boot partition's cmdline
names both UUIDs, so the existing boot.img boots the restored image unchanged.
(GNU tar prints "Ignoring unknown extended header keyword 'hdrcharset'" for files with binary
xattrs; harmless, the xattrs are restored. The ext2 `/boot` image is restored raw, so its
`lost+found` and DTB backups come back too.)

**Build test: done 2026-11-19** with a subset (`./etc ./usr/local ./root`) in a 14 GB sparse file
on tmpfs: see the verification table above. The full-size build stays a procedure until ≥ 12 GB
of disk can be spared. Full-size build: **done 2026-11-19 00:27-00:31 CET** on the owner's scratch disk
(`/mnt/win/linux-scratch/l25/`, NTFS): `build-userdata-image.sh … userdata.img 16` exit 0; the whole
archive extracted (ext4: 90 068 inodes, 11.6 GB used of 16 GB), `e2fsck -fn` clean, GPT label-id,
PARTUUIDs and both fs UUIDs equal to `meta/`. Only the img2simg/fastboot step was not run (no phone
in fastboot). The image was deleted afterwards.

---

## 3A. Restore from the backup

### Getting the phone into fastboot (headless unit)

| Way | How | State |
|---|---|---|
| 1. From the running system (preferred) | `psu systemctl reboot --reboot-argument=bootloader` (equivalently `reboot bootloader`). The live DT has `pon@800` with `mode-bootloader = <2>` (`syscon-reboot-mode` driver loaded), so the kernel writes the bootloader reason into the PMIC and ABL stops in fastboot | **not tested on this unit** (4: rescue-boot test). Needs ssh, i.e. a running system |
| 2. Power key + Volume-down by hand | phone off (powerbox force-off: GP29 held 12 s), then hold Vol-down and power on (GP29 6 s) | the volume keys are not wired on this headless build: needs the back open and the side-key flex, or a wire (row 3) |
| 3. Wired Volume-down (hardware change, owner) | Vol-down is the PM8998 `RESIN_N` line, active low against GND. A second powerbox output through an open-drain transistor (2N7000, like the power-key line on GP29) pulled low during the 6 s power-on press gives fastboot remotely | to build; then fastboot is reachable even with a dead rootfs |
| 4. EDL (Qualcomm 9008) | test points on the board; Poco F1 EDL flashing needs a Xiaomi-authorised account (firehose auth) | last resort, not practical |

In fastboot the phone's USB-C port is a **device**: unplug the car hub from the phone and cable the
phone to the laptop (or nokia1). The bootloader does not enable VBUS. Fastboot on the laptop:
`sudo apt install fastboot` (not installed on 2026-11-18), or `pmbootstrap flasher` (uses the
chroot's android-tools).

### Restore paths

| Failure | Restore | Hand on the phone | Duration |
|---|---|---|---|
| bad kernel/initramfs/DTB, system does not come up | `zstd -dc $B/parts/sde45-boot.img.zst > boot.img && fastboot flash boot boot.img && fastboot reboot` | fastboot entry + USB cable | ~5 min |
| broken rootfs (bad upgrade, deleted files), boot partition fine | build `userdata.simg` (2), `fastboot flash userdata userdata.simg && fastboot reboot` | fastboot entry + cable | build 30-45 min (laptop), flash ~5-10 min, first boot ~3 min (grows the fs) |
| partial: a few files, phone still reachable | `zstd -dc $B/rootfs.tar.zst \| tar -x … ./etc/prius` on the laptop, `rsync` back (or use the phone-side copy `/home/user/l25/img/rootfs.tar.zst`) | none | minutes |
| lost modem calibration / EFS (no LTE, IMEI gone) | `fastboot flash modemst1 sdf6-modemst1.img`, `modemst2`, `fsg`, `fsc`, `persist` (decompress first). ABL may refuse critical partitions: `fastboot flashing unlock_critical` first (wipes nothing on this unit's state, but needs a hand on the phone to confirm) | yes | ~5 min |
| bootloader chain / everything | flash every `parts/*` image to its partition (`meta/partlabels.txt` maps label ↔ device); the A/B bootloader pairs (`xbl_a/_b` …) and system/vendor/cust come back as they were | yes | ~30 min |

After a userdata restore: excluded data back from the laptop (as needed):
`rsync -a --partial ~/datasets/rides/ $P:rides/` (19 GB, ~3.5 h over the AP; only for replay on the phone),
`scripts/roadsense-maps install --host $P` (map pack), `ljkr.nv12` into `~user/rs-replay/` (GPU check input).

### Verify after a restore

| Check | Command | Pass |
|---|---|---|
| boot + DT patch | `ssh $P 'uname -r; systemctl --failed --no-legend; prius-dt-check status'` | `7.1.0-rc1-sdm845`; only `usb-signaller` failed; `ok …` |
| USB host + VBUS | `psu prius-dtb-patch check-live; psu sh -c "'grep -l usb_vbus /sys/class/regulator/*/name; cat /sys/kernel/debug/regmap/0-02/registers \| grep ^1109'"` | P1-P3 ok; regulator present; `1109: 0a` |
| hub, powerbox, gateway, panda, MFD | `lsusb -t`; `curl -s http://127.0.0.1:8080/api/v1/state \| jq .connection` (on the phone) | hub 1-1, powerbox, gateway, panda enumerate; links fresh |
| AP lifeline | laptop: `ping -c3 10.42.0.1`; `ssh $P prius-lifeline status` | AP up |
| WireGuard (LTE antenna on) | `psu wg show wg-homelab latest-handshakes`; laptop `ssh user@10.200.0.14` | handshake < 2 min |
| SIM/modem | `journalctl -b -u prius-sim-unlock`; `mmcli -m any` | PIN verified, registered |
| backend | `ssh $P 'curl -s http://127.0.0.1:8080/health; systemctl is-active prius-backend prius-bled prius-gnss-bridge prius-lifeline prius-power'` | all active |
| pins | `psu apk upgrade --simulate \| grep -E "mesa\|linux-postmarketos\|firmware\|libcamera"` | nothing |
| roadsense GPU | the audit's `gpucheck.sh` (POCO_OS_MAINTENANCE.md §6 step 1) | md/sg/envd sums equal to the pre-backup run |

---

## 3B. Rebuild from scratch

Use when neither the backup nor the phone's own disk is usable. Most of a day. Order matters.

| # | Piece | How | Source of truth |
|---|---|---|---|
| 1 | Bootloader unlocked | already (2026-10-21); a new phone: Mi Unlock, account in vault `xiaomi_unlock_password` | vault |
| 2 | pmOS install | laptop `~/pmbootstrap` (3.12.1 checkout, config `~/.config/pmbootstrap_v3.cfg`): `device=xiaomi-beryllium`, `kernel=tianma` (**not** ebbg: black screen on this panel), `ui=phosh` (headless now; `ui=none`/console would also do, but the world list below assumes phosh), `hostname=poco`, `timezone=Europe/Warsaw`, `extra_packages=dtc,usbutils`, channel edge, systemd. `./pmbootstrap.py install --password <PIN from vault poco_user_password>` then `./pmbootstrap.py flasher flash_rootfs && ./pmbootstrap.py flasher flash_kernel` (phone in fastboot). Edge has moved since 10-21: expect a different kernel; P1-P3 go on by step 4 | pmbootstrap config; original image `~/.local/var/pmbootstrap/chroot_native/home/pmos/rootfs/xiaomi-beryllium.img` (2026-10-21, sparse 2.3 GB) and the install chroot `chroot_rootfs_xiaomi-beryllium` are still on the laptop |
| 3 | Packages | world list: `$B/meta/apk-world` (exact), versions `$B/meta/apk-installed.txt`; repo `POCO/packages.txt` is the curated subset; `apk add $(cat world)`; then the pins (`POCO/rootfs/usr/local/share/prius/apk-pins.txt`) once the versions are chosen | backup, repo |
| 4 | DT patch (USB host, VBUS, touch) | `POCO/sync.sh --core` installs `prius-dtb-patch`, the boot-deploy hook and `prius-dt-check`; then `sudo mkinitfs` (the hook patches and flashes), `prius-dt-check` → `ok`, reboot. Different kernel: check `prius-dtb-patch check` on the new DTB first | repo `POCO/rootfs`, `POCO/dts` |
| 5 | Users, ssh | user `user` (uid 10000, groups wheel audio video netdev plugdev feedbackd) is created by pmbootstrap; `authorized_keys`: the laptop key(s) `piotr@ai` (ed25519 + rsa, `~/.ssh/id_*.pub` on the laptop). ssh host keys: from the backup (`etc/ssh/ssh_host_*`) to keep the fingerprints, else new ones (update `known_hosts`). root key for the MFD Pi `/root/.ssh/id_ed25519`: from the backup, else new + authorise on the Pi (`prius_rpi_ssh`) | laptop, backup (**gap**) |
| 6 | WireGuard, AP, home/dev Wi-Fi, LTE | `POCO/sync.sh` (full, not `--core`) renders `rootfs/etc/NetworkManager/system-connections/*.tmpl` from `POCO/secrets.env` (laptop, gitignored): `WG_PRIVATE_KEY` = vault `poco_wg_private_key`, `AP_PSK` = `poco_ap_psk`, `SIM_PIN` = `poco_sim_pin`, home/dev PSKs (only in `secrets.env`); WG peer/endpoint in the CMDB `networks/` file. The hand-made `home.nmconnection` is not in the repo (backup) | repo, vault, `secrets.env`, CMDB |
| 7 | SIM unlock | `prius-sim-unlock.service` (repo), PIN file `/etc/prius/sim-pin` rendered from `SIM_PIN` | repo, vault |
| 8 | nftables | pmOS defaults + `POCO/rootfs/etc/nftables.d/50_prius_backend.nft`; `50_roadsense.nft` from the roadsense installer | repos |
| 9 | prius-* units, flags, backend | `POCO/sync.sh --core --app --sudo-stdin` (units, scripts, udev, sysctl, watchdog 10-watchdog.conf, backend venv via `prius-backend ensure-venv`, dashboard `dist/` built on the laptop). Then restore `/etc/prius/backend.env` (hand-tuned; **gap**, backup), `backend-auth.env` (vault `prius_backend_auth_token`), `/etc/prius/satellites/` (car Ed25519 key + keychain: **gap**, backup only; without it every satellite must be re-enrolled), `/var/lib/prius/auth/sessions.json` (optional: browsers can re-pair) | repo, vault, backup |
| 10 | Backend data | `/home/user/cyberpunk/data/` (metrics.db, battery.db, alerts, trip records): backup `sqlite/` + tar (**gap**) | backup |
| 11 | ath10k retry, watchdog extra | `/etc/systemd/system/ath10k-retry.service` and `/etc/systemd/system.conf.d/watchdog.conf` exist only on the phone (**gap**, backup; the repo's `10-watchdog.conf` covers the watchdog) | backup |
| 12 | Head-unit ADB tooling | `tools/headunit/install.sh --sudo-stdin` (car repo) + adb key from vault `poco_adbkey_b64` | repo, vault |
| 13 | roadsense | roadsense repo: `scripts/sync-phone.sh`, ncnn `scripts/build-ncnn.sh` (tag 20261020, unmodified; the phone's `~/vulkan-work/ncnn-blob-barrier.patch` is an unapplied experiment, backup only), `CMAKE_ARGS="-DROADSENSE_NCNN=ON -DNCNN_DIR=/home/user/vulkan-work/ncnn" BUILD=build-ncnn sh scripts/install-phone.sh`, `install-camerad-dvr.sh --enable`; signs: roadsense-signs `scripts/install-phone.sh` (signd built in `build-l15`, unit by hand, CMDB). Models: `scripts/fetch-models.sh` + `push-models.sh` (roadsense) and roadsense-signs `fetch-models.sh`; the complete installed set (1.1 GB, incl. lights/sign-verifier/l12 classifier) is only on the phone and in the backup (**gap**: check that every model has a fetch or training record). Config `/etc/roadsense/*.json`: installer defaults, then from the backup: `calib.json` (camera calibration), `mapd.json` (`vision_odometry false`), `recorder.json` (**gap**) | repos, backup |
| 14 | Map pack | `scripts/roadsense-maps install --host user@<phone>` + the printed root symlink step | laptop `~/roadsense-maps/packs/` |
| 15 | udev rules | car repo `POCO/rootfs/etc/udev/rules.d` (rp2040-nomm, mfd-usb0, headunit); roadsense `60-roadsense-dvr.rules` from its installer | repos |
| 16 | Time | timesyncd (default) + `prius-clock-fallback.timer` (repo, full sync); `htpdate` in world. No RTC: first boot without LTE starts in the past: `date -u -s @<epoch>` over the AP | repo |
| 17 | Headless | `systemctl set-default multi-user.target; systemctl disable greetd`; `backlight-off.service` (repo) | CMDB |
| 18 | Recorder sessions | `/var/lib/roadsense/rec` (real drives not yet copied off): backup only (**gap**) | backup |
| 19 | Pins + checks | world pins (1), `prius-dt-check` ok, then section 3A verify list | this doc |

---

## 3C. Gaps: state that exists only on the phone

All of these are in the 2026-11-18 backup; none has another copy. Each needs a home (repo
template, vault key, or a laptop copy) so a rebuild does not depend on a backup:

| Gap | Path on the phone | Suggested home |
|---|---|---|
| car Ed25519 satellite signing key + keychain (esp1 enrolled) | `/etc/prius/satellites/car.key`, `car.pub`, `keychain.json` | vault (`prius_car_ed25519`), keychain in the CMDB or repo data |
| root ssh client key for the MFD Pi | `/root/.ssh/id_ed25519` | vault, or accept re-keying |
| ssh host keys | `/etc/ssh/ssh_host_*` | vault (optional) |
| backend config (hand-tuned) | `/etc/prius/backend.env` (+ 5 dated copies) | repo template `backend.env.tmpl` with the car's values |
| backend data | `/home/user/cyberpunk/data/` (metrics.db, battery.db, alerts, trip logs) | periodic pull to the laptop |
| roadsense device config | `/etc/roadsense/calib.json`, `mapd.json`, `recorder.json`, `camera*.json`, `api.env` | roadsense repo `deploy/poco/` |
| roadsense models (complete set) | `/var/lib/roadsense/models` (1.1 GB) | laptop copy + fetch script coverage |
| recorder sessions (real drives) | `/var/lib/roadsense/rec` (1.2 GB) | pull to the laptop's rides tree |
| ath10k retry unit, extra watchdog conf | `/etc/systemd/system/ath10k-retry.service`, `/etc/systemd/system.conf.d/watchdog.conf` | car repo `POCO/rootfs` |
| home Wi-Fi profile (hand-made) | `/etc/NetworkManager/system-connections/home.nmconnection` | drop (duplicate of `prius-home-wifi`) or template |
| home/dev Wi-Fi PSKs | NM profiles; laptop `POCO/secrets.env` | vault keys |
| ncnn experiment patch, lane results | `~user/vulkan-work/ncnn-blob-barrier.patch`, `~user/l*`, `~user/quant-work` (non-ONNX), `~user/pocopilot` | roadsense repo lanes / laptop |
| app rollback tarballs | `~user/*-pre-*.tgz` | not needed after the next deploy |
| DTB edit history | `/root/boot-backup/` | superseded by `prius-dtb-patch` + the pristine DTB in `/usr/local/share/prius/dts/` |

---

## 4. Rescue-boot test (prepared, not done)

Needs the owner at the phone (fastboot entry may fail and need the power key) and the USB cable
moved from the car hub to the laptop. Nothing is written to the phone.

1. Laptop: `sudo apt install fastboot` (or use `pmbootstrap flasher`, which installs android-tools in its chroot).
2. `psu systemctl reboot --reboot-argument=bootloader`; move the phone's USB-C cable from the hub to the laptop.
   Expected within ~20 s: `fastboot devices` lists the phone (serial in the CMDB). If ssh is gone instead and nothing
   enumerates after 2 min: the PON reason path does not work on this ABL → force off (powerbox GP29
   12 s) and use Vol-down + power by hand; note it in this doc and the CMDB.
3. RAM-boot the current boot image (flashes nothing): `zstd -dc $B/parts/sde45-boot.img.zst > /tmp/boot.img && fastboot boot /tmp/boot.img`
   → the phone boots the normal system (host mode: USB networking is gone, so check over the AP:
   `ssh user@10.42.0.1 prius-dt-check status`).
4. Optional, the pmOS debug shell: `cd ~/pmbootstrap && ./pmbootstrap.py flasher boot --cmdline pmos.debug-shell`
   boots the laptop chroot's kernel with a **device-mode** DTB (it is the unpatched install chroot) →
   telnet `172.16.42.1` over the USB cable. From there the root fs can be mounted and the backup
   untarred over it. (The chroot's kernel must still match the phone's: same 7.1_rc1-r4 today.)
5. `fastboot reboot`, cable back to the hub, verify with the 3A table.

Record the outcome (way 1 works or not, time to fastboot) in the CMDB `servers/poco.md`.

---

## 5. Prepared, not applied: ssh, firewall, journal, netwatch

Owner's target: ssh only over WireGuard and the lifeline AP, keys only. Run these **with the owner
at the phone**, two ssh sessions open (one stays logged in as a way back), each step with an armed
rollback as on 2026-11-15. Nothing here was executed by lane L25.

### M5: ssh keys only
```sh
# armed rollback first (undoes the drop-in after 5 min unless cancelled)
psu systemd-run --unit=ssh-rollback --on-active=5min sh -c "'rm -f /etc/ssh/sshd_config.d/60-prius.conf && systemctl reload sshd'"
psu sh -c "'printf \"%s\n\" \"# keys only (owner 2026-11-xx)\" \"PasswordAuthentication no\" \"KbdInteractiveAuthentication no\" \"PermitRootLogin no\" \"AuthenticationMethods publickey\" > /etc/ssh/sshd_config.d/60-prius.conf'"
psu sh -c "'/usr/sbin/sshd.pam -t && /usr/sbin/sshd.pam -T | grep -E \"^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|authenticationmethods) \"'"
psu systemctl reload sshd
ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no $P true   # must FAIL: Permission denied (publickey)
ssh $P true                                                                    # must work (key)
psu systemctl stop ssh-rollback.timer                                          # keep it
```
The PIN stays the sudo password. Spare key (recommended): add nokia1's public key to `~user/.ssh/authorized_keys` first.
Repo: add `POCO/rootfs/etc/ssh/sshd_config.d/60-prius.conf` with the same lines so `sync.sh` keeps it.

### M5b: port 22 only on WireGuard and the AP
A new file next to the backend guard (same pattern: priority -10 drops before the pmOS accepts):
```sh
psu systemd-run --unit=nft-rollback --on-active=3min sh -c "'rm -f /etc/nftables.d/52_prius_ssh.nft && nft -f /etc/nftables.nft'"
psu sh -c "'cat > /etc/nftables.d/52_prius_ssh.nft'" <<'EOF'
# ssh only from WireGuard and the car AP (owner 2026-11-xx); everything else dropped
table inet prius_ssh_guard {
	chain input {
		type filter hook input priority -10; policy accept;
		tcp dport 22 jump ssh_ports
	}
	chain ssh_ports {
		iifname "lo" return
		iifname "wg*" return
		iifname "wlan*" ip saddr 10.42.0.0/24 ip daddr 10.42.0.1 return
		counter drop comment "ssh: interface not allowed"
	}
}
EOF
psu nft -c -f /etc/nftables.nft && psu nft -f /etc/nftables.nft
ssh $P true                       # over the AP: works; over WG (LTE antenna on): ssh user@10.200.0.14 true
psu systemctl stop nft-rollback.timer
```
Effect: the USB device-mode lifeline (172.16.42.1, only in device mode) and Wi-Fi client mode lose
ssh. Keep `iifname "usb*" return` in the chain if the USB lifeline should stay a way in.
Repo: `POCO/rootfs/etc/nftables.d/52_prius_ssh.nft`.

### M6: drop the unused pmOS accepts (24727 AusweisApp2, 53317 LocalSend)
```sh
psu sh -c "'cp -a /etc/nftables.d/50_ausweisapp2.nft /etc/nftables.d/50_localsend.nft /root/l25/ && : > /etc/nftables.d/50_ausweisapp2.nft && : > /etc/nftables.d/50_localsend.nft && nft -c -f /etc/nftables.nft && nft -f /etc/nftables.nft'"
psu sh -c "'nft list ruleset | grep -cE \"24727|53317\"'"        # 0
```
(These are package config files: an upgrade of postmarketos-base-nftables writes `.apk-new` next to them instead of overwriting.)

### M3: journal cap
```sh
psu sh -c "'mkdir -p /etc/systemd/journald.conf.d && printf \"[Journal]\nSystemMaxUse=300M\nMaxRetentionSec=1month\n\" > /etc/systemd/journald.conf.d/10-prius.conf && systemctl restart systemd-journald && journalctl --disk-usage'"
```
Restarting journald is safe (no daemon-reload, so the transient key-off timers are not re-armed).
Repo: `POCO/rootfs/etc/systemd/journald.conf.d/10-prius.conf`.

### M4: netwatch failure counter reset when armed (repo change + deploy)
`POCO/rootfs/usr/local/sbin/prius-power`, function `netwatch_later` and `netwatch_stop`:
```diff
 netwatch_stop() {
     systemctl stop "$NETWATCH_ARM_UNIT.timer" "$NETWATCH_ARM_UNIT.service" >/dev/null 2>&1 || true
     systemctl stop prius-netwatch.timer prius-netwatch.service >/dev/null 2>&1 || true
+    rm -f /run/prius-netwatch.fail
 }
@@ netwatch_later
     systemd-run --quiet --collect --unit="$NETWATCH_ARM_UNIT" --on-active="$g" \
         --timer-property=AccuracySec=15s --description="Arm prius-netwatch after key-off grace ($g)" \
-        systemctl start prius-netwatch.timer || echo "prius-power: could not arm netwatch" >&2
+        sh -c 'rm -f /run/prius-netwatch.fail; exec systemctl start prius-netwatch.timer' \
+        || echo "prius-power: could not arm netwatch" >&2
```
Deploy with `POCO/sync.sh --core --sudo-stdin` (no restart of the backend needed; prius-power is a oneshot).

### Bench note: masking netwatch for a session
`systemctl mask --runtime prius-netwatch.service` (audit step 0) **does not work** on this phone:
the unit file lives in `/etc/systemd/system`, which takes precedence over the runtime mask in
`/run/systemd/system` (`LoadState` stays `loaded`). Use a runtime drop-in instead (gone at reboot):
```sh
psu sh -c "'mkdir -p /run/systemd/system/prius-netwatch.service.d && printf \"[Unit]\nConditionPathExists=/nonexistent/lane-session-netwatch-off\n\" > /run/systemd/system/prius-netwatch.service.d/99-lane-off.conf && systemctl daemon-reload && systemctl stop prius-netwatch.timer'"
# undo: rm -r /run/systemd/system/prius-netwatch.service.d && systemctl daemon-reload
```
`daemon-reload` re-arms the transient key-off timers (Wi-Fi suspend) to a full 20 min.
