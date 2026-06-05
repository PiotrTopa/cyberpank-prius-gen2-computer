# Provisioning — POCO F1 "infrastructure as code"

Goal: treat the phone like a container image. Every custom file and change on top of
the stock postmarketOS install lives in this repo, and a single sync script makes the
device **match the repo exactly**. You edit locally, run `sync.sh`, done.

## Layout

```
POCO/
├── rootfs/                     # 1:1 mirror of the device filesystem (the "image layers")
│   ├── usr/local/sbin/prius-*  #   helper scripts (mode 755)
│   ├── etc/prius/              #   (flags are seeded by provision, not synced — live state)
│   ├── etc/systemd/system/*    #   units + .path/.timer watchers
│   ├── etc/NetworkManager/conf.d/*           # drop-ins
│   ├── etc/NetworkManager/system-connections/*.nmconnection.tmpl  # secrets templated
│   └── etc/resolv.conf
├── dts/pmi8998-usb-vbus.dtso   # device-tree VBUS regulator node (see ARCHITECTURE.md)
├── packages.txt                # apk packages we add on top of stock (the "FROM + apk add")
├── provision.sh                # runs ON the phone: install overlay + activate (idempotent)
├── sync.sh                     # runs on the dev host: render + rsync + provision
├── secrets.env.example         # template for the secrets below
└── secrets.env                 # REAL secrets — gitignored, never committed
```

## One-time setup

1. **Use WSL2 (recommended).** `sync.sh` needs `rsync`, `ssh`, `scp`, `envsubst`. On
   Windows, git-bash usually lacks `rsync`/`envsubst`, and Windows CRLF line endings
   break `#!/bin/sh` shebangs. WSL2 gives a native Linux toolchain that matches the
   target, so there are no surprises. (`.gitattributes` already forces LF as a safety net.)
   - In WSL: `sudo apt install rsync openssh-client gettext-base`.
   - Clone/keep the repo on the Linux filesystem (`~/...`) rather than `/mnt/d/...` so
     git tracks the `755` mode bits on the scripts correctly.
2. `cp secrets.env.example secrets.env` and fill in the real values (or recover them
   from the phone — see the comment in `secrets.env.example`).

## Daily workflow

```bash
cd POCO
./sync.sh                 # render templates, rsync rootfs -> phone, install + activate
./sync.sh --dry-run       # preview what rsync would change (no writes)
./sync.sh --packages      # also `apk add` the package list (needs network)
./sync.sh --usb           # also apply USB host + VBUS  (REBOOTS the phone)
PHONE=user@10.200.0.5 ./sync.sh    # override target (default user@10.200.0.5)
```

What happens under the hood:
1. `sync.sh` copies `rootfs/` to a temp staging dir and renders every `*.tmpl` with the
   values from `secrets.env` (so secrets only ever travel over SSH, never into git).
2. It ensures `rsync` exists on the phone, then `rsync --delete` the staging tree to
   `/tmp/prius-rootfs` on the device.
3. It copies `provision.sh` + `packages.txt` over and runs
   `sudo sh provision.sh /tmp/prius-rootfs` which installs each file to its real path
   with the right mode/owner, seeds `/etc/prius/*` flags if missing, then
   `daemon-reload` + `enable` the units and `nmcli connection reload`.

`provision.sh` is idempotent — re-running changes nothing if the device already matches.

## Rebuild from a clean flash

```bash
# 0. flash stock pmOS beryllium-tianma, get SSH (USB lifeline or Wi-Fi), set hostname.
cd POCO
./sync.sh --packages      # install packages + overlay + units
./sync.sh --usb           # set dr_mode=host + add VBUS regulator, reboot
# after reboot the phone sources 5V VBUS as a USB host and is on the VPN.
```

## Secrets handled

`secrets.env` carries: WireGuard private key + server public key, SIM PIN, and the
Wi-Fi PSKs (home / AP / dev). These are **never** committed — `.gitignore` covers
`secrets.env` and any rendered `*.nmconnection`. The repo only holds `*.tmpl` files
with `${PLACEHOLDER}` references.

## Notes / gotchas

- `reboot &` over SSH gets SIGHUP and does **not** reboot — `provision.sh`/scripts use
  `systemctl --no-block reboot`.
- A kernel/`linux-postmarketos-qcom-sdm845` update reinstalls a pristine DTB, dropping
  `dr_mode=host` **and** the VBUS node → re-run `./sync.sh --usb`.
- Flag files in `/etc/prius/` are runtime state, so `provision.sh` only **seeds** them
  when missing; it never overwrites a live value. Change modes with the `prius-*` tools.
