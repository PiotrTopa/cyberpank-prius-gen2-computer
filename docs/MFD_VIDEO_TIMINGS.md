# Prius Gen 2 MFD Video Timings & Framebuffer Configuration

## 1. Hardware Architecture

The Toyota Prius Gen 2 Multi-Function Display (MFD) utilizes a 7-inch analog RGB screen driven over an **RGBS** interface (Analog Red, Green, Blue + active-low Composite Sync).

In this system:
- **Host:** Raspberry Pi Zero 2W.
- **Video Output:** DPI (Display Parallel Interface) via a custom resistor-ladder DAC (VGA666 / Gert VGA pinout).
- **Format:** 480x240 @ 60 Hz progressive scan (262 total scanlines, matching 525-line NTSC field timing).
- **Sync:** Negative polarity composite sync (C-Sync) fed to the MFD sync input.

---

## 2. NTSC Video Standard & Mathematical Requirements

The Denso MFD video receiver uses standard NTSC deflection timings:

| Parameter | Standard NTSC Value | System Target |
| :--- | :--- | :--- |
| **Horizontal Line Rate ($f_H$)** | $\frac{4.5\text{ MHz}}{286} \approx 15,734.26\text{ Hz}$ | $15,737.70\text{ Hz}$ ($+0.021\%$ error) |
| **Line Period ($T_H$)** | $63.555\ \mu\text{s}$ | $63.542\ \mu\text{s}$ |
| **Vertical Field Rate ($f_V$)** | $59.94\text{ Hz} - 60.00\text{ Hz}$ | $60.067\text{ Hz}$ |
| **Total Lines per Field** | $262.5$ (interlaced) / $262$ (progressive) | $262\text{ lines}$ |
| **Active Video Lines** | $240\text{ lines}$ | $240\text{ lines}$ |
| **Horizontal Sync Pulse ($T_{sync}$)** | $4.70\ \mu\text{s} \pm 0.1\ \mu\text{s}$ | $4.79\ \mu\text{s}$ (46 clocks @ 9.6 MHz) |

---

## 3. Root Cause Analysis: The ~25° Diagonal Skew

### Flawed Configuration
The previous `/boot/firmware/config.txt` used:
```ini
dpi_timings=480 1 40 48 72 240 1 4 3 15 0 0 0 60 0 9600000 1
```

### The Math of the Defect
1. **Pixel Clock ($f_{pix}$):** $9.6\text{ MHz}$ ($T_{pix} = 104.167\text{ ns}$).
2. **Total Horizontal Clocks:**
   $$H_{total} = 480 + 40 + 48 + 72 = 640\text{ clocks}$$
3. **Resulting Line Frequency:**
   $$f_H = \frac{9,600,000}{640} = 15,000.00\text{ Hz} \quad (T_H = 66.667\ \mu\text{s})$$
4. **Phase Drift:**
   The MFD horizontal flyback oscillator locked to approximately $15.734\text{ kHz}$ ($T_{line} = 63.555\ \mu\text{s}$). The Pi emitted scanlines every $66.667\ \mu\text{s}$:
   $$\Delta T = 66.667\ \mu\text{s} - 63.555\ \mu\text{s} = 3.112\ \mu\text{s}$$
   $$\Delta \text{clocks} = \frac{3.112\ \mu\text{s}}{0.104167\ \mu\text{s}} = 29.87 \approx 30\text{ clocks/line}$$
5. **Geometric Shear:**
   Each successive line started $\approx 23$ display pixels to the right of the previous scanline. Over the 240 visible lines, vertical edges sheared into diagonal lines at:
   $$\theta = \arctan\left(\frac{\Delta Y}{\Delta X}\right) = \arctan\left(\frac{1\text{ scanline}}{2\text{ pixels}}\right) \approx 26.56^\circ \approx 25^\circ$$
   The user observed razor-sharp diagonal streaks instead of vertical borders.

---

## 4. Derived & Verified DPI Timings

To achieve $f_H = 15,734\text{ Hz}$ at $f_{pix} = 9.6\text{ MHz}$:
$$H_{total} = \frac{9,600,000}{15,734.26} = 610.13 \implies \mathbf{610\text{ clocks}}$$

### Horizontal Timing Breakdown ($H_{total} = 610$)
- **Active Pixels ($H_{act}$):** 480
- **Front Porch ($H_{fp}$):** 24 clocks ($2.50\ \mu\text{s}$)
- **Sync Pulse ($H_{sync}$):** 46 clocks ($4.79\ \mu\text{s}$, matches standard $4.7\ \mu\text{s}$ NTSC sync)
- **Back Porch ($H_{bp}$):** 60 clocks ($6.25\ \mu\text{s}$)
- **Check:** $480 + 24 + 46 + 60 = 610\text{ clocks} = 63.542\ \mu\text{s} \implies f_H = 15,737.70\text{ Hz}$

### Vertical Timing Breakdown ($V_{total} = 262$)
- **Active Lines ($V_{act}$):** 240
- **Front Porch ($V_{fp}$):** 4 lines
- **Sync Pulse ($V_{sync}$):** 3 lines
- **Back Porch ($V_{bp}$):** 15 lines
- **Check:** $240 + 4 + 3 + 15 = 262\text{ lines} \implies f_V = \frac{15,737.70}{262} = 60.067\text{ Hz}$

### Complete `dpi_timings` String
```ini
dpi_timings=480 1 24 46 60 240 1 4 3 15 0 0 0 60 0 9600000 1
```

---

## 5. Persistent Pi Configuration (`/boot/firmware/config.txt`)

On the Raspberry Pi Zero 2W, verify `/boot/firmware/config.txt` contains:

```ini
# DPI Display configuration for Prius Gen 2 MFD (VGA666 RGB DAC)
dtoverlay=vga666-6bpc
enable_dpi_lcd=1
display_default_lcd=1
dpi_group=2
dpi_mode=87
dpi_output_format=0x00017
dpi_timings=480 1 24 46 60 240 1 4 3 15 0 0 0 60 0 9600000 1
framebuffer_width=480
framebuffer_height=240
```

---

## 6. Frontend Service Configuration (`mfd.service`)

The user interface runs headlessly without X11 or Wayland, writing directly to the Linux framebuffer (`/dev/fb0`) via SDL dummy driver:

### Unit File: `/etc/systemd/system/mfd.service`
```ini
[Unit]
Description=CyberPunk Prius Gen 2 - MFD Frontend
After=network.target

[Service]
Type=simple
User=piotr
WorkingDirectory=/home/piotr/cyberpunk_computer
Environment="PYTHONUNBUFFERED=1"
Environment="SDL_VIDEODRIVER=dummy"
Environment="SDL_FBDEV=/dev/fb0"
Environment="SDL_NOMOUSE=1"
Environment="FRAMEBUFFER=/dev/fb0"
ExecStart=/home/piotr/cyberpunk_computer/.venv/bin/python -m cyberpunk_computer.frontend --host 10.42.0.1 --fullscreen --scale 1 --production
Restart=always
RestartSec=5
TimeoutStopSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Verification Commands
```bash
# Check service status
systemctl status mfd.service

# Check active framebuffer updates
python3 -c "import hashlib, time; h1 = hashlib.md5(open('/dev/fb0', 'rb').read(480*240*4)).hexdigest(); time.sleep(1); h2 = hashlib.md5(open('/dev/fb0', 'rb').read(480*240*4)).hexdigest(); print('Dynamic rendering active:', h1 != h2)"
```
