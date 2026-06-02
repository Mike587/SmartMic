# -*- coding: utf-8 -*-

#################################################################
# File        : MS_CD7_API_LoA_Test.py
# Author      : Mike Stebler
# Institution : ETH Zurich | ScopeM
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

import sys
import time
from pathlib import Path

# Import the functions from the main API module
try:
    from MS_CD7_API_LoA import (
        set_objective_set_optovar_sync,
        move_stage_to_new_xy_position,
        get_current_xy_stage_position,
        validate_xy_position,
        get_current_objective_and_optovar
    )
except ImportError as e:
    print(f"[ERROR] Failed to import MS_CD7_API_LoA module: {e}")
    print("Make sure MS_CD7_API_LoA.py is in the same directory or Python path.")
    sys.exit(1)


def test_objective_and_optovar_switching() -> None:
    """
    Test the objective and optovar switching functions.
    
    This test:
    1. Moves to a safe middle position
    2. Performs 10 objective/optovar switches
    3. Verifies each switch completes successfully
    4. Provides detailed output for monitoring
    
    Note: The test cycles through different objective/optovar combinations.
    """
    print("=" * 60)
    print("Testing objective and optovar switching functions")
    print("=" * 60)
    
    # Define test sequences
    # Each test is (objective_nr, optovar_nr, description)
    # Note: 50x objective (position 4) is an immersion objective - needs longer timeouts
    test_sequences = [
        (2, 1, "5x objective with 2x tubelens"),
        (1, 2, "20x/0.7 objective with 1x tubelens"),
        (2, 1, "5x objective with 2x tubelens"),
        (3, 2, "20x/0.95 objective with 1x tubelens"),
        (2, 1, "5x objective with 2x tubelens"),
        (4, 1, "50x/1.2 immersion objective with 2x tubelens"),
        (2, 1, "5x objective with 2x tubelens"),
        (1, 3, "20x/0.7 objective with 0.5x tubelens"),
        (2, 1, "5x objective with 2x tubelens"),
        (3, 1, "20x/0.95 objective with 2x tubelens"),
        (2, 1, "5x objective with 2x tubelens"),
        (4, 2, "50x/1.2 immersion objective with 1x tubelens"),
        (2, 1, "5x objective with 2x tubelens"),
    ]
    
    print("\n[INFO] Starting objective/optovar switching test...")
    print(f"[INFO] Will perform {len(test_sequences)} switches")
    print("[WARNING] Test includes 50x immersion objective - will take longer")
    
    # Step 1: Move to a safe middle position
    print("\n" + "-" * 40)
    print("Step 1: Moving to safe middle position...")
    print("-" * 40)
    
    safe_x = 0.05  # meters
    safe_y = 0.045  # meters
    
    try:
        # Validate and move to safe position
        from MS_CD7_API_LoA import validate_xy_position, move_stage_to_new_xy_position
        is_valid, message = validate_xy_position(safe_x, safe_y)
        if is_valid:
            print(f"  Moving to X={safe_x:.3f}m, Y={safe_y:.3f}m")
            move_stage_to_new_xy_position(safe_x, safe_y)
            print(f"  [SUCCESS] Moved to safe position")
        else:
            print(f"  [WARNING] Safe position validation failed: {message}")
            print("  [INFO] Continuing test anyway...")
    except Exception as e:
        print(f"  [WARNING] Could not move to safe position: {e}")
        print("  [INFO] Continuing test anyway...")
    
    # Give stage time to settle
    time.sleep(2.0)
    
    # Track test results and details
    test_results = []
    test_details = []
    
    # Step 2: Perform objective/optovar switches
    print("\n" + "-" * 40)
    print("Step 2: Testing objective/optovar switching...")
    print("-" * 40)
    
    for i, (obj_nr, opt_nr, description) in enumerate(test_sequences):
        print(f"\nTest {i + 1}/{len(test_sequences)}:")
        print(f"  Switching to: {description}")
        print(f"  Objective: {obj_nr}, Optovar: {opt_nr}")
        
        try:
            # Record start time
            start_time = time.time()
            
            # Adjust timeout for immersion objectives
            if obj_nr == 4:  # 50x immersion objective
                timeout = 120.0  # Much longer timeout for immersion
                print("  [INFO] Using extended timeout (120s) for 50x immersion objective")
            else:
                timeout = 60.0
            
            # Perform the switch
            success = set_objective_set_optovar_sync(
                objective_nr=obj_nr,
                optovar_nr=opt_nr,
                timeout_seconds=timeout
            )
            
            # Calculate elapsed time
            elapsed_time = time.time() - start_time
            
            if success:
                print(f"  [SUCCESS] Switch completed in {elapsed_time:.1f} seconds")
                test_results.append(True)
            else:
                print(f"  [FAIL] Switch timed out or failed after {elapsed_time:.1f} seconds")
                test_results.append(False)
            
            # Get current position to verify
            try:
                from MS_CD7_API_LoA import get_current_objective_and_optovar
                current_obj, current_opt = get_current_objective_and_optovar()
                print(f"  Current position: Objective={current_obj}, Optovar={current_opt}")
                
                # Verify the switch was successful
                if current_obj == obj_nr and current_opt == opt_nr:
                    print("  [VERIFIED] Position matches target")
                    verified = True
                else:
                    print(f"  [WARNING] Position mismatch: expected ({obj_nr}, {opt_nr}), got ({current_obj}, {current_opt})")
                    verified = False
            except Exception as e:
                print(f"  [WARNING] Could not verify position: {e}")
                verified = False
            
            # Store detailed results
            test_details.append({
                'index': i + 1,
                'objective': obj_nr,
                'optovar': opt_nr,
                'description': description,
                'success': success,
                'verified': verified,
                'elapsed_time': elapsed_time,
                'current_obj': current_obj if 'current_obj' in locals() else None,
                'current_opt': current_opt if 'current_opt' in locals() else None
            })
            
        except Exception as e:
            print(f"  [ERROR] Test failed with exception: {e}")
            test_results.append(False)
            test_details.append({
                'index': i + 1,
                'objective': obj_nr,
                'optovar': opt_nr,
                'description': description,
                'error': str(e)
            })
        
        # Wait between switches to allow hardware to settle
        # Longer wait for immersion objectives
        if obj_nr == 4:  # 50x immersion objective
            print("  [INFO] 50x immersion objective detected - waiting longer...")
            time.sleep(5.0)
        else:
            time.sleep(2.0)
    
    # Step 3: Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    passed = sum(test_results)
    total = len(test_results)
    
    print(f"\nTests completed: {total}")
    print(f"Tests passed:    {passed}")
    print(f"Tests failed:    {total - passed}")
    
    if passed == total:
        print("\n[SUCCESS] All objective/optovar switches completed successfully!")
    else:
        print(f"\n[WARNING] {total - passed} switch(es) failed. Check the logs above for details.")
    
    # Print detailed summary table
    print("\n" + "=" * 60)
    print("Detailed Switching Summary")
    print("=" * 60)
    print("\nTest | Objective | Optovar | Description" + " " * 30 + "| Success | Verified | Time (s)")
    print("-" * 110)
    
    for detail in test_details:
        if 'error' in detail:
            print(f"{detail['index']:4d} | {detail['objective']:9d} | {detail['optovar']:7d} | "
                  f"{detail['description'][:40]:40s} | {'ERROR':7s} | {'ERROR':8s} | {'ERROR':8s}")
        else:
            success_str = "YES" if detail['success'] else "NO"
            verified_str = "YES" if detail['verified'] else "NO"
            print(f"{detail['index']:4d} | {detail['objective']:9d} | {detail['optovar']:7d} | "
                  f"{detail['description'][:40]:40s} | {success_str:7s} | {verified_str:8s} | {detail['elapsed_time']:8.1f}")
    
    # Calculate overall statistics
    if test_details and 'error' not in test_details[0]:
        successful_tests = [d for d in test_details if d.get('success', False)]
        avg_time = sum(d['elapsed_time'] for d in successful_tests) / len(successful_tests) if successful_tests else 0
        max_time = max(d['elapsed_time'] for d in test_details if 'elapsed_time' in d) if test_details else 0
        
        print("\n" + "-" * 110)
        print(f"Statistics: Average switch time (successful): {avg_time:.1f} seconds")
        print(f"           Maximum switch time: {max_time:.1f} seconds")
        print(f"           Successful switches: {len(successful_tests)}/{total}")
    
    # Return to 5x objective (safe configuration)
    print("\n" + "-" * 40)
    print("Returning to 5x objective (safe configuration)...")
    print("-" * 40)
    
    try:
        success = set_objective_set_optovar_sync(
            objective_nr=2,
            optovar_nr=1,
            timeout_seconds=60.0
        )
        if success:
            print("[SUCCESS] Returned to 5x objective with 2x tubelens")
        else:
            print("[WARNING] Failed to return to 5x objective")
    except Exception as e:
        print(f"[INFO] Could not return to 5x objective: {e}")
    
    print("\n" + "=" * 60)
    print("Objective/optovar test complete!")
    print("=" * 60)


