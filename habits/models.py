"""
models.py — 習慣(Habit)・記録(Entry)・スケジュールの定義と値の正規化。

設計方針
--------
- 値は **内部的には float 一本**(check=1/0、number=実数、time=分)で持ち、
  表示・入力のときだけ人間向けの表記(`06:12` / `2.5h`)に変換する。
  判定ロジックが型分岐だらけになるのを避けるため。
- 就寝時刻のような日をまたぐ目標のために、時刻は 24時以降(`25:10`)を許容する。
  目標が夜(20:00以降)の習慣では、記録値の 12:00 未満は翌日扱い(+24h)に寄せる。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field, replace

# 曜日: Python の weekday() と同じ 月=0 … 日=6
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_JP = "月火水木金土日"

KINDS = ("check", "number", "time")
COMPARATORS = (">=", "<=")
# 睡眠時間の算出に使う役割。時刻習慣にだけ意味がある。
ROLES = ("", "bedtime", "wakeup")

# 夜の目標(これ以降)は記録値の 12:00 未満を翌日とみなす閾値
_NIGHT_TARGET_MIN = 20 * 60
_WRAP_CUTOFF_MIN = 12 * 60


class HabitError(ValueError):
    """CLI 利用者の入力ミスに起因するエラー(スタックトレースを見せずに扱う)。"""


# ---------------------------------------------------------------------------
# 日付・時刻のパース
# ---------------------------------------------------------------------------
def parse_date(text: str | None, today: dt.date | None = None) -> dt.date:
    """`2026-09-21` / `today` / `yesterday` / `-2`(2日前)を日付に変換する。"""
    today = today or dt.date.today()
    if text is None or text == "":
        return today
    s = text.strip().lower()
    if s in ("today", "きょう", "今日"):
        return today
    if s in ("yesterday", "きのう", "昨日"):
        return today - dt.timedelta(days=1)
    if re.fullmatch(r"[+-]\d+", s):
        return today + dt.timedelta(days=int(s))
    try:
        return dt.date.fromisoformat(s)
    except ValueError as exc:
        raise HabitError(f"日付として解釈できない: {text!r}(例: 2026-09-21 / today / -1)") from exc


def parse_clock(text: str) -> float:
    """`6:05` / `06:05` / `2510`(=25:10)を「0時起点の分」に変換する。"""
    s = str(text).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s) or re.fullmatch(r"(\d{1,2})(\d{2})", s)
    if not m:
        raise HabitError(f"時刻として解釈できない: {text!r}(例: 06:30 / 24:45)")
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute > 59 or hour > 47:
        raise HabitError(f"時刻の範囲外: {text!r}")
    return float(hour * 60 + minute)


def format_clock(minutes: float) -> str:
    """分を `HH:MM` に戻す(24時以降はそのまま `25:10` と表示する)。"""
    total = int(round(minutes))
    return f"{total // 60:02d}:{total % 60:02d}"


# ---------------------------------------------------------------------------
# スケジュール
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Schedule:
    """いつやる習慣かの定義。

    - `daily`  : 毎日
    - `days`   : 指定曜日のみ(例 平日 = mon,tue,wed,thu,fri)
    - `weekly` : 週に `times` 回(曜日は問わない)
    """

    type: str
    days: tuple[int, ...] = ()
    times: int = 0

    @classmethod
    def parse(cls, spec: str) -> "Schedule":
        """`daily` / `weekdays` / `mon,wed,fri` / `weekly:3` を Schedule にする。"""
        s = (spec or "daily").strip().lower()
        if s in ("daily", "everyday", "毎日"):
            return cls("daily")
        if s in ("weekdays", "平日"):
            return cls("days", days=(0, 1, 2, 3, 4))
        if s in ("weekend", "週末"):
            return cls("days", days=(5, 6))
        if s.startswith("weekly:") or s.startswith("週"):
            raw = s.split(":", 1)[1] if ":" in s else s.lstrip("週")
            if not raw.isdigit() or int(raw) < 1:
                raise HabitError(f"週あたり回数が不正: {spec!r}(例: weekly:3)")
            return cls("weekly", times=int(raw))
        days: list[int] = []
        for token in s.replace(" ", "").split(","):
            if not token:
                continue
            if token in WEEKDAY_KEYS:
                days.append(WEEKDAY_KEYS.index(token))
            elif len(token) == 1 and token in WEEKDAY_JP:
                days.append(WEEKDAY_JP.index(token))
            else:
                raise HabitError(
                    f"スケジュールとして解釈できない: {spec!r}"
                    "(例: daily / weekdays / mon,wed,fri / weekly:3)"
                )
        if not days:
            raise HabitError(f"スケジュールが空: {spec!r}")
        return cls("days", days=tuple(sorted(set(days))))

    def is_scheduled(self, day: dt.date) -> bool:
        """その日が対象日か。weekly は日単位では判定できないので常に True。"""
        if self.type == "daily":
            return True
        if self.type == "days":
            return day.weekday() in self.days
        return True

    @property
    def is_weekly(self) -> bool:
        return self.type == "weekly"

    def label(self) -> str:
        if self.type == "daily":
            return "毎日"
        if self.type == "weekly":
            return f"週{self.times}回"
        if tuple(self.days) == (0, 1, 2, 3, 4):
            return "平日"
        if tuple(self.days) == (5, 6):
            return "週末"
        return "".join(WEEKDAY_JP[d] for d in self.days)

    def to_dict(self) -> dict:
        out: dict = {"type": self.type}
        if self.days:
            out["days"] = list(self.days)
        if self.times:
            out["times"] = self.times
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "Schedule":
        return cls(
            type=raw.get("type", "daily"),
            days=tuple(raw.get("days", ())),
            times=int(raw.get("times", 0)),
        )


# ---------------------------------------------------------------------------
# 習慣
# ---------------------------------------------------------------------------
@dataclass
class Habit:
    """1つの習慣の定義。`target` は kind に応じて 実数 / 分 を意味する。"""

    id: str
    name: str
    kind: str = "check"
    schedule: Schedule = Schedule("daily")
    cmp: str = ">="
    target: float | None = None
    unit: str = ""
    role: str = ""
    created: dt.date = field(default_factory=dt.date.today)
    archived: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise HabitError(f"kind は {'/'.join(KINDS)} のいずれか: {self.kind!r}")
        if self.cmp not in COMPARATORS:
            raise HabitError(f"cmp は {'/'.join(COMPARATORS)} のいずれか: {self.cmp!r}")
        if self.kind != "check" and self.target is None:
            raise HabitError(f"kind={self.kind} には目標値(--target)が必要: {self.id}")
        if self.role not in ROLES:
            raise HabitError(f"role は {'/'.join(r or 'なし' for r in ROLES)} のいずれか: {self.role!r}")
        if self.role and self.kind != "time":
            raise HabitError(f"role は時刻習慣にのみ指定できる: {self.id}")

    # --- 値の入出力 --------------------------------------------------------
    def parse_value(self, raw: str | float | None) -> float:
        """CLI から渡された値を内部表現(float)に直す。"""
        if self.kind == "check":
            if raw in (None, "", True):
                return 1.0
            s = str(raw).strip().lower()
            if s in ("1", "true", "yes", "y", "done", "o", "○"):
                return 1.0
            if s in ("0", "false", "no", "n", "miss", "x", "×"):
                return 0.0
            raise HabitError(f"チェック習慣の値が不正: {raw!r}(指定不要、または 1/0)")
        if raw is None or str(raw).strip() == "":
            raise HabitError(f"習慣 {self.id} は値の指定が必要(--value)")
        if self.kind == "time":
            return self._wrap_clock(parse_clock(str(raw)))
        try:
            return float(str(raw).strip())
        except ValueError as exc:
            raise HabitError(f"数値として解釈できない: {raw!r}") from exc

    def _wrap_clock(self, minutes: float) -> float:
        """夜の目標に対する早朝の記録(00:30 など)を翌日扱い(+24h)に寄せる。"""
        target = self.target if self.target is not None else 0.0
        if target >= _NIGHT_TARGET_MIN and minutes < _WRAP_CUTOFF_MIN:
            return minutes + 24 * 60
        return minutes

    def format_value(self, value: float) -> str:
        if self.kind == "check":
            return "達成" if value >= 1.0 else "未達"
        if self.kind == "time":
            return format_clock(value)
        text = f"{value:g}"
        return f"{text}{self.unit}" if self.unit else text

    def target_label(self) -> str:
        if self.kind == "check" or self.target is None:
            return "実行する"
        sign = "以上" if self.cmp == ">=" else "以下"
        return f"{self.format_value(self.target)} {sign}"

    # --- 判定 --------------------------------------------------------------
    def achieved(self, value: float) -> bool:
        if self.kind == "check":
            return value >= 1.0
        assert self.target is not None  # __post_init__ で保証済み
        return value >= self.target if self.cmp == ">=" else value <= self.target

    # --- 直列化 ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "schedule": self.schedule.to_dict(),
            "cmp": self.cmp,
            "target": self.target,
            "unit": self.unit,
            "role": self.role,
            "created": self.created.isoformat(),
            "archived": self.archived,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Habit":
        return cls(
            id=raw["id"],
            name=raw.get("name", raw["id"]),
            kind=raw.get("kind", "check"),
            schedule=Schedule.from_dict(raw.get("schedule", {})),
            cmp=raw.get("cmp", ">="),
            target=raw.get("target"),
            unit=raw.get("unit", ""),
            role=raw.get("role", ""),
            created=dt.date.fromisoformat(raw["created"]) if raw.get("created") else dt.date.today(),
            archived=bool(raw.get("archived", False)),
        )

    def archived_copy(self, archived: bool = True) -> "Habit":
        return replace(self, archived=archived)


# ---------------------------------------------------------------------------
# 記録
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    """ある習慣の、ある日の記録。1習慣 × 1日 につき最大1件。"""

    habit_id: str
    date: dt.date
    value: float
    note: str = ""

    def to_dict(self) -> dict:
        out = {"habit": self.habit_id, "date": self.date.isoformat(), "value": self.value}
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "Entry":
        return cls(
            habit_id=raw["habit"],
            date=dt.date.fromisoformat(raw["date"]),
            value=float(raw["value"]),
            note=raw.get("note", ""),
        )
