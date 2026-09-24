"""
storage.py — 習慣データ(JSON 1ファイル)の読み書き。

置き場所は既定で `habits_data/habits.json`(.gitignore 済み)。
環境変数 `HABITS_FILE` で差し替えられるので、Dropbox 等に置いて同期してもよい。
書き込みは一時ファイル + rename の原子的置換にして、途中終了で記録を失わないようにする。
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .models import Entry, Habit, HabitError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "habits_data" / "habits.json"
PATH_ENV = "HABITS_FILE"
SCHEMA_VERSION = 1


def default_path() -> Path:
    """記録ファイルのパス(環境変数 `HABITS_FILE` があればそちらを優先)。"""
    env = os.environ.get(PATH_ENV)
    return Path(env).expanduser() if env else DEFAULT_PATH


class Tracker:
    """習慣定義と記録のコンテナ。CLI からの操作はすべてここを経由する。"""

    def __init__(self, habits: list[Habit] | None = None, entries: list[Entry] | None = None,
                 path: Path | None = None) -> None:
        self.habits: list[Habit] = habits or []
        self.entries: list[Entry] = entries or []
        self.path = path or default_path()

    # --- 入出力 ------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Tracker":
        path = path or default_path()
        if not path.exists():
            return cls(path=path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        habits = [Habit.from_dict(h) for h in raw.get("habits", [])]
        entries = [Entry.from_dict(e) for e in raw.get("entries", [])]
        return cls(habits=habits, entries=entries, path=path)

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "habits": [h.to_dict() for h in self.habits],
            "entries": [e.to_dict() for e in sorted(self.entries, key=lambda e: (e.date, e.habit_id))],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)  # 原子的置換:書き込み中断で既存データを壊さない
        return self.path

    # --- 習慣 --------------------------------------------------------------
    def active_habits(self) -> list[Habit]:
        return [h for h in self.habits if not h.archived]

    def get(self, habit_id: str) -> Habit:
        """ID の完全一致、なければ前方一致1件だけを許して解決する。"""
        for h in self.habits:
            if h.id == habit_id:
                return h
        matches = [h for h in self.habits if h.id.startswith(habit_id)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise HabitError(f"ID が曖昧: {habit_id!r} → {', '.join(m.id for m in matches)}")
        raise HabitError(f"習慣が見つからない: {habit_id!r}(`habit list` で確認)")

    def add_habit(self, habit: Habit) -> Habit:
        if any(h.id == habit.id for h in self.habits):
            raise HabitError(f"ID が重複している: {habit.id!r}")
        self.habits.append(habit)
        return habit

    def remove_habit(self, habit_id: str) -> Habit:
        habit = self.get(habit_id)
        self.habits = [h for h in self.habits if h.id != habit.id]
        self.entries = [e for e in self.entries if e.habit_id != habit.id]
        return habit

    def set_archived(self, habit_id: str, archived: bool) -> Habit:
        habit = self.get(habit_id)
        habit.archived = archived
        return habit

    # --- 記録 --------------------------------------------------------------
    def entries_of(self, habit_id: str) -> list[Entry]:
        return sorted((e for e in self.entries if e.habit_id == habit_id), key=lambda e: e.date)

    def entry_on(self, habit_id: str, day: dt.date) -> Entry | None:
        for e in self.entries:
            if e.habit_id == habit_id and e.date == day:
                return e
        return None

    def record(self, habit: Habit, day: dt.date, value: float, note: str = "") -> tuple[Entry, bool]:
        """記録を追加・更新する。戻り値は (記録, 上書きだったか)。"""
        existing = self.entry_on(habit.id, day)
        if existing is not None:
            existing.value = value
            if note:
                existing.note = note
            return existing, True
        entry = Entry(habit_id=habit.id, date=day, value=value, note=note)
        self.entries.append(entry)
        return entry, False

    def unrecord(self, habit: Habit, day: dt.date) -> Entry:
        entry = self.entry_on(habit.id, day)
        if entry is None:
            raise HabitError(f"{habit.id} の {day.isoformat()} に記録がない")
        self.entries = [e for e in self.entries if not (e.habit_id == habit.id and e.date == day)]
        return entry
