# MFD main screen: scene and power box

The centre column of the main screen (`ui/screens/main_screen.py`, page 1) shows the car when it is
parked and the road when it drives. Code: `cyberpunk_computer/ui/scene/`. Tests:
`tests/test_mfd_scene.py`. Demo: `python -m cyberpunk_computer.ui.scene.demo`.

## 1. Hardware it is built for

| Fact | Value | Consequence |
|---|---|---|
| Panel | 480 x 240, 60 Hz progressive, analog RGBS through a VGA666 resistor DAC (`docs/MFD_VIDEO_TIMINGS.md`) | everything is laid out in native pixels |
| Colour | RGB666 (64 levels per channel), per-channel skew on edges | no anti-aliasing: text, lines and polygons are 1-bit; the only soft pixels are stepped lamp halos |
| Fonts | Terminus from 14 px (even sizes), 04B03 below (`ui/fonts.py`) | read-outs are Terminus 14/16/28, labels 04B03 8 px |
| CPU | Pi Zero 1.3, one ARMv6 core, about 21 fps for the whole UI | the scene is a cached surface, re-drawn at most 10 times a second and only while it changes |
| Start-up | UI start time is a known problem | no pygame objects, mesh or sprites at import; they are built on the first frame |

## 2. Layout (centre column, x 120..359)

```
y   0 ┌────────────────────────────┐
      │ scene, 238 x 168           │  parked: the car   driving: the road
y 169 ├────────────────────────────┤
      │ power box, 238 x 54        │  HV power, history bars, SOC
y 224 ├────────────────────────────┤
      │ cruise line · page dots    │  (unchanged, 16 px)
y 240 └────────────────────────────┘
```

The side panels (AUDIO, AMBIENT, ENGINE, CLIMATE, LIGHTS, BATTERY) and page 2 are unchanged. The link
LED stays in the top-right corner of the scene.

Power box: left the HV battery power (kW, green while charging) with an `ICE` / `EV` flag; middle
41 history bars (2 px + 1 px gap, discharge up in cyan, charge down in green, +-25 kW full scale,
time base = `display.power_chart_time_base`, 60 s by default); right the SOC as the 8 segments of
the factory gauge plus the percentage.

## 3. Art direction

Vector-scan arcade: black background, near-black flat fills, 1 px lines in three brightness tiers of
the VFD cyan, a violet floor grid, amber for anything open or warning, red for tail and stop lamps.
The car is an original low-poly model of the NHW20 (`scene/model.py`): 13 cross-sections lofted from
the car's dimensions (4450 x 1725 x 1490 mm, wheelbase 2700 mm), 98 body faces, doors and hatch as
hinged parts, lamp lenses and wheel arches as decals. Hidden surfaces need no depth buffer: the
closed body is drawn with back-face culling, wheels and open parts are ordered around it by the side
the camera is on. Silhouette edges are bright, shut lines and creases dimmer.

What the references show, and what was taken from them: driving visualisations of the Tesla kind put
the own car at the bottom centre seen from behind and above, lane lines and road edges as vectors,
the vehicle ahead as a drawn model (coloured by how much it matters), other traffic, cones and
pedestrians as models, traffic lights as a three-lamp signal, the speed limit as a sign, and the
speed at the top. Adopted here: the same composition, the ego lane filled, road edges in a second
colour, the lead vehicle as a drawn rear view with target brackets and a distance read-out, a
three-lamp signal, a European limit sign, speed top-left. Not adopted: realism. The floor grid, the
striped sun and the ridge line are 80s arcade; the depth scale is logarithmic (section 5).

## 4. States

`scene/director.py` (pure Python) decides; the renderer only reads it.

