"""
AVC-LAN bus monitor / sniffer screen.

Live view of AVC-LAN messages for pattern analysis and reverse engineering.
Messages are grouped by master→slave address pair, showing data bytes,
message age, and hit count. Recently changed values flash to help
correlate bus activity with real-world events (doors, buttons, lights).

Modes:
  LIVE   – all AVC traffic, grouped, sorted by recency
  FREEZE – stop updating, scroll through captured data

Layout (480x240):
┌──────────────────────────────────────────────────────────┐
│  AVC-LAN MONITOR              LIVE  ▌243 msg  12 pairs  │
├──────────────────────────────────────────────────────────┤
│  ADDR         CNT   AGE   DATA                          │
│  ─────────────────────────────────────────────────────   │
│  040→200       51   0.3s  28 00 C1 08 E2                │
│  10C→310       21   1.2s  00 00 00 00 08 0A 90 80       │
│  110→490       18   2.1s  00 00 00 08 A4 04 03 00       │
│  400→020       12   0.1s  21                             │
│  002→660        8   5.4s  10 50 C4 52 82 01 ...         │
│  ...                                                     │
├──────────────────────────────────────────────────────────┤
│  [PRESS] FREEZE/LIVE   [HOLD] BACK   [TURN] SCROLL     │
└──────────────────────────────────────────────────────────┘

Highlighting:
  - Bright cyan:  message received in last 0.5s (JUST NOW)
  - Dim cyan:     received in last 3s
  - Grey:         older than 3s
  - Yellow flash: data bytes that CHANGED from previous message
"""

import pygame
import time
from typing import Tuple, Optional, Dict, List
from dataclasses import dataclass, field as dc_field
from collections import OrderedDict

from .base import Screen
from ..colors import COLORS, dim_color
from ..fonts import get_title_font, get_mono_font, get_font
from ...input.manager import InputEvent as IE
from ...io.ports import RawMessage, MessageCategory
from ...comm.avc_decoder import AVCDecoder, PRIUS_GEN2_ADDRESSES


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AVCMessageRecord:
    """Tracks a unique master→slave message pattern."""
    master_addr: int
    slave_addr: int
    last_data: List[str]         # Last received data bytes (hex strings)
    prev_data: Optional[List[str]] = None  # Previous data (for diff)
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    changed_indices: List[int] = dc_field(default_factory=list)  # Byte positions that changed  # type: ignore[assignment]
    change_time: float = 0.0     # When the last data change happened


