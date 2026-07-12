"""
synthetic.py — 合成データ生成器(APIキー無しでパイプライン検証用)。

J-Quants のスキーマを模した価格・財務データを決定論的に生成する。
陽性サプライズの直後に緩やかな上方ドリフトを人工的に埋め込んであるため、
「シグナルが機能していれば期待値がプラスに出る」ことをパイプライン全体で確認できる。

本物のデータではないため、生成物はあくまで配線(データフロー)確認用。
戦略の実力評価には data_fetch 経由の実データを使うこと。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _business_days(start: str, end: str) -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, end=end)


def generate(
    n_codes: int = 12,
    start: str = "2018-06-01",
    end: str = "2025-12-31",
    seed: int = 42,
    drift_strength: float = 0.015,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """
    合成の (quotes_by_code, statements_by_code) を返す。

    - 各銘柄は四半期ごと(約63営業日ごと)に決算を開示。
    - サプライズ率はランダムだが、陽性サプライズ後は約20営業日かけて上方ドリフト。
    - 出来高・売買代金は流動性フィルタを通る水準に設定。
    """
    rng = np.random.default_rng(seed)
    dates = _business_days(start, end)

    quotes_by_code: dict[str, pd.DataFrame] = {}
    statements_by_code: dict[str, pd.DataFrame] = {}

    for i in range(n_codes):
        code = f"{1300 + i * 7}0"  # 5桁コード風
        n = len(dates)

        # 日次リターンのベース(ランダムウォーク)
        daily_ret = rng.normal(0.0002, 0.018, n)

        # --- 決算イベントとサプライズを先に決める ---
        # 最初の決算は流動性ウィンドウ確保のため 30営業日目以降から
        event_idxs = list(range(30, n, 63))
        stmt_rows = []
        period_cycle = ["1Q", "2Q", "3Q", "FY"]
        base_forecast = float(rng.uniform(800, 3000)) * 1e6  # 通期営業利益計画(円)

        for k, ev in enumerate(event_idxs):
            period = period_cycle[k % 4]
            fraction = {"1Q": 0.25, "2Q": 0.5, "3Q": 0.75, "FY": 1.0}[period]
            # サプライズ率(-25%〜+40%)。正のとき後続にドリフトを注入。
            surprise = float(rng.uniform(-25, 40))
            prorated = base_forecast * fraction
            actual_op = prorated * (1 + surprise / 100.0)

            if surprise > 0:
                # 開示翌日から drift_days 営業日かけて上方ドリフトを上乗せ
                drift_days = 20
                mag = drift_strength * (surprise / 40.0)  # サプライズ強度に比例
                for d in range(1, drift_days + 1):
                    j = ev + d
                    if j < n:
                        daily_ret[j] += mag / drift_days * 4  # 日次に配分

            disclosed = dates[ev]
            stmt_rows.append(
                {
                    "LocalCode": code,
                    "DisclosedDate": disclosed.strftime("%Y-%m-%d"),
                    "DisclosedTime": "15:00",
                    "TypeOfCurrentPeriod": period,
                    "OperatingProfit": str(int(actual_op)),
                    "ForecastOperatingProfit": str(int(base_forecast)),
                }
            )
            # 年度替わりで通期計画を更新
            if period == "FY":
                base_forecast = float(rng.uniform(800, 3000)) * 1e6

        # --- 価格系列を生成 ---
        price0 = float(rng.uniform(800, 4000))
        close = price0 * np.exp(np.cumsum(daily_ret))
        # 日中のOHLCを終値まわりに構成
        intraday = rng.normal(0, 0.008, n)
        open_ = close * (1 + rng.normal(0, 0.006, n))
        high = np.maximum(open_, close) * (1 + np.abs(intraday))
        low = np.minimum(open_, close) * (1 - np.abs(intraday))
        volume = rng.integers(200_000, 1_500_000, n).astype(float)
        turnover = close * volume  # 売買代金(円)

        qdf = pd.DataFrame(
            {
                "Date": dates,
                "Code": code,
                "Open": open_,
                "High": high,
                "Low": low,
                "Close": close,
                "Volume": volume,
                "TurnoverValue": turnover,
            }
        )
        quotes_by_code[code] = qdf
        statements_by_code[code] = pd.DataFrame(stmt_rows)

    return quotes_by_code, statements_by_code
