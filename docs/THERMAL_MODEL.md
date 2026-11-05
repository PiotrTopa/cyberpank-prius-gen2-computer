# POCO F1 heat model and chassis-fan policy

Bench campaign 2026-11-04/05, phone on its heatpipe, outside-box ambient (BMP2)
24–27 °C, ACC off for most of it. Everything below comes from one ambient and a
bench camera scene: see section 7 before trusting it for summer.

**Result in one paragraph.** The hot CPU readings are not a cooling problem the
fan can solve: CPU zones peak at 95–97 °C at every fan speed, 100 % included. The
limiting zone is the GPU, which follows the sink and throttles at 85 °C. Airflow
has steep diminishing returns, so the old controller's 100 % bought nothing over
about 30 %. The new policy controls a filtered GPU-led temperature and ran the
production load at 31 % (2050 RPM) with the same road rate as 100 % (4780 RPM).
A hysteresis controller with measured thresholds does this; no lumped physical
model runs in the controller, because none fitted well enough to be worth it
(section 4).

Raw data: `docs/data/thermal/2026-11-04/` (`<run>/samples.csv.gz`, 1 Hz: all zones,
clocks, supply power, fan, ambient; `<run>/phases.jsonl`; fit outputs). Scripts:
`scripts/thermal/`. Those scripts were run from a scratch directory and on the
phone, so paths inside them are not repo-relative.

## 1. Hardware under test

| item | detail |
|---|---|
| computer | POCO F1 (SDM845, postmarketOS), no screen, no battery: 4.1 V fed to the battery terminals, so `qcom-battery` V·I is the total phone power `P`. The `qcom-battery` zone is meaningless and is ignored |
| box power | powerbox `power_draw_w` on the 12 V side (phone + hub + fan + whatever else is on), recorded as `box_power_w` |
| heat path | phone on a laptop heatpipe, ADDA AB07505HX110B00 fan; RPM measured per level below |
| sensors | 23 thermal zones; BMP2 outside the box. **BMP1 and AHT20 read None on the bench: no box-air sensor** |
| kernel trips | CPU passive 90/95 °C, GPU passive **85 °C**, hot 90 °C |
| not usable | `cpuinfo_cur_freq` returns 2995200 kHz on both clusters, so the true hardware clock is unknown. The `dcvsh` (LMh) counter steps only in the first seconds after cores come online, even at a 31 °C die, and never during sustained load, so it is not a throttle indicator here |

"Throttling" below therefore means the GPU cooling device being non-zero and, as
the outcome that matters, the road-model rate. CPU cooling devices stayed at 0 in
every run, including the 94.7 °C plateau, even though the trips are at 90 °C. That
is unexplained; the plateau may be a hardware limit the kernel does not report.

## 2. Protocols: which produced what

| protocol | runs | load | what it gives |
|---|---|---|---|
| **A** long load/cool pairs | run1 (20:08–20:53, stopped early) | 7 pinned NumPy matmul processes + `benchncnn` Vulkan loop (CPU+GPU together), `full` profile, 7.1–7.9 W | heavy-load tails at fan 100/80 %. Its die plateau is **not** a saturation measure (see below) |
| **P** pulse series | run3 (20:57–23:59) | 3 pinned little cores + `benchncnn` Vulkan, about 4 W, kept below the throttle point; `full` CPU profile forced with `prius-power full` because ACC was off | sink saturation vs pulse length at fan 0/40/70/100 % |
| **B** production | run4 (fan 0, aborted at 98.2 °C), run5 (100/70/40/20/10 %, 00:23–01:41) | roadsense itself, 6.2–6.7 W phone, 8.9–10.5 W box | operating points for the `full` policy; 180 s sink read-out tail after each level in run5 |
| **L** passive reference | run7 (02:28–03:26); run6 was cut off | same as P but in the real `low` profile, fan 0, 45 min pulse, 8.5 min tail | passive time constant, worst-case `low` temperatures |
| **V** live verification | v1, w1 | see section 6 | policy on real hardware, fan automatic |

