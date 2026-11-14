# Satellites: multi-transport discovery, trust and a single API

Contract between the backend, `prius-bled`, `prius-satkey` and satellite firmware.
The RS485 rail, wake-locks and RS485 job queue are in `SATELLITE_POWER.md`.

A **satellite** is any small device that talks NDJSON with the car computer:
the RS485 nodes behind the gateway (DRL 106, rain/light 107, VFD 110), BLE
nodes that connect to the car's advertisement, and Wi-Fi nodes that join the
`prius` access point. The rest of the backend (rules, API, MFD, dashboard)
addresses a satellite by its **satellite id** and never sees the transport.

```
                 ┌──────────────── SatelliteRegistry ────────────────┐
 rules / API ───►│ send(sat_id, payload)  nodes  discovered  pairing │
                 │ keychain (trust)       per-transport job queues   │
                 └───────┬──────────────────┬──────────────────┬─────┘
                         │                  │                  │
                  Rs485Transport      BleTransport       WifiTransport
                  gateway serial      prius-bled         UDP beacon 8787
                  relay ch5 rail      (BlueZ D-Bus)      TCP NDJSON 8788
                  ids 100–199         ids 300–399        ids 400–499
```

## 1. Identity and ids

* `sat_id` is a stable integer assigned at enrolment and stored in the
  keychain. Ranges: RS485 `100–199` (fixed by firmware), BLE
  `300–399`, Wi-Fi `400–499`. Ids never move between transports.
* `name` is a short slug (`drl`, `garage`, `trailer`), unique in the keychain.
* Cryptographic identity: every authenticated satellite holds an **Ed25519
  key pair**. Its public key's fingerprint `fp` = first 16 bytes of
  SHA-256(pubkey), base32 lower-case without padding (26 chars) — this is the
  stable handle shown in logs and the dashboard.

## 2. Keychain (trust)

Root of trust = the **car key**, an Ed25519 key pair generated once by
`prius-satkey init`:

| File | Content | Mode |
|------|---------|------|
| `/etc/prius/satellites/car.key` | car private key (PEM) | root 0600 |
| `/etc/prius/satellites/car.pub` | car public key (PEM) | 0644 |
| `/etc/prius/satellites/keychain.json` | enrolled satellites + revocations | root 0600 |

Bench/dev default: `BACKEND_SATELLITE_KEYCHAIN_DIR` (defaults to
`/etc/prius/satellites`; the backend falls back to `./satellites/` with a
warning when that is not writable, e.g. on the P52).

`keychain.json`:

```json
{
  "version": 1,
  "car_fp": "…",
  "satellites": {
    "300": {"name": "garage", "transport": "ble", "fp": "…", "pubkey": "<base64 raw 32 B>",
            "address": "AA:BB:CC:DD:EE:FF", "caps": ["relay", "temp"],
            "enrolled": 1762700000, "cert": {…}, "auth": "ed25519"},
    "301": {"name": "door", "transport": "ble", "ble_role": "beacon",
            "address": "AA:BB:CC:DD:EE:B1", "…": "…"},
    "106": {"name": "drl", "transport": "rs485", "auth": "none"}
  },
  "revoked": ["<fp>", "…"]
}
```

* `auth: "none"` is allowed **only** for `rs485` (wired, inside the car). BLE
  and Wi-Fi satellites must authenticate (`require_auth` per transport,
  default `rs485: false, ble: true, wifi: true`).
* **Certificate** = the car's signature over the canonical JSON of
  `{"v":1,"sat_id":300,"name":"garage","transport":"ble","fp":"…","pubkey":"…","caps":[…],"issued":…,"expires":null}`
  (`json.dumps(obj, sort_keys=True, separators=(",", ":"))`, UTF-8). A
  satellite that presents a valid cert is trusted even if the car has never
  seen it (so a satellite can be signed on the laptop with a copy of the car
  key and simply show up); the registry then adds it to `keychain.json`.
  Revoked fingerprints are refused regardless of cert.
* `prius-satkey` (POCO/rootfs/usr/local/sbin, plain Python 3 + `cryptography`,
  runs on the phone and on the P52):
  * `init` — create car.key/car.pub if missing, print `car_fp`;
  * `list` — table of enrolled satellites + revocations;
  * `new <name> --transport ble|wifi [--id N] [--caps a,b]` — generate a
    satellite key pair + cert, print the **provisioning blob** (JSON with the
    private key, cert, car public key) to flash into the satellite, and add
    the entry to the keychain. `--id` picks a free id in the range otherwise;
  * `sign --pubkey <b64> <name> --transport …` — same without generating the
    key (the satellite made its own);
  * `new`/`sign` take `--ble-role scanner|beacon` (BLE only, default
    scanner) and `--address` (required for a beacon: the car connects to it;
    see §4 "BLE roles");
  * `add-rs485` (keychain entry for a wired node), `show` (print a provisioning
    blob, §8), `revoke <id|fp|name>`, `forget <id>`, `export-pub`.

