#!/usr/bin/env python3
"""
Board Information Collection Tool
Simple synchronous request-response approach: send command, read response, save, next command
Can be run standalone or called from aml-burn-tool.py
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

try:
  import serial
except ImportError:
  print("ERROR: pyserial not installed. Run: pip install pyserial")
  sys.exit(1)


class BoardInfoCollector:
  """Collect board information via serial port - synchronous version"""

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
      }
    else:
      return {
        "RESET": "",
        "SERIAL": "",
        "PATTERN": "",
        "FSM": "",
      }
  
  @property
  def COLORS(self):
    """Get color codes (property to check TTY each time)"""
    return self._get_colors()

  # Pattern definitions
  PATTERNS = {
    "login_prompt": re.compile(r"login:\s*$", re.MULTILINE),
    "shell_prompt": re.compile(r"root@.*?:\~#\s*$", re.MULTILINE),
    "uboot_prompt": re.compile(
      r"(s4_polaris#|a4_mainstream#|a4_ba400#|=>|U-Boot>)\s*$", re.MULTILINE
    ),
  }

  def __init__(
    self,
    serial_port: str,
    baudrate: int,
    log_dir: Optional[Path] = None,
  ):
    self.serial_port = serial_port
    self.baudrate = baudrate
    self.serial_conn = None
    
    # Setup log directory
    if log_dir:
      self.log_dir = log_dir
      self.board_info_md = log_dir / "board-info.md"
    else:
      base_log_dir = Path("logs")
      base_log_dir.mkdir(exist_ok=True)
      timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
      self.log_dir = base_log_dir / f"board-info-{timestamp}"
      self.log_dir.mkdir(exist_ok=True)
      self.board_info_md = self.log_dir / "board-info.md"
    
    # Setup logging
    self.setup_logging()
    
    # Board info collection
    self.commands = [
      {"cmd": "hostname", "section": "system", "title": "Hostname"},
      {"cmd": "uname -a", "section": "kernel", "title": "Kernel Information"},
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
      {"cmd": "find /proc/device-tree/", "section": "hardware", "title": "Device Tree"},
      {"cmd": "ls -la /sys/kernel/debug 2>/dev/null | head -n 50", "section": "debug", "title": "Debug Filesystem Contents"},
      {"cmd": "find /sys/kernel/debug/pinctrl -type f 2>/dev/null | head -n 20", "section": "debug", "title": "Pinctrl Debug Files"},
      {"cmd": "cat /sys/kernel/debug/pinctrl/*/pinmux-pins 2>/dev/null | head -n 100", "section": "debug", "title": "Pinmux Configuration"},
    ]
    self.collected_data = {}

  def setup_logging(self):
    """Setup logging with colored console and plain file"""
    root_logger = logging.getLogger()
    root_logger.handlers = []
    
    class NoColorFormatter(logging.Formatter):
      """Formatter that removes ANSI color codes"""
      def format(self, record):
        msg = super().format(record)
        msg = re.sub(r'\033\[[0-9;]*m', '', msg)
        msg = re.sub(r'\x1b\[[0-9;]*m', '', msg)
        return msg
    
    console_formatter = logging.Formatter(
      "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
      datefmt="%Y-%m-%d %H:%M%S"
    )
    
    file_formatter = NoColorFormatter(
      "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
      datefmt="%Y-%m-%d %H:%M%S"
    )
    
    file_handler = logging.FileHandler(self.log_dir / "collect_board_info_sync.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    self.logger = logging.getLogger(__name__)

  def send_command(self, command: str, send_ctrl_c: bool = False):
    """Send command to serial port"""
    if not self.serial_conn or not self.serial_conn.is_open:
      return
    
    color = self.COLORS["SERIAL"]
    reset = self.COLORS["RESET"]
    
    if send_ctrl_c:
      self.logger.info(f"{color}[Serial]{reset} Sending Ctrl+C")
      self.serial_conn.write(b"\x03")
      time.sleep(0.1)
    
    if command:
      self.logger.info(f"{color}[Serial]{reset} Sending: {command}")
      self.serial_conn.write(command.encode())
    
    self.serial_conn.write(b"\r\n")
    time.sleep(0.1)

  def read_line(self, timeout: float = 5.0) -> Optional[str]:
    """Read a line from serial port with timeout"""
    if not self.serial_conn or not self.serial_conn.is_open:
      return None
    
    start_time = time.time()
    line_buffer = b""
    
    while time.time() - start_time < timeout:
      if self.serial_conn.in_waiting > 0:
        char = self.serial_conn.read(1)
        if char == b"\n":
          line = line_buffer.decode("utf-8", errors="ignore").strip()
          return line
        elif char != b"\r":  # Ignore carriage return
          line_buffer += char
      else:
        time.sleep(0.01)  # Small sleep to avoid busy waiting
    
    # Timeout - return what we have
    if line_buffer:
      return line_buffer.decode("utf-8", errors="ignore").strip()
    return None

  def wait_for_prompt(self, timeout: float = 10.0) -> Optional[str]:
    """Wait for a prompt (login, shell, or uboot)"""
    start_time = time.time()
    lines_read = []
    
    self.logger.info(f"Waiting for prompt (timeout: {timeout}s)...")
    
    while time.time() - start_time < timeout:
      line = self.read_line(timeout=1.0)
      if line:
        lines_read.append(line)
        self.logger.debug(f"Read line: {line[:100]}")
        for pattern_name, pattern in self.PATTERNS.items():
          if pattern.search(line):
            color = self.COLORS["PATTERN"]
            reset = self.COLORS["RESET"]
            self.logger.info(f"{color}[Pattern]{reset} Matched '{pattern_name}': {line}")
            return pattern_name
      else:
        # If no line read, try to wake up the board
        if time.time() - start_time > 2.0 and len(lines_read) == 0:
          self.logger.debug("No data received, sending Enter to wake up")
          self.send_command("", send_ctrl_c=False)
        time.sleep(0.1)
    
    # Timeout - log what we read
    if lines_read:
      self.logger.warning(f"Timeout waiting for prompt. Last {min(10, len(lines_read))} lines read:")
      for line in lines_read[-10:]:
        self.logger.warning(f"  {line[:100]}")
    else:
      self.logger.warning("Timeout waiting for prompt. No data received from serial port.")
    
    return None

  def collect_command_output(self, command: Dict[str, str]) -> str:
    """Collect output for a single command - simple: send command, read until shell prompt"""
    cmd = command["cmd"]
    title = command["title"]
    
    self.logger.info(f"Collecting: {title}")
    
    # Clear screen first and flush any pending data
    self.serial_conn.reset_input_buffer()  # Clear input buffer before clear
    self.send_command("clear", send_ctrl_c=False)
    time.sleep(0.3)  # Wait for clear to complete
    
    # Read and discard any remaining data from clear command
    while self.serial_conn.in_waiting > 0:
      self.read_line(timeout=0.1)
    
    # Final buffer flush
    self.serial_conn.reset_input_buffer()
    
    # Send command (no marker, just the command)
    self.send_command(cmd, send_ctrl_c=False)
    
    # Read lines until we see shell prompt
    output_lines = []
    start_time = time.time()
    timeout = 30.0  # Max 30 seconds per command
    
    while time.time() - start_time < timeout:
      line = self.read_line(timeout=1.0)
      if line is None:
        continue
      
      # Check for shell prompt - if we see it, command is done
      if self.PATTERNS["shell_prompt"].search(line):
        self.logger.info(f"Shell prompt detected, saving output ({len(output_lines)} lines)")
        break
      
      # Filter out unwanted lines
      if (line.strip() and 
          line.strip() != cmd and 
          not line.strip().endswith(cmd) and
          line.strip() != "clear" and
          not line.strip().startswith("clear") and
          not line.strip().startswith("root@") and
          not re.match(r'^\x1b\[', line.strip())):  # Filter ANSI escape sequences
        output_lines.append(line)
        self.logger.debug(f"Collected output line: {line[:80]}")
    
    output = "\n".join(output_lines)
    output_size = len(output)
    self.logger.info(f"Saved: {title} ({output_size} chars)")
    
    return output

  def run(self) -> bool:
    """Main execution - synchronous"""
    try:
      # Open serial port
      self.logger.info(f"Opening serial port: {self.serial_port} @ {self.baudrate}")
      self.serial_conn = serial.Serial(
        port=self.serial_port,
        baudrate=self.baudrate,
        timeout=1.0,
        write_timeout=1.0,
      )
      time.sleep(2.0)  # Wait for serial port to stabilize
      
      # Flush any existing data
      self.serial_conn.reset_input_buffer()
      self.serial_conn.reset_output_buffer()
      
      # Wait for initial prompt - try sending Enter first to wake up
      self.logger.info("Waiting for initial prompt...")
      self.send_command("", send_ctrl_c=False)
      time.sleep(0.5)
      prompt = self.wait_for_prompt(timeout=10.0)
      
      if prompt is None:
        # Try Ctrl+C to wake up
        self.logger.info("No prompt detected, trying Ctrl+C to wake up...")
        self.send_command("", send_ctrl_c=True)
        time.sleep(0.5)
        prompt = self.wait_for_prompt(timeout=10.0)
      
      if prompt == "uboot_prompt":
        self.logger.error("Board is at U-Boot prompt. Please boot the board first.")
        return False
      
      # If at login prompt, login
      if prompt == "login_prompt":
        self.logger.info("Login prompt detected, sending Ctrl+C and root")
        self.send_command("", send_ctrl_c=True)
        time.sleep(0.2)
        self.send_command("root", send_ctrl_c=False)
        # Wait for shell prompt
        prompt = self.wait_for_prompt(timeout=10.0)
        if prompt != "shell_prompt":
          self.logger.error("Failed to get shell prompt after login")
          return False
      
      if prompt != "shell_prompt":
        self.logger.error(f"Unexpected prompt: {prompt}. Expected shell_prompt.")
        return False
      
      self.logger.info("Shell prompt detected, starting command collection")
      
      # Collect all commands
      for cmd_info in self.commands:
        output = self.collect_command_output(cmd_info)
        self.collected_data[cmd_info["title"]] = {
          "command": cmd_info["cmd"],
          "section": cmd_info["section"],
          "output": output,
        }
      
      # Generate markdown
      self.generate_markdown()
      
      self.logger.info("Collection complete!")
      return True
      
    except serial.SerialException as e:
      self.logger.error(f"Serial port error: {e}")
      return False
    except KeyboardInterrupt:
      self.logger.info("Interrupted by user")
      return False
    except Exception as e:
      self.logger.error(f"Unexpected error: {e}", exc_info=True)
      return False
    finally:
      if self.serial_conn and self.serial_conn.is_open:
        self.serial_conn.close()
        self.logger.info("Serial port closed")

  def generate_markdown(self):
    """Generate markdown file with collected data"""
    sections = {}
    
    # Organize by section
    for title, data in self.collected_data.items():
      section = data["section"]
      if section not in sections:
        sections[section] = []
      sections[section].append((title, data))
    
    # Generate markdown
    md_lines = ["# Board Information\n"]
    
    # Table of Contents
    md_lines.append("## Table of Contents\n")
    section_order = ["system", "kernel", "hardware", "network", "storage", "debug"]
    for section in section_order:
      if section in sections:
        md_lines.append(f"- [{section.capitalize()}](#{section}-information)")
    md_lines.append("")
    
    # Content by section
    for section in section_order:
      if section not in sections:
        continue
      
      md_lines.append(f"## {section.capitalize()} Information\n")
      
      for title, data in sections[section]:
        md_lines.append(f"### {title}\n")
        md_lines.append(f"**Command:** `{data['command']}`\n")
        md_lines.append("```\n")
        if data["output"]:
          md_lines.append(data["output"])
        else:
          md_lines.append("(No output)")
        md_lines.append("```\n")
        md_lines.append("")
        md_lines.append("---\n")
        md_lines.append("")
    
    # Write to file
    content = "\n".join(md_lines)
    self.board_info_md.write_text(content, encoding="utf-8")
    self.logger.info(f"Markdown file generated: {self.board_info_md}")


def load_config() -> Dict[str, Any]:
  """Load configuration from JSON file"""
  config_path = Path("aml-burn-tool-config.json")
  if config_path.exists():
    try:
      with open(config_path, "r") as f:
        return json.load(f)
    except Exception as e:
      print(f"Warning: Could not load config: {e}")
  return {}


def main():
  """Main entry point"""
  config = load_config()
  
  parser = argparse.ArgumentParser(
    description="Board Information Collection Tool - Synchronous Version"
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
    "--log-dir",
    type=Path,
    default=None,
    help="Log directory (if called from aml-burn-tool.py)",
  )
  
  args = parser.parse_args()
  
  collector = BoardInfoCollector(
    serial_port=args.serial,
    baudrate=args.baudrate,
    log_dir=args.log_dir,
  )
  
  success = collector.run()
  sys.exit(0 if success else 1)


if __name__ == "__main__":
  main()

