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
import subprocess
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
  
  VERSION = "v2.0"  # Script version

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
    
    # Board info collection - hierarchical order:
    # 1. Operating System (os-release, version, hostname)
    # 2. Board Hardware (CPU, Memory, Disk, Network)
    # 3. Kernel (uname, lsmod, config, device-tree)
    self.commands = [
      # Operating System Information
      {"cmd": "cat /etc/os-release 2>/dev/null || echo 'Not available'", "section": "system", "title": "OS Release Information"},
      {"cmd": "cat /etc/version 2>/dev/null || echo 'Not available'", "section": "system", "title": "Version Information"},
      {"cmd": "hostname", "section": "system", "title": "Hostname"},
      # Board Hardware Information
      {"cmd": "cat /proc/cpuinfo", "section": "hardware", "title": "CPU Information"},
      {"cmd": "cat /proc/meminfo", "section": "hardware", "title": "Memory Information"},
      {"cmd": "df -h", "section": "storage", "title": "Filesystem Usage"},
      {"cmd": "mount", "section": "storage", "title": "Mounted Filesystems"},
      {"cmd": "fdisk -l 2>/dev/null || echo 'fdisk not available'", "section": "storage", "title": "Partition Table"},
      {"cmd": "ip a", "section": "network", "title": "Network Interfaces"},
      # Kernel Information
      {"cmd": "uname -a", "section": "kernel", "title": "Kernel Information"},
      {"cmd": "lsmod", "section": "kernel", "title": "Loaded Kernel Modules"},
      {"cmd": "zcat /proc/config.gz 2>/dev/null || echo 'Kernel config not available'", "section": "kernel", "title": "Kernel Configuration"},
      {"cmd": "find /proc/device-tree/", "section": "kernel", "title": "Device Tree"},
      # Debug Information
      {"cmd": "ls -la /sys/kernel/debug 2>/dev/null", "section": "debug", "title": "Debug Filesystem Contents"},
      {"cmd": "find /sys/kernel/debug/pinctrl -type f 2>/dev/null", "section": "debug", "title": "Pinctrl Debug Files"},
      {"cmd": "cat /sys/kernel/debug/pinctrl/*/pinmux-pins 2>/dev/null", "section": "debug", "title": "Pinmux Configuration"},
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
          # Remove ANSI escape sequences (including cursor position queries like ;231R)
          line = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', line)  # Standard ANSI escape sequences
          line = re.sub(r';\d+R', '', line)  # Cursor position responses like ;231R
          line = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', line)  # Other control characters
          return line
        elif char != b"\r":  # Ignore carriage return
          line_buffer += char
      else:
        time.sleep(0.01)  # Small sleep to avoid busy waiting
    
    # Timeout - return what we have
    if line_buffer:
      line = line_buffer.decode("utf-8", errors="ignore").strip()
      # Remove ANSI escape sequences
      line = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', line)
      line = re.sub(r';\d+R', '', line)
      line = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', line)
      return line
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
    
    # Flush any pending data
    self.serial_conn.reset_input_buffer()
    
    # Send command (no marker, just the command)
    self.send_command(cmd, send_ctrl_c=False)
    
    # Read lines until we see shell prompt
    output_lines = []
    start_time = time.time()
    timeout = 60.0  # Max 60 seconds per command (increased for long outputs)
    last_output_time = start_time
    no_output_timeout = 3.0  # If no output for 3 seconds after getting some output, assume command is done
    
    while time.time() - start_time < timeout:
      line = self.read_line(timeout=1.0)
      if line is None:
        # No line read, check if we've been waiting too long without output
        elapsed_no_output = time.time() - last_output_time
        if elapsed_no_output > no_output_timeout and len(output_lines) > 0:
          # We have some output but nothing new for 3 seconds, check for shell prompt in buffer
          # Read one more time to catch shell prompt that might be in buffer
          final_line = self.read_line(timeout=0.5)
          if final_line and self.PATTERNS["shell_prompt"].search(final_line):
            self.logger.info(f"Shell prompt detected after no-output timeout, saving output ({len(output_lines)} lines)")
          else:
            self.logger.info(f"No new output for {elapsed_no_output:.1f}s, assuming command complete ({len(output_lines)} lines)")
          break
        continue
      
      last_output_time = time.time()  # Update last output time
      
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
    
    # Check if we timed out
    elapsed = time.time() - start_time
    if elapsed >= timeout:
      self.logger.warning(f"Command timeout after {elapsed:.1f}s, saving collected output ({len(output_lines)} lines)")
    
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
        self.logger.info("Waiting 5 seconds after login...")
        time.sleep(5.0)
        
        # After 5 seconds, check if shell prompt is already in buffer
        # Read any available data first (with timeout to avoid blocking)
        shell_prompt_found = False
        buffer_check_timeout = time.time() + 2.0  # Check buffer for up to 2 seconds
        while time.time() < buffer_check_timeout and self.serial_conn.in_waiting > 0:
          line = self.read_line(timeout=0.1)
          if line:
            self.logger.debug(f"Read line after login wait: {line[:100]}")
            # Check if this line contains shell prompt
            if self.PATTERNS["shell_prompt"].search(line):
              color = self.COLORS["PATTERN"]
              reset = self.COLORS["RESET"]
              self.logger.info(f"{color}[Pattern]{reset} Matched 'shell_prompt' in buffer: {line}")
              prompt = "shell_prompt"
              shell_prompt_found = True
              break
        
        # If not found in buffer, wait for prompt
        if not shell_prompt_found:
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
    
    # Header section with metadata
    current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md_lines.append("---\n")
    md_lines.append(f"**Serial Port:** {self.serial_port}\n")
    md_lines.append(f"**Date:** {current_datetime}\n")
    md_lines.append(f"**Script Version:** {self.VERSION}\n")
    md_lines.append("---\n")
    md_lines.append("\n")
    
    # Table of Contents (with clickable links)
    md_lines.append("## Table of Contents\n")
    section_order = ["system", "hardware", "storage", "network", "kernel", "debug"]
    section_titles = {
      "system": "Operating System",
      "hardware": "Board Hardware",
      "storage": "Storage",
      "network": "Network",
      "kernel": "Kernel",
      "debug": "Debug",
    }
    for section in section_order:
      if section in sections:
        # Create anchor link for PDF navigation
        anchor = section.replace("_", "-")
        md_lines.append(f"- [{section_titles.get(section, section.capitalize())}](#{anchor}-information)")
    md_lines.append("\n")
    
    # Content by section
    for section in section_order:
      if section not in sections:
        continue
      
      # Add anchor for TOC links
      anchor = section.replace("_", "-")
      md_lines.append(f"## {section_titles.get(section, section.capitalize())} Information {{#{anchor}-information}}\n")
      
      for title, data in sections[section]:
        md_lines.append(f"### {title}\n")
        md_lines.append(f"**Command:** `{data['command']}`\n")
        md_lines.append("\n")
        md_lines.append("```\n")
        if data["output"]:
          md_lines.append(data["output"])
        else:
          md_lines.append("(No output)")
        md_lines.append("```\n")
        md_lines.append("\n")
        md_lines.append("---\n")
        md_lines.append("\n")
    
    # End of report
    md_lines.append("---\n")
    md_lines.append("\n")
    md_lines.append("## End of Report\n")
    md_lines.append("\n")
    
    # Write to file
    content = "\n".join(md_lines)
    self.board_info_md.write_text(content, encoding="utf-8")
    self.logger.info(f"Markdown file generated: {self.board_info_md}")
    
    # Generate PDF from markdown
    self.generate_pdf()
  
  def _markdown_to_html(self, md_content: str) -> str:
    """Convert markdown to HTML manually (no markdown module needed)"""
    import html as html_escape
    lines = md_content.split('\n')
    html_lines = []
    in_code_block = False
    code_block_lines = []
    
    for line in lines:
      # Code blocks
      if line.strip().startswith('```'):
        if in_code_block:
          # End code block
          html_lines.append('<pre><code>' + '\n'.join(code_block_lines) + '</code></pre>')
          code_block_lines = []
          in_code_block = False
        else:
          # Start code block
          in_code_block = True
        continue
      
      if in_code_block:
        code_block_lines.append(html_escape.escape(line))
        continue
      
      # Headers
      if line.startswith('# '):
        html_lines.append(f'<h1>{html_escape.escape(line[2:].strip())}</h1>')
      elif line.startswith('## '):
        # Check for anchor
        anchor_match = re.search(r'\{#([^}]+)\}', line)
        if anchor_match:
          anchor = anchor_match.group(1)
          text = re.sub(r'\{#[^}]+\}', '', line[3:]).strip()
          html_lines.append(f'<h2 id="{anchor}">{html_escape.escape(text)}</h2>')
        else:
          html_lines.append(f'<h2>{html_escape.escape(line[3:].strip())}</h2>')
      elif line.startswith('### '):
        html_lines.append(f'<h3>{html_escape.escape(line[4:].strip())}</h3>')
      # Horizontal rule
      elif line.strip() == '---':
        html_lines.append('<hr>')
      # Links
      elif '[' in line and '](' in line:
        # Simple link: [text](url)
        line = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', line)
        html_lines.append(f'<p>{line}</p>')
      # Bold
      elif '**' in line:
        line = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', line)
        # Inline code
        line = re.sub(r'`([^`]+)`', r'<code>\1</code>', line)
        html_lines.append(f'<p>{line}</p>')
      # Lists
      elif line.strip().startswith('- '):
        if not html_lines or html_lines[-1] != '<ul>':
          html_lines.append('<ul>')
        item_text = line.strip()[2:]
        # Handle links in list items
        item_text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', item_text)
        html_lines.append(f'<li>{item_text}</li>')
      # Empty line
      elif not line.strip():
        if html_lines and html_lines[-1].startswith('<ul>'):
          html_lines.append('</ul>')
        html_lines.append('<br>')
      # Regular paragraph
      else:
        # Inline code
        line = re.sub(r'`([^`]+)`', r'<code>\1</code>', line)
        html_lines.append(f'<p>{html_escape.escape(line)}</p>')
    
    # Close any open lists
    if html_lines and html_lines[-1].startswith('<li>'):
      html_lines.append('</ul>')
    
    return '\n'.join(html_lines)
  
  def generate_pdf(self):
    """Generate PDF from markdown file using weasyprint or pandoc"""
    pdf_path = self.board_info_md.with_suffix('.pdf')
    
    try:
      # Try weasyprint first (better control over styling)
      try:
        from weasyprint import HTML
        self.logger.debug("Using weasyprint for PDF generation")
        
        # Convert markdown to HTML manually (no markdown module needed)
        md_content = self.board_info_md.read_text(encoding="utf-8")
        html_content = self._markdown_to_html(md_content)
        
        # Post-process HTML to style commands in red
        import re as html_re
        # Style commands in red
        html_content = html_re.sub(
          r'<strong>Command:</strong>\s*<code>(.*?)</code>',
          r'<strong style="color: #d73a49;">Command:</strong> <code style="color: #d73a49; font-weight: 500;">\1</code>',
          html_content
        )
        
        # GitHub-like CSS styling with Roboto font (fallback to system fonts)
        css_content = """
        @page {
          size: A3 landscape;
          margin: 1.5cm;
        }
        body {
          font-family: 'Roboto', 'DejaVu Sans', 'Liberation Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
          font-size: 10pt;
          line-height: 1.5;
          color: #24292e;
          background-color: #ffffff;
        }
        h1 {
          font-size: 1.8em;
          border-bottom: 2px solid #eaecef;
          padding-bottom: 0.3em;
          margin-top: 0;
          margin-bottom: 16px;
          font-weight: 600;
        }
        h2 {
          font-size: 1.4em;
          border-bottom: 1px solid #eaecef;
          padding-bottom: 0.3em;
          margin-top: 20px;
          margin-bottom: 12px;
          font-weight: 600;
        }
        h3 {
          font-size: 1.15em;
          margin-top: 20px;
          margin-bottom: 12px;
          font-weight: 600;
        }
        /* Inline code (commands) - red color */
        p code {
          font-family: 'Roboto Mono', 'DejaVu Sans Mono', 'Liberation Mono', 'Courier New', monospace;
          background-color: #f6f8fa;
          padding: 0.15em 0.4em;
          border-radius: 3px;
          font-size: 90%;
        }
        /* Code blocks (command outputs) - black/white in rectangle */
        pre {
          background-color: #f6f8fa;
          border: 1px solid #d1d5da;
          border-radius: 6px;
          padding: 12px;
          overflow: auto;
          font-size: 9pt;
          line-height: 1.4;
          margin: 8px 0;
          page-break-inside: avoid;
        }
        pre code {
          font-family: 'Roboto Mono', 'DejaVu Sans Mono', 'Liberation Mono', 'Courier New', monospace;
          background-color: transparent;
          padding: 0;
          border: none;
          color: #24292e;
          font-weight: normal;
          white-space: pre;
          word-wrap: break-word;
        }
        ul, ol {
          padding-left: 2em;
        }
        a {
          color: #0366d6;
          text-decoration: none;
        }
        a:hover {
          text-decoration: underline;
        }
        /* Header section styling */
        hr {
          height: 0.25em;
          padding: 0;
          margin: 20px 0;
          background-color: #e1e4e8;
          border: 0;
        }
        hr + p {
          margin: 8px 0;
          font-size: 11pt;
        }
        table {
          border-collapse: collapse;
          width: 100%;
          margin: 16px 0;
        }
        th, td {
          border: 1px solid #dfe2e5;
          padding: 6px 13px;
        }
        th {
          background-color: #f6f8fa;
          font-weight: 600;
        }
        """
        
        # Wrap HTML with proper structure
        full_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>{css_content}</style>
</head>
<body>
{html_content}
</body>
</html>"""
        
        # Generate PDF
        try:
          HTML(string=full_html).write_pdf(pdf_path)
          self.logger.info(f"PDF file generated with weasyprint: {pdf_path}")
          return
        except Exception as e:
          self.logger.warning(f"weasyprint PDF generation failed: {e}")
          raise  # Re-raise to trigger fallback
        
      except ImportError as e:
        self.logger.warning(f"weasyprint not available: {e}")
        # weasyprint not available, try pandoc (but it won't respect our CSS)
        try:
          self.logger.info("Falling back to pandoc (note: CSS styling will not be applied)")
          result = subprocess.run(
            ['pandoc', str(self.board_info_md), '-o', str(pdf_path), 
             '--pdf-engine=xelatex', 
             '-V', 'mainfont=Roboto',
             '-V', 'geometry:margin=2cm',
             '-V', 'papersize=a3paper',
             '-V', 'geometry:landscape=true'],
            capture_output=True,
            text=True,
            timeout=30,
          )
          if result.returncode == 0:
            self.logger.info(f"PDF file generated with pandoc: {pdf_path}")
            self.logger.warning("Note: pandoc does not support custom CSS. For full styling, install weasyprint: pip install weasyprint markdown")
            return
          else:
            self.logger.warning(f"pandoc failed: {result.stderr}")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
          self.logger.debug(f"pandoc not available: {e}")
        
        self.logger.warning("PDF generation skipped: Install 'weasyprint' (pip install weasyprint) for full PDF support with custom styling")
        
    except Exception as e:
      self.logger.warning(f"Failed to generate PDF: {e}")


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

