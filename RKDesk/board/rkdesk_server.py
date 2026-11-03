#!/usr/bin/env python3
"""Entry point for the RKDesk board service."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rkdesk.server import main  # noqa: E402

if __name__ == "__main__":
    main()
