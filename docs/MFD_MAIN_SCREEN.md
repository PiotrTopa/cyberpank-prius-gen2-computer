# MFD main screen: scene and status strip

The centre column of the main screen (`ui/screens/main_screen.py`, page 1) shows the car when it is
parked and the road when it drives. Code: `cyberpunk_computer/ui/scene/`. Tests:
`tests/test_mfd_scene.py`. Demo: `python -m cyberpunk_computer.ui.scene.demo`.

## 1. Hardware it is built for

| Fact | Value | Consequence |
|---|---|---|
| Panel | 480 x 240, 60 Hz progressive, analog RGBS through a VGA666 resistor DAC (`docs/MFD_VIDEO_TIMINGS.md`) | everything is laid out in native pixels |
| Colour | RGB666 (64 levels per channel), per-channel skew on edges | no anti-aliasing: text, lines and polygons are 1-bit; the only soft pixels are stepped lamp halos |
| Fonts | Terminus from 14 px (even sizes), 04B03 below (`ui/fonts.py`) | read-outs are Terminus 14/16/28, labels 04B03 8 px; sign pictograms are drawn, not typeset |
| CPU | Pi Zero 1.3, one ARMv6 core, about 21 fps for the whole UI | the scene is a cached surface, re-drawn at most 10 times a second and only while it changes |
| Start-up | UI start time is a known problem | `ui/scene` is imported and built after the first frame is on the glass (`MainScreen.scene_page`), not at start-up |

## 2. Layout (centre column, x 120..359)

```
y   0 ┌────────────────────────────┐
      │ scene, 238 x 216           │  parked: the car   driving: the road
y 217 ├──────────┬──────────┬──────┤
      │ CRUISE   │ HV power │ dots │  strip 194 x 21 + page dots 44 px
y 240 └──────────┴──────────┴──────┘
```

The side panels (AUDIO, AMBIENT, ENGINE, CLIMATE, LIGHTS, BATTERY) and page 2 are unchanged; the
strip is shown on every page. The link LED stays in the top-right corner of the scene.

Scene HUD while driving (all on the scene surface):

| Place | Content |
|---|---|
| top-left | speed (Terminus 28, `display_speed_kmh`), `km/h`, gear; under it `SET nn` in green while cruise is engaged |
| top-right | link LED, speed-limit sign, ego traffic light as a three-lamp signal with its distance |
| right column, from y 33 | the rules in effect as sign pictograms, 21 px pitch, up to six; `L` / `R` beside a rule that binds one side |
| left column, from y 45 | up to three warning signs ahead, each with the expected distance |
| bottom-left | chips for the lamps that are on (`POS`, `LOW`, `HIGH`, `FOG`), above them the perception status (nothing when live) |
| on the road | lead vehicle with target brackets, distance (m), time gap (s) and a closing/opening arrow with the speed difference in km/h |

Status strip (`scene/powerbox.py::StatusStrip`, 194 x 21):

| x | Content |
|---|---|
| 0..71 | **Cruise slot.** `CRUISE`, the state (`--` nothing known, `OFF` main switch off, `READY` main switch on, `ON` engaged, green), the set speed in Terminus 14 (`--` when unknown; the memory speed, dim with an `M`, when only that is known), three gap bars at x 61..69 drawn only when a gap setting exists |
| 76..130 | HV battery power in kW (green while charging), `ICE` / `CHG` / `EV` flag |
| 133..176 | 22 history bars (1 px + 1 px gap, discharge up in cyan, charge down in green, +-25 kW, time base = `display.power_chart_time_base`, 60 s by default) |
| 182..190 | SOC as the 8 segments of the factory gauge |

The strip is the stopgap for the power read-out until the VFD satellite shows it; the BATTERY panel
keeps the numbers.

Side panels: two additions tie them to the scene, each switchable in
`main_screen.SIDE_STYLE` (off = the earlier look of that item):

