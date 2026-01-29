# Virtual Twin Architecture

## Overview

The CyberPunk Prius Computer follows a **Virtual Twin** architectural pattern. The application maintains a digital replica (twin) of the physical vehicle state. All UI rendering and business logic is driven by this virtual model, not by direct hardware communication.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                          VIRTUAL TWIN ARCHITECTURE                              │
├────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ╔═══════════════════════════════════════════════════════════════════════════╗ │
│  ║                        INPUT PORTS (Abstracted)                            ║ │
│  ╠═══════════════════════════════════════════════════════════════════════════╣ │
│  ║  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      ║ │
│  ║  │ Serial/UART │  │ File Replay │  │  Mock/Test  │  │   Future    │      ║ │
│  ║  │   (Prod)    │  │   (Dev)     │  │   (Unit)    │  │   (Socket)  │      ║ │
│  ║  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘      ║ │
│  ║         │                │                │                │              ║ │
│  ║         └────────────────┴────────────────┴────────────────┘              ║ │
│  ║                                   │                                        ║ │
│  ║                                   ▼                                        ║ │
│  ║                         ┌─────────────────┐                                ║ │
│  ║                         │  InputPort API  │  Common interface              ║ │
│  ║                         └────────┬────────┘                                ║ │
│  ╚══════════════════════════════════│════════════════════════════════════════╝ │
│                                      │                                          │
│                                      ▼                                          │
│  ┌───────────────────────────────────────────────────────────────────────────┐ │
│  │                        INGRESS CONTROLLER                                  │ │
│  │                                                                            │ │
│  │  - Receives raw messages from InputPort                                   │ │
│  │  - Decodes protocol (AVC-LAN, CAN, RS485)                                │ │
│  │  - Converts to domain Actions                                             │ │
│  │  - Dispatches to Store                                                    │ │
│  └────────────────────────────────────┬──────────────────────────────────────┘ │
│                                        │ dispatch(Action)                       │
│                                        ▼                                        │
│  ╔═══════════════════════════════════════════════════════════════════════════╗ │
│  ║                     VIRTUAL TWIN (Store + State)                           ║ │
│  ╠═══════════════════════════════════════════════════════════════════════════╣ │
│  ║                                                                            ║ │
│  ║  ┌─────────────────────────────────────────────────────────────────────┐  ║ │
│  ║  │                          AppState                                    │  ║ │
│  ║  │  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐           │  ║ │
│  ║  │  │  Audio    │ │ Climate   │ │ Vehicle   │ │  Energy   │           │  ║ │
│  ║  │  └───────────┘ └───────────┘ └───────────┘ └───────────┘           │  ║ │
│  ║  │  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐           │  ║ │
│  ║  │  │  Lights   │ │ Sensors   │ │ Satellites│ │ UI State  │           │  ║ │
│  ║  │  └───────────┘ └───────────┘ └───────────┘ └───────────┘           │  ║ │
│  ║  └─────────────────────────────────────────────────────────────────────┘  ║ │
│  ║                                                                            ║ │
│  ║  ┌─────────────────────────────────────────────────────────────────────┐  ║ │
│  ║  │                       State Rules Engine                             │  ║ │
│  ║  │                                                                      │  ║ │
│  ║  │  Rules react to state changes and compute derived state:             │  ║ │
│  ║  │  - DRLRule: drl_user_mode + gear + light_sensor → drl_output_status │  ║ │
│  ║  │  - AutoACRule: inside_temp + target_temp → ac_should_run            │  ║ │
│  ║  │  - ...                                                               │  ║ │
│  ║  └─────────────────────────────────────────────────────────────────────┘  ║ │
│  ║                                                                            ║ │
│  ╚═══════════════════════════════════════════════════════════════════════════╝ │
│                   │                                       │                     │
│                   │ subscribe()                           │ subscribe()         │
│                   ▼                                       ▼                     │
│  ┌────────────────────────────────┐    ┌────────────────────────────────────┐  │
│  │          UI LAYER              │    │        EGRESS CONTROLLER           │  │
│  │                                │    │                                     │  │
│  │  - Subscribes to state slices  │    │  - Subscribes to OUTPUT states     │  │
│  │  - Renders based on state only │    │  - Watches for changes needing HW  │  │
│  │  - User input → dispatch()     │    │  - Encodes commands for protocol   │  │
│  │                                │    │  - Sends via OutputPort            │  │
│  └────────────────────────────────┘    └──────────────────┬─────────────────┘  │
│                                                            │                    │
│                                                            ▼                    │
│  ╔═══════════════════════════════════════════════════════════════════════════╗ │
│  ║                       OUTPUT PORTS (Abstracted)                            ║ │
│  ╠═══════════════════════════════════════════════════════════════════════════╣ │
│  ║                         ┌─────────────────┐                                ║ │
│  ║                         │ OutputPort API  │  Common interface              ║ │
│  ║                         └────────┬────────┘                                ║ │
│  ║         ┌────────────────────────┼────────────────────────┐                ║ │
│  ║         │                        │                        │                ║ │
│  ║         ▼                        ▼                        ▼                ║ │
│  ║  ┌─────────────┐          ┌─────────────┐          ┌─────────────┐        ║ │
│  ║  │ Serial/UART │          │  Log/Stdout │          │  Mock/Test  │        ║ │
│  ║  │   (Prod)    │          │   (Dev)     │          │   (Unit)    │        ║ │
│  ║  └─────────────┘          └─────────────┘          └─────────────┘        ║ │
│  ╚═══════════════════════════════════════════════════════════════════════════╝ │
│                                                                                 │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Core Principles

