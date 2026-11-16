# Battery health: HV pack and 12 V battery over months and years

Code: `cyberpunk_computer/battery/` (standard library only; the same code runs in the backend on the
phone and offline on a laptop). UI: dashboard tab **Battery**. Tests: `tests/test_battery_health.py`,
`dashboard/tests/battery.test.ts`. State of this document: 2026-11-15, not deployed.

The HV pack is NiMH, 28 modules of 6 cells, read by the battery ECU as 14 blocks of two modules
(12 cells, about 16 V). Sign convention everywhere: battery current > 0 = discharge.

## 1. Data sources

Checked on the recording of the 2026-11-13 21:48 drive (19 min, 2.4 km, battery 7–10 °C) and on the
backend's metrics database (2026-11-04 … 11-15).

| Quantity | Source | Passive / poll | Rate seen | Resolution | Note |
|---|---|---|---|---|---|
| pack current, pack voltage | CAN 0x03B (panda) | passive | 116 frames/s after the ingress dedupe (bus: ~125/s) | 0.1 A, 1 V | voltage in whole volts limits the pack-resistance estimate |
| SOC, charge / discharge current limit, 2 battery temperatures | CAN 0x3CB (panda) | passive | 3.2/s after dedupe | 0.5 %, 1 A, 1 °C | limits read 70–81 A at 7–10 °C |
| READY flag | CAN 0x529 | passive | 1/s | | |
| vehicle speed | CAN 0x0B4 | passive | 33/s (every 16th used) | 0.01 km/h | |
| SOC, current, 14 block voltages | `21 CE` to 0x7E3 | poll, existing (2 s) | 0.48/s | 0.5 %, 0.01 A, 0.01 V | the 21CE current matches 0x03B at zero lag (mean abs. difference 1.5 A) |
| intake air temp, fan motor voltage, 12 V at the battery ECU | `21 CF` first frame | poll, existing (2 s; 10 s since 2026-11-17) | 0.49/s | 0.01 °C, 0.2 V | see §7: only the first frame arrived |
| battery temperatures 1–3, limits in kW, delta SOC, fan step | `21 CF` further frames | poll, existing | **not received** | | fixed in this branch (ISO-TP on the subscription), unverified on the car |
| ECU block resistances, accumulated-time counters | `21 D0` | poll, existing (5 s) | 0.19/s | 1 mΩ, 1 s | all 14 blocks read 19 mΩ on 2026-11-13; counters 0 |
| 12 V at the ECM | OBD `01 42` | poll, existing (5 s) | 0.2/s | 1 mV | only while IG-ON |
| ambient temperature | OBD `01 46` | poll, existing (10 s) | 0.1/s | 1 °C | |
| MG1 / MG2 inverter temperature | `21 C3` | poll, existing (1 s) | 0.9/s | 1 °C | maximum per ride only |
| 12 V at the computer feed, computer current, mAh counter, cabin temperature | powerbox INA219 + BMP280 | passive (USB) | 0.9/s driving, about 1/s parked | 4 mV, 0.1 mA | available around the clock |
| ignition (ACC) | powerbox | passive | on change | | |

No poll is added. The module taps the ingress message stream (exactly the messages the trip recorder
writes), so with the `battery_cells` group switched off on the MFD Data Sources screen the block
metrics are simply absent and rides are graded C.

Not collected today, and what it would take:

| Missing | Effect | Needed |
|---|---|---|
| HV data while the car is off | no pack voltage during a rest; self-discharge only as a difference between rides | nothing reasonable (the ECUs sleep) |
| car quiescent current on 12 V | only the computer's own drain is measured | a shunt at the 12 V battery |
| 12 V battery temperature | rested voltage normalised with the cabin sensor | a sensor at the battery (boot) |
| per-module voltage | a weak module is seen only as its block | not available from the ECU |
| odd and even blocks sampled at the same instant | see §7 | — |

## 2. Collection

Unit: the **ride** = one period with the battery ECU on the bus (IG-ON … IG-OFF); it opens on the
first battery frame and closes after 20 s without one. The **rest** is the time between two rides.
A ride is the natural unit here: every ride has one key-on state, one temperature, a set of current
steps, and is followed by one rest. Trends are computed over rides; the heat map also groups by week.