def test_stage_movement_and_position() -> None:
    """
    Test the stage movement and position retrieval functions.
    
    This test:
    1. Changes to the 5x objective (position 2 with optovar 1)
    2. Moves to 10 hardcoded positions from the original C.json file
    3. Retrieves the current position after each move
    4. Verifies the positions match within a tolerance
    5. Provides detailed output for monitoring
    
    Note: The test uses hardcoded positions to avoid file dependency.
    """
    print("=" * 60)
    print("Testing stage movement and position functions")
    print("=" * 60)
    
    # Hardcoded positions from C.json (first 10 entries)
    # Each position is [x, y] in meters
    test_positions = [
        [0.00675, 0.01525],
        [0.01125, 0.01525],
        [0.01575, 0.01525],
        [0.02025, 0.01525],
        [0.02475, 0.01525],
        [0.02925, 0.01525],
        [0.03375, 0.01525],
        [0.03825, 0.01525],
        [0.04275, 0.01525],
        [0.04725, 0.01525]
    ]
    
    # Tolerance for position matching (in meters)
    # Stage positioning may not be exact due to mechanical tolerances
    POSITION_TOLERANCE = 0.0005  # 0.5 mm tolerance
    
    print("\n[INFO] Starting stage movement test...")
    print(f"[INFO] Will test {len(test_positions)} positions")
    print(f"[INFO] Position tolerance: {POSITION_TOLERANCE * 1000:.2f} mm")
    
    # Step 1: Change to 5x objective (position 2) with optovar 1
    print("\n" + "-" * 40)
    print("Step 1: Changing to 5x objective...")
    print("-" * 40)
    
    try:
        # 5x objective is position 2, using optovar 1 (2x Tubelens)
        success = set_objective_set_optovar_sync(
            objective_nr=2,
            optovar_nr=1,
            timeout_seconds=30.0
        )
        
        if success:
            print("[SUCCESS] Objective changed to 5x successfully")
        else:
            print("[WARNING] Objective change may have timed out")
            print("[INFO] Continuing with test anyway...")
    except Exception as e:
        print(f"[ERROR] Failed to change objective: {e}")
        print("[INFO] Continuing with test anyway...")
    
    # Give hardware a moment to settle
    time.sleep(2.0)
    
    # Track test results and details
    test_results = []
    test_details = []
    
    # Step 2: Test each position
    print("\n" + "-" * 40)
    print("Step 2: Testing stage movement...")
    print("-" * 40)
    
    for i, target_pos in enumerate(test_positions):
        target_x, target_y = target_pos
        
        print(f"\nTest {i + 1}/{len(test_positions)}:")
        print(f"  Target position: X={target_x:.6f}m, Y={target_y:.6f}m")
        
        # Validate the position before moving
        is_valid, message = validate_xy_position(target_x, target_y)
        if not is_valid:
            print(f"  [SKIP] Invalid position: {message}")
            test_results.append(False)
            continue
        
        try:
            # Move to the target position
            print("  Moving stage to target position...")
            move_stage_to_new_xy_position(target_x, target_y)
            
            # Give stage time to settle
            time.sleep(1.0)
            
            # Get current position
            print("  Retrieving current position...")
            current_pos = get_current_xy_stage_position()
            current_x, current_y = current_pos
            
            print(f"  Current position: X={current_x:.6f}m, Y={current_y:.6f}m")
            
            # Calculate differences
            diff_x = abs(current_x - target_x)
            diff_y = abs(current_y - target_y)
            
            try:
                print(f"  Differences: dX={diff_x:.6f}m, dY={diff_y:.6f}m")
            except UnicodeEncodeError:
                # Fallback for Windows console encoding issues
                print(f"  Differences: dX={diff_x:.6f}m, dY={diff_y:.6f}m")
            
            # Check if within tolerance
            if diff_x <= POSITION_TOLERANCE and diff_y <= POSITION_TOLERANCE:
                print("  [PASS] Position within tolerance")
                test_results.append(True)
            else:
                print("  [FAIL] Position outside tolerance")
                test_results.append(False)
            
            # Store detailed results for summary
            test_details.append({
                'index': i + 1,
                'target_x': target_x,
                'target_y': target_y,
                'current_x': current_x,
                'current_y': current_y,
                'diff_x': diff_x,
                'diff_y': diff_y,
                'passed': diff_x <= POSITION_TOLERANCE and diff_y <= POSITION_TOLERANCE
            })
                
        except Exception as e:
            print(f"  [ERROR] Test failed with exception: {e}")
            test_results.append(False)
            test_details.append({
                'index': i + 1,
                'target_x': target_x,
                'target_y': target_y,
                'error': str(e)
            })
        
        # Small delay between tests
        time.sleep(0.5)
    
    # Step 3: Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    passed = sum(test_results)
    total = len(test_results)
    
    print(f"\nTests completed: {total}")
    print(f"Tests passed:    {passed}")
    print(f"Tests failed:    {total - passed}")
    
    if passed == total:
        print("\n[SUCCESS] All tests passed! Stage functions are working correctly.")
    else:
        print(f"\n[WARNING] {total - passed} test(s) failed. Check the logs above for details.")
    
    # Print detailed summary table
    print("\n" + "=" * 60)
    print("Detailed Position Summary")
    print("=" * 60)
    print("\nTest | Target X (m)   | Target Y (m)   | Current X (m)  | Current Y (m)  | dX (mm)    | dY (mm)    | Status")
    print("-" * 100)
    
    for detail in test_details:
        if 'error' in detail:
            print(f"{detail['index']:4d} | {detail['target_x']:14.6f} | {detail['target_y']:14.6f} | {'ERROR':14s} | {'ERROR':14s} | {'ERROR':10s} | {'ERROR':10s} | FAILED")
        else:
            status = "PASS" if detail['passed'] else "FAIL"
            # Convert differences to millimeters for easier reading
            diff_x_mm = detail['diff_x'] * 1000
            diff_y_mm = detail['diff_y'] * 1000
            print(f"{detail['index']:4d} | {detail['target_x']:14.6f} | {detail['target_y']:14.6f} | "
                  f"{detail['current_x']:14.6f} | {detail['current_y']:14.6f} | "
                  f"{diff_x_mm:10.3f} | {diff_y_mm:10.3f} | {status}")
    
    # Calculate overall statistics
    if test_details and 'error' not in test_details[0]:
        avg_diff_x = sum(d['diff_x'] for d in test_details if 'diff_x' in d) / len([d for d in test_details if 'diff_x' in d])
        avg_diff_y = sum(d['diff_y'] for d in test_details if 'diff_y' in d) / len([d for d in test_details if 'diff_y' in d])
        max_diff_x = max(d['diff_x'] for d in test_details if 'diff_x' in d)
        max_diff_y = max(d['diff_y'] for d in test_details if 'diff_y' in d)
        
        print("\n" + "-" * 100)
        print(f"Statistics: Average dX: {avg_diff_x * 1000:.3f} mm, Average dY: {avg_diff_y * 1000:.3f} mm")
        print(f"           Maximum dX: {max_diff_x * 1000:.3f} mm, Maximum dY: {max_diff_y * 1000:.3f} mm")
        print(f"           Tolerance:  {POSITION_TOLERANCE * 1000:.2f} mm")
    
    # Return to a safe position (optional)
    print("\n" + "-" * 40)
    print("Returning to a safe position...")
    print("-" * 40)
    
    try:
        # Move to a central, safe position
        safe_x = 0.05  # meters
        safe_y = 0.045  # meters
        
        try:
            is_valid, message = validate_xy_position(safe_x, safe_y)
            if is_valid:
                move_stage_to_new_xy_position(safe_x, safe_y)
                print(f"[INFO] Moved to safe position: X={safe_x:.3f}m, Y={safe_y:.3f}m")
            else:
                print(f"[INFO] Safe position validation failed: {message}")
        except Exception as e:
            print(f"[INFO] Could not return to safe position: {e}")
    except Exception as e:
        print(f"[INFO] Could not return to safe position: {e}")
    
    print("\n" + "=" * 60)
    print("Test complete!")
    print("=" * 60)


