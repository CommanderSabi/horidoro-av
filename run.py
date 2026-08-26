#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Horidoro AV — development launcher.

Runs from the source modules (no build step needed while developing).
For the single-file version people receive, run:  python3 horidoro-av.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "horidoro"))

from gui import main  # noqa: E402

if __name__ == "__main__":
    main()
