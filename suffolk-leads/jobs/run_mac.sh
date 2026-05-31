#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_mac.sh — Double-click launcher for run_backfill_mac.py
#
# SETUP (one time only):
#   1. Open Terminal and run:
#        chmod +x run_mac.sh
#   2. Fill in your API keys below where it says PASTE_YOUR_KEY_HERE
#   3. Export your websurrogates.nycourts.gov cookies using EditThisCookie
#      and save the file as:
#        cookies.json   (in the same folder as this script)
#   4. Double-click run_mac.sh in Finder (or run it from Terminal)
#
# The script will:
#   • Install required Python packages if missing
#   • Run the backfill
#   • Save results to ~/Desktop/backfill_results.csv
#   • Email the CSV to Anthonychamalerealty@gmail.com when done
# ─────────────────────────────────────────────────────────────────────────────

# ── PASTE YOUR API KEYS HERE ──────────────────────────────────────────────────
export OPENAI_API_KEY="PASTE_YOUR_OPENAI_API_KEY_HERE"
export SENDGRID_API_KEY="PASTE_YOUR_SENDGRID_API_KEY_HERE"
# ─────────────────────────────────────────────────────────────────────────────

# Resolve the directory this script lives in (works even if double-clicked)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo "  NY Probate Backfill — Mac Local Runner"
echo "  Script dir: $SCRIPT_DIR"
echo "============================================================"

# ── Check Python ──────────────────────────────────────────────────────────────
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "ERROR: Python 3 is not installed."
    echo "Download it from https://www.python.org/downloads/"
    read -p "Press Enter to close..."
    exit 1
fi

echo "Using Python: $($PYTHON --version)"

# ── Install required packages if missing ─────────────────────────────────────
echo ""
echo "Checking required packages..."

install_if_missing() {
    PACKAGE="$1"
    IMPORT_NAME="${2:-$1}"
    if ! $PYTHON -c "import $IMPORT_NAME" &>/dev/null; then
        echo "  Installing $PACKAGE..."
        $PYTHON -m pip install --quiet "$PACKAGE"
    else
        echo "  $PACKAGE — OK"
    fi
}

install_if_missing "playwright"       "playwright"
install_if_missing "beautifulsoup4"   "bs4"
install_if_missing "requests"         "requests"
install_if_missing "sendgrid"         "sendgrid"

# ── Install Playwright Chromium browser if needed ────────────────────────────
if ! $PYTHON -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.stop()" &>/dev/null 2>&1; then
    echo "  Installing Playwright Chromium browser..."
    $PYTHON -m playwright install chromium
fi

# ── Check cookies.json ────────────────────────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/cookies.json" ]; then
    echo ""
    echo "ERROR: cookies.json not found in $SCRIPT_DIR"
    echo ""
    echo "To get cookies:"
    echo "  1. Install the EditThisCookie extension in Chrome"
    echo "  2. Go to https://websurrogates.nycourts.gov"
    echo "  3. Log in / solve the Cloudflare challenge"
    echo "  4. Click the EditThisCookie icon → Export"
    echo "  5. Save the file as cookies.json in this folder"
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

# ── Run the backfill ──────────────────────────────────────────────────────────
echo ""
echo "Starting backfill..."
echo "Results will be saved to: ~/Desktop/backfill_results.csv"
echo "Progress is saved to:     $SCRIPT_DIR/backfill_progress.json"
echo "(If interrupted, just run this script again to resume.)"
echo ""

$PYTHON "$SCRIPT_DIR/run_backfill_mac.py"
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "============================================================"
    echo "  DONE! Check ~/Desktop/backfill_results.csv"
    echo "  An email has been sent to Anthonychamalerealty@gmail.com"
    echo "============================================================"
else
    echo "============================================================"
    echo "  The script exited with an error (code $EXIT_CODE)."
    echo "  Check the output above for details."
    echo "============================================================"
fi

# Keep the Terminal window open so you can read the output
read -p "Press Enter to close..."
