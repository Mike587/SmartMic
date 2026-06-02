"""
MS_Testing_Positions.py

Framework for testing CD7 microscope positions and movements.
"""

import sys
import time
from pathlib import Path

# Add the parent directory to sys.path to import modules
sys.path.append(str(Path(__file__).parent))

try:
    import MS_CD7_API_LoA as ms
    HAS_MS_MODULE = True
except ImportError as e:
    print(f"[ERROR] Could not import MS_CD7_API_LoA: {e}")
    HAS_MS_MODULE = False
    sys.exit(1)

def print_header(title):
    """Print a formatted header."""
    print("\n" + "="*70)
    print(f" {title}")
    print("="*70)

def print_step(step_num, description):
    """Print a formatted step."""
    print(f"\n[STEP {step_num}] {description}")
    print("-" * 50)

def print_position_info(label, x, y, units="m"):
    """Print position information in a formatted way."""
    # Convert to micrometers for display if in meters
    if units == "m":
        x_display = x * 1e6
        y_display = y * 1e6
        unit_label = "µm"
    else:
        x_display = x
        y_display = y
        unit_label = units
    
    print(f"  {label}:")
    print(f"    X = {x_display:.2f} {unit_label}")
    print(f"    Y = {y_display:.2f} {unit_label}")

def print_objective_info(label, objective_nr, optovar_nr):
    """Print objective information in a formatted way."""
    # Map objective numbers to descriptions
    objective_map = {
        1: "20x/0.7",
        2: "5x/0.35", 
        3: "20x/0.95",
        4: "50x/1.2 (Immersion)"
    }
    
    # Map optovar numbers to descriptions
    optovar_map = {
        1: "2x Tubelens",
        2: "1x Tubelens",
        3: "0.5x Tubelens"
    }
    
    obj_desc = objective_map.get(objective_nr, f"Unknown ({objective_nr})")
    opt_desc = optovar_map.get(optovar_nr, f"Unknown ({optovar_nr})")
    
    print(f"  {label}:")
    print(f"    Objective: {obj_desc} (Position {objective_nr})")
    print(f"    Optovar: {opt_desc} (Position {optovar_nr})")

