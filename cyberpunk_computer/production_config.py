"""
Production configuration for deployment to Raspberry Pi Zero 2W.

This module provides specialized configuration for production deployment
with proper logging levels, USB UART handling, and output routing.
"""

from dataclasses import dataclass
from typing import Optional

from .config import Config
from .io import VirtualTwinConfig, ExecutionMode


@dataclass
class ProductionConfig:
    """Production-specific configuration for RPI deployment."""
    
    # Serial/UART configuration
    serial_port: str = "/dev/ttyACM0"
    serial_baudrate: int = 1_000_000
    serial_reconnect: bool = True  # Auto-reconnect on disconnect
    serial_reconnect_delay: float = 2.0  # Seconds between reconnect attempts
    
    # UDP output configuration (for VFD satellite and monitoring)
    enable_udp_output: bool = True
    vfd_udp_host: str = "localhost"
    vfd_udp_port: int = 5110
    
    # Logging configuration
    log_level: str = "INFO"  # INFO, WARNING, ERROR for production
    log_to_file: bool = True
    log_file: str = "/var/log/cyberpunk_computer/app.log"
    log_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    log_backup_count: int = 5
    
    # Display configuration for RPI
    display_scale: int = 1  # Native resolution for embedded display
    display_fullscreen: bool = True
    
    # Performance settings
    target_fps: int = 30
    effects_enabled: bool = True


def create_production_app_config(prod_config: Optional[ProductionConfig] = None) -> Config:
    """
    Create application Config for production deployment.
    
    Args:
        prod_config: Production-specific configuration (uses defaults if None)
        
    Returns:
        Configured Config instance
    """
    if prod_config is None:
        prod_config = ProductionConfig()
    
    return Config(
        dev_mode=False,
        scale_factor=prod_config.display_scale,
        fullscreen=prod_config.display_fullscreen,
        gateway_port=prod_config.serial_port,
        gateway_enabled=True,
        target_fps=prod_config.target_fps,
        effects_enabled=prod_config.effects_enabled,
        show_fps=False,
        show_grid=False
    )


def create_production_twin_config(prod_config: Optional[ProductionConfig] = None) -> VirtualTwinConfig:
    """
    Create Virtual Twin configuration for production deployment.
    
    Args:
        prod_config: Production-specific configuration (uses defaults if None)
        
    Returns:
        Configured VirtualTwinConfig instance
    """
    if prod_config is None:
        prod_config = ProductionConfig()
    
    return VirtualTwinConfig(
        mode=ExecutionMode.PRODUCTION,
        serial_port=prod_config.serial_port,
        serial_baudrate=prod_config.serial_baudrate,
        enable_vfd_satellite=prod_config.enable_udp_output,
        vfd_udp_host=prod_config.vfd_udp_host,
        vfd_udp_port=prod_config.vfd_udp_port,
        verbose=False,  # Reduced logging for production
        log_commands=False  # Disable command logging for performance
    )


def get_default_production_config() -> ProductionConfig:
    """
    Get default production configuration for RPI Zero 2W.
    
    Returns:
        ProductionConfig with sensible defaults
    """
    return ProductionConfig(
        serial_port="/dev/ttyACM0",
        serial_baudrate=1_000_000,
        serial_reconnect=True,
        serial_reconnect_delay=2.0,
        enable_udp_output=True,
        vfd_udp_host="localhost",
        vfd_udp_port=5110,
        log_level="INFO",
        log_to_file=True,
        log_file="/var/log/cyberpunk_computer/app.log",
        display_scale=1,
        display_fullscreen=True,
        target_fps=30,
        effects_enabled=True
    )
