#!/bin/sh
# build-userdata-image.sh - build the flashable pmOS userdata image of the POCO F1 car
# computer from a phone-backup.sh backup (laptop side, needs sudo, sfdisk, mkfs.ext4,
# GNU tar, zstd). docs/POCO_REBUILD.md section 2.
#
#   build-userdata-image.sh BACKUP_DIR OUT.img SIZE_GB [TAR_MEMBER ...]
#
# OUT.img: raw image with 4096-byte sectors: the phone's nested GPT (same label-id,
# PARTUUIDs, types, names; p2 shortened to fit SIZE_GB), p1 = the raw pmOS /boot ext2
# (same UUID), p2 = new ext4 with the phone's root UUID/label holding the rootfs archive.
# The pmOS initramfs grows p2 and its ext4 to the whole userdata partition at the first
# boot (subpartitions), so SIZE_GB only has to hold the data (+ ~3 GB).
# TAR_MEMBER (e.g. ./etc ./usr/local): extract only these (build test with little disk).
# Flash: fastboot flash userdata OUT.img (fastboot resparses), or convert first with
# img2simg (pmbootstrap chroot_native/usr/bin/img2simg) as pmbootstrap does.
set -eu
B=${1:?backup dir}; OUT=${2:?out.img}; SIZE_GB=${3:?size in GB}; shift 3
ROOT_UUID=$(sed -n 's/^Filesystem UUID: *//p' "$B/meta/tune2fs-root.txt")
ROOT_LABEL=$(sed -n 's/^Filesystem volume name: *//p' "$B/meta/tune2fs-root.txt")
[ -n "$ROOT_UUID" ] && [ -n "$ROOT_LABEL" ] || { echo "no root UUID/label in $B/meta/tune2fs-root.txt" >&2; exit 1; }

truncate -s "${SIZE_GB}G" "$OUT"
LAST=$(( SIZE_GB * 1024 * 1024 * 1024 / 4096 - 6 ))     # leave room for the backup GPT
P2START=$(sed -n 's|^/dev/loop0p2 : start= *\([0-9]*\),.*|\1|p' "$B/meta/sfdisk-loop0.txt")
sed -e "/^last-lba:/c last-lba: $LAST" -e "/^device:/d" \
    -e "s|^\(/dev/loop0p2 : start= *[0-9]*, size= *\)[0-9]*,|\1$((LAST - P2START + 1)),|" \
    "$B/meta/sfdisk-loop0.txt" > "$OUT.sfdisk"
L=$(sudo losetup -b 4096 -P -f --show "$OUT")
MNT=$(mktemp -d /tmp/poco-root.XXXXXX)
cleanup() { sudo umount "$MNT" 2>/dev/null || true; sudo losetup -d "$L" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true; }
trap cleanup EXIT
sudo sfdisk -q "$L" < "$OUT.sfdisk"
sudo partx -u "$L" 2>/dev/null || true
zstd -dc "$B/pmos-boot.img.zst" | sudo dd of="${L}p1" bs=1M conv=fsync status=none
sudo mkfs.ext4 -q -b 4096 -L "$ROOT_LABEL" -U "$ROOT_UUID" -O metadata_csum,64bit,orphan_file "${L}p2"
sudo mount "${L}p2" "$MNT"
zstd -dc "$B/rootfs.tar.zst" | sudo tar --xattrs --xattrs-include='*' --numeric-owner -xpf - -C "$MNT" "$@"
sudo mkdir -p "$MNT/var/log/journal"
sudo umount "$MNT"
sudo e2fsck -fy "${L}p2" >/dev/null || true
sudo e2fsck -fn "${L}p2"
sudo blkid "${L}p1" "${L}p2"
sudo sfdisk -d "$L"
echo "built $OUT"
