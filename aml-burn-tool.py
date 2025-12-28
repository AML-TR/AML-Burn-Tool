#!/usr/bin/env python3
"""
Amlogic SBC Burn Tool
Event-driven FSM for automated image flashing via serial port and adnl_burn_pkg
"""

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

try:
  import serial
  import serial.tools.list_ports
except ImportError:
  print("ERROR: pyserial not installed. Run: pip install pyserial")
  sys.exit(1)

try:
  import requests
except ImportError:
  print("ERROR: requests not installed. Run: pip install requests")
  sys.exit(1)


class State(Enum):
  """FSM States"""
  INIT = "INIT"
  BOOTROM = "BOOTROM"
  BL2 = "BL2"
  UBOOT = "UBOOT"
  DOWNLOAD = "DOWNLOAD"
  LINUX = "LINUX"
  LOGIN = "LOGIN"
  BOOT_VERIFY = "BOOT_VERIFY"  # Verifying successful boot
  BOARD_INFO = "BOARD_INFO"  # Collecting board information
  COMPLETE = "COMPLETE"
  ERROR = "ERROR"


class BurnTool:
  """Main burn tool class with event-driven FSM"""

  # ANSI color codes for log prefixes (only if stdout is a TTY)
  @staticmethod
  def _get_colors():
    """Get color codes if stdout is a TTY, otherwise empty strings"""
    if sys.stdout.isatty():
      return {
        "RESET": "\033[0m",
        "SERIAL": "\033[36m",  # Cyan
        "PATTERN": "\033[33m",  # Yellow
        "FSM": "\033[32m",  # Green
        "ADNL": "\033[35m",  # Magenta
      }
    else:
      return {
        "RESET": "",
        "SERIAL": "",
        "PATTERN": "",
        "FSM": "",
        "ADNL": "",
      }
  
  @property
  def COLORS(self):
    """Get color codes (property to check TTY each time)"""
    return self._get_colors()

  # Pattern definitions for state detection
  PATTERNS = {
    "autoboot": re.compile(r"Hit any key to stop autoboot", re.IGNORECASE),
    # U-Boot prompts: s4_polaris#, a4_mainstream#, =>, U-Boot>, etc. (NOT root@)
    "uboot_prompt": re.compile(
      r"(s4_polaris#|a4_mainstream#|a4_ba400#|=>|U-Boot>)\s*$", re.MULTILINE
    ),
    "login_prompt": re.compile(r"login:\s*$", re.MULTILINE),
    # Shell prompts: root@hostname:~# (Linux shell, NOT U-Boot)
    "shell_prompt": re.compile(r"root@.*?:\~#\s*$", re.MULTILINE),
    "uboot_version": re.compile(r"U-Boot\s+\d+\.\d+", re.IGNORECASE),
    "bl2": re.compile(r"BL2[EX]?\s+.*Built", re.IGNORECASE),
    "bl31": re.compile(r"BL31\s+.*Built|NOTICE:\s+BL31", re.IGNORECASE),
    "bl32": re.compile(r"BL3[23]|BL3-2", re.IGNORECASE),
    "bootrom": re.compile(r"chip_family_id|ops_bining", re.IGNORECASE),
    "usb_reset": re.compile(r"USB RESET", re.IGNORECASE),
    "rebooting": re.compile(r"Rebooting\.|Restarting system", re.IGNORECASE),
  }

  def __init__(
    self,
    serial_port: str,
    baudrate: int,
    image_path: str,
    relay_ip: Optional[str] = None,
  ):
    self.serial_port = serial_port
    self.baudrate = baudrate
    self.image_path = Path(image_path)
    self.relay_ip = relay_ip

    self.state = State.INIT
    self.serial_conn: Optional[serial.Serial] = None
    self.adnl_process: Optional[subprocess.Popen] = None

    # State tracking flags
    self.adnl_sent = False
    self.login_sent = False
    self.reboot_sent = False

    # Log files - create timestamped directory
    self.log_dir = Path("logs")
    self.log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    self.session_log_dir = self.log_dir / timestamp
    self.session_log_dir.mkdir(exist_ok=True)
    self.serial_log = self.session_log_dir / f"serial_{timestamp}.log"
    self.adnl_log = self.session_log_dir / f"adnl_{timestamp}.log"
    self.script_log = self.session_log_dir / f"script_{timestamp}.log"
    self.board_info_md = self.session_log_dir / "board-info.md"

    # Setup logging
    self.setup_logging()

    # FSM state tracking
    self.state_history: list[tuple[float, State, str]] = []
    self.last_activity = time.time()
    self.timeout_seconds = 300  # 5 minutes timeout
    self.serial_reader_started = False
    self.lines_received = 0
    self.initial_wake_sent = False
    self.first_data_timeout = 30  # 30 seconds timeout for first data
    self.last_line_time = None  # Timestamp of last received line
    self.boot_verify_sent = False  # Track if uname -a sent
    # Board info collection
    self.board_info_uname_received = False  # Track if uname -a output received
    self.board_info_collection_queue = []  # Queue of commands to execute
    self.board_info_current_command = None  # Current command being executed
    self.board_info_output = {}  # Dictionary to store collected information
    self.board_info_collecting = False  # Flag to track if we're collecting output
    self.board_info_output_buffer = []  # Buffer for current command output
    self.board_info_initialized = False  # Track if collection queue initialized
    self.boot_verify_timeout = 120  # 2 minutes to verify boot after burn
    self.no_lines_warning_count = 0  # Count consecutive "no new lines" warnings
    self.burn_complete_time = None  # Timestamp when burn completed
    self.continuous_enter_task = None  # Task for continuous Enter sending
    self.stop_enter_sending = False  # Flag to stop Enter sending
    self.uboot_prompt_seen_after_reboot = False  # Track if U-Boot prompt seen after reboot

  def setup_logging(self):
    """Setup logging to script log file"""
    # Remove all existing handlers
    root_logger = logging.getLogger()
    root_logger.handlers = []
    
    # Create a custom formatter that strips ANSI codes for file output
    class NoColorFormatter(logging.Formatter):
      """Formatter that removes ANSI color codes"""
      def format(self, record):
        # Get the formatted message
        msg = super().format(record)
        # Remove ANSI escape sequences: \033[XXm, \x1b[XXm, etc.
        msg = re.sub(r'\033\[[0-9;]*m', '', msg)
        msg = re.sub(r'\x1b\[[0-9;]*m', '', msg)
        return msg
    
    # Plain formatter for console (keeps colors)
    console_formatter = logging.Formatter(
      "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
      datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # No-color formatter for file
    file_formatter = NoColorFormatter(
      "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
      datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # File handler (no colors)
    file_handler = logging.FileHandler(self.script_log)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(file_formatter)
    
    # Console handler (keeps colors)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    
    # Setup root logger
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    self.logger = logging.getLogger(__name__)

  def log_line(self, log_file: Path, line: str):
    """Log a line with millisecond timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with open(log_file, "a", encoding="utf-8") as f:
      f.write(f"[{timestamp}] {line}\n")

  def check_serial_port(self) -> tuple[bool, str]:
    """Check if serial port exists and is available"""
    if not os.path.exists(self.serial_port):
      return False, f"Serial port {self.serial_port} does not exist"

    # Check if port is in use
    try:
      # Try to open in exclusive mode
      test_ser = serial.Serial(self.serial_port, self.baudrate, timeout=0.1)
      test_ser.close()
    except serial.SerialException as e:
      if "Permission denied" in str(e) or "could not open port" in str(e).lower():
        # Check for processes using the port
        try:
          result = subprocess.run(
            ["lsof", self.serial_port],
            capture_output=True,
            text=True,
            timeout=2,
          )
          if result.returncode == 0 and result.stdout:
            processes = result.stdout.strip().split("\n")[1:]  # Skip header
            proc_info = []
            for proc in processes:
              if proc.strip():
                parts = proc.split()
                if len(parts) > 1:
                  proc_info.append(f"PID {parts[1]}")
            if proc_info:
              return (
                False,
                f"{self.serial_port} is in use by: {', '.join(proc_info)}. Please close other processes (minicom, screen, etc.)",
              )
        except (subprocess.TimeoutExpired, FileNotFoundError):
          pass
        return False, f"{self.serial_port} is in use or permission denied: {e}"
      return False, f"Cannot open {self.serial_port}: {e}"

    # Check for minicom/screen processes
    try:
      result = subprocess.run(
        ["pgrep", "-f", "minicom|screen.*" + os.path.basename(self.serial_port)],
        capture_output=True,
        text=True,
        timeout=2,
      )
      if result.returncode == 0 and result.stdout.strip():
        pids = result.stdout.strip().split("\n")
        return (
          False,
          f"Found minicom/screen processes using serial port: PIDs {', '.join(pids)}. Please close them first.",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
      pass

    return True, "OK"

  def check_relay(self) -> tuple[bool, str, Optional[str]]:
    """Check relay status and return (success, message, current_status)"""
    if not self.relay_ip:
      return True, "No relay configured", None

    try:
      url = f"http://{self.relay_ip}/cm?cmnd=Power"
      response = requests.get(url, timeout=5)
      response.raise_for_status()
      data = response.json()
      status = data.get("POWER", "UNKNOWN")
      return True, f"Relay status: {status}", status
    except requests.exceptions.RequestException as e:
      return False, f"Cannot connect to relay at {self.relay_ip}: {e}", None

  def relay_power_off(self) -> bool:
    """Turn relay power OFF"""
    if not self.relay_ip:
      return False
    try:
      url = f"http://{self.relay_ip}/cm?cmnd=Power%20OFF"
      response = requests.get(url, timeout=5)
      response.raise_for_status()
      self.logger.info(f"Relay power OFF sent to {self.relay_ip}")
      return True
    except requests.exceptions.RequestException as e:
      self.logger.error(f"Failed to turn relay OFF: {e}")
      return False

  def relay_power_on(self) -> bool:
    """Turn relay power ON"""
    if not self.relay_ip:
      return False
    try:
      url = f"http://{self.relay_ip}/cm?cmnd=Power%20ON"
      response = requests.get(url, timeout=5)
      response.raise_for_status()
      self.logger.info(f"Relay power ON sent to {self.relay_ip}")
      return True
    except requests.exceptions.RequestException as e:
      self.logger.error(f"Failed to turn relay ON: {e}")
      return False

  def relay_power_cycle(self, off_delay: float = 5.0):
    """Power cycle via relay: OFF -> wait (with countdown) -> ON (no wait)"""
    if not self.relay_ip:
      self.logger.warning("No relay configured, skipping power cycle")
      return

    self.logger.info("Power cycling board via relay...")
    self.relay_power_off()
    
    # Wait for capacitors to discharge (minimum 5 seconds)
    # Show countdown to user
    self.logger.info(f"Waiting {off_delay:.0f} seconds for capacitors to discharge...")
    for remaining in range(int(off_delay), 0, -1):
      self.logger.info(f"  {remaining}...")
      time.sleep(1.0)
    
    self.relay_power_on()
    self.logger.info("Power ON sent, starting Enter sending immediately...")

  def send_serial_command(self, command: str, delay: float = 0.002):
    """Send command character by character with delay"""
    if not self.serial_conn or not self.serial_conn.is_open:
      return

    # Send Ctrl+C first to clear any running command
    self.serial_conn.write(b"\x03")
    time.sleep(delay)

    # Send command character by character
    for char in command:
      self.serial_conn.write(char.encode())
      time.sleep(delay)

    # Send carriage return
    self.serial_conn.write(b"\r")
    self.logger.debug(f"Sent command: {command}")
  
  def send_ctrl_c_enter(self):
    """Send Ctrl+C followed by Enter to wake up board from adnl mode"""
    if not self.serial_conn or not self.serial_conn.is_open:
      return
    self.serial_conn.write(b"\x03")  # Ctrl+C
    time.sleep(0.1)
    self.serial_conn.write(b"\r")  # Enter
    time.sleep(0.1)

  async def send_continuous_enter(self):
    """Send Enter continuously (every 1ms) to catch autoboot"""
    self.logger.info("Starting continuous Enter sending (1ms interval)")
    enter_count = 0
    
    # Send first Enter immediately (before async sleep) to catch autoboot as fast as possible
    if self.serial_conn and self.serial_conn.is_open:
      try:
        self.serial_conn.write(b"\r")
        enter_count += 1
      except Exception as e:
        self.logger.error(f"Error sending first Enter: {e}")
        return
    
    while not self.stop_enter_sending and self.serial_conn and self.serial_conn.is_open:
      try:
        await asyncio.sleep(0.001)  # 1ms = 0.001 seconds
        if self.stop_enter_sending:
          break
        self.serial_conn.write(b"\r")
        enter_count += 1
        if enter_count % 100 == 0:  # Log every 100 Enter
          self.logger.debug(f"Sent {enter_count} Enter commands")
      except Exception as e:
        self.logger.error(f"Error sending continuous Enter: {e}")
        break
    
    self.logger.info(f"Stopped continuous Enter sending (total: {enter_count} Enter commands)")

  def change_state(self, new_state: State, reason: str = ""):
    """Change FSM state and log transition"""
    if self.state != new_state:
      timestamp = time.time()
      self.state_history.append((timestamp, self.state, new_state))
      elapsed = timestamp - self.last_activity if self.last_activity else 0
      color = self.COLORS["FSM"]
      reset = self.COLORS["RESET"]
      self.logger.info(
        f"{color}[FSM]{reset} State transition: {self.state.value} -> {new_state.value} "
        f"({reason}) [elapsed: {elapsed:.1f}s, lines: {self.lines_received}]"
      )
      self.state = new_state
      self.last_activity = timestamp

      # Reset flags when going back to INIT
      if new_state == State.INIT:
        self.reboot_sent = False

  def match_pattern(self, line: str) -> Optional[str]:
    """Match line against known patterns, return pattern name"""
    for pattern_name, pattern in self.PATTERNS.items():
      if pattern.search(line):
        return pattern_name
    return None

  async def read_serial_async(self):
    """Async serial port reader"""
    if not self.serial_conn:
      self.logger.error("Serial connection not available for reading")
      return

    self.logger.info("Serial reader task started")
    self.serial_reader_started = True
    buffer = b""
    last_data_time = time.time()
    first_data_received = False
    serial_start_time = time.time()
    last_buffer_process_time = time.time()
    buffer_process_interval = 0.5  # Process buffer every 0.5 seconds even without newline

    while self.serial_conn.is_open:
      try:
        # Read available data
        if self.serial_conn.in_waiting > 0:
          data = self.serial_conn.read(self.serial_conn.in_waiting)
          buffer += data
          current_time = time.time()
          
          if not first_data_received:
            first_data_received = True
            elapsed = current_time - serial_start_time
            self.logger.info(
              f"First serial data received after {elapsed:.1f} seconds "
              f"({len(data)} bytes)"
            )

          last_data_time = current_time
          self.logger.debug(f"Read {len(data)} bytes from serial port")

          # Process complete lines
          while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            try:
              line = line_bytes.decode("utf-8", errors="replace").strip()
              if line:
                # Remove all ANSI escape codes (colors, cursor positions, queries, etc.)
                # This includes: \x1b[31m, \x1b[2;3H, \x1b[?25h, \x1b[230;1R, etc.
                line = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", line)
                # Also remove standalone escape sequences like ;230R (cursor position responses)
                line = re.sub(r";\d+R", "", line)
                # Remove any remaining control characters
                line = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", line)
                self.log_line(self.serial_log, line)
                self.lines_received += 1
                
                # Log all lines at INFO level so user can see what's happening
                self.last_line_time = time.time()
                # Reset warning counter when new line is received
                self.no_lines_warning_count = 0
                color = self.COLORS["SERIAL"]
                reset = self.COLORS["RESET"]
                self.logger.info(f"{color}[Serial #{self.lines_received}]{reset} {line}")
                
                await self.process_serial_line(line)
            except Exception as e:
              self.logger.error(f"Error processing line: {e}")

        # Process buffer even without newline if enough time has passed
        # This handles cases where prompt comes without newline
        current_time = time.time()
        if (
          buffer
          and current_time - last_buffer_process_time > buffer_process_interval
        ):
          try:
            # Try to decode buffer as a line (even without newline)
            line = buffer.decode("utf-8", errors="replace").strip()
            if line:
              # Remove all ANSI escape codes (colors, cursor positions, queries, etc.)
              # This includes: \x1b[31m, \x1b[2;3H, \x1b[?25h, \x1b[230;1R, etc.
              line = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", line)
              # Also remove standalone escape sequences like ;230R (cursor position responses)
              line = re.sub(r";\d+R", "", line)
              # Remove any remaining control characters
              line = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", line)
              self.log_line(self.serial_log, line)
              self.lines_received += 1
              
              self.last_line_time = time.time()
              # Reset warning counter when new line is received
              self.no_lines_warning_count = 0
              color = self.COLORS["SERIAL"]
              reset = self.COLORS["RESET"]
              self.logger.info(f"{color}[Serial #{self.lines_received}]{reset} {line}")
              
              await self.process_serial_line(line)
              # Clear buffer after processing
              buffer = b""
          except Exception as e:
            self.logger.debug(f"Error processing buffer without newline: {e}")
          
          last_buffer_process_time = current_time

        # Check for first data timeout (only if no data received yet)
        if not first_data_received:
          elapsed_no_data = time.time() - serial_start_time
          if elapsed_no_data > self.first_data_timeout:
            self.logger.error(
              f"No serial data received for {elapsed_no_data:.1f} seconds. "
              f"Please check board power or serial connections."
            )
            self.change_state(State.ERROR, "No serial data received")
            break

        await asyncio.sleep(0.01)  # Small delay to prevent CPU spinning

      except Exception as e:
        self.logger.error(f"Error reading serial: {e}")
        await asyncio.sleep(0.1)

    self.logger.info("Serial reader task ended")

  async def process_serial_line(self, line: str):
    """Process a line from serial port and update FSM"""
    self.last_activity = time.time()

    # Pattern matching
    pattern = self.match_pattern(line)
    if pattern:
      color = self.COLORS["PATTERN"]
      reset = self.COLORS["RESET"]
      self.logger.info(f"{color}[Pattern]{reset} Matched '{pattern}' in: {line[:100]}")

    # State machine logic
    if self.state == State.INIT:
      if pattern == "bootrom" or pattern == "bl2":
        self.change_state(State.BOOTROM, "BootROM/BL2 detected")
      elif pattern == "uboot_version":
        self.change_state(State.UBOOT, "U-Boot detected")
      elif pattern == "uboot_prompt":
        # Already at U-Boot prompt
        self.change_state(State.UBOOT, "U-Boot prompt detected")
      elif pattern == "login_prompt":
        # Board is at login prompt - send root to login
        if not self.login_sent:
          self.send_serial_command("root")
          self.logger.info("Sent 'root' for login (no password)")
          self.login_sent = True
          self.change_state(State.LOGIN, "Login sent")
      elif pattern == "shell_prompt":
        # Already booted to Linux - send reboot immediately and stay in INIT
        if not self.reboot_sent:
          self.send_serial_command("reboot -f")
          self.logger.info("Sent 'reboot -f' command to reboot board")
          self.reboot_sent = True
          # Reset flags for next cycle
          self.login_sent = False
          self.adnl_sent = False
          self.uboot_prompt_seen_after_reboot = False
          self.stop_enter_sending = False
          # Stay in INIT state (rebooting)
          self.logger.info("Board rebooting, waiting for bootloader stages...")
      # After reboot, detect bootloader stages and start continuous Enter
      if self.reboot_sent and (pattern == "bl2" or pattern == "bl31" or pattern == "bl32"):
        if not self.continuous_enter_task or self.continuous_enter_task.done():
          self.logger.info(
            f"Bootloader stage detected ({pattern}), starting continuous Enter to catch autoboot"
          )
          # Send first Enter IMMEDIATELY (synchronously) before starting async task
          # This is critical to catch autoboot which has 0 delay
          if self.serial_conn and self.serial_conn.is_open:
            try:
              self.serial_conn.write(b"\r")
              self.logger.debug("Sent immediate Enter to catch autoboot")
            except Exception as e:
              self.logger.error(f"Error sending immediate Enter: {e}")
          self.stop_enter_sending = False
          self.uboot_prompt_seen_after_reboot = False
          self.continuous_enter_task = asyncio.create_task(
            self.send_continuous_enter()
          )

    elif self.state == State.BOOTROM:
      if pattern == "uboot_version":
        self.change_state(State.UBOOT, "U-Boot detected")
      # After reboot, detect bootloader stages and start continuous Enter
      if self.reboot_sent and (pattern == "bl2" or pattern == "bl31" or pattern == "bl32"):
        if not self.continuous_enter_task or self.continuous_enter_task.done():
          self.logger.info(
            f"Bootloader stage detected ({pattern}), starting continuous Enter to catch autoboot"
          )
          # Send first Enter IMMEDIATELY (synchronously) before starting async task
          # This is critical to catch autoboot which has 0 delay
          if self.serial_conn and self.serial_conn.is_open:
            try:
              self.serial_conn.write(b"\r")
              self.logger.debug("Sent immediate Enter to catch autoboot")
            except Exception as e:
              self.logger.error(f"Error sending immediate Enter: {e}")
          self.stop_enter_sending = False
          self.uboot_prompt_seen_after_reboot = False
          self.continuous_enter_task = asyncio.create_task(
            self.send_continuous_enter()
          )

    elif self.state == State.UBOOT:
      if pattern == "autoboot":
        # Send Enter immediately to stop autoboot
        self.send_serial_command("")
        self.logger.info("Sent Enter to stop autoboot")
      elif pattern == "uboot_prompt":
        # U-Boot prompt detected
        if self.reboot_sent and not self.uboot_prompt_seen_after_reboot:
          # After reboot, stop continuous Enter sending
          self.stop_enter_sending = True
          self.uboot_prompt_seen_after_reboot = True
          self.logger.info(
            "U-Boot prompt detected after reboot, stopped continuous Enter sending"
          )
        
        if not self.adnl_sent:
          # We're at U-Boot prompt, send adnl command (only once)
          self.send_serial_command("adnl")
          self.logger.info("Sent 'adnl' command to enter download mode")
          self.adnl_sent = True
          self.change_state(State.DOWNLOAD, "Entered download mode")
      elif pattern == "login_prompt":
        # Booted to Linux, need to login and reboot
        # Send root login immediately
        if not self.login_sent:
          self.send_serial_command("root")
          self.logger.info("Sent 'root' for login (no password)")
          self.login_sent = True
          self.change_state(State.LOGIN, "Linux login detected, login sent")
        else:
          # Already sent login, just transition to LINUX state
          self.change_state(State.LINUX, "Linux login detected")
      elif pattern == "shell_prompt":
        # Board booted to Linux shell (autoboot wasn't caught)
        # Send reboot immediately to start over
        if not self.reboot_sent:
          self.send_serial_command("reboot -f")
          self.logger.info("Sent 'reboot -f' command to reboot board (from UBOOT state)")
          self.reboot_sent = True
          # Reset flags for next cycle
          self.login_sent = False
          self.adnl_sent = False
          self.uboot_prompt_seen_after_reboot = False
          self.stop_enter_sending = False
          self.change_state(State.INIT, "Rebooting to start over (autoboot was missed)")

    elif self.state == State.DOWNLOAD:
      # Wait for adnl_burn_pkg to complete
      if pattern == "usb_reset":
        self.logger.info("USB download mode active")
      elif pattern == "rebooting":
        # Board rebooting after burn - this is expected, continue monitoring
        self.logger.info("Board rebooting after burn, monitoring boot sequence...")

    elif self.state == State.BOOT_VERIFY:
      # After burn, monitor boot and verify successful boot
      # Don't send any commands during bootloader stages, just monitor
      if pattern == "login_prompt" and not self.login_sent:
        # Login prompt detected, send root
        self.send_serial_command("root")
        self.logger.info("Sent 'root' for login (no password)")
        self.login_sent = True
      elif pattern == "shell_prompt" and not self.boot_verify_sent:
        # Shell prompt detected, send uname -a to verify boot
        self.send_serial_command("uname -a")
        self.logger.info("Sent 'uname -a' to verify successful boot")
        self.boot_verify_sent = True
      
      # Check if line contains kernel version (uname -a output)
      # Look for Linux kernel version string
      if self.boot_verify_sent and not self.board_info_uname_received and (
        "Linux" in line
        and ("#1" in line or "SMP" in line or "PREEMPT" in line or "GNU/Linux" in line)
      ):
        # Kernel version detected, boot successful - now collect board info
        self.logger.info(f"Kernel version detected: {line[:100]}")
        self.board_info_uname_received = True
        self.logger.info("Boot verified successfully, calling collect_board_info.py")
        # Call external collect_board_info.py script
        self._call_collect_board_info_script()
        self.change_state(State.COMPLETE, "Boot verified and board information collected")

    elif self.state == State.LINUX:
      if pattern == "login_prompt" and not self.login_sent:
        # Send root login
        self.send_serial_command("root")
        self.logger.info("Sent 'root' for login")
        self.login_sent = True
        self.change_state(State.LOGIN, "Login sent")
      elif pattern == "shell_prompt":
        if not self.reboot_sent:
          # Logged in, send reboot to go back to U-Boot
          self.send_serial_command("reboot -f")
          self.logger.info("Sent 'reboot -f' command to reboot board")
          self.reboot_sent = True
          # Reset flags for next cycle
          self.login_sent = False
          self.adnl_sent = False
          self.uboot_prompt_seen_after_reboot = False
          self.stop_enter_sending = False
          self.change_state(State.INIT, "Rebooting to start over")
        else:
          # Already rebooted, just wait
          pass
      # Also check for shell_prompt even if we just transitioned to LINUX
      # This handles the case where we transitioned from INIT to LINUX in the same line
      if pattern == "shell_prompt" and not self.reboot_sent:
        # Send reboot immediately
        self.send_serial_command("reboot -f")
        self.logger.info("Sent 'reboot -f' command to reboot board (immediate)")
        self.reboot_sent = True
        # Reset flags for next cycle
        self.login_sent = False
        self.adnl_sent = False
        self.uboot_prompt_seen_after_reboot = False
        self.stop_enter_sending = False
        self.change_state(State.INIT, "Rebooting to start over")

    elif self.state == State.LOGIN:
      if pattern == "shell_prompt":
        if not self.reboot_sent:
          # Logged in successfully, send reboot
          self.send_serial_command("reboot -f")
          self.logger.info("Sent 'reboot -f' command to reboot board")
          self.reboot_sent = True
          self.login_sent = False
          self.adnl_sent = False
          self.uboot_prompt_seen_after_reboot = False
          self.stop_enter_sending = False
          self.change_state(State.INIT, "Rebooting to start over")
      # Also check for shell_prompt even if we just transitioned to LOGIN
      # This handles the case where we transitioned from INIT to LOGIN in the same line
      if pattern == "shell_prompt" and not self.reboot_sent:
        # Send reboot immediately
        self.send_serial_command("reboot -f")
        self.logger.info("Sent 'reboot -f' command to reboot board (immediate)")
        self.reboot_sent = True
        self.login_sent = False
        self.adnl_sent = False
        self.uboot_prompt_seen_after_reboot = False
        self.stop_enter_sending = False
        self.change_state(State.INIT, "Rebooting to start over")

  def _initialize_board_info_collection(self):
    """Initialize board info collection queue with all commands"""
    # Device tree command: try tree first, then find
    device_tree_cmd = "if command -v tree >/dev/null 2>&1; then tree /proc/device-tree; else find /proc/device-tree -type f 2>/dev/null | while read f; do echo \"=== $f ===\"; cat \"$f\" 2>/dev/null; done; fi"
    
    self.board_info_collection_queue = [
      {"cmd": "hostname", "section": "system", "title": "Hostname"},
      {"cmd": "lsmod", "section": "kernel", "title": "Loaded Kernel Modules"},
      {"cmd": "ip a", "section": "network", "title": "Network Interfaces"},
      {"cmd": "zcat /proc/config.gz 2>/dev/null || echo 'Kernel config not available'", "section": "kernel", "title": "Kernel Configuration"},
      {"cmd": "cat /etc/version 2>/dev/null || echo 'Not available'", "section": "system", "title": "Version Information"},
      {"cmd": "cat /etc/os-release 2>/dev/null || echo 'Not available'", "section": "system", "title": "OS Release Information"},
      {"cmd": "df -h", "section": "storage", "title": "Filesystem Usage"},
      {"cmd": "mount", "section": "storage", "title": "Mounted Filesystems"},
      {"cmd": "fdisk -l 2>/dev/null || echo 'fdisk not available'", "section": "storage", "title": "Partition Table"},
      {"cmd": "cat /proc/cpuinfo", "section": "hardware", "title": "CPU Information"},
      {"cmd": "cat /proc/meminfo", "section": "hardware", "title": "Memory Information"},
      {"cmd": device_tree_cmd, "section": "hardware", "title": "Device Tree"},
      {"cmd": "ls -la /sys/kernel/debug 2>/dev/null | head -n 50", "section": "debug", "title": "Debug Filesystem Contents"},
      {"cmd": "find /sys/kernel/debug/pinctrl -type f 2>/dev/null | head -n 20", "section": "debug", "title": "Pinctrl Debug Files"},
      {"cmd": "cat /sys/kernel/debug/pinctrl/*/pinmux-pins 2>/dev/null | head -n 100", "section": "debug", "title": "Pinmux Configuration"},
    ]
    self.board_info_output = {}
    self.board_info_output_buffer = []
    self.board_info_collecting = False
    self.board_info_current_command = None
    self.logger.info(f"Initialized board info collection with {len(self.board_info_collection_queue)} commands")

  def _start_next_board_info_command(self):
    """Start the next command in the collection queue"""
    if not self.board_info_collection_queue:
      return
    
    cmd_info = self.board_info_collection_queue.pop(0)
    self.board_info_current_command = cmd_info
    self.board_info_output_buffer = []
    self.board_info_collecting = True
    
    self.logger.info(f"Collecting: {cmd_info['title']} ({cmd_info['cmd']})")
    self.send_serial_command(cmd_info['cmd'])

  def _save_current_command_output(self):
    """Save collected output for current command"""
    if not self.board_info_current_command:
      return
    
    cmd_info = self.board_info_current_command
    section = cmd_info['section']
    title = cmd_info['title']
    
    if section not in self.board_info_output:
      self.board_info_output[section] = {}
    
    self.board_info_output[section][title] = {
      'command': cmd_info['cmd'],
      'output': '\n'.join(self.board_info_output_buffer)
    }
    
    self.board_info_collecting = False
    self.board_info_current_command = None
    self.board_info_output_buffer = []
    
    self.logger.info(f"Saved: {title} ({len(self.board_info_output[section][title]['output'])} chars)")

  def _generate_board_info_markdown(self):
    """Generate markdown file with collected board information"""
    sections_order = ['system', 'kernel', 'hardware', 'network', 'storage', 'debug']
    sections_titles = {
      'system': 'System Information',
      'kernel': 'Kernel Information',
      'hardware': 'Hardware Information',
      'network': 'Network Information',
      'storage': 'Storage Information',
      'debug': 'Debug Information'
    }
    
    md_lines = []
    md_lines.append("# Board Information")
    md_lines.append("")
    # Add hostname if available
    hostname = self.board_info_output.get('system', {}).get('Hostname', {}).get('output', '').strip()
    if hostname:
      md_lines.append(f"**Hostname:** `{hostname}`")
      md_lines.append("")
    md_lines.append(f"*Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    md_lines.append("")
    
    # Table of Contents
    md_lines.append("## Table of Contents")
    md_lines.append("")
    for section in sections_order:
      if section in self.board_info_output:
        md_lines.append(f"- [{sections_titles[section]}](#{section.replace('_', '-')}-information)")
        for title in self.board_info_output[section].keys():
          anchor = title.lower().replace(' ', '-').replace('(', '').replace(')', '')
          md_lines.append(f"  - [{title}](#{anchor})")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")
    
    # Content sections
    for section in sections_order:
      if section not in self.board_info_output:
        continue
      
      md_lines.append(f"## {sections_titles[section]}")
      md_lines.append("")
      
      for title, data in self.board_info_output[section].items():
        anchor = title.lower().replace(' ', '-').replace('(', '').replace(')', '')
        md_lines.append(f"### {title}")
        md_lines.append("")
        md_lines.append(f"**Command:** `{data['command']}`")
        md_lines.append("")
        md_lines.append("```")
        md_lines.append(data['output'])
        md_lines.append("```")
        md_lines.append("")
      
      md_lines.append("---")
      md_lines.append("")
    
    # Write to file
    try:
      with open(self.board_info_md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines))
      self.logger.info(f"Board information saved to: {self.board_info_md}")
    except Exception as e:
      self.logger.error(f"Failed to write board info markdown: {e}")

  def _call_collect_board_info_script(self):
    """Call external collect_board_info.py script with real-time output"""
    script_path = Path(__file__).parent / "collect_board_info.py"
    if not script_path.exists():
      self.logger.warning(f"collect_board_info.py not found at {script_path}, skipping external collection")
      return False
    
    try:
      self.logger.info("Calling collect_board_info.py for board information collection...")
      
      # Use Popen for real-time output streaming
      process = subprocess.Popen(
        [
          sys.executable,
          str(script_path),
          "--serial", self.serial_port,
          "--baudrate", str(self.baudrate),
          "--log-dir", str(self.session_log_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Merge stderr into stdout
        text=True,
        bufsize=1,  # Line buffered
      )
      
      # Read output line by line in real-time
      start_time = time.time()
      timeout = 300  # 5 minute timeout
      
      while True:
        # Check timeout
        elapsed = time.time() - start_time
        if elapsed > timeout:
          process.kill()
          process.wait()
          self.logger.warning(f"collect_board_info.py timed out after {timeout} seconds")
          return False
        
        # Check if process is still running
        return_code = process.poll()
        if return_code is not None:
          # Process finished, read any remaining output
          remaining = process.stdout.read()
          if remaining:
            for line in remaining.splitlines():
              if line.strip():
                self.logger.info(f"[collect_board_info] {line}")
          break
        
        # Read line (with select for non-blocking check)
        try:
          # Use select to check if data is available (non-blocking)
          import select
          ready, _, _ = select.select([process.stdout], [], [], 0.1)
          if ready:
            # Data available, read line (should not block now)
            line = process.stdout.readline()
            if line:
              line = line.rstrip()
              if line:
                # Log in real-time
                self.logger.info(f"[collect_board_info] {line}")
          else:
            # No data available, small sleep to avoid busy waiting
            time.sleep(0.05)
        except (ImportError, OSError):
          # select not available on Windows, or other OS error
          # Fallback: just readline (may block briefly, but bufsize=1 helps)
          try:
            line = process.stdout.readline()
            if line:
              line = line.rstrip()
              if line:
                # Log in real-time
                self.logger.info(f"[collect_board_info] {line}")
            else:
              # EOF or no data, small sleep
              time.sleep(0.05)
          except Exception as e:
            self.logger.debug(f"Error reading from process: {e}")
            time.sleep(0.1)
      
      # Wait for process to complete
      return_code = process.wait()
      
      if return_code == 0:
        self.logger.info("collect_board_info.py completed successfully")
        # Verify markdown file was created
        board_info_md = self.session_log_dir / "board-info.md"
        if board_info_md.exists():
          file_size = board_info_md.stat().st_size
          self.logger.info(f"Board info markdown file created: {board_info_md} ({file_size} bytes)")
          return True
        else:
          self.logger.warning(f"Board info markdown file not found at {board_info_md}")
          return False
      else:
        self.logger.warning(f"collect_board_info.py exited with code {return_code}")
        return False
    except subprocess.TimeoutExpired:
      self.logger.warning("collect_board_info.py timed out after 5 minutes")
      return False
    except Exception as e:
      self.logger.warning(f"Failed to call collect_board_info.py: {e}")
      return False

  async def run_adnl_burn_pkg(self):
    """Run adnl_burn_pkg tool and capture output (non-blocking)"""
    if not self.image_path.exists():
      self.logger.error(f"Image file not found: {self.image_path}")
      self.change_state(State.ERROR, "Image file not found")
      return

    self.logger.info(f"Starting adnl_burn_pkg with image: {self.image_path}")

    try:
      # Keep serial port open - burn process also outputs to serial port
      # USB and serial can work simultaneously
      # Run adnl_burn_pkg using async subprocess
      cmd = ["sudo", "adnl_burn_pkg", "-p", str(self.image_path), "-r", "1"]
      self.logger.info(f"Executing: {' '.join(cmd)}")

      # Use asyncio.create_subprocess_exec for non-blocking I/O
      self.adnl_process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
      )

      # Track last progress update time to detect stalls
      last_progress_time = time.time()
      last_progress_percent = 0
      progress_stall_timeout = 60  # 60 seconds without progress = stall

      # Read output line by line (async, non-blocking)
      while True:
        # Use asyncio.wait_for with timeout to allow other tasks to run
        try:
          line_bytes = await asyncio.wait_for(
            self.adnl_process.stdout.readline(),
            timeout=1.0  # 1 second timeout to allow serial reader to run
          )
        except asyncio.TimeoutError:
          # Timeout is OK - allows serial reader to process data
          # Check if process is still running
          if self.adnl_process.returncode is not None:
            break
          continue

        if not line_bytes:
          break

        line = line_bytes.decode("utf-8", errors="replace").strip()
        if line:
          self.log_line(self.adnl_log, line)
          color = self.COLORS["ADNL"]
          reset = self.COLORS["RESET"]
          self.logger.info(f"{color}[adnl]{reset} {line}")

          # Track progress to detect stalls
          progress_match = re.search(r"%(\d+)\.\.", line)
          if progress_match:
            current_percent = int(progress_match.group(1))
            if current_percent > last_progress_percent:
              last_progress_percent = current_percent
              last_progress_time = time.time()
              self.logger.debug(f"Burn progress: {current_percent}%")
            elif time.time() - last_progress_time > progress_stall_timeout:
              self.logger.warning(
                f"Burn progress stalled at {last_progress_percent}% for "
                f"{int(time.time() - last_progress_time)} seconds"
              )

          # Check for success
          if "burn successful" in line.lower() or "burn successful^_^" in line:
            self.logger.info("Burn successful! Waiting for board to reboot and boot...")
            self.burn_complete_time = time.time()
            # Reset login flag for post-burn login
            self.login_sent = False
            self.boot_verify_sent = False
            self.change_state(State.BOOT_VERIFY, "Burn completed, verifying boot")
            break

      # Wait for process to complete
      return_code = await self.adnl_process.wait()
      if return_code != 0:
        self.logger.error(f"adnl_burn_pkg exited with code {return_code}")
        self.change_state(State.ERROR, f"adnl_burn_pkg failed with code {return_code}")
      else:
        self.logger.info("adnl_burn_pkg completed")

    except Exception as e:
      self.logger.error(f"Error running adnl_burn_pkg: {e}")
      self.change_state(State.ERROR, f"adnl_burn_pkg error: {e}")

  async def monitor_timeout(self):
    """Monitor for timeout conditions"""
    self.logger.info("Timeout monitor task started")
    check_interval = 1.0  # Check every second
    last_log_time = 0
    log_interval = 5.0  # Log status every 5 seconds

    while self.state not in [State.COMPLETE, State.ERROR]:
      await asyncio.sleep(check_interval)
      elapsed = time.time() - self.last_activity
      current_time = time.time()

      # Log status periodically (only after initial wake sequence)
      if self.initial_wake_sent and current_time - last_log_time > log_interval:
        last_line_elapsed = (
          current_time - self.last_line_time
          if self.last_line_time
          else float("inf")
        )
        status_msg = (
          f"Status: state={self.state.value}, "
          f"elapsed={elapsed:.1f}s, "
          f"lines_received={self.lines_received}, "
          f"last_line={last_line_elapsed:.1f}s ago"
        )
        if last_line_elapsed > 10:
          status_msg += " [WARNING: No new lines for 10+ seconds]"
          self.no_lines_warning_count += 1
          
          # If we've warned 2 times (approximately 20 seconds total), stop with detailed error
          # First warning at ~10s, second at ~15s, so we stop after second warning
          if self.no_lines_warning_count >= 2:
            self.logger.error("=" * 60)
            self.logger.error("CRITICAL: No serial data received for 20+ seconds")
            self.logger.error("=" * 60)
            self.logger.error("")
            self.logger.error("Please check the following:")
            self.logger.error("")
            self.logger.error("1. Are you sure the board is accessible via serial UART?")
            self.logger.error("   - Check serial cable connection")
            self.logger.error("   - Verify serial port path: " + self.serial_port)
            self.logger.error("   - Check baudrate matches board configuration: " + str(self.baudrate))
            self.logger.error("")
            self.logger.error("2. Are you sure the board is powered on?")
            self.logger.error("   - Check power LED indicators")
            self.logger.error("   - Verify power supply is connected and working")
            self.logger.error("")
            self.logger.error("3. If both above are OK, but still no update from serial:")
            self.logger.error("   - Board kernel might be crashed")
            self.logger.error("   - Either use relay (--relay <IP>) to power cycle")
            self.logger.error("   - Or manually power cycle the board")
            self.logger.error("")
            self.logger.error("=" * 60)
            self.change_state(State.ERROR, "No serial data for 20+ seconds")
            break
        else:
          # Reset counter if we received data
          self.no_lines_warning_count = 0
        
        self.logger.info(status_msg)
        last_log_time = current_time

      # Check for timeout (only after we've received some data)
      if self.lines_received > 0 and elapsed > self.timeout_seconds:
        self.logger.error(
          f"Timeout after {elapsed:.1f} seconds of inactivity "
          f"(state: {self.state.value}, lines: {self.lines_received})"
        )
        self.change_state(State.ERROR, "Timeout")
        break

    self.logger.info("Timeout monitor task ended")

  async def run(self):
    """Main run loop"""
    self.logger.info("=" * 60)
    self.logger.info("Amlogic Burn Tool Starting")
    self.logger.info(f"Serial port: {self.serial_port}")
    self.logger.info(f"Baudrate: {self.baudrate}")
    self.logger.info(f"Image: {self.image_path}")
    self.logger.info(f"Relay IP: {self.relay_ip or 'None'}")
    self.logger.info("=" * 60)

    # Pre-flight checks
    ok, msg = self.check_serial_port()
    if not ok:
      self.logger.error(f"Serial port check failed: {msg}")
      return False

    if self.relay_ip:
      ok, msg, status = self.check_relay()
      if not ok:
        self.logger.error(f"Relay check failed: {msg}")
        return False
      self.logger.info(msg)

    # Open serial port
    try:
      self.serial_conn = serial.Serial(
        self.serial_port,
        self.baudrate,
        timeout=0.1,
        write_timeout=1.0,
      )
      self.logger.info(f"Serial port opened: {self.serial_port}")
    except Exception as e:
      self.logger.error(f"Failed to open serial port: {e}")
      return False

    # Start async tasks first (needed for both relay and non-relay modes)
    self.logger.info("Starting async tasks...")
    self.logger.info("Creating serial reader task...")
    serial_task = asyncio.create_task(self.read_serial_async())
    
    self.logger.info("Creating timeout monitor task...")
    timeout_task = asyncio.create_task(self.monitor_timeout())
    
    # Wait a bit for tasks to start
    await asyncio.sleep(0.5)
    self.logger.info("Async tasks started, ready to monitor serial output")

    # Power cycle if relay configured
    if self.relay_ip:
      self.logger.info("Relay configured, starting power cycle...")
      
      # Try up to 2 times to catch autoboot
      max_attempts = 2
      autoboot_caught = False
      
      for attempt in range(1, max_attempts + 1):
        if attempt > 1:
          self.logger.info(f"Retry attempt {attempt}/{max_attempts}...")
        
        # Power cycle: OFF -> wait 5s -> ON (no wait)
        self.relay_power_cycle()
        
        # Set flags to catch autoboot after power cycle
        self.reboot_sent = True
        self.login_sent = False
        self.adnl_sent = False
        self.uboot_prompt_seen_after_reboot = False
        self.stop_enter_sending = False
        
        # Start sending Enter immediately after power ON
        # Send Enter every 0.5 seconds for up to 10 seconds
        enter_timeout = 10.0
        enter_interval = 0.5
        start_time = time.time()
        enter_count = 0
        last_log_time = start_time
        
        self.logger.info(f"Sending Enter every {enter_interval}s for up to {enter_timeout}s to catch autoboot...")
        
        while (time.time() - start_time) < enter_timeout:
          if self.serial_conn and self.serial_conn.is_open:
            try:
              self.serial_conn.write(b"\r")
              enter_count += 1
              current_time = time.time()
              # Log every 0.5 seconds
              if (current_time - last_log_time) >= enter_interval:
                elapsed = current_time - start_time
                self.logger.info(f"Sending Enter... ({elapsed:.1f}s / {enter_timeout}s)")
                last_log_time = current_time
            except Exception as e:
              self.logger.error(f"Error sending Enter: {e}")
              break
          
          await asyncio.sleep(enter_interval)
          
          # Check if U-Boot prompt was detected
          if self.uboot_prompt_seen_after_reboot:
            elapsed = time.time() - start_time
            self.logger.info(f"U-Boot prompt detected after {elapsed:.1f}s, autoboot caught!")
            autoboot_caught = True
            break
        
        # Check if we successfully caught autoboot
        if autoboot_caught:
          self.logger.info("Autoboot successfully caught, continuing...")
          break
        else:
          elapsed = time.time() - start_time
          self.logger.warning(f"Autoboot not caught after {elapsed:.1f}s (attempt {attempt}/{max_attempts})")
          if attempt < max_attempts:
            self.logger.info("Retrying power cycle...")
            await asyncio.sleep(1.0)  # Brief pause before retry
          else:
            self.logger.error("Failed to catch autoboot after 2 attempts. Please check board connections and boot configuration.")
            self.change_state(State.ERROR, "Failed to catch autoboot after power cycle")
            return False
    else:
      self.logger.info("No relay configured, assuming board is already powered on")
      
      # Initial wait and wake-up sequence
      # Board might be in adnl mode, so we need to try different wake-up methods
      self.logger.info("Waiting 3 seconds for initial boot/console wake-up...")
      await asyncio.sleep(3)

      # Check if we received any data
      if self.lines_received == 0:
        self.logger.info(
          "No data received yet, trying to wake up console..."
        )
        
        # First attempt: Send Enter every 0.5s for 10 seconds
        enter_timeout = 10.0
        enter_interval = 0.5
        start_time = time.time()
        enter_count = 0
        
        self.logger.info(f"Sending Enter every {enter_interval}s for up to {enter_timeout}s...")
        
        while (time.time() - start_time) < enter_timeout:
          if self.serial_conn and self.serial_conn.is_open:
            try:
              self.serial_conn.write(b"\r")
              enter_count += 1
              if enter_count % 2 == 0:  # Log every 0.5s
                elapsed = time.time() - start_time
                self.logger.info(f"Sending Enter... ({elapsed:.1f}s / {enter_timeout}s)")
            except Exception as e:
              self.logger.error(f"Error sending Enter: {e}")
              break
          
          await asyncio.sleep(enter_interval)
          
          # Check if we received any data
          if self.lines_received > 0:
            elapsed = time.time() - start_time
            self.logger.info(f"Received data after {elapsed:.1f}s, console is active")
            break
        
        # If still no data, try Ctrl+C + Enter (board might be in adnl mode)
        if self.lines_received == 0:
          self.logger.info("Still no response, trying Ctrl+C + Enter (board might be in adnl mode)...")
          for attempt in range(2):
            self.send_ctrl_c_enter()
            self.logger.info(f"Sent Ctrl+C + Enter (attempt {attempt + 1}/2)")
            await asyncio.sleep(1.0)
            
            # Check if we received any data
            if self.lines_received > 0:
              self.logger.info("Received data after Ctrl+C + Enter, console is active")
              break
          
          # Final check
          if self.lines_received == 0:
            self.logger.error("No response after all wake-up attempts. Please check:")
            self.logger.error("  - Board is powered on")
            self.logger.error("  - Serial cable is connected")
            self.logger.error("  - Serial port permissions")
            self.logger.error("  - Baudrate matches board configuration")
            self.change_state(State.ERROR, "No serial response after wake-up attempts")
            return False
        else:
          self.initial_wake_sent = True
      else:
        self.logger.info(
          f"Received {self.lines_received} lines already, console is active"
        )

    # Continue with main state machine loop (async tasks already started above)
    try:

      # Main loop: wait for state transitions
      loop_count = 0
      self.logger.info("Entering main state machine loop...")
      uboot_timeout_start = None
      complete_wait_start = None
      
      while self.state != State.ERROR:
        await asyncio.sleep(0.1)
        loop_count += 1

        # Check if we're waiting for U-Boot prompt after reboot
        if (
          self.reboot_sent
          and not self.uboot_prompt_seen_after_reboot
          and (self.state == State.INIT or self.state == State.BOOTROM)
        ):
          if uboot_timeout_start is None:
            uboot_timeout_start = time.time()
          elapsed = time.time() - uboot_timeout_start
          if elapsed > 30:  # 30 seconds timeout
            self.logger.error(
              "Timeout: U-Boot prompt not detected after reboot. "
              "Autoboot may not have been caught."
            )
            self.change_state(State.ERROR, "U-Boot prompt not detected after reboot")
            break
        else:
          uboot_timeout_start = None

        # Log every 50 iterations (5 seconds)
        if loop_count % 50 == 0:
          self.logger.debug(
            f"Main loop iteration {loop_count}, "
            f"state: {self.state.value}, "
            f"lines: {self.lines_received}"
          )

        # If we're in DOWNLOAD state, run adnl_burn_pkg
        if self.state == State.DOWNLOAD and self.adnl_process is None:
          self.logger.info("DOWNLOAD state detected, preparing to run adnl_burn_pkg...")
          # Small delay to ensure board is ready
          await asyncio.sleep(1)
          await self.run_adnl_burn_pkg()
          # Serial port remains open during and after burn

        # If we're in BOOT_VERIFY state, check timeout
        if self.state == State.BOOT_VERIFY and self.burn_complete_time:
          elapsed = time.time() - self.burn_complete_time
          if elapsed > self.boot_verify_timeout:
            self.logger.warning(
              f"Boot verification timeout after {elapsed:.1f} seconds. "
              f"Boot may not have completed successfully."
            )
            self.change_state(State.COMPLETE, "Boot verification timeout")
        
        # If we're in COMPLETE state, kernel verified - exit immediately
        if self.state == State.COMPLETE:
          if complete_wait_start is None:
            complete_wait_start = time.time()
            if self.board_info_output:
              self.logger.info(
                "Burn, boot verification, and board information collection complete!"
              )
            else:
              self.logger.info(
                "Burn and boot verification complete. "
                "Kernel version verified - boot successful!"
              )
          # Exit immediately after completion - no need to wait
          break

      # Stop continuous Enter if running
      if self.continuous_enter_task and not self.continuous_enter_task.done():
        self.stop_enter_sending = True
        self.logger.info("Stopping continuous Enter task...")
        try:
          await asyncio.wait_for(self.continuous_enter_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
          pass

      # Cancel tasks
      serial_task.cancel()
      timeout_task.cancel()

      try:
        await serial_task
      except asyncio.CancelledError:
        pass

      try:
        await timeout_task
      except asyncio.CancelledError:
        pass

    finally:
      if self.serial_conn and self.serial_conn.is_open:
        self.serial_conn.close()
        self.logger.info("Serial port closed")

    success = self.state == State.COMPLETE
    if success:
      self.logger.info("=" * 60)
      # Check if board-info.md was created by collect_board_info.py
      board_info_md = self.session_log_dir / "board-info.md"
      if board_info_md.exists():
        file_size = board_info_md.stat().st_size
        self.logger.info("Burn, boot verification, and board information collection completed successfully!")
        self.logger.info(f"Board information saved to: {board_info_md} ({file_size} bytes)")
      else:
        self.logger.info("Burn and boot verification completed successfully!")
        if self.boot_verify_sent:
          self.logger.info("Kernel version verified - boot successful!")
      self.logger.info(f"Logs saved to: {self.session_log_dir}")
      self.logger.info("=" * 60)
    else:
      self.logger.error("=" * 60)
      self.logger.error("Process failed!")
      self.logger.error(f"Logs saved to: {self.log_dir}")
      self.logger.error("=" * 60)

    return success


def validate_image(image_path: Path) -> tuple[bool, str]:
  """Validate image file
  
  Checks:
  - File exists
  - File size is at least 50MB
  
  Future enhancements:
  - Binary header validation
  - File format verification
  - Checksum validation
  
  Args:
    image_path: Path to image file
    
  Returns:
    Tuple of (is_valid, error_message)
  """
  if not image_path.exists():
    return False, f"Image file not found: {image_path}"
  
  file_size = image_path.stat().st_size
  min_size = 50 * 1024 * 1024  # 50 MB in bytes
  
  if file_size < min_size:
    return False, f"Image file too small: {image_path} ({file_size / (1024*1024):.2f} MB < {min_size / (1024*1024)} MB)"
  
  return True, ""


def load_config() -> Dict[str, Any]:
  """Load configuration from config file
  
  Searches in:
  1. Script directory: ./aml-burn-tool-config.json
  2. System-wide: /etc/aml-burn-tool/aml-burn-tool-config.json
  
  Returns:
    Dict with config values
    
  Raises:
    SystemExit: If config file not found or invalid
  """
  config_name = "aml-burn-tool-config.json"
  
  # Get script directory
  script_dir = Path(__file__).parent.absolute()
  local_config = script_dir / config_name
  system_config = Path("/etc/aml-burn-tool") / config_name
  
  # Try local config first, then system-wide
  config_path = None
  if local_config.exists():
    config_path = local_config
  elif system_config.exists():
    config_path = system_config
  else:
    print("ERROR: Configuration file not found!")
    print(f"  Expected locations:")
    print(f"    - {local_config}")
    print(f"    - {system_config}")
    print(f"\n  Please copy aml-burn-tool-config.json.example to one of these locations")
    print(f"  and configure it according to your setup.")
    sys.exit(1)
  
  # Load and parse config
  try:
    with open(config_path, 'r') as f:
      config = json.load(f)
  except json.JSONDecodeError as e:
    print(f"ERROR: Invalid JSON in config file: {config_path}")
    print(f"  {e}")
    sys.exit(1)
  except Exception as e:
    print(f"ERROR: Failed to read config file: {config_path}")
    print(f"  {e}")
    sys.exit(1)
  
  # Validate required fields
  required_fields = ["serial_port", "baudrate"]
  missing_fields = [field for field in required_fields if field not in config]
  if missing_fields:
    print(f"ERROR: Missing required fields in config file: {', '.join(missing_fields)}")
    sys.exit(1)
  
  # Validate baudrate is integer
  if not isinstance(config["baudrate"], int):
    try:
      config["baudrate"] = int(config["baudrate"])
    except (ValueError, TypeError):
      print(f"ERROR: Invalid baudrate value in config: {config['baudrate']}")
      sys.exit(1)
  
  return config


def main():
  """Main entry point"""
  # Load config first
  config = load_config()
  
  parser = argparse.ArgumentParser(
    description="Amlogic SBC automated burn tool with event-driven FSM"
  )
  parser.add_argument(
    "--serial",
    default=config.get("serial_port", "/dev/serial-polaris"),
    help=f"Serial port device (default from config: {config.get('serial_port', '/dev/serial-polaris')})",
  )
  parser.add_argument(
    "--baudrate",
    type=int,
    default=config.get("baudrate", 921600),
    help=f"Serial port baudrate (default from config: {config.get('baudrate', 921600)})",
  )
  parser.add_argument(
    "--relay",
    default=config.get("relay_ip"),
    help=f"Tasmota relay IP address (default from config: {config.get('relay_ip', 'None')})",
  )
  parser.add_argument(
    "--image",
    default=config.get("default_image", "polaris.img"),
    help=f"Image file path (default from config: {config.get('default_image', 'polaris.img')})",
  )

  args = parser.parse_args()

  # Validate image file before starting
  image_path = Path(args.image)
  is_valid, error_msg = validate_image(image_path)
  if not is_valid:
    print(f"ERROR: {error_msg}")
    sys.exit(1)

  tool = BurnTool(
    serial_port=args.serial,
    baudrate=args.baudrate,
    image_path=args.image,
    relay_ip=args.relay if args.relay else None,
  )

  try:
    success = asyncio.run(tool.run())
    sys.exit(0 if success else 1)
  except KeyboardInterrupt:
    print("\nInterrupted by user")
    sys.exit(130)
  except Exception as e:
    print(f"Fatal error: {e}")
    import traceback

    traceback.print_exc()
    sys.exit(1)


if __name__ == "__main__":
  main()