### 1. Single Source of Truth

The `AppState` dataclass is the **only** place where vehicle state lives. Every component that needs to know about vehicle state reads from here. There is no hidden state.

### 2. Unidirectional Data Flow

```
InputPorts → IngressController → Store (dispatch) → State → [RulesEngine] → UI/EgressController
                                                              ↓
                                                          OutputPorts
```

Data flows in one direction. The UI never directly talks to hardware. The hardware never directly modifies UI.

### 3. State Changes Are Actions

Every state change is an `Action` with:
- **Type**: What happened (`SET_VOLUME`, `SET_GEAR`, etc.)
- **Source**: Where it came from (`GATEWAY`, `UI`, `INTERNAL`, `RULE`)
- **Data**: The new values

### 4. Computed State via Rules

Complex business logic (like the DRL example) lives in the **Rules Engine**. Rules:
- Subscribe to specific state slices
- React when relevant state changes
- Compute derived/output state
- Dispatch new actions to update that derived state

### 5. IO Abstraction

Input and output are abstracted behind port interfaces. This enables:
- **Production**: Serial UART to/from RP2040 Gateway
- **Development**: File replay input + console log output
- **Testing**: Mock ports for unit tests
- **Future**: Network sockets, additional buses

---

## Layer Descriptions

### Input Ports (`cyberpunk_computer/io/ports.py`)

Abstract interface for receiving messages:

```python
from abc import ABC, abstractmethod
from typing import Optional, Callable
from dataclasses import dataclass

@dataclass
class RawMessage:
    """Raw message from any input source."""
    device_id: int       # 0=system, 1=CAN, 2=AVC, 100+=satellites
    timestamp: float     # Unix timestamp
    data: dict           # Raw data payload
    sequence: Optional[int] = None

class InputPort(ABC):
    """Abstract input port interface."""
    
    @abstractmethod
    def start(self) -> None:
        """Start receiving messages."""
        pass
    
    @abstractmethod
    def stop(self) -> None:
        """Stop receiving messages."""
        pass
    
    @abstractmethod
    def poll(self) -> Optional[RawMessage]:
        """Poll for next available message (non-blocking)."""
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if the port is connected/active."""
        pass
```

### Output Ports (`cyberpunk_computer/io/ports.py`)

Abstract interface for sending commands:

```python
@dataclass
class OutgoingCommand:
    """Command to send to hardware."""
    device_id: int       # Target device (1=CAN, 2=AVC, 100+=satellites)
    command_type: str    # Command identifier
    payload: dict        # Command data

class OutputPort(ABC):
    """Abstract output port interface."""
    
    @abstractmethod
    def send(self, command: OutgoingCommand) -> bool:
        """Send a command. Returns True if queued/sent successfully."""
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if the port can send."""
        pass
```

### Ingress Controller (`cyberpunk_computer/io/ingress.py`)

Receives messages from InputPort and updates the Virtual Twin:

