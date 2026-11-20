# L44: the car computer on the chris layout (chris-integrator on top of roadsense)

Lane L44, 2026-11-20, laptop only. Branch `l44-chris` from `main` (748546b) with `l42-road-users` (8d35397, MFD road
users by class) merged first (fast-forward). Nothing was deployed: the phone (`user@10.200.0.14`) was only read
(configs, units), the Pi was not reachable without root's key. Integration lane L43 (roadsense `v2-l43`) ran in
parallel; every run here used a snapshot of it at **424ef8e** (`git archive`, clean tree at that commit).

## 0. Result in one page

**The layout this branch expects** (agreed with L43):

| | before | chris layout |
|---|---|---|
| consolidated API (scene feed, proxy, health, preview) | roadsense api, 8090 | **chris-integrator**, 8090 (same paths, topics, shapes; `/v2/` beside `/v1/`) |
| roadsense's own API | the consumer API | one camera's debug face, **8091**, loopback |
| vehicle input (position feeder) | `/run/roadsense/position.sock` (mapd) | **`/run/chris/position.sock`** (chris-integrator, alias of its `feed.sock`) |
| units | target + mapd | `chris-integrator.service` part of `roadsense.target` (drop-ins in this repo); `roadsense-mapd.service` disabled + masked |
| config / data | `/etc/roadsense`, `/var/lib/roadsense` | plus `/etc/chris`, `/var/lib/chris` |

**Done and working on the laptop:**

* Repoint: unit list, health logic (healthy = the API answers **and** `health.daemons.camerad` / `modeld` are
  connected; STARTING -> RUNNING and the auto-restart use it), proxy with `/v2/` and the consolidated topics, the
  feeder against `/run/chris` with `/run/roadsense` as fallback, POCO drop-ins, firewall guard, provisioning.
  Everything also works against the old roadsense (the phone runs it until the deploy; a rollback needs no backend
  change).
* Honesty (open since L37): the backend never shows a raw lead or raw lane lines while the integrator says the
  camera is not believable or the lead / lanes are not valid (also when those topics age out); scene keys `vis`
  and `lead_unknown`; no `lead_departed` from a lead that went invalid; alerts: "unknown" distinct from "no lead",
  no fallback while vision is not ok; the green-light helper does not take an unknown lead as a clear road. Screen:
  `CAMERA UNSURE` (steady, quieter amber) instead of an empty road, `LEAD ?` (steady, dim) for an unknown lead.
* End to end against the real chris-integrator (424ef8e): the feeder, scene feed, map box and status work
  (`tests/test_position_mapd_e2e.py`: 2 passed, 1 xfail = `reset` not handled by the integrator).
