# Application Architecture

## Overview

The CyberPunk Computer is a pygame-based HMI application designed for embedded deployment on Raspberry Pi Zero 2W, with development support on desktop systems.

## Core Design Principles

### 1. Resolution Independence

The application always renders at **480×240** internally. Display scaling is handled as post-processing:

```
┌──────────────────────────────────────────────────────────────┐
│                    Rendering Pipeline                        │
│                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐  │
│  │   Game      │    │   Native    │    │   Scaled        │  │
│  │   Logic     │───►│   Surface   │───►│   Window        │  │
│  │             │    │   480×240   │    │   (960×480 etc) │  │
│  └─────────────┘    └─────────────┘    └─────────────────┘  │
│                           │                                  │
│                     (post-process)                           │
│                     - Scanlines                              │
│                     - Glow effects                           │
│                     - CRT simulation                         │
└──────────────────────────────────────────────────────────────┘
```

### 2. Input Abstraction

The input system abstracts physical controls through an event-based interface:

```python
class InputEvent:
    ROTATE_LEFT = "rotate_left"
    ROTATE_RIGHT = "rotate_right"
    PRESS_LIGHT = "press_light"   # Short/light press
    PRESS_STRONG = "press_strong" # Long/strong press
    BACK = "back"
```

**Development mapping (keyboard):**
- `←` → ROTATE_LEFT
- `→` → ROTATE_RIGHT
- `Enter` → PRESS_LIGHT
- `Space` → PRESS_STRONG
- `Escape` → BACK

**Production mapping (encoder):**
- Encoder rotation → ROTATE_LEFT/RIGHT
- Light click → PRESS_LIGHT
- Strong click → PRESS_STRONG

### 3. Encoder Configuration Protocol

When focus changes between widgets, the application sends configuration to the encoder:

```python
class EncoderMode:
    SMOOTH = "smooth"      # Continuous, for volume/brightness
    STEPPED = "stepped"    # Discrete positions
    BOUNDED = "bounded"    # With min/max limits

encoder_config = {
    "mode": EncoderMode.SMOOTH,
    "min": 0,
    "max": 100,
    "step": 1,
    "detents": False,      # Haptic detent feedback
    "current": 50
}
```

---

## UI Framework

### Widget Hierarchy

```
Widget (base)
├── Frame
│   ├── AudioFrame
│   ├── AmbientFrame
│   ├── ClimateFrame
│   └── LightsFrame
├── Control
│   ├── VolumeSlider
│   ├── ToggleSwitch
│   └── ValueDisplay
└── Container
    ├── Screen
    └── Panel
```

### Screen Management

```python
class ScreenManager:
    """Manages screen stack and transitions."""
    
    def push(self, screen: Screen) -> None:
        """Push a new screen onto the stack."""
        
    def pop(self) -> Screen:
        """Return to previous screen."""
        
    def replace(self, screen: Screen) -> None:
        """Replace current screen."""
```

### Focus System

The focus system manages which widget receives input:

```python
class FocusManager:
    """Manages focus navigation between focusable widgets."""
    
    def __init__(self, widgets: list[Widget]):
        self.widgets = widgets
        self.focus_index = 0
    
    def next(self) -> None:
        """Move focus to next widget."""
        
    def prev(self) -> None:
        """Move focus to previous widget."""
        
    @property
    def focused(self) -> Widget:
        """Get currently focused widget."""
```

---

## Main Screen Layout

```
┌─────────────────────────────────────────────────────────────┐
│                        480 × 240                             │
├───────────┬─────────────────────────────┬───────────────────┤
│           │                             │                   │
│  AUDIO    │                             │   CLIMATE         │
│  120×80   │                             │   120×80          │
│           │                             │                   │
├───────────┤        MAIN AREA            ├───────────────────┤
│           │         240×240             │                   │
│  AMBIENT  │                             │   LIGHTS          │
│  120×80   │       (reserved)            │   120×80          │
│           │                             │                   │
├───────────┤                             ├───────────────────┤
│           │                             │                   │
│  (spare)  │                             │   (spare)         │
│  120×80   │                             │   120×80          │
│           │                             │                   │
└───────────┴─────────────────────────────┴───────────────────┘
```

### Frame Details

| Frame | Location | Content (Main Screen) | Sub-screen |
|-------|----------|----------------------|------------|
| AUDIO | Top-Left | Volume indicator | Full audio control |
| AMBIENT | Mid-Left | ON/OFF status | Ambient lighting settings |
| CLIMATE | Top-Right | Temps (in/out/target) | Climate control |
| LIGHTS | Mid-Right | DRL/BiLED status | Light controls |

---

## Communication Layer

### Gateway Connection

```python
class GatewayConnection:
    """Manages serial connection to the Gateway."""
    
    def __init__(self, port: str, baudrate: int = 1_000_000):
        self.port = port
        self.baudrate = baudrate
        
    async def connect(self) -> bool:
        """Establish connection to Gateway."""
        
    async def send(self, device_id: int, data: dict) -> None:
        """Send message to Gateway."""
        
    async def receive(self) -> AsyncIterator[dict]:
        """Receive messages from Gateway."""
```

### Data Flow

```
┌─────────────┐         ┌─────────────┐         ┌─────────────┐
│   Widget    │◄───────►│   State     │◄───────►│   Gateway   │
│   (View)    │         │   Store     │         │   Client    │
└─────────────┘         └─────────────┘         └─────────────┘
      │                       │                       │
      │ render()              │ update()              │ parse()
      ▼                       ▼                       ▼
    Display              Local State            Serial Port
```

---

## Visual Style Guide

### Color Palette

```python
# Primary colors (VFD-inspired)
COLORS = {
    # Backgrounds
    "bg_dark": (8, 10, 15),        # Near-black with blue tint
    "bg_panel": (15, 20, 30),      # Panel background
    "bg_frame": (20, 25, 35),      # Frame background
    
    # Accents
    "cyan": (0, 255, 255),         # Primary accent
    "cyan_dim": (0, 128, 128),     # Dimmed cyan
    "magenta": (255, 0, 128),      # Secondary accent
    "orange": (255, 128, 0),       # Warning/highlight
    
    # Text
    "text_primary": (200, 220, 255),   # Main text
    "text_secondary": (100, 120, 150), # Dimmed text
    "text_highlight": (255, 255, 255), # Highlighted text
    
    # State
    "focus_glow": (0, 200, 255, 100),  # Focus indicator (with alpha)
    "active": (0, 255, 128),           # Active/enabled
    "inactive": (80, 80, 80),          # Inactive/disabled
}
```

### Typography

- **Headers:** Monospace, uppercase, letter-spacing
- **Values:** Large, high-contrast
- **Labels:** Small, dimmed

### Effects

1. **Scanlines** - Subtle horizontal lines (optional)
2. **Glow** - Bloom effect on bright elements
3. **Flicker** - Subtle animation on focused elements
4. **Transitions** - Smooth fade/slide between screens

---

## File Structure Rationale

```
cyberpunk_computer/
├── core/           # Engine fundamentals (app loop, rendering)
├── ui/             # All visual components
│   ├── widgets/    # Reusable components
│   ├── screens/    # Full-screen layouts
│   ├── colors.py   # Color definitions
│   └── fonts.py    # Font management
├── input/          # Input abstraction layer
├── comm/           # Gateway communication
├── state/          # Application state management
└── config.py       # All configuration in one place
```

This separation allows:
- Easy testing of individual components
- Clear dependency graph
- Simple mocking for development
