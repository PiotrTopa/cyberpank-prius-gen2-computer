# State Management Architecture

## Overview

The CyberPunk Prius Gen 2 Computer uses a centralized state management system inspired by Redux/Flux patterns, adapted for Python and real-time vehicle communication.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              APPLICATION                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ┌────────────────┐                                    ┌────────────────┐  │
│   │  INPUT PORTS   │                                    │    DISPLAY     │  │
│   │ (Serial/File)  │                                    │     (UI)       │  │
│   │                │                                    │                │  │
│   │  ┌──────────┐  │      ┌──────────────────┐         │  ┌──────────┐  │  │
│   │  │ AVC-LAN  │──┼─────▶│ IngressController│         │  │ Screens  │  │  │
│   │  │ Messages │  │      │                  │         │  │          │  │  │
│   │  └──────────┘  │      │  - Decode msgs   │         │  └──────────┘  │  │
│   │                │      │  - Create Actions│         │        ▲       │  │
│   │  ┌──────────┐  │      │  - Dispatch      │         │        │       │  │
│   │  │   CAN    │──┼─────▶│                  │         │  ┌─────┴────┐  │  │
│   │  │ Messages │  │      └────────┬─────────┘         │  │ Widgets  │  │  │
│   │  └──────────┘  │               │                   │  │          │  │  │
│   │                │               │                   │  └──────────┘  │  │
│   └────────────────┘               │                   └───────▲────────┘  │
│           ▲                        │                           │           │
│           │                        ▼                           │           │
│           │              ┌──────────────────┐                  │           │
│           │              │                  │                  │           │
│   ┌───────┴───────┐      │      STORE       │──────────────────┘           │
│   │ OUTPUT PORTS  │      │   (AppState)     │    subscribe(AUDIO, cb)      │
│   │ (Serial/Log)  │      │                  │    subscribe(CLIMATE, cb)    │
│   │               │      │  ┌────────────┐  │                              │
│   │  ┌─────────┐  │      │  │ AudioState │  │                              │
│   │  │ Egress  │◀─┼──────│  ├────────────┤  │                              │
│   │  │Controller│ │      │  │ClimateState│  │                              │
│   │  └─────────┘  │      │  ├────────────┤  │                              │
│   │               │      │  │VehicleState│  │                              │
│   └───────────────┘      │  ├────────────┤  │                              │
│                          │  │EnergyState │  │                              │
│                          │  ├────────────┤  │                              │
│                          │  │ConnectionSt│  │                              │
│                          │  └────────────┘  │                              │
│                          │                  │                              │
│                          └────────┬─────────┘                              │
│                                   │                                        │
│                                   │ dispatch(Action)                       │
│                                   ▼                                        │
│                          ┌──────────────────┐                              │
│                          │   RULES ENGINE   │◀─────────────────────────────┤
│                          │  - Compute state │     dispatch(SetVolume,      │
│                          │  - React to chgs │        source=UI)            │
│                          └──────────────────┘                              │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

## Key Concepts

### 1. Single Source of Truth

All application state lives in `AppState`:

```python
@dataclass(frozen=True)
class AppState:
    audio: AudioState       # Volume, bass, treble, etc.
    climate: ClimateState   # Temp, fan, AC, etc.
    vehicle: VehicleState   # READY, gear, speed
    energy: EnergyState     # Battery SOC, power flow
    connection: ConnectionState  # Gateway status
```

State is **immutable** (`frozen=True`) - updates create new state objects.

### 2. Actions

Actions describe "what happened":

```python
# From Gateway (incoming data)
SetVolumeAction(volume=35, source=ActionSource.GATEWAY)

# From UI (user interaction)
SetVolumeAction(volume=40, source=ActionSource.UI)
```

The `source` field determines behavior:
- `GATEWAY`: Data from vehicle - update state only
- `UI`: User action - update state AND send to vehicle
- `INTERNAL`: App logic - update state only

### 3. Store

The Store processes actions and notifies subscribers:

```python
store = Store()

# Subscribe to specific state slices
store.subscribe(StateSlice.AUDIO, self._on_audio_change)
store.subscribe(StateSlice.ALL, self._on_any_change)

# Dispatch actions
store.dispatch(SetVolumeAction(50, source=ActionSource.UI))
```

### 4. Virtual Twin IO

The IO layer bridges hardware to the state store:

```python
# Ingress: raw messages → Actions → Store
ingress = IngressController(store, input_port)
ingress.update()  # Processes pending messages

# Egress: UI Actions → Commands → OutputPort
egress = EgressController(store, output_port)
# (automatically sends when UI actions dispatched)
```

