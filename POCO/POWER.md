# Power Management — POCO F1 Prius Board Computer

## Principle: never suspend

The device is **always-on**. We do **not** use system suspend (`freeze`/`mem`), because it
would tear down the cellular modem, WireGuard, the SSH session, and the OTG serial listener.
Instead we keep the system awake and scale the **CPU** between two profiles:

| Profile | Meaning | What it does | `cpus online` |
|---------|---------|--------------|----------------|
| `low`  | ignition OFF / resting | offline Gold cluster (`cpu4-7`); Silver cluster `schedutil`, capped to 1.2 GHz | `0-3` |
| `full` | ignition ON / active   | all cores online; `schedutil`, max clocks on both clusters | `0-7` |

Across **both** profiles the modem, WireGuard, SSH, and OTG serial stay fully alive.
The GPU (`freedreno`) auto-idles to `cur=0` on its own (`simple_ondemand`) — we don't touch it.

## Hardware facts (SD845)

- 8 cores, 2 cpufreq policies:
  - `policy0` = `cpu0-3` **Silver** (300 MHz – 1.766 GHz)
  - `policy4` = `cpu4-7` **Gold** (0.825 – 2.65 GHz)
- Governors available: `ondemand userspace performance schedutil` — **no `powersave`**,
  so low mode is achieved by **offlining the Gold cluster + capping Silver**, not a governor.
- GPU `5000000.gpu` (freedreno) idles to `cur=0` via `simple_ondemand`.

## The mode flag

```
/etc/prius/power-mode      # contains exactly  "low"  or  "full"
```

- This is the **single source of truth** for the power profile.
- Default after install: `low` (resting / key-off).
- A systemd **path watcher** re-applies the profile within ~1–2 s of the file changing.
- A future "ignition" message only has to **write this flag** — nothing else.

## The script

```
/usr/local/sbin/prius-power      # POSIX sh, chmod 755
```

Usage:

```bash
sudo prius-power low       # write flag=low  + apply now
sudo prius-power full      # write flag=full + apply now
sudo prius-power apply     # apply whatever the flag currently says (used on boot)
sudo prius-power status    # print current cores/governors/freqs (no change)
```

- `low`  → `schedutil` on all policies, `policy0` max = `1228800`, offline `cpu4-7`.
- `full` → online `cpu4-7`, `schedutil`, each policy max = its `cpuinfo_max_freq`.

## systemd integration

| Unit | Type | Purpose |
|------|------|---------|
| `prius-power.service` | oneshot | runs `prius-power apply` (on boot + when triggered) |
| `prius-power.path` | path | watches `/etc/prius/power-mode`, triggers the service on change |

Both are **enabled**. So:
- on **boot**, the service applies whatever the flag says (survives reboot);
- on **flag change**, the path watcher re-applies automatically.

## How to switch power (the two equivalent ways)

```bash
# A) via the CLI (writes flag + applies immediately)
sudo prius-power full          # going to full performance (key on)
sudo prius-power low           # going to rest (key off)

# B) by writing the flag only (path watcher applies it in ~1-2 s)
echo full | sudo tee /etc/prius/power-mode
echo low  | sudo tee /etc/prius/power-mode
```

Use **B** from the future ignition/battery message handler — it just needs to write the file.

## Verified behaviour

- LOW: cores drop to `0-3`, `policy0` capped at 1.2 GHz, Gold cluster gone — while modem
  stayed `connected`, WireGuard alive, default route via `qmapmux0.0`, SSH responsive.
- Path watcher: writing `full` → cores auto-online to `0-7`; writing `low` → back to `0-3`.
- Survives reboot (service applies the flag on boot).

## Status / decisions

- **Current scope is final for now: CPU-only scaling.** Going further on `low` (I/O throttle,
  disabling subsystems, modem low-power, etc.) is **deferred** — we keep `low` as it is rather
  than risk destabilising a working system. Revisit only if real current draw turns out too high.
- An **intermediate** profile could be added later if needed, but is not implemented yet.
- Invariants that must hold in **every** profile: system **loggable over SSH**, **modem ON**,
  **USB ON**, OTG serial + WireGuard alive. Only "everything else" gets trimmed when we optimise.

## Notes / limitations

- **No current/mA measurement** (battery amputated, no coulomb counting per requirements).
  Savings are qualitative: 4 cores offline + clock cap vs 8 cores at full clock.
- Modem is deliberately kept `power state: on`; a modem low-power state would drop
  SSH-over-cellular.
- When you need to **work on the phone** (e.g. compiling), set `full` — `low` limits it to
  4× 1.2 GHz.
