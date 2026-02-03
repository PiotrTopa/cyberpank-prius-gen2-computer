#!/bin/bash
# Setup script for Raspberry Pi framebuffer access
# Run this once on the RPI to configure permissions

set -e

USER="${1:-piotr}"

echo "Setting up framebuffer access for user: $USER"

# 1. Add user to required groups
echo "Adding $USER to video and tty groups..."
sudo usermod -a -G video,tty "$USER"

# 2. Install udev rules for framebuffer access
echo "Installing udev rules..."
sudo cp 99-framebuffer.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

# 3. Set proper permissions on framebuffer
echo "Setting framebuffer permissions..."
sudo chmod 666 /dev/fb0 || true

# 4. Verify framebuffer exists
echo ""
echo "Checking framebuffer status:"
ls -l /dev/fb0
echo ""
cat /sys/class/graphics/fb0/name 2>/dev/null || echo "No framebuffer name available"
cat /sys/class/graphics/fb0/mode 2>/dev/null || echo "No mode info"
echo ""

# 5. Test pygame with fbcon
echo "Testing pygame with fbcon..."

# Use venv python if it exists
if [ -f "/home/$USER/cyberpunk_computer/venv/bin/python" ]; then
    PYTHON="/home/$USER/cyberpunk_computer/venv/bin/python"
    echo "Using venv python: $PYTHON"
else
    PYTHON="python3"
    echo "Warning: venv not found, using system python3"
fi

SDL_VIDEODRIVER=fbcon SDL_FBDEV=/dev/fb0 $PYTHON -c "
import pygame
pygame.init()
try:
    screen = pygame.display.set_mode((480, 240))
    print('✓ Pygame framebuffer test PASSED')
    pygame.quit()
except Exception as e:
    print(f'✗ Pygame framebuffer test FAILED: {e}')
    pygame.quit()
    exit(1)
"

echo ""
echo "Setup complete! Please log out and log back in for group changes to take effect."
echo "Then redeploy the service with: python deploy.py --install --restart"
