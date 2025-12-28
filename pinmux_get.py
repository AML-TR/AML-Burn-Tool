#!/usr/bin/env python3
"""
Simple pinmux command test - read everything until shell prompt appears
"""

import argparse
import sys
import time
import re
from datetime import datetime

try:
  import serial
except ImportError:
  print("ERROR: pyserial not installed. Run: pip install pyserial")
  sys.exit(1)


def log_with_timestamp(message, elapsed=None):
  """Log message with timestamp and optional elapsed time"""
  timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
  if elapsed is not None:
    print(f"[{timestamp}] [{elapsed:.3f}s] {message}")
  else:
    print(f"[{timestamp}] {message}")


def main():
  parser = argparse.ArgumentParser(description="Test pinmux command output")
  parser.add_argument("--serial", type=str, required=True, help="Serial port path")
  parser.add_argument("--baudrate", type=int, default=921600, help="Baudrate")
  
  args = parser.parse_args()
  
  script_start = time.time()
  log_with_timestamp("Script started")
  
  # Open serial port
  log_with_timestamp("Opening serial port...")
  open_start = time.time()
  ser = serial.Serial(args.serial, args.baudrate, timeout=1.0)
  open_elapsed = time.time() - open_start
  log_with_timestamp(f"Serial port opened", open_elapsed)
  
  log_with_timestamp("Waiting 2 seconds for serial port to stabilize...")
  time.sleep(2.0)
  log_with_timestamp("Flushing input buffer...")
  ser.reset_input_buffer()
  
  # Wait for shell prompt
  log_with_timestamp("Waiting for shell prompt...")
  shell_prompt = re.compile(r"root@.*?:\~#\s*$")
  buffer = ""
  prompt_wait_start = time.time()
  last_data_time = prompt_wait_start
  no_data_count = 0
  enter_sent = False
  
  while time.time() - prompt_wait_start < 10.0:
    elapsed = time.time() - prompt_wait_start
    if ser.in_waiting > 0:
      data = ser.read(ser.in_waiting).decode("utf-8", errors="ignore")
      buffer += data
      last_data_time = time.time()
      no_data_count = 0
      log_with_timestamp(f"Received {len(data)} bytes (total: {len(buffer)} bytes)", elapsed)
      # Check for shell prompt
      if shell_prompt.search(buffer):
        log_with_timestamp("Shell prompt found!", elapsed)
        break
    else:
      no_data_count += 1
      # If no data for 2 seconds, send Enter to wake up
      if elapsed > 2.0 and not enter_sent:
        log_with_timestamp("No data received, sending Enter to wake up...", elapsed)
        ser.write(b"\r\n")
        enter_sent = True
        time.sleep(0.1)
      elif no_data_count % 10 == 0:  # Log every 1 second (10 * 0.1s)
        log_with_timestamp(f"Still waiting for shell prompt... (no data for {elapsed:.1f}s)", elapsed)
      time.sleep(0.1)
  
  prompt_wait_elapsed = time.time() - prompt_wait_start
  if not shell_prompt.search(buffer):
    log_with_timestamp(f"WARNING: Shell prompt not found after {prompt_wait_elapsed:.1f}s, proceeding anyway", prompt_wait_elapsed)
  
  # Send command
  log_with_timestamp("Sending command: cat /sys/kernel/debug/pinctrl/*/pinmux-pins")
  send_start = time.time()
  ser.write(b"cat /sys/kernel/debug/pinctrl/*/pinmux-pins\r\n")
  send_elapsed = time.time() - send_start
  log_with_timestamp(f"Command sent", send_elapsed)
  
  # Read everything until shell prompt appears again
  log_with_timestamp("Reading output (will wait until shell prompt appears)...")
  output_buffer = ""
  shell_prompt_found = False
  read_start = time.time()
  last_data_time = read_start
  read_count = 0
  no_data_count = 0
  
  while time.time() - read_start < 60.0:  # Max 60 seconds total
    elapsed = time.time() - read_start
    # Always try to read available data
    if ser.in_waiting > 0:
      data = ser.read(ser.in_waiting).decode("utf-8", errors="ignore")
      output_buffer += data
      read_count += 1
      last_data_time = time.time()
      no_data_count = 0
      
      if read_count % 10 == 0:  # Log every 10th read
        log_with_timestamp(f"Read {read_count} times, buffer size: {len(output_buffer)} chars", elapsed)
      
      # Check for shell prompt in the buffer (check last 200 chars to be efficient)
      if len(output_buffer) > 200:
        check_region = output_buffer[-200:]
      else:
        check_region = output_buffer
      
      if shell_prompt.search(check_region):
        log_with_timestamp(f"Shell prompt detected, stopping read (read {read_count} times, {len(output_buffer)} chars)", elapsed)
        shell_prompt_found = True
        break
    else:
      no_data_count += 1
      elapsed_no_data = time.time() - last_data_time
      # Log if no data for more than 1 second
      if no_data_count % 20 == 0:  # Every 1 second (20 * 0.05s)
        log_with_timestamp(f"No data for {elapsed_no_data:.1f}s, buffer: {len(output_buffer)} chars", elapsed)
      
      # No data available - but if we have output, check if shell prompt is already there
      if output_buffer:
        if shell_prompt.search(output_buffer):
          log_with_timestamp(f"Shell prompt found in buffer (no data for {elapsed_no_data:.1f}s)", elapsed)
          shell_prompt_found = True
          break
      time.sleep(0.05)  # Small sleep when no data
  
  read_elapsed = time.time() - read_start
  log_with_timestamp(f"Reading complete (total: {read_elapsed:.2f}s, {read_count} reads, {len(output_buffer)} chars)", read_elapsed)
  
  # Clean up output: remove command echo and shell prompt
  lines = output_buffer.split('\n')
  result_lines = []
  
  for line in lines:
    line = line.strip()
    # Skip command echo
    if "cat /sys/kernel/debug/pinctrl" in line and len(line) < 100:
      continue
    # Skip shell prompt
    if shell_prompt.search(line):
      continue
    # Skip empty lines
    if not line:
      continue
    result_lines.append(line)
  
  output = "\n".join(result_lines)
  
  total_elapsed = time.time() - script_start
  log_with_timestamp(f"Total script execution time", total_elapsed)
  
  print("\n" + "=" * 80)
  print("PINMUX OUTPUT:")
  print("=" * 80)
  print(output)
  print("=" * 80)
  print(f"\nOutput length: {len(output)} characters")
  print(f"Output lines: {len(output.splitlines())} lines")
  
  log_with_timestamp("Closing serial port...")
  ser.close()
  log_with_timestamp("Script finished")


if __name__ == "__main__":
  main()