Path (`service.py`): ingress message-log callback → `tap` (a dict lookup and a `deque.append` on the
ingress threads, no decoding) → worker thread `battery-health` (nice +10) drains every 0.5 s into
`FrameDecoder` → `Collector`. A closed ride is analysed and stored; the summary the UI reads is
rebuilt after each ride.

Raw record of a ride (`collector.py`, encoded by `model.encode_record`: delta-coded int16/int32
columns, zlib):

| Table | Row | Columns |
|---|---|---|
| `blk` | every 21CE answer | t (0.1 s), SOC, current (0.01 A), 14 block voltages (0.01 V): kept in full, this is the health data |
| `sec` | every second | mean / min / max current (0.1 A), mean voltage (0.1 V) from 0x03B |
| `slow` | every 10 s | SOC, T1, T2, current limits, speed, intake, fan, TB1–3, limits kW, delta SOC, 12 V (ECM, battery ECU, powerbox), computer current, ambient, cabin, READY, ICE running, inverter temps |
| `wake` | powerbox sample | 12 V and computer current from 30 s before to 90 s after the ride start |
| meta | once | Ah and Wh charged / discharged, ∫I²dt, time per current bin, time within 10 % of the current limit, peaks, sums for the pack-resistance pairs (full 0x03B rate), 21D0 means and counters, distance, READY time |

Raw record of a rest: one row per minute with mean and minimum voltage, mean current, mAh counter,
temperature, ACC.

Features (`analysis.py`) are derived from the raw record only and carry `ANALYSIS_VERSION`; at
start-up records analysed by an older version are re-analysed. The raw records are never rewritten.

## 3. Storage

SQLite `battery.db` next to the metrics database (`/home/user/cyberpunk/data/battery.db`;
`BACKEND_BATTERY_DB` overrides; `BACKEND_BATTERY=0` switches the module off). WAL journal,
`synchronous=NORMAL`. Tables: `rides`, `rests` (time span, source, grade, features JSON, raw BLOB),
`cache` (the summary), `meta` (`schema_version`). A database with a newer schema than the code is
refused; migrations go in `BatteryStore._migrate`. Nothing is pruned.

| Item | Size |
|---|---|
| ride raw record | 15–17 kB for the real 19 min ride: about 50–55 kB per hour of driving |
| ride features | 3.6–4.1 kB per ride |
| rest record | about 4 bytes per minute: 6 kB per day parked |
| per year at 1 h and 2 rides a day | about 25 MB (synthetic 300 days, 535 rides, 142 h: 11.8 MB) |

Restart and power loss: the open ride and the open rest are written to the database every 60 s
(`closed` = 0). After a crash or power cut at most the last minute is lost; at start-up an open ride
is closed as it stands with the flag `truncated`, the open rest is continued. A backend restart in
the middle of a drive therefore gives two rides. Time inside a ride runs on CLOCK_BOOTTIME anchored
to the wall clock while no ride is open, so an NTP step cannot fall inside a ride; a ride stamped
before 2026 is marked `clock_suspect`.

Sources and precedence when time spans overlap: `live` > `trip` (backfill from a trip recording) >
`metrics1m` (backfill from the metrics database). Backfills are idempotent.

## 4. Metrics per ride

Thresholds of the regimes are in `model.py`.

**Balance.** deviation of a block = block voltage − median of the 14 at the same sample.
* `bal.rest / dis / chg`: mean deviation per block and mean / 95th-percentile spread (max − min) over
  the samples with |I| ≤ 3 A, I ≥ 15 A, I ≤ −15 A.
* `bal.fit`: per block a least-squares fit deviation = a + k·I (+ c·(SOC − mean) when the SOC moved
  8 % or more) over all samples of the ride (needs 60 samples and a current span of 20 A).
  `dev0_mv` = a (offset at zero current: charge imbalance), `dr_mohm` = −k (resistance above the
  median block), `dev_ref_mv` = a − 50 A·dr (deviation at 50 A discharge). The weakest block is the
  lowest `dev_ref_mv`; the long-run views use it normalised to 25 °C (`dev25_mv`).
