# Deployment Guide

This guide explains how to deploy the CyberPunk Prius Gen 2 Computer to your Raspberry Pi Zero 2W.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Initial Setup](#initial-setup)
- [Deployment Process](#deployment-process)
- [Service Management](#service-management)
- [Monitoring and Debugging](#monitoring-and-debugging)
- [Troubleshooting](#troubleshooting)

## Prerequisites

### Hardware

- Raspberry Pi Zero 2W with Raspberry Pi OS installed
- USB UART device (RP2040 Gateway) connected to the RPI
- Display connected to RPI (framebuffer `/dev/fb0`)
- Network connection (WiFi or Ethernet)

### Software

Your development machine needs:
- Python 3.8 or later
- `rsync` command-line tool
- SSH access configured to the RPI

Your Raspberry Pi needs:
- Python 3.8 or later
- `systemd` (included in Raspberry Pi OS)
- Required system packages (will be installed during setup)

## Initial Setup

### 1. Configure SSH Access

Ensure you can SSH into your Raspberry Pi:

```bash
# Test SSH connection
ssh prius

# If this doesn't work, add to ~/.ssh/config:
Host prius
    HostName <RPI_IP_ADDRESS>
    User pi
    IdentityFile ~/.ssh/id_rsa
```

### 2. Prepare Raspberry Pi

On your Raspberry Pi, install required system packages:

```bash
ssh prius

# Update package list
sudo apt update

# Install Python and development tools
sudo apt install -y python3 python3-pip python3-venv

# Install SDL2 for pygame (display/input)
sudo apt install -y libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev

# Install framebuffer support
sudo apt install -y libfreetype6-dev libportmidi-dev

# For serial communication
sudo apt install -y python3-serial

# Add user to dialout group (for serial port access)
sudo usermod -a -G dialout pi

# Reboot to apply group changes
sudo reboot
```

## Deployment Process

### Quick Deployment

For a quick deployment (updates code and dependencies):

```bash
# From your development machine
cd d:/Projects/CyberPunk/cyberpank-prius-gen2-computer
python deploy.py --restart
```

This will:
1. Sync code to the RPI
2. Install/update Python dependencies
3. Restart the service (if already installed)

### First-Time Deployment

For the first deployment, you need to install the systemd service:

```bash
# Install service and restart
python deploy.py --install --restart

# Or dry-run to see what will happen
python deploy.py --install --restart --dry-run
```

This will:
1. Sync code to `/home/pi/cyberpunk_computer`
2. Create Python virtual environment
3. Install dependencies from `requirements.txt`
4. Create log directory at `/var/log/cyberpunk_computer`
5. Install systemd service
6. Enable auto-start on boot
7. Start the service

### Deployment Options

```bash
# Basic deployment (no service restart)
python deploy.py

# Deployment with custom host/user
python deploy.py --host my-rpi --user myuser

# Install service
python deploy.py --install

# Restart service after deployment
python deploy.py --restart

# Show logs after deployment
python deploy.py --logs

# Dry-run (show what would be done)
python deploy.py --dry-run

# Full deployment with all options
python deploy.py --install --restart --logs
```

## Production Mode Features

When running with `--production` flag, the application:

### Logging
- **Level**: INFO (reduced verbosity)
- **Console**: Yes (viewable via journalctl)
- **File**: `/var/log/cyberpunk_computer/app.log`
  - Max size: 10 MB per file
  - 5 rotating backup files
  - Total max: 50 MB

### USB UART Resilience
- **Auto-reconnect**: Enabled
- **Reconnect delay**: 2 seconds
- Gracefully handles USB device disconnect/reconnect
- Continues running even when Gateway is disconnected

### Display
- **Scale**: 1 (native 480x240 resolution)
- **Fullscreen**: Yes
- **Effects**: Enabled (scanlines, glow)

### Output
- **Primary**: USB UART to RP2040 Gateway
- **Secondary**: UDP to VFD satellite (localhost:5110)

## Service Management

### Start/Stop/Restart

```bash
# Start service
ssh prius 'sudo systemctl start cyberpunk-computer'

# Stop service
ssh prius 'sudo systemctl stop cyberpunk-computer'

# Restart service
ssh prius 'sudo systemctl restart cyberpunk-computer'

# Check status
ssh prius 'sudo systemctl status cyberpunk-computer'
```

### Enable/Disable Auto-Start

```bash
# Enable auto-start on boot
ssh prius 'sudo systemctl enable cyberpunk-computer'

# Disable auto-start
ssh prius 'sudo systemctl disable cyberpunk-computer'
```

### View Service Configuration

```bash
# View service file
ssh prius 'cat /etc/systemd/system/cyberpunk-computer.service'
```

## Monitoring and Debugging

### View Logs

#### Live Log Streaming

```bash
# Follow logs in real-time
ssh prius 'sudo journalctl -u cyberpunk-computer -f'

# Show last 50 lines
ssh prius 'sudo journalctl -u cyberpunk-computer -n 50'

# Show logs since boot
ssh prius 'sudo journalctl -u cyberpunk-computer -b'
```

#### View Log Files

```bash
# View application log
ssh prius 'tail -f /var/log/cyberpunk_computer/app.log'

# List all log files
ssh prius 'ls -lh /var/log/cyberpunk_computer/'
```

### Check Service Status

```bash
# Detailed service status
ssh prius 'sudo systemctl status cyberpunk-computer'

# Check if service is running
ssh prius 'systemctl is-active cyberpunk-computer'

# Check if auto-start is enabled
ssh prius 'systemctl is-enabled cyberpunk-computer'
```

### Monitor System Resources

```bash
# CPU and memory usage
ssh prius 'top -bn1 | grep python'

# Full process info
ssh prius 'ps aux | grep cyberpunk'

# Disk usage
ssh prius 'df -h /var/log/cyberpunk_computer'
```

## Troubleshooting

### Service Won't Start

**Check logs for errors:**
```bash
ssh prius 'sudo journalctl -u cyberpunk-computer -n 100 --no-pager'
```

**Common issues:**

1. **Missing dependencies**
   ```bash
   ssh prius 'cd /home/pi/cyberpunk_computer && ./venv/bin/pip install -r requirements.txt'
   ```

2. **Permission issues**
   ```bash
   # Check serial port permissions
   ssh prius 'ls -l /dev/ttyACM0'
   
   # Verify user is in dialout group
   ssh prius 'groups pi'
   ```

3. **Display issues**
   ```bash
   # Check framebuffer access
   ssh prius 'ls -l /dev/fb0'
   
   # Test display environment
   ssh prius 'echo $DISPLAY'
   ```

### USB UART Not Detected

```bash
# List USB devices
ssh prius 'lsusb'

# Check serial ports
ssh prius 'ls -l /dev/tty*'

# View kernel messages
ssh prius 'dmesg | tail -n 50'

# Check if device appears when connected
ssh prius 'sudo tail -f /var/log/syslog | grep tty'
```

### Auto-Reconnect Not Working

The application should automatically reconnect when USB UART is plugged back in. Check logs:

```bash
ssh prius 'sudo journalctl -u cyberpunk-computer -f | grep -i "reconnect\|serial"'
```

You should see messages like:
- `Serial connection lost: ...`
- `Attempting to reconnect to /dev/ttyACM0...`
- `Serial port reconnected successfully (attempt #N)`

### High CPU/Memory Usage

```bash
# Monitor resources
ssh prius 'top -d 1'

# Check for memory leaks
ssh prius 'watch -n 1 "ps aux | grep python | grep -v grep"'
```

### Service Crashes on Start

```bash
# Check for Python errors
ssh prius 'sudo journalctl -u cyberpunk-computer -n 200 | grep -i "error\|exception\|traceback"'

# Test manual start (for detailed output)
ssh prius
cd /home/pi/cyberpunk_computer
./venv/bin/python -m cyberpunk_computer --production
```

### Deployment Fails

1. **Check SSH connection**
   ```bash
   ssh prius 'echo "Connection OK"'
   ```

2. **Check disk space on RPI**
   ```bash
   ssh prius 'df -h'
   ```

3. **Verify rsync is installed**
   ```bash
   which rsync
   ```

4. **Test deployment with dry-run**
   ```bash
   python deploy.py --dry-run
   ```

## Manual Deployment

If the deployment script fails, you can deploy manually:

```bash
# 1. Sync code
rsync -avz --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.git' \
  d:/Projects/CyberPunk/cyberpank-prius-gen2-computer/ \
  pi@prius:/home/pi/cyberpunk_computer/

# 2. SSH into RPI
ssh prius

# 3. Create virtual environment
cd /home/pi/cyberpunk_computer
python3 -m venv venv

# 4. Install dependencies
./venv/bin/pip install -r requirements.txt

# 5. Create log directory
sudo mkdir -p /var/log/cyberpunk_computer
sudo chown pi:pi /var/log/cyberpunk_computer

# 6. Install service
sudo cp deployment/cyberpunk-computer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cyberpunk-computer
sudo systemctl start cyberpunk-computer
```

## Testing

### Test Without Service

Run directly to see output:

```bash
ssh prius
cd /home/pi/cyberpunk_computer
./venv/bin/python -m cyberpunk_computer --production
```

Press `Ctrl+C` to stop.

### Test Serial Connection

```bash
# Check if Gateway is responding
ssh prius 'python3 -c "
import serial
ser = serial.Serial(\"/dev/ttyACM0\", 1000000, timeout=1)
print(\"Serial port opened:\", ser.name)
ser.close()
"'
```

### Test Display

```bash
# Test framebuffer access
ssh prius 'sudo cat /dev/urandom | sudo tee /dev/fb0 > /dev/null &'
# Kill after a few seconds
ssh prius 'sudo killall cat'
```

## Performance Tuning

### Optimize for Raspberry Pi Zero 2W

Edit service file if needed:

```bash
ssh prius 'sudo nano /etc/systemd/system/cyberpunk-computer.service'
```

Add environment variables:
```ini
Environment=SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS=0
Environment=SDL_RENDER_DRIVER=software
```

Reload and restart:
```bash
ssh prius 'sudo systemctl daemon-reload && sudo systemctl restart cyberpunk-computer'
```

## Updating

To update after making code changes:

```bash
# Quick update and restart
python deploy.py --restart --logs

# Or step by step
python deploy.py           # Sync code
ssh prius 'sudo systemctl restart cyberpunk-computer'  # Restart
ssh prius 'sudo journalctl -u cyberpunk-computer -f'  # Watch logs
```

## Uninstallation

To remove the service:

```bash
# Stop and disable service
ssh prius 'sudo systemctl stop cyberpunk-computer'
ssh prius 'sudo systemctl disable cyberpunk-computer'

# Remove service file
ssh prius 'sudo rm /etc/systemd/system/cyberpunk-computer.service'
ssh prius 'sudo systemctl daemon-reload'

# Optionally remove code and logs
ssh prius 'rm -rf /home/pi/cyberpunk_computer'
ssh prius 'sudo rm -rf /var/log/cyberpunk_computer'
```

## Additional Resources

- [Raspberry Pi Documentation](https://www.raspberrypi.org/documentation/)
- [systemd Service Documentation](https://www.freedesktop.org/software/systemd/man/systemd.service.html)
- [Pygame Documentation](https://www.pygame.org/docs/)
- [PySerial Documentation](https://pythonhosted.org/pyserial/)
