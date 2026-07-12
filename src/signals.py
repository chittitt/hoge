"""
signals.py — 決算サプライズシグナルと流動性フィルタ。

シグナル定義
------------
四半期決算で開示される「営業利益(累計実績)」を、会社の通期計画を期間按分した
値と比較し、+X% 以上のサプライズがあれば買いシグナルとする。

  期間按分計画 = 通期営業利益計画 × 進捗率
    進捗率: 1Q=0.25, 2Q=0.50, 3Q=0.75, FY(本決算)=1.00
  サプライズ(%) = (累計実績営業利益 / 期間按分計画 − 1) × 100

ルックアヘッド排除
------------------
* サプライズは開示日(DisclosedDate)時点で確定する情報のみで計算する。
* 流動性フィルタ(直近20日平均売買代金)は「開示日以前」の価格のみ参照する。
  開示日当日や翌日の出来高を混ぜると未来情報の混入になるため厳密に < 開示日 とする。
* 実際のエントリーは backtest 側で「開示日の翌営業日寄付」に統一する。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config

# 決算種別 → 通期に対する進捗率(期間按分の分子)
_PERIOD_FRACTION = {
    "1Q": 0.25,
    "2Q": 0.50,
    "3Q": 0.75,
    "FY": 1.00,
}


def _to_float(series: pd.Series) -> pd.Series:
    """J-Quants の数値は文字列や空文字で来ることがあるため安全に float 化。"""
    return pd.to_numeric(series, errors="coerce")


def compute_surprises(statements: pd.DataFrame) -> pd.DataFrame:
    """
    財務情報 DataFrame から各四半期のサプライズ率を計算して返す。

    返り値の列:
      Code, DisclosedDate, DisclosedTime, PeriodType,
      ActualOP(累計実績), ForecastOP(通期計画), ProratedForecast(期間按分),
      SurprisePct
    """
    if statements.empty:
        return pd.DataFrame(
            columns=[
                "Code", "DisclosedDate", "DisclosedTime", "PeriodType",
                "ActualOP", "ForecastOP", "ProratedForecast", "SurprisePct",
            ]
        )

    df = statements.copy()
    df["DisclosedDate"] = pd.to_datetime(df["DisclosedDate"])

    period = df.get("TypeOfCurrentPeriod", pd.Series(index=df.index, dtype=object))
    actual_op = _to_float(df.get("OperatingProfit"))
    forecast_op = _to_float(df.get("ForecastOperatingProfit"))
    fraction = period.map(_PERIOD_FRACTION)

    prorated = forecast_op * fraction

    # サプライズ率。計画がゼロ/欠損/負(赤字計画)のケースは分母が無意味になるので除外。
    #   赤字計画(prorated<=0)からの黒転は率の解釈が破綻するため NaN とし、後段で落とす。
    with np.errstate(divide="ignore", invalid="ignore"):
        surprise = np.where(
            prorated > 0,
            (actual_op / prorated - 1.0) * 100.0,
            np.nan,
        )

    out = pd.DataFrame(
        {
            "Code": df.get("LocalCode", df.get("Code")),
            "DisclosedDate": df["DisclosedDate"],
            "DisclosedTime": df.get("DisclosedTime"),
            "PeriodType": period,
            "ActualOP": actual_op.values,
            "ForecastOP": forecast_op.values,
            "ProratedForecast": prorated.values,
            "SurprisePct": surprise,
        }
    )
    # 進捗率が引けなかった(通期/四半期以外の書類種別)行やサプライズ欠損行を除外
    out = out.dropna(subset=["SurprisePct"]).reset_index(drop=True)
    # 同一開示日の重複(訂正開示など)は最後の1件を採用
    out = out.sort_values(["Code", "DisclosedDate"]).drop_duplicates(
        subset=["Code", "DisclosedDate", "PeriodType"], keep="last"
    )
    return out.reset_index(drop=True)


def average_turnover_before(
    quotes: pd.DataFrame, as_of: pd.Timestamp, window: int
) -> float:
    """
    as_of(開示日)より前の直近 window 日の平均売買代金を返す。
    厳密に Date < as_of とし、開示日当日以降のデータは使わない(ルックアヘッド排除)。
    データ不足の場合は 0.0 を返し、フィルタで落とす。
    """
    hist = quotes.loc[quotes["Date"] < as_of]
    if len(hist) < window:
        return 0.0
    return float(hist["TurnoverValue"].tail(window).mean())


def generate_signals(
    surprises: pd.DataFrame,
    quotes_by_code: dict[str, pd.DataFrame],
    params: "config.StrategyParams",
    portfolio: "config.PortfolioConfig" = config.PORTFOLIO,
) -> pd.DataFrame:
    """
    サプライズ表 + 価格から、X閾値・流動性フィルタを満たすシグナルを抽出。

    返り値(1行=1シグナル):
      Code, DisclosedDate, PeriodType, SurprisePct, AvgTurnover
    エントリー日(翌営業日寄付)の確定は backtest 側で行う。
    """
    if surprises.empty:
        return surprises.assign(AvgTurnover=[])

    rows = []
    for r in surprises.itertuples(index=False):
        if r.SurprisePct < params.surprise_threshold:
            continue
        quotes = quotes_by_code.get(str(r.Code))
        if quotes is None or quotes.empty:
            continue
        avg_turnover = average_turnover_before(
            quotes, r.DisclosedDate, portfolio.turnover_window
        )
        if avg_turnover < portfolio.min_turnover:
            continue
        rows.append(
            {
                "Code": str(r.Code),
                "DisclosedDate": r.DisclosedDate,
                "PeriodType": r.PeriodType,
                "SurprisePct": r.SurprisePct,
                "AvgTurnover": avg_turnover,
            }
        )

    cols = ["Code", "DisclosedDate", "PeriodType", "SurprisePct", "AvgTurnover"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("DisclosedDate").reset_index(drop=True)
