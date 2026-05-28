"""
run_all.py
----------
Orchestrates and runs all scrapers and pipeline jobs in sequence:
  1. scrapers/parcel_access.py
  2. scrapers/fire_reports.py
  3. scrapers/probate.py
  4. scrapers/obituary.py
  5. scrapers/social_signals.py
  6. processor/enrich.py
  7. jobs/daily_digest.py

Logs each step to the console so progress can be monitored in Railway logs.
"""

from __future__ import annotations
import os
import sys
import subprocess
import logging
import time

# Path setup — allow running as a script from any working directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("run_all_orchestrator")

def run_script(script_path: str) -> bool:
    """
    Runs a Python script as a subprocess, inherits stdout/stderr for real-time logging,
    and returns True if it completes successfully (exit code 0).
    """
    full_path = os.path.join(BASE_DIR, script_path)
    logger.info(f"=========================================")
    logger.info(f"STARTING STEP: {script_path}")
    logger.info(f"Full path: {full_path}")
    logger.info(f"=========================================")
    
    start_time = time.time()
    try:
        # Use sys.executable to ensure we use the same Python interpreter/environment
        result = subprocess.run(
            [sys.executable, full_path],
            check=False,  # Don't raise exception immediately, we want to log the exit code
            text=True
        )
        duration = time.time() - start_time
        if result.returncode == 0:
            logger.info(f"SUCCESS: {script_path} completed successfully in {duration:.2f} seconds.")
            return True
        else:
            logger.error(f"FAILED: {script_path} failed with exit code {result.returncode} after {duration:.2f} seconds.")
            return False
    except Exception as exc:
        duration = time.time() - start_time
        logger.error(f"ERROR: Exception occurred while running {script_path} after {duration:.2f} seconds: {exc}", exc_info=True)
        return False

def main() -> None:
    """
    Main orchestrator execution.
    """
    logger.info("Starting Suffolk Leads Orchestrator (run_all.py)...")
    pipeline_start_time = time.time()
    
    # List of steps to run in sequence
    steps = [
        "scrapers/parcel_access.py",
        "scrapers/fire_reports.py",
        "scrapers/probate.py",
        "scrapers/obituary.py",
        "scrapers/social_signals.py",
        "processor/enrich.py",
        "jobs/daily_digest.py"
    ]
    
    failed_steps = []
    
    for step in steps:
        success = run_script(step)
        if not success:
            failed_steps.append(step)
            logger.warning(f"Step {step} failed, but continuing with remaining pipeline steps.")
    
    total_duration = time.time() - pipeline_start_time
    logger.info("=========================================")
    logger.info("PIPELINE RUN SUMMARY")
    logger.info(f"Total duration: {total_duration:.2f} seconds ({total_duration/60:.2f} minutes)")
    
    if failed_steps:
        logger.error(f"Pipeline finished with {len(failed_steps)} failed step(s):")
        for step in failed_steps:
            logger.error(f"  - {step}")
        # Exit with a non-zero code if any step failed, so Railway can report a failure if needed
        sys.exit(1)
    else:
        logger.info("ALL PIPELINE STEPS COMPLETED SUCCESSFULLY!")
        sys.exit(0)

if __name__ == "__main__":
    main()
