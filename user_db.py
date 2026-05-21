import json
import os
import sqlite3
from datetime import datetime, timedelta

from filelock import FileLock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_DIR = os.path.join(CURRENT_DIR, "Json")
os.makedirs(JSON_DIR, exist_ok=True)

USERS_FILE = os.path.join(JSON_DIR, "user_accounts.json")
UPGRADE_HISTORY_FILE = os.path.join(JSON_DIR, "upgrade_history.json")
LEGACY_DB_PATH = os.path.join(CURRENT_DIR, "users.db")

UPGRADE_PLANS = {
    "monthly": {"days": 30, "price": 2.99, "label": "Monthly"},
    "6month": {"days": 180, "price": 15.0, "label": "6 Months"},
    "yearly": {"days": 365, "price": 30.0, "label": "Yearly"},
}


def _lock_path(path: str) -> str:
    return path + ".lock"


def _read_json_unlocked(path: str, default):
    if not os.path.exists(path):
        return default() if callable(default) else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return default() if callable(default) else default
    if isinstance(default, dict) and isinstance(data, dict):
        return data
    if isinstance(default, list) and isinstance(data, list):
        return data
    return default() if callable(default) else default


def _write_json_unlocked(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_json(path: str, default):
    with FileLock(_lock_path(path), timeout=15):
        return _read_json_unlocked(path, default)


def _write_json(path: str, data) -> None:
    with FileLock(_lock_path(path), timeout=15):
        _write_json_unlocked(path, data)


def _load_users() -> dict:
    return _read_json(USERS_FILE, dict)


def _save_users(users: dict) -> None:
    _write_json(USERS_FILE, users)


def _load_upgrade_history() -> list:
    return _read_json(UPGRADE_HISTORY_FILE, list)


def _save_upgrade_history(records: list) -> None:
    _write_json(UPGRADE_HISTORY_FILE, records)


def _next_upgrade_id(records: list) -> int:
    if not records:
        return 1
    return max(int(r.get("id", 0) or 0) for r in records) + 1


def _migrate_sqlite_if_needed() -> None:
    """One-time import from legacy users.db into JSON files."""
    if not os.path.exists(LEGACY_DB_PATH):
        return
    users = _load_users()
    history = _load_upgrade_history()
    if users and history:
        return

    try:
        conn = sqlite3.connect(LEGACY_DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if not users:
            for row in cursor.execute("SELECT * FROM users").fetchall():
                uid = str(row["user_id"])
                users[uid] = {
                    "user_id": uid,
                    "username": row["username"] or "Unknown",
                    "plan": row["plan"] or "Free Plan",
                    "expiry_date": row["expiry_date"],
                    "is_banned": bool(row["is_banned"]),
                }
        if not history:
            for row in cursor.execute(
                "SELECT id, user_id, username, plan_key, plan_label, duration_days, amount, upgraded_at "
                "FROM upgrade_history ORDER BY id ASC"
            ).fetchall():
                history.append(
                    {
                        "id": int(row["id"]),
                        "user_id": str(row["user_id"]),
                        "username": row["username"] or "Unknown",
                        "plan_key": row["plan_key"],
                        "plan_label": row["plan_label"],
                        "duration_days": int(row["duration_days"]),
                        "amount": float(row["amount"] or 0.0),
                        "upgraded_at": row["upgraded_at"],
                    }
                )
        conn.close()
        if users:
            _save_users(users)
        if history:
            _save_upgrade_history(history)
        print("Migrated user data from SQLite (users.db) to JSON files.")
    except Exception as exc:
        print(f"SQLite migration skipped: {exc}")


def init_db():
    """Ensure JSON data files exist (replaces SQLite init)."""
    if not os.path.exists(USERS_FILE):
        _save_users({})
    if not os.path.exists(UPGRADE_HISTORY_FILE):
        _save_upgrade_history([])
    _migrate_sqlite_if_needed()


init_db()


def get_user_plan(user_id, username="Unknown"):
    """Get the user's plan, register them if new, and auto-downgrade if expired."""
    uid = str(user_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with FileLock(_lock_path(USERS_FILE), timeout=15):
        users = _read_json_unlocked(USERS_FILE, dict)
        user = users.get(uid)

        if not user:
            users[uid] = {
                "user_id": uid,
                "username": username,
                "plan": "Free Plan",
                "expiry_date": None,
                "is_banned": False,
            }
            _write_json_unlocked(USERS_FILE, users)
            return "Free Plan"

        plan = user.get("plan", "Free Plan")
        expiry = user.get("expiry_date")

        if plan == "Premium" and expiry and expiry < now_str:
            user["plan"] = "Free Plan"
            user["expiry_date"] = None
            users[uid] = user
            _write_json_unlocked(USERS_FILE, users)
            return "Free Plan"

        if user.get("username") != username:
            user["username"] = username
            users[uid] = user
            _write_json_unlocked(USERS_FILE, users)

        return user.get("plan", "Free Plan")


def set_user_plan(
    user_id,
    username,
    plan,
    duration_days=None,
    upgrade_price=None,
    record_upgrade=False,
    plan_key=None,
    plan_label=None,
):
    """Update a user's plan (triggered from Admin Dashboard)."""
    uid = str(user_id)

    if plan == "Premium":
        final_days = int(duration_days) if duration_days else 30
        expiry = (datetime.now() + timedelta(days=final_days)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        final_days = 0
        expiry = None

    with FileLock(_lock_path(USERS_FILE), timeout=15):
        users = _read_json_unlocked(USERS_FILE, dict)
        existing = users.get(uid, {})
        users[uid] = {
            "user_id": uid,
            "username": username or existing.get("username", "Unknown"),
            "plan": plan,
            "expiry_date": expiry,
            "is_banned": bool(existing.get("is_banned", False)),
        }
        _write_json_unlocked(USERS_FILE, users)

    if plan == "Premium" and record_upgrade:
        amount = float(upgrade_price or 0.0)
        safe_plan_key = plan_key or "custom"
        final_label = (plan_label or "").strip() or UPGRADE_PLANS.get(safe_plan_key, {}).get(
            "label", safe_plan_key
        )
        with FileLock(_lock_path(UPGRADE_HISTORY_FILE), timeout=15):
            records = _read_json_unlocked(UPGRADE_HISTORY_FILE, list)
            records.append(
                {
                    "id": _next_upgrade_id(records),
                    "user_id": uid,
                    "username": username or "Unknown",
                    "plan_key": safe_plan_key,
                    "plan_label": final_label,
                    "duration_days": final_days,
                    "amount": amount,
                    "upgraded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            _write_json_unlocked(UPGRADE_HISTORY_FILE, records)


def get_all_users_dict():
    """Fetch all users for the Admin Panel."""
    users = _load_users()
    return {uid: dict(data) for uid, data in users.items()}


def is_banned(user_id):
    """Checks if a user is currently banned."""
    uid = str(user_id)
    user = _load_users().get(uid)
    if user:
        return bool(user.get("is_banned", False))
    return False


def set_banned(user_id, status: bool):
    """Updates the ban status of a specific user."""
    uid = str(user_id)

    with FileLock(_lock_path(USERS_FILE), timeout=15):
        users = _read_json_unlocked(USERS_FILE, dict)
        existing = users.get(uid, {})
        users[uid] = {
            "user_id": uid,
            "username": existing.get("username", "Unknown"),
            "plan": existing.get("plan", "Free Plan"),
            "expiry_date": existing.get("expiry_date"),
            "is_banned": bool(status),
        }
        _write_json_unlocked(USERS_FILE, users)


def get_upgrade_history(user_id=None, limit=200):
    """Return latest upgrade transactions, optionally filtered by user_id."""
    records = _load_upgrade_history()
    if user_id:
        uid = str(user_id)
        records = [r for r in records if str(r.get("user_id")) == uid]
    records = sorted(records, key=lambda r: int(r.get("id", 0) or 0), reverse=True)
    return records[: int(limit)]


def get_upgrade_summary(user_id=None):
    """Return aggregate upgrade stats (count and total revenue)."""
    records = _load_upgrade_history()
    if user_id:
        uid = str(user_id)
        records = [r for r in records if str(r.get("user_id")) == uid]
    total_upgrades = len(records)
    total_revenue = sum(float(r.get("amount", 0) or 0) for r in records)
    return {
        "total_upgrades": total_upgrades,
        "total_revenue": total_revenue,
    }
