#!/usr/bin/env python3
"""
Logout script - sends 'exit' command and waits for login prompt
"""

import argparse
import sys
import time
import re

try:
  import serial
except ImportError:
  print("ERROR: pyserial not installed. Run: pip install pyserial")
  sys.exit(1)


def main():
  parser = argparse.ArgumentParser(description="Logout from serial port")
  parser.add_argument("--serial", type=str, required=True, help="Serial port path")
  parser.add_argument("--baudrate", type=int, default=921600, help="Baudrate")
  
  args = parser.parse_args()
  
  # Open serial port
  print(f"Opening serial port: {args.serial} @ {args.baudrate}")
  ser = serial.Serial(args.serial, args.baudrate, timeout=1.0)
  time.sleep(2.0)  # Wait for serial port to stabilize
  ser.reset_input_buffer()
  
  # Wait for shell prompt
  print("Waiting for shell prompt...")
  shell_prompt = re.compile(r"root@.*?:\~#\s*$")
  login_prompt = re.compile(r"login:\s*$")
  buffer = ""
  prompt_wait_start = time.time()
  enter_sent = False
  
  while time.time() - prompt_wait_start < 10.0:
    elapsed = time.time() - prompt_wait_start
    if ser.in_waiting > 0:
      data = ser.read(ser.in_waiting).decode("utf-8", errors="ignore")
      buffer += data
      enter_sent = False
      # Check for shell prompt
      if shell_prompt.search(buffer):
        print(f"Shell prompt found! ({elapsed:.2f}s)")
        break
      # Check for login prompt (already logged out)
      if login_prompt.search(buffer):
        print(f"Already at login prompt! ({elapsed:.2f}s)")
        ser.close()
        return 0
    else:
      # If no data for 2 seconds, send Enter to wake up
      if elapsed > 2.0 and not enter_sent:
        print("No data received, sending Enter to wake up...")
        ser.write(b"\r\n")
        enter_sent = True
        time.sleep(0.1)
      time.sleep(0.1)
  
  if not shell_prompt.search(buffer):
    print("ERROR: Shell prompt not found")
    ser.close()
    return 1
  
  # Send 'exit' command
  print("Sending 'exit' command...")
  ser.write(b"exit\r\n")
  time.sleep(2.0)  # Wait 2 seconds as requested
  
  # Send Enter
  print("Sending Enter...")
  ser.write(b"\r\n")
  
  # Wait for login prompt
  print("Waiting for login prompt...")
  buffer = ""
  login_wait_start = time.time()
  
  while time.time() - login_wait_start < 10.0:
    elapsed = time.time() - login_wait_start
    if ser.in_waiting > 0:
      data = ser.read(ser.in_waiting).decode("utf-8", errors="ignore")
      buffer += data
      # Check for login prompt
      if login_prompt.search(buffer):
        print(f"Login prompt detected! ({elapsed:.2f}s)")
        ser.close()
        return 0
    else:
      time.sleep(0.1)
  
  # Check if login prompt is already in buffer
  if login_prompt.search(buffer):
    print("Login prompt found in buffer")
    ser.close()
    return 0
  
  print("ERROR: Login prompt not found after logout")
  print(f"Last 100 chars of buffer: {repr(buffer[-100:])}")
  ser.close()
  return 1


if __name__ == "__main__":
  sys.exit(main())

