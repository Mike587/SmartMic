import sys
import time
from pathlib import Path

# Add the parent directory to sys.path to import zenapi_MS_Messaround
sys.path.append(str(Path(__file__).parent))

import MS_CD7_API_LoA as ms

'''
Make sure the CD7 is:
    - connected
    - a 384 WP is loaded
    - stage is in Well H12
'''

def test_objective_changes():
    """
    Test changing objectives and optovars, then verify the positions match.
    
    This function tests various objective and optovar combinations,
    changes them using set_objective_set_optovar_sync,
    reads back the current positions, and verifies they match.
    
    Returns:
        tuple: (all_passed, results, test_times) where all_passed is a boolean indicating
               if all tests passed, results is a list of test results, and test_times is a list
               of tuples (description, time_taken, from_obj, from_opt, to_obj, to_opt, status)
    """
    print("Starting objective change tests...")
    
    # Define test cases: (objective_nr, optovar_nr, description)
    test_cases = [
        (1, 1, "20x/0.7 with 2x Tubelens"),
        (2, 2, "5x/0.35 with 1x Tubelens"),
        (3, 3, "20x/0.95 with 0.5x Tubelens"),
        (4, 1, "50x/1.2 (Immersion) with 2x Tubelens"),
        (2, 3, "5x/0.35 with 0.5x Tubelens"),
        (1, 2, "20x/0.7 with 1x Tubelens")
    ]
    
    results = []
    test_times = []
    all_passed = True
    
    for obj_nr, opt_nr, description in test_cases:
        print(f"\nTesting: {description} (Objective: {obj_nr}, Optovar: {opt_nr})")
        
        # Get current position before change
        current_obj_before, current_opt_before = ms.get_current_objective_and_optovar()
        print(f"  Before change - Objective: {current_obj_before}, Optovar: {current_opt_before}")
        
        # Special handling for 50x immersion objective
        if obj_nr == 4:  # Moving TO 50x immersion objective
            print("  [INFO] 50x is an immersion objective - allowing extra time for switching")
            print("  [INFO] Immersion objectives require special handling and take longer")
            timeout = 180.0  # Much longer timeout for immersion objective
            max_retries = 5
        elif current_obj_before == 4:  # Moving FROM 50x immersion objective
            print("  [INFO] Moving away from 50x immersion objective")
            print("  [INFO] Immersion may leave residue that could affect focusing")
            print("  [INFO] Hardware may need additional time for cleaning/safety checks")
            timeout = 180.0  # Longer timeout when leaving immersion
            max_retries = 5
        else:
            timeout = 80.0
            max_retries = 3
        
        # Change to the target objective and optovar
        print(f"  Changing to Objective {obj_nr}, Optovar {opt_nr} (timeout: {timeout}s, max_retries: {max_retries})...")
        
        # Start timing
        start_time = time.time()
        
        try:
            success = ms.set_objective_set_optovar_sync(obj_nr, opt_nr, timeout_seconds=timeout, max_retries=max_retries)
        except Exception as e:
            end_time = time.time()
            time_taken = end_time - start_time
            print(f"  [FAIL] Exception while changing to Objective {obj_nr}, Optovar {opt_nr}: {e}")
            results.append((obj_nr, opt_nr, description, False, f"Exception: {e}"))
            test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                              obj_nr, opt_nr, "FAIL"))
            all_passed = False
            
            # Special handling for immersion objective errors
            if obj_nr == 4 or current_obj_before == 4:
                print("  [INFO] Error with immersion objective - this is expected behavior")
                print("  [INFO] The 50x immersion objective has special requirements:")
                print("  [INFO] 1. May need manual cleaning of immersion residue")
                print("  [INFO] 2. Hardware safety interlocks may prevent certain moves")
                print("  [INFO] 3. Switching times are significantly longer")
            
            continue
        
        end_time = time.time()
        time_taken = end_time - start_time
        
        if not success:
            print(f"  [FAIL] Timeout while changing to Objective {obj_nr}, Optovar {opt_nr}")
            results.append((obj_nr, opt_nr, description, False, "Timeout during change"))
            test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                              obj_nr, opt_nr, "FAIL"))
            all_passed = False
            continue
        
        # Get position after change
        current_obj_after, current_opt_after = ms.get_current_objective_and_optovar()
        print(f"  After change - Objective: {current_obj_after}, Optovar: {current_opt_after}")
        
        # Verify the change was successful
        if current_obj_after == obj_nr and current_opt_after == opt_nr:
            print(f"  [OK] Successfully changed to {description} (took {time_taken:.2f}s)")
            results.append((obj_nr, opt_nr, description, True, "Match successful"))
            test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                              obj_nr, opt_nr, "PASS"))
        else:
            print(f"  [FAIL] Mismatch: Expected Objective {obj_nr}, Optovar {opt_nr}, "
                  f"got Objective {current_obj_after}, Optovar {current_opt_after} (took {time_taken:.2f}s)")
            results.append((obj_nr, opt_nr, description, False, 
                           f"Expected ({obj_nr}, {opt_nr}), got ({current_obj_after}, {current_opt_after})"))
            test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                              obj_nr, opt_nr, "FAIL"))
            all_passed = False
        
        # Add a variable delay between tests based on the objective
        # Special handling for 50x immersion objective
        if obj_nr == 4:  # Just moved TO 50x immersion objective
            print("  [INFO] 50x immersion objective - allowing extended time for immersion settling")
            print("  [INFO] Immersion objectives need time for liquid stabilization")
            time.sleep(15)  # Extended delay for immersion settling
        elif current_obj_before == 4:  # Just moved FROM 50x immersion objective
            print("  [INFO] Moved from 50x immersion - allowing time for potential cleaning")
            print("  [INFO] Immersion residue may need to dry or be cleaned")
            time.sleep(10)  # Longer delay after immersion objective
        else:
            time.sleep(2)  # Standard delay for non-immersion objectives
    
    # Print detailed summary table
    print("\n" + "="*80)
    print("OBJECTIVE CHANGE TEST SUMMARY - DETAILED TIMING")
    print("="*80)
    print(f"{'Test Description':<40} {'From':<10} {'To':<10} {'Time (s)':<12} {'Status':<8}")
    print("-"*80)
    
    total_time = 0
    for desc, t_taken, from_obj, from_opt, to_obj, to_opt, status in test_times:
        from_str = f"{from_obj}/{from_opt}"
        to_str = f"{to_obj}/{to_opt}"
        print(f"{desc:<40} {from_str:<10} {to_str:<10} {t_taken:<12.2f} {status:<8}")
        total_time += t_taken
    
    print("-"*80)
    print(f"{'TOTAL TIME':<40} {'':<10} {'':<10} {total_time:<12.2f}")
    print("="*80)
    
    # Print failure details if any
    failures = [(desc, t_taken, from_obj, from_opt, to_obj, to_opt) 
                for desc, t_taken, from_obj, from_opt, to_obj, to_opt, status in test_times 
                if status == "FAIL"]
    
    if failures:
        print("\nFAILED TESTS DETAILS:")
        print("-"*60)
        for desc, t_taken, from_obj, from_opt, to_obj, to_opt in failures:
            print(f"Test: {desc}")
            print(f"  Change from: Objective {from_obj}, Optovar {from_opt}")
            print(f"  Change to:   Objective {to_obj}, Optovar {to_opt}")
            print(f"  Time taken:  {t_taken:.2f} seconds")
            print("-"*40)
    
    # Print simple summary
    print("\n" + "="*60)
    print("Objective Change Test Summary:")
    print("="*60)
    for obj_nr, opt_nr, desc, passed, msg in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status}: {desc} - {msg}")
    
    if all_passed:
        print("\n[OK] All objective change tests passed!")
    else:
        print("\n[FAIL] Some objective change tests failed.")
    
    return all_passed, results, test_times

