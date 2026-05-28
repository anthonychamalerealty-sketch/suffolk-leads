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

Every step is wrapped in try/except so errors are always printed to stdout
and never silently swallowed.  All subprocess stdout/stderr is captured and
re-printed so Railway logs show exactly what each child script is doing.
"""

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP BANNER — printed as the very first thing before any imports that
# could fail, so Railway logs always confirm the script is executing.
# ─────────────────────────────────────────────────────────────────────────────
import sys
print("=" * 70, flush=True)
print("STARTING SUFFOLK LEADS JOB", flush=True)
print("=" * 70, flush=True)

import os
import subprocess
import logging
import time
import platform
import datetime

# ─────────────────────────────────────────────────────────────────────────────
# Environment diagnostics — printed immediately so Railway logs show the
# container state even if a later import crashes.
# ─────────────────────────────────────────────────────────────────────────────
print(f"[DIAG] Python      : {sys.version}", flush=True)
print(f"[DIAG] Platform    : {platform.platform()}", flush=True)
print(f"[DIAG] Working dir : {os.getcwd()}", flush=True)
print(f"[DIAG] Script path : {os.path.abspath(__file__)}", flush=True)
print(f"[DIAG] UTC time    : {datetime.datetime.utcnow().isoformat()}", flush=True)

# Key environment variables (values redacted for secrets)
_ENV_KEYS = ["DATABASE_URL", "DATABASE_PATH", "SENDGRID_API_KEY", "DIGEST_EMAIL",
             "RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME", "RAILWAY_PROJECT_NAME"]
for _k in _ENV_KEYS:
    _v = os.environ.get(_k)
    if _v:
        # Redact anything that looks like a secret (longer than 20 chars)
        _display = _v if len(_v) <= 20 else _v[:8] + "…[redacted]"
        print(f"[DIAG] {_k} = {_display}", flush=True)
    else:
        print(f"[DIAG] {_k} = <not set>", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# Path setup — BASE_DIR is the project root (parent of jobs/)
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
print(f"[DIAG] BASE_DIR    : {BASE_DIR}", flush=True)
sys.path.insert(0, BASE_DIR)

# ─────────────────────────────────────────────────────────────────────────────
# Logging — force stdout so Railway captures every line
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("run_all")


# ─────────────────────────────────────────────────────────────────────────────
# DB path probe — log where the SQLite database is (or isn't) so Railway logs
# make the path issue immediately obvious.
# ─────────────────────────────────────────────────────────────────────────────
def _probe_db_path() -> None:
    """Print the resolved database location to stdout."""
    db_url = os.environ.get("DATABASE_URL", "")
    db_path_env = os.environ.get("DATABASE_PATH", "")

    if db_url and not db_url.startswith("sqlite"):
        logger.info(f"[DB] Using external database: {db_url[:30]}…")
        return

    # SQLite candidates in priority order
    candidates = []
    if db_path_env:
        candidates.append(db_path_env)
    candidates += [
        os.path.join(BASE_DIR, "sql_app.db"),          # project root
        os.path.join(os.getcwd(), "sql_app.db"),        # cwd
        "/data/sql_app.db",                             # Railway volume mount
        "/app/sql_app.db",                              # Dockerfile WORKDIR
    ]

    for path in candidates:
        exists = os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
        logger.info(f"[DB] Candidate: {path}  exists={exists}  size={size}B")

    # Set DATABASE_URL to the first existing file, or the project-root default
    for path in candidates:
        if os.path.isfile(path):
            os.environ.setdefault("DATABASE_URL", f"sqlite:///{path}")
            logger.info(f"[DB] Resolved DATABASE_URL → sqlite:///{path}")
            return

    # No existing file found — use project root (will be created by init_db)
    fallback = os.path.join(BASE_DIR, "sql_app.db")
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{fallback}")
    logger.warning(f"[DB] No existing SQLite file found. Will use: {fallback}")


# ─────────────────────────────────────────────────────────────────────────────
# Subprocess runner — captures stdout/stderr and re-prints them so Railway
# logs show the full output of every child script.
# ─────────────────────────────────────────────────────────────────────────────
def run_script(script_path: str) -> bool:
    """
    Run a Python script as a subprocess.
    Captures and re-prints all stdout/stderr so Railway logs contain the full
    output.  Returns True on exit-code 0, False otherwise.
    """
    full_path = os.path.join(BASE_DIR, script_path)
    logger.info("=" * 60)
    logger.info(f"STEP START : {script_path}")
    logger.info(f"Full path  : {full_path}")
    logger.info(f"Exists     : {os.path.isfile(full_path)}")
    logger.info("=" * 60)

    if not os.path.isfile(full_path):
        logger.error(f"Script not found: {full_path} — skipping.")
        return False

    start_time = time.time()
    try:
        result = subprocess.run(
            [sys.executable, full_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout
            text=True,
            env={**os.environ},         # pass current env (including DATABASE_URL)
        )
        duration = time.time() - start_time

        # Re-print every line of the child's output with a prefix
        for line in result.stdout.splitlines():
            print(f"  [{script_path}] {line}", flush=True)

        if result.returncode == 0:
            logger.info(f"STEP OK    : {script_path} finished in {duration:.2f}s")
            return True
        else:
            logger.error(
                f"STEP FAIL  : {script_path} exited {result.returncode} "
                f"after {duration:.2f}s"
            )
            return False

    except Exception as exc:
        duration = time.time() - start_time
        logger.error(
            f"STEP ERROR : Exception running {script_path} after {duration:.2f}s: {exc}",
            exc_info=True,
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("Suffolk Leads Orchestrator — pipeline starting")
    pipeline_start = time.time()

    # Probe DB path before running any step
    try:
        _probe_db_path()
    except Exception as exc:
        logger.error(f"DB probe failed: {exc}", exc_info=True)

    steps = [
        "scrapers/parcel_access.py",
        "scrapers/fire_reports.py",
        "scrapers/probate.py",
        "scrapers/obituary.py",
        "scrapers/social_signals.py",
        "processor/enrich.py",
        "jobs/daily_digest.py",
    ]

    results: dict[str, bool] = {}

    for step in steps:
        logger.info(f"Running step: {step}")
        try:
            ok = run_script(step)
        except Exception as exc:
            logger.error(f"Unexpected error for step {step}: {exc}", exc_info=True)
            ok = False
        results[step] = ok
        if not ok:
            logger.warning(f"Step {step} failed — continuing with remaining steps.")

    # ── Summary ──────────────────────────────────────────────────────────────
    total = time.time() - pipeline_start
    logger.info("=" * 60)
    logger.info("PIPELINE SUMMARY")
    logger.info(f"Total duration: {total:.2f}s ({total / 60:.2f} min)")
    for step, ok in results.items():
        status = "OK  " if ok else "FAIL"
        logger.info(f"  [{status}] {step}")

    failed = [s for s, ok in results.items() if not ok]
    if failed:
        logger.error(f"Pipeline finished with {len(failed)} failed step(s).")
        sys.exit(1)
    else:
        logger.info("ALL STEPS COMPLETED SUCCESSFULLY.")
        sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: Unhandled exception in run_all.py: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(2)