| State | Condition | Picture |
|---|---|---|
| Parked | not moving, and gear P, or a door open, or stopped for 2.5 s; also while reversing | three-quarter view of the car on a grid pad; gear, `READY`/`ACC`/`OFF`, `ICE`; chips for the lamps that are on; lit lamp lenses with halos |
| Doors | per door, 0.55 s swing | the door opens on the model with an amber outline; `OPEN DRIVER PASSENGER ...` at the bottom; `DOORS CLOSED` only when the state is known |
| Rear group | only the shared "rear door or back door" bit is set | both rear doors and the hatch ajar (34 %) in amber, text `REAR/BACK`: one of them is open, the car does not say which |
| Opening | real speed >= 3 km/h in a forward gear | 1.3 s camera swing from the three-quarter view to the chase view, the pad opens into the road |
| Driving, road live | roadsense running, scene data younger than 2.5 s | ego lane filled, lane lines (ego pair bright and 2 px, outer pair dim, below probability 0.3 not drawn), road edges violet, lead vehicle with distance, time gap and closing/opening arrow, objects, traffic light, limit sign, sky by light condition |
| Driving, road stale | scene data 2.5..7 s old | lanes frozen and dimmed, no lane fill, no lead vehicle, no objects, `SIGNAL LOST` blinking |
| Driving, signal lost | road data was live and is now older than 7 s | floor grid only, `NO SIGNAL` blinking |
| Driving, no road data | roadsense running, no scene data received yet (stack starting, scene feed off) | floor grid only, `NO ROAD DATA` (steady, dim); limit sign and traffic light from the health mirror still show |
| Driving, vision off | roadsense not running | floor grid only (a floor line runs under the car: never a fake lane), `VISION OFF` |
| Closing | stopped for 2.5 s (at once in P or with a door open) | the swing in reverse |

Rules that hold in every state: nothing is drawn for data that is not there (no lead in the scene
= no vehicle; unknown doors = no door text; unknown lamps = dark lenses); a short stop keeps the
road view; the speed shown is `display_speed_kmh` (the speedometer value), motion and the time gap
use the real speed.

Lead vehicle colour: cyan at a time gap >= 1.5 s, amber below, red below 0.8 s (information only;
nothing acts on it). The limit digits turn amber more than 3 km/h above the limit.

Sky strip above the horizon, from the roadsense light label: `dusk_dawn` a striped setting sun,
`day` a sun clear of the horizon, `night` stars and a crescent, unknown none. The ridge line is
decoration.

## 5. Chase-view geometry

Camera 8 m behind and 3.6 m above the car, looking at a point 12 m ahead, focal length 196 px.
True perspective would squeeze 30..100 m into seven pixel rows, so distances ahead are remapped:
the inverse camera depth falls linearly with `log(1 + r / 1.55)` from the nose (1:1 scale there) to
a displayed depth of 32 m for the 100 m line (`render.compress`). Results: 100 m of road on screen,
a vehicle 30 m ahead about 20 px wide, 100 m ahead about 11 px. The road centre line is scaled back
to where true perspective puts it, so curves keep their shape; widths (lanes, vehicles) keep the
displayed scale. Because this camera looks straight down the road, a ground point's row and scale
depend on its distance only and its column is linear in the lateral offset: lane points need no
projection (`SceneRenderer._row`, `_xs_table`).

## 6. Data inputs

`scene/inputs.py::read_inputs(state)` is the only place that names state fields. Unknown reads as
`None`.

| Scene input | Source today |
|---|---|
| speed for display | `vehicle.display_speed_kmh`, else `vehicle.real_speed_kmh` |
| motion, time gap | `vehicle.real_speed_kmh` |
| gear, READY, ACC | `vehicle.gear`, `vehicle.ready_mode`, `powerbox.acc_on` or `vehicle.acc_on` |
| ICE flag | `vehicle.ice_running`, forced off by `vehicle.ice_stopped` |
| driver / passenger door | `bean.decoded["driver_door_open"]`, `["passenger_door_open"]` |
| rear group | `bean.decoded["rear_door_open"]` (one bit for rear-left, rear-right and back door) |
| any door | `bean.decoded["any_door_open"]`, `["door_open"]`, `dynamics.door_open` when set |
| low beam, tail, front fog, high beam | `bean.decoded["lamp_head" / "lamp_tail" / "lamp_fog_front" / "lamp_high_beam"]`, else `dynamics.low_beam / parking_lights / fog_front / high_beam` |
| stop lamps | `vehicle.brake_switch`, else `vehicle.brake_pressed > 0` |
| HV power, SOC | `energy.battery_power_kw`, `energy.battery_soc`, bars from `avc.battery_bars` else `dynamics.soc_bars` |
| roadsense running | `roadsense.state == "running"` and `roadsense.api_ok` not False |
| speed limit, ego traffic light, light/place/weather | the scene mapping, else `roadsense.health["speed_limit" / "lights" / "env"]` (refreshed every 5 s) |
| lanes, edges, lead vehicle | the scene mapping only (section 7), from the backend feed `backend/roadsense_scene.py` |
| objects | the scene mapping only; roadsense provides none yet (section 8.2) |

