"""
Lights screen.

Left: the car model lit from the real lamp state, and what the camera
recommends (both read-only). Right: the lights settings as tiles. The
settings are stored values only (persistence.LightsSettings); nothing in the
car is switched by them yet, and the screen says so in each control panel.
"""

import pygame
import time
from typing import Tuple, List, Any, Callable

from .base import Screen
from ..widgets.base import Rect
from ..widgets.controls import ValueDisplay
from ..colors import COLORS
from ..fonts import get_font, get_mono_font
from ...persistence import get_settings, save_settings


class LightsScreen(Screen):
    """
    Lights settings screen.
    
    Settings:
    - Mode: AUTO / MANUAL / OFF
    - DRL: ON / OFF
    - BiLED Mode: OFF / ON / PWM
    - BiLED Brightness: 0-100 (only when PWM mode)
    """
    
    # Modes
    MODES = ["AUTO", "MANUAL", "OFF"]
    BILED_MODES = ["OFF", "ON", "PWM"]
    
    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        mode: str = "AUTO",
        drl_enabled: bool = True,
        biled_mode: str = "OFF",
        biled_brightness: int = 100
    ):
        """Initialize the lights screen."""
        super().__init__(size, app)
        
        # Settings
        self._mode = mode
        self._drl_enabled = drl_enabled
        self._biled_mode = biled_mode
        self._biled_brightness = biled_brightness
        
        # Menu state
        self._menu_items: List[str] = []
        self._selected_index = 0
        self._editing = False
        self._last_activity_time = time.time()
        
        # Build menu
        self._build_menu()

        # Cached car picture (re-rendered when the lamp state changes)
        self._car_key = None
        self._car_surf = None
    
    def _build_menu(self) -> None:
        """Build the menu items list."""
        self._menu_items = [
            "mode",
            "drl",
            "biled_mode",
        ]
        # Only show brightness when in PWM mode
        if self._biled_mode == "PWM":
            self._menu_items.append("biled_brightness")
    
    def _get_item_label(self, item: str) -> str:
        """Get display label for a menu item."""
        labels = {
            "mode": "Mode",
            "drl": "DRL",
            "biled_mode": "BiLED Mode",
            "biled_brightness": "BiLED Brightness",
        }
        return labels.get(item, item)
    
    def _get_item_value(self, item: str) -> str:
        """Get current value string for a menu item."""
        if item == "mode":
            return self._mode
        elif item == "drl":
            return "ON" if self._drl_enabled else "OFF"
        elif item == "biled_mode":
            return self._biled_mode
        elif item == "biled_brightness":
            return f"{self._biled_brightness}%"
        return ""
    
    def _adjust_value(self, item: str, delta: int) -> None:
        """Adjust a menu item value by delta."""
        if item == "mode":
            idx = self.MODES.index(self._mode)
            idx = (idx + delta) % len(self.MODES)
            self._mode = self.MODES[idx]
        elif item == "drl":
            self._drl_enabled = not self._drl_enabled
        elif item == "biled_mode":
            idx = self.BILED_MODES.index(self._biled_mode)
            idx = (idx + delta) % len(self.BILED_MODES)
            self._biled_mode = self.BILED_MODES[idx]
            # Rebuild menu when biled mode changes
            self._build_menu()
            # Clamp selected index if needed
            if self._selected_index >= len(self._menu_items):
                self._selected_index = len(self._menu_items) - 1
        elif item == "biled_brightness":
            self._biled_brightness = max(0, min(100, self._biled_brightness + delta * 5))
        
        # Save settings
        self._save_settings()
    
    def _save_settings(self) -> None:
        """Save current settings to persistence."""
        settings = get_settings()
        settings.lights.mode = self._mode
        settings.lights.drl_enabled = self._drl_enabled
        settings.lights.biled_mode = self._biled_mode
        settings.lights.biled_brightness = self._biled_brightness
        save_settings()
    
    def update(self, dt: float) -> None:
        """Update screen with inactivity timeout."""
        super().update(dt)
        
        # Get timeout from config
        screen_timeout = 30.0
        editing_timeout = 60.0
        if self.app and hasattr(self.app, 'config'):
            screen_timeout = self.app.config.timeout_screen_exit
            editing_timeout = self.app.config.timeout_editing_exit
        
        timeout = editing_timeout if self._editing else screen_timeout
        
        if time.time() - self._last_activity_time > timeout:
            self._exit_screen()
    
    def _exit_screen(self) -> None:
        """Exit back to main screen."""
        if self.app:
            self.app.pop_screen()
    
    # ─── Layout (native pixels) ───────────────────────────────────────

    BACK_RECT = (4, 2, 62, 21)
    PIC_RECT = (4, 26, 232, 168)            # the car, lit from the real lamp state
    CAM_RECT = (4, 198, 232, 40)            # "camera says"
    MODE_PANEL = (240, 26, 236, 64)
    BILED_PANEL = (240, 94, 236, 100)
    DRL_RECT = (240, 198, 236, 40)
    MODE_TILES = tuple((244 + i * 77, 42, 74, 44) for i in range(3))
    BILED_TILES = tuple((244 + i * 77, 110, 74, 40) for i in range(3))
    BRI_MINUS = (244, 154, 44, 36)
    BRI_PLUS = (428, 154, 44, 36)

    # ─── State the picture shows (read-only) ──────────────────────────

    def _lamps(self):
        """(tail, head, high, fog, brake) as the car reports them, each
        True / False / None (unknown): the same inputs as the main scene."""
        store = getattr(self.app, "_store", None) if self.app else None
        state = getattr(store, "state", None)
        if state is None:
            return (None,) * 5, None
        from ..scene.inputs import read_inputs
        inp = read_inputs(state)
        return (inp.tail, inp.head, inp.high, inp.fog, inp.brake), getattr(state, "vision", None)

    def _car_picture(self, lamps) -> pygame.Surface:
        """The NHW20 model with its lamp groups lit; cached per lamp state."""
        key = tuple(bool(v) for v in lamps)
        if self._car_key == key and self._car_surf is not None:
            return self._car_surf
        import math
        from ..scene.car import CarRenderer, car_palette
        from ..scene.model import Camera
        from ..scene.render import make_glow
        tail, head, high, fog, brake = key
        w, h = self.PIC_RECT[2] - 2, self.PIC_RECT[3] - 36
        surf = pygame.Surface((w, h))
        surf.fill((0, 0, 0))
        az, el, dist = math.radians(38.0), math.radians(16.0), 11.0
        target = (0.0, 0.0, 0.45)
        pos = (target[0] + dist * math.cos(el) * math.cos(az),
               target[1] + dist * math.cos(el) * math.sin(az), target[2] + dist * math.sin(el))
        cam = Camera(pos, target, w * 2.0, w / 2.0 + 4, h / 2.0 + 8)
        grid = (40, 24, 70)
        for g in range(-4, 5):
            pygame.draw.line(surf, grid, cam.project((g, -3.0, 0.0))[:2], cam.project((g, 3.0, 0.0))[:2])
        for g in range(-3, 4):
            pygame.draw.line(surf, grid, cam.project((-4.0, g, 0.0))[:2], cam.project((4.0, g, 0.0))[:2])
        car = CarRenderer(car_palette(COLORS))
        car.draw(surf, cam, lamps={"head": head or high, "tail": tail or head or high, "brake": brake})
        pts = car.lamp_points(cam)

        def halo(name, radius, color):
            if name in pts:
                surf.blit(make_glow(radius, color), (pts[name][0] - radius, pts[name][1] - radius),
                          special_flags=pygame.BLEND_RGB_ADD)

        for n in ("head_l", "head_r"):
            if head or high:
                halo(n, 10 if high else 7, COLORS["amber"])
            elif tail:
                halo(n, 4, (150, 130, 70))
        if tail or head or high or brake:
            for n in ("tail_l", "tail_r"):
                halo(n, 8 if brake else 6, (255, 48, 40) if brake else (200, 24, 30))
        self._car_key, self._car_surf = key, surf
        return surf

    # ─── Drawing ──────────────────────────────────────────────────────

    def _panel(self, surface, rect, selected=False, editing=False) -> None:
        pygame.draw.rect(surface, COLORS["bg_panel"], rect)
        col = COLORS["amber"] if editing else COLORS["cyan_mid"] if selected else COLORS["cyan_dark"]
        pygame.draw.rect(surface, col, rect, 1)
        x, y, w, h = rect
        tick = COLORS["cyan_mid"]
        for cx, cy, dx, dy in ((x, y, 1, 1), (x + w - 1, y, -1, 1), (x, y + h - 1, 1, -1),
                               (x + w - 1, y + h - 1, -1, -1)):
            pygame.draw.line(surface, tick, (cx, cy), (cx + dx * 5, cy))
            pygame.draw.line(surface, tick, (cx, cy), (cx, cy + dy * 5))

    def _tile(self, surface, rect, label, on, accent, enabled=True) -> None:
        font = get_mono_font(16)
        x, y, w, h = rect
        if on:
            pygame.draw.rect(surface, (58, 50, 22) if accent == COLORS["amber"] else (0, 46, 44), rect)
            pygame.draw.rect(surface, accent, rect, 1)
            pygame.draw.rect(surface, accent, (x + 4, y + h - 4, w - 8, 2))
            col = accent
        else:
            pygame.draw.rect(surface, (0, 0, 0), rect)
            pygame.draw.rect(surface, COLORS["cyan_dark"], rect, 1)
            col = COLORS["inactive"] if enabled else COLORS["cyan_dark"]
        t = font.render(label, False, col)
        surface.blit(t, (x + (w - t.get_width()) // 2, y + (h - t.get_height()) // 2 - 1))

    def _caption(self, surface, text, x, y, color=None, right=None) -> None:
        t = get_font(8, "tiny").render(text, False, color or COLORS["cyan_mid"])
        surface.blit(t, ((right - t.get_width()) if right is not None else x, y))

    def render(self, surface: pygame.Surface) -> None:
        """Lamp state on the car model, what the camera recommends, and the
        lights settings as tiles.

        The picture and the camera panel are read-only facts from the car.
        The tiles are the stored settings (mode, BiLED, DRL): nothing in the
        car is switched by them yet, which each control panel says in its
        corner ("STORED ONLY")."""
        surface.fill(COLORS["bg_dark"])
        sel = self._menu_items[self._selected_index] if self._menu_items else ""
        amber, bright = COLORS["amber"], COLORS["cyan_bright"]
        dim = COLORS["text_dim"]

        # header: BACK key and title
        bx, by, bw, bh = self.BACK_RECT
        pygame.draw.rect(surface, COLORS["bg_panel"], self.BACK_RECT)
        pygame.draw.rect(surface, COLORS["cyan_mid"], self.BACK_RECT, 1)
        bt = get_mono_font(14).render("< BACK", False, bright)
        surface.blit(bt, (bx + (bw - bt.get_width()) // 2, by + 3))
        title = get_font(16, "title").render("LIGHTS", False, COLORS["cyan_mid"])
        surface.blit(title, (bx + bw + 10, 4))

        # left: the car as the status picture
        lamps, vision = self._lamps()
        tail, head, high, fog, brake = lamps
        px, py, pw, ph = self.PIC_RECT
        self._panel(surface, self.PIC_RECT)
        pygame.draw.rect(surface, (0, 0, 0), (px + 1, py + 1, pw - 2, ph - 2))
        self._caption(surface, "LAMPS NOW", px + 6, py + 5)
        known = any(v is not None for v in lamps)
        if not known:
            self._caption(surface, "NO DATA", 0, py + 5, dim, right=px + pw - 6)
        surface.blit(self._car_picture(lamps), (px + 1, py + 13))
        x = px + 6
        chip_font = get_mono_font(14)
        for label, on, col in (("POS", tail, COLORS["green_bright"]), ("LOW", head, amber),
                               ("HIGH", high, (66, 165, 245)), ("FOG", fog, COLORS["green_bright"])):
            r = (x, py + ph - 21, 52, 17)
            if on:
                pygame.draw.rect(surface, col, r)
            else:
                pygame.draw.rect(surface, COLORS["cyan_dark"], r, 1)
            t = chip_font.render(label, False, (0, 0, 0) if on else (dim if on is not None else COLORS["cyan_dark"]))
            surface.blit(t, (x + (52 - t.get_width()) // 2, r[1] + 2))
            x += 56

        # camera recommendation (AppState.vision), read-only
        cx, cy, cw, ch = self.CAM_RECT
        self._panel(surface, self.CAM_RECT)
        self._caption(surface, "CAMERA SAYS", cx + 6, cy + 5)
        rec = getattr(vision, "lights_recommended", None) if getattr(vision, "enabled", False) else None
        if rec is None:
            surface.blit(get_mono_font(16).render("--", False, dim), (cx + 6, cy + 17))
        else:
            surface.blit(get_mono_font(16).render("LIGHTS ON" if rec else "LIGHTS OFF", False,
                                                  amber if rec else bright), (cx + 6, cy + 17))
            reason = str(getattr(vision, "reason", "") or "").upper()
            if reason:
                self._caption(surface, reason[:18], 0, cy + 5, dim, right=cx + cw - 6)
            conf = getattr(vision, "confidence", None)
            if conf is not None:
                t = get_mono_font(14).render("%d%%" % round(conf * 100), False, dim)
                surface.blit(t, (cx + cw - 6 - t.get_width(), cy + 20))

        # right: the stored settings
        mx, my, mw, mh = self.MODE_PANEL
        self._panel(surface, self.MODE_PANEL, sel == "mode", sel == "mode" and self._editing)
        self._caption(surface, "MODE", mx + 6, my + 5)
        self._caption(surface, "STORED ONLY", 0, my + 5, COLORS["cyan_dim"], right=mx + mw - 6)
        for rect, name in zip(self.MODE_TILES, self.MODES):
            self._tile(surface, rect, name, name == self._mode, amber)

        bsel = sel in ("biled_mode", "biled_brightness")
        self._panel(surface, self.BILED_PANEL, bsel, bsel and self._editing)
        ox, oy, ow, oh = self.BILED_PANEL
        self._caption(surface, "BI-LED", ox + 6, oy + 5)
        self._caption(surface, "STORED ONLY", 0, oy + 5, COLORS["cyan_dim"], right=ox + ow - 6)
        for rect, name in zip(self.BILED_TILES, self.BILED_MODES):
            self._tile(surface, rect, name, name == self._biled_mode, bright)
        pwm = self._biled_mode == "PWM"
        for rect, sign in ((self.BRI_MINUS, "-"), (self.BRI_PLUS, "+")):
            pygame.draw.rect(surface, (0, 0, 0), rect)
            pygame.draw.rect(surface, COLORS["cyan_mid"] if pwm else COLORS["cyan_dark"], rect, 1)
            kx, ky = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
            col = bright if pwm else COLORS["cyan_dark"]
            pygame.draw.line(surface, col, (kx - 7, ky), (kx + 7, ky), 2)
            if sign == "+":
                pygame.draw.line(surface, col, (kx, ky - 7), (kx, ky + 7), 2)
        sx = self.BRI_MINUS[0] + self.BRI_MINUS[2] + 8
        lit = self._biled_brightness // 10 if pwm else 0
        for i in range(10):
            r = (sx + i * 12, 157, 10, 14)
            if i < lit:
                pygame.draw.rect(surface, bright, r)
            else:
                pygame.draw.rect(surface, COLORS["cyan_dark"], r, 1)
        t = get_mono_font(14).render("%d%%" % self._biled_brightness if pwm else "PWM ONLY", False,
                                     bright if pwm else COLORS["cyan_dim"])
        surface.blit(t, (sx + 60 - t.get_width() // 2, 174))

        dsel = sel == "drl"
        self._tile(surface, self.DRL_RECT, "DRL  " + ("ON" if self._drl_enabled else "OFF"),
                   self._drl_enabled, bright)
        if dsel:
            pygame.draw.rect(surface, amber if self._editing else COLORS["cyan_mid"], self.DRL_RECT, 1)
        self._caption(surface, "STORED ONLY", 0, self.DRL_RECT[1] + 4, COLORS["cyan_dim"],
                      right=self.DRL_RECT[0] + self.DRL_RECT[2] - 6)

    def handle_input(self, event) -> bool:
        """Handle input events."""
        from ...input.manager import InputEvent as IE
        
        self._last_activity_time = time.time()

        # BACK always exits
        if event == IE.BACK:
            self._editing = False
            self._exit_screen()
            return True
        
        if self._editing:
            # Editing mode
            if event == IE.ROTATE_LEFT:
                self._adjust_value(self._menu_items[self._selected_index], -1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._adjust_value(self._menu_items[self._selected_index], 1)
                return True
            elif event == IE.PRESS_LIGHT or event == IE.PRESS_STRONG:
                self._editing = False
                return True
        else:
            # Navigation mode
            if event == IE.ROTATE_LEFT:
                self._selected_index = max(0, self._selected_index - 1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._selected_index = min(len(self._menu_items) - 1, self._selected_index + 1)
                return True
            elif event == IE.PRESS_LIGHT:
                self._editing = True
                return True
            elif event == IE.PRESS_STRONG:
                self._exit_screen()
                return True
        
        return False

    # ─── Pointer / touch ──────────────────────────────────────────────

    def _set(self, item: str, value) -> None:
        """A tile was tapped: store that value (same persistence as the
        encoder path; nothing is sent to the car)."""
        self._last_activity_time = time.time()
        if item in self._menu_items:
            self._selected_index = self._menu_items.index(item)
        self._editing = False
        if item == "mode":
            self._mode = value
        elif item == "drl":
            self._drl_enabled = not self._drl_enabled
        elif item == "biled_mode":
            self._biled_mode = value
            self._build_menu()
            self._selected_index = self._menu_items.index("biled_mode")
        elif item == "biled_brightness":
            if self._biled_mode != "PWM":
                return
            self._biled_brightness = max(0, min(100, self._biled_brightness + value))
        self._save_settings()

    def setup_pointer(self) -> None:
        """One tap per setting: every tile is a target (74 x 40 or larger,
        the brightness keys 44 x 36). BACK is the key in the header."""
        for rect, name in zip(self.MODE_TILES, self.MODES):
            self.add_button_zone(rect, lambda x, y, n=name: self._set("mode", n), name="mode-" + name)
        for rect, name in zip(self.BILED_TILES, self.BILED_MODES):
            self.add_button_zone(rect, lambda x, y, n=name: self._set("biled_mode", n),
                                 name="biled-" + name)
        self.add_button_zone(self.BRI_MINUS, lambda x, y: self._set("biled_brightness", -10),
                             name="bri-minus")
        self.add_button_zone(self.BRI_PLUS, lambda x, y: self._set("biled_brightness", 10),
                             name="bri-plus")
        self.add_button_zone(self.DRL_RECT, lambda x, y: self._set("drl", None), name="drl")
        bx, by, bw, bh = self.BACK_RECT
        self.add_button_zone((0, 0, bx + bw + 8, by + bh + 4), lambda x, y: self.pointer_back(),
                             name="back")
