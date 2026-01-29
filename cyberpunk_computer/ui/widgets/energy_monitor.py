"""
Energy Monitor Widget for Toyota Prius Gen 2.

Displays hybrid system power flow visualization:
- Battery state of charge (SOC)
- Power flow between battery, motor, ICE, and wheels
- ICE status and regenerative braking indication

Based on real Prius Gen 2 hybrid system architecture.
Enhanced with cyberpunk aesthetics: glowing effects, scanlines, neon colors.
"""

import math
import random
import pygame
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Tuple, List

from .base import Widget, Rect
from ..colors import COLORS, lerp_color, dim_color


class PowerFlowDirection(Enum):
    """Direction of power flow."""
    NONE = 0
    FORWARD = 1      # Power flowing forward (discharge/drive)
    BACKWARD = 2     # Power flowing backward (charge/regen)


@dataclass
class PowerFlow:
    """Power flow state between components."""
    direction: PowerFlowDirection = PowerFlowDirection.NONE
    intensity: float = 0.0  # 0.0 to 1.0


@dataclass 
class Particle:
    """Animated particle for power flow effects."""
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    color: Tuple[int, int, int]
    size: float


class EnergyMonitorWidget(Widget):
    """
    Displays hybrid system energy flow with cyberpunk aesthetics.
    
    Shows the classic Prius energy monitor visualization with:
    - Battery icon on the left with glowing effects
    - Motor/Generator in center with pulsing animation
    - ICE (engine) on top with heat shimmer
    - Wheels on the right with rotation
    
    Enhanced with:
    - Neon glow effects
    - Particle system for power flow
    - Scanline overlay
    - Pulsing borders
    - Data readouts with glitch effects
    """
    
    # Cyberpunk color palette
    COLOR_BATTERY = COLORS["cyan_bright"]
    COLOR_BATTERY_GLOW = (0, 255, 204)  # Neon cyan
    COLOR_MOTOR = COLORS["magenta"]
    COLOR_MOTOR_GLOW = (255, 0, 180)  # Neon magenta
    COLOR_ICE = COLORS["orange"]
    COLOR_ICE_GLOW = (255, 120, 0)  # Neon orange
    COLOR_WHEELS = COLORS["cyan_mid"]
    COLOR_FLOW_ACTIVE = (0, 255, 255)  # Electric cyan
    COLOR_FLOW_REGEN = (255, 0, 255)  # Electric magenta
    COLOR_EV_MODE = (0, 255, 150)  # Green for EV mode
    COLOR_BG = COLORS["bg_dark"]
    COLOR_BORDER = COLORS["cyan_dim"]
    COLOR_SCANLINE = (0, 0, 0)
    
    def __init__(
        self,
        rect: Rect,
        show_labels: bool = True
    ):
        """
        Initialize energy monitor.
        
        Args:
            rect: Widget position and size
            show_labels: Whether to show component labels
        """
        super().__init__(rect, focusable=False)
        
        self.show_labels = show_labels
        
        # State
        self.battery_soc: float = 0.6  # State of charge (0.0 - 1.0)
        self.ice_running: bool = False
        self.ready_mode: bool = False
        self.ev_mode: bool = False  # EV mode indicator
        
        # Speed display
        self.speed_kmh: float = 0.0
        
        # Voltage and current display
        self._current_voltage: float = 0.0
        self._current_amperage: float = 0.0
        self._power_kw: float = 0.0  # Calculated power in kW
        
        # Power history for mini chart
        self._power_history: List[float] = []
        self._power_history_max_size: int = 60
        
        # Delta SOC (difference between min/max cell blocks) for diagnostics
        # This is the key diagnostic value for battery health
        # TODO: Delta SOC requires SOLICITED OBD2 query to ECU 0x7E2 with PID 21CF
        #       See docs/TODO_SOLICITED_OBD2.md for implementation details
        #       Until implemented, this chart will show 0.0%
        self._delta_soc: float = 0.0
        self._delta_soc_history: List[float] = []
        self._delta_soc_history_max_size: int = 120  # Keep more history for trend
        
        # Power flows (intensity 0.0 - 1.0)
        self.flow_battery_motor = PowerFlow()
        self.flow_motor_wheels = PowerFlow()
        self.flow_ice_motor = PowerFlow()
        
        # Animation state
        self._anim_time: float = 0.0
        self._flow_offset: float = 0.0
        self._glitch_timer: float = 0.0
        self._glitch_active: bool = False
        self._pulse_phase: float = 0.0
        
        # Particle system for power flow effects
        self._particles: List[Particle] = []
        self._max_particles: int = 30
        
    def update(self, dt: float) -> None:
        """Update animation state with cyberpunk effects."""
        super().update(dt)
        
        self._anim_time += dt
        self._flow_offset = (self._flow_offset + dt * 80) % 12  # Faster flow animation
        self._pulse_phase += dt * 3.0  # Pulsing effect
        
        # Random glitch effect
        self._glitch_timer -= dt
        if self._glitch_timer <= 0:
            self._glitch_active = random.random() < 0.05  # 5% chance of glitch
            self._glitch_timer = 0.1 if self._glitch_active else random.uniform(0.5, 2.0)
        
        # Update particles
        self._update_particles(dt)
        
        # Spawn new particles along power flow lines
        self._spawn_flow_particles()
        
        self._dirty = True  # Always animate
    
    def _update_particles(self, dt: float) -> None:
        """Update particle positions and lifetimes."""
        alive_particles = []
        for p in self._particles:
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.life -= dt
            if p.life > 0:
                alive_particles.append(p)
        self._particles = alive_particles
        
    def _spawn_flow_particles(self) -> None:
        """Spawn particles along active power flow lines."""
        if len(self._particles) >= self._max_particles:
            return
            
        # Calculate component positions (same as render)
        cx, cy = self.rect.center
        w, h = self.rect.width, self.rect.height
        comp_size = min(w, h) // 5
        
        battery_pos = (self.rect.x + comp_size + 10, cy + comp_size // 2)
        motor_pos = (cx, cy + comp_size // 3)
        wheels_pos = (self.rect.right - comp_size - 15, cy + comp_size // 2)
        ice_pos = (cx, self.rect.y + comp_size + 5)
        
        # Spawn particles based on flow intensity
        if self.flow_battery_motor.direction != PowerFlowDirection.NONE:
            if random.random() < self.flow_battery_motor.intensity * 0.3:
                self._spawn_particle_on_line(battery_pos, motor_pos, 
                    self.COLOR_FLOW_ACTIVE if self.flow_battery_motor.direction == PowerFlowDirection.FORWARD 
                    else self.COLOR_FLOW_REGEN)
                    
        if self.flow_motor_wheels.direction != PowerFlowDirection.NONE:
            if random.random() < self.flow_motor_wheels.intensity * 0.3:
                self._spawn_particle_on_line(motor_pos, wheels_pos,
                    self.COLOR_FLOW_ACTIVE if self.flow_motor_wheels.direction == PowerFlowDirection.FORWARD
                    else self.COLOR_FLOW_REGEN)
                    
        if self.flow_ice_motor.direction != PowerFlowDirection.NONE:
            if random.random() < self.flow_ice_motor.intensity * 0.3:
                self._spawn_particle_on_line(ice_pos, motor_pos, self.COLOR_ICE_GLOW)
    
    def _spawn_particle_on_line(self, start: Tuple[int, int], end: Tuple[int, int], 
                                 color: Tuple[int, int, int]) -> None:
        """Spawn a particle somewhere along a line."""
        t = random.random()
        x = start[0] + (end[0] - start[0]) * t
        y = start[1] + (end[1] - start[1]) * t
        
        # Perpendicular velocity for spread effect
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.sqrt(dx * dx + dy * dy)
        if length > 0:
            nx, ny = -dy / length, dx / length
        else:
            nx, ny = 0, 0
            
        spread = random.uniform(-10, 10)
        particle = Particle(
            x=x + nx * spread,
            y=y + ny * spread,
            vx=random.uniform(-5, 5),
            vy=random.uniform(-5, 5),
            life=random.uniform(0.3, 0.8),
            max_life=0.8,
            color=color,
            size=random.uniform(1, 3)
        )
        self._particles.append(particle)
        
    def set_battery_soc(self, soc: float) -> None:
        """Set battery state of charge (0.0 - 1.0)."""
        self.battery_soc = max(0.0, min(1.0, soc))
        self._dirty = True
    
    def set_speed(self, speed_kmh: float) -> None:
        """Set vehicle speed in km/h."""
        self.speed_kmh = max(0.0, speed_kmh)
        self._dirty = True
    
    def set_voltage(self, voltage: float) -> None:
        """Set current HV battery voltage."""
        self._current_voltage = voltage
        self._update_power()
        self._dirty = True
    
    def set_current(self, current: float) -> None:
        """Set current HV battery current in Amps."""
        self._current_amperage = current
        self._update_power()
        self._dirty = True
        
    def _update_power(self) -> None:
        """Calculate power from voltage and current."""
        if self._current_voltage > 0 and self._current_amperage != 0:
            self._power_kw = (self._current_voltage * self._current_amperage) / 1000.0
            # Add to history
            self._power_history.append(self._power_kw)
            if len(self._power_history) > self._power_history_max_size:
                self._power_history = self._power_history[-self._power_history_max_size:]
    
    def set_ev_mode(self, active: bool) -> None:
        """Set EV mode indicator."""
        self.ev_mode = active
        self._dirty = True
    
    def set_delta_soc(self, delta_soc: float) -> None:
        """
        Set battery delta SOC (difference between min/max cell blocks).
        
        This is a key diagnostic value:
        - 0-1%: Excellent battery health
        - 1-2%: Good condition
        - 2-3%: Fair, may have weak cells
        - >3%: Poor, cells need attention
        """
        self._delta_soc = delta_soc
        self._delta_soc_history.append(delta_soc)
        # Keep history limited
        if len(self._delta_soc_history) > self._delta_soc_history_max_size:
            self._delta_soc_history = self._delta_soc_history[-self._delta_soc_history_max_size:]
        self._dirty = True
        
    def set_ice_running(self, running: bool) -> None:
        """Set ICE (engine) running state."""
        self.ice_running = running
        self._dirty = True
        
    def set_ready_mode(self, ready: bool) -> None:
        """Set vehicle READY mode."""
        self.ready_mode = ready
        self._dirty = True
        
    def set_power_flow(
        self,
        battery_to_motor: float = 0.0,
        motor_to_wheels: float = 0.0,
        ice_to_motor: float = 0.0
    ) -> None:
        """
        Set power flow intensities.
        
        Positive values = forward flow (discharge/drive)
        Negative values = backward flow (charge/regen)
        
        Args:
            battery_to_motor: Battery discharge/charge flow (-1.0 to 1.0)
            motor_to_wheels: Motor drive/regen flow (-1.0 to 1.0)
            ice_to_motor: ICE power output (0.0 to 1.0, always forward)
        """
        self.flow_battery_motor = self._make_flow(battery_to_motor)
        self.flow_motor_wheels = self._make_flow(motor_to_wheels)
        self.flow_ice_motor = self._make_flow(ice_to_motor)
        self._dirty = True
        
    def _make_flow(self, value: float) -> PowerFlow:
        """Convert signed value to PowerFlow."""
        if abs(value) < 0.01:
            return PowerFlow(PowerFlowDirection.NONE, 0.0)
        elif value > 0:
            return PowerFlow(PowerFlowDirection.FORWARD, min(1.0, value))
        else:
            return PowerFlow(PowerFlowDirection.BACKWARD, min(1.0, abs(value)))
            
    def render(self, surface: pygame.Surface) -> None:
        """Render the energy monitor with cyberpunk aesthetics."""
        if not self.visible:
            return
            
        # Draw background with subtle gradient effect
        pygame.draw.rect(surface, self.COLOR_BG, self.rect.to_pygame())
        
        # Draw scanlines for CRT effect
        self._draw_scanlines(surface)
        
        # Calculate component positions
        cx, cy = self.rect.center
        w, h = self.rect.width, self.rect.height
        
        # Component dimensions
        comp_size = min(w, h) // 5
        
        # Component positions
        battery_pos = (self.rect.x + comp_size + 10, cy + comp_size // 2)
        motor_pos = (cx, cy + comp_size // 3)
        ice_pos = (cx, self.rect.y + comp_size + 5)
        wheels_pos = (self.rect.right - comp_size - 15, cy + comp_size // 2)
        
        # Draw flow lines first (behind components)
        self._draw_flow_line(surface, battery_pos, motor_pos, 
                           self.flow_battery_motor, self.COLOR_FLOW_ACTIVE)
        self._draw_flow_line(surface, motor_pos, wheels_pos,
                           self.flow_motor_wheels, self.COLOR_FLOW_ACTIVE)
        self._draw_flow_line(surface, ice_pos, motor_pos,
                           self.flow_ice_motor, self.COLOR_ICE)
        
        # Draw particles
        self._draw_particles(surface)
        
        # Draw components with glow effects
        self._draw_battery(surface, battery_pos, comp_size)
        self._draw_motor(surface, motor_pos, comp_size)
        self._draw_ice(surface, ice_pos, comp_size)
        self._draw_wheels(surface, wheels_pos, comp_size)
        
        # Draw READY/EV indicator
        self._draw_ready_indicator(surface)
        
        # Draw speed display (large, in top-right area)
        self._draw_speed(surface)
        
        # Draw power readout (kW display)
        self._draw_power_display(surface)
        
        # Draw power flow chart (bottom area)
        self._draw_power_chart(surface)
        
        # Draw pulsing border
        self._draw_cyberpunk_border(surface)
        
        # Apply glitch effect occasionally
        if self._glitch_active:
            self._draw_glitch_effect(surface)
    
    def _draw_scanlines(self, surface: pygame.Surface) -> None:
        """Draw CRT-style scanlines overlay."""
        for y in range(self.rect.y, self.rect.bottom, 3):
            alpha = 20 + int(10 * math.sin(y * 0.1 + self._anim_time))
            pygame.draw.line(surface, (0, 0, 0), 
                           (self.rect.x, y), (self.rect.right, y), 1)
    
    def _draw_particles(self, surface: pygame.Surface) -> None:
        """Draw animated particles."""
        for p in self._particles:
            alpha = p.life / p.max_life
            size = int(p.size * alpha)
            if size > 0:
                color = tuple(int(c * alpha) for c in p.color)
                pygame.draw.circle(surface, color, (int(p.x), int(p.y)), size)
    
    def _draw_power_display(self, surface: pygame.Surface) -> None:
        """Draw power (kW) display with direction indicator."""
        from ..fonts import get_font, get_tiny_font
        
        # Power display area - left of speed
        x = self.rect.x + 4
        y = self.rect.y + 20
        
        font = get_font(14, "title")
        font_small = get_tiny_font(8)
        
        # Determine color and sign based on power direction
        if abs(self._power_kw) < 0.1:
            color = dim_color(COLORS["cyan_mid"], 0.5)
            power_text = "0.0"
            direction = "IDLE"
        elif self._power_kw > 0:
            # Positive = discharging
            color = self.COLOR_FLOW_ACTIVE
            power_text = f"+{abs(self._power_kw):.1f}"
            direction = "OUT"
        else:
            # Negative = charging/regen
            color = self.COLOR_FLOW_REGEN
            power_text = f"-{abs(self._power_kw):.1f}"
            direction = "REGEN"
        
        # Pulsing effect for active power
        if abs(self._power_kw) > 0.5:
            pulse = 0.7 + 0.3 * math.sin(self._pulse_phase * 2)
            color = lerp_color(dim_color(color, 0.5), color, pulse)
        
        # Render power value
        power_surf = font.render(power_text, True, color)
        surface.blit(power_surf, (x, y))
        
        # kW label
        unit_surf = font_small.render("kW", True, dim_color(color, 0.7))
        surface.blit(unit_surf, (x + power_surf.get_width() + 2, y + power_surf.get_height() - 10))
        
        # Direction indicator
        dir_surf = font_small.render(direction, True, dim_color(color, 0.5))
        surface.blit(dir_surf, (x, y + power_surf.get_height()))
    
    def _draw_power_chart(self, surface: pygame.Surface) -> None:
        """Draw mini power history chart."""
        from ..fonts import get_tiny_font
        
        # Chart area - bottom strip of widget
        chart_height = 28
        chart_width = self.rect.width - 8
        chart_x = self.rect.x + 4
        chart_y = self.rect.bottom - chart_height - 4
        
        # Draw chart background
        bg_rect = (chart_x, chart_y, chart_width, chart_height)
        pygame.draw.rect(surface, dim_color(COLORS["bg_dark"], 0.5), bg_rect)
        
        # Draw center line (0 kW)
        center_y = chart_y + chart_height // 2
        pygame.draw.line(surface, dim_color(COLORS["cyan_dim"], 0.3),
                        (chart_x, center_y), (chart_x + chart_width, center_y), 1)
        
        # Draw power history as line chart
        if len(self._power_history) > 1:
            max_power = 30.0  # Scale: -30kW to +30kW
            points = []
            for i, power in enumerate(self._power_history):
                x = chart_x + 2 + int((i / max(len(self._power_history) - 1, 1)) * (chart_width - 4))
                # Normalize: positive = above center, negative = below
                y_norm = max(-1.0, min(1.0, power / max_power))
                y = center_y - int(y_norm * (chart_height // 2 - 2))
                points.append((x, y))
            
            if len(points) > 1:
                # Draw discharge in cyan, charge in magenta
                # For simplicity, color based on last value
                if self._power_kw > 0:
                    line_color = self.COLOR_FLOW_ACTIVE
                elif self._power_kw < 0:
                    line_color = self.COLOR_FLOW_REGEN
                else:
                    line_color = COLORS["cyan_mid"]
                pygame.draw.lines(surface, line_color, False, points, 2)
        
        # Draw current power as vertical bar
        if abs(self._power_kw) > 0.1:
            bar_width = 4
            bar_x = chart_x + chart_width - bar_width - 2
            bar_max_height = chart_height // 2 - 2
            bar_height = int(min(abs(self._power_kw) / 30.0, 1.0) * bar_max_height)
            
            if self._power_kw > 0:
                # Discharge - bar goes up
                bar_color = self.COLOR_FLOW_ACTIVE
                pygame.draw.rect(surface, bar_color,
                               (bar_x, center_y - bar_height, bar_width, bar_height))
            else:
                # Charge - bar goes down
                bar_color = self.COLOR_FLOW_REGEN
                pygame.draw.rect(surface, bar_color,
                               (bar_x, center_y, bar_width, bar_height))
        
        # Draw chart border with pulsing effect
        pulse = 0.3 + 0.2 * math.sin(self._pulse_phase)
        border_color = lerp_color(dim_color(COLORS["cyan_dim"], 0.2), COLORS["cyan_dim"], pulse)
        pygame.draw.rect(surface, border_color, bg_rect, 1)
        
        # Labels
        font = get_tiny_font(7)
        label_surf = font.render("PWR", True, dim_color(COLORS["cyan_dim"], 0.6))
        surface.blit(label_surf, (chart_x + 2, chart_y + 1))
    
    def _draw_cyberpunk_border(self, surface: pygame.Surface) -> None:
        """Draw pulsing neon border."""
        # Calculate pulse intensity
        pulse = 0.5 + 0.5 * math.sin(self._pulse_phase)
        
        # Base border
        border_color = lerp_color(dim_color(self.COLOR_BORDER, 0.3), self.COLOR_BORDER, pulse)
        pygame.draw.rect(surface, border_color, self.rect.to_pygame(), 1)
        
        # Corner accents - bright corners
        corner_size = 8
        corner_color = lerp_color(COLORS["cyan_dim"], self.COLOR_BATTERY_GLOW, pulse * 0.5)
        
        # Top-left
        pygame.draw.line(surface, corner_color, 
                        (self.rect.x, self.rect.y), (self.rect.x + corner_size, self.rect.y), 2)
        pygame.draw.line(surface, corner_color,
                        (self.rect.x, self.rect.y), (self.rect.x, self.rect.y + corner_size), 2)
        
        # Top-right
        pygame.draw.line(surface, corner_color,
                        (self.rect.right - corner_size, self.rect.y), (self.rect.right, self.rect.y), 2)
        pygame.draw.line(surface, corner_color,
                        (self.rect.right - 1, self.rect.y), (self.rect.right - 1, self.rect.y + corner_size), 2)
        
        # Bottom-left
        pygame.draw.line(surface, corner_color,
                        (self.rect.x, self.rect.bottom - 1), (self.rect.x + corner_size, self.rect.bottom - 1), 2)
        pygame.draw.line(surface, corner_color,
                        (self.rect.x, self.rect.bottom - corner_size), (self.rect.x, self.rect.bottom), 2)
        
        # Bottom-right
        pygame.draw.line(surface, corner_color,
                        (self.rect.right - corner_size, self.rect.bottom - 1), (self.rect.right, self.rect.bottom - 1), 2)
        pygame.draw.line(surface, corner_color,
                        (self.rect.right - 1, self.rect.bottom - corner_size), (self.rect.right - 1, self.rect.bottom), 2)
    
    def _draw_glitch_effect(self, surface: pygame.Surface) -> None:
        """Draw occasional glitch effect."""
        # Random horizontal slice displacement
        slice_y = random.randint(self.rect.y, self.rect.bottom - 5)
        slice_height = random.randint(2, 8)
        offset = random.randint(-5, 5)
        
        if offset != 0 and slice_y + slice_height < self.rect.bottom:
            # Just draw some glitch lines
            glitch_color = random.choice([self.COLOR_FLOW_ACTIVE, self.COLOR_FLOW_REGEN, (255, 255, 255)])
            pygame.draw.line(surface, glitch_color,
                           (self.rect.x + abs(offset), slice_y),
                           (self.rect.right - abs(offset), slice_y), 1)
        
    def _draw_battery(self, surface: pygame.Surface, pos: Tuple[int, int], size: int) -> None:
        """Draw battery icon with SOC level and neon glow effects."""
        x, y = pos[0] - size // 2, pos[1] - size // 2
        bw, bh = size, int(size * 0.6)
        
        # Determine base color based on charge state
        is_charging = self.flow_battery_motor.direction == PowerFlowDirection.BACKWARD
        is_discharging = self.flow_battery_motor.direction == PowerFlowDirection.FORWARD
        
        if is_charging:
            base_color = self.COLOR_FLOW_REGEN
            glow_color = self.COLOR_MOTOR_GLOW
        elif is_discharging:
            base_color = self.COLOR_FLOW_ACTIVE
            glow_color = self.COLOR_BATTERY_GLOW
        else:
            base_color = self.COLOR_BATTERY
            glow_color = self.COLOR_BATTERY_GLOW
        
        # Outer glow effect when active
        if is_charging or is_discharging:
            pulse = 0.5 + 0.5 * math.sin(self._pulse_phase * 2)
            glow_dim = dim_color(glow_color, 0.2 * pulse)
            pygame.draw.rect(surface, glow_dim, (x - 2, y - 2, bw + 4, bh + 4), 0)
        
        # Battery outline with pulse
        outline_color = base_color if self.ready_mode else dim_color(base_color, 0.4)
        pygame.draw.rect(surface, outline_color, (x, y, bw, bh), 2)
        
        # Battery terminal
        term_w, term_h = 4, bh // 3
        pygame.draw.rect(surface, outline_color, 
                        (x + bw, y + (bh - term_h) // 2, term_w, term_h))
        
        # SOC fill with gradient effect
        fill_w = int((bw - 4) * self.battery_soc)
        if fill_w > 0:
            # Color based on SOC level
            if self.battery_soc > 0.6:
                fill_color = self.COLOR_BATTERY_GLOW
            elif self.battery_soc > 0.3:
                fill_color = COLORS["orange"]
            else:
                fill_color = COLORS["error"]
            
            # Add pulsing to fill when active
            if is_charging or is_discharging:
                pulse = 0.7 + 0.3 * math.sin(self._pulse_phase * 3)
                fill_color = lerp_color(dim_color(fill_color, 0.6), fill_color, pulse)
                
            pygame.draw.rect(surface, fill_color,
                           (x + 2, y + 2, fill_w, bh - 4))
            
            # Inner highlight line
            highlight = lerp_color(fill_color, (255, 255, 255), 0.3)
            pygame.draw.line(surface, highlight, 
                           (x + 2, y + 3), (x + 2 + fill_w - 1, y + 3), 1)
        
        # SOC percentage text
        if self.show_labels:
            from ..fonts import get_tiny_font
            font = get_tiny_font(8)
            soc_text = f"{int(self.battery_soc * 100)}%"
            text_color = base_color if self.ready_mode else dim_color(base_color, 0.5)
            text_surf = font.render(soc_text, True, text_color)
            surface.blit(text_surf, (x + (bw - text_surf.get_width()) // 2,
                                    y + bh + 2))
                                    
    def _draw_motor(self, surface: pygame.Surface, pos: Tuple[int, int], size: int) -> None:
        """Draw motor/generator icon with spinning effect."""
        x, y = pos
        radius = size // 3
        
        # Determine if motor is active
        is_active = (self.flow_battery_motor.direction != PowerFlowDirection.NONE or 
                     self.flow_motor_wheels.direction != PowerFlowDirection.NONE)
        
        # Motor color with pulsing
        if is_active and self.ready_mode:
            pulse = 0.6 + 0.4 * math.sin(self._pulse_phase * 4)
            color = lerp_color(self.COLOR_MOTOR, self.COLOR_MOTOR_GLOW, pulse)
            # Outer glow
            glow_color = dim_color(self.COLOR_MOTOR_GLOW, 0.2 * pulse)
            pygame.draw.circle(surface, glow_color, (x, y), radius + 3)
        elif self.ready_mode:
            color = self.COLOR_MOTOR
        else:
            color = dim_color(self.COLOR_MOTOR, 0.3)
        
        # Motor circle
        pygame.draw.circle(surface, color, (x, y), radius, 2)
        
        # Rotating inner segments when active
        if is_active and self.ready_mode:
            seg_count = 6
            for i in range(seg_count):
                angle = self._anim_time * 4 + i * (2 * math.pi / seg_count)
                inner_r = radius - 4
                end_x = x + int(math.cos(angle) * inner_r)
                end_y = y + int(math.sin(angle) * inner_r)
                seg_color = lerp_color(dim_color(color, 0.3), color, 0.5)
                pygame.draw.line(surface, seg_color, (x, y), (end_x, end_y), 1)
        
        # Label
        if self.show_labels:
            from ..fonts import get_tiny_font
            font = get_tiny_font(9)
            text_surf = font.render("MG", True, color)
            surface.blit(text_surf, (x - text_surf.get_width() // 2,
                                    y - text_surf.get_height() // 2))
                                    
    def _draw_ice(self, surface: pygame.Surface, pos: Tuple[int, int], size: int) -> None:
        """Draw ICE (engine) icon with heat shimmer effect."""
        x, y = pos[0] - size // 3, pos[1] - size // 4
        w, h = size * 2 // 3, size // 2
        
        # Engine block color
        if self.ice_running:
            pulse = 0.7 + 0.3 * math.sin(self._anim_time * 8)
            color = lerp_color(self.COLOR_ICE, self.COLOR_ICE_GLOW, pulse)
            # Heat glow
            glow_color = dim_color(self.COLOR_ICE_GLOW, 0.15 * pulse)
            pygame.draw.rect(surface, glow_color, (x - 2, y - 2, w + 4, h + 4))
        else:
            color = dim_color(self.COLOR_ICE, 0.3)
        
        # Main block
        pygame.draw.rect(surface, color, (x, y, w, h), 1)
        
        # Engine fill when running
        if self.ice_running:
            # Animated "heat" bars inside
            bar_count = 3
            for i in range(bar_count):
                bar_phase = self._anim_time * 6 + i * 0.5
                bar_intensity = 0.3 + 0.4 * math.sin(bar_phase)
                bar_color = lerp_color(dim_color(color, 0.2), color, bar_intensity)
                bar_y = y + 3 + i * (h - 6) // bar_count
                bar_h = max(2, (h - 6) // bar_count - 2)
                pygame.draw.rect(surface, bar_color, (x + 2, bar_y, w - 4, bar_h))
        
        # ICE label
        if self.show_labels:
            from ..fonts import get_tiny_font
            font = get_tiny_font(8)
            text_surf = font.render("ICE", True, color)
            surface.blit(text_surf, (x + (w - text_surf.get_width()) // 2,
                                    y + h + 2))
                                    
    def _draw_wheels(self, surface: pygame.Surface, pos: Tuple[int, int], size: int) -> None:
        """Draw wheel icon with enhanced spinning effect."""
        x, y = pos
        radius = size // 3
        
        is_moving = self.flow_motor_wheels.direction != PowerFlowDirection.NONE
        is_regen = self.flow_motor_wheels.direction == PowerFlowDirection.BACKWARD
        
        # Color based on state
        if is_regen:
            color = self.COLOR_FLOW_REGEN
        elif is_moving and self.ready_mode:
            color = self.COLOR_FLOW_ACTIVE
        elif self.ready_mode:
            color = self.COLOR_WHEELS
        else:
            color = dim_color(self.COLOR_WHEELS, 0.3)
        
        # Outer glow when moving
        if is_moving:
            pulse = 0.3 + 0.3 * math.sin(self._pulse_phase * 2)
            glow_color = dim_color(color, 0.15 * pulse)
            pygame.draw.circle(surface, glow_color, (x, y), radius + 4)
        
        # Wheel circle - double ring for tire effect
        pygame.draw.circle(surface, color, (x, y), radius, 2)
        pygame.draw.circle(surface, dim_color(color, 0.4), (x, y), radius - 4, 1)
        
        # Spokes (rotating animation when moving)
        spoke_speed = 5 if is_moving else 0
        spoke_angle = self._anim_time * spoke_speed * self.flow_motor_wheels.intensity
        spoke_count = 5
        for i in range(spoke_count):
            angle = spoke_angle + i * (2 * math.pi / spoke_count)
            end_x = x + int(math.cos(angle) * (radius - 5))
            end_y = y + int(math.sin(angle) * (radius - 5))
            spoke_color = color if is_moving else dim_color(color, 0.5)
            pygame.draw.line(surface, spoke_color, (x, y), (end_x, end_y), 1)
        
        # Speed indicator below wheel
        if is_moving and self.speed_kmh > 0:
            from ..fonts import get_tiny_font
            font = get_tiny_font(7)
            speed_text = f"{int(self.speed_kmh)}"
            text_surf = font.render(speed_text, True, color)
            surface.blit(text_surf, (x - text_surf.get_width() // 2, y + radius + 2))
            
    def _draw_flow_line(
        self,
        surface: pygame.Surface,
        start: Tuple[int, int],
        end: Tuple[int, int],
        flow: PowerFlow,
        color: Tuple[int, int, int]
    ) -> None:
        """Draw animated power flow line with neon effect."""
        if flow.direction == PowerFlowDirection.NONE:
            # Draw dim static line with dashes
            self._draw_dashed_line(surface, start, end, dim_color(color, 0.15), 1, 4)
            return
            
        # Calculate line properties
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.sqrt(dx * dx + dy * dy)
        
        if length < 1:
            return
            
        # Normalize direction
        nx, ny = dx / length, dy / length
        
        # Determine flow direction for animation
        actual_start, actual_end = start, end
        if flow.direction == PowerFlowDirection.BACKWARD:
            # Reverse direction
            actual_start, actual_end = end, start
            nx, ny = -nx, -ny
            color = self.COLOR_FLOW_REGEN  # Use regen color
        
        # Pulsing intensity
        pulse = 0.6 + 0.4 * math.sin(self._pulse_phase * 2)
        effective_intensity = flow.intensity * pulse
        
        # Draw glow line (wider, dimmer)
        glow_color = dim_color(color, 0.2 * effective_intensity)
        pygame.draw.line(surface, glow_color, start, end, 5)
        
        # Draw base line
        line_color = lerp_color(dim_color(color, 0.4), color, effective_intensity)
        pygame.draw.line(surface, line_color, start, end, 2)
        
        # Draw animated energy packets along line
        packet_spacing = 12
        num_packets = max(2, int(length / packet_spacing))
        
        for i in range(num_packets):
            # Calculate packet position with animation offset
            t = ((i * packet_spacing + self._flow_offset * flow.intensity * 4) % length) / length
            packet_x = int(actual_start[0] + (actual_end[0] - actual_start[0]) * t)
            packet_y = int(actual_start[1] + (actual_end[1] - actual_start[1]) * t)
            
            # Packet brightness varies along the line
            brightness = 0.5 + 0.5 * math.sin(t * math.pi)
            packet_color = lerp_color(dim_color(color, 0.5), color, brightness * effective_intensity)
            
            # Draw packet as small glowing dot
            pygame.draw.circle(surface, packet_color, (packet_x, packet_y), 3)
            # Inner bright core
            core_color = lerp_color(packet_color, (255, 255, 255), 0.4)
            pygame.draw.circle(surface, core_color, (packet_x, packet_y), 1)
    
    def _draw_dashed_line(self, surface: pygame.Surface, start: Tuple[int, int], 
                          end: Tuple[int, int], color: Tuple[int, int, int],
                          width: int, dash_length: int) -> None:
        """Draw a dashed line."""
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.sqrt(dx * dx + dy * dy)
        if length < 1:
            return
        
        nx, ny = dx / length, dy / length
        num_dashes = int(length / (dash_length * 2))
        
        for i in range(num_dashes):
            dash_start = (
                int(start[0] + nx * i * dash_length * 2),
                int(start[1] + ny * i * dash_length * 2)
            )
            dash_end = (
                int(start[0] + nx * (i * dash_length * 2 + dash_length)),
                int(start[1] + ny * (i * dash_length * 2 + dash_length))
            )
            pygame.draw.line(surface, color, dash_start, dash_end, width)
            
    def _draw_ready_indicator(self, surface: pygame.Surface) -> None:
        """Draw READY/EV mode indicator with cyberpunk styling."""
        if not self.show_labels:
            return
            
        from ..fonts import get_tiny_font
        font = get_tiny_font(8)
        
        x = self.rect.x + 4
        y = self.rect.y + 4
        
        # READY indicator
        if self.ready_mode:
            pulse = 0.7 + 0.3 * math.sin(self._pulse_phase)
            color = lerp_color(dim_color(COLORS["active"], 0.6), COLORS["active"], pulse)
            text = "READY"
        else:
            color = dim_color(COLORS["inactive"], 0.4)
            text = "OFF"
            
        text_surf = font.render(text, True, color)
        surface.blit(text_surf, (x, y))
        
        # EV mode indicator (shown when EV mode active or ICE not running while ready)
        if self.ready_mode and not self.ice_running:
            ev_pulse = 0.6 + 0.4 * math.sin(self._pulse_phase * 1.5)
            ev_color = lerp_color(dim_color(self.COLOR_EV_MODE, 0.5), self.COLOR_EV_MODE, ev_pulse)
            ev_surf = font.render("EV", True, ev_color)
            surface.blit(ev_surf, (x + text_surf.get_width() + 6, y))
    
    def _draw_speed(self, surface: pygame.Surface) -> None:
        """Draw large speed display in top-right area."""
        from ..fonts import get_font, get_tiny_font
        
        # Speed in top-right corner of widget
        speed_text = f"{int(self.speed_kmh)}"
        
        # Large font for speed number, smaller for unit
        font_large = get_font(24, "title")  # Use title font for large speed
        font_small = get_tiny_font(8)
        
        # Render speed number
        color = COLORS["cyan_bright"] if self.speed_kmh > 0 else dim_color(COLORS["cyan_mid"], 0.6)
        speed_surf = font_large.render(speed_text, True, color)
        
        # Render "km/h" label
        unit_surf = font_small.render("km/h", True, dim_color(COLORS["cyan_dim"], 0.8))
        
        # Position in top-right area
        x = self.rect.right - speed_surf.get_width() - 8
        y = self.rect.y + 4
        
        surface.blit(speed_surf, (x, y))
        surface.blit(unit_surf, (x + speed_surf.get_width() - unit_surf.get_width(), 
                                 y + speed_surf.get_height() - 2))


class MiniEnergyMonitor(Widget):
    """
    Compact energy monitor for status bar.
    
    Shows simplified battery level and power direction.
    """
    
    def __init__(self, rect: Rect):
        """Initialize mini energy monitor."""
        super().__init__(rect, focusable=False)
        
        self.battery_soc: float = 0.6
        self.charging: bool = False
        self.discharging: bool = False
        
    def set_state(self, soc: float, charging: bool = False, discharging: bool = False) -> None:
        """Set battery state."""
        self.battery_soc = max(0.0, min(1.0, soc))
        self.charging = charging
        self.discharging = discharging
        self._dirty = True
        
    def render(self, surface: pygame.Surface) -> None:
        """Render mini battery indicator."""
        if not self.visible:
            return
            
        x, y = self.rect.x, self.rect.y
        w, h = self.rect.width, self.rect.height
        
        # Battery outline
        bw = w - 4
        pygame.draw.rect(surface, COLORS["cyan_dim"], (x, y, bw, h), 1)
        
        # Terminal
        pygame.draw.rect(surface, COLORS["cyan_dim"], (x + bw, y + h // 4, 3, h // 2))
        
        # Fill
        fill_w = int((bw - 2) * self.battery_soc)
        if fill_w > 0:
            if self.battery_soc > 0.6:
                color = COLORS["cyan_mid"]
            elif self.battery_soc > 0.3:
                color = COLORS["orange"]
            else:
                color = COLORS["error"]
                
            if self.charging:
                color = COLORS["magenta"]
            elif self.discharging:
                color = COLORS["cyan_bright"]
                
            pygame.draw.rect(surface, color, (x + 1, y + 1, fill_w, h - 2))
