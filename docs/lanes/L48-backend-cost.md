# L48-B: where the car backend's CPU goes, and what to cut

Lane L48 (whole-system optimisation of the perception stack on the car phone), sub-lane B: the car backend
(`prius-backend`, `python -m cyberpunk_computer.backend`). 2026-11-20, 07:45-08:40 CET. Branch `l48-opt` from main
8b38c6d. main and the deployed `l44-chris` 9804552 are identical under `cyberpunk_computer/{backend,io,state}`.

Sources of numbers, marked in every table:
**[phone]** measured on the phone (read-only: `/proc/<pid>/task/*/stat|status`, two snapshots 25.19 s apart,
2026-11-20 07:47 CET, CLK_TCK 100; the journal); **[laptop]** measured on the P52 with the code of this branch;
**[code]** read from the code, not measured.

## 1. Setting

* The backend runs in `prius.slice` with `AllowedCPUs=3` [phone: `systemctl cat prius.slice`, cgroup
  `cpuset.cpus.effective` = 3]. Every thread's last CPU was 3. cpu3 is a little Cortex-A55. The roadsense, system and
  user slices are kept off cpu3 (drop-ins `50-prius-cpus.conf`). So the backend does NOT sit on a big core, and
  `Nice=-10` hardly matters: there are no other user tasks on cpu3 to win against (only kernel threads and IRQs). It is
  harmless; no change proposed. The real constraint is the other way round: **the whole backend shares ONE little
  core**, so whatever one thread wastes, the others wait for (121 involuntary switches/s on the main thread).
* Bench state during the capture: no car, gateway and powerbox on USB, the panda on the CAN hub (`1-1.3.4`
  `bbaa:ddcc`, connected, 1 frame/s), the roadsense replay stack up, MFD Pi on USB.

## 2. Per-thread budget

[phone] 25.19 s window, % of one core (cpu3), switches per second. Sum = 59 % of a core, which matches L45's 60 % and
busybox top's 7 % of 8 cores.

| thread | CPU % of a core | vol. sw/s | invol. sw/s | what it is [code] |
|---|---:|---:|---:|---|
| `PandaCanReader` | **38.0** | **4531** | 33 | io/panda_can.py reader: bulk IN loop, decode, ingress fast path |
| `python` (main) | 9.3 | 60 | 121 | service.run(): 50 Hz engine loop, 25 tick functions |
| `roadsense-scene` | 5.2 | 32 | 56 | backend/roadsense_scene.py: WebSocket from chris-integrator 8090 -> MFD scene, 10 Hz |
| `thermal-sampler` | 1.5 | 7 | 30 | backend/thermal_sampler.py: max of the CPU/GPU/battery zones every 0.19 s (60 % of it kernel time) |
| `SerialReader` (2nd) | 1.5 | 15 | 90 | io/serial_io.py, one per USB-CDC link (gateway / powerbox; which is which: inferred, not checked) |
| `zmq-server` | 1.3 | 24 | 24 | backend/zmq_server.py: state PUB 1 Hz + events to the MFD, REP commands |
| `SerialReader` (1st) | 0.8 | 14 | 45 | the other serial link |
| `api-server` | 0.3 | 14 | 14 | uvicorn/FastAPI on 8080 |
| `position-feeder` | 0.3 | 13 | 7 | backend/position_feeder.py |
| `ZMQbg/IO/0` | 0.3 | 13 | 1 | libzmq I/O |
| `CommLogger-Writer` | 0.2 | 28 | 3 | io/comm_logger.py: trip file writer |
| `metrics-sink` | 0.2 | 3 | 3 | sqlite metrics |
| 12 other threads | < 0.3 together | | | avc-nav, sat-wifi, ble-transport, battery-health, sntp, SerialWriter x2, usb-serial-monitor, headunit-adb, ... |
| **total** | **59** | | | |

The main loop's 50 Hz steps [laptop, idle backend without devices, per-call thread CPU, 79 s]: whole main thread
2.3-3.2 % of a P52 core; the largest steps are `satellite_registry.tick` 107 us/call, `VirtualTwin.update` 99 us,
`alerts.tick` 87 us (already self-limited to its period), `_poco_power_tick` 41 us average (1 Hz sysfs + a store
dispatch), everything else 3-12 us per call. Nothing in the main loop is an order of magnitude on its own.

Other car-side processes on the phone [phone, 20 s]: `hu-gnss-bridge` 0.3 %, `prius-bled` < 0.1 %. Outside this
sub-lane but next door: `chris_integrator` **47 %** of a core and `roadsense_api` 4.9 % (both perception side).

## 3. The hot threads explained

### PandaCanReader: an empty-transfer spin (38 % on the bench)

`PandaCanSource._read_loop` calls `libusb_bulk_transfer(EP 0x81, 16384 B, timeout 20 ms)` in a tight loop and
sends the heartbeat / polls health between reads. The car's panda runs firmware `DEV-f120999e-DEBUG` (2022, CAN
packet v2). That firmware answers a bulk IN at once with an **empty transfer** when its CAN queue is empty, so the
20 ms timeout never applies and the loop runs as fast as USB answers: **4531 transfers/s** [phone: voluntary context
switches of the thread, one per synchronous transfer], each costing ~84 us of A55 time (38 % / 4531), for 1 frame/s
of payload. It is 64 % of the whole backend's CPU on the bench. (Openpilot's boardd paces its reads at 100 Hz for
the same reason.)

