"""
User settings persistence.

Saves and loads user preferences to/from a JSON file.
Bridges between the Store's AppState and persistent storage.

Two categories of persistent data:
1. User preferences (ambient mode, lights, audio EQ) - managed by screens
2. Store state subset (display time base, brightness) - auto-saved from Store
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
    mid: int = 0
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
class DisplaySettings:
    """Display and UI preferences (persisted from Store state)."""
    screen_brightness: int = 100
    power_chart_time_base: int = 60  # seconds
    vfd_brightness: int = 100  # VFD satellite brightness


@dataclass
class DataSourceSettings:
    """Which CAN subscription groups are enabled.
    
    Each field corresponds to a toggleable group key in
    comm.subscription_groups.TOGGLEABLE_GROUPS.
    """
    battery_cells: bool = True
    hybrid_extended: bool = True
    engine_sensors: bool = True
    aux_battery: bool = True
    environment: bool = True
    odometer: bool = True

    def is_group_enabled(self, key: str) -> bool:
        """Check if a group is enabled by its key."""
        return getattr(self, key, True)

    def set_group_enabled(self, key: str, enabled: bool) -> None:
        """Enable or disable a group by its key."""
        if hasattr(self, key):
            setattr(self, key, enabled)

    def as_dict(self) -> dict:
        """Return enabled state as {key: bool} dict."""
        import dataclasses
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


@dataclass
class UserSettings:
    """All user-configurable settings."""
    ambient: AmbientSettings = field(default_factory=AmbientSettings)
    lights: LightsSettings = field(default_factory=LightsSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    climate: ClimateSettings = field(default_factory=ClimateSettings)
    display: DisplaySettings = field(default_factory=DisplaySettings)
    data_sources: DataSourceSettings = field(default_factory=DataSourceSettings)


def _safe_load(cls, data: dict):
    """
    Safely construct a dataclass from a dict.
    
    Ignores unknown keys (forward compat) and uses defaults
    for missing keys (backward compat with older settings files).
    """
    import dataclasses
    valid_fields = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    return cls(**filtered)


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
            
            # Parse nested dataclasses (tolerant of missing/extra fields)
            if 'ambient' in data:
                self.settings.ambient = _safe_load(AmbientSettings, data['ambient'])
            if 'lights' in data:
                self.settings.lights = _safe_load(LightsSettings, data['lights'])
            if 'audio' in data:
                self.settings.audio = _safe_load(AudioSettings, data['audio'])
            if 'climate' in data:
                self.settings.climate = _safe_load(ClimateSettings, data['climate'])
            if 'display' in data:
                self.settings.display = _safe_load(DisplaySettings, data['display'])
            if 'data_sources' in data:
                self.settings.data_sources = _safe_load(DataSourceSettings, data['data_sources'])
            
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
                'display': asdict(self.settings.display),
                'data_sources': asdict(self.settings.data_sources),
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
    
    @property
    def display(self) -> DisplaySettings:
        return self.settings.display

    @property
    def data_sources(self) -> DataSourceSettings:
        return self.settings.data_sources

    # --- Store state bridge ---

    def build_initial_app_state(self):
        """
        Build an AppState pre-filled with persisted user preferences.
        
        Only populates fields that represent user preferences.
        Live telemetry fields (temps, RPMs, SOC, etc.) use defaults.
        
        Returns:
            AppState with persisted values applied
        """
        from .state.app_state import (
            AppState, AudioState, DisplayState, VFDSatelliteState
        )
        
        s = self.settings
        
        return AppState(
            # Audio EQ preferences (source/muted are runtime — left as defaults)
            audio=AudioState(
                volume=s.audio.volume,
                bass=s.audio.bass,
                mid=s.audio.mid,
                treble=s.audio.treble,
                balance=s.audio.balance,
                fader=s.audio.fader,
            ),
            # Display preferences
            display=DisplayState(
                power_chart_time_base=s.display.power_chart_time_base,
            ),
            # VFD brightness
            vfd_satellite=VFDSatelliteState(
                brightness=s.display.vfd_brightness,
                time_base=s.display.power_chart_time_base,
            ),
            # UI preferences
            screen_brightness=s.display.screen_brightness,
            ambient_hue=s.ambient.hue,
            ambient_saturation=s.ambient.saturation,
            ambient_brightness=s.ambient.brightness,
        )
    
    def update_from_app_state(self, state) -> None:
        """
        Extract persistable preferences from current AppState.
        
        Call this before save() to capture Store state changes.
        Only updates preference fields — ignores live telemetry.
        
        Args:
            state: Current AppState from Store
        """
        # Audio EQ 
        self.settings.audio.volume = state.audio.volume
        self.settings.audio.bass = state.audio.bass
        self.settings.audio.mid = state.audio.mid
        self.settings.audio.treble = state.audio.treble
        self.settings.audio.balance = state.audio.balance
        self.settings.audio.fader = state.audio.fader
        
        # Display preferences
        self.settings.display.screen_brightness = state.screen_brightness
        self.settings.display.power_chart_time_base = state.display.power_chart_time_base
        self.settings.display.vfd_brightness = state.vfd_satellite.brightness
        
        # Ambient (hue/sat/brightness — mode is managed separately by screens)
        self.settings.ambient.hue = state.ambient_hue
        self.settings.ambient.saturation = state.ambient_saturation
        self.settings.ambient.brightness = state.ambient_brightness


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
