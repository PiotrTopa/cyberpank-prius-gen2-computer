# CyberPunk Prius Gen 2 - Onboard Computer

Custom onboard computer system for the **CyberPunk Prius Gen 2** retrofit, a retro-modded Toyota Prius.

**Tags:** `cyberpunk` `prius` `gen2` `raspberry-pi` `pygame` `embedded` `hmi` `automotive` `can-bus` `ndjson`

## 🎯 Overview

A pygame-based HMI (Human-Machine Interface) application designed to run on a **Raspberry Pi Zero 2W**, displaying on the native MFD (Multi-Function Display) of the Prius.

### Key Features

- **Native Resolution:** 480×240 pixels
- **Aesthetic:** Cyberpunk / Synthwave / VFD-inspired visuals
- **Input:** Rotary encoder with haptic feedback (keyboard for development)
- **Communication:** NDJSON protocol over USB UART Gateway

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Raspberry Pi Zero 2W                      │
│  ┌─────────────────────────────────────────────────────────┐│
│  │              CyberPunk Computer (This App)              ││
│  │  ┌──────────┐  ┌──────────┐  ┌────────────────────────┐││
│  │  │   UI     │  │  Core    │  │    Communication       │││
│  │  │ (Pygame) │◄─┤  Engine  │◄─┤  (NDJSON over Serial)  │││
│  │  └──────────┘  └──────────┘  └───────────┬────────────┘││
│  └──────────────────────────────────────────┼──────────────┘│
│                                              │ USB           │
└──────────────────────────────────────────────┼───────────────┘
                                               │
┌──────────────────────────────────────────────┼───────────────┐
│                    Gateway (RP2040)          │               │
│  ┌───────────────────────────────────────────┴─────────────┐ │
│  │                    NDJSON Router                        │ │
│  └────┬──────────────────┬─────────────────────┬───────────┘ │
│       │                  │                     │             │
│  ┌────┴────┐       ┌─────┴─────┐        ┌──────┴──────┐      │
│  │   CAN   │       │  AVC-LAN  │        │    RS485    │      │
│  │  (id:1) │       │   (id:2)  │        │  (id:6-255) │      │
│  └────┬────┘       └─────┬─────┘        └──────┬──────┘      │
└───────┼──────────────────┼─────────────────────┼─────────────┘
        │                  │                     │
   ┌────┴────┐       ┌─────┴─────┐        ┌──────┴──────┐
   │ Vehicle │       │   Audio   │        │  Satellites │
   │   ECUs  │       │  System   │        │  (Custom)   │
   └─────────┘       └───────────┘        └─────────────┘

┌──────────────────────────────────────────────────────────────┐
│                    Powerbox (RP2040)                         │
│  Direct USB-CDC NDJSON connection to POCO                    │
│  Monitors INA219 (V/A) and controls ignition state.          │
└──────────────────────────────────────────────────────────────┘
```

## 📡 Communication Protocol

This application communicates with the vehicle through the **Gateway** using the NDJSON protocol.

See: [PROTOCOL.md](./docs/PROTOCOL.md) for full specification.

### Quick Reference

```json
{"id": 0, "d": {...}}  // SYSTEM - Gateway control
{"id": 1, "d": {...}}  // CAN - Vehicle bus
{"id": 2, "d": {...}}  // AVC-LAN - Multimedia bus
{"id": 6+, "d": {...}} // SATELLITES - RS485 modules
{"id": 200+, "d": {...}} // POWERBOX - Local power telemetry
```

## 🚀 Quick Start

### Requirements

- Python 3.11+
- Pygame 2.5+

### Installation

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# or
.venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt
```

### Running

```bash
# Development mode (2x upscale, keyboard input)
python -m cyberpunk_computer --dev --scale 2

# Production mode (native resolution, serial connection with auto-reconnect)
python -m cyberpunk_computer --production

# Replay recorded data for testing
python -m cyberpunk_computer --replay assets/data/full.ndjson --dev --scale 2
```

### Deployment to Raspberry Pi

