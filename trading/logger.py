from __future__ import annotations

import logging
import os

from config import DB_FILE  # noqa: F401 — re-exported for convenience

SCANNER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE  = os.path.join(SCANNER_DIR, "state.json")
LOG_FILE    = os.path.join(SCANNER_DIR, "scanner.log")

# ── Structured logger ────────────────────────────────────────────────────────
# Available to all scan functions. TUI can attach its own handler via
# logging.getLogger("scanner").addHandler(...).
logger = logging.getLogger("scanner")
if not logger.handlers:
    logger.setLevel(logging.DEBUG)
    _fh = logging.FileHandler(LOG_FILE)
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_fh)
    # Console handler only in CLI mode (TUI has its own RichLog)
    if os.environ.get("SCANNER_LOG_CONSOLE", "0") == "1" or not os.environ.get("TEXTUAL_APP"):
        _ch = logging.StreamHandler()
        _ch.setLevel(logging.INFO)
        _ch.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(_ch)
