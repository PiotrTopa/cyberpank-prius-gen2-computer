# Quick Deployment Reference

## First-Time Setup

```bash
# One-time setup: Install service and start
python deploy.py --install --restart --logs
```

## Regular Updates

```bash
# Update code and restart
python deploy.py --restart
```

## Common Commands

### Deployment
```bash
python deploy.py                    # Sync code only
python deploy.py --restart          # Sync + restart service
python deploy.py --logs             # Sync + show logs
python deploy.py --dry-run          # Preview changes
```

### Service Management
```bash
ssh prius 'sudo systemctl status cyberpunk-computer'    # Check status
ssh prius 'sudo systemctl restart cyberpunk-computer'   # Restart
ssh prius 'sudo systemctl stop cyberpunk-computer'      # Stop
ssh prius 'sudo systemctl start cyberpunk-computer'     # Start
```

### Logs
```bash
ssh prius 'sudo journalctl -u cyberpunk-computer -f'              # Follow live
ssh prius 'sudo journalctl -u cyberpunk-computer -n 50'           # Last 50 lines
ssh prius 'tail -f /var/log/cyberpunk_computer/app.log'          # App log file
```

### Troubleshooting
```bash
# Test manually (see errors directly)
ssh prius
cd /home/pi/cyberpunk_computer
./venv/bin/python -m cyberpunk_computer --production

# Check serial port
ssh prius 'ls -l /dev/ttyACM0'

# Check if USB device is detected
ssh prius 'lsusb | grep -i "usb\|serial"'

# Watch for USB connect/disconnect
ssh prius 'sudo dmesg -w | grep tty'
```

## Production Mode Features

The `--production` flag enables:
- ✓ Auto-reconnect to USB UART (handles disconnect/reconnect)
- ✓ Proper logging (INFO level, file rotation)
- ✓ Fullscreen display (native resolution)
- ✓ UDP output to VFD satellite
- ✓ Optimized for embedded deployment

## Architecture

```
Development Machine
       │
       │ rsync (deploy.py)
       ▼
Raspberry Pi Zero 2W
       │
       ├─ systemd service (auto-start, restart on failure)
       ├─ Python venv (isolated dependencies)
       └─ Application
              ├─ USB UART → RP2040 Gateway (auto-reconnect)
              ├─ UDP → VFD Satellite (localhost:5110)
              └─ Framebuffer → Display (/dev/fb0)
```

See [DEPLOYMENT.md](docs/DEPLOYMENT.md) for complete documentation.