Why protocol P replaced A (the user's correction): "saturated" means the mass
behind the die is charged, read from the cooling curve after load-off. A die
plateau only shows the SoC limiting itself. Sink temperature is read from the die
15–20 s after load-off, once the fast internal relaxation is over.

Load comparability: the `benchncnn` load is not constant. In `full` it holds
3.9–4.5 W, but in the `low` profile it swings 1.0–3.9 W (p5–p95, mean 2.5 W). Energy
is therefore integrated from measured power rather than assumed.

Run interruptions: ACC was off from 20:43 on, so `acc_off`/`power_mode_low` tags
in `phases.jsonl` are expected from then on. In run3 the backend state says `low`
while the real profile was the forced `full` (all cores online throughout).
run6 ended at 02:10 because `prius-netwatch` rebooted the phone after a sustained
SIM/VPN outage, not for a thermal reason; its data is not included.

## 3. Raw results

### 3.1 Production load vs fan (protocol B, roadsense, ambient 24.6–25.1 °C)

GPU is the 20 s low-pass mean over the settled part; CPU is the hottest CPU/cluster
zone. Sink is the bulk SoC mean 20 s after the stack stopped.

| fan % | RPM | P W | box W | GPU °C | GPU throttled % | CPU mean / peak °C | mem °C | sink@20 s °C | road runs/s | time s |
|---|---|---|---|---|---|---|---|---|---|---|
| 100 | 4781 | 6.73 | 10.51 | 75.5 | 0 | 87.9 / 96.2 | 64.1 | 40.0 | 13.7 | 641 |
| 70 | 3825 | 6.62 | 9.89 | 76.5 | 0 | 81.4 / 96.5 | 64.0 | 40.6 | 13.4 | 812 |
| 40 | 2524 | 6.47 | 9.58 | 77.9 | 0 | 84.1 / 95.9 | 65.6 | 43.0 | 13.5 | 762 |
| 20 | 1444 | 6.34 | 9.21 | 82.6 | 6 | 86.4 / 96.9 | 69.7 | 47.9 | 13.3 | 534 |
| 10 | 655 | 6.42 | 8.90 | 85.9 | 60 | 87.3 / 97.2 | 77.6 | 56.1 | 11.2 | 901 (cap) |
| 0 | 0 | 6.22 | 9.02 | 86.9 at abort | 40 | peak 98.2 | 76.0 at abort | not read | not recorded | 581 (abort) |

At fan 0, GPU passed 85 °C after 270 s and a CPU zone hit the 98 °C abort after
581 s, still rising. With the fan then at 100 % and the stack still running, GPU came
back to 77.8 °C within about 9 min. Road-rate differences above 20 % are within noise
(the order of the runs also differs). Note the CPU peak does not respond to the fan.

Maximum ambient before the GPU mean reaches its 85 °C trip, assuming temperatures
shift 1:1 with ambient (**assumed, not measured**):

| fan % | 100 | 70 | 40 | 20 | 10 |
|---|---|---|---|---|---|
| ambient °C | 34.4 | 33.4 | 32.0 | 27.3 | 24.2 |

### 3.2 Pulse series (protocol P, about 4 W, 3.2–3.4 W net of idle)

Sink read-out (die, hottest CPU, 15–20 s after load-off) vs pulse length:

| fan % | RPM | idle die °C | 60 s | 120 s | 240 s | 480 s | 900 s |
|---|---|---|---|---|---|---|---|
| 0 | 0 | 32.9 | 37.6 | 40.7 | 44.2 | 49.2 | 50.8 |
| 40 | 2525 | 32.0 | 35.2 | 35.6 | 37.1 | 38.5 | |
| 70 | 3830 | 30.9 | 34.2 | 35.0 | 35.9 | 37.1 | |
| 100 | 4785 | 30.7 | 34.0 | 34.7 | 35.6 | 36.6 | 35.4 |

Slow cooling-tail time constant (hottest CPU zone, 2-exponential fit):

| fan % | 60 s | 120 s | 240 s | 480 s | 900 s |
|---|---|---|---|---|---|
| 0 | 19 (poor fit) | 229 | 259 | 263 | 310 |
| 40 | 27 | 33 | 27 | 57 | |
| 70 | 23 | 27 | 39 | 37 | |
| 100 | 23 | 24 | 25 | 30 | 32 |

Energy in (∫(P − P_idle)dt) was 177–182, 348–354, 693–716, 1536–1623 and
2897–3081 J for the five pulse lengths.

Caveat at fan 0: the cool-downs hit their 10 min cap, so pulses started up to 5.6 K
above the idle baseline. The fan-0 sink values are biased high, and later pulses more
so. At 100 % the 900 s value falling below the 480 s one shows the size of the noise
(about 1 K, because `benchncnn` power varies).

### 3.3 Heavy load (protocol A)

| fan % | RPM | P W | CPU °C | GPU °C | mem °C | CPU tail: fast, slow |
|---|---|---|---|---|---|---|
| 100 | 4731 | 7.08 | 94.7 | 68.5 | 67.9 | 25.2 K/4.0 s, 12.1 K/48 s |
| 80 | 4136 | 7.85 | 94.6 | 80.3 | 72.6 | 21.4 K/5.5 s, 12.7 K/49 s (first 213 s, before ACC off) |

The die falls from about 95 to about 40 °C in 20 s. The SoC-to-pipe contact works:
the early hypothesis of poor contact is **retracted**.

### 3.4 Passive reference (protocol L, `low` profile, fan 0, ambient 24.1 °C)

| phase | P W | box W | GPU mean / p95 / max °C | notes |
|---|---|---|---|---|
| idle | 0.62 | 1.31 | 32.1 (flat) | die 8 K above ambient at 0.62 W |
| load, 45 min | 2.51 | 3.33 | last 15 min 56.1 / 67.6 / 71.2 | GPU 20 s low-pass max 67.7 °C |
| tail | 0.64 | 1.33 | | slow mode 7.0–7.4 K, τ 352 (mem), 402 (GPU), 427 s (pm8998) |

## 4. Fitted parameters, and why no model runs in the controller

Three time scales are visible directly in the tails, without any fit:

| scale | value | what it is (interpretation) |
|---|---|---|
| fast | 3–8 s | die and package relaxing into the sink, independent of the fan |
| middle | 23–57 s with the fan running | the sink with airflow |
| slow | 229–427 s with the fan off | more mass taking part once airflow is gone |

Time to saturation from these: about 4–8 min with the fan at 40–100 %; about 15–20 min
passive (the sink series stalls between 480 and 900 s, τ ≈ 400 s).

Network fits over run3, run4, run5 and run7 together (measured power as input):

| fit | C_die J/K | R_int K/W | C_sink J/K | R_sink at fan 0/10/20/40/70/100 % K/W | rms K |
|---|---|---|---|---|---|
| 2-node, bulk SoC | 0.69 | 4.4–5.0 | 15.7 | 4.19 / 4.17 / 3.29 / 2.18 / 2.12 / 2.22 | 3.6 |
| 2-node, GPU zone | 0.44 | 5.2–5.8 | 19.8 | 4.82 / 4.73 / 3.90 / 2.61 / 2.41 / 2.26 | 3.8 |
| 3-node, bulk SoC | 0.48 | 1.8–2.1 | 1.6 | 7.87 / 7.60 / 6.79 / 5.54 / 5.45 / 5.53 | 3.2 |

The 3-node fit gains 0.4 K while its parameters become unphysical (the chassis path
to ambient runs away to about 380000 K/W, R_int drops to 2), so it is not identifiable
from this data. The 2-node fit's C_sink describes the middle time scale only; it
cannot also produce the 400 s passive tail. Its R_sink(0) ≈ R_sink(10) also rests on
fan-0 production data that never reached steady state. Take these as rough numbers.

Estimates of the quantities asked for, with their spread:

| quantity | estimate | basis |
|---|---|---|
| die-to-sink, GPU zone | ≈ 5.2 K/W, about 33 K at the production load | steady GPU minus sink read-outs, joint fit |
| sink to ambient, passive | ≈ 4–5 K/W (3.1–5.2 across methods) | joint fit; P000 900 s pulse gives 5.2; run7 tail gives 3.1 but is biased low by a power lull just before load-off |
| sink to ambient, fan 40/70/100 % | ≈ 2.6 / 2.4 / 2.3 K/W | joint fit, GPU zone |
| effective heat capacity | ≈ 40 J/K at 1 min, rising to ≈ 100 J/K at 8 min | E_in / sink rise at fan 0 (38, 45, 63, 100 J/K); not a single number, the mass taking part grows with time. Ignores heat lost during the pulse and the raised starting points |
| fan cost | ≈ 1.6 W box power, 10 % vs 100 % | table 3.1 (the load also varied slightly) |

**Decision: hysteresis with measured thresholds, no physical model in the
controller.** The controller needs only the GPU-vs-fan curve in 3.1 and the time
scales above. A model would add parameters that the data does not pin down.

## 5. Policy (`cyberpunk_computer/backend/fan_policy.py`)

Control temperature: `max(GPU_f, CPU_f − 15)`, both low-passed at τ = 20 s, published
as `poco_ema_temp`.

| parameter | `full` | `low` | follows from |
|---|---|---|---|
| on | 78 °C | 70 °C | full: sink ≈ 45 °C, between the 40 % and 20 % operating points. low: 53 °C is the expected idle die in a 45 °C car (32.1 °C at 24.3 °C ambient, shifted), so idle never engages |
| off | 68 °C | 58 °C | wide enough that the fan cannot cycle: the die falls 30–40 K within 30 s of load-off. low: releases above idle even in a 45 °C car |
| duty at on | 20 % | 20 % | lowest level with a clear effect (table 3.1); 10 % already loses road rate at 25 °C |
| duty ceiling reached at | 84 °C | 80 °C | full: 1 K before the GPU trip |
| ceiling | 100 % | 40 % | full: hot cabin, 100 % gives 2.4 K over 40 %, worth having only then. low: nothing above 40 % is worth the noise with the car parked |
| ambient gate | ≥ 10 K | ≥ 15 K | no fan when the control temperature is close to the cabin: airflow cannot help |

Shared numbers:

| parameter | value | follows from |
|---|---|---|
| filter τ | 20 s | removes the scene-driven jitter, tracks the middle time scale. 20 and 30 s gave the same statistics; the old "65 s heatsink" constant does not correspond to anything measured here |
| CPU offset | 15 K | keeps the GPU in charge: filtered CPU peaks 92.6 °C at fan 100 %, so 77.6 °C after the offset, just under the 78 °C on-point. A CPU-only load still counts |
| override on | control ≥ 86 °C, or raw ≥ 98 °C | past the GPU trip; raw 98 °C is above anything seen with the fan running (peak 97.2 °C) |
| override release | control < 82 °C | |
| slew up / down | 5 / 0.5 %/s, 4 % deadband | steady sound instead of following the 1–2 K jitter |
| box purge | unchanged | no sensor on the bench, so nothing to justify a change. Untested |

**Replaced because the data says it is wrong:** the old override at raw 80 °C. Raw CPU
reaches 95–97 °C at any fan speed, so it kept the fan at 100 % whenever roadsense ran,
with no benefit. The old CLI flags (`--fan-full-start-delta` and others) were not set
in `backend.env`; the new `--fan-*` flags override single policy numbers.

Expected fan-off limit under roadsense, extrapolated from about 10 K/W total: the fan
stays off below an ambient of roughly 12 °C (78 °C on-point), so a cold car stays silent.

## 6. Live verification (fan automatic throughout)

| cycle | result |
|---|---|
| `full`, roadsense, 25 min from cold, ambient 24.1 °C (v1) | fan off for the first 138 s, then 25 % (about 1750 RPM). Last 10 min: duty 31 % (23–39 %), 2052 RPM, GPU 79.0 °C mean / 82.1 °C max, GPU throttled 0 %, CPU peak 96.5 °C, P 6.26 W, box 9.12 W, road 13.71 runs/s. No override, 1 on/off edge |
| `full` cool-down (v1) | stack off: fan stopped 27 s later, GPU 41 °C at 30 s, 33.5 °C at 480 s, no restart |
| `low`, worst-case load, 20 min (v1) | fan stayed 0 % throughout, GPU max 69.9 °C raw, P 2.74 W in the last 10 min |
| `low` cool-down (v1) | fan stayed 0 %, GPU 33.5 °C at 480 s |
| `low`, repeat of the above (w1) | fan 0 %, GPU max 69.3 °C raw. Intended as the lowered-threshold test, but the unquoted flags never reached the backend, so it ran the real thresholds |
| `low` engage/release, **thresholds lowered for this run only** (`--fan-low-on 46 --fan-low-off 40 --fan-low-full 58`, confirmed on the process command line; w2, 10 min) | engaged after 49 s at 25 %, stayed within the 40 % ceiling, released 27 s after load-off, no restart. 3 on/off edges during the load, because this load swings 1–4 W and the lowered band is 6 K, half the real 12 K |

The real `low` on-point (70 °C) was **not reached on hardware**: at this ambient the
worst-case load tops out at a filtered 67.7 °C. Engagement at the real numbers rests on
w2 and the tests.

Compared with fixed 100 %: 4781 → 2052 RPM and 10.51 → 9.12 W box, same road rate,
GPU 3.5 K warmer and still 6 K below the trip on average. Duty wanders slowly within
23–39 %, because the GPU reading follows the scene. Whether that is audible has not
been judged by ear.

## 7. Unmeasured / open

* **Ambient range.** All data is from 24–27 °C. Hot-cabin behaviour (up to 45 °C) and
  cold (−10 °C) are extrapolated with an assumed 1:1 shift, and exercised only in the
  simulated tests. Above about 34 °C ambient the data predicts GPU throttling at
  roadsense load even at 100 %: the fan cannot prevent it, and road rate should degrade
  gradually (11.2 runs/s at 60 % throttled).
* **Real scene.** The camera saw a static underground-parking view. Driving scenes may
  change the load.
* **Installed geometry.** Bench airflow, not the box in the car. Box air and box purge
  are untested, since BMP1/AHT20 read None.
* **Noise.** Only RPM was measured, nothing acoustic.
* **True CPU clock** is unreadable, and CPU cooling devices did not act at 95 °C (section
  1). Road rate is the only throughput evidence, and CPU-only throughput was not tested.
* **Passive steady state at production load** was not reached (abort at 98 °C).
* **`low` worst case** uses a synthetic GPU load that the real key-off state does not
  have; real key-off load is expected near the 0.62 W idle.
* **Network watchdog.** `prius-netwatch` rebooted the phone during key-off when the
  SIM/VPN link dropped. Not thermal, but a key-off reboot may matter elsewhere.