| Key | Effect |
|---|---|
| `corner_ticks` | 5 px bright ticks on the corners of the six panels at rest (the motif of the lead vehicle's target brackets); the focus and edit accents are unchanged |
| `ambient_swatch` | AMBIENT, empty before: the hue as 26 colour blocks with a marker, the brightness as 10 segments, the mode name; outlines only while ambient is off. Values from the persisted ambient settings, drawn once per change |

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
striped sun and the skylines are 80s arcade; the depth scale is logarithmic (section 5).

## 4. States

`scene/director.py` (pure Python) decides; the renderer only reads it.

| State | Condition | Picture |
|---|---|---|
| Parked | not moving, and gear P, or a door open, or stopped for 2.5 s; also while reversing | three-quarter view of the car on a grid pad; gear, `READY`/`ACC`/`OFF`, `ICE`; chips for the lamps that are on; lit lamp lenses with halos |
| Doors | per door, 0.55 s swing | the door opens on the model with an amber outline; `OPEN DRIVER PASSENGER ...` at the bottom; `DOORS CLOSED` only when the state is known |
| Rear group | only the shared "rear door or back door" bit is set | both rear doors and the hatch ajar (34 %) in amber, text `REAR/BACK`: one of them is open, the car does not say which |
| Opening | real speed >= 3 km/h in a forward gear | 1.3 s camera swing from the three-quarter view to the chase view, the pad opens into the road |
| Driving, road live | roadsense running, scene data younger than 2.5 s | ego lane filled, lane lines (ego pair bright and 2 px, outer pair dim, below probability 0.3 not drawn), road edges violet, the planned path as chevrons, lead vehicle with distance, time gap and closing speed (stop lamps lit when it decelerates harder than 1.5 m/s2), objects, traffic light, limit sign, sign columns, scenery by place / light / weather |
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

The limit sign has a thick red ring when a sign was seen; a limit that follows from a default rule
(`src: default`) or has a confidence below 0.35 gets a thin dim ring and dim digits.

## 5. Chase-view geometry

Camera 8.5 m behind and 5.2 m above the car, looking at the ground 10 m ahead, focal length 196 px;
the horizon is at y 66, the car's tail at y 208. True perspective would squeeze 30..100 m into a few
pixel rows, so distances ahead are remapped: the inverse camera depth falls linearly with
`log(1 + r / 1.92)` from the nose (1:1 scale there) to a displayed depth of 48 m for the 100 m line
(`render.compress`):

| Distance ahead | Row (y) | Width of a 1.8 m vehicle |
|---|---|---|
| 5 m | 146 | 26 px |
| 10 m | 135 | 23 px |
| 30 m | 114 | 16 px |
| 60 m | 100 | 11 px |
| 100 m | 89 | 9 px (floor of the drawing) |

The road centre line is scaled back to where true perspective puts it, so curves keep their shape;
widths (lanes, vehicles) keep the displayed scale. Because this camera looks straight down the road,
a ground point's row and scale depend on its distance only and its column is linear in the lateral
offset: lane points need no projection (`SceneRenderer._row`, `_xs_table`).

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
| low beam, tail (position), front fog, high beam | `bean.decoded["lamp_head" / "lamp_tail" / "lamp_fog_front" / "lamp_high_beam"]`, else `dynamics.low_beam / parking_lights / fog_front / high_beam` |
| stop lamps | `vehicle.brake_switch`, else `vehicle.brake_pressed > 0` |
| cruise engaged, main switch, set speed, memory speed | `vehicle.cruise_active`, `.cruise_main_switch` (None = unknown), `.cruise_set_speed`, `.cruise_memory_speed` (solicited 21D3; unverified on the car) |
| cruise gap setting | `vehicle.cruise_gap` / `.acc_gap` if a state ever has one (none today) |
| HV power, SOC | `energy.battery_power_kw`, `energy.battery_soc`, bars from `avc.battery_bars` else `dynamics.soc_bars` |
| roadsense running | `roadsense.state == "running"` and `roadsense.api_ok` not False |
| speed limit, ego traffic light, place / light / weather | the scene mapping, else `roadsense.health["speed_limit" / "lights" / "env"]` (refreshed every 5 s) |
| lanes, edges, planned path, lead vehicle, rules in effect, warnings ahead | the scene mapping only (section 7), from the backend feed `backend/roadsense_scene.py` |
| objects | the scene mapping only; roadsense provides none yet (section 8.2) |

Explicit per-door fields are read first if a state ever has them (`door_fl_open`, `door_fr_open`,
`door_rl_open`, `door_rr_open`, `hatch_open` / `back_door_open` on `state.body`, `dynamics` or
`vehicle`): a full door swing then replaces the ajar group.

### 6.1 Headlamps on the road (`render._draw_beams`)

| Lamp state | Road view | Parked view |
|---|---|---|
| off | nothing | dark lenses |
| position (tail lamps without low beam) | nothing on the road; tail lamps red; chip `POS` | small marker glow in the headlamp; chip `TAIL` |
| low beam | hatched fan 46 m long, 0.8 m growing to 2.7 m half-width, with the cut-off: the left half ends at 70 % of the reach | fan 3.4 m long off the nose, lit lens with halo |
| high beam | fan 112 m long, up to 4.6 m half-width, brighter, no cut-off | fan 6 m, larger halo |
| front fog | short wide fan, 13 m long, up to 6.2 m half-width, green-yellow, under the others | fan 2.4 m |

A fan is rows of the road lit at geometric distance steps, an outline and a bright end line. It is
fixed to the car (it does not follow the road), so in the road view each kind is one cached sprite.

### 6.2 Scenery (`scene/scenery.py`)

The road view's backdrop, roadside and weather follow roadsense's environment labels. Vocabulary
(roadsense `models/envd-classes.json`, `tools/envd/prompts.py`; passed through unchanged by the
feed and by the health mirror):

| Group | Labels |
|---|---|
| place | `highway`, `city_street`, `residential`, `rural_road`, `forest_road`, `tunnel`, `parking_lot`, `underground_parking`, `gas_station`, `bridge` |
| light | `day`, `dusk_dawn`, `night` |
| weather | `clear`, `overcast`, `rain`, `snow`, `fog` |

| Place | Backdrop (above the horizon) | Roadside (streams past with the distance driven) |
|---|---|---|
| (unknown) | bare ridge | none |
| `highway` | low hills, a power line on lattice pylons | marker posts every 25 m, white reflector left, red right |
| `city_street` | skyline of slabs and a stepped ziggurat; lit windows, mast lights and neon signs at dusk and night | none |
| `residential` | houses with pitched roofs, trees; lit windows at dusk and night | none |
| `rural_road` | two mountain ridges, lone trees | marker posts |
| `forest_road` | two rows of conifers, opening over the road | conifers every 17 m beyond both road edges |
| `bridge` | suspension towers, main cables, hangers | marker posts joined by a rail |
| `parking_lot` | parked cars along the far edge, light masts | painted bays every 5 m on both sides |
| `gas_station` | canopy with lamps, pumps, price pylon | none |
| `tunnel` | none (no sky): arch frames every 10 m, wall and ceiling lines, a lamp row on each wall | the frames themselves |
| `underground_parking` | none: low ceiling beams, four pillar rows, strip lights | the frames themselves |

| Light | Sky |
|---|---|
| `day` | sun with rays, vector clouds, blue scan-line glow over the horizon; skyline in grey-cyan outlines |
| `dusk_dawn` | striped sun on the horizon, afterglow bands, magenta glow; violet skyline, some windows lit |
| `night` | stars, crescent; dark violet skyline, windows and lamps lit |
| (unknown) | nothing in the sky; violet skyline |

| Weather | Effect |
|---|---|
| `clear` / unknown | as above |
| `overcast` | cloud deck instead of sun, moon and stars |
| `rain` | cloud deck; slanted streaks falling over the scene (10 redraws a second also when standing) |
| `snow` | cloud deck; flakes drifting down |
| `fog` | cloud deck; a dithered band over the far road and the foot of the skyline |

All of this is decoration that states a label; detected things arrive as `objects`. A label outside
the vocabulary draws the neutral variant. The backdrop pans up to 40 px with the lateral position of
the road's far end. Roadside scenery, weather and the enclosed places are drawn in the open road
view only (the parked view stays static).

### 6.3 Signs (`scene/signs.py`)

The right column shows `signs.on` (rules in effect that bind the car, without the speed limit), the
left column `signs.ahead` (warnings, nearest first, with the expected distance). Pictograms are
19 x 19 original drawings keyed by the roadsense rule id (`api/roadsense_api/rules/pl.json`,
`effects[].id`); `--shots` writes a legend of all of them. Drawn ids:

* prohibitions: `no_overtaking`, `no_overtaking_hgv`, `no_stopping`, `no_parking`, `no_entry`,
  `road_closed`, `no_left_turn`, `no_right_turn`, `no_u_turn`, `no_turns`, `no_straight`,
  `height_limit`, `width_limit`, `weight_limit`, `give_way_oncoming`, `no_motor_vehicles`,
  `no_trucks`, `no_buses`, `no_motorcycles`, `no_trailers`;
* priority: `stop`, `yield`, `priority_road`, `end_of_priority_road`, `priority_over_oncoming`;
* information and mandatory: `built_up_area`, `living_street`, `motorway`, `expressway`, `one_way`,
  `one_way_side`, `dead_end`, `pedestrian_crossing`, `roundabout`, `mandatory_direction`;
* warnings (yellow triangle with a glyph): `roundabout_ahead`, `crossroads_ahead`,
  `side_road_ahead`, `traffic_signals_ahead`, `stop_ahead`, `level_crossing_ahead`,
  `pedestrian_crossing_ahead`, `children_ahead`, `cyclists_ahead`, `tram_crossing_ahead`,
  `animals_ahead`, `roadworks_ahead`, `slippery_ahead`, `uneven_road_ahead`, `road_narrows_ahead`,
  `two_way_ahead`, `curve_ahead`, `lane_change_ahead`, `steep_ahead`, `falling_rocks_ahead`,
  `other_danger_ahead`, `height_restriction_ahead`, `warning_other`.

Any other id is shown as a plate with roadsense's sign code (`B13`), so a rule is never dropped. An
entry roadsense marks uncertain carries a `?`.

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
 "path": {"y": [...]},                         # the model's planned path; may be shorter than "x"
 "lead": {"d": 34.2, "y": 0.3, "v_rel": -1.2, "a": -0.4, "prob": 0.93, "kind": "car"} | null,
 "objects": [{"kind": "car|truck|bus|van|person|bike|cone|lamp", "x": 41.0, "y": -3.4, "state": "on"}],
 "light": {"state": "red|yellow|red_yellow|green|off|unknown", "d": 34.0, "y": 2.1, "flashing": false} | null,
 "limit": {"kmh": 50, "src": "sign|default", "conf": 0.71} | null,
 "signs": {"on":    [{"id": "no_stopping", "s": "B-36", "v": 20, "side": "l|r", "q": true}],   # <= 6
           "ahead": [{"id": "roundabout_ahead", "s": "A-8", "m": 60}]},                         # <= 3
 "env": {"light": "day|dusk_dawn|night", "place": "...", "weather": "..."}}
