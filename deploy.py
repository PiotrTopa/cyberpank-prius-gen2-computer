#!/usr/bin/env python3
"""
Deployment script for CyberPunk Computer to Raspberry Pi Zero 2W.

This script automates the deployment process:
- Syncs code to the target device
- Installs/updates Python dependencies
- Configures OS-level requirements (SDL2, framebuffer permissions)
- Configures the systemd service
- Manages the application lifecycle

Configuration:
    Create a .env file with:
        DEPLOY_HOST=prius
        DEPLOY_USER=piotr

Usage:
    python deploy.py [options]

Options:
    --host HOST        SSH host (default: from .env or 'prius')
    --user USER        SSH user (default: from .env or 'piotr')
    --setup-os         Install SDL2 packages and configure framebuffer
    --install          Install systemd service and enable auto-start
    --restart          Restart the service after deployment
    --logs             Show service logs after deployment
    --dry-run          Show what would be done without executing

Examples:
    # First-time setup on fresh RPI:
    python deploy.py --setup-os --install --restart

    # Regular code update:
    python deploy.py --restart

    # Just sync code (no restart):
    python deploy.py
"""

import argparse
import subprocess
import sys
import os
from pathlib import Path
from typing import List, Optional, Dict


class Colors:
    """ANSI color codes for terminal output."""
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


class DeploymentError(Exception):
    """Raised when deployment fails."""
    pass


def load_env_file(filepath: Path) -> Dict[str, str]:
    """Load environment variables from a .env file."""
    env_vars = {}
    if not filepath.exists():
        return env_vars
    
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            parts = line.split('=', 1)
            if len(parts) == 2:
                key, value = parts
                env_vars[key.strip()] = value.strip()
    return env_vars


