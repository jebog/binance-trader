#!/bin/bash
# Trading Scanner wrapper — loads credentials then runs scanner
# Cron-safe: uses absolute paths

SCANNER_DIR="/Users/jebog/Documents/Claude/Projects/Trading"
LOG_FILE="$SCANNER_DIR/scanner.log"
PYTHON="/Users/jebog/.pyenv/versions/3.12.4/bin/python3"

# Load Binance credentials
if [ -f "$HOME/.env" ]; then
    export BINANCE_API_KEY=$(grep '^BINANCE_API_KEY=' "$HOME/.env" | cut -d= -f2-)
    export BINANCE_SECRET_KEY=$(grep '^BINANCE_SECRET_KEY=' "$HOME/.env" | cut -d= -f2-)
fi

echo "--- $(date) ---" >> "$LOG_FILE"
cd "$SCANNER_DIR"
# Run non-interactive (no CONFIRM prompt in cron mode)
SCANNER_CRON=1 "$PYTHON" "$SCANNER_DIR/scanner.py" >> "$LOG_FILE" 2>&1
