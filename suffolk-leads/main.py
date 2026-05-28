# ─────────────────────────────────────────────────────────────────────────────
# main.py — Suffolk Leads entry point
#
# Railway runs this via:
#   CMD ["python", "main.py"]          (Dockerfile)
#   startCommand = "python main.py"    (railway.toml)
#
# Prints output BEFORE any import that could fail so Railway logs always
# confirm the container started, even on a crash.
# ─────────────────────────────────────────────────────────────────────────────

# ── VERY FIRST LINES: print with flush=True before any import ────────────────
import sys
print("SUFFOLK LEADS STARTING", flush=True)
print("=" * 70, flush=True)

# ── Safe stdlib imports ───────────────────────────────────────────────────────
import os
import time
import traceback
import platform
import datetime
import subprocess
import logging

# ── Diagnostic block ─────────────────────────────────────────────────────────
print(f"[DIAG] Python      : {sys.version}", flush=True)
print(f"[DIAG] Platform    : {platform.platform()}", flush=True)
print(f"[DIAG] Working dir : {os.getcwd()}", flush=True)
print(f"[DIAG] Script path : {os.path.abspath(__file__)}", flush=True)
print(f"[DIAG] UTC time    : {datetime.datetime.utcnow().isoformat()}", flush=True)

_ENV_KEYS = [
    "DATABASE_URL", "DATABASE_PATH", "SENDGRID_API_KEY", "DIGEST_EMAIL",
    "RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME", "RAILWAY_PROJECT_NAME",
    "RAILWAY_DEPLOYMENT_ID",
]
for _k in _ENV_KEYS:
    _v = os.environ.get(_k)
    if _v:
        _display = _v if len(_v) <= 20 else _v[:8] + "…[redacted]"
        print(f"[DIAG] {_k} = {_display}", flush=True)
    else:
        print(f"[DIAG] {_k} = <not set>", flush=True)

# ── Path setup ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
print(f"[DIAG] BASE_DIR    : {BASE_DIR}", flush=True)
sys.path.insert(0, BASE_DIR)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("main")


# ─────────────────────────────────────────────────────────────────────────────
# DB URL resolution
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_and_set_database_url() -> str:
    existing = os.environ.get("DATABASE_URL", "")
    if existing:
        logger.info(f"[DB] DATABASE_URL already set: {existing[:40]}")
        return existing

    db_path_env = os.environ.get("DATABASE_PATH", "")
    if db_path_env:
        url = f"sqlite:///{db_path_env}"
        os.environ["DATABASE_URL"] = url
        logger.info(f"[DB] Using DATABASE_PATH: {db_path_env}")
        return url

    candidates = [
        os.path.join(BASE_DIR, "sql_app.db"),
        os.path.join(os.getcwd(), "sql_app.db"),
        "/data/sql_app.db",
        "/app/sql_app.db",
    ]
    for path in candidates:
        exists = os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
        logger.info(f"[DB] Probe: {path}  exists={exists}  size={size}B")
        if exists:
            url = f"sqlite:///{path}"
            os.environ["DATABASE_URL"] = url
            logger.info(f"[DB] Resolved -> {url}")
            return url

    fallback = os.path.join(BASE_DIR, "sql_app.db")
    url = f"sqlite:///{fallback}"
    os.environ["DATABASE_URL"] = url
    logger.warning(f"[DB] No existing SQLite file found — will create: {fallback}")
    return url


