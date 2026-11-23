#!/usr/bin/env python3
"""Run the SDK Tooling CLI from a checked-out source revision."""

import sys
from importlib import import_module
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(import_module("sdk_tooling.__main__").main())