```python
class IngressController:
    """
    Ingress controller - bridges InputPort to Virtual Twin.
    
    Responsibilities:
    - Poll InputPort for messages
    - Decode protocol-specific data (AVC-LAN, CAN, RS485)
    - Convert to domain Actions
    - Dispatch to Store
    """
    
    def __init__(self, store: Store, input_port: InputPort):
        self._store = store
        self._input_port = input_port
        self._decoders = {
            DEVICE_AVC: AVCDecoder(),
            DEVICE_CAN: CANDecoder(),
            # Satellites use generic RS485 decoder
        }
    
    def update(self) -> None:
        """Process all pending messages from input port."""
        while True:
            msg = self._input_port.poll()
            if msg is None:
                break
            self._process_message(msg)
```

### Egress Controller (`cyberpunk_computer/io/egress.py`)

Subscribes to state changes and sends commands to OutputPort:

```python
class EgressController:
    """
    Egress controller - bridges Virtual Twin to OutputPort.
    
    Responsibilities:
    - Subscribe to relevant state changes
    - Determine which changes require hardware commands
    - Encode commands for appropriate protocol
    - Send via OutputPort
    """
    
    def __init__(self, store: Store, output_port: OutputPort):
        self._store = store
        self._output_port = output_port
        self._encoders = {
            DEVICE_AVC: AVCCommandGenerator(),
            # Satellites use generic RS485 encoder
        }
        
        # Subscribe to all state changes
        store.subscribe(StateSlice.ALL, self._on_state_change)
        
        # Track previous state for change detection
        self._prev_state: Optional[AppState] = None
    
    def _on_state_change(self, state: AppState) -> None:
        """Handle state changes that need hardware commands."""
        # Only process user-initiated or rule-computed changes
        # that should be sent to hardware
        pass
```

### State Rules Engine (`cyberpunk_computer/state/rules.py`)

Reactive business logic layer:

```python
from abc import ABC, abstractmethod
from typing import List, Set

class StateRule(ABC):
    """
    Base class for reactive state rules.
    
    Rules subscribe to specific state slices and compute
    derived state based on input states.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Rule identifier for logging/debugging."""
        pass
    
    @property
    @abstractmethod
    def watches(self) -> Set[StateSlice]:
        """State slices this rule reacts to."""
        pass
    
    @abstractmethod
    def evaluate(self, old_state: AppState, new_state: AppState, store: Store) -> None:
        """
        Evaluate rule and dispatch any resulting actions.
        
        Called when any watched state slice changes.
        """
        pass


class RulesEngine:
    """
    Manages and executes state rules.
    
    Rules are evaluated in order when their watched slices change.
    Rules can dispatch new actions which may trigger other rules.
    """
    
    def __init__(self, store: Store):
        self._store = store
        self._rules: List[StateRule] = []
        self._prev_state: Optional[AppState] = None
        
        # Subscribe to all changes
        store.subscribe(StateSlice.ALL, self._on_state_change)
    
    def register(self, rule: StateRule) -> None:
        """Register a rule with the engine."""
        self._rules.append(rule)
    
    def _on_state_change(self, state: AppState) -> None:
        """Evaluate applicable rules on state change."""
        if self._prev_state is None:
            self._prev_state = state
            return
        
        # Determine which slices changed
        changed = self._detect_changes(self._prev_state, state)
        
        # Evaluate rules that watch changed slices
        for rule in self._rules:
            if rule.watches & changed:
                rule.evaluate(self._prev_state, state, self._store)
        
        self._prev_state = state
```

---

## Example: Implementing the DRL Light Scenario

The DRL (Daytime Running Lights) scenario demonstrates the full architecture:

### Step 1: Define State

Add to `app_state.py`:

```python
class DRLUserMode(Enum):
    """User-selected DRL mode."""
    OFF = auto()
    ON = auto()
    AUTO = auto()

@dataclass(frozen=True)
class LightsState:
    """Lights system state."""
    # User-controlled mode
    drl_user_mode: DRLUserMode = DRLUserMode.AUTO
    
    # Computed output (what the lights should actually be)
    drl_output_active: bool = False
    
    # State from sensors (rain/light satellite)
    is_daytime: bool = True
    is_raining: bool = False

@dataclass(frozen=True)
class SatellitesState:
    """RS485 satellite devices state."""
    # Satellite 6: DRL controller (receives commands)
    drl_satellite_connected: bool = False
    
    # Satellite 7: Rain/Light sensor (sends data)
    rain_light_sensor_connected: bool = False
    rain_detected: bool = False
    light_level: int = 0  # 0-100 (0=dark, 100=bright)
```

