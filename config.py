"""
config.py — 全体の設定・パラメータ・グリッド定義を集約するモジュール。

ここで定義した値だけを触れば戦略挙動が変わるように、マジックナンバーは
各モジュールに散らさずすべてここへ寄せる方針。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# パス
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"        # parquet キャッシュ置き場(.gitignore)
RESULTS_DIR = ROOT / "results"  # CSV / PNG / MD 出力先(.gitignore)

for _d in (DATA_DIR, RESULTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# J-Quants API
# ---------------------------------------------------------------------------
JQ_BASE_URL = "https://api.jquants.com/v1"
# 認証情報は .env から読む(コード・リポジトリに秘密を残さない)
JQ_MAIL_ENV = "J_QUANTS_MAIL"
JQ_PASS_ENV = "J_QUANTS_PASS"


# ---------------------------------------------------------------------------
# バックテスト実行の固定条件
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PortfolioConfig:
    initial_capital: float = 2_500_000.0   # 想定資金 250万円
    position_fraction: float = 0.10        # 1銘柄あたり資金の10%
    max_positions: int = 8                 # 同時最大保有銘柄数
    lot_size: int = 100                    # 日本株の売買単位(単元)
    fee_rate: float = 0.001                # 片道手数料+スリッページ 0.1%
    min_turnover: float = 100_000_000.0    # 流動性フィルタ Y:直近20日平均売買代金(円)
    turnover_window: int = 20              # 平均売買代金の算出日数
    # 追加エントリーフィルタ(すべて発表日より前のデータのみで判定=ルックアヘッド排除)
    rsi_period: int = 14                   # RSI の期間(Wilder 平滑)
    rsi_upper: float = 55.0                # RSI 上限:これ以下(過熱・急騰後を除外)
    min_operating_margin: float = 8.0      # 営業利益率の下限(%):営業利益 ÷ 売上高
    vol_window: int = 20                   # ボラティリティ算出の営業日数
    max_annual_vol: float = 60.0           # 年率ボラティリティの上限(%)。60%≒日次±3.8%
    trading_days_per_year: int = 252       # ボラティリティ年率換算の営業日数


# ---------------------------------------------------------------------------
# walk-forward の期間定義
#   in-sample(2019-2022)でパラメータ選択 → out-of-sample(2023-2025)で検証
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WalkForwardConfig:
    is_start: str = "2019-01-01"
    is_end: str = "2022-12-31"
    oos_start: str = "2023-01-01"
    oos_end: str = "2025-12-31"


# ---------------------------------------------------------------------------
# 戦略パラメータのグリッド
#   X: サプライズ閾値(会社通期計画の期間按分対比 営業利益 +X%以上)
#   N: 保有営業日数(N営業日後の引けで手仕舞い)
#   Z: 損切りライン(-Z%)。None は損切りなし。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ParamGrid:
    surprise_thresholds: tuple[float, ...] = (5.0, 10.0, 15.0, 20.0)  # X
    hold_days: tuple[int, ...] = (3, 5, 10, 20)                       # N
    stop_losses: tuple[float | None, ...] = (None, 5.0, 8.0)          # Z

    def combinations(self):
        """(X, N, Z) の直積を生成。"""
        for x in self.surprise_thresholds:
            for n in self.hold_days:
                for z in self.stop_losses:
                    yield StrategyParams(surprise_threshold=x, hold_days=n, stop_loss=z)


@dataclass(frozen=True)
class StrategyParams:
    """1組の戦略パラメータ。"""
    surprise_threshold: float          # X (%)
    hold_days: int                     # N (営業日)
    stop_loss: float | None            # Z (%) / None

    @property
    def label(self) -> str:
        z = "none" if self.stop_loss is None else f"{self.stop_loss:g}"
        return f"X{self.surprise_threshold:g}_N{self.hold_days}_Z{z}"


# 既定インスタンス(各モジュールから import して使う)
PORTFOLIO = PortfolioConfig()
WALK_FORWARD = WalkForwardConfig()
PARAM_GRID = ParamGrid()

# パラメータ選択に用いる主評価軸。
#   勝率ではなく「1トレードあたり期待リターン(%)」を第一評価軸とする方針(要件)。
SELECTION_METRIC = "expectancy_pct"


def get_credentials() -> tuple[str, str]:
    """.env / 環境変数から J-Quants の認証情報を取得する。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    mail = os.environ.get(JQ_MAIL_ENV)
    passwd = os.environ.get(JQ_PASS_ENV)
    if not mail or not passwd:
        raise RuntimeError(
            f"J-Quants の認証情報が見つかりません。.env に {JQ_MAIL_ENV} と "
            f"{JQ_PASS_ENV} を設定してください(.env.example を参照)。"
        )
    return mail, passwd
