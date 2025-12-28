#!/usr/bin/env python3
"""
Multi-Board Verification Script for AML Burn Tool

Runs all test cases from verification.md for all boards defined in multiboard_verification.json
and generates a comprehensive test report.
"""

import subprocess
import sys
import time
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import re

class MultiBoardVerificationRunner:
    def __init__(self, config_file: Optional[Path] = None):
        self.script_dir = Path(__file__).parent
        self.config_file = config_file or (self.script_dir / "multiboard_verification.json")
        self.boards = []
        self.report_dir = self.script_dir / "logs" / f"verification-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.report_file = self.report_dir / "verification-report.md"
        self.results: List[Dict] = []
        
        # Load config
        self.load_config()
        
    def load_config(self):
        """Load board configuration from JSON file"""
        if not self.config_file.exists():
            self.log(f"ERROR: Config file not found: {self.config_file}", "ERROR")
            sys.exit(1)
        
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
            
            self.boards = config.get("boards", [])
            if not self.boards:
                self.log("ERROR: No boards defined in config file", "ERROR")
                sys.exit(1)
            
            self.log(f"Loaded {len(self.boards)} boards from config")
            for board in self.boards:
                self.log(f"  - {board['name']}: {board['serial_port']}")
        except Exception as e:
            self.log(f"ERROR: Failed to load config: {e}", "ERROR")
            sys.exit(1)
        
    def log(self, message: str, level: str = "INFO"):
        """Log message with timestamp"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{timestamp} [{level}] {message}")
        
    def run_command(self, cmd: List[str], timeout: Optional[int] = None, cwd: Optional[Path] = None) -> Tuple[int, str, str]:
        """Run command and return (returncode, stdout, stderr)"""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd or self.script_dir
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Command timed out after {timeout} seconds"
        except Exception as e:
            return -1, "", str(e)
    
    def test_1_invalid_config(self) -> Dict:
        """Test 1: Error Handling with Invalid Configuration"""
        self.log("=== Test 1: Error Handling with Invalid Configuration ===")
        test_results = []
        
        # Create invalid config
        invalid_config = {
            "serial_port": "/dev/serial-incorrect-device-node",
            "baudrate": 921600,
            "relay_ip": "192.168.1.225",
            "image_path": "nonexistent.img"
        }
        config_file = self.script_dir / "aml-burn-tool-config.json"
        
        try:
            # Save invalid config
            with open(config_file, 'w') as f:
                json.dump(invalid_config, f, indent=2)
            self.log(f"Created invalid config file: {config_file}")
            
            # Test collect_board_info.py with invalid config
            self.log("Testing collect_board_info.py with invalid config...")
            returncode, stdout, stderr = self.run_command(
                ["./collect_board_info.py"],
                timeout=10
            )
            
            test_results.append({
                "test": "collect_board_info.py with invalid config",
                "passed": returncode != 0 and ("error" in stdout.lower() or "error" in stderr.lower() or "could not open" in stdout.lower() or "could not open" in stderr.lower()),
                "returncode": returncode,
                "output": stdout + stderr
            })
            
            # Clean up
            if config_file.exists():
                config_file.unlink()
                self.log("Cleaned up invalid config file")
                
        except Exception as e:
            test_results.append({
                "test": "Test 1 setup",
                "passed": False,
                "error": str(e)
            })
        
        passed = all(r["passed"] for r in test_results)
        return {
            "test_name": "Test 1: Error Handling with Invalid Configuration",
            "passed": passed,
            "results": test_results
        }
    
    def test_2_invalid_arguments(self) -> Dict:
        """Test 2: Error Handling with Invalid Arguments"""
        self.log("=== Test 2: Error Handling with Invalid Arguments ===")
        test_results = []
        
        # Test invalid serial port
        self.log("Testing invalid serial port...")
        returncode, stdout, stderr = self.run_command(
            ["./collect_board_info.py", "--serial", "/dev/nonexistent"],
            timeout=10
        )
        test_results.append({
            "test": "Invalid serial port",
            "passed": returncode != 0,
            "returncode": returncode,
            "output": stdout + stderr
        })
        
        # Test invalid baudrate
        self.log("Testing invalid baudrate...")
        returncode, stdout, stderr = self.run_command(
            ["./collect_board_info.py", "--serial", "/dev/serial-signify-pro", "--baudrate", "invalid"],
            timeout=10
        )
        test_results.append({
            "test": "Invalid baudrate",
            "passed": returncode != 0 and ("error" in stdout.lower() or "error" in stderr.lower()),
            "returncode": returncode,
            "output": stdout + stderr
        })
        
        passed = all(r["passed"] for r in test_results)
        return {
            "test_name": "Test 2: Error Handling with Invalid Arguments",
            "passed": passed,
            "results": test_results
        }
    
    def test_3_login_logout(self) -> Dict:
        """Test 3: Login and Logout Control"""
        self.log("=== Test 3: Login and Logout Control ===")
        test_results = []
        
        for board in self.boards:
            board_name = board["name"]
            serial_port = board["serial_port"]
            self.log(f"Testing {board_name} ({serial_port})...")
            
            # 3.1: Logout test
            self.log(f"  3.1: Logout test for {board_name}...")
            returncode, stdout, stderr = self.run_command(
                ["./logout.py", "--serial", serial_port],
                timeout=30
            )
            logout_passed = returncode == 0 and ("login prompt" in stdout.lower() or "login:" in stdout.lower() or "login:" in stderr.lower())
            test_results.append({
                "test": f"Logout test ({board_name})",
                "passed": logout_passed,
                "returncode": returncode,
                "output": stdout + stderr
            })
            
            # 3.2: Script execution after logout
            self.log(f"  3.2: Testing collect_board_info.py after logout ({board_name})...")
            returncode, stdout, stderr = self.run_command(
                ["./collect_board_info.py", "--serial", serial_port],
                timeout=300
            )
            after_logout_passed = returncode == 0 and ("collection complete" in stdout.lower() or "markdown file generated" in stdout.lower())
            test_results.append({
                "test": f"collect_board_info.py after logout ({board_name})",
                "passed": after_logout_passed,
                "returncode": returncode,
                "output": stdout[-500:] + stderr[-500:] if len(stdout) > 500 else stdout + stderr
            })
            
            # 3.3: Script execution when already logged in
            self.log(f"  3.3: Testing when already logged in ({board_name})...")
            returncode, stdout, stderr = self.run_command(
                ["./collect_board_info.py", "--serial", serial_port],
                timeout=300
            )
            logged_in_passed = returncode == 0 and ("collection complete" in stdout.lower() or "markdown file generated" in stdout.lower())
            test_results.append({
                "test": f"collect_board_info.py when logged in ({board_name})",
                "passed": logged_in_passed,
                "returncode": returncode,
                "output": stdout[-500:] + stderr[-500:] if len(stdout) > 500 else stdout + stderr
            })
        
        passed = all(r["passed"] for r in test_results)
        return {
            "test_name": "Test 3: Login and Logout Control",
            "passed": passed,
            "results": test_results
        }
    
    def test_4_burn_test(self) -> Dict:
        """Test 4: Burn Test"""
        self.log("=== Test 4: Burn Test ===")
        self.log("NOTE: Burn tests require actual burn process and should be run manually when needed.")
        self.log("Skipping automated burn tests for safety.")
        
        return {
            "test_name": "Test 4: Burn Test",
            "passed": None,  # Skipped
            "results": [{
                "test": "Burn test",
                "passed": None,
                "note": "Skipped - requires manual execution"
            }]
        }
    
    def test_5_report_correctness(self) -> Dict:
        """Test 5: Report Correctness Test"""
        self.log("=== Test 5: Report Correctness Test ===")
        test_results = []
        
        for board in self.boards:
            board_name = board["name"]
            serial_port = board["serial_port"]
            self.log(f"Testing {board_name} ({serial_port})...")
            
            # Run collect_board_info.py
            returncode, stdout, stderr = self.run_command(
                ["./collect_board_info.py", "--serial", serial_port],
                timeout=300
            )
            
            if returncode != 0:
                test_results.append({
                    "test": f"collect_board_info.py execution ({board_name})",
                    "passed": False,
                    "returncode": returncode,
                    "output": stdout + stderr
                })
                continue
            
            # Find generated files
            logs_dir = self.script_dir / "logs"
            board_info_dirs = sorted([d for d in logs_dir.glob("board-info-*") if d.is_dir()], key=lambda x: x.stat().st_mtime, reverse=True)
            
            if not board_info_dirs:
                test_results.append({
                    "test": f"Report files generated ({board_name})",
                    "passed": False,
                    "error": "No board-info directories found"
                })
                continue
            
            latest_dir = board_info_dirs[0]
            md_file = latest_dir / "board-info.md"
            pdf_file = latest_dir / "board-info.pdf"
            
            # Check files exist
            md_exists = md_file.exists()
            pdf_exists = pdf_file.exists()
            
            test_results.append({
                "test": f"Markdown file exists ({board_name})",
                "passed": md_exists,
                "file": str(md_file) if md_exists else None
            })
            
            test_results.append({
                "test": f"PDF file exists ({board_name})",
                "passed": pdf_exists,
                "file": str(pdf_file) if pdf_exists else None
            })
            
            # Check report content if MD exists
            if md_exists:
                try:
                    with open(md_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Check for required sections
                    required_sections = [
                        "Operating System Information",
                        "Board Hardware Information",
                        "Storage Information",
                        "Network Information",
                        "Kernel Information",
                        "Debug Information"
                    ]
                    
                    sections_found = [section for section in required_sections if section in content]
                    test_results.append({
                        "test": f"Required sections present ({board_name})",
                        "passed": len(sections_found) == len(required_sections),
                        "sections_found": len(sections_found),
                        "sections_required": len(required_sections),
                        "missing": [s for s in required_sections if s not in content]
                    })
                    
                    # Check file size
                    file_size = md_file.stat().st_size
                    test_results.append({
                        "test": f"Report file size ({board_name})",
                        "passed": file_size > 1000,  # At least 1KB
                        "size_bytes": file_size
                    })
                    
                except Exception as e:
                    test_results.append({
                        "test": f"Report content check ({board_name})",
                        "passed": False,
                        "error": str(e)
                    })
        
        passed = all(r["passed"] for r in test_results if r["passed"] is not None)
        return {
            "test_name": "Test 5: Report Correctness Test",
            "passed": passed,
            "results": test_results
        }
    
    def generate_report(self):
        """Generate markdown report from test results"""
        md_lines = []
        md_lines.append("# Multi-Board Verification Test Report")
        md_lines.append("")
        md_lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        md_lines.append(f"**Config File:** {self.config_file}")
        md_lines.append(f"**Boards Tested:** {len(self.boards)}")
        md_lines.append("")
        md_lines.append("## Boards Configuration")
        md_lines.append("")
        for board in self.boards:
            md_lines.append(f"- **{board['name']}**:")
            md_lines.append(f"  - Serial Port: `{board['serial_port']}`")
            md_lines.append(f"  - Baudrate: {board['baudrate']}")
            md_lines.append(f"  - Relay IP: {board.get('relay_ip', 'None')}")
            md_lines.append(f"  - Default Image: `{board.get('default_image', 'N/A')}`")
            md_lines.append("")
        md_lines.append("---")
        md_lines.append("")
        
        # Summary
        total_tests = len(self.results)
        passed_tests = sum(1 for r in self.results if r.get("passed") is True)
        failed_tests = sum(1 for r in self.results if r.get("passed") is False)
        skipped_tests = sum(1 for r in self.results if r.get("passed") is None)
        
        md_lines.append("## Summary")
        md_lines.append("")
        md_lines.append(f"- **Total Tests:** {total_tests}")
        md_lines.append(f"- **Passed:** {passed_tests} ✅")
        md_lines.append(f"- **Failed:** {failed_tests} ❌")
        md_lines.append(f"- **Skipped:** {skipped_tests} ⏭️")
        md_lines.append("")
        
        if passed_tests == total_tests - skipped_tests:
            md_lines.append("**Overall Status:** ✅ **ALL TESTS PASSED**")
        elif failed_tests > 0:
            md_lines.append("**Overall Status:** ❌ **SOME TESTS FAILED**")
        else:
            md_lines.append("**Overall Status:** ⚠️ **SOME TESTS SKIPPED**")
        md_lines.append("")
        md_lines.append("---")
        md_lines.append("")
        
        # Detailed results
        md_lines.append("## Detailed Results")
        md_lines.append("")
        
        for result in self.results:
            test_name = result["test_name"]
            passed = result.get("passed")
            
            if passed is True:
                status = "✅ PASSED"
            elif passed is False:
                status = "❌ FAILED"
            else:
                status = "⏭️ SKIPPED"
            
            md_lines.append(f"### {test_name}")
            md_lines.append("")
            md_lines.append(f"**Status:** {status}")
            md_lines.append("")
            
            if "results" in result:
                md_lines.append("#### Sub-tests:")
                md_lines.append("")
                for sub_result in result["results"]:
                    sub_test = sub_result.get("test", "Unknown")
                    sub_passed = sub_result.get("passed")
                    
                    if sub_passed is True:
                        sub_status = "✅"
                    elif sub_passed is False:
                        sub_status = "❌"
                    else:
                        sub_status = "⏭️"
                    
                    md_lines.append(f"- {sub_status} **{sub_test}**")
                    
                    if "returncode" in sub_result:
                        md_lines.append(f"  - Return code: {sub_result['returncode']}")
                    if "error" in sub_result:
                        md_lines.append(f"  - Error: {sub_result['error']}")
                    if "file" in sub_result and sub_result["file"]:
                        md_lines.append(f"  - File: `{sub_result['file']}`")
                    if "sections_found" in sub_result:
                        md_lines.append(f"  - Sections found: {sub_result['sections_found']}/{sub_result['sections_required']}")
                    if "missing" in sub_result and sub_result["missing"]:
                        md_lines.append(f"  - Missing sections: {', '.join(sub_result['missing'])}")
                    if "size_bytes" in sub_result:
                        size_kb = sub_result["size_bytes"] / 1024
                        md_lines.append(f"  - File size: {size_kb:.1f} KB")
                    if "output" in sub_result and sub_result["output"]:
                        # Show last few lines of output
                        output_lines = sub_result["output"].split("\n")
                        if len(output_lines) > 10:
                            md_lines.append(f"  - Output (last 10 lines):")
                            md_lines.append("    ```")
                            for line in output_lines[-10:]:
                                md_lines.append(f"    {line}")
                            md_lines.append("    ```")
                        else:
                            md_lines.append(f"  - Output:")
                            md_lines.append("    ```")
                            for line in output_lines:
                                md_lines.append(f"    {line}")
                            md_lines.append("    ```")
                    md_lines.append("")
            
            md_lines.append("---")
            md_lines.append("")
        
        # End of report
        md_lines.append("## End of Report")
        md_lines.append("")
        md_lines.append(f"*Report generated by multiboard_verification.py on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        
        # Write report
        with open(self.report_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(md_lines))
        
        self.log(f"Report generated: {self.report_file}")
        return self.report_file
    
    def run_all_tests(self):
        """Run all verification tests"""
        self.log("Starting multi-board verification...")
        self.log(f"Config file: {self.config_file}")
        self.log(f"Boards: {len(self.boards)}")
        self.log(f"Report will be saved to: {self.report_file}")
        self.log("")
        
        start_time = time.time()
        
        # Run all tests
        self.results.append(self.test_1_invalid_config())
        self.results.append(self.test_2_invalid_arguments())
        self.results.append(self.test_3_login_logout())
        self.results.append(self.test_4_burn_test())
        self.results.append(self.test_5_report_correctness())
        
        elapsed = time.time() - start_time
        
        self.log("")
        self.log(f"All tests completed in {elapsed:.1f} seconds")
        
        # Generate report
        report_file = self.generate_report()
        
        # Open report
        try:
            subprocess.run(["xdg-open", str(report_file)], check=False)
            self.log(f"Opened report with xdg-open: {report_file}")
        except Exception as e:
            self.log(f"Could not open report automatically: {e}")
            self.log(f"Please open manually: {report_file}")
        
        return report_file

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Multi-Board Verification Script")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to multiboard_verification.json config file (default: ./multiboard_verification.json)"
    )
    args = parser.parse_args()
    
    runner = MultiBoardVerificationRunner(config_file=args.config)
    report_file = runner.run_all_tests()
    print(f"\n{'='*60}")
    print(f"Verification complete!")
    print(f"Report: {report_file}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()