### Step 2: Define Actions

Add to `actions.py`:

```python
# User changes DRL mode via UI
@dataclass
class SetDRLUserModeAction(Action):
    mode: DRLUserMode = DRLUserMode.AUTO
    
    def __init__(self, mode: DRLUserMode, source: ActionSource = ActionSource.UI):
        super().__init__(ActionType.SET_DRL_USER_MODE, source)
        self.mode = mode

# Satellite sensor reports light/rain data
@dataclass
class SetLightSensorDataAction(Action):
    is_daytime: bool = True
    is_raining: bool = False
    
    def __init__(self, is_daytime: bool, is_raining: bool, 
                 source: ActionSource = ActionSource.GATEWAY):
        super().__init__(ActionType.SET_LIGHT_SENSOR_DATA, source)
        self.is_daytime = is_daytime
        self.is_raining = is_raining

# Rule engine computes DRL output
@dataclass
class SetDRLOutputAction(Action):
    active: bool = False
    
    def __init__(self, active: bool, source: ActionSource = ActionSource.RULE):
        super().__init__(ActionType.SET_DRL_OUTPUT, source)
        self.active = active
```

### Step 3: Create the Rule

Create `cyberpunk_computer/state/rules/drl_rule.py`:

```python
from ..rules import StateRule
from ..store import Store, StateSlice
from ..app_state import AppState, DRLUserMode, GearPosition
from ..actions import SetDRLOutputAction, ActionSource

class DRLControlRule(StateRule):
    """
    Rule: Compute DRL output based on user mode, gear, and sensor data.
    
    Logic:
    - OFF mode: Always off
    - ON mode: On if not in PARK
    - AUTO mode: On if daytime AND not raining AND not in PARK
    """
    
    @property
    def name(self) -> str:
        return "DRLControlRule"
    
    @property
    def watches(self) -> set:
        return {StateSlice.LIGHTS, StateSlice.VEHICLE, StateSlice.SENSORS}
    
    def evaluate(self, old_state: AppState, new_state: AppState, store: Store) -> None:
        lights = new_state.lights
        vehicle = new_state.vehicle
        
        # Compute what DRL output should be
        should_be_active = self._compute_drl_output(
            user_mode=lights.drl_user_mode,
            gear=vehicle.gear,
            is_daytime=lights.is_daytime,
            is_raining=lights.is_raining
        )
        
        # Only dispatch if changed
        if should_be_active != lights.drl_output_active:
            store.dispatch(SetDRLOutputAction(
                active=should_be_active,
                source=ActionSource.RULE
            ))
    
    def _compute_drl_output(
        self, 
        user_mode: DRLUserMode,
        gear: GearPosition,
        is_daytime: bool,
        is_raining: bool
    ) -> bool:
        """Pure function: compute DRL output state."""
        # Never on in PARK
        if gear == GearPosition.PARK:
            return False
        
        if user_mode == DRLUserMode.OFF:
            return False
        elif user_mode == DRLUserMode.ON:
            return True
        else:  # AUTO
            return is_daytime and not is_raining
```

### Step 4: Register the Rule

In application initialization:

```python
# Create rules engine
rules_engine = RulesEngine(store)

# Register rules
rules_engine.register(DRLControlRule())
```

### Step 5: Configure Egress for DRL Satellite

In `EgressController`, add handler for DRL output changes:

```python
def _on_state_change(self, state: AppState) -> None:
    if self._prev_state is None:
        self._prev_state = state
        return
    
    # Check if DRL output changed
    if state.lights.drl_output_active != self._prev_state.lights.drl_output_active:
        self._send_drl_command(state.lights.drl_output_active)
    
    self._prev_state = state

def _send_drl_command(self, active: bool) -> None:
    """Send DRL command to satellite."""
    command = OutgoingCommand(
        device_id=DEVICE_SATELLITE_DRL,  # 106
        command_type="set_drl",
        payload={"drl": active}
    )
    self._output_port.send(command)
```

