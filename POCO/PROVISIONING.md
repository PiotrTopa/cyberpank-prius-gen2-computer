# Provisioning — POCO F1 overlay as code

Every custom file on top of stock postmarketOS lives in this directory; `sync.sh`
makes the phone match the repo.

## Layout

```
POCO/
├── rootfs/                     # 1:1 mirror of the device filesystem
│   ├── usr/local/sbin/prius-*  #   scripts (mode 755)
│   ├── etc/prius/              #   sim-pin.tmpl only; flags are seeded, not synced
│   ├── etc/systemd/…           #   units, slices, drop-ins, watchdog conf
│   ├── etc/NetworkManager/conf.d/*                                # drop-ins
│   ├── etc/NetworkManager/system-connections/*.nmconnection.tmpl  # secrets templated
│   ├── etc/udev/rules.d/*, etc/nftables.d/*, etc/sysctl.d/*
│   └── etc/resolv.conf
├── dts/pmi8998-usb-vbus.dtso   # VBUS regulator node (ARCHITECTURE.md)
├── packages.txt                # apk packages added on top of stock
├── provision.sh                # runs ON the phone: install overlay + activate (idempotent)
├── sync.sh                     # runs on the dev host: render + rsync + provision
├── secrets.env.example         # template
└── secrets.env                 # real secrets — gitignored
```

## Setup

- Dev host: Linux or WSL2 with `rsync`, `ssh`, `scp`, `envsubst`
  (`sudo apt install rsync openssh-client gettext-base`). git-bash lacks
  rsync/envsubst; CRLF breaks `#!/bin/sh` (`.gitattributes` forces LF). Keep the clone
  on a Linux filesystem so git tracks the 755 mode bits.
- `cp secrets.env.example secrets.env` and fill it in (the example shows how to read
  the current values off the phone).

## Commands

```bash
cd POCO
./sync.sh                 # render templates, rsync rootfs, install + activate
./sync.sh --dry-run       # show what rsync would change
./sync.sh --packages      # also apk add packages.txt (needs network)
./sync.sh --app           # also rsync the app code (incl. powerbox/) to /home/user/cyberpunk
./sync.sh --usb           # also USB host + VBUS (REBOOTS the phone)
./sync.sh --core          # backend + power + udev only: no NetworkManager/resolv.conf,
                          # prius-wifi, prius-clock-fallback, prius-sim-unlock, display
                          # units; no secrets.env needed
./sync.sh --sudo-stdin    # target sudo asks for a password: read once from stdin
PHONE=user@<host> ./sync.sh    # target (default in sync.sh: user@10.200.0.5, see NETWORKING.md open gaps)
# bench POCO on home Wi-Fi with a sudo password:
dotinfra vault exec poco_user_password -- env PHONE=user@192.168.0.86 ./sync.sh --core --app --sudo-stdin
```

Steps: `sync.sh` copies `rootfs/` to a staging dir and renders every `*.tmpl` from
`secrets.env` (secrets travel only over SSH), rsyncs (`--delete`) to
`/tmp/prius-rootfs` on the phone, copies `provision.sh` + `packages.txt` and runs
`sudo sh provision.sh /tmp/prius-rootfs`. `provision.sh` installs each file with its
mode/owner, seeds missing flags in `/etc/prius/` (`power-mode:full wifi-mode:ap
usb-mode:host wifi-grace:20min netwatch-grace:20min`) and `/etc/prius/backend.env`,
reloads udev and nftables, `daemon-reload`s, enables the units (never
`prius-netwatch.timer`) and reloads NetworkManager connections. Re-running changes
nothing on a matching device. Flags and `backend.env` are never overwritten: change
modes with the `prius-*` tools.

## Rebuild from a clean flash

```bash
# 0. flash stock pmOS beryllium-tianma, get SSH (USB lifeline or Wi-Fi), set hostname
cd POCO
./sync.sh --packages      # packages + overlay + units
./sync.sh --usb           # dr_mode=host + VBUS node, reboot
```

After the reboot the phone sources 5 V VBUS as a USB host and is on the VPN.

## Secrets

`secrets.env`: WireGuard private key, server public key, endpoint and tunnel address
(`WG_ADDRESS`), SIM PIN, Wi-Fi PSKs (home / AP / dev). `.gitignore` covers
`secrets.env` and rendered `*.nmconnection`; the repo holds only `*.tmpl` with
`${PLACEHOLDER}` references.

## Gotchas

- `reboot &` over SSH gets SIGHUP and does not reboot: use `systemctl --no-block reboot`.
- A `linux-postmarketos-qcom-sdm845` update reinstalls a pristine DTB (no host role, no
  VBUS node): re-run `./sync.sh --usb`.
