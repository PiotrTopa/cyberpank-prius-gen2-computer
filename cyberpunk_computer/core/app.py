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
        
        # Input source (file replay or real gateway)
        self._input_source = None
        self._gateway_adapter = None
        
        # Verbose logging flags
        self._verbose_in = False   # Log incoming messages
        self._verbose_out = False  # Log outgoing commands
        
        # Verbose source filters (which data sources to show)
        self._verbose_avc = True   # Show AVC-LAN messages
        self._verbose_can = True   # Show CAN messages
        self._verbose_sat = True   # Show RS485/Satellite messages
        
        # Developer analysis mode - detailed logging for reverse engineering
        self._analysis_mode = False
        self._analysis_log_file = None
        
        # Jump mode state (for entering row numbers)
        self._jump_mode = False
        self._jump_buffer = ""
    
    def set_input_source(self, input_source) -> None:
        """
        Set input source for gateway data.
        
        Args:
            input_source: FileInput or GatewayConnection instance
        """
        self._input_source = input_source
        
        # Create gateway adapter to process messages
        from ..comm.gateway_adapter import GatewayAdapter
        from ..state.store import Store
        
        # Create store and adapter
        store = Store()
        self._gateway_adapter = GatewayAdapter(store)
        
        # Set up verbose logging callback
        self._gateway_adapter.set_message_log_callback(self._log_gateway_message)
        
        # Store reference for UI
        self._store = store
    
    def _log_gateway_message(self, message, direction: str) -> None:
        """Log gateway message if verbose enabled."""
        from ..comm.gateway_adapter import MessageType
        from ..comm.protocol import DEVICE_SATELLITE_BASE
        
        # Check direction filter
        if direction == "IN" and not self._verbose_in:
            return
        if direction == "OUT" and not self._verbose_out:
            return
        
        # Check source filter based on message type
        raw = message.raw
        device_id = raw.get("id", 0)
        
        if message.type == MessageType.AVC_LAN:
            if not self._verbose_avc:
                return
            source_tag = "AVC"
        elif message.type == MessageType.CAN:
            if not self._verbose_can:
                return
            source_tag = "CAN"
        elif device_id >= DEVICE_SATELLITE_BASE:
            if not self._verbose_sat:
                return
            source_tag = f"SAT{device_id}"
        else:
            # Gateway system messages - always show when verbose is on
            source_tag = "SYS"
        
        # Format the message based on type
        d = raw.get("d", {})
        seq = raw.get("seq", "-")
        if seq is None:
            seq = "-"
        
        arrow = "<--" if direction == "IN" else "-->"
        prefix = "[IN]" if direction == "IN" else "[OUT]"
        
        if message.type == MessageType.AVC_LAN:
            # AVC-LAN format: master->slave: [data]
            master = d.get("m", "???")
            slave = d.get("s", "???")
            data = d.get("d", [])
            
            # Format addresses - convert int to hex if needed
            if isinstance(master, int):
                master = f"{master:03X}"
            if isinstance(slave, int):
                slave = f"{slave:03X}"
            
            # Format data bytes
            if data:
                data_hex = " ".join(
                    f"{b:02X}" if isinstance(b, int) else b 
                    for b in data
                )
            else:
                data_hex = ""
            
            print(f"{prefix} [{source_tag}] [{seq:>4}] {arrow} {master}->{slave}: [{data_hex}]", flush=True)
            
        elif message.type == MessageType.CAN:
            # CAN format: id [data]
            can_id = d.get("id", d.get("i", "???"))
            data = d.get("data", d.get("d", []))
            
            # Format data bytes
            if data:
                data_hex = " ".join(
                    f"{b:02X}" if isinstance(b, int) else str(b)
                    for b in data
                )
            else:
                data_hex = ""
            
            print(f"{prefix} [{source_tag}] [{seq:>4}] {arrow} ID:{can_id} [{data_hex}]", flush=True)
            
        elif device_id >= DEVICE_SATELLITE_BASE:
            # RS485 Satellite format
            print(f"{prefix} [{source_tag}] [{seq:>4}] {arrow} {d}", flush=True)
            
        else:
            # System messages
            print(f"{prefix} [{source_tag}] [{seq:>4}] {arrow} {d}", flush=True)
    
    def _print_source_filter_status(self) -> None:
        """Print current source filter status."""
        avc = "ON" if self._verbose_avc else "OFF"
        can = "ON" if self._verbose_can else "OFF"
        sat = "ON" if self._verbose_sat else "OFF"
        print(f"      Sources: AVC-LAN={avc}, CAN={can}, RS485={sat}")
    
    def _print_stats(self) -> None:
        """Print message statistics from gateway adapter."""
        print("\n===================================================================")
        print("                    MESSAGE STATISTICS")
        print("===================================================================")
        
        if self._gateway_adapter:
            stats = self._gateway_adapter.stats
            print(f"  Messages received:  {stats.get('messages_received', 0):>6}")
            print(f"  AVC-LAN messages:   {stats.get('avc_messages', 0):>6}")
            print(f"  CAN messages:       {stats.get('can_messages', 0):>6}")
            print(f"  Errors:             {stats.get('errors', 0):>6}")
        
        if self._input_source:
            print(f"\n  Playback position:  {self._input_source.position}/{self._input_source.total_entries}")
            progress = int(self._input_source.progress * 100)
            print(f"  Progress:           {progress}%")
        
        # Show logging filter status
        v_in = "ON" if self._verbose_in else "OFF"
        v_out = "ON" if self._verbose_out else "OFF"
        analysis = "ON" if self._analysis_mode else "OFF"
        print(f"\n  Verbose logging:    IN={v_in}, OUT={v_out}")
        avc = "ON" if self._verbose_avc else "OFF"
        can = "ON" if self._verbose_can else "OFF"
        sat = "ON" if self._verbose_sat else "OFF"
        print(f"  Source filters:     AVC-LAN={avc}, CAN={can}, RS485={sat}")
        print(f"  Analysis mode:      {analysis}")
        
        print("===================================================================\n")
    
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
        
        # Connect store to main screen if available
        if hasattr(self, '_store') and self._store:
            main_screen.set_store(self._store)
        
        self.push_screen(main_screen)
        
        while self.running:
            # Calculate delta time
            self.delta_time = self.clock.tick(self.config.target_fps) / 1000.0
            self.frame_count += 1
            
            # Update input source (file replay or gateway)
            if self._input_source:
                self._input_source.update()
                
                # Process incoming messages through gateway adapter
                if self._gateway_adapter:
                    while True:
                        msg = self._input_source.receive()
                        if msg is None:
                            break
                        # Convert Message to raw dict format for adapter
                        raw = {
                            "id": msg.device_id,
                            "d": msg.data,
                            "ts": msg.timestamp,
                            "seq": msg.sequence
                        }
                        self._gateway_adapter.process_message(raw)
            
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
            
            # Handle playback control keys for file input
            if self._input_source and event.type == pygame.KEYDOWN:
                from ..comm.file_input import FileInput
                if isinstance(self._input_source, FileInput):
                    # Handle jump mode number input
                    if self._jump_mode:
                        if event.key == pygame.K_RETURN:
                            # Execute jump
                            try:
                                pos = int(self._jump_buffer)
                                self._input_source.seek(pos)
                                print(f"[PLAY] Jumped to row {pos} - {self._input_source.get_status()}")
                            except ValueError:
                                print("[ERR] Invalid row number")
                            self._jump_mode = False
                            self._jump_buffer = ""
                            continue
                        elif event.key == pygame.K_ESCAPE:
                            # Cancel jump
                            print("[ERR] Jump cancelled")
                            self._jump_mode = False
                            self._jump_buffer = ""
                            continue
                        elif event.key == pygame.K_BACKSPACE:
                            # Delete last character
                            self._jump_buffer = self._jump_buffer[:-1]
                            print(f"\r[JMP] Jump to row: {self._jump_buffer}_  ", end="", flush=True)
                            continue
                        elif event.unicode.isdigit():
                            # Add digit
                            self._jump_buffer += event.unicode
                            print(f"\r[JMP] Jump to row: {self._jump_buffer}_  ", end="", flush=True)
                            continue
                        else:
                            # Ignore other keys in jump mode
                            continue
                    
                    if event.key == pygame.K_p:
                        self._input_source.toggle()
                        print(f"[PLAY] {self._input_source.get_status()}")
                        continue
                    elif event.key == pygame.K_r:
                        self._input_source.stop()
                        self._input_source.start()
                        print("[PLAY] Restarted")
                        continue
                    elif event.key == pygame.K_j:
                        # J = enter jump mode
                        self._input_source.pause()
                        self._jump_mode = True
                        self._jump_buffer = ""
                        print(f"[JMP] Jump to row: _  (Enter=confirm, Esc=cancel)", end="", flush=True)
                        continue
                    elif event.key == pygame.K_LEFTBRACKET:
                        # [ = step backward 1
                        self._input_source.pause()
                        self._input_source.seek(self._input_source.position - 1)
                        print(f"[PLAY] {self._input_source.get_status()}")
                        continue
                    elif event.key == pygame.K_RIGHTBRACKET:
                        # ] = step forward 1
                        self._input_source.pause()
                        self._input_source.seek(self._input_source.position + 1)
                        print(f"[PLAY] {self._input_source.get_status()}")
                        continue
                    elif event.key == pygame.K_MINUS:
                        # - = step backward 10
                        self._input_source.pause()
                        self._input_source.seek(self._input_source.position - 10)
                        print(f"[PLAY] {self._input_source.get_status()}")
                        continue
                    elif event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                        # + = step forward 10
                        self._input_source.pause()
                        self._input_source.seek(self._input_source.position + 10)
                        print(f"[PLAY] {self._input_source.get_status()}")
                        continue
            
            # Verbose logging toggle keys (dev mode only)
            if self.config.dev_mode and event.type == pygame.KEYDOWN:
                if event.key == pygame.K_v:
                    # V = toggle both IN and OUT
                    both_on = self._verbose_in and self._verbose_out
                    self._verbose_in = not both_on
                    self._verbose_out = not both_on
                    status = "ON" if self._verbose_in else "OFF"
                    print(f"[LOG] Verbose logging: {status}")
                    continue
                elif event.key == pygame.K_i:
                    # I = toggle incoming only
                    self._verbose_in = not self._verbose_in
                    status = "ON" if self._verbose_in else "OFF"
                    print(f"[LOG] Incoming message logging: {status}")
                    continue
                elif event.key == pygame.K_o:
                    # O = toggle outgoing only
                    self._verbose_out = not self._verbose_out
                    status = "ON" if self._verbose_out else "OFF"
                    print(f"[LOG] Outgoing commands logging: {status}")
                    continue
                elif event.key == pygame.K_t:
                    # T = toggle state change logging
                    self._store.verbose = not self._store.verbose
                    status = "ON" if self._store.verbose else "OFF"
                    print(f"[LOG] State change logging: {status}")
                    continue
                elif event.key == pygame.K_s:
                    # S = print statistics
                    self._print_stats()
                    continue
                elif event.key == pygame.K_1:
                    # 1 = toggle AVC-LAN source filter
                    self._verbose_avc = not self._verbose_avc
                    status = "ON" if self._verbose_avc else "OFF"
                    print(f"[LOG] AVC-LAN messages: {status}")
                    self._print_source_filter_status()
                    continue
                elif event.key == pygame.K_2:
                    # 2 = toggle CAN source filter
                    self._verbose_can = not self._verbose_can
                    status = "ON" if self._verbose_can else "OFF"
                    print(f"[LOG] CAN messages: {status}")
                    self._print_source_filter_status()
                    continue
                elif event.key == pygame.K_3:
                    # 3 = toggle RS485/Satellite source filter
                    self._verbose_sat = not self._verbose_sat
                    status = "ON" if self._verbose_sat else "OFF"
                    print(f"[LOG] RS485/Satellite messages: {status}")
                    self._print_source_filter_status()
                    continue
                elif event.key == pygame.K_0:
                    # 0 = toggle all sources
                    all_on = self._verbose_avc and self._verbose_can and self._verbose_sat
                    self._verbose_avc = not all_on
                    self._verbose_can = not all_on
                    self._verbose_sat = not all_on
                    status = "ON" if self._verbose_avc else "OFF"
                    print(f"[LOG] All message sources: {status}")
                    self._print_source_filter_status()
                    continue
                elif event.key == pygame.K_a:
                    # A = toggle analysis mode for reverse engineering
                    self._analysis_mode = not self._analysis_mode
                    status = "ON" if self._analysis_mode else "OFF"
                    print(f"[DEV] Analysis mode: {status}")
                    if self._analysis_mode:
                        print("      Logging: Button presses, Touch events, Energy packets (A00->258)")
                        print("      Press A again to disable")
                    if self._gateway_adapter:
                        self._gateway_adapter.set_analysis_mode(self._analysis_mode)
                    continue
            
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
        
        # Render playback overlay in dev mode
        if self.config.dev_mode and self._input_source:
            self._render_playback_overlay(surface)
        
        # Present the frame (handles scaling)
        self.renderer.present()
    
    def _render_fps(self, surface: pygame.Surface) -> None:
        """Render FPS counter."""
        fps = self.clock.get_fps()
        font = pygame.font.Font(None, 16)
        fps_text = font.render(f"FPS: {fps:.1f}", True, COLORS["text_secondary"])
        surface.blit(fps_text, (5, 5))
    
    def _render_playback_overlay(self, surface: pygame.Surface) -> None:
        """Render playback time overlay (dev mode only)."""
        if not self._input_source:
            return
        
        from ..comm.file_input import FileInput, PlaybackState
        if not isinstance(self._input_source, FileInput):
            return
        
        # Get timing info
        current_time = self._input_source.current_playback_time
        total_duration = self._input_source.total_duration
        state = self._input_source.state
        
        # Format times as MM:SS
        def format_time(seconds: float) -> str:
            mins = int(seconds) // 60
            secs = int(seconds) % 60
            return f"{mins:02d}:{secs:02d}"
        
        current_str = format_time(current_time)
        total_str = format_time(total_duration)
        
        # State icon
        state_icons = {
            PlaybackState.STOPPED: "[]",
            PlaybackState.PLAYING: ">",
            PlaybackState.PAUSED: "||"
        }
        icon = state_icons.get(state, "?")
        
        # Render overlay text
        font = pygame.font.Font(None, 16)
        text = f"{icon} {current_str}/{total_str}"
        text_surface = font.render(text, True, COLORS["text_secondary"])
        
        # Position in top-right corner
        x = surface.get_width() - text_surface.get_width() - 5
        y = 5
        surface.blit(text_surface, (x, y))
    
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