For production deployment to RPI Zero 2W:

```bash
# First-time setup (installs service and enables auto-start)
python deploy.py --install --restart --logs

# Regular updates (sync code and restart service)
python deploy.py --restart
```

See [DEPLOYMENT.md](./docs/DEPLOYMENT.md) for complete deployment guide and [Quick Reference](./deployment/QUICK_REFERENCE.md) for common commands.

**Production mode features:**
- ✓ Auto-reconnect to USB UART (handles disconnect/reconnect)
- ✓ Proper logging (INFO level, file rotation)
- ✓ Fullscreen display (native resolution)
- ✓ systemd service (auto-start, restart on failure)

### Controls (Development)

| Key | Action |
|-----|--------|
| ← / → | Rotate encoder (navigate) |
| Enter | Light press (select/enter) |
| Space | Strong press (context action) |
| Escape | Back / Exit |

## 📁 Project Structure

```
cyberpunk-prius-gen2-computer/
├── cyberpunk_computer/          # Main package
│   ├── __init__.py
│   ├── __main__.py              # Entry point
│   ├── config.py                # Configuration
│   ├── core/                    # Core engine
│   │   ├── app.py               # Main application loop
│   │   └── renderer.py          # Rendering with scaling
│   ├── ui/                      # UI framework
│   │   ├── colors.py            # Cyberpunk color palette
│   │   ├── fonts.py             # Font management
│   │   ├── widgets/             # Reusable UI widgets
│   │   └── screens/             # Screen definitions
│   ├── input/                   # Input handling
│   │   ├── manager.py           # Input abstraction
│   │   └── touch.py             # Touch event handling
│   ├── io/                      # Virtual Twin IO layer
│   │   ├── ports.py             # InputPort/OutputPort interfaces
│   │   ├── ingress.py           # Data input → State
│   │   ├── egress.py            # State → Hardware output
│   │   ├── file_io.py           # File replay (development)
│   │   ├── serial_io.py         # Serial UART (production)
│   │   ├── powerbox.py          # Powerbox state translation
│   │   └── factory.py           # VirtualTwin factory
│   ├── state/                   # State management
│   │   ├── store.py             # Central state store
│   │   ├── app_state.py         # State dataclasses
│   │   ├── actions.py           # Action definitions
│   │   └── rules.py             # Rules engine
│   └── comm/                    # Protocol decoders
│       ├── avc_decoder.py       # AVC-LAN protocol
│       ├── avc_commands.py      # AVC-LAN commands
│       └── can_decoder.py       # CAN bus decoder
├── powerbox/                    # RP2040 Powerbox firmware
│   ├── main.py                  # NDJSON firmware
│   ├── ina219.py                # I2C driver
│   ├── rescue.sh                # Bootloop recovery
│   └── README.md                # Powerbox docs
├── assets/                      # Static assets
│   ├── fonts/
│   └── data/                    # Sample recordings
├── docs/                        # Documentation
│   ├── ARCHITECTURE.md
│   ├── VIRTUAL_TWIN_ARCHITECTURE.md
│   └── PROTOCOL.md
├── examples/                    # Usage examples
├── requirements.txt
└── README.md
```

## 🎨 Design Guidelines

### Visual Style

- **Theme:** Cyberpunk / Blade Runner / VFD displays
- **Colors:** Cyan, magenta, orange accents on dark backgrounds
- **Typography:** Monospace/technical fonts
- **Effects:** Scanlines, glow, subtle animations

### UI Principles

1. **High contrast** - Must be readable in all lighting conditions
2. **Minimal latency** - Instant response to input
3. **Clear focus** - Always obvious which element is selected
4. **Haptic correlation** - UI feedback matches encoder mode

## 📜 License

MIT License - See [LICENSE](./LICENSE) for details.

## 🔗 Related Projects

- [Gateway](../Gateway/) - RP2040-based communication bridge
- [Satellites](../Gateway/satellites/) - Distributed RS485 modules
- [Powerbox](./powerbox/) - POCO F1 companion RP2040 power manager

