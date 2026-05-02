"""
Chat history retention: keep entries within the last N days (default 180 ≈ 6 months)
for training exports while limiting long-term storage of user data.
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, List, Tuple

from filelock import FileLock

RETENTION_DAYS = int(os.environ.get("CHAT_HISTORY_RETENTION_DAYS", "180"))


def _entry_datetime(entry: Dict[str, Any]) -> datetime.datetime | None:
    ts = entry.get("timestamp")
    if not ts or not isinstance(ts, str):
        return None
    ts = ts.strip()
    if len(ts) >= 19:
        try:
            return datetime.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if len(ts) >= 16:
        try:
            return datetime.datetime.strptime(ts[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    if len(ts) >= 10:
        try:
            return datetime.datetime.strptime(ts[:10], "%Y-%m-%d")
        except ValueError:
            pass
    return None


def filter_retention(
    entries: List[Dict[str, Any]], retention_days: int | None = None
) -> Tuple[List[Dict[str, Any]], int]:
    days = RETENTION_DAYS if retention_days is None else retention_days
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    kept: List[Dict[str, Any]] = []
    removed = 0
    for e in entries:
        dt = _entry_datetime(e)
        if dt is None:
            kept.append(e)
        elif dt >= cutoff:
            kept.append(e)
        else:
            removed += 1
    return kept, removed


def run_history_retention_once(history_path: str) -> int:
    """Load history under lock, drop entries older than retention window, save if changed."""
    lock_path = history_path + ".lock"
    with FileLock(lock_path, timeout=5):
        data: List[Dict[str, Any]] = []
        if os.path.exists(history_path):
            try:
                with open(history_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    data = raw
            except Exception:
                return 0
        kept, removed = filter_retention(data)
        if removed:
            with open(history_path, "w", encoding="utf-8") as f:
                json.dump(kept, f, ensure_ascii=False, indent=2)
        return removed
