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
│   │    GATEWAY     │                                    │    DISPLAY     │  │
│   │   (Serial)     │                                    │     (UI)       │  │
│   │                │                                    │                │  │
│   │  ┌──────────┐  │      ┌──────────────────┐         │  ┌──────────┐  │  │
│   │  │ AVC-LAN  │──┼─────▶│  GatewayAdapter  │         │  │ Screens  │  │  │
│   │  │ Messages │  │      │                  │         │  │          │  │  │
│   │  └──────────┘  │      │  - Decode msgs   │         │  └──────────┘  │  │
│   │                │      │  - Create Actions│         │        ▲       │  │
│   │  ┌──────────┐  │      │  - Encode cmds   │         │        │       │  │
│   │  │   CAN    │──┼─────▶│                  │         │  ┌─────┴────┐  │  │
│   │  │ Messages │  │      └────────┬─────────┘         │  │ Widgets  │  │  │
│   │  └──────────┘  │               │                   │  │          │  │  │
│   │                │               │                   │  └──────────┘  │  │
│   └────────────────┘               │                   └───────▲────────┘  │
│           ▲                        │                           │           │
│           │                        ▼                           │           │
│           │              ┌──────────────────┐                  │           │
│           │              │                  │                  │           │
│           │              │      STORE       │──────────────────┘           │
│           │              │   (AppState)     │    subscribe(AUDIO, cb)      │
│           │              │                  │    subscribe(CLIMATE, cb)    │
│           │              │  ┌────────────┐  │                              │
│           │              │  │ AudioState │  │                              │
│           │              │  ├────────────┤  │                              │
│           │              │  │ClimateState│  │                              │
│           │              │  ├────────────┤  │                              │
│           │              │  │VehicleState│  │                              │
│           │              │  ├────────────┤  │                              │
│           │              │  │EnergyState │  │                              │
│           │              │  ├────────────┤  │                              │
│           │              │  │ConnectionSt│  │                              │
│           │              │  └────────────┘  │                              │
│           │              │                  │                              │
│           │              └────────┬─────────┘                              │
│           │                       │                                        │
│           │                       │ dispatch(Action)                       │
│           │                       ▼                                        │
│           │              ┌──────────────────┐                              │
│           │              │    MIDDLEWARE    │                              │
│           └──────────────│                  │                              │
│              send_cmd()  │  - Log actions   │                              │
│                          │  - Route to GW   │◀─────────────────────────────┤
│                          │  - Side effects  │     dispatch(SetVolume,      │
│                          └──────────────────┘        source=UI)            │
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

### 4. Gateway Adapter

Bridges Gateway protocol ↔ State Store:

```python
adapter = GatewayAdapter(store)

# Incoming: raw JSON → Actions → Store
adapter.process_message({"id": 2, "d": {...}})

# Outgoing: UI Action → Command → Gateway
# (handled via middleware, automatic)
```

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
1. Gateway receives: {"id":2,"d":{"m":"190","s":"110","d":["00","23"]}}
2. GatewayAdapter.process_message() decodes AVC-LAN
3. Creates: SetVolumeAction(volume=35, source=GATEWAY)
4. Store.dispatch() updates state
5. Subscribers notified: UI redraws volume bar
```

### Example 2: User Changes Volume

```
1. User presses + on AudioScreen
2. UI calls: store.dispatch(SetVolumeAction(36, source=UI))
3. Store updates state
4. Middleware sees source=UI, generates AVC command
5. Gateway sends command to vehicle
6. UI subscriber redraws (already updated)
```

### Example 3: CAN Battery Update

```
1. Gateway receives CAN: {"id":1,"d":{"id":"3C8","data":[...]}}
2. GatewayAdapter parses CAN ID 0x3C8 (battery SOC)
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
│   └── selectors.py        # State accessors
│
├── comm/
│   ├── gateway_adapter.py  # Gateway ↔ Store bridge
│   ├── avc_decoder.py      # AVC-LAN protocol
│   ├── avc_commands.py     # Command generation
│   └── gateway.py          # Serial communication
│
└── ui/
    ├── screens/            # Full-screen views
    └── widgets/            # Reusable components
```

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