```

Units and frame as roadsense: metres, x forward, **y right** (the frontend flips it), origin at the
camera. `y` arrays are lateral offsets at the `x` distances. `v_rel` in m/s, negative = closing; `a`
in m/s2, negative = braking. `signs.on[].id` / `.ahead[].id` are roadsense rule ids, `s` the sign
code, `v` a value where the rule has one, `q` set when roadsense marks the entry uncertain, `m` the
expected distance of a warning's hazard in metres.
Every key is optional; a missing key means "not known" and nothing is drawn for it.

### 7.1 Backend feed (`cyberpunk_computer/backend/roadsense_scene.py`)

The backend reads roadsense's WebSocket `ws://127.0.0.1:8090/ws?topics=road,lights,speed_limit,env,rules`
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
| `lead.a` | `road.leads[0].a[0]` | 0.1 m/s2, the model's estimate; the screen lights the lead's stop lamps at -1.5 or below |
| `lead.prob` | `road.leads[0].prob` | 0.01; `lead` is null below 0.3 (the screen draws from 0.5) |
| `path.y` | `road.plan.y` at `road.plan.x` (33 points along the planned trajectory) | linear interpolation at the 11 distances, cut where the plan ends (short at low speed); left out when the plan is unusable |
| `lead.kind` | not provided | not sent: the screen draws a car |
| `objects` | not provided | not sent |
| `light` | `lights.ego` (api annotation): `state`, `dist_m`, `lateral_m`, `flashing` | `d` = `dist_m` (housing-size estimate, about +-30 %), `y` = `lateral_m`: offset from the planned path, not from the camera; null when no light is relevant |
| `limit` | `speed_limit.limit_kmh`, `.source.kind`, `.confidence` | `{"kmh", "src", "conf"}`; null when the limit is unknown |
| `signs.on` | `rules.in_effect[]` with `applies_to_car` not false, without `id: speed_limit` | `id`, `s` = `sign` (8 characters), `v` = `value`, `side` = first letter of `side`, `q` when `uncertain` is not empty; first 6 |
| `signs.ahead` | `rules.ahead[]` (already nearest first) | `id`, `s`, `m` = `expected_in_m` rounded; first 3; `signs` is null when both lists are empty |
| `env` | `env.light/place/weather.label` | the labels as roadsense sends them, no mapping (vocabulary: section 6.2) |

