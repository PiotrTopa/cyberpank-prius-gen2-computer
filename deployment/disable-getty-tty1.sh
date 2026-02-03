#!/bin/bash
# Script to disable getty on tty1 and enable the application to use it
# Run once on the RPI

echo "Disabling getty on tty1 to allow application framebuffer access..."

# Mask getty@tty1 to prevent it from starting
sudo systemctl mask getty@tty1.service

# Stop it if running
sudo systemctl stop getty@tty1.service

echo "✓ getty@tty1 disabled"
echo ""
echo "Now deploy the service with: python deploy.py --install --restart"
