"""
User settings persistence.

Saves and loads user preferences to/from a JSON file.
"""

import json
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AmbientSettings:
    """Ambient lighting settings."""
    mode: str = "OFF"  # OFF, MANUAL, CYBER, SMOOTH, ROMANCE, MUSIC
    hue: int = 180  # 0-360
    saturation: int = 100  # 0-100
    brightness: int = 80  # 0-100


@dataclass
class LightsSettings:
    """Lights control settings."""
    mode: str = "AUTO"  # AUTO, MANUAL, OFF
    biled_mode: str = "OFF"  # OFF, ON, PWM
    biled_brightness: int = 100  # 0-100 (only for PWM mode)
    drl_enabled: bool = True


@dataclass
class AudioSettings:
    """Audio settings."""
    volume: int = 35
    bass: int = 0
    treble: int = 0
    balance: int = 0
    fader: int = 0
    position: int = 0  # 0=DRIVER, 1=FRONT, 2=CENTER, 3=ALL


@dataclass
class ClimateSettings:
    """Climate control settings."""
    target_temp: int = 21
    fan_speed: int = 3
    mode: int = 0  # 0=AUTO, 1=MANUAL, 2=ECO
    ac_on: bool = True
    recirculation: bool = False
    air_direction: int = 0  # 0=FACE, 1=FACE+FEET, 2=FEET, 3=DEFROST


@dataclass
class UserSettings:
    """All user-configurable settings."""
    ambient: AmbientSettings = field(default_factory=AmbientSettings)
    lights: LightsSettings = field(default_factory=LightsSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    climate: ClimateSettings = field(default_factory=ClimateSettings)


class SettingsManager:
    """
    Manages loading and saving user settings.
    
    Settings are stored in a JSON file in the user's config directory.
    """
    
    DEFAULT_FILENAME = "user_settings.json"
    
    def __init__(self, config_dir: Optional[Path] = None):
        """
        Initialize settings manager.
        
        Args:
            config_dir: Directory for settings file. Defaults to app directory.
        """
        if config_dir is None:
            # Use app directory by default
            config_dir = Path(__file__).parent.parent
        
        self.config_dir = Path(config_dir)
        self.settings_file = self.config_dir / self.DEFAULT_FILENAME
        self.settings = UserSettings()
        
        # Try to load existing settings
        self.load()
    
    def load(self) -> bool:
        """
        Load settings from file.
        
        Returns:
            True if settings were loaded, False if using defaults
        """
        if not self.settings_file.exists():
            logger.info(f"No settings file found at {self.settings_file}, using defaults")
            return False
        
        try:
            with open(self.settings_file, 'r') as f:
                data = json.load(f)
            
            # Parse nested dataclasses
            if 'ambient' in data:
                self.settings.ambient = AmbientSettings(**data['ambient'])
            if 'lights' in data:
                self.settings.lights = LightsSettings(**data['lights'])
            if 'audio' in data:
                self.settings.audio = AudioSettings(**data['audio'])
            if 'climate' in data:
                self.settings.climate = ClimateSettings(**data['climate'])
            
            logger.info(f"Loaded settings from {self.settings_file}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            return False
    
    def save(self) -> bool:
        """
        Save settings to file.
        
        Returns:
            True if settings were saved successfully
        """
        try:
            # Convert to dict
            data = {
                'ambient': asdict(self.settings.ambient),
                'lights': asdict(self.settings.lights),
                'audio': asdict(self.settings.audio),
                'climate': asdict(self.settings.climate),
            }
            
            with open(self.settings_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.debug(f"Saved settings to {self.settings_file}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            return False
    
    # Convenience accessors
    @property
    def ambient(self) -> AmbientSettings:
        return self.settings.ambient
    
    @property
    def lights(self) -> LightsSettings:
        return self.settings.lights
    
    @property
    def audio(self) -> AudioSettings:
        return self.settings.audio
    
    @property
    def climate(self) -> ClimateSettings:
        return self.settings.climate


# Global settings manager instance
_settings_manager: Optional[SettingsManager] = None


def get_settings() -> SettingsManager:
    """Get the global settings manager instance."""
    global _settings_manager
    if _settings_manager is None:
        _settings_manager = SettingsManager()
    return _settings_manager


def save_settings() -> bool:
    """Save current settings to disk."""
    return get_settings().save()