class AVCMonitorScreen(Screen):
    """
    Live AVC-LAN bus monitor for pattern analysis.

    Captures raw AVC messages, groups by address pair,
    highlights changes to help reverse-engineer meaning.
    """

    HEADER_H = 22
    TABLE_HEADER_H = 14
    ROW_H = 16
    FOOTER_H = 16
    ADDR_COL_W = 76
    CNT_COL_W = 36
    AGE_COL_W = 40
    DATA_COL_X = 156  # Start of data column

    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        store=None,
    ):
        super().__init__(size, app)
        self._store = store
        self._last_activity = time.time()

        # Message tracking: key = (master, slave) → record
        self._records: OrderedDict[Tuple[int, int], AVCMessageRecord] = OrderedDict()
        self._total_count: int = 0

        # UI state
        self._frozen: bool = False
        self._scroll_offset: int = 0
        self._dirty: bool = True

        # Decoder for device name lookup
        self._avc_decoder = AVCDecoder()

        # Callback reference (for removal on exit)
        self._callback_ref = None

        # Filter mode
        self._filter_addr: Optional[int] = None  # None = show all

    @property
    def _visible_rows(self) -> int:
        """Number of visible data rows."""
        body_h = self.height - self.HEADER_H - self.TABLE_HEADER_H - self.FOOTER_H
        return max(1, body_h // self.ROW_H)

    # ─────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────

    def on_enter(self) -> None:
        self._last_activity = time.time()
        # Register for raw AVC messages via ingress callback
        if self.app and hasattr(self.app, '_virtual_twin') and self.app._virtual_twin:
            ingress = self.app._virtual_twin.ingress
            self._callback_ref = self._on_raw_message
            ingress.add_message_log_callback(self._callback_ref)

    def on_exit(self) -> None:
        # Remove callback
        if self._callback_ref and self.app and hasattr(self.app, '_virtual_twin') and self.app._virtual_twin:
            cbs = self.app._virtual_twin.ingress._message_log_callbacks
            if self._callback_ref in cbs:
                cbs.remove(self._callback_ref)
            self._callback_ref = None

    # ─────────────────────────────────────────────────────────────────────
    # Message capture
    # ─────────────────────────────────────────────────────────────────────

    def _on_raw_message(self, msg: RawMessage, direction: str) -> None:
        """Callback from IngressController for every raw message."""
        if msg.category != MessageCategory.AVC_LAN:
            return
        if self._frozen:
            return

        data = msg.data
        if not data or "m" not in data or "s" not in data:
            return

        try:
            master = int(data["m"], 16)
            slave = int(data["s"], 16)
            raw_bytes = data.get("d", [])
        except (ValueError, TypeError):
            return

        now = time.time()
        key = (master, slave)
        self._total_count += 1

        if key in self._records:
            rec = self._records[key]
            rec.count += 1
            rec.prev_data = rec.last_data
            rec.last_data = list(raw_bytes)
            rec.last_seen = now

            # Detect changed byte positions
            if rec.prev_data and rec.prev_data != rec.last_data:
                changed = []
                max_len = max(len(rec.prev_data), len(rec.last_data))
                for i in range(max_len):
                    old = rec.prev_data[i] if i < len(rec.prev_data) else None
                    new = rec.last_data[i] if i < len(rec.last_data) else None
                    if old != new:
                        changed.append(i)
                rec.changed_indices = changed
                rec.change_time = now

            # Move to end (most recent)
            self._records.move_to_end(key)
        else:
            self._records[key] = AVCMessageRecord(
                master_addr=master,
                slave_addr=slave,
                last_data=list(raw_bytes),
                count=1,
                first_seen=now,
                last_seen=now
            )

        self._dirty = True

    # ─────────────────────────────────────────────────────────────────────
    # Input
    # ─────────────────────────────────────────────────────────────────────

    def handle_input(self, event) -> bool:
        self._last_activity = time.time()

        if event == IE.PRESS_STRONG or event == IE.BACK:
            self._exit_screen()
            return True

        if event == IE.PRESS_LIGHT:
            self._frozen = not self._frozen
            self._dirty = True
            return True

        if event == IE.ROTATE_RIGHT:
            filtered = self._get_filtered_records()
            max_scroll = max(0, len(filtered) - self._visible_rows)
            self._scroll_offset = min(self._scroll_offset + 1, max_scroll)
            self._dirty = True
            return True

        if event == IE.ROTATE_LEFT:
            self._scroll_offset = max(0, self._scroll_offset - 1)
            self._dirty = True
            return True

        return False

    def _exit_screen(self) -> None:
        if self.app:
            self.app.pop_screen()

    # ─────────────────────────────────────────────────────────────────────
    # Update
    # ─────────────────────────────────────────────────────────────────────

    def update(self, dt: float) -> None:
        super().update(dt)
        # No auto-timeout — diagnostic screen stays until strong-press exit

    # ─────────────────────────────────────────────────────────────────────
    # Filtering
    # ─────────────────────────────────────────────────────────────────────

    def _get_filtered_records(self) -> List[AVCMessageRecord]:
        """Return records in display order (most recent first)."""
        records = list(reversed(self._records.values()))
        if self._filter_addr is not None:
            records = [
                r for r in records
                if r.master_addr == self._filter_addr or r.slave_addr == self._filter_addr
            ]
        return records

    # ─────────────────────────────────────────────────────────────────────
    # Rendering
    # ─────────────────────────────────────────────────────────────────────

    def render(self, surface: pygame.Surface) -> None:
        now = time.time()
        surface.fill(COLORS["bg_dark"])

        self._render_header(surface, now)
        self._render_table_header(surface)
        self._render_rows(surface, now)
        self._render_footer(surface)
        self._render_scrollbar(surface)

    def _render_header(self, surface: pygame.Surface, now: float) -> None:
        """Draw title bar."""
        font = get_title_font(12)
        y = 2

        # Title
        title = "AVC-LAN MONITOR"
        title_surf = font.render(title, True, COLORS["cyan_bright"])
        surface.blit(title_surf, (6, y))

        # Status
        small = get_mono_font(10)

        # Mode indicator
        if self._frozen:
            mode_text = "FROZEN"
            mode_color = COLORS["warning"]
        else:
            mode_text = "LIVE"
            mode_color = COLORS["green_bright"]
        mode_surf = small.render(mode_text, True, mode_color)
        surface.blit(mode_surf, (160, y + 2))

        # Stats
        n_pairs = len(self._records)
        stats = f"{self._total_count} msg  {n_pairs} pairs"
        stats_surf = small.render(stats, True, COLORS["text_secondary"])
        surface.blit(stats_surf, (self.width - stats_surf.get_width() - 6, y + 2))

        # Divider line
        pygame.draw.line(
            surface, COLORS["border_dim"],
            (0, self.HEADER_H - 1), (self.width, self.HEADER_H - 1)
        )

    def _render_table_header(self, surface: pygame.Surface) -> None:
        """Draw column headers."""
        font = get_mono_font(8)
        y = self.HEADER_H + 1
        color = COLORS["text_dim"]

        headers = [
            (4, "ADDR"),
            (self.ADDR_COL_W + 4, "CNT"),
            (self.ADDR_COL_W + self.CNT_COL_W + 4, "AGE"),
            (self.DATA_COL_X, "DATA"),
        ]
        for x, text in headers:
            surf = font.render(text, True, color)
            surface.blit(surf, (x, y))

        # Separator
        sep_y = self.HEADER_H + self.TABLE_HEADER_H - 1
        pygame.draw.line(surface, COLORS["border_dim"], (0, sep_y), (self.width, sep_y))

    def _render_rows(self, surface: pygame.Surface, now: float) -> None:
        """Draw message rows."""
        records = self._get_filtered_records()
        start = self._scroll_offset
        end = start + self._visible_rows
        visible = records[start:end]

        font = get_mono_font(10)
        small_font = get_mono_font(8)
        y_base = self.HEADER_H + self.TABLE_HEADER_H + 1

        for i, rec in enumerate(visible):
            y = y_base + i * self.ROW_H
            self._render_row(surface, rec, y, now, font, small_font)

    def _render_row(
        self,
        surface: pygame.Surface,
        rec: AVCMessageRecord,
        y: int,
        now: float,
        font: pygame.font.Font,
        small_font: pygame.font.Font,
    ) -> None:
        """Render a single message row with age-based coloring and change highlights."""
        age = now - rec.last_seen
        change_age = now - rec.change_time if rec.change_time else 999

        # Row background for very fresh messages
        if age < 0.5:
            bg_alpha = max(0, int(40 * (1.0 - age / 0.5)))
            bg_rect = pygame.Rect(0, y, self.width, self.ROW_H)
            bg_surf = pygame.Surface((self.width, self.ROW_H), pygame.SRCALPHA)
            bg_surf.fill((*COLORS["cyan_dark"][:3], bg_alpha))
            surface.blit(bg_surf, bg_rect)

        # Address color based on age
        if age < 0.5:
            addr_color = COLORS["cyan_bright"]
        elif age < 3.0:
            addr_color = COLORS["cyan"]
        elif age < 10.0:
            addr_color = COLORS["text_secondary"]
        else:
            addr_color = COLORS["text_dim"]

        # Address pair (e.g. "040→200")
        addr_text = f"{rec.master_addr:03X}\u2192{rec.slave_addr:03X}"
        addr_surf = font.render(addr_text, True, addr_color)
        surface.blit(addr_surf, (4, y + 1))

        # Count
        cnt_text = f"{rec.count:>4}"
        cnt_color = COLORS["text_secondary"] if rec.count < 100 else COLORS["text_primary"]
        cnt_surf = small_font.render(cnt_text, True, cnt_color)
        surface.blit(cnt_surf, (self.ADDR_COL_W + 4, y + 3))

        # Age
        if age < 60:
            age_text = f"{age:4.1f}s"
        elif age < 3600:
            age_text = f"{age / 60:4.1f}m"
        else:
            age_text = "old"
        age_surf = small_font.render(age_text, True, addr_color)
        surface.blit(age_surf, (self.ADDR_COL_W + self.CNT_COL_W + 4, y + 3))

        # Data bytes with per-byte change highlighting
        x = self.DATA_COL_X
        max_data_bytes = 18  # Truncate very long messages
        data = rec.last_data[:max_data_bytes]

        for bi, byte_hex in enumerate(data):
            # Check if this byte changed recently
            byte_changed = (bi in rec.changed_indices and change_age < 3.0)

            if byte_changed:
                # Flash yellow for changed bytes, fade over 3 seconds
                flash = max(0.0, 1.0 - change_age / 3.0)
                r = int(COLORS["yellow"][0] * flash + COLORS["text_secondary"][0] * (1 - flash))
                g = int(COLORS["yellow"][1] * flash + COLORS["text_secondary"][1] * (1 - flash))
                b = int(COLORS["yellow"][2] * flash + COLORS["text_secondary"][2] * (1 - flash))
                byte_color = (r, g, b)
            else:
                byte_color = addr_color

            byte_surf = small_font.render(byte_hex, True, byte_color)
            surface.blit(byte_surf, (x, y + 3))
            x += byte_surf.get_width() + 3

            if x > self.width - 20:
                # Overflow indicator
                dot_surf = small_font.render("\u2026", True, COLORS["text_dim"])
                surface.blit(dot_surf, (x, y + 3))
                break

    def _render_footer(self, surface: pygame.Surface) -> None:
        """Draw footer with controls."""
        y = self.height - self.FOOTER_H
        pygame.draw.line(surface, COLORS["border_dim"], (0, y), (self.width, y))

        font = get_mono_font(8)

        mode_label = "LIVE" if self._frozen else "FREEZE"
        hint = f"[PRESS] {mode_label}   [HOLD] BACK   [TURN] SCROLL"
        hint_surf = font.render(hint, True, COLORS["text_dim"])
        hint_x = (self.width - hint_surf.get_width()) // 2
        surface.blit(hint_surf, (hint_x, y + 4))

    def _render_scrollbar(self, surface: pygame.Surface) -> None:
        """Draw scrollbar if content overflows."""
        records = self._get_filtered_records()
        total = len(records)
        visible = self._visible_rows
        if total <= visible:
            return

        track_y = self.HEADER_H + self.TABLE_HEADER_H
        track_h = self.height - track_y - self.FOOTER_H
        bar_h = max(8, int(track_h * visible / total))
        bar_y = track_y + int((track_h - bar_h) * self._scroll_offset / max(1, total - visible))

        # Track
        pygame.draw.rect(
            surface, COLORS["bg_panel"],
            (self.width - 4, track_y, 4, track_h)
        )
        # Thumb
        pygame.draw.rect(
            surface, COLORS["cyan_dim"],
            (self.width - 3, bar_y, 2, bar_h)
        )