## 3. Authentication handshake (transport-agnostic, NDJSON)

Runs on the satellite channel right after a link comes up (BLE connection,
Wi-Fi TCP session, or — optional — RS485 discovery). Payloads are the `d`
object of the usual envelope.

```
car → sat  {"a":"auth","v":1,"nonce":"<b64 32 B>","car":"<car_fp>"}
sat → car  {"a":"auth","v":1,"fp":"<fp>","pubkey":"<b64>","cert":{…}|null,
            "nonce":"<b64 32 B>","sig":"<b64 Ed25519 over MSG>"}
car → sat  {"a":"auth_ok","sat_id":300,"sig":"<b64 car sig over MSG2>"}   or   {"a":"auth_fail","why":"…"}
```

* `MSG  = b"prius-sat-auth-v1" + car_nonce + sat_nonce + car_fp + sat_fp + transport`
  (all ASCII/bytes concatenated, fingerprints as their 26-char strings).
* `MSG2 = b"prius-car-auth-v1" + sat_nonce + car_nonce + sat_fp + car_fp`
  — lets the satellite verify it talks to its own car (mutual auth).
* Accept iff: `fp` matches `pubkey`; not revoked; and (`fp` is enrolled in
  the keychain **or** `cert` verifies against `car.pub` and matches fp/
  transport); and `sig` verifies. Replay-safe by the fresh car nonce.
* Timeout 10 s, then disconnect. Unknown devices while the pairing window is
  **open** are kept in `discovered` with `trusted=false` and their offered
  `pubkey`, so the operator can enrol them (`satellite_enrol`); otherwise
  they are disconnected immediately.
* Before `auth_ok`, nothing but `auth` is accepted from the device and
  nothing is sent to it.
* BLE adds link-layer security: LE Secure Connections bonding (Just Works),
  the GATT characteristics require an encrypted link. App-level auth stays
  mandatory on top (bond theft is not enough).

## 4. Transports

Common interface: `cyberpunk_computer/io/satellite_transport.py`
(`SatelliteTransport`, `DiscoveredDevice`, `TransportStatus`, `LinkEvent`).
Every transport is driven from the backend engine loop (`tick(now)`), never
owns a thread that touches the store, and reports:

* **discovery**: `discover(force)` + periodic rediscovery with a per-transport
  interval (`satellite_rediscover_s`, default 300 s; immediately on an
  "availability" edge: RS485 rail up, bled connected, AP up); `discovered()`
  lists devices seen in the last `discovery_ttl_s` (default 600 s);
* **links**: `LinkEvent(kind=up|down|data, address, line)` from `poll()`;
* **egress**: `send(address, line)`;
* **status**: `TransportStatus(available, state, detail, last_discovery,
  devices_seen)`.

### RS485 (`Rs485Transport`)
Wraps the existing gateway `OutputPort` + ingress. Address = the device id
as a string. Presence stays passive (any ingress refreshes), plus
rediscovery = `{"a":"status"}` poll of every keychain RS485 id and of the
`satellite_poll_devices` list, sent through the RS485 job queue (so the rail
wake-lock is taken). Auth: `none` unless the keychain entry says
otherwise.

### BLE (`BleTransport` ↔ `prius-bled`)
The backend does **not** touch BlueZ. `prius-bled` (POCO/rootfs/usr/local/sbin,
Python 3, `dbus-python` + GLib, root, `prius-bled.service`) owns `hci0`:

* **Peripheral role, always.** Advertises with the car's fixed public address
  `02:00:E8:51:02:58` (set by pmOS `bootmac`, stable across boots):
  connectable, local name `PRIUS`, service UUID
  `580e04f7-d4c0-458b-b2e0-4ebb01a960c8`, interval 10 000–10 240 ms by
  default (`adv` command changes it; 1 000 ms while a pairing window is
  open or when ACC is on), manufacturer data 2 bytes = flags (bit0 ACC on,
  bit1 pairing open). Rotating (RPA) addresses are **off**.
* **GATT service** `580e04f7-…` with two characteristics:
  * `…-0001` TX: car → satellite, `notify` (+ `read`), encrypted;
  * `…-0002` RX: satellite → car, `write` / `write-without-response`,
    encrypted.
  Payload = NDJSON lines, UTF-8, `\n`-terminated, may span writes (bled
  reassembles per connection; a line > 4 kB closes the link).
* **Optional short scans** (`scan` command, ≤ 10 s) list nearby devices
  advertising our service UUID — for "available" satellites that have not
  connected yet. No continuous scanning (power).
* Pairing agent: `NoInputNoOutput`, `Pairable` only while the backend's
  pairing window is open.
