"""
Climate settings screen.

Full-screen menu for climate/HVAC configuration.
"""

import pygame
from typing import Tuple, List, Optional, Any
import time

from .base import Screen
from ..widgets.base import Rect
from ..colors import COLORS
from ..fonts import get_font, get_title_font, get_mono_font


class ClimateMenuItem:
    """A single climate menu item with label and value."""
    
    def __init__(
        self,
        label: str,
        value: Any,
        min_val: Any = None,
        max_val: Any = None,
        step: Any = 1,
        options: Optional[List[str]] = None,
        unit: str = "",
        readonly: bool = False
    ):
        """
        Initialize menu item.
        
        Args:
            label: Display label
            value: Current value
            min_val: Minimum value (for numeric)
            max_val: Maximum value (for numeric)
            step: Step size for adjustments
            options: List of options (for select type)
            unit: Unit suffix (°C, %, etc.)
            readonly: If true, item is display-only
        """
        self.label = label
        self.value = value
        self.min_val = min_val
        self.max_val = max_val
        self.step = step
        self.options = options
        self.unit = unit
        self.readonly = readonly
        
        if options and isinstance(value, int):
            self._option_index = value
        else:
            self._option_index = 0
    
    @property
    def display_value(self) -> str:
        """Get value as display string."""
        if self.options:
            return self.options[self._option_index]
        if self.value is None or self.value == "--":
            return "--"
        return f"{self.value}{self.unit}"
    
    def adjust(self, delta: int) -> None:
        """Adjust value by delta (positive = right/increase)."""
        if self.readonly:
            return
            
        if self.options:
            self._option_index = (self._option_index + delta) % len(self.options)
            self.value = self._option_index
        elif self.min_val is not None and self.max_val is not None:
            self.value = max(self.min_val, min(self.max_val, self.value + delta * self.step))


