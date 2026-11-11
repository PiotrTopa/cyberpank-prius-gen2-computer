# BLE test satellite (ESP32, Arduino + NimBLE)

Reference firmware for a BLE satellite of the car computer: `docs/SATELLITES.md`
§3 (auth handshake) and §4 (BLE: GATT UUIDs, TX address prefix, bonding, BLE roles).
The directory says `esp32s3`, but the code builds for any NimBLE-capable ESP32. The
bench board is an **ESP32-C6** (8 MB flash, native USB-JTAG/serial `303a:1001`,
WS2812 RGB LED on GPIO8).

| Mode | Build flag | Role |
|------|------------|------|
| A, scanner satellite (default) | — | GAP central: scans for the car, connects to `prius-bled`'s GATT server |
| B, beacon satellite | `-DSAT_MODE_BEACON=1` | GAP peripheral + GATT server with the mirrored UUIDs (TX `…0001` notify = sat→car, RX `…0002` write = car→sat, no address prefix); the car connects |

## Toolchain (no sudo)

```sh
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=~/.local/bin sh
arduino-cli config init --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32            # 3.3.12 tested (about 1.5 GB)
arduino-cli lib install NimBLE-Arduino Crypto ArduinoJson   # 2.5.2 / 0.4.0 (rweather) / 7.4.3 tested
```

## Provision (one blob, one rebuild)

```sh
prius-satkey --dir <keychain dir> new esp1 --transport ble -o esp1.json   # on the car: /etc/prius/satellites
tools/blob2header.py esp1.json                     # writes esp32s3_ble/provision.h (git-ignored, 0600)
tools/blob2header.py esp1.json --car-addr AA:BB:CC:DD:EE:FF   # bench: another BlueZ adapter plays the car
```

`provision.h` holds the satellite's Ed25519 private key: **never commit it** (it is
in `.gitignore`, like `blob*.json` and `build*/`). Without it the build falls back
to `provision.example.h`, which has zero keys: the satellite runs, but the car refuses it.
Pin the car by address: the blob's `ble.car_addr` (`02:00:E8:51:02:58`, the phone)
is the default. `--car-addr any` takes the first device that advertises the
service UUID. A passive scan never sees the `PRIUS` name, because the name is
in the scan response; build with `-DSAT_ACTIVE_SCAN=1` to match on the name.

## Build and flash

```sh
FQBN=esp32:esp32:esp32c6:CDCOnBoot=cdc       # ESP32-S3: esp32:esp32:esp32s3:CDCOnBoot=cdc
arduino-cli compile --fqbn $FQBN --output-dir build esp32s3_ble
# mode B:
arduino-cli compile --fqbn $FQBN --build-property "compiler.cpp.extra_flags=-DSAT_MODE_BEACON=1" \
    --output-dir build-beacon esp32s3_ble
arduino-cli upload --fqbn $FQBN -p /dev/ttyACM0 --input-dir build esp32s3_ble
# flashing from another host (esptool only): build/esp32s3_ble.ino.merged.bin at 0x0
esptool --chip esp32c6 --port /dev/ttyACM0 write-flash 0x0 build/esp32s3_ble.ino.merged.bin
```

Size: about 749 kB of the 1.25 MB app partition (default scheme). Uploading
does not touch NVS, so bonds and a stored cert survive a reflash. To clear them,
run `esptool erase-flash` before flashing.

Other build knobs: `-DSAT_LED_PIN=<gpio>` (with `-DSAT_LED_RGB=0` for a plain LED).
The default is the variant's `RGB_BUILTIN` (C6: GPIO8, S3 DevKitC: GPIO48).

## Serial monitor

```sh
arduino-cli monitor -p /dev/ttyACM0 -c baudrate=115200
```

Each log line starts with `[seconds.ms]` since boot. The boot lines show the firmware version,
mode, name/sat_id/fp/car fp, `cert yes|NO`, `key ok` (the private key derives
the provisioned public key), and the **own BLE address**, which is the public
address the car frames TX lines with.

A good mode-A session:

