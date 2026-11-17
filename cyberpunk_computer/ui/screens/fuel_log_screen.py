"""
Fuelling log screen (docs/FUEL_LOG.md §5), opened from ENGINE / DIAGNOSTICS.

Two modes:

* summary: per fuel the km since the last fill, the last and rolling
  full-to-full l/100 km and the last fill; underneath the combined
  petrol-equivalent figure and the cost per 100 km; two keys ADD FILL and
  UNDO LAST (undo asks for a second OK).
* add: a list in the settings-screen style (audio screen): FUEL, ENTER AS
  (amount paid or litres), TOTAL PLN / LITRES, PRICE, FULL, SAVE, CANCEL.
  OK on a number edits it digit by digit: the turn changes the current
  digit (coarse first), OK moves to the next finer digit, after the last
  digit the selection moves on to the next row. Defaults: last price, last
  amount and last litres of the chosen fuel, FULL = yes.

All figures come from ``state.fuel_log`` (backend FuelLogService); SAVE and
UNDO dispatch FuelLogCommandAction, which the MFD's RemoteStore sends as the
``fuel_log_add`` / ``fuel_log_undo`` commands.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

import pygame

from .base import Screen
from ..colors import COLORS
from ..fonts import get_mono_font, get_tiny_font, get_title_font
from ...input.manager import InputEvent as IE

FUEL_LABEL = {"lpg": "LPG", "petrol": "PETROL"}

#: Digit steps per numeric field, coarse to fine.
STEPS = {
    "total": (100.0, 10.0, 1.0, 0.1, 0.01),
    "litres": (10.0, 1.0, 0.1),
    "price": (1.0, 0.1, 0.01),
}
LIMITS = {"total": (0.0, 999.99), "litres": (0.0, 200.0), "price": (0.0, 19.99)}


def _fmt(v: Optional[float], nd: int = 1, dash: str = "--") -> str:
    return dash if v is None else ("%.*f" % (nd, v))


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict) and not hasattr(d, "get"):
            return None
        d = d.get(p) if d is not None else None
    return d


class FuelLogScreen(Screen):
    HEADER_HEIGHT = 26
    FOOTER_HEIGHT = 22
    ROW_H = 20
    ROW_PAD = 3
    SIDE = 16

    ROWS = ("fuel", "mode", "amount", "price", "full", "save", "cancel")

    def __init__(self, size: Tuple[int, int], app=None, store=None):
        super().__init__(size, app)
        self._store = store
        self._last_activity = time.time()
        self.mode = "summary"            # summary | add
        self._action_index = 0           # summary: 0 = ADD FILL, 1 = UNDO LAST
        self._undo_armed = False
        self._selected_index = 0         # add: row
        self._editing = False
        self._step_i = 0
        self._sent_at: Optional[float] = None
        self._sent_op: Optional[str] = None
        self.form: Dict[str, Any] = {}

    # ── data ───────────────────────────────────────────────────────────

    @property
    def fl(self):
        st = getattr(self._store, "state", None)
        return getattr(st, "fuel_log", None)

    def _odometer(self) -> Optional[float]:
        st = getattr(self._store, "state", None)
        return getattr(getattr(st, "vehicle", None), "odometer_km", None)

    def _defaults(self, fuel: str) -> Dict[str, Any]:
        fl = self.fl
        d = dict(_get(fl.defaults if fl else None, fuel) or {})
        return d

    def _reset_form(self, fuel: Optional[str] = None) -> None:
        if fuel is None:
            # Start with the fuel filled most recently (LPG on an empty log).
            fuel = "lpg"
            fl = self.fl
            if fl and fl.entries:
                fuel = fl.entries[0].get("fuel", "lpg")
        d = self._defaults(fuel)
        price = d.get("price_per_l")
        litres = d.get("litres") or 25.0
        total = d.get("total")
        if total is None:
            total = round(litres * price, 2) if price else 100.0
        self.form = {
            "fuel": fuel,
            "by_total": True,            # the owner knows the amount paid + the price
            "total": float(total),
            "litres": float(round(litres, 1)),
            "price": float(price) if price else (3.99 if fuel == "lpg" else 6.49),
            "price_set": price is not None,
            "full": True,
        }

    def computed(self) -> Dict[str, Any]:
        """The command parameters the form stands for (None = not valid)."""
        f = self.form
        p: Dict[str, Any] = {"fuel": f["fuel"], "full": bool(f["full"])}
        if f["by_total"]:
            if f["price"] <= 0 or f["total"] <= 0:
                return {"valid": False, "params": None, "litres": None}
            p["total"] = round(f["total"], 2)
            p["price_per_l"] = round(f["price"], 2)
            litres = round(f["total"] / f["price"], 2)
        else:
            if f["litres"] < 0.5:
                return {"valid": False, "params": None, "litres": None}
            p["litres"] = round(f["litres"], 1)
            litres = p["litres"]
            if f["price_set"] and f["price"] > 0:
                p["price_per_l"] = round(f["price"], 2)
        ok = 0.5 <= litres <= 200
        return {"valid": ok, "params": p if ok else None, "litres": litres}

    # ── lifecycle ──────────────────────────────────────────────────────

    def on_enter(self) -> None:
        self._last_activity = time.time()

    def _timeout(self) -> float:
        if self.app and hasattr(self.app, "config"):
            t = self.app.config.timeout_screen_exit
        else:
            t = 30.0
        return t * (3 if self.mode == "add" else 1)

    def update(self, dt: float) -> None:
        super().update(dt)
        if time.time() - self._last_activity > self._timeout():
            self._exit()

    def _exit(self) -> None:
        if self.app:
            self.app.pop_screen()

    # ── commands ───────────────────────────────────────────────────────

    def _dispatch(self, op: str, params: Optional[Dict[str, Any]] = None) -> None:
        from ...fuel_log.actions import FuelLogCommandAction
        if self._store is not None:
            self._store.dispatch(FuelLogCommandAction(op, params or {}))
        self._sent_at = time.time()
        self._sent_op = op

    def _status_line(self) -> Tuple[str, Any]:
        fl = self.fl
        lc = getattr(fl, "last_command", None) if fl else None
        if self._sent_at is not None:
            got = lc and lc.get("ts", 0) >= self._sent_at - 1 and lc.get("op") == self._sent_op
            if not got:
                if time.time() - self._sent_at > 5:
                    return "NO ANSWER FROM BACKEND", COLORS["amber"]
                return "SENDING...", COLORS["text_tertiary"]
            if not lc.get("ok"):
                return ("ERR: %s" % lc.get("error")).upper(), COLORS["amber"]
            if lc["op"] == "add":
                return "SAVED #%s  %.2f L" % (lc.get("id"), lc.get("litres") or 0), COLORS["green_bright"]
            if lc["op"] == "undo":
                return "REMOVED #%s" % lc.get("id"), COLORS["green_bright"]
            return "SAVED", COLORS["green_bright"]
        return "", COLORS["text_tertiary"]

    # ── input ──────────────────────────────────────────────────────────

    def handle_input(self, event) -> bool:
        self._last_activity = time.time()
        if self.mode == "summary":
            return self._input_summary(event)
        return self._input_add(event)

    def _input_summary(self, event) -> bool:
        if event in (IE.BACK, IE.PRESS_STRONG):
            if self._undo_armed:
                self._undo_armed = False
                return True
            self._exit()
            return True
        if event in (IE.ROTATE_LEFT, IE.ROTATE_RIGHT):
            self._undo_armed = False
            self._action_index = 0 if event == IE.ROTATE_LEFT else 1
            return True
        if event == IE.PRESS_LIGHT:
            if self._action_index == 0:
                self.open_add()
            elif self._undo_armed:
                self._undo_armed = False
                self._dispatch("undo")
            elif self.fl and self.fl.count:
                self._undo_armed = True
            return True
        return False

    def open_add(self) -> None:
        self._reset_form()
        self.mode = "add"
        self._selected_index = 0
        self._editing = False
        self._undo_armed = False

    def _row_kind(self, i: int) -> str:
        return self.ROWS[i]

    def _num_field(self, row: str) -> Optional[str]:
        if row == "amount":
            return "total" if self.form["by_total"] else "litres"
        if row == "price":
            return "price"
        return None

    def _input_add(self, event) -> bool:
        row = self._row_kind(self._selected_index)
        if event in (IE.BACK, IE.PRESS_STRONG):
            if self._editing:
                self._editing = False
            else:
                self.mode = "summary"
            return True
        if self._editing:
            field = self._num_field(row)
            if event in (IE.ROTATE_LEFT, IE.ROTATE_RIGHT):
                d = 1 if event == IE.ROTATE_RIGHT else -1
                step = STEPS[field][self._step_i]
                lo, hi = LIMITS[field]
                v = round(self.form[field] + d * step, 2)
                self.form[field] = min(hi, max(lo, v))
                if field == "price":
                    self.form["price_set"] = True
                return True
            if event == IE.PRESS_LIGHT:
                self._step_i += 1
                if self._step_i >= len(STEPS[field]):
                    self._editing = False
                    self._selected_index = min(self._selected_index + 1, len(self.ROWS) - 1)
                return True
            return False
        if event == IE.ROTATE_LEFT:
            self._selected_index = max(0, self._selected_index - 1)
            return True
        if event == IE.ROTATE_RIGHT:
            self._selected_index = min(len(self.ROWS) - 1, self._selected_index + 1)
            return True
        if event == IE.PRESS_LIGHT:
            if row == "fuel":
                fuel = "petrol" if self.form["fuel"] == "lpg" else "lpg"
                by_total = self.form["by_total"]
                self._reset_form(fuel)
                self.form["by_total"] = by_total
            elif row == "mode":
                self.form["by_total"] = not self.form["by_total"]
                if not self.form["by_total"]:
                    c = self.form["total"] / self.form["price"] if self.form["price"] > 0 else 0
                    self.form["litres"] = round(c, 1) or self.form["litres"]
                elif self.form["price"] > 0:
                    self.form["total"] = round(self.form["litres"] * self.form["price"], 2)
            elif row == "full":
                self.form["full"] = not self.form["full"]
            elif row == "save":
                c = self.computed()
                if c["valid"]:
                    self._dispatch("add", c["params"])
                    self.mode = "summary"
                    self._action_index = 0
            elif row == "cancel":
                self.mode = "summary"
            else:
                self._editing = True
                self._step_i = 0
            return True
        return False

    # ── rendering ──────────────────────────────────────────────────────

    def render(self, surface: pygame.Surface) -> None:
        surface.fill(COLORS["bg_dark"])
        self._render_header(surface)
        if self.mode == "summary":
            self._render_summary(surface)
        else:
            self._render_add(surface)
        self._render_footer(surface)

    def _render_header(self, surface: pygame.Surface) -> None:
        pygame.draw.rect(surface, COLORS["bg_panel"], (0, 0, self.width, self.HEADER_HEIGHT))
        title = "FUEL LOG" if self.mode == "summary" else "ADD FILL"
        s = get_title_font(14).render(title, True, COLORS["cyan"])
        surface.blit(s, ((self.width - s.get_width()) // 2, (self.HEADER_HEIGHT - s.get_height()) // 2))
        odo = self._odometer()
        if odo is not None:
            t = get_mono_font(12).render("%d KM" % int(odo), True, COLORS["text_secondary"])
            surface.blit(t, (self.width - t.get_width() - 8, (self.HEADER_HEIGHT - t.get_height()) // 2))
        fl = self.fl
        if not (fl and fl.enabled):
            t = get_mono_font(12).render("NO LOG", True, COLORS["amber"])
            surface.blit(t, (8, (self.HEADER_HEIGHT - t.get_height()) // 2))
        pygame.draw.line(surface, COLORS["border_focus"], (0, self.HEADER_HEIGHT - 1),
                         (self.width, self.HEADER_HEIGHT - 1))

    def _cap(self, surface, text, x, y, color=None):
        s = get_tiny_font(8).render(text, False, color or COLORS["text_tertiary"])
        surface.blit(s, (x, y))
        return s.get_width()

    def _val(self, surface, text, x, y, color=None, right=None):
        s = get_mono_font(14).render(text, True, color or COLORS["text_value"])
        surface.blit(s, (right - s.get_width() if right is not None else x, y))
        return s.get_width()

    def _render_summary(self, surface: pygame.Surface) -> None:
        figs = getattr(self.fl, "figures", None) or {}
        top = self.HEADER_HEIGHT + 4
        col_w = (self.width - 3 * 8) // 2
        for ci, fuel in enumerate(("lpg", "petrol")):
            x = 8 + ci * (col_w + 8)
            f = figs.get(fuel) or {}
            rect = pygame.Rect(x, top, col_w, 104)
            pygame.draw.rect(surface, COLORS["bg_panel"], rect)
            pygame.draw.rect(surface, COLORS["border_normal"], rect, 1)
            name = get_mono_font(14).render(FUEL_LABEL[fuel], True, COLORS["cyan"])
            surface.blit(name, (x + 6, top + 3))
            last = f.get("last_fill")
            if last:
                lt = time.strftime("%d.%m", time.localtime(last["ts"]))
                txt = "%s %.1f L%s" % (lt, last["litres"], "" if last.get("full") else " P")
                self._val(surface, txt, 0, top + 3, COLORS["text_secondary"], right=x + col_w - 6)
            rows = [
                ("SINCE FILL KM", _fmt(_get(f, "since_last", "km"), 0)),
                ("LAST L/100", _fmt(_get(f, "last", "l_per_100km"), 1)),
                ("AVG %d L/100" % int(figs.get("rolling_n") or 3), _fmt(_get(f, "rolling", "l_per_100km"), 1)),
                ("ALL L/100", _fmt(_get(f, "overall", "l_per_100km"), 1)),
            ]
            y = top + 23
            for cap, val in rows:
                self._cap(surface, cap, x + 6, y + 5)
                self._val(surface, val, 0, y, right=x + col_w - 6)
                y += 19
        # combined
        y = top + 110
        comb = _get(figs, "combined", "overall") or {}
        roll = _get(figs, "combined", "rolling") or {}
        pe = comb.get("petrol_eq_l_per_100km")
        prefix = ">=" if comb.get("lower_bound") else ""
        self._cap(surface, "COMBINED PETROL-EQ L/100", 8, y + 5)
        self._val(surface, prefix + _fmt(pe, 1), 0, y, COLORS["cyan"], right=self.width // 2 - 8)
        self._cap(surface, "COST PLN/100KM", self.width // 2 + 8, y + 5)
        cost = comb.get("cost_per_100km")
        self._val(surface, (prefix if cost is not None else "") + _fmt(cost, 2), 0, y, COLORS["cyan"],
                  right=self.width - 8)
        y += 18
        cal = comb.get("measured_over_est")
        note = "LPG x%.2f ENERGY" % float(figs.get("energy_factor") or 0.77)
        if roll.get("petrol_eq_l_per_100km") is not None and roll is not comb:
            note += "   AVG%d %s%s" % (int(figs.get("rolling_n") or 3),
                                       ">=" if roll.get("lower_bound") else "",
                                       _fmt(roll.get("petrol_eq_l_per_100km"), 1))
        if cal is not None:
            note += "   VS ESTIMATE x%.2f" % cal
        self._cap(surface, note, 8, y + 2)
        # keys + status
        y = self.height - self.FOOTER_HEIGHT - 26
        self._key_rects = []
        labels = ["ADD FILL", "UNDO LAST?" if self._undo_armed else "UNDO LAST"]
        kw = 110
        for i, lab in enumerate(labels):
            r = pygame.Rect(8 + i * (kw + 8), y, kw, 22)
            self._key_rects.append(r)
            sel = i == self._action_index
            pygame.draw.rect(surface, COLORS["bg_frame_focus"] if sel else COLORS["bg_panel"], r)
            border = COLORS["border_active"] if (i == 1 and self._undo_armed) else (
                COLORS["border_focus"] if sel else COLORS["border_normal"])
            pygame.draw.rect(surface, border, r, 1)
            col = COLORS["amber"] if (i == 1 and self._undo_armed) else (
                COLORS["cyan"] if sel else COLORS["text_secondary"])
            s = get_mono_font(14).render(lab, True, col)
            surface.blit(s, (r.x + (r.width - s.get_width()) // 2,
                             r.y + (r.height - s.get_height()) // 2))
        status, col = self._status_line()
        if self._undo_armed and self.fl and self.fl.entries:
            last = max(self.fl.entries, key=lambda e: e.get("id") or 0)
            status, col = ("OK = REMOVE #%s %s %.1f L" % (last.get("id"), FUEL_LABEL.get(last.get("fuel"), "?"),
                                                         last.get("litres") or 0), COLORS["amber"])
        if status:
            s = get_mono_font(14).render(status[:32], True, col)
            surface.blit(s, (8 + 2 * (kw + 8), y + (22 - s.get_height()) // 2))

    def _row_text(self, row: str) -> Tuple[str, str]:
        f = self.form
        if row == "fuel":
            return "FUEL", FUEL_LABEL[f["fuel"]]
        if row == "mode":
            return "ENTER AS", "PAID + PRICE" if f["by_total"] else "LITRES"
        if row == "amount":
            if f["by_total"]:
                return "PAID PLN", "%.2f" % f["total"]
            return "LITRES", "%.1f" % f["litres"]
        if row == "price":
            if not f["by_total"] and not f["price_set"]:
                return "PRICE PLN/L", "-- (OPTIONAL)"
            return "PRICE PLN/L", "%.2f" % f["price"]
        if row == "full":
            return "FULL TANK", "YES" if f["full"] else "NO (PARTIAL)"
        if row == "save":
            return "SAVE", ""
        return "CANCEL", ""

    def _render_add(self, surface: pygame.Surface) -> None:
        y = self.HEADER_HEIGHT + self.ROW_PAD
        font = get_mono_font(14)
        w = self.width - 2 * self.SIDE
        for i, row in enumerate(self.ROWS):
            sel = i == self._selected_index
            editing = sel and self._editing
            r = pygame.Rect(self.SIDE, y, w, self.ROW_H)
            if sel:
                pygame.draw.rect(surface, COLORS["bg_frame_focus"], r)
                pygame.draw.rect(surface, COLORS["border_active"] if editing else COLORS["border_focus"], r, 1)
            label, value = self._row_text(row)
            if row in ("save", "cancel"):
                c = self.computed()
                col = COLORS["cyan"] if sel else COLORS["text_secondary"]
                if row == "save" and not c["valid"]:
                    col = COLORS["text_tertiary"]
                s = font.render("[ %s ]" % label, True, col)
                surface.blit(s, (r.x + (r.width - s.get_width()) // 2, y + (self.ROW_H - s.get_height()) // 2))
            else:
                s = font.render(label, True, COLORS["cyan"] if sel else COLORS["text_secondary"])
                surface.blit(s, (r.x + 8, y + (self.ROW_H - s.get_height()) // 2))
                vcol = COLORS["active"] if editing else (COLORS["text_value"] if sel else COLORS["text_secondary"])
                if editing:
                    value = "< %s >" % value
                vs = font.render(value, True, vcol)
                vx = r.right - vs.get_width() - 8
                surface.blit(vs, (vx, y + (self.ROW_H - vs.get_height()) // 2))
                if editing:
                    self._underline_digit(surface, row, vx, y, font)
            y += self.ROW_H + self.ROW_PAD
        # computed line
        c = self.computed()
        f = self.form
        if f["by_total"]:
            txt = "= %s L" % _fmt(c["litres"], 2)
        else:
            tot = (f["litres"] * f["price"]) if f["price_set"] else None
            txt = "= %s PLN" % _fmt(tot, 2) if tot is not None else "NO PRICE"
        txt += "   %s %s" % (FUEL_LABEL[f["fuel"]], "FULL" if f["full"] else "PARTIAL")
        odo = self._odometer()
        if odo is not None:
            txt += "   AT %d KM" % int(odo)
        s = font.render(txt, True, COLORS["cyan"] if c["valid"] else COLORS["amber"])
        surface.blit(s, (self.SIDE + 8, y + 1))

    def _underline_digit(self, surface, row, vx, y, font) -> None:
        """Mark the digit the encoder changes (coarse to fine)."""
        field = self._num_field(row)
        if field is None:
            return
        step = STEPS[field][self._step_i]
        nd = 2 if field in ("total", "price") else 1
        text = "%.*f" % (nd, self.form[field])
        # index of the digit for this step within text
        dot = text.index(".")
        if step >= 1:
            idx = dot - 1 - int(round(math.log10(step)))
        else:
            idx = dot + int(round(-math.log10(step)))
        if idx < 0:
            return
        x0 = vx + font.size("< " + text[:idx])[0]
        cw = font.size(text[idx])[0]
        pygame.draw.line(surface, COLORS["active"], (x0, y + self.ROW_H - 3), (x0 + cw - 1, y + self.ROW_H - 3))

    def _render_footer(self, surface: pygame.Surface) -> None:
        bar_y = self.height - self.FOOTER_HEIGHT
        pygame.draw.rect(surface, COLORS["bg_panel"], (0, bar_y, self.width, self.FOOTER_HEIGHT))
        pygame.draw.line(surface, COLORS["border_dim"], (0, bar_y), (self.width, bar_y))
        if self.mode == "summary":
            hint = "[TURN] SELECT   [OK] %s   [BACK] EXIT" % ("CONFIRM" if self._undo_armed else "OPEN")
        elif self._editing:
            field = self._num_field(self._row_kind(self._selected_index))
            step = STEPS[field][self._step_i]
            hint = "[TURN] STEP %g   [OK] FINER / NEXT   [BACK] DONE" % step
        else:
            hint = "[TURN] SELECT   [OK] EDIT   [BACK] SUMMARY"
        s = get_mono_font(10).render(hint, True, COLORS["text_tertiary"])
        surface.blit(s, ((self.width - s.get_width()) // 2, bar_y + (self.FOOTER_HEIGHT - s.get_height()) // 2))

    # ── pointer ────────────────────────────────────────────────────────

    def setup_pointer(self) -> None:
        from ..pointer import PointerZone
        from .pointer_helpers import MenuListZone

        screen = self

        class _AddRows(MenuListZone):
            def is_enabled(self):
                return screen.mode == "add"

            def hit_test(self, x, y):
                return screen.mode == "add" and super().hit_test(x, y)

        self.add_pointer_zone(_AddRows(
            self, x=self.SIDE, width=self.width - 2 * self.SIDE,
            first_y=self.HEADER_HEIGHT + self.ROW_PAD, stride=self.ROW_H + self.ROW_PAD,
            row_h=self.ROW_H, bottom=self.HEADER_HEIGHT + self.ROW_PAD + len(self.ROWS) * (self.ROW_H + self.ROW_PAD),
            count=lambda: len(self.ROWS)))

        y = self.height - self.FOOTER_HEIGHT - 26
        for i in range(2):
            def click(x, y_, i=i):
                if self.mode != "summary":
                    return
                if i == 1 and self._action_index != 1:
                    self._undo_armed = False
                self._action_index = i
                self.handle_input(IE.PRESS_LIGHT)
            self.add_pointer_zone(PointerZone(
                (8 + i * 118, y, 110, 22), on_click=click,
                enabled=lambda: self.mode == "summary", name="fuel-key-%d" % i))
        self.add_back_chip()