* `sag_first_frac`: share of the discharge samples in which each block was the lowest.

**Resistance.**
* `r.blk_mohm` (per block), `sum_mohm`, `med_mohm`: through-origin slope −ΔV/ΔI over pairs of
  consecutive 21CE samples at most 3.5 s apart with |ΔI| ≥ 8 A (at least 15 such steps for the
  trends). A 2 s value: ohmic resistance plus the fast part of the polarisation. `blk_se` is the
  standard error of the slope.
* `r.rel` = block / median block; `max_rel` is the spread figure (a ratio: temperature cancels).
* `r.pack_fast_mohm`, `pack_1s_mohm`: the same slope on the 0x03B stream over 0.25 s and 1 s, pairs
  on a fixed grid with |ΔI| ≥ 15 A. Coarse (1 V steps); kept as a cross-check.
* `ecu.r_mohm`: the ECU's own figures (21D0), stored unchanged.
* Temperature: R(T) = R25·exp(B·(1/T − 1/298.15 K)). B starts at a prior of 2260 K (own estimate:
  resistance doubles from 25 °C to 0 °C) and is replaced by the value learnt from this pack: the
  median of d ln R / d(1/T) over pairs of rides at most 45 days apart and at least 3 K apart,
  shrunk towards the prior while there are few pairs. Rides with a mean battery temperature below
  0 °C are flagged `cold` and kept out of the resistance trend. SOC: the Prius stays within about
  45–70 %, where the resistance is flat; the ride's mean SOC is stored, not corrected for.

**Capacity proxies.** The car holds the SOC in a narrow window and the SOC is the ECU's own estimate
(mostly its ampere-hour count), so capacity is not measurable. What is stored:
* `soc.pct_per_ah`: slope of SOC against integrated ampere-hours within the ride (needs a 4 % swing
  and 0.25 Ah); nominal 100/6.5 = 15.4 %/Ah. It tells which capacity the ECU assumes, and rises when
  the ECU corrects the SOC faster than the ampere-hours explain.
* `soc.jumps`: SOC changes of 1.5 % or more within 20 s that the ampere-hours do not explain.
* `soc.frac_low / frac_high`: share of the ride at SOC ≤ 42 % / ≥ 78 %; `half_cycles_10`: SOC
  reversals of 10 % or more.

**Rest behaviour** (`pair_rest`, between two consecutive rides): change of the mean block voltage and
of the SOC from the key-off snapshot to the next key-on snapshot (|I| ≤ 5 A within 20 s of the ride
end / start, IR-corrected), and per block the change of `dev0_mv`.

**Thermal.** start / end / min / max / mean battery temperature (TB1–3 from 21CF when present, else
the two 0x3CB sensors), largest gradient between sensors, intake temperature and battery − intake,
fan motor voltage and on-fraction, temperature rise per Ah throughput (rides with ≥ 2 Ah), highest
inverter temperature.

**Stress.** Ah and Wh in and out, peak and RMS current, peak power, time per current bin (edges
−80, −40, −20, −5, 5, 20, 40, 80 A), time within 10 % of the ECU's current limit (0x3CB), lowest
limits of the ride.

**12 V in a ride.** median ECM voltage in READY (OBD 0142), the same at the powerbox and their
difference, lowest powerbox voltage from 10 s before to 30 s after the first HV frame, pre-wake level.

**12 V in a rest.** voltage 0.5, 1, 2, 6, 12, 24, 48 h after key-off (5-minute means, ACC off),
lowest one-minute minimum, minutes below 11.8 V and 11.5 V, mean computer current and mAh drawn,
temperature, slope in mV/h after the first 2 h, and `cap_proxy_ah` = Ah drawn per volt lost × 1.15 V
(a rough capacity figure: on the real data it scatters between 8 and 27 Ah, so it is shown as a
trend line only).

## 5. Quality grades

