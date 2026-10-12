#!/usr/bin/env python3
"""Run the plugin CLI directly with a prepared Python environment."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from choir_assistant.cli import main
if __name__ == "__main__":
    raise SystemExit(main())