* **Control socket** `/run/prius/bled.sock` (Unix stream, NDJSON both ways,
  one client = the backend; bled keeps running without a client):

  | direction | message |
  |-----------|---------|
  | bled → backend | `{"ev":"hello","adapter":"hci0","address":"02:00:…","adv":{…}}` on connect |
  | bled → backend | `{"ev":"link","state":"up"\|"down","addr":"…","name":"…","bonded":true,"mtu":247}` |
  | bled → backend | `{"ev":"data","addr":"…","line":"<one NDJSON line without \n>"}` |
  | bled → backend | `{"ev":"scan","devices":[{"addr":"…","name":"…","rssi":-60,"uuids":[…]}]}` |
  | bled → backend | `{"ev":"status","powered":true,"advertising":true,"links":[…],"pairable":false}` (on change and every 30 s) |
  | backend → bled | `{"cmd":"send","addr":"…","line":"…"}` → `{"ack":"send","ok":true}` |
  | backend → bled | `{"cmd":"disconnect","addr":"…"}` |
  | backend → bled | `{"cmd":"scan","seconds":5}` |
  | backend → bled | `{"cmd":"adv","interval_ms":10000,"flags":1}` |
  | backend → bled | `{"cmd":"pairable","seconds":120}` (0 = close) |
  | backend → bled | `{"cmd":"status"}` |
  | backend → bled | `{"cmd":"central","enabled":true,"addresses":["AA:…"],"scan_s":8,"period_s":20}` (central role, see "BLE roles") |

  `BleTransport` reconnects to the socket with backoff; `available=false`
  while bled is unreachable.

#### BLE implementation details (prius-bled)

* **UUIDs**: TX = `580e04f7-d4c0-458b-b2e0-4ebb01a90001`, RX =
  `580e04f7-d4c0-458b-b2e0-4ebb01a90002` (service UUID with its last 4 hex
  digits replaced). GATT flags: TX `encrypt-read` + `encrypt-notify`; RX
  `encrypt-write` + `write-without-response` (BlueZ has no
  `encrypt-write-without-response` flag; the attribute's encrypted-write
  permission applies to write commands too, which are dropped on an
  unencrypted link).
* **Advertisement**: BlueZ `LEAdvertisingManager1.RegisterAdvertisement`,
  `Type=peripheral`, `Discoverable=true`, `MinInterval`/`MaxInterval`.
  `MaxInterval = MinInterval + 240 ms` (capped at 10 240); the requested
  interval is clamped to 20–10 240 ms. Manufacturer data = company `0xFFFF`
  + the 16-bit flag word **little-endian** (`01 00` = ACC on). The AD stays
  at 27 bytes (flags + UUID128 + mfr) so BlueZ uses **legacy ADV_IND PDUs**
  (visible to any scanner); `PRIUS` goes in the scan response (verified with
  btmon on the P52, BlueZ 5.85). The advertisement is replaced (unregister +
  register) on every change; `advertising` stays true across a replacement.
  Fallback if bluez rejects the D-Bus advertisement: `btmgmt
  add-ext-adv-params -r MIN -x MAX -c -g 1` + `add-ext-adv-data` (instance 1),
  logged as `btmgmt fallback advertisement`.
* **Pairing window**: while open, bled itself sets flag bit1 and caps the
  interval at 1 000 ms (the backend's `adv` values are kept and restored when
  the window closes). `Adapter1.Pairable` (= mgmt Bondable) follows the
  window; the agent (`NoInputNoOutput`, registered as default agent) accepts
  Just Works / authorization only while it is open (bonded devices may always
  use the service). `pairable.seconds` is clamped to 0–600.
* **Links**: a connected central counts as a satellite link when its Device1
  `UUIDs` contain our service UUID, or at its **first RX write**. A satellite
  must therefore write one line — a bare `\n` is enough and is not forwarded —
  after subscribing to TX; the car then starts the auth handshake. `mtu` in
  `link`/`status` is the ATT MTU BlueZ passes with each write (23 until the
  first write). Link down = Device1 `Connected=false`, bluetoothd gone, or the
  adapter powered off.
* **TX addressing**: the BlueZ D-Bus GATT
  server has no per-device notify, so one TX characteristic is shared and
  every notified line is framed as `<ADDR> <line>\n` — ADDR = destination
  device address exactly as BlueZ reports it (17 chars, upper-case,
  colon-separated), one space, then the NDJSON line. The satellite compares
  the first 17 bytes with its own (static/public) address and drops lines
  that are not for it. The backend and `BleTransport` are unchanged (they
  still `send {addr, line}`); bled adds the prefix. Chunking (smallest link
  MTU − 3) applies to the framed line; the 4 kB cap applies to the line
  without the 18-byte prefix. RX (satellite → car) is not framed.
  * **Subscription**: BlueZ itself notifies only devices that enabled the TX
    CCC. bled cannot track subscription per device: `StartNotify()` /
    `StopNotify()` take no arguments (no `device` option; only
    `AcquireNotify` has one, and it hands out one fd per characteristic, not
    per link), so "only notify subscribed links" is BlueZ's guarantee, not
    bled's bookkeeping.
  * **Caveat**: other authenticated satellites of the same car connected at
    the same time can read this traffic — the links are encrypted, but the
    notifications are shared (open gap, §9).