Explicit per-door fields are read first if a state ever has them (`door_fl_open`, `door_fr_open`,
`door_rl_open`, `door_rr_open`, `hatch_open` / `back_door_open` on `state.body`, `dynamics` or
`vehicle`): a full door swing then replaces the ajar group.

## 7. Road scene contract (backend -> frontend)

The state publish is limited to 1 Hz, too slow for a road. The frontend therefore takes the road
from a backend **event** `roadsense_scene` on the existing ZMQ event channel (the one that carries
`touch`), 5..10 Hz, latest wins (`inputs.SceneFeed`, registered in `core/app.py`). The same mapping
is also accepted as a state field `roadsense.scene` or as `roadsense.health["scene"]`; the geometry
is low-passed (0.3 s) in the director, so 1 Hz still moves smoothly. Staleness is judged by `seq`
not changing, on the frontend clock.

```
{"seq": 4711,                                  # roadsense frame seq of the road message
 "x": [0, 5, 10, 15, 20, 30, 40, 50, 65, 80, 100],   # sample distances, m ahead of the camera (optional: this is the default)
 "lanes": [L, L, L, L],                        # far-left, left, right, far-right; L = {"y": [...], "prob": 0..1} or null
 "edges": [E, E],                              # left, right; E = {"y": [...]} or null
 "lead": {"d": 34.2, "y": 0.3, "v_rel": -1.2, "prob": 0.93, "kind": "car"} | null,
 "objects": [{"kind": "car|truck|bus|van|person|bike|cone|lamp", "x": 41.0, "y": -3.4, "state": "on"}],
 "light": {"state": "red|yellow|red_yellow|green|off|unknown", "d": 34.0, "y": 2.1, "flashing": false} | null,
 "limit": {"kmh": 50, "src": "sign|default"} | null,
 "env": {"light": "day|dusk_dawn|night", "place": "...", "weather": "..."}}
```

Units and frame as roadsense: metres, x forward, **y right** (the frontend flips it), origin at the
camera. `y` arrays are lateral offsets at the `x` distances. `v_rel` in m/s, negative = closing.
Every key is optional; a missing key means "not known" and nothing is drawn for it.

### 7.1 Backend feed (`cyberpunk_computer/backend/roadsense_scene.py`)

The backend reads roadsense's WebSocket `ws://127.0.0.1:8090/ws?topics=road,lights,speed_limit,env`
(roadsense's own API on loopback, not the backend's `/api/v1/roadsense/ws` front) with a stdlib
RFC 6455 client in its own thread and publishes `roadsense_scene` on the ZMQ PUB channel only (not
on the dashboard websocket, whose latest-only queue would let scenes displace state snapshots),
through a latest-only slot per event name (`ZmqServer.enqueue_latest_event`, compact JSON).

| Scene field | roadsense source | Conversion |
|---|---|---|
| `seq` | `road.seq` (ring frame seq of the second frame of the model pair) | as is; restarts at 1 when the camera restarts |
| `x` | not sent | the default distances above; lanes and edges are resampled to them |
| `lanes[i].y` | `road.lane_lines[i].y` at `road.lane_lines[i].x` (33 points, 0..192 m) | linear interpolation at the 11 distances, 0.01 m; null when a needed point is null (invalid model output) |
| `lanes[i].prob` | `road.lane_lines[i].prob` | 0.01; the whole lane is null below 0.2 (the screen draws from 0.3) |
| `edges[i].y` | `road.road_edges[i].y` at `.x` | as lanes; no probability in roadsense |
| `lead.d`, `lead.y` | `road.leads[0].x[0]`, `.y[0]` (the "now" hypothesis; m from the camera, y right) | 0.1 m / 0.01 m |
| `lead.v_rel` | `road.leads[0].v[0] - road.v_ego` (else `- road.pose.vx`) | 0.1 m/s; both are the model's own estimates, so the car's speed is not mixed in |
| `lead.prob` | `road.leads[0].prob` | 0.01; `lead` is null below 0.3 (the screen draws from 0.5) |
| `lead.kind` | not provided | not sent: the screen draws a car |
| `objects` | not provided | not sent |
| `light` | `lights.ego` (api annotation): `state`, `dist_m`, `lateral_m`, `flashing` | `d` = `dist_m` (housing-size estimate, about +-30 %), `y` = `lateral_m`: offset from the planned path, not from the camera; null when no light is relevant |
| `limit` | `speed_limit.limit_kmh`, `.source.kind` | `{"kmh", "src"}`; null when the limit is unknown |
| `env` | `env.light/place/weather.label` | labels only |