def test_immersion_exit_patterns():
    """
    Test specific patterns for moving away from 50x immersion objective.
    
    Hypothesis: Moving away from 50x immersion objective has specific limitations.
    This test will try different approaches to understand what works and what doesn't.
    
    Returns:
        tuple: (all_passed, results, test_times)
    """
    print("\n" + "="*80)
    print("IMMERSION EXIT PATTERN TESTS")
    print("="*80)
    print("Testing hypothesis: Moving away from 50x immersion has limitations")
    print("="*80)
    
    # Start from 50x if not already there
    current_obj, current_opt = ms.get_current_objective_and_optovar()
    print(f"Current position before test: Objective {current_obj}, Optovar {current_opt}")
    
    if current_obj != 4:
        print("Moving to 50x immersion objective first...")
        success = ms.set_objective_set_optovar_sync(4, 1, timeout_seconds=120.0)
        if not success:
            print("[FAIL] Could not move to 50x immersion objective to start test")
            # Try to get current position again
            current_obj, current_opt = ms.get_current_objective_and_optovar()
            print(f"Current position after failed move: Objective {current_obj}, Optovar {current_opt}")
            return False, [], []
        time.sleep(10)  # Longer wait after moving to immersion
        # Verify we're at 50x
        verify_obj, verify_opt = ms.get_current_objective_and_optovar()
        print(f"Verified position: Objective {verify_obj}, Optovar {verify_opt}")
        if verify_obj != 4:
            print("[WARN] Not at 50x after move attempt")
    else:
        print("Already at 50x immersion objective")
        time.sleep(5)  # Wait for stabilization
    
    # Define test cases: (description, objective_nr, optovar_nr, approach)
    test_cases = [
        ("Direct to 20x/0.7 with 2x", 1, 1, "Direct move"),
        ("Direct to 5x/0.35 with 1x", 2, 2, "Direct move"),
        ("Direct to 20x/0.95 with 0.5x", 3, 3, "Direct move"),
        ("To 20x/0.7 then to 5x/0.35", 2, 2, "Via 20x intermediate"),
        ("To 20x/0.95 then to 20x/0.7", 1, 1, "Via 20x/0.95 intermediate"),
        ("Same objective, change optovar only", 4, 2, "Optovar change only"),
        ("Same objective, different optovar", 4, 3, "Optovar change only"),
    ]
    
    results = []
    test_times = []
    all_passed = True
    
    for description, obj_nr, opt_nr, approach in test_cases:
        print(f"\nTest: {description}")
        print(f"  Approach: {approach}")
        print(f"  Target: Objective {obj_nr}, Optovar {opt_nr}")
        
        # Get current position before change
        try:
            current_obj_before, current_opt_before = ms.get_current_objective_and_optovar()
            # Check for invalid position
            if current_obj_before == 0 or current_opt_before == 0:
                print(f"  [WARN] Invalid position detected: Objective {current_obj_before}, Optovar {current_opt_before}")
                print(f"  [INFO] Retrying position query...")
                time.sleep(2)
                current_obj_before, current_opt_before = ms.get_current_objective_and_optovar()
        except Exception as e:
            print(f"  [ERROR] Failed to get current position: {e}")
            print(f"  [INFO] Skipping this test")
            results.append((description, False, f"Failed to get current position: {e}"))
            test_times.append((description, 0.0, 0, 0, obj_nr, opt_nr, "FAIL", approach))
            all_passed = False
            continue
        
        print(f"  Current: Objective {current_obj_before}, Optovar {current_opt_before}")
        
        # Skip if we're not at 50x for immersion exit tests
        if current_obj_before != 4 and "immersion" in description.lower():
            print(f"  [SKIP] Not at 50x immersion objective (at {current_obj_before})")
            print(f"  [INFO] This test requires starting from 50x")
            results.append((description, False, f"Not at 50x to start"))
            test_times.append((description, 0.0, current_obj_before, current_opt_before, 
                              obj_nr, opt_nr, "SKIP", approach))
            continue
        
        # For intermediate approaches
        if "intermediate" in approach.lower():
            if "20x intermediate" in approach:
                intermediate_obj, intermediate_opt = 1, 1  # 20x/0.7 with 2x
            elif "20x/0.95 intermediate" in approach:
                intermediate_obj, intermediate_opt = 3, 3  # 20x/0.95 with 0.5x
                    
            print(f"  Step 1: Moving to intermediate Objective {intermediate_obj}, Optovar {intermediate_opt}")
            start_time = time.time()
            try:
                success = ms.set_objective_set_optovar_sync(intermediate_obj, intermediate_opt, timeout_seconds=120.0, max_retries=3)
                end_time = time.time()
                time_taken = end_time - start_time
                
                if success:
                    print(f"  [OK] Intermediate move successful (took {time_taken:.2f}s)")
                    # Verify intermediate position
                    inter_obj, inter_opt = ms.get_current_objective_and_optovar()
                    if inter_obj == intermediate_obj and inter_opt == intermediate_opt:
                        print(f"  [OK] Verified at intermediate position")
                    else:
                        print(f"  [WARN] Intermediate position mismatch: got {inter_obj}/{inter_opt}")
                    
                    time.sleep(5)  # Wait after intermediate move
                    
                    # Now try final target
                    print(f"  Step 2: Moving to final target Objective {obj_nr}, Optovar {opt_nr}")
                    start_time2 = time.time()
                    try:
                        success2 = ms.set_objective_set_optovar_sync(obj_nr, opt_nr, timeout_seconds=100.0, max_retries=3)
                        end_time2 = time.time()
                        time_taken2 = end_time2 - start_time2
                        total_time = time_taken + time_taken2
                        
                        if success2:
                            # Verify final position
                            final_obj, final_opt = ms.get_current_objective_and_optovar()
                            if final_obj == obj_nr and final_opt == opt_nr:
                                print(f"  [OK] Final move successful (total {total_time:.2f}s)")
                                results.append((description, True, f"Success via intermediate"))
                                test_times.append((description, total_time, current_obj_before, current_opt_before, 
                                                  obj_nr, opt_nr, "PASS", approach))
                            else:
                                print(f"  [FAIL] Position mismatch: got {final_obj}/{final_opt}")
                                results.append((description, False, f"Position mismatch"))
                                test_times.append((description, total_time, current_obj_before, current_opt_before, 
                                                  obj_nr, opt_nr, "FAIL", approach))
                                all_passed = False
                        else:
                            print(f"  [FAIL] Final move timeout")
                            results.append((description, False, f"Final move timeout"))
                            test_times.append((description, total_time, current_obj_before, current_opt_before, 
                                              obj_nr, opt_nr, "FAIL", approach))
                            all_passed = False
                    except Exception as e2:
                        end_time2 = time.time()
                        time_taken2 = end_time2 - start_time2
                        total_time = time_taken + time_taken2
                        print(f"  [FAIL] Final move exception: {e2}")
                        results.append((description, False, f"Final exception: {e2}"))
                        test_times.append((description, total_time, current_obj_before, current_opt_before, 
                                          obj_nr, opt_nr, "FAIL", approach))
                        all_passed = False
                else:
                    print(f"  [FAIL] Intermediate move timeout")
                    results.append((description, False, f"Intermediate timeout"))
                    test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                                      obj_nr, opt_nr, "FAIL", approach))
                    all_passed = False
            except Exception as e:
                end_time = time.time()
                time_taken = end_time - start_time
                print(f"  [FAIL] Intermediate move exception: {e}")
                results.append((description, False, f"Intermediate exception: {e}"))
                test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                                  obj_nr, opt_nr, "FAIL", approach))
                all_passed = False
        else:
            # Direct move
            start_time = time.time()
            try:
                # For optovar-only changes on 50x, use longer timeout
                if obj_nr == 4 and "Optovar change only" in approach:
                    timeout = 180.0  # Longer timeout for optovar changes on immersion
                    max_retries = 5
                else:
                    timeout = 120.0
                    max_retries = 3
                
                success = ms.set_objective_set_optovar_sync(obj_nr, opt_nr, timeout_seconds=timeout, max_retries=max_retries)
                end_time = time.time()
                time_taken = end_time - start_time
                
                if success:
                    # Verify position
                    final_obj, final_opt = ms.get_current_objective_and_optovar()
                    if final_obj == obj_nr and final_opt == opt_nr:
                        print(f"  [OK] Direct move successful (took {time_taken:.2f}s)")
                        results.append((description, True, f"Direct move successful"))
                        test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                                          obj_nr, opt_nr, "PASS", approach))
                    else:
                        print(f"  [FAIL] Position mismatch: got {final_obj}/{final_opt}")
                        results.append((description, False, f"Position mismatch"))
                        test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                                          obj_nr, opt_nr, "FAIL", approach))
                        all_passed = False
                else:
                    print(f"  [FAIL] Direct move timeout (timeout was {timeout}s)")
                    results.append((description, False, f"Direct move timeout"))
                    test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                                      obj_nr, opt_nr, "FAIL", approach))
                    all_passed = False
            except Exception as e:
                end_time = time.time()
                time_taken = end_time - start_time
                print(f"  [FAIL] Direct move exception: {e}")
                results.append((description, False, f"Direct exception: {e}"))
                test_times.append((description, time_taken, current_obj_before, current_opt_before, 
                                  obj_nr, opt_nr, "FAIL", approach))
                all_passed = False
        
        # Wait between tests
        time.sleep(3)
    
    # Print summary table
    print("\n" + "="*80)
    print("IMMERSION EXIT TEST SUMMARY")
    print("="*80)
    print(f"{'Test Description':<35} {'Approach':<20} {'Time (s)':<10} {'Status':<8}")
    print("-"*80)
    
    for desc, t_taken, from_obj, from_opt, to_obj, to_opt, status, approach in test_times:
        print(f"{desc:<35} {approach:<20} {t_taken:<10.2f} {status:<8}")
    
    print("="*80)
    
    # Analyze patterns
    print("\n" + "="*80)
    print("ANALYSIS OF IMMERSION EXIT LIMITATIONS")
    print("="*80)
    
    successful_direct = [desc for desc, _, _, _, _, _, status, approach in test_times 
                         if status == "PASS" and "Direct" in approach]
    successful_intermediate = [desc for desc, _, _, _, _, _, status, approach in test_times 
                               if status == "PASS" and "intermediate" in approach.lower()]
    failed_direct = [desc for desc, _, _, _, _, _, status, approach in test_times 
                     if status == "FAIL" and "Direct" in approach]
    failed_intermediate = [desc for desc, _, _, _, _, _, status, approach in test_times 
                           if status == "FAIL" and "intermediate" in approach.lower()]
    
    print(f"Successful direct moves: {len(successful_direct)}")
    for desc in successful_direct:
        print(f"  - {desc}")
    
    print(f"\nSuccessful intermediate moves: {len(successful_intermediate)}")
    for desc in successful_intermediate:
        print(f"  - {desc}")
    
    print(f"\nFailed direct moves: {len(failed_direct)}")
    for desc in failed_direct:
        print(f"  - {desc}")
    
    print(f"\nFailed intermediate moves: {len(failed_intermediate)}")
    for desc in failed_intermediate:
        print(f"  - {desc}")
    
    print("\nHYPOTHESIS EVALUATION:")
    print("-"*40)
    if len(successful_intermediate) > len(successful_direct):
        print("[YES] Intermediate moves work better than direct moves")
    else:
        print("[NO] Intermediate moves don't seem to help")
    
    if any("Optovar change only" in approach for _, _, _, _, _, _, status, approach in test_times if status == "PASS"):
        print("[YES] Optovar changes while on 50x work")
    else:
        print("[NO] Optovar changes on 50x may also fail")
    
    print("="*80)
    
    return all_passed, results, test_times