* **Control socket**: newest client wins (a second connection replaces the
  first). Every command gets an ack `{"ack":<cmd>,"ok":bool[,"error":"…"]}`
  (errors: `not linked`, `not connected`, `scan in progress`, `radio down`,
  `notify failed`, parse errors). `status` also carries `pairable_left`,
  `scanning` and `adv` (`interval_ms`, `max_interval_ms`, `flags`,
  `requested_interval_ms`, `name`, `uuid`, `active`); `hello.adv` is the same
  object. Status is emitted on change (ignoring the `pairable_left`
  countdown) and every 30 s. `scan.seconds` is clamped to 1–10.
* **Lifecycle**: bled tracks the `org.bluez` name owner and re-registers
  agent, GATT app and advertisement (and re-applies Pairable) when bluetoothd
  returns; on SIGTERM it unregisters everything, stops discovery and
  restores the adapter's `Pairable` as found at start.
* **BleTransport**: `available` once bled said `hello`; `state` =
  `pairing` / `advertising` / `down` (bled unreachable, adapter off, or not
  advertising), `detail` = the effective interval (`"10 s"`, `"1 s"`) or the
  reason. `discover(force)` = a 5 s scan, at most every 60 s unless forced;
  links reported by a fresh `status` are reconciled into `up`/`down` events
  (after a bled restart). Optional `rediscover_s` makes `tick()` scan on its
  own; by default the registry drives rediscovery.

### BLE roles: car-advertises and satellite-advertises

Two satellite kinds, chosen per satellite at enrolment (`keychain.json`
`ble_role`: `"scanner"` default, or `"beacon"`):

| | **scanner satellite** (mains/12 V powered) | **beacon satellite** (coin cell, passive) |
|---|---|---|
| who advertises | the **car** (always, 10 s; 1 s with ACC on or pairing open) | the **satellite** (its own interval, our service UUID + its name) |
| who scans + connects | the satellite (GAP central) | the **car**, only while the power profile is `full` (ACC on, or a pairing window open) — never in `low` |
| GATT server | the car (`prius-bled`, service `580e04f7-…`, TX notify car→sat with the 17-char address prefix, RX write sat→car) | the satellite: the **same service and characteristic UUIDs**, mirrored: TX (`…0001`, notify) = sat→car, RX (`…0002`, write) = car→sat; no address prefix (one client) |
| auth | car sends the challenge first on every link, both cases (docs §3) | same |
| discovery listing | connected-but-unknown devices while pairing is open | car's scan results (RSSI, name) while in `full`; enrolled beacons are connected as soon as seen |

`prius-bled` therefore runs both roles on hci0: peripheral always; central
(scan + connect + GATT client) only when the backend sends
`{"cmd":"central","enabled":true,"addresses":[...]}` (enrolled beacon
addresses to connect to; empty list = scan-and-report only). The backend
sends `enabled=true` when the power profile is `full` or a pairing window is
open, `false` otherwise; `BleTransport.set_central(enabled, addresses)` is the
hook. A beacon link surfaces to the backend exactly like a scanner link
(`link up/down`, `data`), so the registry/auth/queue code is identical.

Power: continuous scanning ≈ 10 mA at the phone, only with ACC on, so free;
in `low` the car just advertises (< 0.1 mA) and beacon satellites wait.

#### Central role implementation (prius-bled)

* **Command** `{"cmd":"central","enabled":bool,"addresses":[…],"scan_s":8,"period_s":20}`
  → ack `{"ack":"central","ok":true}`. `enabled` must be a JSON boolean
  (default false); `addresses` = up to 32 MACs (normalised to upper-case
  colon form, de-duplicated; empty = scan and report only); `scan_s`
  clamped to 1–60, `period_s` to `scan_s`–300 (`scan_s == period_s` =
  continuous). The command is idempotent: re-sending the same values (the
  backend does after every reconnect) does not restart the duty cycle;
  changing `addresses` drops central links/attempts to addresses no longer
  listed; `enabled:false` stops discovery and disconnects every central link
  and attempt.