Rate and staleness:

* one scene per new road frame, at most 10 per second (`BACKEND_ROADSENSE_SCENE_HZ`, 1..10); modeld
  runs 10..14 times a second (fewer when it throttles), so the scene comes at 7..10 Hz;
* a road frame is published only once (same `seq` never twice) and only within 1 s of its arrival;
* `light`, `limit`, `env` ride along only while their last message is younger than 2 s, 3 s and 5 s
  (roadsense sends them about 3/s, at least 1/s and 1/s); otherwise the key is left out and the
  screen falls back to the health mirror;
* publishing runs only while the roadsense manager is `running` and the MFD manager is `on` or
  `grace` (without an MFD manager: roadsense alone decides); otherwise the socket is closed. When
  roadsense stops or stalls nothing is sent and the screen's own staleness applies (section 4).

Connection: 3 s connect timeout, reconnect after 1 s doubling to 10 s while active, a stream silent
for 5 s is reopened, pings are answered (roadsense pings every 20 s). The feed never starts, stops or
reconfigures roadsense and does not change its duty; it only reads what the stack already sends.

Config: `BACKEND_ROADSENSE_SCENE=1` (default on; needs `BACKEND_ROADSENSE_ENABLED=1`), `0` turns
the events off; `BACKEND_ROADSENSE_SCENE_HZ=10`. Host and port follow `BACKEND_ROADSENSE_API_HOST` /
`BACKEND_ROADSENSE_API_PORT`.

Budget, measured on the recorded roadsense messages of `tests/fixtures/roadsense_ws_2026-11-14`:

| Item | Value |
|---|---|
| roadsense `road` message received by the feed | about 15 kB, 10..14/s (about 180 kB/s on loopback) |
| work per road message that is not published | a 48-byte type peek, no JSON decode |
| decode + conversion per published scene (phone) | 0.31 ms on an i7-8850H, about 1 ms on the phone's cpu3: about 1 % of that core at 10 Hz |
| event on the wire (envelope, compact JSON) | 590 B without, 730..750 B with light, limit and env |
| phone -> Pi link | about 7.5 kB/s (60 kbit/s) at 10 Hz |
| decode on the Pi Zero per event | envelope `json.loads` 0.011 ms + `SceneFeed.push` and `read_inputs` 0.046 ms on the desktop: about 4 ms on the Pi (x70), about 4 % of its core at 10 Hz, plus the scene redraws of section 10 |

The frontend client does not log `roadsense_scene` events (formatting one costs more than decoding
it on the Pi).

## 8. Requests

### 8.1 Backend (this repository)

The event `roadsense_scene` (section 7.1) and the health mirror (`RoadsenseState.health`:
`speed_limit`, `lights`, `env`) cover everything the screen draws today.

### 8.2 roadsense

| Request | Field, meaning, units, rate |
|---|---|
| Scene topic | api-derived topic `scene` on `/ws` with exactly the mapping of section 7, 10 Hz: down-sampled lane/edge polylines (11 distances instead of 33), `leads[0]`, the ego light, the limit. Removes the resampling from every consumer and the 15 kB `road` message per model run from the loopback socket (the backend needs 0.6 kB of it) |
| Road edge confidence | `road_edges[i].prob` (or the `y_std` already sent, summarised) 0..1, per model run, so a doubtful edge can be dimmed or left out like a lane line |
| Light distance from the road | `lights.ego` `lateral_m` measured from the camera axis, or the path offset at that distance alongside it, m, 3/s (today it is the offset from the planned path) |
| Other vehicles | `objects[]` entries `{"track_id", "kind", "x" m, "y" m, "v_rel" m/s, "prob"}` for vehicles in adjacent lanes and oncoming, 5..10 Hz. Today modeld has only the lead hypotheses |
| Lead vehicle class and stop lamps | `lead.kind` (`car`/`van`/`truck`/`bus`/`bike`), `lead.braking` bool, 5 Hz |
| Lane line type | per lane line `"type": "solid|dashed|double"`, 2 Hz, so dashes can be drawn and scroll |
| Street lamps | `objects[]` with `kind: "lamp"`, `x`, `y`, `state: "on|off"`, 2..5 Hz (the renderer draws them already; the demo feeds simulated ones) |
| Stop line | `light.stop_line_m` / a `stop` rule distance in m, 5 Hz |
| Vulnerable road users | `objects[]` with `kind: "person" / "bike"`, 5..10 Hz |