```
scanning 5000 ms (passive) for 580e04f7-… car_addr A4:C3:F0:D5:85:21
seen car candidate A4:C3:F0:D5:85:21 name='' rssi -45 (provisioned car_addr)
connecting to a4:c3:f0:d5:85:21 (bonded: yes)
connected to a4:c3:f0:d5:85:21
ATT MTU 247
link security: encrypted=1 bonded=1 authenticated=0 keysize=16
subscribed to TX
link up (hello written), waiting for the car's auth challenge
RX< {"d":{"a":"auth","v":1,"nonce":"…","car":"2bvkx7epgaemngdjq6wxmecstu"}}
challenge from car 2bvkx7epgaemngdjq6wxmecstu, signed in 58 ms
TX> {"d":{"a":"auth","v":1,"fp":"…","pubkey":"…","cert":{…},"nonce":"…","sig":"…"}}
RX< {"d":{"a":"auth_ok","sat_id":300,"sig":"…"}}
AUTHENTICATED as sat_id 300 (car signature verified)
TX> {"id":300,"d":{"a":"status","ver":"esp32c6-0.1","temp":37.5,"up":45,"led":false}}
RX< {"id":300,"d":{"a":"led","on":true}}
LED ON (car command)
TX> {"id":300,"d":{"a":"ack","cmd":"led"}}
```

## Behaviour

* **Scan** (mode A): passive, a 5 s burst every 15 s. While the car advertises
  at 10 s (ACC off), a single burst catches it about half the time. While it
  advertises at 1 s (ACC on or pairing window open), the first burst finds it.
* **Connect**: connect timeout 15 s, because the initiator must wait for the next ADV_IND,
  up to 10.24 s away. Then MTU 247, then `secureConnection()` (LE Secure
  Connections, Just Works, bonding, keys in NVS), discover, subscribe to TX, write
  a bare `\n` to RX (prius-bled counts the link at its first RX write).
* **Bonding**: the car accepts a new bond only while its pairing window is open
  (`satellite_pair_open`). A satellite with a valid bond reconnects at any time.
  If the car has lost the bond, encryption fails and the satellite logs
  `pairing/encryption failed …` and backs off. It pairs again as soon as the
  window is open; no manual bond clearing is needed on the satellite.
* **Auth**: byte-for-byte `SatelliteAuthClient` (MSG/MSG2, Ed25519 via rweather
  Crypto). The satellite refuses a challenge from a foreign `car` fp, verifies the car's MSG2
  signature on `auth_ok`, and stores `sat_id`, plus a new `cert` if `auth_ok`
  carries one, in NVS. Before `auth_ok` the envelope is `{"d":…}`; after it,
  `{"id":<sat_id>,"d":…}`.
* **After auth**: the satellite sends `status` (`ver`, `temp` = chip temperature, `up` in seconds, `led`)
  right away and every 30 s. It answers `ping` with `pong` and acks every other
  command with `{"a":"ack","cmd":<a>}`. A `status` request gets a status report
  and an ack. `{"a":"led","on":bool}` switches the LED.
* **TX framing** (mode A): notifications are reassembled into lines at `\n`. A
  `<17-char ADDR> ` prefix that is not our own address drops the line. A line
  without a prefix is taken as addressed to us.
* **Failure handling**: on disconnect or failure, the satellite waits 2 s and then
  rescans, doubling the wait up to 60 s. The wait resets to 2 s after a successful auth. `auth_fail`,
  a foreign car, or a bad car signature waits 60 s. With no `auth_ok` after 90 s
  (the car may hold an uncertified key while the pairing window is open), the
  satellite drops the link.

LED (RGB): off = idle or scanning, dim amber = linked and waiting for auth, dim
blue = authenticated, bright green = the car sent `led on`.

## Bench (laptop BlueZ plays the car)

Run `prius-bled` on the laptop's `hci0` with a socket in a scratch directory.
A small stand-in for the backend opens the socket, sends `pairable 120`, runs
`satellite_auth.LinkAuth` on `link up`/`data` against a test keychain
(`prius-satkey --dir K init` + `new esp1 --transport ble`), then sends
`led on/off`, `ping` and `status`. Provision the satellite with
`--car-addr <laptop adapter address>`. Mode B works the same way with a D-Bus
GATT client as the car: Device1.Connect + Pair, StartNotify on `…0001`,
WriteValue on `…0002`. Stop bled afterwards; on SIGTERM it unregisters
everything and restores the adapter.

Bench note: a bond made in one role (mode A) does not work after switching the
same pair of devices to the other role (mode B). BlueZ aborts the connection. Run
`bluetoothctl remove <sat addr>` on the car side when switching modes.
