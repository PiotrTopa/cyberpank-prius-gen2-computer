# Deployment Resources

This directory contains all deployment-related files for the CyberPunk Computer.

## Files

### Configuration

- **`../.env`** - Local configuration file (git-ignored)
  - Copy `.env.example` to `.env`
  - Set `DEPLOY_USER` and `DEPLOY_HOST`
  - Prevents sensitive info from being committed

- **`cyberpunk-computer.service`** - systemd service configuration file
  - Installed to: `/etc/systemd/system/cyberpunk-computer.service`
  - Manages: Auto-start, restart on failure, environment setup
  - Runs as root for framebuffer access

- **`run-on-console.sh`** - Wrapper script for the service
  - Sets up SDL environment variables
  - Uses `SDL_VIDEODRIVER=dummy` with direct framebuffer output

### Scripts

- **`../deploy.py`** - Main deployment automation script
  - Usage: `python deploy.py [options]`
  - Auto-loads settings from `.env` if present
  - Works on Windows (uses scp fallback if rsync not available)
  - See: `--help` for all options

- **`rpi-helper.sh`** - Quick helper script for common operations
  - Usage: `./deployment/rpi-helper.sh <command>`
  - Commands: status, logs, restart, deploy, setup, test, etc.

### Documentation

- **`QUICK_REFERENCE.md`** - Quick command reference
- **`IMPLEMENTATION_SUMMARY.md`** - Complete implementation details

## Quick Start

### Fresh RPI Installation

```bash
# 1. Ensure SSH access works (add SSH key first)
ssh prius

# 2. Full deployment with OS setup (installs SDL2, configures permissions)
python deploy.py --setup-os --install --restart

# This will:
# - Install required system packages (libsdl2-dev, etc.)
# - Configure framebuffer permissions (/dev/fb0)
# - Add user to video/tty groups
# - Install udev rules
# - Sync application code
# - Install Python dependencies
# - Install and start systemd service
```

### Regular Updates

```bash
# Quick deploy and restart
python deploy.py --restart

# Or use helper script
./deployment/rpi-helper.sh deploy
```

### Service Management

```bash
# Check status
./deployment/rpi-helper.sh status

# View logs
./deployment/rpi-helper.sh logs

# Restart service
./deployment/rpi-helper.sh restart
```

## Deploy Script Options

```bash
python deploy.py [options]

Options:
  --host HOST   SSH host (default: from .env or 'prius')
  --user USER   SSH user (default: from .env or 'piotr')
  --setup-os    Run OS-level setup (SDL2 packages, framebuffer permissions)
  --install     Install systemd service and enable auto-start
  --restart     Restart service after deployment
  --logs        Show service logs after deployment
  --dry-run     Show what would be done without executing

Examples:
  python deploy.py                      # Sync code only
  python deploy.py --restart            # Sync and restart service
  python deploy.py --setup-os --install # Fresh install with OS setup
  python deploy.py --install --restart  # Install service and restart
```

## Architecture

```
deployment/
├── cyberpunk-computer.service    # systemd service file
├── run-on-console.sh             # Environment wrapper script
├── 99-framebuffer.rules          # udev rules for /dev/fb0
├── rpi-helper.sh                 # Quick helper commands
├── QUICK_REFERENCE.md            # Command cheatsheet
├── IMPLEMENTATION_SUMMARY.md     # Technical details
└── README.md                     # This file

../deploy.py                      # Main deployment script
../.env                           # Local configuration (git-ignored)
../.env.example                   # Example configuration
```

## VGA666 Display Setup

The RPI must have VGA666 overlay configured in `/boot/firmware/config.txt`:

```ini
# Disable KMS (required for VGA666 DPI mode)
disable_fw_kms_setup=1

# VGA666 overlay with custom 15kHz timings for Prius MFD
dtoverlay=vga666
dpi_output_format=0x06
dpi_mode=87
dpi_timings=480 1 32 48 80 240 1 3 10 6 0 0 0 60 0 9600000 1
```

The application uses direct framebuffer output (memory-mapped `/dev/fb0`) instead
of SDL's fbcon driver, which was removed from modern SDL2 builds.

## Support

- **Questions about deployment?** See [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md)
- **Need quick commands?** See [QUICK_REFERENCE.md](QUICK_REFERENCE.md)
- **Implementation details?** See [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)
