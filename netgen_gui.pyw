"""Double-click launcher for Network Config Tools (no console window).

It only makes the program folder importable, whatever the current folder is,
then starts netgen_gui.main().
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import netgen_gui  # noqa: E402

netgen_gui.main()
