"""
Main application class.

Handles the main game loop, event processing, and screen management.
"""

import pygame

from ..config import Config
from ..ui.colors import COLORS
from ..ui.screens.main_screen import MainScreen
from ..input.manager import InputManager, InputEvent
from .renderer import Renderer


class Application:
    """
    Main application controller.
    
    Manages the game loop, screen stack, and coordinates
    between input, rendering, and communication systems.
    """
    
    def __init__(self, config: Config):
        """
        Initialize the application.
        
        Args:
            config: Application configuration
        """
        self.config = config
        self.running = False
        
        # Initialize Pygame
        pygame.init()
        pygame.display.set_caption("CyberPunk Prius - Onboard Computer")
        
        # Create renderer (handles display and scaling)
        self.renderer = Renderer(config)
        
        # Create input manager
        self.input_manager = InputManager(config)
        
        # Screen management
        self.screen_stack: list = []
        self.current_screen = None
        
        # Timing
        self.clock = pygame.time.Clock()
        self.delta_time = 0.0
        
        # Debug info
        self.frame_count = 0
    
    def push_screen(self, screen) -> None:
        """
        Push a new screen onto the stack.
        
        Args:
            screen: Screen to push
        """
        if self.current_screen:
            self.current_screen.on_pause()
            self.screen_stack.append(self.current_screen)
        
        self.current_screen = screen
        self.current_screen.on_enter()
    
    def pop_screen(self) -> bool:
        """
        Pop the current screen and return to previous.
        
        Returns:
            True if a screen was popped, False if at root
        """
        if self.current_screen:
            self.current_screen.on_exit()
        
        if self.screen_stack:
            self.current_screen = self.screen_stack.pop()
            self.current_screen.on_resume()
            return True
        
        return False
    
    def run(self) -> None:
        """Main application loop."""
        self.running = True
        
        # Create and push main screen
        main_screen = MainScreen(self.config.native_size, self)
        self.push_screen(main_screen)
        
        while self.running:
            # Calculate delta time
            self.delta_time = self.clock.tick(self.config.target_fps) / 1000.0
            self.frame_count += 1
            
            # Process events
            self._process_events()
            
            # Update current screen
            if self.current_screen:
                self.current_screen.update(self.delta_time)
            
            # Render
            self._render()
        
    def _process_events(self) -> None:
        """Process pygame events and convert to input events."""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
                return
            
            # Convert pygame events to input events
            input_event = self.input_manager.process_event(event)
            
            if input_event:
                self._handle_input(input_event)
    
    def _handle_input(self, event: InputEvent) -> None:
        """
        Handle an input event.
        
        Args:
            event: Input event to handle
        """
        # Global escape handling
        if event == InputEvent.BACK:
            if not self.pop_screen():
                self.running = False
            return
        
        # Pass to current screen
        if self.current_screen:
            self.current_screen.handle_input(event)
    
    def _render(self) -> None:
        """Render the current frame."""
        # Get the native surface to draw on
        surface = self.renderer.get_surface()
        
        # Clear with background color
        surface.fill(COLORS["bg_dark"])
        
        # Render current screen
        if self.current_screen:
            self.current_screen.render(surface)
        
        # Render debug overlay if enabled
        if self.config.show_fps:
            self._render_fps(surface)
        
        # Present the frame (handles scaling)
        self.renderer.present()
    
    def _render_fps(self, surface: pygame.Surface) -> None:
        """Render FPS counter."""
        fps = self.clock.get_fps()
        font = pygame.font.Font(None, 16)
        fps_text = font.render(f"FPS: {fps:.1f}", True, COLORS["text_secondary"])
        surface.blit(fps_text, (5, 5))
    
    def cleanup(self) -> None:
        """Clean up resources."""
        # Exit all screens
        while self.current_screen:
            self.current_screen.on_exit()
            if self.screen_stack:
                self.current_screen = self.screen_stack.pop()
            else:
                self.current_screen = None
        
        # Cleanup renderer
        self.renderer.cleanup()
        
        # Quit pygame
        pygame.quit()