## 9. Future data / sensors

Wanted by the design, not available from the car or roadsense today, and therefore not drawn:

| Element | Needs |
|---|---|
| traffic in adjacent lanes, oncoming traffic | roadsense object tracks (8.2) |
| lead vehicle type and its stop lamps | roadsense (8.2) |
| dashed / solid lane lines | roadsense lane line type |
| street lamps along the road | a lamp detector in roadsense (drawing code exists: `objects` kind `lamp`) |
| stop line at a red light or stop sign | roadsense |
| pedestrians, cyclists, cones | roadsense object classes (drawing code exists) |
| rear-left / rear-right / back door separately, bonnet | body switches the BEAN frame does not separate; a satellite input or a further BEAN decode |
| turn signals, hazard lamps, reversing lamp | not decoded on CAN/BEAN yet |
| blind-spot markers | side radar or a rear/side camera |
| adaptive-cruise gap and target | the car has plain cruise; set speed (`vehicle.cruise_set_speed`) can join the HUD once its decode is confirmed |
| traffic behind | a rear camera in roadsense |
| parking distance arcs in the parked view | ultrasonic sensors |
| steered front wheels in the parked view | `dynamics.steering_angle` calibrated in degrees |
| speed-limit source by map | GPS + the OSM speed-limit service |

New elements go in as a layer function in `scene/render.py` (the layer list is in its docstring) fed
by a field of `SceneInputs`; world objects only need a `kind` branch in `_draw_object`.

## 10. Performance budget

Measured on a desktop (i7-8850H, pygame-ce 2.5.8, `SDL_VIDEODRIVER=dummy`), whole
`MainScreen.update + render` per frame; the Pi estimate uses x70 (the Pi runs the previous screen,
0.56 ms here, at about 21 fps = 47 ms with roughly 7 ms of that in the framebuffer write; treat it
as x50..x85).

| Case | Desktop | Pi Zero estimate |
|---|---|---|
| previous home page (every frame) | 0.56 ms | 39 ms |
| parked, steady (two cached blits) | 0.41 ms | 29 ms |
| driving, no vision | 0.42 ms + 0.06 ms per scene redraw | 32 ms average |
| driving, road live with lead and four lamps | 0.42 ms + 0.16 ms per scene redraw | 35 ms average (30 ms, and 41 ms on the redraw frames) |
| one car render (cache miss): camera step, door step or lamp change | 0.4..0.75 ms | 30..50 ms, once per sprite |
| first frame: mesh, 15 cameras, first car | 2.5 ms | about 0.2 s, once |
| import of `ui/scene` (the `state` package next to it: 42 ms) | 4 ms | about 0.3 s |

Rules that keep it there:

* the scene surface is redrawn only when its signature changes or while something animates, and then
  at most every 95 ms; a parked car costs one opaque 238 x 168 blit per frame;
* the own car is a colour-keyed (RLE) sprite cached per (camera step, door steps, lamps), 64 entries;
  the 15 camera steps of the swing are pre-rendered one per 0.4 s while the scene is steady, for
  the current lamps with stop lamps off and on, so the swing itself renders no car;
* halos are small pre-rendered sprites blitted additively; there is no per-pixel alpha and no
  full-surface blend anywhere;
* text is rendered once per distinct string and cached; the power box is redrawn once a second;
* the chase view projects nothing per point (section 5);
* no numpy.

The frame titles of the side panels and the cruise line are cached as well (they were re-rendered
every frame); that is where 0.05 ms of the saving comes from. The six side panels are now the
largest cost of the screen (0.34 ms of 0.41 ms).

## 11. Demo and screenshots

```
python -m cyberpunk_computer.ui.scene.demo --shots OUT_DIR      # 18 states, native + 4x PNG, headless
python -m cyberpunk_computer.ui.scene.demo --live --scale 3     # the timeline in a window
python -m cyberpunk_computer.ui.scene.demo --bench              # frame cost including the simulation
```

Timeline (44 s): parked, doors open and close, READY and lamps, pull away, lead vehicle closing in,
limit sign, left curve at night with street lamps, red light, stop, the scene closes. Vehicle values
go through the real state fields; the road goes in as `roadsense_scene` events. The street lamps of
the demo are simulated data for the `lamp` object kind (section 9).
