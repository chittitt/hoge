"""
data_fetch.py — J-Quants API クライアントとローカル parquet キャッシュ。

設計方針
--------
* 取得したデータはすべて data/ 配下に parquet でキャッシュし、再実行時は
  API を叩かない(force_refresh=True のときだけ再取得)。
* 生存者バイアス対策:
  ユニバースは「期間中に価格データが存在したことのある全銘柄コード」の和集合で
  構成する。現在の上場銘柄一覧だけを使うと、期間途中で廃止・上場廃止になった
  銘柄が抜け落ちて成績が過大評価される(survivorship bias)。廃止銘柄も
  価格・財務データに残っている限りユニバースへ含める。
* 株価は必ず調整済み(Adjustment*)列を使う。分割・併合の影響を除去するため。

ライトプラン想定:取得可能な履歴期間・レート制限に制約があるため、
ページネーション(pagination_key)と素直なリトライのみ実装。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

import config

_SESSION = requests.Session()
_ID_TOKEN: str | None = None


# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------
def _authenticate() -> str:
    """メール/パスワード → refresh token → id token を取得してキャッシュ。"""
    global _ID_TOKEN
    if _ID_TOKEN is not None:
        return _ID_TOKEN

    mail, passwd = config.get_credentials()

    r = _SESSION.post(
        f"{config.JQ_BASE_URL}/token/auth_user",
        json={"mailaddress": mail, "password": passwd},
        timeout=30,
    )
    r.raise_for_status()
    refresh_token = r.json()["refreshToken"]

    r = _SESSION.post(
        f"{config.JQ_BASE_URL}/token/auth_refresh",
        params={"refreshtoken": refresh_token},
        timeout=30,
    )
    r.raise_for_status()
    _ID_TOKEN = r.json()["idToken"]
    return _ID_TOKEN


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {_authenticate()}"}


def _get_paginated(endpoint: str, params: dict[str, Any], data_key: str) -> list[dict]:
    """
    pagination_key を辿って全ページを取得する共通ヘルパ。

    J-Quants はレスポンスに pagination_key が含まれる場合、それを次リクエストへ
    渡すことで続きを取得できる。レート制限・一時的失敗には指数バックオフで対処。
    """
    rows: list[dict] = []
    pagination_key: str | None = None
    while True:
        q = dict(params)
        if pagination_key:
            q["pagination_key"] = pagination_key

        for attempt in range(5):
            resp = _SESSION.get(
                f"{config.JQ_BASE_URL}{endpoint}",
                headers=_auth_header(),
                params=q,
                timeout=60,
            )
            if resp.status_code == 200:
                break
            # 429(レート制限)/5xx は待って再試行
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
        payload = resp.json()
        rows.extend(payload.get(data_key, []))
        pagination_key = payload.get("pagination_key")
        if not pagination_key:
            break
    return rows


# ---------------------------------------------------------------------------
# キャッシュ入出力
# ---------------------------------------------------------------------------
def _cache_path(name: str) -> Path:
    return config.DATA_DIR / f"{name}.parquet"


def _load_or_fetch(
    name: str, fetch_fn, force_refresh: bool = False
) -> pd.DataFrame:
    """parquet があれば読み、無ければ fetch_fn() を呼んで保存する。"""
    path = _cache_path(name)
    if path.exists() and not force_refresh:
        return pd.read_parquet(path)
    df = fetch_fn()
    if not df.empty:
        df.to_parquet(path, index=False)
    return df


# ---------------------------------------------------------------------------
# 1. 上場銘柄一覧 /listed/info
# ---------------------------------------------------------------------------
def fetch_listed_info(date: str | None = None, force_refresh: bool = False) -> pd.DataFrame:
    """
    上場銘柄一覧を取得。date を指定するとその日時点の構成銘柄
    (=当時上場していて後に廃止された銘柄も含む)を返す。
    生存者バイアスを避けたい場合は複数 date で呼んで和集合を取る運用を推奨。
    """
    tag = f"listed_info_{date or 'latest'}"

    def _fetch() -> pd.DataFrame:
        params = {"date": date} if date else {}
        return pd.DataFrame(_get_paginated("/listed/info", params, "info"))

    return _load_or_fetch(tag, _fetch, force_refresh)


# ---------------------------------------------------------------------------
# 2. 日足株価 /prices/daily_quotes
# ---------------------------------------------------------------------------
def fetch_daily_quotes(
    code: str, date_from: str, date_to: str, force_refresh: bool = False
) -> pd.DataFrame:
    """
    1銘柄の日足を取得。調整済み株価(AdjustmentOpen/High/Low/Close)を主に使う。
    キャッシュは銘柄コード単位。
    """
    tag = f"quotes_{code}_{date_from}_{date_to}"

    def _fetch() -> pd.DataFrame:
        params = {"code": code, "from": date_from, "to": date_to}
        df = pd.DataFrame(_get_paginated("/prices/daily_quotes", params, "daily_quotes"))
        return _normalize_quotes(df)

    return _load_or_fetch(tag, _fetch, force_refresh)


def _normalize_quotes(df: pd.DataFrame) -> pd.DataFrame:
    """調整済み株価を Open/High/Low/Close/Volume に正規化した DataFrame を返す。"""
    if df.empty:
        return df
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    # 調整済み列があればそれを採用(なければ生値へフォールバック)
    ren = {
        "AdjustmentOpen": "Open",
        "AdjustmentHigh": "High",
        "AdjustmentLow": "Low",
        "AdjustmentClose": "Close",
        "AdjustmentVolume": "Volume",
    }
    for adj, plain in ren.items():
        if adj in df.columns:
            df[plain] = df[adj]
    # 売買代金(流動性フィルタ用)。TurnoverValue が無ければ Close*Volume で近似。
    if "TurnoverValue" not in df.columns:
        df["TurnoverValue"] = df["Close"] * df["Volume"]
    keep = ["Date", "Code", "Open", "High", "Low", "Close", "Volume", "TurnoverValue"]
    return df[[c for c in keep if c in df.columns]].sort_values("Date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. 財務情報 /fins/statements
# ---------------------------------------------------------------------------
def fetch_statements(code: str, force_refresh: bool = False) -> pd.DataFrame:
    """
    1銘柄の財務情報(四半期決算・会社予想を含む)を取得。
    DisclosedDate / DisclosedTime を含み、これがエントリー時点(翌営業日寄付)の
    基準となる。ルックアヘッド排除のため必ず開示日時を使う。
    """
    tag = f"statements_{code}"

    def _fetch() -> pd.DataFrame:
        return pd.DataFrame(_get_paginated("/fins/statements", {"code": code}, "statements"))

    return _load_or_fetch(tag, _fetch, force_refresh)


# ---------------------------------------------------------------------------
# 4. 決算発表予定日 /fins/announcement
# ---------------------------------------------------------------------------
def fetch_announcement(force_refresh: bool = False) -> pd.DataFrame:
    """
    翌営業日の決算発表予定銘柄一覧(前向きスケジュール)。
    過去バックテストのエントリー判定には statements の DisclosedDate を使うため、
    本エンドポイントは主に運用時(フォワード)の参照用。
    """
    def _fetch() -> pd.DataFrame:
        return pd.DataFrame(_get_paginated("/fins/announcement", {}, "announcement"))

    return _load_or_fetch("announcement", _fetch, force_refresh)


# ---------------------------------------------------------------------------
# ユニバース構築(生存者バイアス対策)
# ---------------------------------------------------------------------------
def build_universe_from_quotes(quotes_by_code: dict[str, pd.DataFrame]) -> list[str]:
    """
    価格データが1本でも存在した銘柄コードの和集合をユニバースとする。
    「現在の上場一覧」ではなく「期間中に取引実績のあった全銘柄」を対象にする
    ことで、期間途中で廃止された銘柄を落とさず survivorship bias を避ける。
    """
    return sorted(code for code, df in quotes_by_code.items() if df is not None and not df.empty)
