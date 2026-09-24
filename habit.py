#!/usr/bin/env python3
"""
habit.py — 習慣トラッカーの入口。

    python habit.py --help
    python habit.py today
"""
from __future__ import annotations

from habits.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