def changing_objective_at_unsafe_stage_position():
    """
    Test what happens when changing objectives at potentially unsafe stage positions.
    
    This test investigates whether the microscope automatically moves the stage
    to a safe position when changing to an objective that requires more clearance.
    """
    print_header("TEST: Objective Change at Unsafe Stage Positions")
    print("Purpose: Investigate stage behavior during objective changes at positions")
    print("         that may be unsafe for certain objectives.")
    
    # Define test positions (in meters)
    save_xy = [0.016603741, 0.02702661499999999]  # Safe position for all objectives
    border_xy = [0.0035306739999999993, 0.010464307]  # Border position
    A1_xy = [0.002505411, 0.005910970999999999]  # A1 position (safe for 5x only)
    
    step_counter = 1
    
    # Step 1: Get initial position
    print_step(step_counter, "Get Initial Stage Position")
    step_counter += 1
    initial_x, initial_y = ms.get_current_xy_stage_position()
    print_position_info("Initial Stage Position", initial_x, initial_y)
    
    # Get initial objective
    initial_obj, initial_opt = ms.get_current_objective_and_optovar()
    print_objective_info("Initial Objective", initial_obj, initial_opt)
    
    # Step 2: Set initial state (5x with 1x tubelens)
    print_step(step_counter, "Set Initial State: 5x/0.35 with 1x Tubelens")
    step_counter += 1
    print("  Changing to Objective 2 (5x/0.35), Optovar 2 (1x Tubelens)...")
    start_time = time.time()
    ms.set_objective_set_optovar_sync(2, 2)
    obj_time = time.time() - start_time
    print(f"  Objective change completed in {obj_time:.2f}s")
    
    # Verify objective change
    current_obj, current_opt = ms.get_current_objective_and_optovar()
    print_objective_info("Current Objective", current_obj, current_opt)
    
    # Step 3: Move to safe position
    print_step(step_counter, "Move to Safe Position")
    step_counter += 1
    safe_x, safe_y = save_xy
    print_position_info("Target Safe Position", safe_x, safe_y)
    
    print("  Moving stage to safe position...")
    start_time = time.time()
    ms.move_stage_to_new_xy_position(safe_x, safe_y)
    move_time = time.time() - start_time
    
    # Get position after move
    current_x, current_y = ms.get_current_xy_stage_position()
    print_position_info("Current Stage Position", current_x, current_y)
    print(f"  Stage move completed in {move_time:.2f}s")
    
    # Step 4: Move to A1 position (safe for 5x only)
    print_step(step_counter, "Move to A1 Position (Safe for 5x Only)")
    step_counter += 1
    a1_x, a1_y = A1_xy
    print_position_info("Target A1 Position", a1_x, a1_y)
    print("  Note: This position is safe for 5x objective but may be unsafe")
    print("        for objectives with larger working distances.")
    
    print("  Moving stage to A1 position...")
    start_time = time.time()
    ms.move_stage_to_new_xy_position(a1_x, a1_y)
    move_time = time.time() - start_time
    
    # Get position after move
    current_x, current_y = ms.get_current_xy_stage_position()
    print_position_info("Current Stage Position", current_x, current_y)
    print(f"  Stage move completed in {move_time:.2f}s")
    
    # Step 5: Return to safe position (still with 5x)
    print_step(step_counter, "Return to Safe Position (with 5x)")
    step_counter += 1
    print("  Moving stage back to safe position while still using 5x objective...")
    start_time = time.time()
    ms.move_stage_to_new_xy_position(safe_x, safe_y)
    move_time = time.time() - start_time
    
    # Get position after move
    current_x, current_y = ms.get_current_xy_stage_position()
    print_position_info("Current Stage Position", current_x, current_y)
    print(f"  Stage move completed in {move_time:.2f}s")
    
    # Step 6: Move to A1 and attempt to change to 20x
    print_step(step_counter, "Critical Test: Change to 20x at A1 Position")
    step_counter += 1
    print("  1. Moving to A1 position...")
    start_time = time.time()
    ms.move_stage_to_new_xy_position(a1_x, a1_y)
    move_time = time.time() - start_time
    
    current_x, current_y = ms.get_current_xy_stage_position()
    print_position_info("Current Stage Position (at A1)", current_x, current_y)
    print(f"  Stage move completed in {move_time:.2f}s")
    
    print("\n  2. Attempting to change to 20x/0.7 with 1x Tubelens...")
    print("     This position may be unsafe for 20x objective.")
    print("     Will the system prevent the change or move the stage automatically?")
    
    start_time = time.time()
    ms.set_objective_set_optovar_sync(1, 2)  # 20x with 1x tubelens
    obj_time = time.time() - start_time
    
    # Get position and objective after change
    final_x, final_y = ms.get_current_xy_stage_position()
    final_obj, final_opt = ms.get_current_objective_and_optovar()
    
    print_position_info("Stage Position After Objective Change", final_x, final_y)
    print_objective_info("Objective After Change", final_obj, final_opt)
    print(f"  Objective change completed in {obj_time:.2f}s")
    
    # Step 7: Analysis
    print_step(step_counter, "Test Analysis")
    step_counter += 1
    
    # Calculate if stage moved during objective change
    distance_moved = ((final_x - a1_x)**2 + (final_y - a1_y)**2)**0.5
    distance_moved_um = distance_moved * 1e6
    
    print("  Analysis Results:")
    print(f"    Initial A1 position: X={a1_x*1e6:.2f} µm, Y={a1_y*1e6:.2f} µm")
    print(f"    Final position: X={final_x*1e6:.2f} µm, Y={final_y*1e6:.2f} µm")
    print(f"    Distance moved during objective change: {distance_moved_um:.2f} µm")
    
    if distance_moved_um > 10.0:  # More than 10 µm movement
        print("\n  [CONCLUSION] The stage WAS MOVED during objective change.")
        print("    The microscope automatically moved to a safer position")
        print("    when changing to an objective that requires more clearance.")
    else:
        print("\n  [CONCLUSION] The stage was NOT MOVED during objective change.")
        print("    The microscope allowed the objective change without")
        print("    adjusting the stage position.")
    
    # Step 8: Return to initial position
    print_step(step_counter, "Cleanup: Return to Initial Position")
    print("  Returning stage to initial position...")
    ms.move_stage_to_new_xy_position(initial_x, initial_y)
    
    # Return to initial objective if different
    if initial_obj != final_obj or initial_opt != final_opt:
        print("  Restoring initial objective configuration...")
        ms.set_objective_set_optovar_sync(initial_obj, initial_opt)
    
    final_x, final_y = ms.get_current_xy_stage_position()
    final_obj, final_opt = ms.get_current_objective_and_optovar()
    
    print_position_info("Final Stage Position", final_x, final_y)
    print_objective_info("Final Objective", final_obj, final_opt)
    
    print_header("TEST COMPLETE")
    print("Summary: Objective change safety mechanism test finished.")
    
    return {
        'initial_position': (initial_x, initial_y),
        'a1_position': (a1_x, a1_y),
        'final_position_after_change': (final_x, final_y),
        'distance_moved_during_change': distance_moved_um,
        'stage_was_moved': distance_moved_um > 10.0
    }