| Grade | Rule | Use |
|---|---|---|
| A | ≥ 5 min, ≥ 60 block samples, ≥ 15 current steps, no flag | all trends |
| B | as A with a flag: `cold`, `block_poll_gappy` (< 0.25 Hz), `no_pack_stream`, `no_temperature`, `truncated`, `low_current_span` | trends, except resistance when cold |
| C | shorter than 5 min, or too few block samples or steps | usage totals only |
| X | under 1 min or no battery data | ignored |
| L | backfilled from the 1-minute metrics | temperature, SOC range, 12 V; no block data |

`21cf_first_frame_only` / `no_21cf` are recorded but do not lower the grade.

## 6. Long-run indicators and trends

`summary.py` builds one JSON document (indicators, trends, heat maps, lists) after each ride.
Trend of a metric (`trends.py`): values are first normalised for their covariate (temperature) with
a coefficient learnt from time-local pairs; then the Theil–Sen slope over time with a 95 % interval
and the Mann–Kendall p-value; Pettitt's test marks a change point. No slope is given before 8 points
(6 for the 12 V metrics) over 21 days. More than 300 points are reduced to time-bin medians.

A level is `watch` / `act` when the value crosses the limit, and `watch` when a significant
worsening slope is at least `trend_min` per 30 days. All limits below are **own estimates** unless a
source is named; they are meant to be re-set once this pack has a year of history.

| Indicator | Value | Watch / act | Tells | Does not tell |
|---|---|---|---|---|
| Weakest block under load | lowest `dev25_mv`, median of 5 rides; the block most often weakest in the last 20 | −200 / −300 mV | a module pair that sags under load | which of the two modules; tens of mV are normal |
| Block resistance spread | highest block resistance above the median | 20 / 35 % | one block ageing faster | absolute resistance; a corroded sense wire or bus bar looks the same |
| Pack resistance at 25 °C | Σ block resistances, normalised; level = rise over this pack's first rides | +25 / +50 % | ageing of the whole pack | includes polarisation, so not comparable with the ECU's figure; a few % of seasonal residue remain |
| SOC movement per Ah | `pct_per_ah` | 20 / 26 %/Ah | the ECU's SOC swinging faster than the charge explains | capacity |
| Block balance at rest | spread of `dev0_mv` | 200 / 300 mV | charge imbalance between blocks | a single weak cell inside a 12-cell block |
| Charge imbalance drift | slope of each block's `dev0_mv` over 90 days, fastest-falling block | −15 / −40 mV/day | a block losing charge against the others | parked loss against loss while driving; the pack's absolute self-discharge |
| Battery temperature | highest temperature of a ride, median of 5 | 45 / 55 °C | heat stress, cooling faults | module hot spots |
| 12 V rested voltage | powerbox voltage 6 h after key-off + offset to the ECM | 12.2 / 11.9 V | a battery that does not hold its charge | not open-circuit (0.1–0.4 A load); capacity only weakly |
| 12 V lowest voltage, 14 days | lowest one-minute minimum between rides | 11.8 / 11.5 V | how close to a no-start | a brief dip under load counts like a slow sag |
| 12 V charge voltage in READY | median OBD 0142 | 13.5 / 13.0 V | DC-DC output | voltage at the battery posts |
| Parked drain of the computer | mean powerbox current while parked | none | Ah per day taken by the computer | the car's own quiescent current |

