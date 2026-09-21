"""
habits — 生活を律するための習慣トラッカー。

バックテスト側(config.py / src/)とは独立したパッケージで、標準ライブラリだけで動く。
実行は リポジトリルートの `habit.py` を使う(`python habit.py --help`)。
"""
from __future__ import annotations

__all__ = ["models", "storage", "stats", "cli"]