In the car the same loop also carries the bus [phone, journal of the drive 2026-11-19 15:39-15:45 CET]:
`Panda reader: 1683-1690 frames/s, busy 29-42 %, largest batch 1112, coalesced 475-6338 per minute, dispatched
100-120/s, deferred 325-592/s, deduped 482-750/s, unknown 280-296/s`. `busy` is only the time spent handling frames
(decode, filters, trip log, store); the spin comes on top. `largest batch 1112` and the coalescing mean the reader
fell up to 0.66 s behind the bus: cpu3 was saturated while driving.

Per frame in the car [code + laptop profile of the replayed car frames]: unpack, motion tap, ingress filters
(unknown-id cache, dedupe, dispatch cap), then for every CHANGED frame a dict + `RawMessage` + a trip-log entry
(`Queue.put` with a condition notify, which also woke the writer thread), and ~110/s store dispatches through the
rules engine. Laptop, 447 recorded frames/s replayed through the real path: reader 6.5 % + writer 1.2 % of a P52
core, i.e. ~170 us per frame including the share of dispatches [laptop]; scaled by ~5 for the A55 this matches the
phone's `busy`.

### main thread (9.3 %)

A fixed 50 Hz loop (`--tick-hz 50`) that calls 25 tick functions; most of them rate-limit themselves internally (1 Hz,
5 s), so the loop mostly pays the per-tick overhead of asking. `roadsense_power.tick` runs `systemctl is-active` and a
`GET /v1/health` on the main thread every 5 s [code]: cheap on average, but blocking (timeouts 15 s / 2 s).

### roadsense-scene (5.2 %)

Reads `ws://127.0.0.1:8090/ws?topics=road,lights,speed_limit,env,rules,fused,map,lead,lanes,view` and publishes
<= 10 scenes/s. In view mode (chris-integrator sends `view` per frame) the 5-15 kB `road` message is kept as raw text
and never decoded [code]. I tested dropping `road` from the subscription (a lean path with a fallback probe): the feed
thread used **0.93 % vs 0.94 %** of a P52 core with 15 kB roads and the same 0.91 % with 5 kB roads, identical scenes
[laptop, 30 s each, chris-like stream 14 frames/s]. The cost is the JSON decode of lead / lanes / view and the build,
not the road bytes. **Null for the backend; not committed.** (It would save chris-integrator one 15 kB send per
frame; not measured, belongs to the integrator sub-lane.) On the bench the journal shows the feed reconnecting every
5 s ("no message for 5s") at 07:40-07:41 CET while the stack restarted: harmless, logged.

### thermal-sampler (1.5 %)

Reads every CPU/GPU/battery zone (about 17 of the 23 thermal zones) every 0.19 s for a 10 s peak hold used by the fan
policy; 60 % of its time is kernel (tsens reads) [phone]. Deliberate (the 1 Hz read missed the hot peaks, see the code
comment); halving the rate or reading only `cpu*`/`gpu*` max zones would save ~1 % at most.

## 4. Ranked candidates