Rate and staleness:

* one scene per new road frame, at most 10 per second (`BACKEND_ROADSENSE_SCENE_HZ`, 1..10); modeld
  runs 10..14 times a second (fewer when it throttles), so the scene comes at 7..10 Hz;
* a road frame is published only once (same `seq` never twice) and only within 1 s of its arrival;
* `light`, `limit`, `signs`, `env` ride along only while their last message is younger than 2 s,
  3 s, 3 s and 5 s (roadsense sends them about 3/s, at least 1/s, at least 1/s and 1/s); otherwise
  the key is left out and the screen falls back to the health mirror (which has no rules);
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
| event on the wire (envelope, compact JSON) | about 0.65 kB with lanes, edges, path and lead; 0.85 kB with light, limit and env; 1.1 kB with four rules and two warnings on top |
| phone -> Pi link | about 11 kB/s (90 kbit/s) at 10 Hz |
| decode on the Pi Zero per event | envelope `json.loads` 0.011 ms + `SceneFeed.push` and `read_inputs` 0.046 ms on the desktop: about 4 ms on the Pi (x70), about 4 % of its core at 10 Hz, plus the scene redraws of section 10 |

The frontend client does not log `roadsense_scene` events (formatting one costs more than decoding
it on the Pi).

## 8. Requests

