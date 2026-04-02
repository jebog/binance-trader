#!/bin/bash
# Trading Scanner wrapper — loads credentials then runs scanner
# Cron-safe: uses absolute paths

SCANNER_DIR="/Users/jebog/Documents/Claude/Projects/Trading"
LOG_FILE="$SCANNER_DIR/scanner.log"
PYTHON="/Users/jebog/.pyenv/versions/3.12.4/bin/python3"

# Load .env (credentials + CRON_ENABLED toggle)
if [ -f "$SCANNER_DIR/.env" ]; then
    set -a
    source "$SCANNER_DIR/.env"
    set +a
elif [ -f "$HOME/.env" ]; then
    set -a
    source "$HOME/.env"
    set +a
fi

# ── Cron toggle: exit early if disabled ──────────────────────────
# Set CRON_ENABLED=true in .env to enable. Default: disabled.
if [ "${CRON_ENABLED:-false}" != "true" ]; then
    echo "$(date) — CRON_ENABLED is not true, skipping scan" >> "$LOG_FILE"
    exit 0
fi

echo "--- $(date) ---" >> "$LOG_FILE"
cd "$SCANNER_DIR"
# Run non-interactive (no CONFIRM prompt in cron mode)
SCANNER_CRON=1 "$PYTHON" "$SCANNER_DIR/scanner.py" >> "$LOG_FILE" 2>&1