Sources of the limits: the 0.3 V block-difference figure for DTC P0A80 / P3011–P3024 is reported by
owners and independent workshops (PriusChat threads on P0A80; automotivetechinfo.com "Diagnosing
Prius Hybrid Battery Pack"); other reports give 1.0–1.3 V; no Toyota manual excerpt with the number
was found, so 0.2 / 0.3 V is used as a conservative scale, not as the ECU's criterion. 12 V: Toyota
service bulletins expect 12.8 V on a charged battery after the surface charge is removed (NHTSA
T-SB copies); 12.2 V / 11.9 V are general lead-acid open-circuit figures for about 50 % / 25 %.

Before the long-run slope exists (under 3 weeks), "Charge imbalance drift" falls back to the median
ride-to-ride change over rests of 8 h or more and is labelled provisional: that figure also carries a
temperature-dependent artefact of a high-resistance block (cold start after a long rest).

## 7. Findings on the recordings so far

One ride with block data (2026-11-13) and 9 days of 12 V data: single measurements, no trends.

* Block resistance (2 s steps, 92 steps, 8.4 °C): median 31.1 mΩ, range 28.2 (block 10) – 33.5
  (block 12); block 12 is 7.6 % above the median, standard error 0.5–0.9 mΩ per block. Sum 436 mΩ
  at 8 °C, about 280 mΩ at 25 °C with the prior coefficient. The ECU's own figure is 19 mΩ for every
  block (21D0), i.e. it does not resolve any difference.
* Balance: offsets at zero current within −24 … +49 mV (spread 73 mV); at 50 A and 25 °C block 12 is
  the lowest (−45 mV), block 10 the highest (+78 mV). Nothing near the 0.2–0.3 V scale.
* SOC 50–57.5 %, 16.3 %/Ah (the ECU's nominal scale within 6 %), no jumps. Limits 70–81 A.
* **Odd and even blocks are not sampled together**: under changing current the deviations alternate
  in sign between neighbouring blocks (key-on snapshot: −7, +50, −44, +35, −43, +24, …), and the
  instantaneous max − min reached 0.66 V in the ride while the fitted spread is 0.07 V. The MFD's and
  the metrics database's `delta_v` (max − min of one sample) therefore overstates the imbalance
  under load; the per-ride fit and the step regression average this out.
* 21CF: every answer was the first frame only (`10 12 61 CF …`, 550 of 550). Intake temperature, fan
  motor voltage and the battery ECU's 12 V reading decode from it; temperatures 1–3, limits in kW,
  delta SOC and fan step never arrived, and the backend's own decoder dropped the whole message
  (`delta_soc`, fan speed and intake temperature in the state stayed empty).
* 12 V: 13.95–13.97 V in READY. Parked, the computer draws 103–126 mA (2.5–3 Ah per day); 6 h after
  key-off 12.07–12.60 V at the powerbox, 24 h after 11.79–12.36 V. Lowest values per rest: 10.59 V
  (2026-11-04), 11.28–11.39 V (11-06, 11-08, 11-10), 11.78 V (11-13 … 11-15). The powerbox reads
  0.11 V below the ECM in READY. Bench work on the powerbox and relays took place in those days, so
  the lowest values are not all battery behaviour; from the numbers alone, a 2-day park takes the
  battery to about 12.1 V and below 11.8 V under the computer's load peaks.
* Metrics database: `hv_current` / `hv_voltage` keep their last value after IG-OFF (4957 minutes
  with a value, 318 with a changing one); the backfill takes only minutes in which the current
  varied as ride time.

## 8. Cost on the phone

Measured on the laptop on the real drive (570 000 messages in 986 s), scaled by 9 (a Python loop
takes 9 times longer on the phone in its parked power profile):

| Step | Where | Cost on the phone (estimate) |
|---|---|---|
| tap filter | ingress threads | 3 µs per message, 0.2 % of a core at 580 messages/s |
| decode + collect | worker, nice +10 | 0.6 % of a core while driving |
| spool | worker, every 60 s | one 15 kB write |
| ride analysis | worker, after IG-OFF | 0.2 s |
| summary | worker, after a ride / on `POST /battery/recompute` | under 0.1 s now; about 8 s with 535 rides |
| memory | | the open ride (about 1 MB of Python lists per hour) + a queue of at most 200 000 messages if the worker stalls |

New dependencies: none.

## 9. API and offline tooling

`GET /api/v1/battery/summary`, `/rides/{id}`, `/rests/{id}`, `/export/{rides|blocks|rests}.csv`,
`/export/ride/{id}.csv`, `/export/battery.db`, `POST /recompute`: same authentication as the rest
of the API; 503 while the module is not running.

    python -m cyberpunk_computer.battery backfill  --db battery.db --metrics metrics.db --trips logs/trips
    python -m cyberpunk_computer.battery summary   --db battery.db [--json summary.json]
    python -m cyberpunk_computer.battery export    --db battery.db --out DIR
    python -m cyberpunk_computer.battery ride      --db battery.db ID [--csv ride.csv]
    python -m cyberpunk_computer.battery reanalyse --db battery.db [--force]
    python -m cyberpunk_computer.battery synth     --db demo.db --days 300 --weak-block 9 --leaky-block 4
    python -m cyberpunk_computer.battery serve     --db battery.db --static dashboard/dist

On a laptop: download `battery.db` from the Data & export view and run the commands on it; the
raw block series of every ride is in the database (`store.record`, `model.decode_record`).
`synth` writes a simulated history (first-order equivalent circuit per block; not a validated
battery model) and refuses an existing file.

## 10. Reading the UI

* **Overview**: the indicators for the HV pack and the 12 V battery with level, value, a sparkline
  and the slope per 30 days with its interval; selecting one shows its series (grey dots = as
  measured, line = temperature-normalised), the limits, a change point if found, and what the
  figure can and cannot tell. Usage totals and time per current bin.
* **Blocks**: bars of the latest graded ride and a heat map of 14 blocks against weeks (or the last
  60 rides), for the deviation under load, at zero current, or the resistance. Red = worse than the
  median block. A failing module is one row turning red under load, neutral at zero current, week
  after week. Weakest-block history, per-block table with own and ECU resistance and slopes.
* **Rides**: list with grade; a ride opens with its current, SOC, temperatures, the block
  deviations over the ride and the weakest / strongest block against current.
* **12 V**: indicators, trend of the rested voltage and others, list of rests with the voltage at
  fixed times after key-off; a rest opens with its voltage and current.
* **Data & export**: grades, what the rides contain, flags, storage, CSV and database download,
  the limits with their origin.

The tab shows statistics only; nothing on it is live.

## 11. Deploy (not executed)

1. Merge `feat/battery-health`; `npm run build` in `dashboard/`; `POCO/sync.sh --app`.
2. Optional before the restart, on the phone as the service user (root), so that the history is in
   place when the collector starts (the collector continues the open rest it finds):

       cd /home/user/cyberpunk && .venv/bin/python -m cyberpunk_computer.battery backfill \
           --db data/battery.db --metrics data/metrics.db --trips logs/trips

   (reads the 190 MB metrics database read-only: about 330 000 rows of the 1-minute tier; run it
   parked). It can also be run later: records from richer sources replace poorer ones.
3. Restart `prius-backend` while parked. Journal: `Battery health collector started`.
4. First drive: `GET /api/v1/battery/summary` → `live.counts` must show `21cf_full` growing and
   no `21cf_first`; `can_diag.isotp_err` in the gateway heartbeat must stay flat. If 21CF fails with
   ISO-TP, revert the one-line change in `comm/subscription_groups.py`; the module works without it.

The 21CF change adds, with every poll (every 10 s since 2026-11-17; 21D0 every 30 s, 21CE unchanged at 2 s), one flow-control frame from the gateway and two consecutive frames
from the battery ECU (the same exchange 21CE and 21D0 already run against that ECU).

## 12. Open gaps

* One ride with block data: every limit and the temperature coefficient are priors. The resistance
  prior decides how winter rides compare until rides at clearly different temperatures exist.
* The 21CF fix is untested on the car; until it works, battery temperature has 1 °C resolution from
  two sensors and there is no delta SOC, fan step or kW limit.
* Pack self-discharge in absolute terms is not measured.
* The 12 V rested voltage is taken under the computer's load and normalised with the cabin
  temperature; the capacity proxy is noisy.
* No MFD presence: the summary is not part of the state the MFD receives. Proposal: publish
  `overall.hv`, `overall.aux` and the persistent weakest block in the state and show one line on
  page 1 of the existing battery screen.
* No alerts: the indicators are ready to feed the alert framework (trend-based, never a driving
  chime) once that branch is merged.
* A backend restart during a drive splits the ride in two.
* `metrics/signals.py::delta_v` and the MFD delta-V chart still show the single-sample max − min
  (see §7).