# ─────────────────────────────────────────────────────────────────────────────
# Guarded database initialisation
# ─────────────────────────────────────────────────────────────────────────────
def _init_database() -> bool:
    try:
        _resolve_and_set_database_url()
    except Exception as exc:
        logger.error(f"[DB] URL resolution failed: {exc}", exc_info=True)

    try:
        from database import init_db  # deferred — DATABASE_URL is now set
        init_db()
        logger.info("[DB] Database initialised successfully.")
        return True
    except ImportError as exc:
        logger.error(f"[DB] Cannot import database module: {exc}")
        traceback.print_exc()
        return False
    except Exception as exc:
        logger.error(f"[DB] init_db() failed: {exc}")
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Determine run mode
# ─────────────────────────────────────────────────────────────────────────────
def _is_cron_mode() -> bool:
    """
    Return True when the container should run the pipeline once and exit.
    Triggered by:
      RAILWAY_CRON_MODE=1, RUN_ONCE=1, or service name containing
      'cron', 'job', or 'digest'.
    """
    if os.environ.get("RAILWAY_CRON_MODE", "").strip() == "1":
        return True
    if os.environ.get("RUN_ONCE", "").strip() == "1":
        return True
    svc = os.environ.get("RAILWAY_SERVICE_NAME", "").lower()
    if any(w in svc for w in ("cron", "job", "digest")):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Run pipeline once (cron / job mode)
# ─────────────────────────────────────────────────────────────────────────────
def _run_pipeline_once() -> int:
    run_all_path = os.path.join(BASE_DIR, "jobs", "run_all.py")
    print(f"[PIPELINE] Delegating to: {run_all_path}", flush=True)
    logger.info(f"[PIPELINE] run_all path: {run_all_path}")

    if not os.path.isfile(run_all_path):
        logger.error(f"[PIPELINE] run_all.py not found at: {run_all_path}")
        return 2

    try:
        result = subprocess.run(
            [sys.executable, run_all_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ},
        )
        for line in result.stdout.splitlines():
            print(f"  [run_all] {line}", flush=True)
        logger.info(f"[PIPELINE] run_all.py exited with code {result.returncode}")
        return result.returncode
    except Exception as exc:
        logger.error(f"[PIPELINE] Failed to run run_all.py: {exc}", exc_info=True)
        return 3


# ─────────────────────────────────────────────────────────────────────────────
# Long-running service mode
# ─────────────────────────────────────────────────────────────────────────────
def _run_service_loop() -> None:
    logger.info("[SERVICE] Long-lived service mode — pipeline runs every hour.")
    print("[SERVICE] Container alive — pipeline runs every hour.", flush=True)

    PIPELINE_INTERVAL = 3600   # seconds
    HEARTBEAT_INTERVAL = 300   # seconds

    last_pipeline = 0.0
    last_heartbeat = 0.0

    while True:
        now = time.time()

        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            ts = datetime.datetime.utcnow().isoformat()
            print(f"[HEARTBEAT] {ts} — container alive", flush=True)
            last_heartbeat = now

        if now - last_pipeline >= PIPELINE_INTERVAL:
            logger.info("[SERVICE] Starting scheduled pipeline run ...")
            try:
                rc = _run_pipeline_once()
                logger.info(f"[SERVICE] Pipeline finished, exit code {rc}")
            except Exception as exc:
                logger.error(f"[SERVICE] Pipeline error: {exc}", exc_info=True)
            last_pipeline = time.time()

        time.sleep(10)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    print("[MAIN] main() entered", flush=True)
    logger.info("Suffolk Leads main() starting")

    db_ok = _init_database()
    if not db_ok:
        logger.warning("[MAIN] Database init failed — continuing anyway.")

    cron = _is_cron_mode()
    mode_label = "CRON/JOB (run once)" if cron else "SERVICE (long-running)"
    logger.info(f"[MAIN] Mode: {mode_label}")
    print(f"[MAIN] Mode: {mode_label}", flush=True)

    if cron:
        rc = _run_pipeline_once()
        print(f"[MAIN] Pipeline exit code: {rc}", flush=True)
        sys.exit(rc)
    else:
        _run_service_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[MAIN] Interrupted.", flush=True)
        sys.exit(0)
    except Exception as exc:
        print(f"FATAL: Unhandled exception in main.py: {exc}", flush=True)
        traceback.print_exc()
        sys.exit(2)
