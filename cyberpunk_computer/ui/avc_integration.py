"""
AVC-LAN UI Integration Module.

Bridges the AVC-LAN state manager with UI components.
Handles event subscription and UI updates based on vehicle state changes.
"""

from typing import Callable, Dict, List, Optional, Any
from dataclasses import dataclass
import logging

from ..comm.avc_state import (
    AVCStateManager,
    AVCEventType,
    AudioState,
    ClimateState,
    VehicleState,
    DisplayState,
    AudioSource,
    ClimateMode
)
from ..comm.avc_decoder import AVCDecoder, AVCMessage

logger = logging.getLogger(__name__)


@dataclass
class UIStateSnapshot:
    """Snapshot of vehicle state for UI."""
    # Audio
    volume: int = 0
    muted: bool = False
    bass: int = 0
    treble: int = 0
    balance: int = 0
    fader: int = 0
    audio_source: str = "---"
    
    # Climate
    target_temp: float = 22.0
    inside_temp: Optional[float] = None
    outside_temp: Optional[float] = None
    fan_speed: int = 0
    ac_on: bool = False
    auto_mode: bool = False
    recirculation: bool = False
    
    # Vehicle
    ready_mode: bool = False
    ice_running: bool = False
    acc_on: bool = False
    park_mode: bool = False
    
    # Energy
    battery_soc: float = 0.6
    charging: bool = False
    discharging: bool = False
    
    # Connection
    connected: bool = False


