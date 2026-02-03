#!/bin/bash
# Wrapper script to run cyberpunk_computer with direct framebuffer output
# Uses SDL dummy driver + direct /dev/fb0 memory-mapped output

# Use dummy video driver - we'll write directly to framebuffer
export SDL_VIDEODRIVER=dummy
export SDL_FBDEV=/dev/fb0
export SDL_NOMOUSE=1
export SDL_AUDIODRIVER=dummy
export FRAMEBUFFER=/dev/fb0

cd /home/piotr/cyberpunk_computer
exec /home/piotr/cyberpunk_computer/venv/bin/python -m cyberpunk_computer --production
