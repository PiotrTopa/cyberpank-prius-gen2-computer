"""
VFD State Management.

Maintains the current display state based on received messages.
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum, auto


class FuelType(Enum):
    """Active fuel type."""
    OFF = auto()
    PTR = auto()  # Petrol
    LPG = auto()


class GearPosition(Enum):
    """Gear position."""
    P = auto()
    R = auto()
    N = auto()
    D = auto()
    B = auto()


@dataclass
class EnergyData:
    """Current energy/power data from host."""
    mg_power: float = 0.0      # -1.0 to +1.0 (motor/generator power)
    fuel_flow: float = 0.0     # 0.0 to 1.0 (fuel consumption)
    brake: float = 0.0         # 0.0 to 1.0 (brake pressure)
    speed: float = 0.0         # 0.0 to 1.0 (vehicle speed)
    battery_soc: float = 0.6   # 0.0 to 1.0 (state of charge)
    petrol_level: int = 30     # Liters (0-45)
    lpg_level: int = 45        # Liters (0-60)
    ice_running: bool = False  # ICE engine running


@dataclass
class StateData:
    """Current state flags from host."""
    active_fuel: FuelType = FuelType.OFF
    gear: GearPosition = GearPosition.P
    ready_mode: bool = False


@dataclass
class ConfigData:
    """Configuration data from host."""
    time_base: int = 60        # Power chart time base (seconds)
    brightness: int = 100      # Display brightness (0-100)


@dataclass
class VFDState:
    """
    Complete VFD display state.
    
    Updated by the receiver when messages arrive.
    Read by the renderer to draw the display.
    """
    energy: EnergyData = field(default_factory=EnergyData)
    state: StateData = field(default_factory=StateData)
    config: ConfigData = field(default_factory=ConfigData)
    
    # Connection status
    connected: bool = False
    last_message_time: Optional[float] = None
    message_count: int = 0
    
    def update_energy(self, data: dict) -> None:
        """Update energy data from message payload."""
        if "mg" in data:
            self.energy.mg_power = max(-1.0, min(1.0, float(data["mg"])))
        if "fl" in data:
            self.energy.fuel_flow = max(0.0, min(1.0, float(data["fl"])))
        if "br" in data:
            self.energy.brake = max(0.0, min(1.0, float(data["br"])))
        if "spd" in data:
            self.energy.speed = max(0.0, min(1.0, float(data["spd"])))
        if "soc" in data:
            self.energy.battery_soc = max(0.0, min(1.0, float(data["soc"])))
        if "ptr" in data:
            self.energy.petrol_level = max(0, min(45, int(data["ptr"])))
        if "lpg" in data:
            self.energy.lpg_level = max(0, min(60, int(data["lpg"])))
        if "ice" in data:
            self.energy.ice_running = bool(data["ice"])
    
    def update_state(self, data: dict) -> None:
        """Update state flags from message payload."""
        if "fuel" in data:
            fuel_str = data["fuel"].upper()
            if fuel_str == "PTR" or fuel_str == "PETROL":
                self.state.active_fuel = FuelType.PTR
            elif fuel_str == "LPG":
                self.state.active_fuel = FuelType.LPG
            else:
                self.state.active_fuel = FuelType.OFF
        
        if "gear" in data:
            gear_str = data["gear"].upper()
            gear_map = {"P": GearPosition.P, "R": GearPosition.R, 
                       "N": GearPosition.N, "D": GearPosition.D, "B": GearPosition.B}
            self.state.gear = gear_map.get(gear_str, GearPosition.P)
        
        if "rdy" in data:
            self.state.ready_mode = bool(data["rdy"])
    
    def update_config(self, data: dict) -> None:
        """Update configuration from message payload."""
        if "tb" in data:
            valid_bases = [15, 60, 300, 900, 3600]
            tb = int(data["tb"])
            if tb in valid_bases:
                self.config.time_base = tb
        
        if "bri" in data:
            self.config.brightness = max(0, min(100, int(data["bri"])))
    
    def process_message(self, message: dict) -> None:
        """
        Process an incoming NDJSON message.
        
        Expected format: {"id": 110, "d": {"t": "E|S|C|R", ...}}
        """
        import time
        
        # Check device ID
        if message.get("id") != 110:
            return
        
        data = message.get("d", {})
        msg_type = data.get("t", "")
        
        if msg_type == "E":
            self.update_energy(data)
        elif msg_type == "S":
            self.update_state(data)
        elif msg_type == "C":
            self.update_config(data)
        elif msg_type == "R":
            # Reset command - handled by renderer
            pass
        
        self.connected = True
        self.last_message_time = time.time()
        self.message_count += 1