class ClimateScreen(Screen):
    """
    Climate settings screen.

    Live on AVC-LAN through :class:`~cyberpunk_computer.state.climate_model.ClimateController`
    when a store is given: every row is re-read from the store each frame
    (optimistic values shown until the next AVC status confirms them) and
    every edit is a climate command (see the table in climate_model.py).

    Presentation: a cabin view of the car with the air streams of the reported
    direction, the temperature and fan keys on the left, the four outlets
    below and single-tap tiles on the right (docs/MFD_MAIN_SCREEN.md). The
    rows below are the encoder's list and the data behind the picture.

    Rows: target temperature (0.5 °C steps), fan (1-7, "AUTO n" under AUTO),
    mode AUTO / MANUAL / OFF, A/C, intake, air direction (FACE, FACE+FEET,
    FEET, FEET+WS, DEFROST), front defrost, rear defogger, outside and inside
    temperature (read-only; inside temperature is not on the bus). The list
    scrolls to keep the selected row visible.

    Without a store (tests, demo) the rows are static values set through the
    legacy ``set_value_from_avc`` / ``update_temperatures`` helpers.
    """

    # Layout constants
    HEADER_HEIGHT = 30
    FOOTER_HEIGHT = 22
    ITEM_HEIGHT = 24
    ITEM_PADDING = 2
    SIDE_MARGIN = 16

    MODE_OPTIONS = ["AUTO", "MANUAL", "OFF"]
    ON_OFF = ["ON", "OFF"]
    INTAKE_OPTIONS = ["FRESH", "RECIRC"]

    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        temp_target: float = 22.0,
        temp_in: Optional[float] = None,
        temp_out: Optional[float] = None,
        ac_on: bool = True,
        auto_mode: bool = True,
        recirc: bool = False,
        store=None,
        controller=None,
    ):
        """Initialize climate screen (``store`` or ``controller`` = live)."""
        super().__init__(size, app)
        from ...state.climate_model import AIR_DIRECTIONS, AIR_DIRECTION_LABELS, ClimateController

        self._air_modes = list(AIR_DIRECTIONS)
        if controller is None and store is not None:
            controller = ClimateController(store)
        self._ctl = controller

        self.items: List[ClimateMenuItem] = [
            ClimateMenuItem("TARGET TEMP", float(temp_target), 18.0, 28.0, 0.5, unit="°C"),
            ClimateMenuItem("FAN SPEED", 3, 1, 7, 1),
            ClimateMenuItem("MODE", 0 if auto_mode else 1, options=list(self.MODE_OPTIONS)),
            ClimateMenuItem("A/C", 0 if ac_on else 1, options=list(self.ON_OFF)),
            ClimateMenuItem("AIR INTAKE", 1 if recirc else 0, options=list(self.INTAKE_OPTIONS)),
            ClimateMenuItem("AIR DIRECTION", 0,
                            options=[AIR_DIRECTION_LABELS[m] for m in AIR_DIRECTIONS]),
            ClimateMenuItem("FRONT DEFROST", 1, options=list(self.ON_OFF)),
            ClimateMenuItem("REAR DEFOGGER", 1, options=list(self.ON_OFF)),
            # Outside temp: AVC-LAN only (1C6->110 00 E0 5D F7); -- while no
            # fresh AVC value. Inside temp is not on AVC-LAN.
            ClimateMenuItem("OUTSIDE TEMP",
                            int(temp_out) if temp_out is not None else "--",
                            unit="°C", readonly=True),
            ClimateMenuItem("INSIDE TEMP",
                            int(temp_in) if temp_in is not None else "--",
                            unit="°C", readonly=True),
        ]
        self._by_label = {item.label: item for item in self.items}
        self._text: dict = {}           # label -> live display text (store mode)
        self._live = False
        self._stale = True

        # Navigation
        self._selected_index = 0
        self._editing = False
        self._scroll_row = 0

        # Inactivity tracking
        self._last_activity = time.time()

        self._focus_visible = False     # the encoder focus box (after encoder use)

        # Cabin picture (rendered once) and small pictograms
        self._cabin_surf = None
        self._cam = None
        self._pics: dict = {}

        # Legacy callback (label, value) — used only without a store
        self._on_value_changed = None
        self.sync_from_model()

    # ── live data ─────────────────────────────────────────────────────────

    @property
    def controller(self):
        return self._ctl

    def sync_from_model(self) -> None:
        """Refresh every row from the climate view-model (no-op without one)."""
        if self._ctl is None:
            return
        v = self._ctl.view()
        self._live, self._stale = v.live, v.stale
        it = self._by_label
        it["TARGET TEMP"].value = v.target_temp
        it["FAN SPEED"].value = v.fan
        self._set_option("MODE", {"auto": 0, "manual": 1, "off": 2}.get(v.mode))
        self._set_option("A/C", 0 if v.ac else 1)
        self._set_option("AIR INTAKE", 1 if v.recirculation else 0)
        self._set_option("AIR DIRECTION", self._air_modes.index(v.air_direction)
                         if v.air_direction in self._air_modes else None)
        self._set_option("FRONT DEFROST", 0 if v.front_defrost else 1)
        self._set_option("REAR DEFOGGER", 0 if v.rear_defogger else 1)
        it["OUTSIDE TEMP"].value = int(v.outside_temp) if v.outside_temp is not None else "--"
        it["INSIDE TEMP"].value = int(v.inside_temp) if v.inside_temp is not None else "--"
        unknown = "--" if not v.live else None
        self._text = {
            "TARGET TEMP": v.target_text + ("°C" if v.target_text not in ("OFF", "--") else ""),
            "FAN SPEED": v.fan_text,
            "MODE": v.mode_text,
            "A/C": unknown or ("ON" if v.ac else "OFF"),
            "AIR INTAKE": unknown or ("RECIRC" if v.recirculation else "FRESH"),
            "AIR DIRECTION": v.air_direction_text,
            "FRONT DEFROST": unknown or ("ON" if v.front_defrost else "OFF"),
            "REAR DEFOGGER": unknown or ("ON" if v.rear_defogger else "OFF"),
        }

    def _set_option(self, label: str, index: Optional[int]) -> None:
        item = self._by_label[label]
        if index is None:
            return
        item._option_index = index
        item.value = index

    def _display_text(self, item: ClimateMenuItem) -> str:
        text = self._text.get(item.label) if self._ctl is not None else None
        if text is not None:
            return text
        if item.label == "TARGET TEMP" and isinstance(item.value, (int, float)):
            from ...state.climate_model import format_temp
            return f"{format_temp(item.value)}{item.unit}"
        return item.display_value

    def _command(self, item: ClimateMenuItem, delta: int) -> None:
        """Turn an encoder step on ``item`` into a climate command."""
        ctl = self._ctl
        label = item.label
        if label == "TARGET TEMP":
            ctl.step_target_temp(delta)
        elif label == "FAN SPEED":
            ctl.step_fan(delta)
        elif label == "MODE":
            v = ctl.view()
            i = {"auto": 0, "manual": 1, "off": 2}.get(v.mode, -1 if delta > 0 else 0)
            ctl.set_mode(("auto", "manual", "off")[(i + delta) % 3])
        elif label == "A/C":
            ctl.toggle_ac()
        elif label == "AIR INTAKE":
            ctl.toggle_recirculation()
        elif label == "AIR DIRECTION":
            ctl.step_air_direction(delta)
        elif label == "FRONT DEFROST":
            ctl.toggle_front_defrost()
        elif label == "REAR DEFOGGER":
            ctl.toggle_rear_defogger()
        self.sync_from_model()

    def _adjust(self, item: ClimateMenuItem, delta: int) -> None:
        if self._ctl is not None:
            self._command(item, delta)
            return
        item.adjust(delta)
        self._notify_value_changed(item.label, item.value)

    @property
    def target_temp(self) -> float:
        """Get current target temperature."""
        return self.items[0].value

    @property
    def ac_on(self) -> bool:
        """Get A/C state."""
        return self.items[3].value == 0

    @property
    def auto_mode(self) -> bool:
        """Get auto mode state."""
        return self.items[2].value == 0

    @property
    def recirc(self) -> bool:
        """Get recirculation state."""
        return self.items[4].value == 1

    def on_enter(self) -> None:
        """Reset activity timer on enter."""
        self._last_activity = time.time()
        self.sync_from_model()

    def _get_timeout(self) -> float:
        """Get screen exit timeout from config."""
        if self.app and hasattr(self.app, 'config'):
            return self.app.config.timeout_screen_exit
        return 30.0  # Default fallback

    def update(self, dt: float) -> None:
        """Keep rows live; check for inactivity timeout."""
        super().update(dt)
        self.sync_from_model()

        if time.time() - self._last_activity > self._get_timeout():
            self._exit_screen()

    def _reset_activity(self) -> None:
        """Reset inactivity timer."""
        self._last_activity = time.time()

    def _exit_screen(self) -> None:
        """Exit back to main screen."""
        if self.app:
            self.app.pop_screen()

    def _notify_value_changed(self, item_label: str, value: Any) -> None:
        """Legacy hook (no-store mode): report a user change."""
        if self._on_value_changed:
            self._on_value_changed(item_label, value)

    def set_on_value_changed(self, callback) -> None:
        """
        Set callback for value changes (used only without a store).

        Args:
            callback: Function(label: str, value: Any)
        """
        self._on_value_changed = callback

    def set_value_from_avc(self, label: str, value: Any) -> None:
        """
        Update value from AVC-LAN (without triggering callback). With a store
        the rows follow the store anyway and this is overwritten next frame.
        """
        item = self._by_label.get(label)
        if item is None:
            return
        if item.options:
            if isinstance(value, int) and 0 <= value < len(item.options):
                item._option_index = value
                item.value = value
        else:
            item.value = value

    def update_temperatures(self, inside: Optional[float] = None,
                          outside: Optional[float] = None) -> None:
        """
        Update temperature readings (no-store mode).

        Args:
            inside: Inside temperature (°C)
            outside: Outside temperature (°C)
        """
        if inside is not None:
            self._by_label["INSIDE TEMP"].value = int(inside)
        self._by_label["OUTSIDE TEMP"].value = int(outside) if outside is not None else "--"

    # ── scrolling ─────────────────────────────────────────────────────────

    @property
    def _stride(self) -> int:
        return self.ITEM_HEIGHT + self.ITEM_PADDING

    @property
    def _list_top(self) -> int:
        return self.HEADER_HEIGHT + self.ITEM_PADDING + 4

    @property
    def visible_rows(self) -> int:
        return max(1, (self.height - self.FOOTER_HEIGHT - self._list_top) // self._stride)

    def _ensure_visible(self) -> None:
        n = self.visible_rows
        if self._selected_index < self._scroll_row:
            self._scroll_row = self._selected_index
        elif self._selected_index >= self._scroll_row + n:
            self._scroll_row = self._selected_index - n + 1
        self._scroll_row = max(0, min(self._scroll_row, max(0, len(self.items) - n)))

    _ensure_selected_visible = _ensure_visible   # MenuListZone hook

    def _scroll_px(self) -> int:
        return self._scroll_row * self._stride

    def handle_input(self, event) -> bool:
        """Handle input events."""
        from ...input.manager import InputEvent as IE
        
        self._reset_activity()

        # BACK always exits
        if event == IE.BACK:
            self._editing = False
            self._exit_screen()
            return True
        
        current_item = self.items[self._selected_index]
        self._focus_visible = True

        if self._editing:
            if current_item.readonly:
                # Readonly items can't be edited
                self._editing = False
                return True
            
            if event == IE.ROTATE_LEFT:
                self._adjust(current_item, -1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._adjust(current_item, 1)
                return True
            elif event == IE.PRESS_LIGHT:
                self._editing = False
                return True
            elif event == IE.PRESS_STRONG:
                self._editing = False
                self._exit_screen()
                return True
        else:
            if event in (IE.ROTATE_LEFT, IE.ROTATE_RIGHT):
                # the read-outs (outside / inside temperature) are not stops
                step = -1 if event == IE.ROTATE_LEFT else 1
                i = self._selected_index
                for _ in range(len(self.items)):
                    i = (i + step) % len(self.items)
                    if not self.items[i].readonly:
                        break
                self._selected_index = i
                return True
            elif event == IE.PRESS_LIGHT:
                if not current_item.readonly:
                    self._editing = True
                return True
            elif event == IE.PRESS_STRONG:
                self._exit_screen()
                return True
        
        return False
    
    # ── layout (native pixels; every touch target is at least 44 px high) ──

    BACK_RECT = (4, 2, 62, 21)
    TEMP_UP = (4, 26, 116, 44)
    TEMP_DOWN = (4, 112, 116, 44)
    FAN_DOWN = (4, 190, 56, 46)
    FAN_UP = (64, 190, 56, 46)
    CABIN = (124, 26, 232, 160)
    VENT_TILES = tuple((124 + i * 59, 190, 55, 46) for i in range(4))
    VENT_MODES = ("face", "face_foot", "foot", "windscreen_foot")
    VENT_LABELS = (("FACE",), ("FACE", "FEET"), ("FEET",), ("FEET", "SCRN"))
    TILES = {                                   # right column, 56 x 66 each
        "AUTO": (360, 26, 56, 66), "A/C": (420, 26, 56, 66),
        "INTAKE": (360, 96, 56, 66), "OFF": (420, 96, 56, 66),
        "FRONT": (360, 166, 56, 66), "REAR": (420, 166, 56, 66),
    }
    # encoder item -> the rectangles that light up for it
    FOCUS = {
        "TARGET TEMP": ((4, 26, 116, 130),),
        "FAN SPEED": ((4, 158, 116, 78),),
        "MODE": ((360, 26, 56, 66), (420, 96, 56, 66)),
        "A/C": ((420, 26, 56, 66),),
        "AIR INTAKE": ((360, 96, 56, 66),),
        "AIR DIRECTION": ((124, 190, 232, 46),),
        "FRONT DEFROST": ((360, 166, 56, 66),),
        "REAR DEFOGGER": ((420, 166, 56, 66),),
    }

    # cabin geometry: (x forward from the car's centre, z up), metres
    STREAMS = {
        "face": (((0.86, 0.96), (0.48, 1.00), (0.08, 1.10)),),
        "foot": (((0.86, 0.76), (0.70, 0.50), (0.34, 0.36)),),
        "screen": (((0.96, 1.00), (0.86, 1.20), (0.56, 1.38)),),
    }
    VENT_STREAMS = {"face": ("face",), "face_foot": ("face", "foot"), "foot": ("foot",),
                    "windscreen_foot": ("screen", "foot"), "defrost": ("screen",)}

    # ── what the screen shows ─────────────────────────────────────────────

    def _snapshot(self) -> dict:
        """One dict for drawing, from the live view-model or, without a
        store, from the static rows. ``known`` False = draw no state."""
        if self._ctl is not None:
            v = self._ctl.view()
            known = bool(v.live)
            return {
                "live": v.live, "stale": v.stale, "known": known,
                "temp": v.target_text, "fan": v.fan if known else 0, "fan_text": v.fan_text,
                "mode": v.mode if known else "unknown", "ac": known and v.ac,
                "recirc": known and v.recirculation, "dir": v.air_direction if known else None,
                "front": known and v.front_defrost, "rear": known and v.rear_defogger,
                "out": v.outside_temp, "inside": v.inside_temp,
                "toggles_ok": known and not v.stale,
            }
        it = self._by_label
        from ...state.climate_model import format_temp
        mode = ("auto", "manual", "off")[it["MODE"]._option_index]
        out = it["OUTSIDE TEMP"].value
        ins = it["INSIDE TEMP"].value
        d = it["AIR DIRECTION"]._option_index
        return {
            "live": True, "stale": False, "known": True,
            "temp": format_temp(it["TARGET TEMP"].value), "fan": int(it["FAN SPEED"].value),
            "fan_text": str(it["FAN SPEED"].value), "mode": mode,
            "ac": it["A/C"]._option_index == 0, "recirc": it["AIR INTAKE"]._option_index == 1,
            "dir": self._air_modes[d] if 0 <= d < len(self._air_modes) else None,
            "front": it["FRONT DEFROST"]._option_index == 0,
            "rear": it["REAR DEFOGGER"]._option_index == 0,
            "out": out if isinstance(out, (int, float)) else None,
            "inside": ins if isinstance(ins, (int, float)) else None,
            "toggles_ok": True,
        }

    # ── cabin picture ─────────────────────────────────────────────────────

    def _cabin_camera(self):
        from ..scene.model import Camera
        x, y, w, h = self.CABIN
        f = w * 30.0 / 3.3                             # 3.3 m of the car across the picture:
        cx = 0.15                                      # the cabin, nose and tail cropped
        return Camera((cx, 30.0, 0.80), (cx, 0.0, 0.80), f, w / 2.0, h / 2.0 + 4)

    def _cabin_base(self) -> pygame.Surface:
        """The NHW20 in side view as an X-ray (nose left): body lines only,
        seat, occupant, dashboard. Rendered once."""
        if self._cabin_surf is not None:
            return self._cabin_surf
        from ..scene.car import CarRenderer, car_palette
        x, y, w, h = self.CABIN
        surf = pygame.Surface((w, h))
        surf.fill((0, 0, 0))
        cam = self._cam = self._cabin_camera()
        pal = car_palette(COLORS)
        pal.update(body=(0, 0, 0), glass=(10, 9, 24), line3=(0, 150, 134), line2=(0, 74, 70),
                   line1=(0, 46, 46), tyre=(0, 0, 0), rim=(0, 60, 58), head_off=(0, 0, 0),
                   tail_off=(0, 0, 0))
        CarRenderer(pal).draw(surf, cam)

        def P(px, pz):
            return cam.project((px, 0.0, pz))[:2]

        soft = (0, 96, 90)
        # seat, occupant, wheel, dashboard
        pygame.draw.lines(surf, soft, False, [P(-0.52, 1.02), P(-0.36, 0.48), P(0.12, 0.44)])
        pygame.draw.lines(surf, COLORS["cyan_mid"], False,
                          [P(-0.20, 0.98), P(-0.28, 0.56), P(0.22, 0.60), P(0.52, 0.30)])
        hx, hy = P(-0.14, 1.12)
        pygame.draw.circle(surf, COLORS["cyan_mid"], (int(hx), int(hy)), 7, 1)
        pygame.draw.line(surf, COLORS["cyan_mid"], P(-0.18, 0.90), P(0.30, 0.86))
        pygame.draw.line(surf, soft, P(0.44, 0.98), P(0.34, 0.80), 2)
        pygame.draw.lines(surf, soft, False, [P(1.18, 0.96), P(0.86, 1.00), P(0.80, 0.70), P(0.98, 0.42)])
        self._cabin_surf = surf
        return surf

    def _stream(self, surface, pts, color, count, phase) -> None:
        """Chevrons moving along a polyline (screen points)."""
        segs = []
        total = 0.0
        for a, b in zip(pts, pts[1:]):
            ln = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
            segs.append((a, b, ln))
            total += ln
        if total < 1.0:
            return
        gap = total / count
        for i in range(count):
            dist = (phase * gap + i * gap) % total
            for a, b, ln in segs:
                if dist <= ln:
                    t = dist / ln
                    x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                    ux, uy = (b[0] - a[0]) / ln, (b[1] - a[1]) / ln
                    nx, ny = -uy, ux
                    pygame.draw.lines(surface, color, False, [
                        (x - ux * 5 + nx * 5, y - uy * 5 + ny * 5), (x, y),
                        (x - ux * 5 - nx * 5, y - uy * 5 - ny * 5)])
                    break
                dist -= ln

    def _zigzag(self, surface, x, y, color, n=3) -> None:
        for i in range(n):
            xx = x + i * 6
            pygame.draw.lines(surface, color, False, [(xx, y + 8), (xx + 2, y + 5), (xx - 1, y + 2), (xx + 1, y - 1)])

    def _draw_cabin(self, surface, snap) -> None:
        x0, y0, w, h = self.CABIN
        surface.blit(self._cabin_base(), (x0, y0))
        pygame.draw.rect(surface, COLORS["cyan_dark"], self.CABIN, 1)
        cam = self._cam

        def P(px, pz):
            sx, sy = cam.project((px, 0.0, pz))[:2]
            return (x0 + sx, y0 + sy)

        cap = get_font(8, "tiny")
        if not snap["known"]:
            t = get_mono_font(16).render("NO CLIMATE DATA", False, COLORS["text_dim"])
            surface.blit(t, (x0 + (w - t.get_width()) // 2, y0 + 6))
            return
        fan = max(0, min(7, int(snap["fan"])))
        amber = COLORS["amber"]
        if snap["mode"] == "off" or fan == 0:
            t = get_mono_font(16).render("SYSTEM OFF", False, COLORS["text_dim"])
            surface.blit(t, (x0 + (w - t.get_width()) // 2, y0 + 6))
        else:
            # air streams: exactly the outlets of the reported direction;
            # more and faster chevrons with the fan level; cold air (A/C on)
            # bright cyan, uncooled air warm
            col = COLORS["cyan_bright"] if snap["ac"] else (214, 170, 96)
            count = 2 + (fan + 1) // 2
            phase = (time.time() * (0.8 + fan * 0.35)) % 1.0
            for name in self.VENT_STREAMS.get(snap["dir"] or "", ()):
                for line in self.STREAMS[name]:
                    self._stream(surface, [P(px, pz) for px, pz in line], col, count, phase)
            # intake: outside air entering at the cowl, or the cabin loop
            if snap["recirc"]:
                loop = [P(0.30, 0.74), P(-0.30, 0.80), P(-0.62, 1.12), P(-0.20, 1.30), P(0.50, 1.24)]
                pygame.draw.lines(surface, amber, False, loop)
                ex, ey = loop[-1]
                pygame.draw.polygon(surface, amber, [(ex + 5, ey - 1), (ex - 1, ey - 5), (ex - 1, ey + 3)])
                surface.blit(cap.render("RECIRC", False, amber), (P(-0.5, 1.30)[0], P(0, 1.52)[1]))
            else:
                a, b = P(1.62, 1.16), P(1.24, 0.98)
                pygame.draw.line(surface, COLORS["cyan_mid"], a, b)
                pygame.draw.polygon(surface, COLORS["cyan_mid"], [(b[0] - 1, b[1] + 1), (b[0] + 6, b[1] - 1), (b[0] + 2, b[1] - 6)])
                surface.blit(cap.render("FRESH", False, COLORS["cyan_mid"]), (a[0] - 8, a[1] - 9))
        # heated glass: front defrost, rear defogger (shown also with the fan off)
        if snap["front"]:
            fx, fy = P(0.74, 1.30)
            self._zigzag(surface, fx - 8, fy - 12, amber)
        if snap["rear"]:
            rx, ry = P(-1.40, 1.36)          # the rear glass, at the picture's edge
            self._zigzag(surface, rx - 6, ry - 14, amber)

    # ── drawing helpers ───────────────────────────────────────────────────

    def _key(self, surface, rect, sign, enabled=True) -> None:
        pygame.draw.rect(surface, COLORS["bg_panel"], rect)
        pygame.draw.rect(surface, COLORS["cyan_mid"] if enabled else COLORS["cyan_dark"], rect, 1)
        cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
        col = COLORS["cyan_bright"] if enabled else COLORS["cyan_dark"]
        pygame.draw.line(surface, col, (cx - 8, cy), (cx + 8, cy), 2)
        if sign == "+":
            pygame.draw.line(surface, col, (cx, cy - 8), (cx, cy + 8), 2)

    def _tile(self, surface, rect, lines, on, enabled=True, pic=None) -> None:
        """A single-tap tile: pictogram or text, an amber LED dash when on
        (the main screen's LED-key language); dim and dash-less when the
        state is unknown or the control cannot be used now."""
        x, y, w, h = rect
        pygame.draw.rect(surface, COLORS["bg_panel"], rect)
        pygame.draw.rect(surface, COLORS["cyan_mid"] if (on and enabled) else COLORS["cyan_dark"], rect, 1)
        col = (COLORS["cyan_bright"] if on else COLORS["inactive"]) if enabled else COLORS["cyan_dark"]
        font = get_mono_font(14)
        ty = y + (h - 8 - 14 * len(lines) - (pic.get_height() + 3 if pic is not None else 0)) // 2
        if pic is not None:
            surface.blit(pic, (x + (w - pic.get_width()) // 2, ty))
            ty += pic.get_height() + 3
        for ln in lines:
            t = font.render(ln, False, col)
            surface.blit(t, (x + (w - t.get_width()) // 2, ty))
            ty += 14
        if enabled:
            pygame.draw.rect(surface, COLORS["amber"] if on else COLORS["cyan_dark"],
                             (x + (w - 21) // 2, y + h - 7, 21, 2))

    def _glass_pic(self, rear: bool, color) -> pygame.Surface:
        key = (rear, tuple(color))
        pic = self._pics.get(key)
        if pic is None:
            pic = pygame.Surface((23, 15))
            pic.fill(COLORS["bg_panel"])
            if rear:
                pygame.draw.rect(pic, color, (1, 2, 21, 12), 1)
            else:
                pygame.draw.lines(pic, color, True, [(4, 2), (18, 2), (22, 13), (0, 13)])
            for i in range(3):
                xx = 6 + i * 5
                pygame.draw.lines(pic, color, False, [(xx, 11), (xx + 2, 8), (xx - 1, 6), (xx + 1, 3)])
            self._pics[key] = pic
        return pic

    def render(self, surface: pygame.Surface) -> None:
        """Cabin view: where the air goes, drawn from the reported state, with
        the controls around it (temperature and fan left, outlets below, the
        single-tap tiles right)."""
        from ..widgets.led_keys import glyph
        surface.fill(COLORS["bg_dark"])
        snap = self._snapshot()
        known = snap["known"]
        bright, dim, amber = COLORS["cyan_bright"], COLORS["text_dim"], COLORS["amber"]
        mono14, mono16 = get_mono_font(14), get_mono_font(16)
        cap = get_font(8, "tiny")

        # header: BACK, title, link state, outside temperature
        bx, by, bw, bh = self.BACK_RECT
        pygame.draw.rect(surface, COLORS["bg_panel"], self.BACK_RECT)
        pygame.draw.rect(surface, COLORS["cyan_mid"], self.BACK_RECT, 1)
        bt = mono14.render("< BACK", False, bright)
        surface.blit(bt, (bx + (bw - bt.get_width()) // 2, by + 3))
        title = get_title_font(16).render("CLIMATE", False, COLORS["cyan_mid"])
        surface.blit(title, (bx + bw + 10, 4))
        right = self.width - 6
        if snap["out"] is not None:
            t = mono14.render("OUT %d\xb0C" % int(snap["out"]), False, COLORS["text_value"])
            right -= t.get_width()
            surface.blit(t, (right, 5))
            right -= 10
        if snap["inside"] is not None:
            t = mono14.render("IN %d\xb0C" % int(snap["inside"]), False, COLORS["text_value"])
            right -= t.get_width()
            surface.blit(t, (right, 5))
            right -= 10
        if self._ctl is not None and (not snap["live"] or snap["stale"]):
            t = mono14.render("NO AVC" if not snap["live"] else "STALE", False, (0, 0, 0))
            right -= t.get_width() + 8
            pygame.draw.rect(surface, amber, (right, 3, t.get_width() + 8, 18))
            surface.blit(t, (right + 4, 5))

        # left: temperature, fan
        self._key(surface, self.TEMP_UP, "+", known)
        self._key(surface, self.TEMP_DOWN, "-", known)
        temp = snap["temp"] if known else "--"
        tt = get_mono_font(32).render(temp, False, bright if known else dim)
        surface.blit(tt, (4 + (116 - tt.get_width()) // 2 - (6 if temp[:1].isdigit() else 0), 74))
        if temp[:1].isdigit():
            surface.blit(mono14.render("\xb0C", False, COLORS["cyan_mid"]),
                         (4 + (116 + tt.get_width()) // 2 - 4, 76))
        surface.blit(cap.render("FAN", False, COLORS["cyan_mid"]), (6, 161))
        ft = mono14.render(snap["fan_text"] if known else "--", False, bright if known else dim)
        surface.blit(ft, (118 - ft.get_width(), 158))
        fan = max(0, min(7, int(snap["fan"])))
        for i in range(7):
            hh = 4 + i
            r = (6 + i * 16, 187 - hh, 13, hh)
            if i < fan:
                pygame.draw.rect(surface, bright, r)
            else:
                pygame.draw.rect(surface, COLORS["cyan_dark"], r, 1)
        self._key(surface, self.FAN_DOWN, "-", known)
        self._key(surface, self.FAN_UP, "+", known)

        # centre: the cabin, the outlets
        self._draw_cabin(surface, snap)
        for rect, mode, lines in zip(self.VENT_TILES, self.VENT_MODES, self.VENT_LABELS):
            self._tile(surface, rect, lines, known and snap["dir"] == mode, known)

        # right: single-tap tiles
        tiles = self.TILES
        mode = snap["mode"]
        self._tile(surface, tiles["AUTO"], ("AUTO",), mode == "auto", known)
        ac_on = bool(snap["ac"])
        self._tile(surface, tiles["A/C"], ("A/C",), ac_on, known,
                   glyph("snow", bright if ac_on else (COLORS["inactive"] if known else COLORS["cyan_dark"])))
        rec = bool(snap["recirc"])
        icol = (bright if rec else COLORS["cyan_mid"]) if known else COLORS["cyan_dark"]
        self._tile(surface, tiles["INTAKE"], ("RECIRC" if rec else "FRESH",), rec, known,
                   glyph("recirc" if rec else "fresh", icol))
        self._tile(surface, tiles["OFF"], ("OFF",), mode == "off", known)
        ok = snap["toggles_ok"]
        for name, on, rear in (("FRONT", snap["front"], False), ("REAR", snap["rear"], True)):
            pcol = (bright if on else COLORS["inactive"]) if ok else COLORS["cyan_dark"]
            self._tile(surface, tiles[name], (name,), bool(on), ok, self._glass_pic(rear, pcol))

        # encoder focus: the selected control (amber while editing); shown
        # once the encoder has been used, hidden again by a touch
        label = self.items[self._selected_index].label
        fcol = amber if self._editing else bright
        for r in self.FOCUS.get(label, ()) if self._focus_visible else ():
            pygame.draw.rect(surface, fcol, (r[0] - 1, r[1] - 1, r[2] + 2, r[3] + 2), 1)
            pygame.draw.rect(surface, fcol, (r[0] - 2, r[1] - 2, r[2] + 4, r[3] + 4), 1)

    # ─── Pointer / touch ──────────────────────────────────────────────

    def _touch(self, label: str, action, arg=None) -> None:
        """A key or tile was tapped: select that control and act on it."""
        self._reset_activity()
        self._editing = False
        self._focus_visible = False
        labels = [it.label for it in self.items]
        if label in labels:
            self._selected_index = labels.index(label)
        ctl = self._ctl
        if ctl is not None:
            v = ctl.view()
            if action == "temp":
                ctl.step_target_temp(arg)
            elif action == "fan":
                ctl.step_fan(arg)
            elif action == "vent":
                ctl.set_air_direction(arg)
            elif action == "auto":
                # no AUTO-off button on the car: leaving AUTO is "manual"
                ctl.set_mode("manual" if v.mode == "auto" else "auto")
            elif action == "off":
                ctl.set_mode("auto" if v.mode == "off" else "off")
            elif action == "ac":
                ctl.toggle_ac()
            elif action == "intake":
                ctl.toggle_recirculation()
            elif action == "front":
                ctl.toggle_front_defrost()
            elif action == "rear":
                ctl.toggle_rear_defogger()
            self.sync_from_model()
            return
        # no store: the static rows
        it = self._by_label
        if action in ("temp", "fan"):
            self._adjust(it[label], arg)
        elif action == "vent":
            item = it["AIR DIRECTION"]
            item._option_index = item.value = self._air_modes.index(arg)
            self._notify_value_changed(item.label, item.value)
        elif action in ("auto", "off"):
            item = it["MODE"]
            cur = item._option_index
            want = 0 if action == "auto" else 2
            item._option_index = item.value = (1 if cur == want and action == "auto" else
                                               0 if cur == want else want)
            self._notify_value_changed(item.label, item.value)
        else:
            self._adjust(it[label], 1)

    def setup_pointer(self) -> None:
        """One tap per function. Keys: temperature and fan -/+. Tiles: the four
        outlets, AUTO, A/C, intake, OFF, front defrost, rear defogger. BACK is
        the key in the header."""
        z = self.add_button_zone
        z(self.TEMP_UP, lambda x, y: self._touch("TARGET TEMP", "temp", 1), name="temp-up")
        z(self.TEMP_DOWN, lambda x, y: self._touch("TARGET TEMP", "temp", -1), name="temp-down")
        z(self.FAN_UP, lambda x, y: self._touch("FAN SPEED", "fan", 1), name="fan-up")
        z(self.FAN_DOWN, lambda x, y: self._touch("FAN SPEED", "fan", -1), name="fan-down")
        for rect, mode in zip(self.VENT_TILES, self.VENT_MODES):
            z(rect, lambda x, y, m=mode: self._touch("AIR DIRECTION", "vent", m), name="vent-" + mode)
        t = self.TILES
        z(t["AUTO"], lambda x, y: self._touch("MODE", "auto"), name="auto")
        z(t["OFF"], lambda x, y: self._touch("MODE", "off"), name="off")
        z(t["A/C"], lambda x, y: self._touch("A/C", "ac"), name="ac")
        z(t["INTAKE"], lambda x, y: self._touch("AIR INTAKE", "intake"), name="intake")
        z(t["FRONT"], lambda x, y: self._touch("FRONT DEFROST", "front"), name="front")
        z(t["REAR"], lambda x, y: self._touch("REAR DEFOGGER", "rear"), name="rear")
        bx, by, bw, bh = self.BACK_RECT
        z((0, 0, bx + bw + 8, by + bh + 4), lambda x, y: self.pointer_back(), name="back")