### 8.1 Backend (this repository)

The event `roadsense_scene` (section 7.1) and the health mirror (`RoadsenseState.health`:
`speed_limit`, `lights`, `env`) cover everything the screen draws today. Open on the car side:

| Item | Type | Meaning |
|---|---|---|
| cruise state | `vehicle.cruise_active`, `.cruise_main_switch`, `.cruise_set_speed`, `.cruise_memory_speed` | exist (solicited 21D3) but are unverified; the cruise slot shows them as they are. The raw frames (`cruise_raw_62c8`, CAN 0x5C8) are being recorded to decode them |
| cruise gap | `vehicle.cruise_gap: Optional[int]` 1..3 | read if present; the car has plain cruise, so this waits for a longitudinal controller |
| high beam | `dynamics.high_beam` / BEAN `lamp_high_beam` | the bit was never seen set on this car; the high-beam fan is drawn when it is |

### 8.2 roadsense

| Request | Field, meaning, units, rate |
|---|---|
| Scene topic | api-derived topic `scene` on `/ws` with exactly the mapping of section 7, 10 Hz: down-sampled lane/edge polylines (11 distances instead of 33), `leads[0]`, the ego light, the limit. Removes the resampling from every consumer and the 15 kB `road` message per model run from the loopback socket (the backend needs 0.6 kB of it) |
| Road edge confidence | `road_edges[i].prob` (or the `y_std` already sent, summarised) 0..1, per model run, so a doubtful edge can be dimmed or left out like a lane line |
| Light distance from the road | `lights.ego` `lateral_m` measured from the camera axis, or the path offset at that distance alongside it, m, 3/s (today it is the offset from the planned path) |
| Other vehicles | `objects[]` entries `{"track_id", "kind", "x" m, "y" m, "v_rel" m/s, "prob"}` for vehicles in adjacent lanes and oncoming, 5..10 Hz. Today modeld has only the lead hypotheses |
| Lead vehicle class and stop lamps | `lead.kind` (`car`/`van`/`truck`/`bus`/`bike`), `lead.braking` bool, 5 Hz |
| Lane line type | per lane line `"type": "solid|dashed|double"`, 2 Hz, so dashes can be drawn and scroll |
| Street lamps and roadside furniture | `objects[]` with `kind: "lamp"`, `x`, `y`, `state: "on|off"`, 2..5 Hz (the renderer draws them already; the demo feeds simulated ones). The roadside of section 6.2 is label-driven decoration until then |
| Stop line | `light.stop_line_m` / a `stop` rule distance in m, 5 Hz |
| Rules in the health summary as data | `/v1/health` `rules` as `[{"id", "sign", "value", "side"}]` instead of display strings, so a consumer without the WebSocket can show the signs (today: `"B-36 no stopping (R)"`) |
| Sign values | `value` for `height_limit`, `weight_limit`, `width_limit` (the rule table says "value unreadable"): number + unit, on change |
| Rule distance | `in_effect[].remaining_m` is sent by roadsense already; a `rules.ahead[].range_m` confidence would let the screen fade a warning that is probably passed |
| Wet road / wipers | `env.road: "dry|wet|snow"` 0.2 Hz, to tint the road surface (weather says what falls, not what lies) |
| Enclosed-place exit | `env.exit_in_m` for `tunnel` (portal light ahead), m, 1 Hz, so the tunnel can show its end |
| Vulnerable road users | `objects[]` with `kind: "person" / "bike"`, 5..10 Hz |

