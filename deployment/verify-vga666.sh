#!/bin/bash
# Verify VGA666 framebuffer configuration
# Run this on the RPI to confirm display setup

echo "=== VGA666 Framebuffer Configuration Check ==="
echo ""

# Check framebuffer device
echo "1. Framebuffer device:"
ls -l /dev/fb0 || echo "ERROR: /dev/fb0 not found!"
echo ""

# Check framebuffer name (should be BCM2708 FB for VGA666)
echo "2. Framebuffer driver:"
cat /sys/class/graphics/fb0/name 2>/dev/null || echo "Cannot read fb name"
echo ""

# Check current mode/resolution
echo "3. Framebuffer mode:"
cat /sys/class/graphics/fb0/mode 2>/dev/null || echo "Cannot read fb mode"
cat /sys/class/graphics/fb0/virtual_size 2>/dev/null || echo "Cannot read virtual size"
echo ""

# Check if VGA666 overlay is loaded
echo "4. Device tree overlays:"
dtoverlay -l | grep -i vga666 && echo "✓ VGA666 overlay loaded" || echo "✗ VGA666 overlay NOT loaded"
echo ""

# Check groups
echo "5. User permissions:"
groups piotr | grep -q video && echo "✓ piotr in video group" || echo "✗ piotr NOT in video group"
groups piotr | grep -q tty && echo "✓ piotr in tty group" || echo "✗ piotr NOT in tty group"
echo ""

# Check framebuffer console mapping
echo "6. Console to framebuffer mapping:"
cat /sys/class/graphics/fbcon/cursor_blink 2>/dev/null
for i in /sys/class/graphics/fb*/name; do
    [ -f "$i" ] && echo "  $(basename $(dirname $i)): $(cat $i)"
done
echo ""

# Check if getty is disabled on tty1
echo "7. Getty status on tty1:"
systemctl is-enabled getty@tty1.service 2>/dev/null || echo "getty@tty1 is masked (correct for app)"
echo ""

echo "=== Configuration Summary ==="
echo "VGA666 should be on /dev/fb0"
echo "Service should use TTYPath=/dev/tty1"
echo "SDL_FBDEV should be /dev/fb0"
echo "SDL_VIDEODRIVER should be fbcon"