def main() -> None:
    """
    Main entry point for the test script.
    """
    print("\n" + "=" * 60)
    print("ZEISS CD7 Microscope Test Suite")
    print("=" * 60)
    print("\nAvailable tests:")
    print("  1. Stage movement and position test")
    print("  2. Objective and optovar switching test")
    print("  3. Run all tests")
    print("\nWARNING: These tests will move microscope hardware!")
    print("Ensure the area is clear and safe to operate.")
    
    try:
        # Get user choice
        choice = input("\nEnter test number (1-3) or 'q' to quit: ").strip()
        
        if choice.lower() == 'q':
            print("\n[INFO] Test suite cancelled by user.")
            sys.exit(0)
        
        if choice not in ['1', '2', '3']:
            print(f"\n[ERROR] Invalid choice: {choice}")
            print("Please enter 1, 2, 3, or 'q' to quit.")
            sys.exit(1)
        
        print("\nPress Ctrl+C within 5 seconds to abort...")
        
        # Countdown before starting
        for i in range(5, 0, -1):
            print(f"Starting in {i}...", end='\r')
            time.sleep(1.0)
        print("\n")
        
        if choice == '1':
            test_stage_movement_and_position()
        elif choice == '2':
            test_objective_and_optovar_switching()
        elif choice == '3':
            print("\n" + "=" * 60)
            print("Running ALL tests")
            print("=" * 60)
            
            # Run stage test
            test_stage_movement_and_position()
            
            # Wait between tests
            print("\n" + "=" * 60)
            print("Preparing for objective/optovar test...")
            print("=" * 60)
            time.sleep(3.0)
            
            # Run objective test
            test_objective_and_optovar_switching()
            
            print("\n" + "=" * 60)
            print("ALL TESTS COMPLETE!")
            print("=" * 60)
        
    except KeyboardInterrupt:
        print("\n\n[INFO] Test aborted by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] Test failed with unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
