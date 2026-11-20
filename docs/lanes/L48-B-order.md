# L48-B order: prove the backend fixes on the phone

Branch `l48-opt` (worktree `/home/piotr/projects/wt/prius-l48`). Two commits change the backend:

1. `520d816` `panda reader: pace bulk reads at 5 ms` (io/panda_can.py, env `BACKEND_PANDA_POLL_MS`, default 5; 0 = old behaviour).
2. `7a11246` `comm logger: deque queue, one write per flush interval` (io/comm_logger.py; trip recorder writer).

Report from which this order comes: `docs/lanes/L48-backend-cost.md`.

## Preconditions

* The phone is not held by another lane (L46 held it on 2026-11-20 night). The bench stack may run (it should: the
  baseline was taken with the replay stack up).
* The panda is on the bus (relay ch3 on): `cat /sys/bus/usb/devices/1-1.3.4/idProduct` = `ddcc`.
* Deploy the branch the usual way (`POCO/sync.sh --core --app`, CMDB `~/.infra/servers/poco.md`), then
  `sudo systemctl restart prius-backend`. Record the commit in the CMDB as usual.

## Measurement (identical before and after)

Run from the laptop; one ssh call; prints per-thread CPU (% of one core) and context switches per second over 30 s:

```sh
ssh -o ConnectTimeout=30 user@10.200.0.14 'P=$(systemctl show -p MainPID --value prius-backend)
snap(){ for t in /proc/$P/task/*; do echo "$(basename $t) $(cat $t/comm|tr " " _) $(cut -d" " -f14,15 $t/stat) $(grep -E "^(voluntary|nonvoluntary)_ctxt" $t/status|awk "{print \$2}"|tr "\n" " ")"; done|sort; }
snap>/tmp/a; sleep 30; snap>/tmp/b
join /tmp/a /tmp/b | awk "{u=\$8+\$9-\$3-\$4; printf \"%-16s %5.1f %%  vol %6.0f/s  invol %5.0f/s\n\", \$2, u/30, (\$10-\$5)/30, (\$11-\$6)/30}" | sort -k2 -nr | head -12
journalctl -u prius-backend -n 400 --no-pager | grep "Panda reader:" | tail -2'
```

(`/tmp/a`, `/tmp/b` are in the user's tmpfs; delete them afterwards.) CLK_TCK is 100 on the phone, so ticks/30 =
percent of a core.

Baseline (measured 2026-11-20 07:47 CET, branch `l44-chris` 9804552, bench, replay stack up, panda on the bus at
1 frame/s): whole backend 59 % of cpu3; `PandaCanReader` 38.0 % with 4530 voluntary switches/s.

## Pass / fail

Bench (no car), after commit 1:

| check | pass | fail |
|---|---|---|
| `PandaCanReader` CPU | <= 4 % of a core | > 8 % |
| `PandaCanReader` voluntary switches | 150..260 /s (one per 5 ms pace + heartbeat) | > 1000 /s |
| whole backend (sum of threads) | <= 25 % of a core | > 35 % |
| panda link | `Panda connected` in the journal, `panda_connected` true in `/api/v1/state` | link lost, `usb_errors` rising |
| frames still arrive | `Panda reader:` line keeps reporting the bench frames (1 frame/s on the bench) | 0 frames/s where it was 1 |

In the car (first drive with the branch; key on, engine running, 10 min): the `Panda reader:` journal line once a
minute is the measurement.

| check | pass | fail |
|---|---|---|
| frames/s | ~1690 as on 2026-11-19 (1683-1690) | lower: frames lost |
| `largest batch` | < 200 (2026-11-19: 1112, the reader fell 0.66 s behind) | >= 1112 |
| `coalesced` per minute | near 0 (2026-11-19: 475-6338) | not lower than 2026-11-19 |
| `busy` | lower than 29-42 % (2026-11-19) | higher |
| MFD energy / speed values | update as before (state rate cap 0.1 s unchanged) | visible lag |
| `PandaCanReader` CPU (the ssh block above) | at least 30 % of a core below a same-length capture with `BACKEND_PANDA_POLL_MS=0` | no difference |

The A/B in the car: `BACKEND_PANDA_POLL_MS=0` in `/etc/prius/backend.env` + restart = the old loop; remove it for
the new one. Only with the owner's agreement and with the car parked or driven by the owner (it changes no CAN
behaviour: the panda stays in SAFETY_SILENT, nothing is transmitted either way).

Commit 2 (comm logger) is visible only with traffic, i.e. in the car: `CommLogger-Writ` voluntary switches drop
from about one per logged frame (hundreds/s) to ~1/s; trip files keep growing (`ls -la
/home/user/cyberpunk/logs/trips | tail -2` twice, 60 s apart), and a copy of the new trip file replays on the laptop
with the backend's `--replay <file>` as before (the line format is unchanged; lines are only written once a second
instead of every 100 entries).

## Rollback

`BACKEND_PANDA_POLL_MS=0` restores the old reader without a redeploy; otherwise redeploy `l44-chris`.
