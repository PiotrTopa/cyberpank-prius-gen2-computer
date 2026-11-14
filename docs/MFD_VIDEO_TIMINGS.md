# Prius Gen 2 MFD video timings and Pi configuration

## 1. Hardware

The Toyota Prius Gen 2 MFD (7-inch, Denso receiver) takes analog **RGBS**: analog red, green,
blue and an active-low composite sync.

| Item | Value |
|---|---|
| Host | Raspberry Pi Zero v1.3 |
| Video output | DPI through a resistor-ladder DAC (VGA666 / Gert VGA pinout), RGB666 config 1 |
| Format | 480×240 @ 60 Hz progressive, 262 lines per field (NTSC field timing) |
| Sync | negative composite sync from the DPI block on **GPIO1** (header pin 28, also named ID_SC), jumpered to the MFD sync line; `dpi_output_format` bit 8 + `gpio=1=a2`. In use since 2026-11-05, picture stable |
| GPIO2 (VSYNC) | stuck high electrically (stays high when forced low, while GPIO3 and GPIO26 obey the same test); `vsync-pin-release.service` sets it to input at boot, because `dtoverlay=vga666` returns it to VSYNC and `gpio=2=ip` does not override that |
| Sync socket on the VGA666 board | DIP-14 wired for a 74HC86 XOR (VCC pin 14, GND pin 7), which combined H-sync (GPIO3) and V-sync (GPIO2) before GPIO1 was used; the chip is removed. An LM339 (same package, used in the AVC-LAN PHY) does not fit this socket: with one inserted the MFD gets no sync |

## 2. NTSC requirements and the chosen timing

| Parameter | NTSC | This configuration |
|---|---|---|
| Line rate f_H | 4.5 MHz / 286 ≈ 15 734.26 Hz | 15 737.70 Hz (+0.021 %) |
| Line period | 63.555 µs | 63.542 µs |
| Field rate | 59.94–60.00 Hz | 60.067 Hz |
| Lines per field | 262.5 interlaced / 262 progressive | 262 |
| Active lines | 240 | 240 |
| H sync pulse | 4.70 ± 0.1 µs | 4.79 µs (46 clocks @ 9.6 MHz) |

Pixel clock 9.6 MHz (104.167 ns). H_total = 9 600 000 / 15 734.26 = 610.13 → **610 clocks**:

| Horizontal | Clocks | Time |
|---|---|---|
| active | 480 | 50.0 µs |
| front porch | 24 | 2.50 µs |
| sync | 46 | 4.79 µs |
| back porch | 60 | 6.25 µs |
| total | 610 | 63.542 µs |

| Vertical | Lines |
|---|---|
| active | 240 |
| front porch | 4 |
| sync | 3 |
| back porch | 15 |
| total | 262 → 15 737.70 / 262 = 60.067 Hz |

```ini
dpi_timings=480 1 24 46 60 240 1 4 3 15 0 0 0 60 0 9600000 1
```

Rule: H_total must give a line rate within the MFD's lock range around 15.734 kHz. A line period
off by 30 clocks (H_total 640 → 15.000 kHz) makes every line start about 30 clocks later than the
previous one and shears vertical edges into ~26° diagonals.

## 3. `/boot/firmware/config.txt` (display part)

Full file: `cyberpunk-prius-gateway/raspberry_pi_config/zero13-image/config.txt` (the image that
runs on the card).

```ini
disable_fw_kms_setup=1          # no vc4-kms-v3d: the app writes the firmware framebuffer /dev/fb0
max_framebuffers=2
dtoverlay=vga666                # not vga666-6bpc
enable_dpi_lcd=1
display_default_lcd=1
dpi_group=2
dpi_mode=87
dpi_output_format=0x105         # RGB666 config 1 (low nibble 5) + combined sync on GPIO1 (bit 8)
gpio=1=a2                       # GPIO1 = DE function, carries the combined sync
dpi_timings=480 1 24 46 60 240 1 4 3 15 0 0 0 60 0 9600000 1
```

`cmdline.txt` carries `fbcon=map:99 logo.nologo` (keeps the console off fb0). The USB gadget
(`usb0`) is brought up late by the image's `g-ether.service`, not from `cmdline.txt`; see the
image README.

**The low nibble of `dpi_output_format` must be `5`.** `0x06` (RGB666 config 2) puts the DPI bit
lanes on the wrong DAC rungs: gray 51 renders saturated green, gray 128 blue, gray 204 pink, and the
G ramp black. The panel colour gains (`MFD_GAIN_R/G/B` in `mfd.service.d/equalizer.conf`) are 1.00
with the correct format.

## 4. Frontend unit

`mfd.service` runs `python -m cyberpunk_computer.frontend --host 192.168.100.1 --fullscreen --scale 1 --production`
as `piotr` with `SDL_VIDEODRIVER=dummy`, `SDL_FBDEV=/dev/fb0`, `SDL_NOMOUSE=1`, `SDL_AUDIODRIVER=dummy`,
`FRAMEBUFFER=/dev/fb0`; no X11/Wayland. Unit copies and deployment: [DEPLOYMENT.md](DEPLOYMENT.md).

Check that the UI is rendering (fb0 changes between two reads):

```bash
python3 -c "import hashlib,time;r=lambda:hashlib.md5(open('/dev/fb0','rb').read(480*240*4)).hexdigest();a=r();time.sleep(1);print('rendering:',a!=r())"
```

## Open gaps

- Cause of the stuck GPIO2 is open: a short on the VGA666 board or a damaged Pi pad. Test: GPIO2 as
  output low with the VGA666 removed.
