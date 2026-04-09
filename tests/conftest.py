"""
Pytest configuration — applied to the entire test suite.

Primary job: prevent tests from polluting the real scanner.log file.

trading/logger.py unconditionally attaches a FileHandler to scanner.log at
import time, using the guard `if not logger.handlers:`. Any test that imports
a trading/* module inherits that handler. Without this shim, error-path tests
(especially test_reconcile) write fake divergences, mocked "tg down" strings,
and other test noise into the production log file — which looks alarmingly
like a real incident when you read scanner.log days later.

Fix: pre-populate the "scanner" logger with a NullHandler *at conftest import
time*, BEFORE any test module is collected (pytest imports conftest.py before
test files in the same directory). trading/logger.py's `if not logger.handlers`
guard then sees the NullHandler and skips its FileHandler installation entirely.

This runs at module level — not via a fixture — because fixture bodies execute
after collection, and by then trading.logger has already been imported and
configured. Module-level code runs at conftest-import time, which happens
before any test file is imported.
"""
from __future__ import annotations

import logging

# Pre-configure the scanner logger so trading/logger.py's `if not handlers`
# guard skips the FileHandler installation. Must happen at import time, not
# in a fixture body.
_scanner_logger = logging.getLogger("scanner")
_scanner_logger.handlers = [logging.NullHandler()]
_scanner_logger.propagate = False  # don't leak to root logger either
_scanner_logger.setLevel(logging.CRITICAL)  # silence even if a test adds a handler
