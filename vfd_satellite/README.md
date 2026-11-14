# VFD satellite simulator

Pygame simulation (v1.0.0) of the VFD satellite display, RS485 device id **110**.
It receives the same NDJSON as the hardware display and renders the dashboard layout
on a 256×48 framebuffer. Protocol: [docs/VFD_SATELLITE_PROTOCOL.md](../docs/VFD_SATELLITE_PROTOCOL.md).

Hardware target: Noritake CU256048-Y1A (256×48 VFD), RP2040, SPI to the display,
UART/RS485 to the bus. The framebuffer and component rendering are meant to be shared
with the hardware build; only the output layer differs.

## Run

```bash
python -m venv venv && . venv/bin/activate
pip install -r requirements.txt

python -m vfd_satellite --udp [--port 5110] [--scale 1-4]   # development input (UDP)
python -m vfd_satellite --serial /dev/ttyUSB0 [--baudrate 115200]
python -m vfd_satellite --demo                              # built-in test data
# --fps (default 60), --verbose
```

## Messages handled

| `t` | Message | Rate (backend, `state/rules/vfd_display.py`) |
|---|---|---|
| `E` | energy: `mg`, `fl`, `br`, `spd`, `soc`, `ptr`, `lpg`, `ice` | 20 Hz |
| `S` | state: `fuel`, `gear`, `rdy` | on change, 1 Hz keepalive |
| `C` | config: `tb`, `bri` | on connect / change |
| `R` | reset (`hist`) | — |

The simulator does not implement the clock (`K`) or canvas (`T`/`D`/`B`) messages of
the protocol.

```json
{"id":110,"d":{"t":"E","mg":0.35,"fl":0.25,"br":0,"spd":0.45,"soc":0.62,"ptr":25,"lpg":42,"ice":true}}
{"id":110,"d":{"t":"S","fuel":"LPG","gear":"D","rdy":true}}
{"id":110,"d":{"t":"C","tb":60,"bri":100}}
```

## Layout (x ranges in px)

| 0–64 | 64–128 | 128–192 | 192–256 |
|---|---|---|---|
| fuel gauge: petrol / LPG / battery, active fuel marker | power flow: ICE, battery, wheels arrows | energy graph: MG power history (±20 kW, time base `tb`) | power bars: instant MG power, fuel / brake |

## Files

| File | Role |
|---|---|
| `__main__.py` | entry point, CLI |
| `receiver.py` | NDJSON receiver (UDP / serial / demo) |
| `state.py` | display state from `E`/`S`/`C`/`R` |
| `renderer.py` | rendering engine |
| `framebuffer.py` | 1-bit framebuffer (portable) |
| `icons.py` | 1-bit icons |
| `components/` | `fuel_gauge.py`, `power_flow.py`, `energy_graph.py`, `power_bars.py` |

## Open gaps

- No tests for the simulator in `tests/`.
- The hardware firmware (which implements `K`/`T`/`D`/`B`) is not in this directory;
  its location is not documented here.
