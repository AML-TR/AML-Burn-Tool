# Amlogic Burn Tool

Event-driven Finite State Machine (FSM) based automated image flashing tool for Amlogic-based SBCs.

## Features

- **Event-driven FSM**: Pattern-based state detection and automated command sending
- **Serial port monitoring**: Continuous reading and pattern matching from serial output
- **Automated U-Boot interaction**: Stops autoboot, enters download mode
- **Relay control**: Optional Tasmota-based relay control for power cycling
- **Comprehensive logging**: Three separate log files with millisecond timestamps
- **Pre-flight checks**: Validates serial port availability and relay connectivity

## Requirements

- Python 3.8+
- pyserial
- requests
- sudo access for `adnl_burn_pkg` tool

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Basic usage (default settings):

```bash
./aml-burn-tool.py
```

This uses default values:
- Serial port: `/dev/serial-polaris`
- Baudrate: `921600`
- Image: `polaris.img`
- No relay (manual power cycle required)

### With custom image file:

```bash
./aml-burn-tool.py --image polaris.img
```

### With relay control (recommended for automated power cycling):

```bash
./aml-burn-tool.py --relay 192.168.1.220
```

### With custom serial port:

```bash
./aml-burn-tool.py --serial /dev/ttyUSB0 --baudrate 921600
```

### Full example with all parameters:

```bash
./aml-burn-tool.py \
  --serial /dev/serial-polaris \
  --baudrate 921600 \
  --relay 192.168.1.220 \
  --image polaris.img
```

### Help:

```bash
./aml-burn-tool.py --help
```

## Command Line Arguments

- `--serial`: Serial port device (default: `/dev/serial-polaris`)
- `--baudrate`: Serial port baudrate (default: `921600`)
- `--relay`: Tasmota relay IP address (optional)
- `--image`: Image file path (default: `polaris.img`)

## Workflow

1. **Pre-flight checks**:
   - Validates serial port exists and is not in use
   - Checks relay connectivity (if configured)
   - Verifies image file exists

2. **Power cycle** (if relay configured):
   - Turns power OFF → waits 3 seconds
   - Turns power ON → waits 5 seconds

3. **Boot monitoring**:
   - Monitors serial output for boot patterns
   - Detects U-Boot autoboot message
   - Sends Enter to stop autoboot

4. **Enter download mode**:
   - Waits for U-Boot prompt (`s4_polaris#` or `=>`)
   - Sends `adnl` command
   - Serial port remains open (USB and serial work simultaneously)

5. **Image flashing**:
   - Runs `sudo adnl_burn_pkg -p <image> -r 1`
   - Captures and logs all output
   - Monitors for "burn successful" message

6. **Completion**:
   - Board automatically reboots after successful burn
   - Script completes and saves logs

## State Machine

The tool uses an event-driven FSM with the following states:

- `INIT`: Initial state, waiting for boot
- `BOOTROM`: BootROM/BL2 detected
- `UBOOT`: U-Boot detected, waiting for prompt
- `DOWNLOAD`: Download mode entered, running adnl_burn_pkg
- `LINUX`: Linux booted, login prompt detected
- `LOGIN`: Login sent, waiting for shell prompt
- `COMPLETE`: Burn completed successfully
- `ERROR`: Error occurred

## Pattern Detection

The tool detects the following patterns in serial output:

- `autoboot`: "Hit any key to stop autoboot"
- `uboot_prompt`: U-Boot prompt (`s4_polaris#`, `=>`, etc.)
- `login_prompt`: Linux login prompt
- `shell_prompt`: Shell prompt (`root@...:~#`)
- `uboot_version`: U-Boot version string
- `bl2`: BL2/BL2E bootloader messages
- `bootrom`: BootROM messages
- `usb_reset`: USB reset messages
- `rebooting`: Reboot messages

## Log Files

All logs are saved to `logs/` directory with timestamps:

- `serial_YYYYMMDD_HHMMSS.log`: All serial port output
- `adnl_YYYYMMDD_HHMMSS.log`: adnl_burn_pkg tool output
- `script_YYYYMMDD_HHMMSS.log`: Script execution logs

All logs include millisecond-precision timestamps.

## Error Handling

- **Serial port in use**: Checks for other processes (minicom, screen) and reports PIDs
- **Relay connectivity**: Validates relay is reachable before use
- **Timeout**: 5-minute timeout for inactivity (warnings every 5 seconds if no data)
- **Image file**: Validates image file exists before starting
- **No serial data**: Warns if no data received for 5+ seconds

## Troubleshooting

### Script hangs after opening serial port

If the script appears to hang after "Serial port opened", check:

1. **Board is powered on**: Ensure the board has power
2. **Serial connection**: Verify USB-to-serial cable is connected
3. **Serial port permissions**: Check if user has access to serial port
   ```bash
   ls -l /dev/serial-polaris
   sudo usermod -a -G dialout $USER  # Then logout/login
   ```
4. **Other processes**: Make sure no other process is using the serial port
   ```bash
   lsof /dev/serial-polaris
   pkill minicom
   pkill screen
   ```

The script will log detailed status every 5 seconds, including:
- Current state
- Elapsed time since last activity
- Number of lines received from serial port
- Serial reader status

### No serial data received

If you see "No serial data received" warnings:

1. Check serial cable connection
2. Verify board is powered on
3. Check baudrate matches board configuration
4. Try different serial port: `--serial /dev/ttyUSB0`

## Notes

- The tool automatically handles the case where board boots to Linux instead of U-Boot:
  - Detects login prompt
  - Sends `root` login
  - Sends `reboot -f` to restart
  - Waits for U-Boot and sends `adnl` again

- Serial port remains open during `adnl_burn_pkg` execution:
  - USB and serial port work simultaneously
  - Burn process outputs progress to serial port (USB RESET, OEM commands, flash progress)
  - All serial output during burn is captured and logged

- The `-r 1` flag to `adnl_burn_pkg` automatically reboots the board after successful burn

## Example Output

```
============================================================
Amlogic Burn Tool Starting
Serial port: /dev/serial-polaris
Baudrate: 921600
Image: polaris.img
Relay IP: 192.168.1.220
============================================================
INFO: Serial port opened: /dev/serial-polaris
INFO: Power cycling board via relay...
INFO: State transition: INIT -> BOOTROM (BootROM/BL2 detected)
INFO: State transition: BOOTROM -> UBOOT (U-Boot detected)
INFO: Sent Enter to stop autoboot
INFO: State transition: UBOOT -> DOWNLOAD (Entered download mode)
INFO: Starting adnl_burn_pkg with image: polaris.img
INFO: [adnl] burn successful^_^
INFO: State transition: DOWNLOAD -> COMPLETE (Burn completed successfully)
INFO: ============================================================
INFO: Burn process completed successfully!
INFO: Logs saved to: logs/
============================================================
```

