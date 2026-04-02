#!/bin/bash
# Trading Scanner wrapper — loads credentials then runs scanner
# Cron-safe: uses absolute paths, grep-based .env loading (no `source`)

SCANNER_DIR="/Users/jebog/Documents/Claude/Projects/Trading"
LOG_FILE="$SCANNER_DIR/scanner.log"
PYTHON="/Users/jebog/.pyenv/versions/3.12.4/bin/python3"

# Load .env via grep (robust in launchd — `source` fails with exit 32256)
ENV_FILE="$SCANNER_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    export CRON_ENABLED=$(grep -m1 '^CRON_ENABLED=' "$ENV_FILE" | cut -d= -f2- | tr -d '[:space:]"'"'")
    export BINANCE_API_KEY=$(grep -m1 '^BINANCE_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
    export BINANCE_SECRET_KEY=$(grep -m1 '^BINANCE_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2-)
    export TELEGRAM_TOKEN=$(grep -m1 '^TELEGRAM_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
    export TELEGRAM_CHAT_ID=$(grep -m1 '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)
fi

# ── Cron toggle: exit early if disabled ──────────────────────────
if [ "${CRON_ENABLED:-false}" != "true" ]; then
    echo "$(date) — CRON_ENABLED is not true, skipping scan" >> "$LOG_FILE"
    exit 0
fi

echo "--- $(date) ---" >> "$LOG_FILE"
cd "$SCANNER_DIR"
SCANNER_CRON=1 "$PYTHON" "$SCANNER_DIR/scanner.py" >> "$LOG_FILE" 2>&1
