#!/bin/sh
# phone-backup.sh - full restorable backup of the POCO F1 car computer, phone side.
# Run as root ON THE PHONE (docs/POCO_REBUILD.md section 2), e.g.
#   sudo systemd-run --unit=poco-backup --collect -p Nice=19 -p IOSchedulingClass=idle \
#        taskset -c 0-3 sh /home/user/l25/phone-backup.sh /home/user/l25/img
# Writes into OUT (must be on the root fs, which is then excluded from the archive):
#   meta/      partition tables (sfdisk dumps + raw GPT head/tail of every LUN and of
#              the pmOS loop image), blkid, tune2fs, cmdline, apk state, deviceinfo
#   parts/     every partition except userdata, raw dd | zstd, + raw sha256
#   pmos-boot.img.zst   the pmOS /boot ext2 (loop0p1), raw
#   sqlite/    consistent online copies of the backend SQLite databases
#   rootfs.tar.zst      pax tar of / (one fs, numeric owners, xattrs) minus EXCLUDES
#   excluded.txt        every excluded path with size (and sha256 for the big files
#                       a copy exists of elsewhere)
#   SHA256SUMS          of every file above
# Needs: busybox, sfdisk, zstd, python3, rootfs-tar.py next to this script. Installs nothing.
set -eu
OUT=${1:?usage: phone-backup.sh OUTDIR}
HERE=$(cd "$(dirname "$0")" && pwd)
ZL=${ZSTD_LEVEL:-3}
mkdir -p "$OUT/meta" "$OUT/parts" "$OUT/sqlite"
cd "$OUT"
log() { echo "$(date -u +%H:%M:%SZ) phone-backup: $*"; }

# ---- excludes (re-creatable bulk; each listed in excluded.txt) ----
EXCL="/home/user/rides /home/user/l21/rec /home/user/l21/lf /home/user/l24 /home/user/l25
/home/user/rs-replay/ljkr.nv12 /home/user/rs-replay/ljkr.nv12.xz
/home/user/roadsense-maps/poland-20261114.rsmap /var/log/journal $OUT"
for f in /home/user/quant-work/*.onnx; do [ -e "$f" ] && EXCL="$EXCL $f"; done

log "meta"
cp /proc/cmdline meta/cmdline
uname -a > meta/uname
cp /etc/apk/world meta/apk-world
apk info -v 2>/dev/null | sort > meta/apk-installed.txt
cp /usr/share/deviceinfo/deviceinfo meta/deviceinfo
losetup -a > meta/losetup.txt
blkid > meta/blkid.txt
for l in /dev/disk/by-partlabel/*; do d=$(readlink -f "$l"); echo "$(basename "$l") $(basename "$d") $(blockdev --getsize64 "$d")"; done | sort -k2 -V > meta/partlabels.txt
for b in sda sdb sdc sdd sde sdf loop0; do
    sfdisk -d /dev/$b > meta/sfdisk-$b.txt 2>/dev/null || true
    ss=$(blockdev --getss /dev/$b); sz=$(blockdev --getsize64 /dev/$b)
    # GPT: protective MBR + header + entries = LBA 0..5 (4096-byte sectors); backup at the end
    dd if=/dev/$b of=meta/gpt-head-$b.bin bs=$ss count=6 2>/dev/null
    dd if=/dev/$b of=meta/gpt-tail-$b.bin bs=$ss skip=$((sz / ss - 5)) count=5 2>/dev/null
done
tune2fs -l /dev/loop0p2 > meta/tune2fs-root.txt
tune2fs -l /dev/loop0p1 > meta/tune2fs-boot.txt
cp /etc/fstab meta/fstab 2>/dev/null || true

log "partitions"
: > parts/SHA256SUMS.raw
while read -r label dev size; do
    case "$label" in userdata|primary) continue ;; esac
    f=parts/$(printf '%s' "$dev")-$label.img.zst
    dd if=/dev/$dev bs=1M 2>/dev/null | zstd -q -$ZL -T2 -o "$f" -f
    echo "$(sha256sum < /dev/$dev | cut -d' ' -f1)  $dev-$label.img" >> parts/SHA256SUMS.raw
done < meta/partlabels.txt
# re-read the partitions the modem/rmtfs may write while running; flag if they moved
for l in modemst1 modemst2 fsg fsc persist; do
    d=$(readlink -f /dev/disk/by-partlabel/$l)
    a=$(grep " $(basename "$d")-$l.img\$" parts/SHA256SUMS.raw | cut -d' ' -f1)
    b=$(zstd -dc parts/$(basename "$d")-$l.img.zst | sha256sum | cut -d' ' -f1)
    c=$(sha256sum < "$d" | cut -d' ' -f1)
    if [ "$a" = "$b" ] && [ "$a" = "$c" ]; then echo "$l stable" ; else echo "$l CHANGED-DURING-DUMP raw=$a img=$b now=$c"; fi
done > parts/stability.txt

log "pmOS boot (loop0p1)"
sync
dd if=/dev/loop0p1 bs=1M 2>/dev/null | zstd -q -$ZL -T2 -o pmos-boot.img.zst -f
echo "$(sha256sum < /dev/loop0p1 | cut -d' ' -f1)  pmos-boot.img" > pmos-boot.img.sha256

log "sqlite online copies"
for db in /home/user/cyberpunk/data/*.db; do
    [ -f "$db" ] || continue
    python3 -c "import sqlite3,sys; s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" "$db" "sqlite/$(basename "$db")" \
        || log "  sqlite copy failed: $db"
done

log "excluded list"
: > excluded.txt
for p in $EXCL; do
    [ -e "$p" ] || continue
    s=$(du -sk "$p" | cut -f1)
    case "$p" in
        *.rsmap|*.nv12|*.nv12.xz) h=$(sha256sum "$p" | cut -d' ' -f1) ;;
        *) h=- ;;
    esac
    echo "$p ${s}KiB sha256=$h" >> excluded.txt
done
( cd /home/user/rides 2>/dev/null && find . -type f -exec ls -ln {} + ) > meta/rides-filelist.txt 2>/dev/null || true

log "rootfs tar"
X=""; for p in $EXCL; do X="$X --exclude $p"; done
# shellcheck disable=SC2086
python3 "$HERE/rootfs-tar.py" --root / $X 2> rootfs-tar.log | zstd -q -$ZL -T2 -o rootfs.tar.zst -f
tail -3 rootfs-tar.log

log "checksums"
find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > SHA256SUMS
chown -R 10000:10000 "$OUT"
chmod -R go-rwx "$OUT"
du -sh "$OUT"
log "done"
