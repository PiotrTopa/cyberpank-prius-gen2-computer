"""Time sync for the DVR camera: packet layout and the FAT check that guards the write."""
import shutil
import subprocess
import time

import pytest

from cyberpunk_computer.backend import dvr_timesync as ts

SECTOR = 512


def test_time_packet_layouts():
    t = time.struct_time((2026, 11, 17, 23, 58, 7, 0, 0, 0))
    v1 = ts.time_packet(True, t)
    v0 = ts.time_packet(False, t)
    assert len(v1) == len(v0) == SECTOR
    assert v1[:11] == bytes([11, 0, 7, 0, 0xEA, 0x07, 11, 17, 23, 58, 7])
    assert v0[:9] == bytes([11, 7, 0xEA, 0x07, 11, 17, 23, 58, 7])
    assert not any(v1[11:]) and not any(v0[9:])


def test_cmd_lba_per_layout():
    assert ts.CMD_LBA == {0x380: 0x340, 0x300: 0x2C0}


def _image(tmp_path, fat, with_hwc=True, part_lba=8):
    """A partitioned FAT image built with mkfs.vfat + mtools, like the camera's card."""
    if not all(shutil.which(x) for x in ("mkfs.vfat", "mmd", "mcopy")):
        pytest.skip("mkfs.vfat / mtools not installed")
    size_mb = 64 if fat == 32 else 16
    part = tmp_path / "part.img"
    part.write_bytes(b"\0" * (size_mb << 20))
    # FAT32 needs >= 65525 clusters to be FAT32 by the cluster-count rule: 512-byte clusters
    spc = "1" if fat == 32 else "8"
    subprocess.run(["mkfs.vfat", "-F", str(fat), "-s", spc, str(part)], check=True, capture_output=True)
    run = lambda *a: subprocess.run(list(a), check=True, capture_output=True)
    run("mmd", "-i", str(part), "::Android", "::Android/data", "::Android/data/com.fvision.camera")
    blob = tmp_path / "blob"
    blob.write_bytes(b"\x5a" * 8192)
    run("mcopy", "-i", str(part), str(blob), "::VSFILE")
    if with_hwc:
        run("mcopy", "-i", str(part), str(blob), "::Android/data/com.fvision.camera/HWC")
    mbr = bytearray(part_lba * SECTOR)
    mbr[0x1C6:0x1CA] = part_lba.to_bytes(4, "little")
    mbr[510:512] = b"\x55\xaa"
    disk = bytes(mbr) + part.read_bytes()
    return disk


def _reader(disk):
    return lambda lba, n: disk[lba * SECTOR: lba * SECTOR + n]


def _find(disk, pattern):
    """LBAs (whole sectors) filled with ``pattern``."""
    return [i // SECTOR for i in range(0, len(disk), SECTOR) if disk[i:i + SECTOR] == pattern * SECTOR]


@pytest.mark.parametrize("fat", [16, 32])
def test_hwc_covers_only_the_files_own_sectors(tmp_path, fat):
    disk = _image(tmp_path, fat)
    read = _reader(disk)
    data = _find(disk, b"\x5a")              # sectors of VSFILE and HWC (16 each)
    assert len(data) == 32
    covered = [lba for lba in data if ts.hwc_covers(read, lba)]
    assert len(covered) == 16                 # HWC's sectors, not VSFILE's
    assert not ts.hwc_covers(read, 0)
    assert not ts.hwc_covers(read, covered[-1] + 4096)


def test_no_hwc_no_write(tmp_path):
    disk = _image(tmp_path, 32, with_hwc=False)
    read = _reader(disk)
    assert not any(ts.hwc_covers(read, lba) for lba in _find(disk, b"\x5a"))


def test_garbage_volume_is_refused():
    assert not ts.hwc_covers(lambda lba, n: b"\0" * n, 0x340)
    def boom(lba, n):
        raise OSError(5, "io")
    assert not ts.hwc_covers(boom, 0x340)
