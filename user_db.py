import json
import os
import sqlite3
from datetime import datetime, timedelta

from filelock import FileLock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
# On Render, mount a persistent disk and set DATA_DIR=/data (or similar).
DATA_ROOT = os.environ.get("DATA_DIR", os.path.join(CURRENT_DIR, "Json"))
os.makedirs(DATA_ROOT, exist_ok=True)

DB_PATH = os.path.join(DATA_ROOT, "users.db")
LEGACY_JSON_DIR = os.path.join(CURRENT_DIR, "Json")
USERS_JSON_FILE = os.path.join(LEGACY_JSON_DIR, "user_accounts.json")
UPGRADE_HISTORY_JSON_FILE = os.path.join(LEGACY_JSON_DIR, "upgrade_history.json")
LEGACY_DB_PATH = os.path.join(CURRENT_DIR, "users.db")

UPGRADE_PLANS = {
    "monthly": {"days": 30, "price": 2.99, "label": "Monthly"},
    "6month": {"days": 180, "price": 15.0, "label": "6 Months"},
    "yearly": {"days": 365, "price": 30.0, "label": "Yearly"},
}


def _lock_path(path: str) -> str:
    return path + ".lock"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL DEFAULT 'Unknown',
            plan TEXT NOT NULL DEFAULT 'Free Plan',
            expiry_date TEXT,
            is_banned INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS upgrade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT 'Unknown',
            plan_key TEXT,
            plan_label TEXT,
            duration_days INTEGER NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            upgraded_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _read_json_file(path: str, default):
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