def main():
    """
    Main function to run the objective change tests.
    """
    print("CD7 Microscope Objective Change Test")
    print("="*60)
    print("Prerequisites:")
    print("- Microscope is connected")
    print("- No sample collision risk during objective changes")
    print("- Sufficient clearance for all objectives")
    print("- Note: Position 4 (50x/1.2) is an IMMERSION objective")
    print("="*60)
    
    # Run the standard objective change tests
    print("\n[PHASE 1] Standard Objective Change Tests")
    print("-"*60)
    all_passed1, results1, test_times1 = test_objective_changes()
    
    # Run the immersion exit pattern tests
    print("\n[PHASE 2] Immersion Exit Pattern Tests")
    print("-"*60)
    all_passed2, results2, test_times2 = test_immersion_exit_patterns()
    
    # Combined summary
    print("\n" + "="*60)
    print("COMBINED TEST SUMMARY")
    print("="*60)
    
    # Count results from both phases
    total_passed = sum(1 for _, _, _, passed, _ in results1 if passed) + \
                   sum(1 for _, passed, _ in results2 if passed)
    total_failed = sum(1 for _, _, _, passed, _ in results1 if not passed) + \
                   sum(1 for _, passed, _ in results2 if not passed)
    total_tests = len(results1) + len(results2)
    
    print(f"Total tests: {total_tests}")
    print(f"Passed: {total_passed}")
    print(f"Failed: {total_failed}")
    
    # Calculate total time
    total_time1 = sum(t_taken for _, t_taken, _, _, _, _, _ in test_times1)
    total_time2 = sum(t_taken for _, t_taken, _, _, _, _, _, _ in test_times2)
    total_time = total_time1 + total_time2
    print(f"Total test time: {total_time:.2f} seconds")
    
    # Immersion objective specific information
    print("\n" + "-"*60)
    print("SPECIAL NOTES FOR 50x IMMERSION OBJECTIVE (Position 4):")
    print("-"*60)
    print("1. Switching to/from immersion takes much longer (~60+ seconds)")
    print("2. Immersion may leave residue affecting focus")
    print("3. Hardware may have safety interlocks preventing certain moves")
    print("4. Manual cleaning may be needed after immersion use")
    print("5. Timeouts are extended for immersion operations")
    print("6. Direct moves from 50x may fail - intermediate moves may help")
    print("-"*60)
    
    # Return appropriate exit code
    if total_failed == 0:
        print("\n[OK] All tests completed!")
        return 0
    else:
        print(f"\n[FAIL] {total_failed} test(s) failed.")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