See [VIRTUAL_TWIN_ARCHITECTURE.md](./VIRTUAL_TWIN_ARCHITECTURE.md) for full details.

### 5. Middleware

Side effects run after state updates:

```python
def gateway_middleware(action: Action, store: Store) -> None:
    if action.source == ActionSource.UI:
        command = action_to_command(action)
        gateway.send(command)

store.add_middleware(gateway_middleware)
```

## Data Flow Examples

### Example 1: Volume Change from Vehicle

```
1. InputPort receives: {"id":2,"d":{"m":"190","s":"110","d":["00","23"]}}
2. IngressController decodes AVC-LAN message
3. Creates: SetVolumeAction(volume=35, source=GATEWAY)
4. Store.dispatch() updates state
5. Subscribers notified: UI redraws volume bar
```

### Example 2: User Changes Volume

```
1. User presses + on AudioScreen
2. UI calls: store.dispatch(SetVolumeAction(36, source=UI))
3. Store updates state
4. EgressController sees source=UI, generates AVC command
5. OutputPort sends command to vehicle
6. UI subscriber redraws (already updated)
```

### Example 3: CAN Battery Update

```
1. InputPort receives CAN: {"id":1,"d":{"i":"3C8","d":[...]}}
2. IngressController parses CAN ID 0x3C8 (battery SOC)
3. Creates: SetBatterySOCAction(soc=0.72, source=GATEWAY)
4. Store.dispatch() updates energy state
5. Energy Monitor widget redraws
```

## File Structure

```
cyberpunk_computer/
├── state/
│   ├── __init__.py         # Public API
│   ├── app_state.py        # State definitions
│   ├── actions.py          # Action types
│   ├── store.py            # Store implementation
│   ├── rules.py            # Rules engine
│   ├── selectors.py        # State accessors
│   └── rules_examples/     # Example rule implementations
│
├── io/                     # Virtual Twin IO layer
│   ├── ports.py            # InputPort/OutputPort interfaces
│   ├── ingress.py          # Input → Store
│   ├── egress.py           # Store → Output
│   ├── file_io.py          # File replay for development
│   ├── serial_io.py        # Serial for production
│   ├── mock_io.py          # Mocks for testing
│   └── factory.py          # VirtualTwin factory
│
├── comm/                   # Protocol decoders/encoders
│   ├── avc_decoder.py      # AVC-LAN protocol decoder
│   ├── avc_commands.py     # AVC-LAN command generation
│   └── can_decoder.py      # CAN bus decoder
│
└── ui/
    ├── screens/            # Full-screen views
    └── widgets/            # Reusable components
```

## Rules Engine

For computed state based on multiple inputs, use the Rules Engine:

```python
from cyberpunk_computer.state.rules import StateRule, RulesEngine

class DRLControlRule(StateRule):
    """Compute DRL output from user mode + gear + sensors."""
    
    @property
    def name(self) -> str:
        return "DRLControlRule"
    
    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.VEHICLE, StateSlice.LIGHTS}
    
    def evaluate(self, old_state, new_state, store):
        # Compute and dispatch...
        pass

# Register with engine
rules_engine = RulesEngine(store)
rules_engine.register(DRLControlRule())
```

See [VIRTUAL_TWIN_ARCHITECTURE.md](./VIRTUAL_TWIN_ARCHITECTURE.md) for complete details on the rules system.

## Usage in UI Components

### Screen Example

```python
class AudioScreen(Screen):
    def __init__(self, store: Store, ...):
        self._store = store
        self._unsubscribe = store.subscribe(
            StateSlice.AUDIO, 
            self._on_audio_update
        )
    
    def _on_audio_update(self, state: AppState):
        self._volume = state.audio.volume
        self._dirty = True
    
    def _on_volume_change(self, delta: int):
        new_vol = self._store.state.audio.volume + delta
        self._store.dispatch(
            SetVolumeAction(new_vol, source=ActionSource.UI)
        )
    
    def on_exit(self):
        self._unsubscribe()  # Clean up subscription
```

### Widget Example

```python
class VolumeBar(Widget):
    def update_from_state(self, state: AppState):
        self._value = state.audio.volume
        self._muted = state.audio.muted
```

## Benefits

1. **Predictable**: All state changes go through dispatch()
2. **Debuggable**: Log all actions to trace issues
3. **Testable**: Mock Store for unit tests
4. **Decoupled**: UI doesn't know about Gateway protocol
5. **No Spaghetti**: Clear unidirectional data flow
