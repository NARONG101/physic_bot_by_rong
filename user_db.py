import sqlite3
import os
from datetime import datetime, timedelta

# Create the SQLite database in the current directory
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'users.db')

UPGRADE_PLANS = {
    "monthly": {"days": 30, "price": 2.99, "label": "Monthly"},
    "6month": {"days": 180, "price": 15.0, "label": "6 Months"},
    "yearly": {"days": 365, "price": 30.0, "label": "Yearly"},
}

def get_db():
    """Establish a connection to the SQLite database."""
    # check_same_thread=False allows Flask (admin_web) and Asyncio (bot) to share the DB safely
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Allows accessing columns by name like a dictionary
    
    # Enable Write-Ahead Logging for better concurrent read/write performance
    conn.execute('pragma journal_mode=wal')
    return conn

def init_db():
    """Initialize the database and ensure all columns exist."""
    with get_db() as conn:
        cursor = conn.cursor()
        # Create table with the new is_banned column for brand new databases
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                plan TEXT DEFAULT 'Free Plan',
                expiry_date TEXT,
                is_banned INTEGER DEFAULT 0
            )
        ''')
        
        # 🚀 Safe Migration: Add the column to existing databases if it's missing
        try:
            cursor.execute('ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            # OperationalError means the column already exists, which is perfectly fine.
            pass 

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS upgrade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                username TEXT,
                plan_key TEXT NOT NULL,
                plan_label TEXT NOT NULL,
                duration_days INTEGER NOT NULL,
                amount REAL NOT NULL,
                upgraded_at TEXT NOT NULL
            )
        ''')
            
        conn.commit()

# Auto-initialize table on import
init_db()

def get_user_plan(user_id, username="Unknown"):
    """Get the user's plan, register them if new, and auto-downgrade if expired."""
    uid = str(user_id)
    # Use full precise time for accurate 30-day expirations
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with get_db() as conn:
        cursor = conn.cursor()
        user = cursor.execute('SELECT * FROM users WHERE user_id = ?', (uid,)).fetchone()
        
        # 1. If user doesn't exist, create them as Free Plan
        if not user:
            cursor.execute('INSERT INTO users (user_id, username, plan, is_banned) VALUES (?, ?, ?, ?)', 
                           (uid, username, 'Free Plan', 0))
            conn.commit()
            return 'Free Plan'
            
        # 2. Check if Premium has expired (auto-downgrade)
        if user['plan'] == 'Premium' and user['expiry_date'] and user['expiry_date'] < now_str:
            cursor.execute('UPDATE users SET plan = ?, expiry_date = NULL WHERE user_id = ?', 
                           ('Free Plan', uid))
            conn.commit()
            return 'Free Plan'
            
        # 3. Update username if they changed it on Telegram
        if user['username'] != username:
            cursor.execute('UPDATE users SET username = ? WHERE user_id = ?', (username, uid))
            conn.commit()

        return user['plan']

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
    """Update a user's plan (Triggered from Admin Dashboard)."""
    uid = str(user_id)
    
    # If upgrading to Premium, default expiry is 30 days unless a custom duration is provided.
    if plan == "Premium":
        final_days = int(duration_days) if duration_days else 30
        expiry = (datetime.now() + timedelta(days=final_days)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        final_days = 0
        expiry = None

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (user_id, username, plan, expiry_date, is_banned) 
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET 
                username=excluded.username, 
                plan=excluded.plan, 
                expiry_date=excluded.expiry_date
        ''', (uid, username, plan, expiry))

        if plan == "Premium" and record_upgrade:
            amount = float(upgrade_price or 0.0)
            safe_plan_key = plan_key or "custom"
            final_label = (plan_label or "").strip() or UPGRADE_PLANS.get(safe_plan_key, {}).get("label", safe_plan_key)
            cursor.execute('''
                INSERT INTO upgrade_history (
                    user_id, username, plan_key, plan_label, duration_days, amount, upgraded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                uid,
                username or "Unknown",
                safe_plan_key,
                final_label,
                final_days,
                amount,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
        conn.commit()

def get_all_users_dict():
    """Fetch all users from the database to display in the Admin Panel."""
    with get_db() as conn:
        rows = conn.cursor().execute('SELECT * FROM users').fetchall()
        # Return a dictionary mapped by user_id for easy lookup in Admin panel
        return {row['user_id']: dict(row) for row in rows}

# --- 🚀 BAN FEATURES ---

def is_banned(user_id):
    """Checks if a user is currently banned."""
    uid = str(user_id)
    with get_db() as conn:
        user = conn.cursor().execute('SELECT is_banned FROM users WHERE user_id = ?', (uid,)).fetchone()
        if user:
            return bool(user['is_banned'])
        return False

def set_banned(user_id, status: bool):
    """Updates the ban status of a specific user."""
    uid = str(user_id)
    banned_int = 1 if status else 0 
    
    with get_db() as conn:
        # Use UPSERT to allow banning an ID even if they haven't messaged the bot yet
        # FIXED: Provided all 4 values for the 4 columns in the INSERT statement
        conn.cursor().execute('''
            INSERT INTO users (user_id, username, plan, is_banned) 
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
                is_banned=excluded.is_banned
        ''', (uid, 'Unknown', 'Free Plan', banned_int))
        conn.commit()


def get_upgrade_history(user_id=None, limit=200):
    """Return latest upgrade transactions, optionally filtered by user_id."""
    sql = '''
        SELECT id, user_id, username, plan_key, plan_label, duration_days, amount, upgraded_at
        FROM upgrade_history
    '''
    params = []
    if user_id:
        sql += ' WHERE user_id = ?'
        params.append(str(user_id))
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(int(limit))

    with get_db() as conn:
        rows = conn.cursor().execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def get_upgrade_summary(user_id=None):
    """Return aggregate upgrade stats (count and total revenue)."""
    sql = 'SELECT COUNT(*) AS total_upgrades, COALESCE(SUM(amount), 0) AS total_revenue FROM upgrade_history'
    params = []
    if user_id:
        sql += ' WHERE user_id = ?'
        params.append(str(user_id))

    with get_db() as conn:
        row = conn.cursor().execute(sql, params).fetchone()
        return {
            "total_upgrades": int(row["total_upgrades"] or 0),
            "total_revenue": float(row["total_revenue"] or 0.0),
        }