* Replay of the incident drive through the integrator and this backend: from 206 s on the glass shows `CAMERA
  UNSURE` (99.3 % of the driving) instead of a road picture from a camera pitched 16-17 degrees up (L41 section 5:
model speed 0.08-0.28 of the car's, phantom lead at 5 m). On the good drive the mark
  shows 6.1 % of the driving time, 14 times in 38 min: the integrator's false camera faults (L41), lengthened by the
  5 s hold (2.5 % with a 1 s hold).

**Not working, or not verified, stated plainly (section 7):** the integrator ignores `reset` (a finished synthetic
drive's place is not forgotten); `tests/test_alerts_mapd_e2e.py` fails against the integrator (the map layer never
became usable on the backend's synthetic drive in 25 s: `low_match_confidence` / `position_unknown`); the map box
state flickers good / unknown / dead_reckoning while synthetic fixes come (15 switches in 27 s); nothing ran on the
phone or the Pi.

## 1. What changed (files)

Backend:

| file | change |
|---|---|
| `cyberpunk_computer/backend/roadsense_power.py` | `DEFAULT_UNITS`: `chris-integrator.service` in, `roadsense-mapd.service` out. `stack_ok` = API answers and the required daemons (`camerad`, `modeld`; `RoadsenseConfig.required_daemons`) are connected; an API without a `daemons` table counts by its answer (old roadsense). STARTING waits for `stack_ok`; the RUNNING watchdog counts "unhealthy" (API silent or a daemon missing) towards `api_dead_restart_s`. `health.server`, `health.stack_ok`, `health.stack_missing` in the mirror |
| `cyberpunk_computer/backend/position_client.py` | `rundir` may list directories (`:`), tried in order; default `/run/chris:/run/roadsense`; `path` = the connected socket |
| `cyberpunk_computer/backend/position_feeder.py`, `__main__.py`, `service.py` | default rundir list; status `server` (who answers: `chris-integrator` / `roadsense-mapd`); docstrings: no reliance on error replies, `reset` ignored by the integrator; env `BACKEND_ROADSENSE_SCENE_INVALID_HOLD_S` |
| `cyberpunk_computer/api/roadsense_proxy.py` | UI rewrite and routes for `/v2/` (the integrator's status page polls `/v2/...`) incl. `/roadsense/v2/frame.jpg`; whitelist + `WS_TOPICS`: `lead`, `lanes`, `view`, `ego_state`, `tracks`, `calib`, `gnss`, `vehicle`, `roadgeom`, `governor`, `mixer`, `version`, `road_users` |
| `cyberpunk_computer/backend/roadsense_scene.py` | section 3; `lead.srcs` from `lead.sources[]`; fused `map.lead.valid`; `python -m cyberpunk_computer.backend.roadsense_scene` = a read-only bench check (prints what the MFD would get, one line a second) |
| `cyberpunk_computer/alerts/inputs.py`, `alerts/functions/stop_go.py` | `lead_unknown`, `vision_ok`, `Lead.sources`; `_scene_lead` returns unknown without fallbacks; compact log `lead: "unknown"`; green-light helper needs a known lead state |

Screen (Pi): `ui/scene/inputs.py` (`RoadScene.vis_ok`, `vis_why`, `lead_unknown`; nothing measured drawn while
`vis_ok` is false), `ui/scene/director.py` (state `doubt`, `lead_unknown`), `ui/scene/render.py` (chips `CAMERA
UNSURE`, `LEAD ?`; signature), `ui/scene/demo.py` (shots 95, 96; `--only`).

POCO (phone overlay): `rootfs/etc/systemd/system/roadsense.target.d/50-prius-chris.conf` (`Wants=chris-integrator.service`),
`rootfs/etc/systemd/system/chris-integrator.service.d/50-prius.conf` (`PartOf=roadsense.target`,
`Slice=roadsense.slice`, `CHRIS_API_HOST=127.0.0.1`, `CHRIS_API_PORT=8090`, `CHRIS_ROADSENSE_HTTP=http://127.0.0.1:8091`;
`/etc/chris/integrator.env` overrides), `rootfs/etc/nftables.d/50_prius_backend.nft` (8091 in the guard, comments),
`provision.sh` (backend.env template text and `BACKEND_POSITION_RUNDIR`; a block that runs only when
`/etc/systemd/system/chris-integrator.service` exists: disable + mask `roadsense-mapd.service`, `systemctl disable
chris-integrator.service`, `/etc/roadsense/api.env` port 8090 / 8081 -> 8091).

Tools and tests: `tools/mfd_chain_replay.py` (`--glass`, `--shots`, `--hold`, `--road-model`),
`tools/mfd_map_drive.py` (`record` runs chris-integrator; `--legacy` the old layout), `tests/test_position_mapd_e2e.py`
and `tests/test_alerts_mapd_e2e.py` (chris-integrator instead of mapd + api), new or changed tests in
`test_roadsense_power.py`, `test_roadsense_front.py`, `test_position_feeder.py`, `test_roadsense_scene.py`,
`test_alerts_framework.py`, `test_alerts_functions.py`, `test_mfd_scene.py`, `test_api_auth.py`.

Docs: `docs/MFD_MAIN_SCREEN.md` (states, scene keys, new section 7.6), `docs/POSITION_FEEDER.md`,
`docs/ARCHITECTURE.md`, `POCO/SCRIPTS_AND_FILES.md`, `POCO/README.md`, `README.md`. Dashboard: `MapTab.tsx` shows
`chris-integrator.service` as the "map unit" (not rebuilt; the old build shows `--` there).

## 2. Health and the auto-restart

chris-integrator answers `/v1/health` also while camerad and modeld are dead, so "the API answers" no longer means
the perception runs. `stack_ok` adds: `health.daemons.camerad.connected` and `health.daemons.modeld.connected`
(both keys exist in the integrator's table at 424ef8e, and in the old roadsense api 2). Consequences:

* STARTING -> RUNNING only when both are connected (else after `start_timeout_s`, 120 s, "degraded" as before);
* RUNNING: unhealthy for `api_dead_restart_s` (180 s) restarts the target (at most every 300 s). A dashcam
  re-enumeration (4-5 s, on the first drive after a long stand 122 s) stays below that; the counter starts over
  when both are back.
* On the bench with the camera hub off or no dashcam this means a restart of `roadsense.target` about every 5 min
  in mode `on`. `BACKEND_ROADSENSE_API_DEAD_RESTART_S=0` turns the auto-restart off for bench work.
* The phone's roadsense api 2 also lists `camerad`; on the bench right now (read 2026-11-20) it says
  `camerad.connected: false`: the new backend applies the same rule to the old stack after a rollback.

## 3. What the scene says when something is not known

Rules in `SceneBuilder.build` (details and table: docs/MFD_MAIN_SCREEN.md 7.6):

* `vis: {"ok": false, "why"}` when the frame's `view.integrity.valid` is false or a `lanes` message said
  `valid: false` within `invalid_hold_s` (5 s, env `BACKEND_ROADSENSE_SCENE_INVALID_HOLD_S`). Then no lines, no
  edges, no path, no lead (with `lead_unknown`), and in `vw` no lines, path, lead or road users.
* `lead_unknown: true` when `vis.ok` is false, the last `lead` said `valid: false` within the hold, or the `lead`
  topic (seen on this connection) aged out. `lead` is null, `lead_departed` never comes from an invalid lead.
* An aged-out `lanes` topic (seen on this connection) gives no lines, never the raw ones; a malformed `lanes`
  message alone is ignored. The raw `road.leads[0]` / `lane_lines` are used only by a roadsense that never sends
  `lead` / `lanes` on this connection (a reconnect starts over: a rollback to an old roadsense works).
* An old roadsense sends no `valid` and no `integrity`: no `vis`, no `lead_unknown`, everything as before
  (`test_old_roadsense_shapes_unchanged`, `test_old_roadsense_scene_has_no_marks`).
* The `ego_state` topic is not subscribed (5.4 kB per frame); `view.integrity` and `lanes.valid` carry the camera
  verdict.

`lead.source` is string-matched nowhere in this repository (checked); `lead.sources[]` goes into the scene as
`lead.srcs` and into the alert inputs as `Lead.sources`.

## 4. Screenshots

Headless (`SDL_VIDEODRIVER=dummy`), native 480x240 plus `@4x`:

| what | file |
|---|---|
| healthy lead (demo timeline) | `~/datasets/l44/shots/10_lead_far_signs.png`, `11_lead_close.png` |
| camera not believable | `~/datasets/l44/shots/95_camera_unsure.png` |
| lead unknown | `~/datasets/l44/shots/96_lead_unknown.png` |
| road users by class (L42) | `~/datasets/l44/shots/88_road_users_lead_truck.png` .. `94_road_users_unknown_lead_traffic.png` |
| healthy lead, replayed through chris-integrator (104128, 2 s) | `~/datasets/l44/replay/shots_104128/02_live_lead_t2.png` |
| incident, replayed (151146): before / from 206 s | `~/datasets/l44/replay/shots_151146/02_live_lead_t37.png` / `03_doubt_t206.png` |
| false camera fault on the good drive | `~/datasets/l44/replay/shots_104128/03_doubt_t523.png` .. `08_doubt_t1104.png` |

Shots 1-94 and the sign legend are byte-identical to L42's (`~/datasets/l42/shots`, 190 files compared). In the
replay shots the vehicle state (speed, gear, panels) is the demo's, the road scene is the replayed one.

## 5. Tests

* Backend, `.venv` (main checkout's): `pytest tests`: 1591 passed, 29 skipped, 5 failed, 2 collection errors, all
  known and unrelated (e2e modules skip without a chris tree). Known unrelated: 5
  `test_bean_*` (fixture untracked in the main checkout); `test_framebuffer_panel_lut.py`, `test_pointer_ui.py`
  do not collect there (no pygame in `.venv`; they pass in `.venv-fe`). `test_mfd_power.py` and
  `test_discovery.py` at the repo root are not under `tests/` and were not run.
* UI, `.venv-fe`: `tests/test_mfd_scene.py` 80 passed (3 new), plus `test_framebuffer_panel_lut.py`,
  `test_pointer_ui.py`, `test_mfd_pacing.py`, `test_mfd_alerts.py`.
* End to end against chris-integrator (`PRIUS_ROADSENSE_REPO=<L43 snapshot 424ef8e>`, Python of
  `~/projects/roadsense/.venv-models`): `test_position_mapd_e2e.py` 2 passed, 1 xfailed (reset);
  `test_alerts_mapd_e2e.py` **1 failed** (section 7). Both skip without such a tree (the default
  `~/projects/roadsense` is `main` without chris-integrator).
* `tools/mfd_map_drive.py record --demo opole-rural --max-s 60` against the integrator: 231 messages, map `good` in
  92 of 105, rendered (`~/datasets/l44/map_drive/opole_chris*.png`).
* Bench check against a local integrator on fake daemons: `python -m cyberpunk_computer.backend.roadsense_scene
  --port 18090` printed `vis ok`, the lead, lines; `/v1/health` `server: chris-integrator`, all daemons connected.

## 6. Replay (`tools/mfd_chain_replay.py --glass`, chris-integrator 424ef8e, this backend)

```
SP=<dir with the L43 snapshot>; FE=.venv-fe/bin/python (+ PYTHONPATH with numpy from ~/projects/roadsense/.venv-models)
$FE tools/mfd_chain_replay.py ~/datasets/roadsense-rec/20261119-151146 --roadsense $SP/rs-l43-424ef8e --glass --shots DIR
```

| | 151146 (incident) | 104128 (good) | 104128, hold 1 s |
|---|---|---|---|
| scenes / minutes | 11 457 / 23.1 | 18 317 / 38.2 | same |
| driving (>= 3 m/s): `CAMERA UNSURE` | **99.3 %** | 6.1 % | 2.5 % |
| driving: lead drawn | 0 % | 44.1 % | 45.6 % |
| `CAMERA UNSURE` on/off | 1 | 14 (0.37/min) | 14 |
| raw fallback scenes | 0 | 0 | 0 |
| `LEAD ?` (lead unknown with the camera fine) | 0 | 0 | 0 |

What the glass shows during the incident (151146): 0-37 s no lead, 37-198 s a lead (the car stands or creeps;
the integrator holds the camera fine there; distances not examined), then from 205.6 s to the end (1384 s) `CAMERA UNSURE`,
floor only, no lead, no lines. The integrator's reasons: own speed uncertainty 4-9x its characterised level, speed
disagreeing with vehicle and wheels (ratio 0.04-0.37: "looks like scale"), as L41 described. Logs:
`~/datasets/l44/replay/glass_151146.txt`, `glass_104128.txt`, `glass_104128_hold1.txt`.

## 7. What does not work, or is unverified

1. **`reset` is ignored by the integrator** (L39 section 4; still so at 424ef8e). The feeder sends it as before;
   the place of a finished synthetic drive is not forgotten (`test_session_end...`: xfail). The scene's
   `SyntheticTaint` clears on the first `unknown` map state; the integrator passes through `unknown` often (item 3),
   so a later `dead_reckoning` on the synthetic place could reach the MFD unmarked. Needs L43's port of `reset`.
2. **`test_alerts_mapd_e2e.py` fails against the integrator**: on the backend's synthetic drive (feeder synthetic
   source, no CAN) the fused map layer never became usable in 25 s (`low_match_confidence`, `position_unknown`);
   with mapd it passed. roadsense's own `synth_drive.py` (richer motion feed) does get `good` (92 of 105 map
   messages). Not diagnosed here (integrator side).
3. The map box state flickers while synthetic fixes come (15 good / not-good switches in 27 s; during the 15-20 s
   drop-out `unknown` rather than `dead_reckoning`). Measured, not asserted (L39 5.1-5.3, L41 section 3).
4. False `CAMERA UNSURE` on a healthy drive: 14 episodes in 38 min, 6.1 % of the driving with the 5 s hold (L41's
   false camera faults). The hold is env-tunable; the cause is upstream.
5. Error replies: the integrator sends none; the feeder only counts them, nothing relies on them.
6. **Unverified**: everything on the phone and the Pi (no deploy, no restart; the Pi needed root's key); the
   provisioning block (only `sh -n`); a unit that has only drop-ins while chris-integrator is not installed
   (systemd logs a refused start of `chris-integrator.service` when the target starts, the target still runs);
   `/v1/frame.jpg` forwarded to a real debug face (only the 503 without one was seen); the integrator's CPU on the
   phone; the dashboard was not rebuilt.

## 8. Deployment notes (bench car computer, tonight)

Assumes L43's final tree has: consolidated API with the 424ef8e paths and health table, `position.sock` alias,
`view.integrity`, `lead.valid`, `lanes.valid`, `chris-integrator/scripts/install.sh`, the debug face's
`/v2/frame.jpg`. If L43's own unit gains `PartOf=roadsense.target` / ports, the drop-ins here are harmless
duplicates; `/etc/chris/integrator.env` overrides the drop-in's ports.

What the phone has now (read 2026-11-20): old roadsense (api 2, `roadsense.target` wants mapd, `/etc/roadsense/api.env`
= `ROADSENSE_API_PORT=8090`, `50_roadsense.nft` present, no `/etc/chris`), backend older than L36/L42 (no
`route_active`, no road users), `BACKEND_POSITION_FEEDER=1`, no `BACKEND_ROADSENSE_UNITS`, no `BACKEND_POSITION_RUNDIR`.

**Order** (phone `P=user@10.200.0.14`; backend API on loopback needs no token):

0. Backup on the phone: `sudo tar czf ~/rollback-20261120-l44.tgz /etc/roadsense /etc/systemd/system/roadsense*
   /usr/local/lib/roadsense /usr/local/bin/{camerad,modeld,camerad-dvr,envd,laned} /etc/prius/backend.env
   /etc/nftables.d /home/user/cyberpunk/cyberpunk_computer`.
1. Stack down: `curl -s -X POST http://127.0.0.1:8080/api/v1/commands/roadsense_mode -H 'Content-Type:
   application/json' -d '{"mode":"off"}'`; wait until `systemctl is-active roadsense.target` is `inactive`.
2. roadsense (L43 tree): dev box `scripts/sync-phone.sh $P`; phone `cd ~/roadsense && sudo API_PORT=8091
   API_HOST=127.0.0.1 [CMAKE_ARGS as the last install] scripts/install-phone.sh` (debug face 8091 loopback; removes
   `50_roadsense.nft`); then `sudo API_PORT=8090 API_HOST=127.0.0.1 ROADSENSE_HTTP=http://127.0.0.1:8091
   chris-integrator/scripts/install.sh` (unit, `/usr/local/lib/chris`, `/etc/chris/integrator.env`). Do **not**
   enable `chris-integrator.service`. Config: `sudo cp /etc/roadsense/recorder.json /etc/chris/` (of L39's list
   only `recorder.json` exists on the phone; copy any other of `integrator lead lanes egolane view roadgeom
   lights`.json if present); map pack `sudo ln -sfn /var/lib/roadsense/maps/current.rsmap
   /var/lib/chris/maps/current.rsmap`. The signs provider (roadsense-signs `l42-road-users`) installs separately
   (L42 section 7).
3. This repo (after step 2, so that provisioning sees the installed unit): dev box `cd
   ~/projects/wt/prius-l44/POCO && PHONE=$P ./sync.sh --core --app` (`--core`: no secrets needed; the network
   layer is not touched). On the phone this installs the two drop-ins and the firewall file, reloads nftables
   (8091 now in the guard), and, because `chris-integrator.service` exists: disables + masks
   `roadsense-mapd.service`, disables `chris-integrator.service`, rewrites `ROADSENSE_API_PORT=8090|8081` to 8091
   in `/etc/roadsense/api.env` (already 8091 after step 2), `daemon-reload`. `--app` pushes `cyberpunk_computer/`
   to `/home/user/cyberpunk`, runs `prius-backend ensure-venv` and restarts `prius-backend.service`. (Full sync
   instead of `--core` is fine if secrets.env is at hand.) If step 3 ran before step 2: run `./sync.sh --core` again.
4. Env: nothing to change in `/etc/prius/backend.env` (defaults: unit list with chris-integrator, feeder rundir
   `/run/chris:/run/roadsense`, API port 8090). Optional for bench work: `BACKEND_ROADSENSE_API_DEAD_RESTART_S=0`.
5. MFD: on the phone `sudo prius-deploy-mfd` (syncs `/home/user/cyberpunk/cyberpunk_computer/` to the Pi, restarts
   `mfd.service`; the Pi must be powered: MFD manager or `set_relay`).

**Bring the stack up on the bench** (ACC off, no driving, the camera sees no road): mode `on`:
`curl -s -X POST http://127.0.0.1:8080/api/v1/commands/roadsense_mode -H 'Content-Type: application/json' -d
'{"mode":"on"}'` -> manager `camera_on` (relay ch4, 8 s settle) -> `starting` (`systemctl start roadsense.target`,
which now pulls in chris-integrator) -> `running` once the integrator answers and camerad + modeld are connected.
`auto` = follow ACC (with ACC off: grace 30 s, then stop); `off` = keep it down.

**Verify on the bench:**

* `systemctl is-active roadsense.target chris-integrator roadsense-api roadsense-camerad-dvr roadsense-modeld` all
  `active`; `systemctl is-enabled roadsense-mapd` = `masked`, `chris-integrator` = `disabled`;
  `systemctl show -p PartOf,Slice chris-integrator` = `roadsense.target`, `roadsense.slice`.
* `ss -ltnp | grep -E ':(8080|8081|8090|8091) '`: 8090 and 8091 on 127.0.0.1 (python), 8080/8081 the backend.
* `curl -s 127.0.0.1:8090/v1/health | python3 -c 'import json,sys; h=json.load(sys.stdin); print(h["api"],
  {k: v["connected"] for k, v in h["daemons"].items()})'` -> `server: chris-integrator`, camerad / modeld true.
* Through the backend: `curl -s 127.0.0.1:8080/api/v1/roadsense/health` (same body); `curl -s -o /tmp/f.jpg -w
  '%{http_code} %{size_download}\n' 127.0.0.1:8080/api/v1/roadsense/frame.jpg` -> 200 (integrator -> debug face);
  the status page `http://10.200.0.14:8080/roadsense/` (over WireGuard, logged in) shows the integrator's page with
  the preview.
* Feeder: `curl -s 127.0.0.1:8080/api/v1/position | python3 -c 'import json,sys; d=json.load(sys.stdin);
  print({k: d.get(k) for k in ("connected","socket","server","vehicle_api","errors")})'` -> `/run/chris/position.sock`,
  `chris-integrator`, `vehicle_api` true, errors 0 (with ACC off there is no CAN: `motion_sent` stays low).
* Scene: `cd /home/user/cyberpunk && .venv/bin/python -m cyberpunk_computer.backend.roadsense_scene --seconds 10`
  (read-only, one line a second). On the bench expect "not known" rather than a road: `vis NOT OK (...)` or
  `lead_unknown True` (no road in view, no vehicle speed with ACC off) are correct outputs; a raw lead with
  `vis NOT OK` would be a bug. The MFD road view itself opens only above 3 km/h, so the marks are not visible on
  the bench glass.
* Stop path: mode `auto` (ACC off) -> after the grace every listed unit incl. chris-integrator goes inactive
  (PartOf); `journalctl -u prius-backend -n 200 | grep roadsense` shows no `stop timeout`.
* Optional auto-restart: with mode `on`, `sudo systemctl stop roadsense-camerad-dvr` -> after 180 s the journal
  shows `roadsense: restart #1 (not connected: camerad for 180s)`.

**Pi (headless):** from the phone as root `ssh piotr@192.168.100.2`: `systemctl is-active mfd.service`;
`journalctl -u mfd -n 100 | grep -i traceback` empty; `cd ~/cyberpunk_computer && SDL_VIDEODRIVER=dummy
.venv/bin/python -m cyberpunk_computer.ui.scene.demo --shots /tmp/l44 --only camera_unsure,lead_unknown` (two
shots, about 10-20 s each on the Pi Zero), copy them back and compare with `~/datasets/l44/shots/95_*`, `96_*`
(same code and fonts: expected byte-identical; not checked). An old frontend with the new backend draws the empty
road without the mark (no crash: unknown keys are ignored); the new frontend with the old backend draws as before.

**Rollback:**

1. Mode `off`.
2. Phone: `sudo systemctl unmask roadsense-mapd.service`; `sudo rm -f /etc/systemd/system/chris-integrator.service
   /etc/systemd/system/roadsense.target.d/50-prius-chris.conf; sudo rm -rf
   /etc/systemd/system/chris-integrator.service.d` (remove the unit file: with it present the next `sync.sh` masks
   mapd again); restore roadsense: `sudo tar xzf ~/rollback-20261120-l44.tgz -C /` (or reinstall the old tree with
   `API_PORT=8090 scripts/install-phone.sh`); check `/etc/roadsense/api.env` says `ROADSENSE_API_PORT=8090`;
   `sudo systemctl daemon-reload`.
3. Backend and MFD: nothing to do. This backend works against the old stack (feeder falls back to
   `/run/roadsense/position.sock`, the unit list only stops mirroring mapd, the scene builder treats the old shapes
   as before, `stack_ok` reads the old api's `daemons` table). To go back anyway: restore
   `/home/user/cyberpunk/cyberpunk_computer` from the tarball, `sudo systemctl restart prius-backend`, `sudo
   prius-deploy-mfd`.
4. Mode `auto`.

## 9. Branch

`l44-chris`: 8d35397 (L42, fast-forward) and the L44 commit(s) on top; not pushed, not merged into `main`.