### Step 6: Configure Ingress for Light Sensor

In `IngressController`, handle messages from light/rain sensor satellite:

```python
def _process_satellite_message(self, msg: RawMessage) -> None:
    device_id = msg.device_id
    
    if device_id == DEVICE_SATELLITE_LIGHT_SENSOR:  # 107
        data = msg.data
        is_daytime = data.get("light_level", 0) > 30
        is_raining = data.get("rain_detected", False)
        
        self._store.dispatch(SetLightSensorDataAction(
            is_daytime=is_daytime,
            is_raining=is_raining,
            source=ActionSource.GATEWAY
        ))
```

---

## Adding New Scenarios

Follow this checklist for adding new reactive features:

### 1. Define State

- [ ] Add state fields to appropriate `*State` dataclass in `app_state.py`
- [ ] Add new `StateSlice` if needed in `store.py`
- [ ] Ensure state is immutable (frozen dataclass)

### 2. Define Actions

- [ ] Add `ActionType` entries for each state change
- [ ] Create action dataclasses in `actions.py`
- [ ] Include `source` field to track origin (GATEWAY, UI, RULE)

### 3. Add Reducers

- [ ] Add reducer logic in `store.py` `_reduce()` method
- [ ] Ensure affected slices are returned for proper notification

### 4. Create Rule (if computed state)

- [ ] Create rule class extending `StateRule`
- [ ] Define watched slices
- [ ] Implement pure computation logic
- [ ] Register rule with `RulesEngine`

### 5. Configure IO

- [ ] Add ingress decoder for new input sources
- [ ] Add egress encoder for output commands
- [ ] Register device ID mappings

### 6. Update UI (if user-facing)

- [ ] Subscribe to relevant state slice
- [ ] Create/update widgets for display
- [ ] Handle user input → dispatch actions

---

## Device ID Conventions

| ID Range | Type | Description |
|----------|------|-------------|
| 0 | System | Gateway control/status messages |
| 1 | CAN | Vehicle CAN bus messages |
| 2 | AVC-LAN | Toyota AVC-LAN protocol |
| 3-99 | Reserved | Future bus types |
| 100-199 | RS485 Satellites | External controller modules |

### Known Satellite Devices

| Device ID | Name | Direction | Description |
|-----------|------|-----------|-------------|
| 106 | DRL Controller | OUT | Controls DRL light state |
| 107 | Rain/Light Sensor | IN | Reports ambient light and rain |

---

## Mode Configuration

The application supports different IO modes:

### Production Mode
```python
# Serial input/output to RP2040 Gateway
input_port = SerialInputPort(port="/dev/ttyACM0")
output_port = SerialOutputPort(port="/dev/ttyACM0")
```

### Development Mode (File Replay)
```python
# Replay from recorded file
input_port = FileInputPort(filepath="assets/data/recording.ndjson")
# Log commands to console
output_port = LogOutputPort(prefix="[WOULD SEND]")
```

### Test Mode
```python
# Programmatic control for unit tests
input_port = MockInputPort()
output_port = MockOutputPort()

# In test:
input_port.inject(RawMessage(...))
assert output_port.last_sent == expected_command
```

---

## Best Practices

### 1. Keep State Flat

Prefer flat state over deeply nested objects. This makes change detection and subscriptions easier.

### 2. Rules Should Be Pure

Rule `evaluate()` methods should compute state, not perform side effects. Side effects (hardware commands) go in the Egress Controller.

### 3. One Source Per State Field

Each state field should typically be updated from one source. If multiple sources update the same field, clearly document the priority/merge logic.

### 4. Use ActionSource Correctly

- `GATEWAY`: Data from physical hardware
- `UI`: User interaction
- `RULE`: Computed by rules engine  
- `INTERNAL`: Application logic (timers, initialization)

### 5. Log State Changes in Dev Mode

Enable verbose logging to trace data flow:
```python
store = Store(verbose=True)
```

### 6. Test Rules in Isolation

Rules can be unit tested without the full store:
```python
def test_drl_rule():
    rule = DRLControlRule()
    result = rule._compute_drl_output(
        user_mode=DRLUserMode.AUTO,
        gear=GearPosition.DRIVE,
        is_daytime=True,
        is_raining=False
    )
    assert result == True
```
