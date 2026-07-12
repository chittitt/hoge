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


def _num_col(df: pd.DataFrame, name: str) -> pd.Series:
    """列が無くても NaN 埋めの Series(df.index 揃え)を返す安全なアクセサ。"""
    if name in df.columns:
        return _to_float(df[name])
    return pd.Series(np.nan, index=df.index, dtype="float64")


def compute_surprises(statements: pd.DataFrame) -> pd.DataFrame:
    """
    財務情報 DataFrame から各四半期のサプライズ率を計算して返す。

    返り値の列:
      Code, DisclosedDate, DisclosedTime, PeriodType,
      ActualOP(累計実績), ForecastOP(通期計画), ProratedForecast(期間按分),
      SurprisePct, NetSales(累計売上高), OperatingMargin(営業利益率 %)
    """
    if statements.empty:
        return pd.DataFrame(
            columns=[
                "Code", "DisclosedDate", "DisclosedTime", "PeriodType",
                "ActualOP", "ForecastOP", "ProratedForecast", "SurprisePct",
                "NetSales", "OperatingMargin",
            ]
        )

    df = statements.copy()
    df["DisclosedDate"] = pd.to_datetime(df["DisclosedDate"])

    period = df.get("TypeOfCurrentPeriod", pd.Series(index=df.index, dtype=object))
    actual_op = _num_col(df, "OperatingProfit")
    forecast_op = _num_col(df, "ForecastOperatingProfit")
    net_sales = _num_col(df, "NetSales")
    fraction = period.map(_PERIOD_FRACTION)

    prorated = forecast_op * fraction

    # 営業利益率(%)= 累計営業利益 ÷ 累計売上高。売上ゼロ/欠損は NaN(=フィルタで落とす)。
    with np.errstate(divide="ignore", invalid="ignore"):
        op_margin = np.where(net_sales > 0, actual_op / net_sales * 100.0, np.nan)

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
            "NetSales": net_sales.values,
            "OperatingMargin": op_margin,
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


def rsi_before(quotes: pd.DataFrame, as_of: pd.Timestamp, period: int) -> float:
    """
    as_of(開示日)より前の終値から RSI(Wilder 平滑)を計算し、直近値を返す。
    開示日当日以降の価格は使わない(ルックアヘッド排除)。データ不足なら NaN。

    RSI 上限フィルタの狙い:決算前に既に急騰(過熱)している銘柄を除外し、
    ドリフト余地の乏しい「出尽くし」を避ける。
    """
    close = quotes.loc[quotes["Date"] < as_of, "Close"]
    if len(close) < period + 1:
        return float("nan")
    delta = close.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder の平滑(指数移動平均 alpha=1/period)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return float("nan")
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def annualized_vol_before(
    quotes: pd.DataFrame,
    as_of: pd.Timestamp,
    window: int,
    trading_days: int = 252,
) -> float:
    """
    as_of より前の直近 window 日の日次対数リターンの標準偏差を年率換算(%)で返す。
    開示日当日以降は使わない(ルックアヘッド排除)。データ不足なら NaN。

    上限フィルタの狙い:値動きが荒すぎる銘柄を除外し、想定外の大損(DD)を抑える。
    60% ≒ 日次標準偏差 3.8%(= 60 / sqrt(252))。
    """
    close = quotes.loc[quotes["Date"] < as_of, "Close"]
    if len(close) < window + 1:
        return float("nan")
    rets = np.log(close / close.shift(1)).dropna().tail(window)
    if len(rets) < window:
        return float("nan")
    return float(rets.std(ddof=1) * np.sqrt(trading_days) * 100.0)


def generate_signals(
    surprises: pd.DataFrame,
    quotes_by_code: dict[str, pd.DataFrame],
    params: "config.StrategyParams",
    portfolio: "config.PortfolioConfig" = config.PORTFOLIO,
) -> pd.DataFrame:
    """
    サプライズ表 + 価格から、全エントリーフィルタを満たすシグナルを抽出。

    フィルタ(すべて開示日より前のデータのみで判定=ルックアヘッド排除):
      1. サプライズ率 >= X(surprise_threshold)
      2. 直近20日平均売買代金 >= min_turnover(流動性)
      3. 営業利益率 >= min_operating_margin(収益性)
      4. RSI(period)<= rsi_upper(過熱・急騰後を除外)
      5. 年率ボラティリティ(直近 vol_window 日)<= max_annual_vol(荒すぎる値動きを除外)

    返り値(1行=1シグナル):
      Code, DisclosedDate, PeriodType, SurprisePct, AvgTurnover,
      OperatingMargin, RSI, AnnualVol
    エントリー日(翌営業日寄付)の確定は backtest 側で行う。
    """
    cols = [
        "Code", "DisclosedDate", "PeriodType", "SurprisePct", "AvgTurnover",
        "OperatingMargin", "RSI", "AnnualVol",
    ]
    if surprises.empty:
        return pd.DataFrame(columns=cols)

    rows = []
    for r in surprises.itertuples(index=False):
        # 1) サプライズ閾値
        if r.SurprisePct < params.surprise_threshold:
            continue
        # 3) 営業利益率(決算の累計実績ベース。開示情報なのでルックアヘッドではない)
        op_margin = getattr(r, "OperatingMargin", float("nan"))
        if pd.isna(op_margin) or op_margin < portfolio.min_operating_margin:
            continue

        quotes = quotes_by_code.get(str(r.Code))
        if quotes is None or quotes.empty:
            continue

        # 2) 流動性
        avg_turnover = average_turnover_before(
            quotes, r.DisclosedDate, portfolio.turnover_window
        )
        if avg_turnover < portfolio.min_turnover:
            continue
        # 4) RSI 上限
        rsi = rsi_before(quotes, r.DisclosedDate, portfolio.rsi_period)
        if pd.isna(rsi) or rsi > portfolio.rsi_upper:
            continue
        # 5) 年率ボラティリティ上限
        ann_vol = annualized_vol_before(
            quotes, r.DisclosedDate, portfolio.vol_window, portfolio.trading_days_per_year
        )
        if pd.isna(ann_vol) or ann_vol > portfolio.max_annual_vol:
            continue

        rows.append(
            {
                "Code": str(r.Code),
                "DisclosedDate": r.DisclosedDate,
                "PeriodType": r.PeriodType,
                "SurprisePct": r.SurprisePct,
                "AvgTurnover": avg_turnover,
                "OperatingMargin": op_margin,
                "RSI": rsi,
                "AnnualVol": ann_vol,
            }
        )

    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("DisclosedDate").reset_index(drop=True)