class Deployer:
    """Handles deployment to Raspberry Pi."""
    
    def __init__(self, host: str, user: str, dry_run: bool = False):
        self.host = host
        self.user = user
        self.dry_run = dry_run
        self.ssh_target = f"{user}@{host}"
        
        # Paths
        self.local_root = Path(__file__).parent.absolute()
        self.remote_root = f"/home/{user}/cyberpunk_computer"
        self.service_name = "cyberpunk-computer"
    
    def log(self, message: str, color: str = Colors.OKBLUE) -> None:
        """Print colored log message."""
        print(f"{color}{message}{Colors.ENDC}")
    
    def success(self, message: str) -> None:
        """Print success message."""
        self.log(f"✓ {message}", Colors.OKGREEN)
    
    def warning(self, message: str) -> None:
        """Print warning message."""
        self.log(f"⚠ {message}", Colors.WARNING)
    
    def error(self, message: str) -> None:
        """Print error message."""
        self.log(f"✗ {message}", Colors.FAIL)
    
    def header(self, message: str) -> None:
        """Print section header."""
        print()
        self.log(f"{'='*60}", Colors.HEADER)
        self.log(message, Colors.HEADER + Colors.BOLD)
        self.log(f"{'='*60}", Colors.HEADER)
    
    def run_local(self, cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run command locally."""
        cmd_str = ' '.join(cmd)
        self.log(f"[LOCAL] {cmd_str}", Colors.OKCYAN)
        
        if self.dry_run:
            self.warning("DRY RUN - command not executed")
            return subprocess.CompletedProcess(cmd, 0)
        
        return subprocess.run(cmd, check=check, capture_output=True, text=True)
    
    def run_remote(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run command on remote host via SSH."""
        self.log(f"[REMOTE] {cmd}", Colors.OKCYAN)
        
        if self.dry_run:
            self.warning("DRY RUN - command not executed")
            return subprocess.CompletedProcess(['ssh'], 0)
        
        ssh_cmd = ['ssh', self.ssh_target, cmd]
        return subprocess.run(ssh_cmd, check=check, capture_output=True, text=True)
    
    def check_connection(self) -> bool:
        """Check SSH connection to target."""
        self.header("Checking SSH Connection")
        
        try:
            result = self.run_remote('echo "Connection OK"', check=True)
            if result.returncode == 0:
                self.success(f"Connected to {self.ssh_target}")
                return True
        except subprocess.CalledProcessError:
            self.error(f"Cannot connect to {self.ssh_target}")
            self.warning("Make sure the device is accessible and SSH keys are configured")
            return False
    
    def sync_code(self) -> bool:
        """Sync code to remote host using rsync or scp fallback."""
        self.header("Syncing Code")
        
        # Check if rsync is available
        has_rsync = False
        try:
            subprocess.run(['rsync', '--version'], capture_output=True, check=True)
            has_rsync = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.warning("rsync not found, falling back to scp")
        
        if has_rsync:
            return self._sync_with_rsync()
        else:
            return self._sync_with_scp()
    
    def _sync_with_rsync(self) -> bool:
        """Sync using rsync (preferred, more efficient)."""
        # Exclude patterns
        excludes = [
            '__pycache__',
            '*.pyc',
            '.git',
            '.vscode',
            'assets/data/*.ndjson',  # Don't sync large replay files
            '*.log',
            '.pytest_cache',
            '.env',  # Don't sync local configuration
        ]
        
        exclude_args = []
        for pattern in excludes:
            exclude_args.extend(['--exclude', pattern])
        
        rsync_cmd = [
            'rsync',
            '-avz',
            '--delete',
            *exclude_args,
            f'{self.local_root}/',
            f'{self.ssh_target}:{self.remote_root}/'
        ]
        
        try:
            result = self.run_local(rsync_cmd, check=True)
            self.success("Code synchronized successfully (rsync)")
            return True
        except subprocess.CalledProcessError as e:
            self.error("Code synchronization failed")
            if not self.dry_run:
                print(e.stderr)
            return False
    
    def _sync_with_scp(self) -> bool:
        """Sync using scp (fallback for Windows without rsync)."""
        # Directories to sync
        dirs_to_sync = ['cyberpunk_computer', 'deployment', 'vfd_satellite', 'assets/fonts']
        files_to_sync = ['requirements.txt', 'README.md']
        
        try:
            # Create remote directory if needed
            self.run_remote(f'mkdir -p {self.remote_root}', check=True)
            
            # Fix permissions on existing files (in case they were created by root)
            self.log("Fixing remote permissions...")
            self.run_remote(
                f'sudo chown -R {self.user}:{self.user} {self.remote_root} 2>/dev/null || true',
                check=False
            )
            
            # Clean up __pycache__ directories before sync to avoid conflicts
            self.log("Cleaning up __pycache__ directories...")
            self.run_remote(
                f'find {self.remote_root} -type d -name "__pycache__" -exec rm -rf {{}} + 2>/dev/null || true',
                check=False
            )
            
            # Sync directories
            for dir_name in dirs_to_sync:
                local_path = self.local_root / dir_name
                if local_path.exists():
                    self.log(f"Syncing {dir_name}/...")
                    # Ensure parent dir exists
                    parent = str(Path(dir_name).parent)
                    if parent != '.':
                        self.run_remote(f'mkdir -p {self.remote_root}/{parent}', check=True)
                    
                    # Remove destination first to ensure clean copy
                    self.run_remote(f'rm -rf {self.remote_root}/{dir_name}', check=False)
                    
                    scp_cmd = [
                        'scp', '-r',
                        str(local_path),
                        f'{self.ssh_target}:{self.remote_root}/{parent}/' if parent != '.' else f'{self.ssh_target}:{self.remote_root}/'
                    ]
                    self.run_local(scp_cmd, check=True)
            
            # Sync individual files
            for file_name in files_to_sync:
                local_path = self.local_root / file_name
                if local_path.exists():
                    scp_cmd = [
                        'scp',
                        str(local_path),
                        f'{self.ssh_target}:{self.remote_root}/'
                    ]
                    self.run_local(scp_cmd, check=True)
            
            self.success("Code synchronized successfully (scp)")
            return True
            
        except subprocess.CalledProcessError as e:
            self.error("Code synchronization failed")
            if not self.dry_run and e.stderr:
                print(e.stderr)
            return False
    
    def install_dependencies(self) -> bool:
        """Install Python dependencies on remote host."""
        self.header("Installing Dependencies")
        
        commands = [
            # Create virtual environment if it doesn't exist
            f'cd {self.remote_root} && python3 -m venv venv || true',
            
            # Install/upgrade pip
            f'cd {self.remote_root} && ./venv/bin/pip install --upgrade pip',
            
            # Install requirements
            f'cd {self.remote_root} && ./venv/bin/pip install -r requirements.txt',
        ]
        
        for cmd in commands:
            try:
                self.run_remote(cmd, check=True)
            except subprocess.CalledProcessError as e:
                self.error(f"Dependency installation failed")
                if not self.dry_run:
                    print(e.stderr)
                return False
        
        self.success("Dependencies installed successfully")
        return True
    
    def install_service(self) -> bool:
        """Install and enable systemd service using the deployment files."""
        self.header("Installing Systemd Service")
        
        # Use the pre-configured service file from deployment directory
        local_service = self.local_root / 'deployment' / 'cyberpunk-computer.service'
        remote_service = f'/etc/systemd/system/{self.service_name}.service'
        
        # Also copy the wrapper script
        local_wrapper = self.local_root / 'deployment' / 'run-on-console.sh'
        remote_wrapper = f'{self.remote_root}/deployment/run-on-console.sh'
        
        commands = [
            # Copy service file (requires sudo)
            f"sudo cp {self.remote_root}/deployment/cyberpunk-computer.service {remote_service}",
            
            # Set permissions on service file
            f"sudo chmod 644 {remote_service}",
            
            # Ensure wrapper script is executable
            f"chmod +x {remote_wrapper}",
            
            # Reload systemd
            "sudo systemctl daemon-reload",
            
            # Enable service
            f"sudo systemctl enable {self.service_name}",
        ]
        
        try:
            for cmd in commands:
                self.run_remote(cmd, check=True)
            
            self.success(f"Service '{self.service_name}' installed and enabled")
            return True
            
        except subprocess.CalledProcessError as e:
            self.error("Service installation failed")
            if not self.dry_run:
                print(e.stderr)
            return False
    
    def restart_service(self) -> bool:
        """Restart the systemd service."""
        self.header("Restarting Service")
        
        try:
            self.run_remote(f"sudo systemctl restart {self.service_name}", check=True)
            self.success(f"Service '{self.service_name}' restarted")
            return True
        except subprocess.CalledProcessError as e:
            self.error("Service restart failed")
            if not self.dry_run:
                print(e.stderr)
            return False
    
    def show_logs(self) -> bool:
        """Show service logs."""
        self.header("Service Logs")
        
        try:
            result = self.run_remote(
                f"sudo journalctl -u {self.service_name} -n 50 --no-pager",
                check=True
            )
            if not self.dry_run:
                print(result.stdout)
            return True
        except subprocess.CalledProcessError as e:
            self.error("Failed to retrieve logs")
            if not self.dry_run:
                print(e.stderr)
            return False
    
    def create_log_directory(self) -> bool:
        """Create log directory on remote host."""
        self.header("Creating Log Directory")
        
        commands = [
            "sudo mkdir -p /var/log/cyberpunk_computer",
            f"sudo chown {self.user}:{self.user} /var/log/cyberpunk_computer",
            "sudo chmod 755 /var/log/cyberpunk_computer",
        ]
        
        try:
            for cmd in commands:
                self.run_remote(cmd, check=True)
            self.success("Log directory created")
            return True
        except subprocess.CalledProcessError as e:
            self.error("Failed to create log directory")
            if not self.dry_run:
                print(e.stderr)
            return False
    
    def setup_os(self) -> bool:
        """
        Configure OS-level requirements for the application.
        
        This includes:
        - Installing required system packages (SDL2)
        - Setting up framebuffer permissions
        - Adding user to required groups
        - Installing udev rules
        """
        self.header("Setting up OS-level Requirements")
        
        # Step 1: Install required system packages
        self.log("Installing system packages...")
        packages = [
            "libsdl2-dev",
            "libsdl2-image-dev", 
            "libsdl2-mixer-dev",
            "libsdl2-ttf-dev",
        ]
        
        try:
            # Check if packages are already installed
            check_cmd = f"dpkg -l {' '.join(packages)} 2>/dev/null | grep -c '^ii' || echo 0"
            result = self.run_remote(check_cmd, check=False)
            
            if not self.dry_run:
                installed_count = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
                if installed_count < len(packages):
                    self.log("Installing SDL2 packages (this may take a while)...")
                    self.run_remote(
                        f"sudo apt-get update && sudo apt-get install -y {' '.join(packages)}",
                        check=True
                    )
                    self.success("SDL2 packages installed")
                else:
                    self.success("SDL2 packages already installed")
            
        except subprocess.CalledProcessError as e:
            self.warning(f"Failed to install some packages: {e}")
            # Continue anyway - they might already be installed
        
        # Step 2: Set up user groups
        self.log("Setting up user groups...")
        try:
            self.run_remote(f"sudo usermod -a -G video,tty {self.user}", check=True)
            self.success(f"User {self.user} added to video and tty groups")
        except subprocess.CalledProcessError:
            self.warning("Failed to add user to groups (may already be member)")
        
        # Step 3: Install udev rules for framebuffer
        self.log("Installing udev rules...")
        try:
            udev_rule = 'KERNEL=="fb0", MODE="0666"'
            self.run_remote(
                f"echo '{udev_rule}' | sudo tee /etc/udev/rules.d/99-framebuffer.rules > /dev/null",
                check=True
            )
            self.run_remote("sudo udevadm control --reload-rules", check=True)
            self.run_remote("sudo udevadm trigger", check=True)
            self.success("Udev rules installed")
        except subprocess.CalledProcessError as e:
            self.warning(f"Failed to install udev rules: {e}")
        
        # Step 4: Set framebuffer permissions directly
        self.log("Setting framebuffer permissions...")
        try:
            self.run_remote("sudo chmod 666 /dev/fb0", check=False)
            self.success("Framebuffer permissions set")
        except subprocess.CalledProcessError:
            self.warning("Could not set framebuffer permissions (device may not exist)")
        
        # Step 5: Verify framebuffer
        self.log("Verifying framebuffer...")
        try:
            result = self.run_remote("cat /sys/class/graphics/fb0/virtual_size 2>/dev/null || echo 'not found'", check=False)
            if not self.dry_run and result.stdout.strip() != 'not found':
                self.success(f"Framebuffer detected: {result.stdout.strip()}")
            else:
                self.warning("Framebuffer not detected (VGA666 may not be configured)")
        except subprocess.CalledProcessError:
            self.warning("Could not check framebuffer status")
        
        self.success("OS setup complete")
        return True

    def deploy(self, install_service: bool = False, restart: bool = False, 
               show_logs: bool = False, setup_os: bool = False) -> bool:
        """
        Execute full deployment.
        
        Args:
            install_service: Install and enable systemd service
            restart: Restart service after deployment
            show_logs: Show logs after deployment
            setup_os: Run OS-level setup (packages, permissions, etc.)
            
        Returns:
            True if deployment successful
        """
        if not self.check_connection():
            return False
        
        if setup_os:
            if not self.setup_os():
                self.warning("OS setup had some issues, continuing anyway...")
        
        if not self.sync_code():
            return False
        
        if not self.install_dependencies():
            return False
        
        if install_service:
            if not self.create_log_directory():
                return False
            if not self.install_service():
                return False
        
        if restart:
            if not self.restart_service():
                return False
        
        if show_logs:
            self.show_logs()
        
        self.header("Deployment Complete")
        self.success("All steps completed successfully!")
        
        if not restart and not install_service:
            self.warning("Note: Service was not restarted. Changes will take effect on next restart.")
            self.log(f"To restart manually: ssh {self.ssh_target} 'sudo systemctl restart {self.service_name}'")
        
        return True


def main() -> int:
    """Main entry point."""
    # Load .env file
    env_path = Path(__file__).parent / '.env'
    env_vars = load_env_file(env_path)
    
    default_host = env_vars.get('DEPLOY_HOST', 'prius')
    default_user = env_vars.get('DEPLOY_USER', 'piotr')
    
    parser = argparse.ArgumentParser(
        description="Deploy CyberPunk Computer to Raspberry Pi Zero 2W",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python deploy.py                      # Sync code only
  python deploy.py --restart            # Sync and restart service
  python deploy.py --setup-os --install # Fresh install with OS setup
  python deploy.py --install --restart  # Install service and restart
  python deploy.py --logs               # Sync and show logs
"""
    )
    parser.add_argument(
        '--host',
        default=default_host,
        help=f'SSH host (default: {default_host})'
    )
    parser.add_argument(
        '--user',
        default=default_user,
        help=f'SSH user (default: {default_user})'
    )
    parser.add_argument(
        '--setup-os',
        action='store_true',
        help='Run OS-level setup (install SDL2, configure framebuffer permissions)'
    )
    parser.add_argument(
        '--install',
        action='store_true',
        help='Install systemd service and enable auto-start'
    )
    parser.add_argument(
        '--restart',
        action='store_true',
        help='Restart service after deployment'
    )
    parser.add_argument(
        '--logs',
        action='store_true',
        help='Show service logs after deployment'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without executing'
    )
    
    args = parser.parse_args()
    
    print(f"Deploying to {args.user}@{args.host}...")
    
    deployer = Deployer(args.host, args.user, args.dry_run)
    
    try:
        success = deployer.deploy(
            install_service=args.install,
            restart=args.restart,
            show_logs=args.logs,
            setup_os=args.setup_os
        )
        return 0 if success else 1
        
    except KeyboardInterrupt:
        deployer.warning("\nDeployment interrupted by user")
        return 130
    except Exception as e:
        deployer.error(f"Deployment failed: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
