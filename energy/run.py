#!/usr/bin/env python3
"""No-install command launcher: python3 run.py setup|once|authorize|loop|pause|audit."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from autopilot.__main__ import main
if __name__=='__main__':sys.exit(main())