| # | candidate | mechanism | expected saving | risk to function | effort | status / proof |
|---|---|---|---|---|---|---|
| 1 | **Pace the panda reads** | after a read that returned less than a full transfer, wait out the rest of a 5 ms period (`BACKEND_PANDA_POLL_MS`, 0 = old loop); a full transfer (backlog) is read again at once | bench: 38 % -> ~2 % of cpu3 (4531 -> ~200 transfers/s; 84 us each) = **~36 % of a little core, ~60 % of the backend**. Car: the empty-read spin goes too and frames come ~8 per transfer instead of ~0.4, which frees cpu3 for the frame work that fell 0.66 s behind | +0..5 ms on a frame's way into the state (the state takes a fast id at most every 0.1 s anyway); wheel-speed timestamps of the motion tap quantised to the 5 ms read. The panda stays SILENT; nothing is sent on CAN. Panda queue: thousands of frames, ~8 per period at 1690/s | S | **committed**. Tests: 3 new (paced 20..100 reads in 0.4 s, unpaced > 1000, full transfers back to back). Laptop: 196 reads/s with an instant-empty fake handle (unpaced: unbounded). Phone proof: order L48-B |
| 2 | **Compact, batched CAN trip logging** | the reader logs every changed frame (~800/s in the car) as dict + RawMessage + entry dict + JSON line. Log one entry per bulk transfer (all frames of the 5 ms batch, compact `[t, id, hex, bus]` rows) and expand on replay | car: most of the ~170 us/frame log path for ~800 frames/s; estimate 10-20 % of cpu3 while driving [inferred from the laptop profile: log path ~1/3 of the reader's frame work, writer thread on top] | trip file format changes (replay and any analysis scripts must read the batched rows); no live behaviour changes | M | not done: needs the replay side and the owner's view on the file format. Prove with the can_bench replay (section 5) and a car drive |
| 3 | **Comm-logger queue without lock/notify** | `queue.Queue` -> `collections.deque`; the writer wakes every `flush_interval` (1 s) and writes everything in one go instead of being woken per entry | laptop, car frames x4 (1847/s): reader + writer 13.2 % -> 11.6 % of a P52 core (writer 3.2 -> 2.1 %); x1: 7.7 -> 7.1 %. **~12 %, not an order of magnitude.** On the phone: writer wake-ups ~800/s -> 1/s on the shared core | lines reach the disk up to 1 s late (before: every 100 entries or 1 s); a crash loses at most 1 s, as before at low rates | S | **committed**. 26 recorder / ingress tests pass |
| 4 | Move the frame work off cpu3 or give prius.slice two little cores | `AllowedCPUs=2-3` (keep the roadsense stack off both) or pin `PandaCanReader` alone to cpu2 | removes the saturation seen on 2026-11-19 (backlog 1112 frames); no CPU saved, but no frame work waits for the 50 Hz loop / scene feed | takes a little core from the perception side (envd runs `--cpus 0-3`) | S (unit drop-in) | decide after #1 is measured in the car: #1 may already remove the backlog |
| 5 | Store dispatch cost of broadcast CAN | ~110 dispatches/s, ~0.6 ms each on the phone (commit dcc7c6e) = ~7 % of cpu3 in the car; raise the dispatch cap from 0.1 s to 0.2 s for ids the MFD draws at 5 Hz, or make the reducers for the hot ids cheaper | up to ~3-4 % of cpu3 while driving | **visible**: energy / speed values on the MFD update at 5 Hz instead of 10 Hz (owner's call) | S (cap) / M (reducers) | not done |
| 6 | Engine loop 50 Hz -> 20 Hz, or event-driven | the loop mostly asks 25 self-rate-limited ticks "is it time?"; the gateway queue could wake it | main thread 9.3 % -> ~4-5 % [inferred: laptop main-thread cost is per tick] | gateway (serial) messages reach the state up to 50 ms instead of 20 ms later; powerbox heartbeat and watchdogs are time-based and unaffected [code], but every tick function would need a review | M | not done |
| 7 | Satellite registry / alerts at 10 Hz instead of 50 Hz | `satellite_registry.tick` (107 us/call laptop) every engine tick | ~1-2 % of cpu3 | satellite event latency +80 ms | S | not done |
| 8 | thermal sampler: fewer zones / slower | read only the zones the fan policy takes the max of, at 0.5 s | ~1 % | peak hold misses spikes shorter than 0.5 s | S | not done |
| - | Drop `road` from the scene subscription | see 3. | backend: none measured (0.94 -> 0.93 %) | none | S | **null, not committed** |

What nobody needs: the 4500 empty USB transfers a second (#1, gone). Everything else on the list is work somebody
consumes (trip log, MFD state, satellites, fan policy); the next big item, the per-frame trip log (#2), is needed but
can be done an order of magnitude cheaper.

## 5. What was implemented and the evidence

Branch `l48-opt` in `/home/piotr/projects/wt/prius-l48` (not pushed, not merged):

* `520d816` panda reader: pace bulk reads at 5 ms. Files: `cyberpunk_computer/io/panda_can.py`
  (`PandaCanConfig.poll_period_s`, `_read_loop`), `backend/service.py` (`panda_poll_s`), `backend/__main__.py`
  (`BACKEND_PANDA_POLL_MS`), `tests/test_panda_can.py` (+3 tests). `tests/test_panda_can.py` 22 passed.
* `7a11246` comm logger: `cyberpunk_computer/io/comm_logger.py` (deque + `threading.Event`; writer drains per
  `flush_interval`). Recorder / ingress tests pass.
* Whole test suite on the branch (pygame-dependent files left out): 1598 passed, 24 skipped, 6 failed. The same 6
  (`test_bean_rx` x3 and `test_bean_tx` x2: a capture fixture is missing; `test_vehicle_feeder` x1) fail on main
  8b38c6d without these changes.

Laptop tools used (scratch, not committed): a backend run with per-call thread-CPU accounting of every engine-loop
step; an instant-empty fake panda handle (paced vs unpaced reads); `can_bench`: 7.9 k CAN frames from the car's trip
file of 2026-11-19 15:37 CET (a 4 MB slice read from the phone) replayed in real time through PandaCanSource ->
IngressController -> Store -> TripRecorder, with per-thread CPU from /proc; a chris-like WebSocket stream for the
scene feed.

## 6. Not verified

* The phone numbers after the fixes: nothing was deployed (the phone was read-only for this lane). Order:
  `docs/lanes/L48-B-order.md`.
* That fw f120999e returns empty transfers at once is inferred from the 4531 synchronous transfers/s at 1 frame/s with
  a 20 ms timeout (and from boardd's pacing), not from a USB trace.
* The car-side effect of #1 (backlog, `busy`) and #3: needs a drive.
* Which `SerialReader` is the gateway and which the powerbox.
* Laptop-to-A55 factor (~5) used to translate laptop per-frame costs; the phone's own `busy` line agrees with it.
