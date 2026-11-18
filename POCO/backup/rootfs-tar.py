#!/usr/bin/env python3
"""rootfs-tar.py - stream a POSIX/pax tar of a mounted root filesystem to stdout.

Written for the POCO F1 car computer, whose busybox tar cannot store extended
attributes and where no package may be installed (the OS is pinned). Python's
tarfile is on the phone already.

  * one file system only (like tar --one-file-system), starting at ROOT
  * numeric owners (uid/gid; names are left empty), modes, mtimes, symlinks,
    hard links, device nodes, FIFOs; sockets are skipped
  * every extended attribute (security.capability, system.posix_acl_*, user.*,
    trusted.*) as a pax "SCHILY.xattr.<name>" record, the format GNU tar
    --xattrs writes and reads (restore: tar --xattrs --xattrs-include='*'
    --numeric-owner -xpf ...)
  * --exclude PATH (absolute, repeatable): the path itself is kept as an empty
    directory (for directories) or skipped (for files); every exclusion is
    listed on stderr at the end
  * files that vanish or change size while being read are reported on stderr
    (the archive is of a live system)

Usage: rootfs-tar.py [--root /] [--exclude /abs/path ...] > rootfs.tar
"""
import argparse
import os
import stat
import sys
import tarfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--exclude-glob-name", action="append", default=[],
                    help="skip files with this exact basename anywhere")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    rdev = os.lstat(root).st_dev
    excl = {os.path.normpath(p) for p in a.exclude}
    names = set(a.exclude_glob_name)
    stats = {"files": 0, "bytes": 0, "xattr": 0, "skipped_sockets": 0,
             "vanished": 0, "changed": 0, "mountpoints": []}
    excluded_hit = []

    out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.PAX_FORMAT,
                       encoding="utf-8", errors="surrogateescape")

    def arcname(path):
        rel = os.path.relpath(path, root)
        return "." if rel == "." else "./" + rel

    def add(path, contents=True):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            stats["vanished"] += 1
            print(f"rootfs-tar: vanished {path}", file=sys.stderr)
            return None
        if stat.S_ISSOCK(st.st_mode):
            stats["skipped_sockets"] += 1
            return st
        ti = out.gettarinfo(path, arcname(path))
        if ti is None:
            return st
        ti.uname = ""
        ti.gname = ""
        try:
            for xn in os.listxattr(path, follow_symlinks=False):
                xv = os.getxattr(path, xn, follow_symlinks=False)
                ti.pax_headers["SCHILY.xattr." + xn] = xv.decode("utf-8", "surrogateescape")
                stats["xattr"] += 1
        except OSError as e:
            if e.errno not in (95, 61):  # EOPNOTSUPP, ENODATA
                print(f"rootfs-tar: xattr {path}: {e}", file=sys.stderr)
        if ti.isreg() and contents:
            try:
                with open(path, "rb") as f:
                    size = os.fstat(f.fileno()).st_size
                    ti.size = size
                    data = _Exact(f, size)
                    out.addfile(ti, data)
                    if data.short:
                        stats["changed"] += 1
                        print(f"rootfs-tar: shrank while reading (zero-padded) {path}", file=sys.stderr)
                stats["bytes"] += size
            except FileNotFoundError:
                stats["vanished"] += 1
                print(f"rootfs-tar: vanished {path}", file=sys.stderr)
                return st
        else:
            if ti.isreg():
                ti.size = 0
            out.addfile(ti)
        stats["files"] += 1
        return st

    def walk(d):
        try:
            entries = sorted(os.listdir(d))
        except FileNotFoundError:
            return
        for n in entries:
            p = os.path.join(d, n)
            if p in excl or n in names:
                try:
                    st = os.lstat(p)
                except FileNotFoundError:
                    continue
                excluded_hit.append(p)
                if stat.S_ISDIR(st.st_mode) and p in excl:
                    add(p, contents=False)  # keep the empty directory
                continue
            st = add(p)
            if st is None or not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                continue
            if st.st_dev != rdev:
                stats["mountpoints"].append(p)  # keep the mount point dir, not its contents
                continue
            walk(p)

    add(root)
    walk(root)
    out.close()
    print(f"rootfs-tar: done files={stats['files']} bytes={stats['bytes']} xattrs={stats['xattr']} "
          f"sockets_skipped={stats['skipped_sockets']} vanished={stats['vanished']} changed={stats['changed']}",
          file=sys.stderr)
    print("rootfs-tar: mount points kept empty: " + " ".join(stats["mountpoints"]), file=sys.stderr)
    for p in excluded_hit:
        print(f"rootfs-tar: excluded {p}", file=sys.stderr)


class _Exact:
    """File wrapper returning exactly `size` bytes (zero-pads a file that shrank)."""

    def __init__(self, f, size):
        self.f, self.left, self.short = f, size, False

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        if n < 0 or n > self.left:
            n = self.left
        b = self.f.read(n)
        if len(b) < n:
            self.short = True
            b += b"\0" * (n - len(b))
        self.left -= len(b)
        return b


if __name__ == "__main__":
    main()
