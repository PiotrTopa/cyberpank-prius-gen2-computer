# VFD Satellite Display

A standalone VFD (Vacuum Fluorescent Display) simulation for the CyberPunk Prius computer system.

## Overview

This is a satellite display that connects to the main CyberPunk Computer application via NDJSON protocol (RS485 in production, UDP for development).

**Device ID**: 110

## Features

- 256×48 pixel VFD simulation with authentic phosphor colors
- Power Flow diagram (Tesla-inspired energy flow visualization)
- Fuel Gauge (Petrol, LPG, Battery with active fuel indicator)
- Energy Monitor (historical MG power graph with configurable time base)
- Power Bars (instant MG power and fuel/brake indicators)

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        VFD Satellite                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐  │
│  │   Receiver   │─────►│    State     │─────►│  Renderer    │  │
│  │  (UDP/Serial)│      │   Manager    │      │ (Framebuffer)│  │
│  └──────────────┘      └──────────────┘      └──────────────┘  │
│                                                      │          │
│                                                      ▼          │
│                                              ┌──────────────┐   │
│                                              │   Display    │   │
│                                              │ (Pygame/SPI) │   │
│                                              └──────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Installation

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt
```

## Usage

### Development Mode (UDP receiver)

```bash
# Run with UDP input (listens on port 5110)
python -m vfd_satellite --udp --port 5110

# With display scaling
python -m vfd_satellite --udp --port 5110 --scale 2
```

### Production Mode (Serial/RS485)

```bash
# Run with serial input
python -m vfd_satellite --serial /dev/ttyUSB0 --baudrate 115200
```

### Demo Mode (built-in test data)

```bash
# Run demo with simulated data
python -m vfd_satellite --demo
```

## Protocol

See [VFD_SATELLITE_PROTOCOL.md](../docs/VFD_SATELLITE_PROTOCOL.md) for the complete protocol specification.

### Quick Reference

**Energy Message (20Hz)**:
```json
{"id":110,"d":{"t":"E","mg":0.35,"fl":0.25,"br":0,"spd":0.45,"soc":0.62,"ptr":25,"lpg":42,"ice":true}}
```

**State Message (on change)**:
```json
{"id":110,"d":{"t":"S","fuel":"LPG","gear":"D","rdy":true}}
```

**Config Message (on connect)**:
```json
{"id":110,"d":{"t":"C","tb":60,"bri":100}}
```

## Display Layout

```
┌────────────────────────────────────────────────────────────────┐
│ 0                64              128             192        256│
│ ├─────────────────┼───────────────┼───────────────┼───────────┤│
│ │   FUEL GAUGE    │  POWER FLOW   │ ENERGY GRAPH  │ POWER BARS││
│ │                 │               │               │           ││
│ │  PTR LPG BTT    │  ⚡──►◯──►🔋  │ ▁▂▃▄▅▃▂▁      │   ⚡  🔥   ││
│ │  ███ ███ ███    │   ICE  BATT   │  +20kW        │  ███ ███  ││
│ │                 │    ─────      │  ──────       │  ███      ││
│ │                 │      │        │  -20kW        │           ││
│ │  ▶PTR◀          │      ▼        │               │           ││
│ ├─────────────────┼───────────────┼───────────────┼───────────┤│
│48                                                              │
└────────────────────────────────────────────────────────────────┘
```

## Hardware Target

- **Display**: Noritake CU256048-Y1A (256×48 VFD)
- **MCU**: RP2040 (Raspberry Pi Pico)
- **Interface**: SPI for display, UART/RS485 for communication

The pygame simulation uses the same rendering code as the hardware version, with only the display output layer being different.

## Development

### File Structure

```
vfd_satellite/
├── __init__.py          # Package init
├── __main__.py          # Entry point
├── receiver.py          # NDJSON receiver (UDP/Serial)
├── state.py             # State management
├── renderer.py          # VFD rendering engine
├── framebuffer.py       # Binary framebuffer (portable)
├── components/          # Display components
│   ├── __init__.py
│   ├── power_flow.py    # Power flow diagram
│   ├── fuel_gauge.py    # Fuel gauge display
│   ├── energy_graph.py  # Energy history graph
│   └── power_bars.py    # Instant power bars
└── icons.py             # VFD icons (binary bitmaps)
```

### Testing

```bash
# Run tests
pytest tests/

# Run with coverage
pytest --cov=vfd_satellite tests/
```

## License

Same as parent project.