class AVCUIBridge:
    """
    Bridges AVC-LAN events to UI updates.
    
    Subscribe to this bridge for UI-friendly state updates.
    """
    
    def __init__(self, state_manager: Optional[AVCStateManager] = None):
        """
        Initialize the UI bridge.
        
        Args:
            state_manager: AVC state manager instance (optional, can be set later)
        """
        self._state_manager = state_manager
        self._decoder = AVCDecoder()
        
        # UI state snapshot
        self._state = UIStateSnapshot()
        
        # Callbacks for UI updates
        self._callbacks: Dict[str, List[Callable[[UIStateSnapshot], None]]] = {
            "audio": [],
            "climate": [],
            "vehicle": [],
            "energy": [],
            "connection": [],
            "all": [],
        }
        
        # Subscribe to state manager events if provided
        if state_manager:
            self._subscribe_to_state_manager()
            
    def set_state_manager(self, state_manager: AVCStateManager) -> None:
        """Set or replace the state manager."""
        self._state_manager = state_manager
        self._subscribe_to_state_manager()
        
    def _subscribe_to_state_manager(self) -> None:
        """Subscribe to all state manager events."""
        if not self._state_manager:
            return
            
        # Audio events
        self._state_manager.subscribe(AVCEventType.VOLUME_CHANGE, self._on_audio_change)
        self._state_manager.subscribe(AVCEventType.SOURCE_CHANGE, self._on_audio_change)
        self._state_manager.subscribe(AVCEventType.MUTE_TOGGLE, self._on_audio_change)
        
        # Climate events
        self._state_manager.subscribe(AVCEventType.CLIMATE_STATE, self._on_climate_change)
        
        # Vehicle events
        self._state_manager.subscribe(AVCEventType.POWER_ON, self._on_vehicle_change)
        self._state_manager.subscribe(AVCEventType.POWER_OFF, self._on_vehicle_change)
        self._state_manager.subscribe(AVCEventType.DISPLAY_MODE, self._on_vehicle_change)
        
    def subscribe(self, category: str, callback: Callable[[UIStateSnapshot], None]) -> None:
        """
        Subscribe to state changes.
        
        Args:
            category: One of "audio", "climate", "vehicle", "energy", "connection", "all"
            callback: Function to call with updated state snapshot
        """
        if category in self._callbacks:
            self._callbacks[category].append(callback)
        else:
            logger.warning(f"Unknown category: {category}")
            
    def unsubscribe(self, category: str, callback: Callable) -> None:
        """Unsubscribe from state changes."""
        if category in self._callbacks and callback in self._callbacks[category]:
            self._callbacks[category].remove(callback)
            
    def _notify(self, categories: List[str]) -> None:
        """Notify subscribers of state change."""
        notified = set()
        
        for category in categories + ["all"]:
            for callback in self._callbacks.get(category, []):
                if callback not in notified:
                    try:
                        callback(self._state)
                    except Exception as e:
                        logger.error(f"Callback error: {e}")
                    notified.add(callback)
                    
    def _on_audio_change(self, event_type: AVCEventType, data: Any) -> None:
        """Handle audio state change from state manager."""
        if self._state_manager:
            audio = self._state_manager.audio_state
            self._state.volume = audio.volume
            self._state.muted = audio.muted
            self._state.bass = audio.bass
            self._state.treble = audio.treble
            self._state.balance = audio.balance
            self._state.fader = audio.fader
            self._state.audio_source = audio.source.name if audio.source else "---"
            
        self._notify(["audio"])
        
    def _on_climate_change(self, event_type: AVCEventType, data: Any) -> None:
        """Handle climate state change from state manager."""
        if self._state_manager:
            climate = self._state_manager.climate_state
            self._state.target_temp = climate.target_temp
            self._state.inside_temp = climate.inside_temp
            self._state.outside_temp = climate.outside_temp
            self._state.fan_speed = climate.fan_speed
            self._state.ac_on = climate.ac_on
            self._state.auto_mode = climate.mode == ClimateMode.AUTO
            self._state.recirculation = climate.recirculation
            
        self._notify(["climate"])
        
    def _on_vehicle_change(self, event_type: AVCEventType, data: Any) -> None:
        """Handle vehicle state change from state manager."""
        if self._state_manager:
            vehicle = self._state_manager.vehicle_state
            self._state.ready_mode = vehicle.ready
            self._state.ice_running = vehicle.ice_running
            self._state.acc_on = vehicle.acc_on
            self._state.park_mode = vehicle.park
            
        self._notify(["vehicle", "energy"])
        
    def process_raw_message(self, raw_data: dict) -> None:
        """
        Process raw gateway message and update state.
        
        Args:
            raw_data: Raw message dict from gateway (NDJSON format)
        """
        # Update connection status
        self._state.connected = True
        self._notify(["connection"])
        
        # Process through state manager (it handles decoding internally)
        if self._state_manager:
            self._state_manager.process_raw_message(raw_data)
            
    def get_state(self) -> UIStateSnapshot:
        """Get current UI state snapshot."""
        return self._state
        
    def set_connection_state(self, connected: bool) -> None:
        """Manually set connection state."""
        if self._state.connected != connected:
            self._state.connected = connected
            self._notify(["connection"])
            
    # ─────────────────────────────────────────────────────────────────────────
    # Direct state setters for mock/test mode
    # ─────────────────────────────────────────────────────────────────────────
    
    def mock_set_volume(self, volume: int) -> None:
        """Set volume directly (for testing)."""
        self._state.volume = volume
        self._notify(["audio"])
    
    def mock_set_audio(
        self,
        volume: Optional[int] = None,
        bass: Optional[int] = None,
        treble: Optional[int] = None,
        balance: Optional[int] = None,
        fader: Optional[int] = None
    ) -> None:
        """Set audio settings directly (for testing)."""
        if volume is not None:
            self._state.volume = volume
        if bass is not None:
            self._state.bass = bass
        if treble is not None:
            self._state.treble = treble
        if balance is not None:
            self._state.balance = balance
        if fader is not None:
            self._state.fader = fader
        self._notify(["audio"])
        
    def mock_set_temperature(self, target: float, inside: Optional[float] = None,
                            outside: Optional[float] = None) -> None:
        """Set temperatures directly (for testing)."""
        self._state.target_temp = target
        if inside is not None:
            self._state.inside_temp = inside
        if outside is not None:
            self._state.outside_temp = outside
        self._notify(["climate"])
        
    def mock_set_vehicle_state(
        self,
        ready: bool = False,
        ice_running: bool = False,
        park: bool = True
    ) -> None:
        """Set vehicle state directly (for testing)."""
        self._state.ready_mode = ready
        self._state.ice_running = ice_running
        self._state.park_mode = park
        self._notify(["vehicle", "energy"])
        
    def mock_set_energy_state(
        self,
        soc: float = 0.6,
        charging: bool = False,
        discharging: bool = False
    ) -> None:
        """Set energy state directly (for testing)."""
        self._state.battery_soc = soc
        self._state.charging = charging
        self._state.discharging = discharging
        self._notify(["energy"])
