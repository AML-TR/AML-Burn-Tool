# Verification Procedure for AML Burn Tool

## Overview
This document describes the verification procedure for `aml-burn-tool.py` and `collect_board_info.py` scripts.

## Test Scripts
- `aml-burn-tool.py`: Main burn tool script
- `collect_board_info.py`: Board information collection script

---

## Test 1: Error Handling with Invalid Configuration

### Purpose
Verify that scripts handle invalid configuration files gracefully and display appropriate error messages.

### Procedure
1. Create an invalid config file in the script's working directory:
   - Replace `/dev/serial-polaris` with `/dev/serial-incorrect-device-node`
   - OR replace relay IP `192.168.1.220` with `192.168.1.225` (invalid IP)

2. Run both scripts with the invalid config:
   ```bash
   ./aml-burn-tool.py
   ./collect_board_info.py
   ```

3. Expected Result:
   - Scripts should detect the error
   - Appropriate error messages should be displayed
   - Scripts should exit with non-zero exit code

---

## Test 2: Error Handling with Invalid Arguments

### Purpose
Verify that scripts handle invalid command-line arguments correctly.

### Procedure
1. Run scripts with invalid arguments:
   ```bash
   ./aml-burn-tool.py --serial /dev/nonexistent
   ./aml-burn-tool.py --image nonexistent.img
   ./collect_board_info.py --serial /dev/nonexistent
   ./collect_board_info.py --baudrate invalid
   ```

2. Expected Result:
   - Scripts should detect invalid arguments
   - Appropriate error messages should be displayed
   - Scripts should exit with non-zero exit code

---

## Test 3: Login and Logout Control

### Purpose
Verify that scripts can handle both logged-out and logged-in board states.

### Procedure

#### 3.1 Logout Test
1. Run `logout.py` to logout the board:
   ```bash
   ./logout.py --serial /dev/serial-polaris
   ./logout.py --serial /dev/serial-signify-pro
   ```

2. Expected Result:
   - Board should logout successfully
   - Login prompt should be detected

#### 3.2 Script Execution After Logout
1. Run scripts after logout:
   ```bash
   ./aml-burn-tool.py --serial /dev/serial-polaris
   ./collect_board_info.py --serial /dev/serial-polaris
   ```

2. Expected Result:
   - Scripts should detect login prompt
   - Scripts should automatically login
   - Scripts should continue execution normally

#### 3.3 Script Execution When Already Logged In
1. Ensure board is logged in (run a command manually or wait)
2. Run scripts:
   ```bash
   ./aml-burn-tool.py --serial /dev/serial-polaris
   ./collect_board_info.py --serial /dev/serial-polaris
   ```

3. Expected Result:
   - Scripts should detect shell prompt directly
   - Scripts should skip login and continue execution
   - No login errors should occur

---

## Test 4: Burn Test

### Purpose
Verify that burn process works correctly on both board types.

### Procedure

#### 4.1 Polaris Board Burn Test
1. Burn `polaris.img` to Polaris board (`/dev/serial-polaris`):
   ```bash
   ./aml-burn-tool.py --serial /dev/serial-polaris --image polaris.img
   ```

2. Expected Result:
   - Burn process should complete successfully
   - Board should boot successfully
   - Success message should be displayed

3. After successful burn, run board info collection:
   ```bash
   ./collect_board_info.py --serial /dev/serial-polaris
   ```

4. Expected Result:
   - Board information should be collected successfully
   - All 16 commands should complete
   - Markdown and PDF files should be generated

#### 4.2 MS-ESR1A Board Burn Test
1. Burn `ms-esr1a.img` to MS-ESR1A board (`/dev/serial-signify-pro`):
   ```bash
   ./aml-burn-tool.py --serial /dev/serial-signify-pro --image ms-esr1a.img
   ```

2. Expected Result:
   - Relay test should fail (no relay connected to this board)
   - Burn process should continue and complete successfully
   - Board should boot successfully
   - Success message should be displayed

3. After successful burn, run board info collection:
   ```bash
   ./collect_board_info.py --serial /dev/serial-signify-pro
   ```

4. Expected Result:
   - Board information should be collected successfully
   - All 16 commands should complete
   - Markdown and PDF files should be generated

---

## Test 5: Report Correctness Test

### Purpose
Verify that generated reports are complete and correct.

### Procedure
1. Run `collect_board_info.py` on both boards:
   ```bash
   ./collect_board_info.py --serial /dev/serial-polaris
   ./collect_board_info.py --serial /dev/serial-signify-pro
   ```

2. Check generated files:
   - Markdown files should exist in `logs/board-info-*/board-info.md`
   - PDF files should exist in `logs/board-info-*/board-info.pdf`

3. Verify report content:
   - All sections should be present:
     * Operating System Information
     * Board Hardware Information
     * Storage Information
     * Network Information
     * Kernel Information
     * Debug Information
   - All fields should be populated (no empty sections)
   - All commands should show output (or "command not found" if unavailable)

4. Performance check:
   - Scripts should never timeout
   - All commands should complete quickly (< 60 seconds each)
   - Total execution time should be reasonable (< 5 minutes)

5. Expected Result:
   - All reports should be complete
   - All fields should be populated
   - No timeouts should occur
   - Reports should be readable and well-formatted

---

## Test Execution

To run all verification tests, execute:
```bash
# Full verification
# (When user types "full verification", execute all tests above)
```

## Notes
- All tests should be run on both `/dev/serial-polaris` and `/dev/serial-signify-pro`
- Tests should be run in sequence to ensure proper state
- Log files should be checked for any unexpected errors
- All test results should be documented