* **Discovery duty cycle**: 8 s of discovery every 20 s by default (40 %).
  BlueZ/kernel LE discovery scans at a ~100 % window while on and the single
  controller also serves our advertising and GATT-server links; 40 % leaves
  it free for connection events most of the time while a beacon advertising
  every ≤ 2 s is still caught in every scan phase (first connect ≤ ~12 s +
  connect time). Discovery = `Adapter1.SetDiscoveryFilter({UUIDs:[service],
  Transport:"le", DuplicateData:true})` + `StartDiscovery`, shared with the
  one-shot `scan` command (one D-Bus client = one BlueZ discovery session,
  so bled starts it while either wants it and stops it, with the filter
  cleared, when neither does). The advertisement stays registered throughout.
* **Sightings** (any device advertising our service while discovery is on)
  go to the backend as the usual `{"ev":"scan","devices":[{addr,name,rssi,uuids}],"central":true}`,
  at most one event per 10 s (devices seen since the previous event,
  newest RSSI) — `BleTransport` turns them into its normal discovery list.
* **Connect** (sighted address ∈ `addresses`, not linked, not within 5 s of a
  failure / link down): `Device1.Connect()` → wait for
  `ServicesResolved=true` → find our service + `…0001`/`…0002` on the remote
  (`ObjectManager.GetManagedObjects`) → if not `Paired`: `Device1.Pair()`
  (Just Works) → `GattCharacteristic1.StartNotify()` on the remote TX →
  link up. Setup timeout 30 s (then `Disconnect`, retry on a later
  sighting). BlueZ reports `Connected=true` during setup; bled does not treat
  that device as a peripheral link.
* **Data**: remote TX notifications arrive as `PropertiesChanged(Value)` on
  the remote characteristic and are reassembled into lines exactly like RX
  writes (4 kB cap, blank lines dropped) → `{"ev":"data",…}`. `send` to a
  central link = `GattCharacteristic1.WriteValue(chunk, {"type":"command"})`
  on the remote RX when it has `write-without-response`, `"request"`
  otherwise; chunks of MTU − 3 bytes, **no address prefix**; MTU from the
  remote characteristic's `MTU` property (BlueZ ≥ 5.62; 23 otherwise).
  The peripheral side's notify chunking uses the smallest MTU of
  **peripheral** links only.
* **Events**: `link` (up/down) and every `status.links[]` entry now carry
  `"role":"peripheral"|"central"`; `status` gains
  `"central":{"enabled","scanning","targets","links"}` (a scan-phase flip
  alone does not emit a status; the 30 s periodic one carries it).
* **Pairing policy**: the agent accepts Just Works pairing/authorization when
  the pairing window is open **or** the device is in the current central
  `addresses` while central is enabled; anything else is rejected. For our
  own `Pair()` the kernel confirms Just Works itself (we are the initiator),
  so the agent is mostly consulted only on re-pairing. Because the kernel
  asks for a *bond* only while the adapter is bondable, bled holds
  `Adapter1.Pairable=true` for the duration of a central `Pair()` and
  restores it afterwards (incoming pairing during that hold still goes
  through the agent above). The bond is what lets the encrypt-only remote
  characteristics work on later connections without re-pairing.
* **Lifecycle**: radio loss drops central links (link down) and pauses the
  cycle; bluetoothd/adapter return restarts it if enabled. Shutdown
  disconnects central links, stops discovery and restores `Pairable`.
* **Beacon satellite contract** (for firmware): advertise connectable with
  the service UUID `580e04f7-…60c8` (+ a name); serve the same service with
  TX `…0001` notify (sat → car; the car subscribes) and RX `…0002` write and/or
  write-without-response (car → sat), both encrypted; allow Just Works
  bonding initiated by the car; NDJSON lines, `\n`-terminated, may span
  notifications/writes; the car sends the auth challenge first (§3). Use a
  static/public address: the keychain stores it and the car connects by it.
* **Backend**: `SatelliteTransport.set_central(enabled, addresses)` (no-op
  default); `BleTransport` sends it and re-sends it after every bled
  `hello`; its status `detail` gets `, central` while bled reports central
  enabled. The registry computes `enabled = power_mode == "full" or acc_on
  or a BLE pairing window open` and `addresses` = keychain BLE entries with
  `ble_role: "beacon"` and an `address` (rechecked every 1 s, immediately on
  pairing open/close), and calls `set_central` on change only.
