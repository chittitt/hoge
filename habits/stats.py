"""
stats.py — 継続率・ストリーク(連続達成)の集計。

集計の単位を「期間(period)」に抽象化してある。
  - daily / 曜日指定 → 対象日 1日 = 1期間
  - 週N回           → 月曜はじまりの 1週 = 1期間(週内の達成回数が N 回以上で達成)

各期間の状態は done(達成) / missed(未達) / pending(まだ判定できない)の3値。
未来や当日など pending は母数に入れない。「まだやってないだけ」で継続率が
下がると記録をやめたくなるため。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .models import Entry, Habit

DONE, MISSED, PENDING = "done", "missed", "pending"

MARKS = {DONE: "■", MISSED: "×", PENDING: "□"}
MARK_OFFDAY = "・"


def week_start(day: dt.date) -> dt.date:
    """その日を含む週(月曜はじまり)の初日。"""
    return day - dt.timedelta(days=day.weekday())


@dataclass
class Period:
    """判定の単位。日次なら 1日、週N回なら 1週。"""

    start: dt.date
    end: dt.date
    status: str
    value: float | None = None  # 日次のみ:その日の記録値
    count: int = 0              # 週次のみ:週内の達成回数


@dataclass
class HabitStats:
    """1習慣の集計結果。"""

    habit: Habit
    periods: list[Period] = field(default_factory=list)
    done: int = 0
    missed: int = 0
    pending: int = 0
    streak: int = 0
    best_streak: int = 0
    values: list[float] = field(default_factory=list)

    @property
    def judged(self) -> int:
        """達成率の母数(pending を除いた期間数)。"""
        return self.done + self.missed

    @property
    def rate(self) -> float | None:
        return self.done / self.judged if self.judged else None

    @property
    def average(self) -> float | None:
        if self.habit.kind == "check" or not self.values:
            return None
        return sum(self.values) / len(self.values)

    @property
    def unit_label(self) -> str:
        return "週" if self.habit.schedule.is_weekly else "日"


def _daily_periods(habit: Habit, entries: dict[dt.date, Entry], start: dt.date,
                   end: dt.date, today: dt.date) -> list[Period]:
    periods: list[Period] = []
    day = start
    while day <= end:
        if habit.schedule.is_scheduled(day):
            entry = entries.get(day)
            if entry is not None:
                status = DONE if habit.achieved(entry.value) else MISSED
                periods.append(Period(day, day, status, value=entry.value))
            else:
                periods.append(Period(day, day, MISSED if day < today else PENDING))
        day += dt.timedelta(days=1)
    return periods


def _weekly_periods(habit: Habit, entries: dict[dt.date, Entry], start: dt.date,
                    end: dt.date, today: dt.date) -> list[Period]:
    periods: list[Period] = []
    cursor = week_start(start)
    while cursor <= end:
        w_end = cursor + dt.timedelta(days=6)
        count = sum(
            1 for day, e in entries.items()
            if cursor <= day <= w_end and habit.achieved(e.value)
        )
        if count >= habit.schedule.times:
            status = DONE
        elif w_end < today:
            status = MISSED
        else:
            status = PENDING
        periods.append(Period(cursor, w_end, status, count=count))
        cursor += dt.timedelta(days=7)
    return periods


def first_day(habit: Habit, by_date: dict[dt.date, Entry]) -> dt.date:
    """集計の左端。登録日より前は対象外だが、遡って記録した日があればそこまで含める。"""
    return min([habit.created, *by_date.keys()]) if by_date else habit.created


def build_periods(habit: Habit, entries: list[Entry], start: dt.date, end: dt.date,
                  today: dt.date) -> list[Period]:
    """[start, end] を習慣のスケジュールに沿った期間列に展開し、状態を判定する。

    登録前の日付は「対象外」として期間に含めない(始める前の日で継続率が
    下がらないようにする)。
    """
    by_date = {e.date: e for e in entries if e.habit_id == habit.id}
    start = max(start, first_day(habit, by_date))
    if start > end:
        return []
    if habit.schedule.is_weekly:
        return _weekly_periods(habit, by_date, start, end, today)
    return _daily_periods(habit, by_date, start, end, today)


def current_streak(periods: list[Period]) -> int:
    """末尾から数えた連続達成数。末尾の pending(今日まだ等)は素通しする。"""
    streak = 0
    for period in reversed(periods):
        if period.status == PENDING:
            continue  # 今日ぶんが未記録でも、昨日までの連続は途切れさせない
        if period.status == DONE:
            streak += 1
            continue
        break
    return streak


def best_streak(periods: list[Period]) -> int:
    """履歴全体での最長連続達成数(pending は連続を打ち切る)。"""
    best = run = 0
    for period in periods:
        if period.status == DONE:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def summarize(habit: Habit, entries: list[Entry], start: dt.date, end: dt.date,
              today: dt.date | None = None) -> HabitStats:
    """[start, end] の成績と、履歴全体での最長ストリークをまとめる。"""
    today = today or dt.date.today()
    periods = build_periods(habit, entries, start, end, today)
    stats = HabitStats(habit=habit, periods=periods)
    for period in periods:
        if period.status == DONE:
            stats.done += 1
        elif period.status == MISSED:
            stats.missed += 1
        else:
            stats.pending += 1

    # ストリークは「窓の切り方」で変わってはいけないので、常に全履歴で数える
    history = build_periods(habit, entries, dt.date.min, max(end, today), today)
    stats.streak = current_streak(history)
    stats.best_streak = best_streak(history)

    stats.values = [
        e.value for e in entries
        if e.habit_id == habit.id and start <= e.date <= end
    ]
    return stats


def calendar_line(habit: Habit, entries: list[Entry], start: dt.date, end: dt.date,
                  today: dt.date | None = None) -> str:
    """直近の達成状況を1行の記号列にする(■達成 ×未達 □未記録 ・対象外)。"""
    today = today or dt.date.today()
    periods = {p.start: p for p in build_periods(habit, entries, start, end, today)}
    marks: list[str] = []
    day = start
    while day <= end:
        if habit.schedule.is_weekly:
            period = periods.get(week_start(day))
            marks.append(MARKS[period.status] if period else MARK_OFFDAY)
        else:
            period = periods.get(day)
            marks.append(MARKS[period.status] if period else MARK_OFFDAY)
        day += dt.timedelta(days=1)
    return "".join(marks)
