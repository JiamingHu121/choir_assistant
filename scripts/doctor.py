#!/usr/bin/env python3
"""Check Python dependencies and external tools without downloading anything."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import doctor_main

if __name__ == "__main__":
    raise SystemExit(doctor_main())