* **Keychain**: optional per-entry `"ble_role": "scanner"|"beacon"` (absent =
  scanner); a beacon needs `"address"` (validated, normalised). The
  provisioning blob's `ble` section carries `role` (+ `address`).
  `prius-satkey new|sign … --transport ble --ble-role beacon --address AA:BB:CC:DD:EE:FF`;
  `list` marks beacons. The cert format is unchanged (role/address are not
  signed; the car's own keychain is authoritative for them).

### Wi-Fi (`WifiTransport`)
Satellites are clients of the `prius` AP (`10.42.0.1/24`, NM shared).

* Discovery: satellites broadcast `{"prius_sat":1,"fp":"…","name":"…","port":8788}`
  to UDP `255.255.255.255:8787` every 10 s; the backend also answers a
  `{"prius_car":1}` broadcast with `{"prius_car":1,"car_fp":"…","port":8788}`
  so a satellite can find the car.
* Session: the **car connects** to the satellite's TCP 8788 (or accepts a
  satellite connecting to the car's 8788 — both allowed), NDJSON lines, auth
  handshake first, keep-alive `{"a":"ping"}` every 15 s, dead after 45 s.
* nftables: 8787/udp and 8788/tcp are accepted on `wlan*` only
  (`POCO/rootfs/etc/nftables.d/50_prius_backend.nft`).

## 5. Registry, queues, state

`backend/satellite_registry.py` — `SatelliteRegistry` owns the transports,
the keychain and the auth state machine per link, and exposes:

* `send(sat_id, payload, priority=…) -> job`, routed to the transport's own
  `SatelliteJobQueue` (RS485 keeps the rail wake-lock behaviour; BLE/Wi-Fi
  queues have no rail and only wait for the link to be authenticated);
* `nodes()`, `discovered()`, `pairing` window, `enrol(...)`, `forget(...)`,
  `discover(transport=None)`;
* the existing `SatelliteSupervisor` (presence ageing, config re-push) and
  `SatelliteScheduler` work on registry nodes regardless of transport.

`SatellitesState` (serialized under `satellites` in `/api/v1/state`):

```jsonc
{
  "nodes": { "106": {...SatelliteNode}, "300": {...} },     // enrolled (keychain) satellites
  "power_holders": [...], "power_requested": false,         // RS485 rail, unchanged
  "queue_depth": 0, "active_job": "",                       // sum / any, for old UI
  "transports": {   // always all three; state "disabled" / "unavailable" when not running
    "rs485": {"available": true,  "state": "rail-off", "detail": "", "last_discovery": 0, "queue_depth": 0, "active_job": "",
              "require_auth": false, "devices_seen": 3},
    "ble":   {"available": true,  "state": "advertising", "detail": "10 s", "last_discovery": 1762.., "queue_depth": 0, "active_job": "",
              "require_auth": true, "devices_seen": 0},
    "wifi":  {"available": false, "state": "ap-down", ..., "require_auth": true}
  },
  "discovered": [   // seen, not enrolled ("available")
    {"transport": "ble", "address": "…", "name": "…", "fp": "…"|null, "rssi": -61,
     "last_seen": 1762.., "linked": true, "pubkey": "…"|null, "sat_id": 300|null}
  ],
  "pairing": {"open": false, "until": 0, "transport": null, "opened_at": 0},
  "last_command": {"op": "enrol", "ok": false, "error": "pairing window is closed", "ts": 1762..},
  "last_update_time": 1762..
}
```

Field rules (the dashboard relies on them):

* `transport` is exactly one of `rs485|ble|wifi`; `caps` is always a list of
  strings; `rssi` a number or null.
* RS485 entries in `discovered` are wired ids seen on the bus but not in the
  keychain; their `address` is the device id as a string (the UI prefills
  the id from it).
* `discovered[].sat_id` is set when the device presented a valid
  certificate for that id (else null); `pubkey` is set when the device proved
  possession of an unknown key while the pairing window was open (enrolable).
* `pairing.opened_at` = `time.time()` when the window opened, 0 when closed.
* `transports[<name>].require_auth` mirrors the per-transport auth policy.
* `last_command` is the result of the last operator command
  (`satellite_discover/pair_open/enrol/forget/revoke`): REST commands are
  fire-and-forget actions, so a refusal (window closed, id taken, nothing to
  revoke …) is reported here and in the journal. `enrol` adds `sat_id`.

`SatelliteNode` gains: `transport`, `address`, `fp`, `trusted` (in keychain),
`authenticated` (handshake done on the current link), `linked` (link up),
`rssi`, `caps`. `online` keeps its meaning (traffic recently, usable): for
BLE/Wi-Fi it implies `authenticated`. RS485 nodes are `trusted` by cabling
(also when not in the keychain), `authenticated=false` (no handshake),
`linked` = `online`, `address` = the id as a string.

## 6. REST commands (`api/commands.py`)

| command | params | effect |
|---------|--------|--------|
| `satellite_send` | `device_id, payload, priority?` | unchanged, now transport-agnostic (`device_id` 100–199 or 300–499) |
| `satellite_power_hold` | `name, on` | unchanged (RS485 rail) |
| `satellite_discover` | `transport?` | force rediscovery now |
| `satellite_pair_open` | `seconds` (default 120, 0 closes), `transport?` | pairing window |
| `satellite_enrol` | `transport, address, name, sat_id?, caps?` | enrol a discovered device that offered a pubkey (BLE/Wi-Fi: window must be open); RS485: just name + id (`address` = id), **no window needed** (wired, trusted by cabling) |
| `satellite_forget` | `sat_id` | remove from keychain, disconnect (a valid cert can bring it back) |
| `satellite_revoke` | `sat_id` | revoke fp, disconnect; on a node without `fp` (auth none, RS485) it is a no-op error `"nothing to revoke"` |

All five registry commands map to one `SatelliteRegistryCommandAction(op, params)`
routed by the backend middleware; results land in `satellites.last_command`.

## 7. Power

Advertising at 10 s < 0.1 mA; one connected BLE satellite polled at ~1 Hz
< 0.5 mA; scans are short bursts only. The `low` profile leaves bled and the
Wi-Fi listener running; Wi-Fi satellites are only reachable while the AP is
up (wifi-grace), which the `wifi` transport reports as `ap-down`.

## 8. Implementation details

Code: `backend/satellite_keychain.py`, `backend/satellite_auth.py`,
`backend/satellite_registry.py`, `io/rs485_transport.py`,
`io/wifi_transport.py`, `POCO/rootfs/usr/local/sbin/prius-satkey`.

**Wire envelope (BLE/Wi-Fi).** One JSON object per line:
`{"id": <sat_id>, "d": {...}}` in both directions; `id` is omitted until
`auth_ok`. A received line without `"d"` is taken as the payload itself
(lenient for small firmware). RS485 keeps the gateway envelope.

**Certificate format.** The cert object is the signed body plus `"sig"`:
`{"v":1,"sat_id",…,"expires", "sig":"<b64>"}`; the signature covers the
canonical JSON of the cert **without** its `sig` key. `expires` is
`null` or a unix time.

**Provisioning blob** (`prius-satkey new` / `sign` / `show`):
`{"v":1,"sat_id","name","transport","fp","pubkey","privkey"(new only),
"cert","car_pub","car_fp", "wifi":{"ssid":"prius","car_ip":"10.42.0.1",
"port":8788,"beacon_port":8787} | "ble":{"service","car_addr"}}`; keys are
standard base64 of the raw 32 bytes. `prius-satkey new -o FILE` writes it
0600.

**Handshake details.**
* Proof of possession comes first: an unknown device is held for pairing
  only after its signature over this session's car nonce verified.
* Held links (pairing window open) are exempt from the 10 s timeout; when
  the window closes they get `auth_fail "pairing window closed"` and are
  dropped. A proven offer stays enrolable for 600 s or until the window
  closes and its link is gone.
* After `satellite_enrol` of a held link the car sends `auth_ok` on the same
  session, carrying the new `"cert"` so the satellite can store it.
* A cert whose `sat_id` is enrolled with a different fp is refused; a cert
  whose `name` is taken is accepted as `<name>-<id>`.
* With no car key (`prius-satkey init` not run) or without `cryptography`,
  BLE/Wi-Fi links get `auth_fail "car has no key"`. The backend never
  creates the car key itself.
* Revoke removes the entry and lists its fp (refused forever); forget only
  removes the entry.

**Keychain sharing.** Backend and CLI serialize every write with `flock` on
`<dir>/.keychain.lock` and atomic replace; the backend re-reads the files
within 2 s of a CLI change (new entries become nodes, removed/revoked ones
lose their links and nodes).

**RS485.**
* Background rediscovery runs only while the powerbox **confirms** the rail
  is up (`powerbox.connected && rs485_power`): on that edge and every
  `satellite_rediscover_s` after. It never powers the rail (and the gateway
  hub bonded to it) by itself with the key off. `satellite_discover`
  (operator) does power it, through the queue wake-lock as before.
* Candidate ids = keychain RS485 ids ∪ `satellite_poll_devices` ∪ the known
  firmware ids (106, 107, 110).
* The RS485 queue, ACC hold, linger, UI rail toggle (`set_relay` ch5 →
  `manual:ui`) and `satellite_send` to 100–199 behave exactly as in
  `SATELLITE_POWER.md`.

**Queues.** One `SatelliteJobQueue` per transport. BLE/Wi-Fi command jobs
carry `requires_online=(sat_id,)` (= authenticated link) and fail after the
usual 20 s ready timeout; they never touch `power_holders`. Each queue
reports `transports[<name>].queue_depth/active_job`; the legacy top-level
`queue_depth` is their sum and `active_job` the first non-empty one.
Periodic polls of BLE/Wi-Fi ids in `satellite_poll_devices` run on their own
queue.

**Presence.** RS485 nodes age on traffic + rail as before. BLE/Wi-Fi nodes
follow the authenticated link: online on `auth_ok` (offline→online bumps
`boot_id` → config re-push through their queue), offline on link down. Any
authenticated payload refreshes `last_seen` (≤ 1/s) and `fw_version` from a
`"ver"` key, then goes to the per-device ingress satellite handler if one is
registered (same handlers as RS485).

**Wi-Fi.**
* The car dials a beaconing satellite when its beacon fp is enrolled for
  `wifi` (and not revoked), or any beacon while the pairing window is open;
  a refused/failed address is not re-dialled for 30 s. Satellites may also
  connect to the car's 8788. If both sessions authenticate, the older one is
  closed (one link per satellite).
* Keep-alive: both sides send `{"a":"ping"}` every 15 s (the satellite also
  while held, so the session survives the pairing window); the car answers
  an authenticated `ping` with `pong`; any received line counts; silence for
  45 s closes the session. Lines over 16 kB close it.
* `discover` broadcasts the car announcement
  `{"prius_car":1,"car_fp":…,"port":8788}` (it carries `car_fp`, so it is not
  mistaken for a probe); satellites should answer with a beacon.
* AP awareness: with `satellite_wifi_iface` set (`wlan0` on the phone) the
  transport is available only while that interface holds
  `satellite_wifi_ap_addr` (10.42.0.1); otherwise state `ap-down`, beacons
  are ignored and sessions refused. Default bind `0.0.0.0` (a unicast bind
  would not receive broadcasts); the iface also scopes the sockets with
  SO_BINDTODEVICE (best effort, needs root).

**BLE.** The registry drives `set_pairable(seconds)` with the pairing window
and `set_advertisement(interval, flags)`: 1 000 ms when ACC is on or a BLE
pairing window is open, else 10 000 ms; flags bit0 ACC, bit1 pairing.
When `io/ble_transport.py` cannot be imported the registry logs once and
reports `transports.ble.state = "unavailable"`.

### Backend config knobs (`BackendConfig`) / env

| Field | Env (`/etc/prius/backend.env`) | Default |
|-------|------|---------|
| `satellite_transports` | `BACKEND_SATELLITE_TRANSPORTS` (comma list) | `rs485,ble,wifi` |
| `satellite_rediscover_s` | `BACKEND_SATELLITE_REDISCOVER_S` | `300` |
| `satellite_discovery_ttl_s` | `BACKEND_SATELLITE_DISCOVERY_TTL_S` | `600` |
| `satellite_keychain_dir` | `BACKEND_SATELLITE_KEYCHAIN_DIR` | `/etc/prius/satellites` (fallback `./satellites`) |
| `satellite_wifi_bind` | `BACKEND_SATELLITE_WIFI_BIND` | `0.0.0.0` |
| `satellite_wifi_iface` | `BACKEND_SATELLITE_WIFI_IFACE` | empty (always available); phone: `wlan0` |
| `satellite_wifi_ap_addr` | `BACKEND_SATELLITE_WIFI_AP_ADDR` | `10.42.0.1` |
| `satellite_wifi_udp_port` / `satellite_wifi_tcp_port` | — | `8787` / `8788` |
| `satellite_ble_socket` | `BACKEND_SATELLITE_BLE_SOCKET` | `/run/prius/bled.sock` |

The RS485 knobs (`satellite_linger_s`, `satellite_poll_*` …) are in
`SATELLITE_POWER.md`. `cryptography` comes from the system (`py3-cryptography`;
the backend venv uses `--system-site-packages`).

### Bench: a fake Wi-Fi satellite

```sh
K=~/satkeys
POCO/rootfs/usr/local/sbin/prius-satkey --dir $K init
POCO/rootfs/usr/local/sbin/prius-satkey --dir $K new garage --transport wifi -o $K/garage.json
# backend (or any registry) with the keychain dir:
BACKEND_SATELLITE_KEYCHAIN_DIR=$K BACKEND_SATELLITE_TRANSPORTS=rs485,wifi python -m cyberpunk_computer.backend ...
# satellite on the same host: its own TCP port, beacon unicast to the local listener
python3 scripts/sat_wifi_sim.py --blob $K/garage.json --port 8790 --beacon-addr 127.0.0.1
# or let it dial the car instead of beaconing:
python3 scripts/sat_wifi_sim.py --blob $K/garage.json --dial 127.0.0.1:8788 --no-beacon
```

**Reference BLE satellite firmware** (ESP32-C6/S3, Arduino + NimBLE, scanner mode A and beacon mode B, bench-verified against prius-bled): `satellites/esp32s3-ble/README.md`.

## 9. Open gaps

| Gap | What would resolve it |
|---|---|
| Shared TX notifications: every authenticated BLE satellite connected at the same time can read lines addressed to the others. | Per-link session key (X25519 in the auth handshake + AEAD per line), needed once a second BLE satellite exists. |
| Rotating BLE addresses (privacy) not supported. | — |
| No reference Wi-Fi satellite firmware (only `scripts/sat_wifi_sim.py`); no ESP32-C3 / RP2040 firmware (Ed25519 via monocypher or mbedtls). | Write and bench one against the backend. |
| Satellite screens on the MFD. | — |