## 9. Future data / sensors

Wanted by the design, not available from the car or roadsense today, and therefore not drawn:

| Element | Needs |
|---|---|
| traffic in adjacent lanes, oncoming traffic | roadsense object tracks (8.2) |
| lead vehicle type; its real stop lamps (today: the model's deceleration estimate) | roadsense (8.2) |
| dashed / solid lane lines | roadsense lane line type |
| street lamps, bollards, barriers as detected objects | a detector in roadsense (drawing code exists: `objects` kind `lamp`) |
| stop line at a red light or stop sign | roadsense |
| pedestrians, cyclists, cones | roadsense object classes (drawing code exists) |
| values on height / weight / width limit signs | roadsense sign reading |
| end of a tunnel ahead, wet road | roadsense env (8.2) |
| rear-left / rear-right / back door separately, bonnet | body switches the BEAN frame does not separate; a satellite input or a further BEAN decode |
| turn signals, hazard lamps, reversing lamp, rear fog | not decoded on CAN/BEAN yet |
| verified cruise set speed and state | decode of BEAN 62:C8 / CAN 0x5C8 (the slot is in place) |
| adaptive-cruise gap and target | the car has plain cruise (the gap bars are reserved in the slot) |
| blind-spot markers | side radar or a rear/side camera |
| traffic behind | a rear camera in roadsense |
| parking distance arcs in the parked view | ultrasonic sensors |
| steered front wheels in the parked view | `dynamics.steering_angle` calibrated in degrees |
| speed-limit source by map | GPS + the OSM speed-limit service |

New elements go in as a layer function in `scene/render.py` (the layer list is in its docstring) fed
by a field of `SceneInputs`; world objects only need a `kind` branch in `_draw_object`, a new sign a
function in `signs.py`, a new place a `_place_<label>` method in `scenery.py`.

## 10. Performance budget

Measured on a desktop (i7-8850H, pygame-ce 2.5.8, `SDL_VIDEODRIVER=dummy`), whole
`MainScreen.update + render` per frame; the Pi estimate uses x70 (the Pi ran the first version of the
screen, 0.56 ms here, at about 21 fps = 47 ms with roughly 7 ms of that in the framebuffer write;
treat it as x50..x85). Averages assume 20 UI frames and 10 scene redraws a second.

| Case | Desktop | Pi Zero estimate |
|---|---|---|
| main screen before the scene existed (every frame) | 0.56 ms | 39 ms |
| parked, steady (cached blits) | 0.31 ms | 22 ms |
| driving, no vision | 0.31 ms + 0.07 ms per scene redraw | 24 ms average |
| driving, city at night: lead, four lamp objects, four signs, low beam | 0.32 ms + 0.21 ms per redraw | 30 ms average |
| the same in rain | 0.32 ms + 0.22 ms per redraw | 30 ms average |
| forest at night in fog with high beam; tunnel | 0.32 ms + 0.28 ms per redraw | 32 ms average |
| one car render (cache miss): camera step, door step or lamp change | 0.4..0.75 ms | 30..50 ms, once per sprite |
| a backdrop strip on a label change | 0.1..0.45 ms | 7..30 ms, once per (place, light, weather) |
| rain / snow tile, fog band, a beam sprite, a sign pictogram (first use) | 0.85 / 0.15 / 0.13 / 0.006 ms | 60 / 10 / 9 / 0.4 ms, once |
| first scene frame: mesh, 15 cameras, first car | 2.6 ms | about 0.2 s, once |
| import of `ui/scene` | 4.1 ms | about 0.3 s, after the first frame is shown, not during start-up |

Rules that keep it there:

* the scene surface is redrawn only when its signature changes or while something animates, and then
  at most every 95 ms; a parked car costs one opaque 238 x 216 blit per frame;
* the own car is a colour-keyed (RLE) sprite cached per (camera step, door steps, lamps), 64 entries;
  the 15 camera steps of the swing are pre-rendered one per 0.4 s while the scene is steady, for
  the current lamps with stop lamps off and on, so the swing itself renders no car;
* backdrops are strips rendered once per label combination (six kept) and blitted with a pan offset;
  beam fans are one sprite per kind; rain and snow are one tile and one blit; fog is one band;
  sign pictograms are cached surfaces;
* halos are small pre-rendered sprites blitted additively; there is no per-pixel alpha and no
  full-surface blend anywhere;
* text: `ui/fonts.py::CrispFont.render` caches every rendered string per font (256 entries), for
  every screen of the UI. This took the six side panels from 0.34 ms to 0.18 ms a frame and is the
  largest single saving (the corner ticks and the ambient swatch of section 2 add 0.05 ms back);
* the chase view projects nothing per point (section 5);
* `MainScreen` builds the scene after its first frame; importing `main_screen` no longer imports
  `ui/scene`;
* no numpy.

## 11. Demo and screenshots

```
python -m cyberpunk_computer.ui.scene.demo --shots OUT_DIR [--copy-to DIR]   # headless PNGs
python -m cyberpunk_computer.ui.scene.demo --live --scale 3                  # the timeline in a window
python -m cyberpunk_computer.ui.scene.demo --bench                           # frame cost incl. the simulation
```

`--shots` writes `NN_name.png` (native 480 x 240) and `NN_name@4x.png` for 45 cases: the states of
section 4, the five lamp states on the road and three parked, 17 scenery variants covering every
place, light and weather label, a full sign column, the cruise slot states, and `signs_legend`
with every pictogram. `--copy-to` also writes the 4x images there as `mfd2_NN_name.png`.

Timeline (44 s): parked, doors open and close, READY and lamps, pull away, lead vehicle closing in,
limit sign and rules, left curve at night with street lamps, red light with the lead braking, stop,
the scene closes. Vehicle values go through the real state fields; the road goes in as
`roadsense_scene` events; `mods` override labels, signs, lamps and cruise for the variant shots. The
street lamps of the demo are simulated data for the `lamp` object kind (section 9).