def _migrate_legacy_sources() -> None:
    """Import legacy JSON/SQLite data into the active SQLite database once."""
    with FileLock(_lock_path(DB_PATH), timeout=30):
        conn = _connect()
        try:
            _init_schema(conn)
            existing = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            if existing:
                return

            imported = False

            if os.path.exists(LEGACY_DB_PATH) and os.path.abspath(LEGACY_DB_PATH) != os.path.abspath(DB_PATH):
                try:
                    legacy = sqlite3.connect(LEGACY_DB_PATH, timeout=15)
                    legacy.row_factory = sqlite3.Row
                    for row in legacy.execute("SELECT * FROM users").fetchall():
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO users (user_id, username, plan, expiry_date, is_banned)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                str(row["user_id"]),
                                row["username"] or "Unknown",
                                row["plan"] or "Free Plan",
                                row["expiry_date"],
                                int(bool(row["is_banned"])),
                            ),
                        )
                    for row in legacy.execute(
                        "SELECT id, user_id, username, plan_key, plan_label, duration_days, amount, upgraded_at "
                        "FROM upgrade_history ORDER BY id ASC"
                    ).fetchall():
                        conn.execute(
                            """
                            INSERT INTO upgrade_history
                            (id, user_id, username, plan_key, plan_label, duration_days, amount, upgraded_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                int(row["id"]),
                                str(row["user_id"]),
                                row["username"] or "Unknown",
                                row["plan_key"],
                                row["plan_label"],
                                int(row["duration_days"] or 0),
                                float(row["amount"] or 0.0),
                                row["upgraded_at"],
                            ),
                        )
                    legacy.close()
                    imported = True
                except Exception as exc:
                    print(f"Legacy SQLite migration skipped: {exc}")

            users_json = _read_json_file(USERS_JSON_FILE, dict)
            if users_json:
                for uid, user in users_json.items():
                    if not isinstance(user, dict):
                        continue
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO users (user_id, username, plan, expiry_date, is_banned)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            str(uid),
                            user.get("username") or "Unknown",
                            user.get("plan") or "Free Plan",
                            user.get("expiry_date"),
                            int(bool(user.get("is_banned", False))),
                        ),
                    )
                imported = True

            history_json = _read_json_file(UPGRADE_HISTORY_JSON_FILE, list)
            if history_json:
                for row in history_json:
                    if not isinstance(row, dict):
                        continue
                    conn.execute(
                        """
                        INSERT INTO upgrade_history
                        (user_id, username, plan_key, plan_label, duration_days, amount, upgraded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("user_id")),
                            row.get("username") or "Unknown",
                            row.get("plan_key"),
                            row.get("plan_label"),
                            int(row.get("duration_days") or 0),
                            float(row.get("amount") or 0.0),
                            row.get("upgraded_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                imported = True

            conn.commit()
            if imported:
                print(f"Migrated user data into SQLite at {DB_PATH}")
        finally:
            conn.close()


def init_db():
    """Ensure SQLite schema exists and legacy data is imported."""
    with FileLock(_lock_path(DB_PATH), timeout=30):
        conn = _connect()
        try:
            _init_schema(conn)
        finally:
            conn.close()
    _migrate_legacy_sources()


def get_user_plan(user_id, username="Unknown"):
    """Get the user's plan, register them if new, and auto-downgrade if expired."""
    uid = str(user_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with FileLock(_lock_path(DB_PATH), timeout=30):
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
            if not row:
                conn.execute(
                    """
                    INSERT INTO users (user_id, username, plan, expiry_date, is_banned)
                    VALUES (?, ?, 'Free Plan', NULL, 0)
                    """,
                    (uid, username or "Unknown"),
                )
                conn.commit()
                return "Free Plan"

            plan = row["plan"] or "Free Plan"
            expiry = row["expiry_date"]
            if plan == "Premium" and expiry and expiry < now_str:
                conn.execute(
                    "UPDATE users SET plan = 'Free Plan', expiry_date = NULL WHERE user_id = ?",
                    (uid,),
                )
                conn.commit()
                return "Free Plan"

            if (row["username"] or "Unknown") != (username or "Unknown"):
                conn.execute(
                    "UPDATE users SET username = ? WHERE user_id = ?",
                    (username or "Unknown", uid),
                )
                conn.commit()
            return plan
        finally:
            conn.close()


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

    with FileLock(_lock_path(DB_PATH), timeout=30):
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
            existing_username = (row["username"] if row else None) or "Unknown"
            conn.execute(
                """
                INSERT INTO users (user_id, username, plan, expiry_date, is_banned)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    plan = excluded.plan,
                    expiry_date = excluded.expiry_date
                """,
                (
                    uid,
                    username or existing_username,
                    plan,
                    expiry,
                    int(bool(row["is_banned"])) if row else 0,
                ),
            )

            if plan == "Premium" and record_upgrade:
                amount = float(upgrade_price or 0.0)
                safe_plan_key = plan_key or "custom"
                final_label = (plan_label or "").strip() or UPGRADE_PLANS.get(safe_plan_key, {}).get(
                    "label", safe_plan_key
                )
                conn.execute(
                    """
                    INSERT INTO upgrade_history
                    (user_id, username, plan_key, plan_label, duration_days, amount, upgraded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        username or existing_username,
                        safe_plan_key,
                        final_label,
                        final_days,
                        amount,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
            conn.commit()
        finally:
            conn.close()


def get_all_users_dict():
    """Fetch all users for the Admin Panel."""
    with FileLock(_lock_path(DB_PATH), timeout=30):
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM users").fetchall()
            return {
                str(row["user_id"]): {
                    "user_id": str(row["user_id"]),
                    "username": row["username"] or "Unknown",
                    "plan": row["plan"] or "Free Plan",
                    "expiry_date": row["expiry_date"],
                    "is_banned": bool(row["is_banned"]),
                }
                for row in rows
            }
        finally:
            conn.close()


def is_banned(user_id):
    """Checks if a user is currently banned."""
    uid = str(user_id)
    with FileLock(_lock_path(DB_PATH), timeout=30):
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT is_banned FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            return bool(row["is_banned"]) if row else False
        finally:
            conn.close()


def set_banned(user_id, status: bool):
    """Updates the ban status of a specific user."""
    uid = str(user_id)

    with FileLock(_lock_path(DB_PATH), timeout=30):
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET is_banned = ? WHERE user_id = ?",
                    (int(bool(status)), uid),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO users (user_id, username, plan, expiry_date, is_banned)
                    VALUES (?, 'Unknown', 'Free Plan', NULL, ?)
                    """,
                    (uid, int(bool(status))),
                )
            conn.commit()
        finally:
            conn.close()


def get_upgrade_history(user_id=None, limit=200):
    """Return latest upgrade transactions, optionally filtered by user_id."""
    with FileLock(_lock_path(DB_PATH), timeout=30):
        conn = _connect()
        try:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT * FROM upgrade_history
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (str(user_id), int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM upgrade_history
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def get_upgrade_summary(user_id=None):
    """Return aggregate upgrade stats (count and total revenue)."""
    with FileLock(_lock_path(DB_PATH), timeout=30):
        conn = _connect()
        try:
            if user_id:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total_upgrades, COALESCE(SUM(amount), 0) AS total_revenue
                    FROM upgrade_history
                    WHERE user_id = ?
                    """,
                    (str(user_id),),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total_upgrades, COALESCE(SUM(amount), 0) AS total_revenue
                    FROM upgrade_history
                    """
                ).fetchone()
            return {
                "total_upgrades": int(row["total_upgrades"] or 0),
                "total_revenue": float(row["total_revenue"] or 0.0),
            }
        finally:
            conn.close()


init_db()
