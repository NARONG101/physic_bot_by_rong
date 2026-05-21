import os
import io
import json
import time
import hmac
import hashlib
import requests
import html
import csv
import datetime
from collections import defaultdict
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, flash, send_from_directory, session, Response, abort
from dotenv import load_dotenv
from filelock import FileLock
from werkzeug.utils import secure_filename

from history_retention import filter_retention 

# 🚀 Import JSON-backed user manager (persists in Json/ like feedback)
import user_db

# Load Environment Variables from this package folder
dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(dotenv_path)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

# Stable dev default so the Flask debug reloader (and file saves) do not log you out every time.
# In production, always set a strong, unique FLASK_SECRET_KEY in the environment.
_DEV_SESSION_FALLBACK = 'dev-phy-admin-session-key-CHANGE-IN-PRODUCTION'

_FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'true').lower() in ('1', 'true', 'yes')
_flask_secret = os.getenv('FLASK_SECRET_KEY')
if not _flask_secret:
    if _FLASK_DEBUG:
        _flask_secret = _DEV_SESSION_FALLBACK
        print(
            'NOTE: FLASK_SECRET_KEY is not set; using a built-in dev key (sessions survive reloads). '
            'Set FLASK_SECRET_KEY in .env for production or a non-default local secret.'
        )
    else:
        raise ValueError('FLASK_SECRET_KEY is required when FLASK_DEBUG is disabled.')

app = Flask(__name__)
app.secret_key = _flask_secret
# Longer so normal admin work is not cut off; cookie is refreshed on activity (see _roll_admin_session).
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(hours=24)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
_use_secure_cookies = (
    os.getenv('FLASK_ENV', '').lower() == 'production'
    or os.getenv('ADMIN_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes')
)
app.config['SESSION_COOKIE_SECURE'] = _use_secure_cookies


@app.before_request
def _roll_admin_session():
    """Keep the signed session cookie current so working in the panel does not time out at a fixed wall clock."""
    if session.get('logged_in'):
        session.permanent = True
        session.modified = True


# --- ADMIN CREDENTIALS (never commit real passwords; use .env) ---
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
if not ADMIN_PASSWORD:
    if _FLASK_DEBUG:
        ADMIN_PASSWORD = 'physics2026'
        print('WARNING: ADMIN_PASSWORD is not set; using a weak default for local dev only. Set ADMIN_PASSWORD in .env before hosting.')
    else:
        raise ValueError('ADMIN_PASSWORD is required when FLASK_DEBUG is disabled.')

# Simple in-memory login throttling (per server process; sufficient for a single admin host)
_login_fail_tracker = {}  # ip -> [count, lockout_until_epoch]

def _digest256(value: str) -> bytes:
    return hashlib.sha256((value or '').encode('utf-8')).digest() 

# --- PATHS CONFIGURATION ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_DIR = os.path.join(CURRENT_DIR, 'Json') 
UPLOAD_DIR = os.path.join(CURRENT_DIR, 'uploads')

os.makedirs(JSON_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True) 

HISTORY_FILE = os.path.join(JSON_DIR, 'chat_history.json')
KNOWLEDGE_FILE = os.path.join(JSON_DIR, 'Data_Training.json')
SETTINGS_FILE = os.path.join(JSON_DIR, 'bot_settings.json') 
FEEDBACK_FILE = os.path.join(JSON_DIR, 'user_feedback.json')
AVATAR_CACHE_TTL_SECONDS = 600
_avatar_cache = {}
UPGRADE_OPTIONS = {
    "monthly": {"label": "Monthly", "days": 30, "price": 2.99},
    "6month": {"label": "6 Months", "days": 180, "price": 15.0},
    "yearly": {"label": "Yearly", "days": 365, "price": 30.0},
}


def _get_upgrade_pricing(settings: dict) -> dict:
    """Return validated upgrade pricing config from settings with safe defaults."""
    raw = settings.get("upgrade_pricing") if isinstance(settings, dict) else None
    base = {k: dict(v) for k, v in UPGRADE_OPTIONS.items()}
    if not isinstance(raw, dict):
        return base
    for key in ("monthly", "6month", "yearly"):
        item = raw.get(key)
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", base[key]["label"])).strip() or base[key]["label"]
        try:
            days = int(item.get("days", base[key]["days"]))
        except Exception:
            days = base[key]["days"]
        try:
            price = float(item.get("price", base[key]["price"]))
        except Exception:
            price = base[key]["price"]
        base[key] = {"label": label, "days": max(1, days), "price": max(0.0, price)}
    return base

# --- SECURITY DECORATOR ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            flash("Please log in to access the Admin Panel.", "danger")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- ROUTE TO SERVE IMAGES ---
@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    if '/' in filename or '\\' in filename:
        abort(404)
    safe = secure_filename(os.path.basename(filename))
    if not safe:
        abort(404)
    full_path = os.path.abspath(os.path.join(UPLOAD_DIR, safe))
    uploads_root = os.path.abspath(UPLOAD_DIR)
    try:
        if os.path.commonpath([full_path, uploads_root]) != uploads_root:
            abort(404)
    except ValueError:
        abort(404)
    if not os.path.isfile(full_path):
        abort(404)
    return send_from_directory(UPLOAD_DIR, safe)


@app.route('/avatar/<user_id>')
@login_required
def user_avatar(user_id):
    if not str(user_id).isdigit():
        return Response(_placeholder_avatar_svg(), mimetype='image/svg+xml')
    blob, mime = _get_avatar_blob_for_user(int(user_id))
    if blob:
        return Response(blob, mimetype=mime)
    return Response(_placeholder_avatar_svg(), mimetype='image/svg+xml')

# --- Helper Functions with FileLocks ---
def get_bot_settings():
    lock_path = SETTINGS_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: pass
    
    return {
        "status": "online", "free_limit": 10, "premium_limit": 40, "temperature": 0.2,
        "cooldown_seconds": 2, "max_input_length": 1000, "support_contact": "admin",
        "model_name": "gemini-3.1-flash-lite-preview",
        "api_retry_attempts": 3,
        "api_retry_base_delay": 4,
        "system_prompt": "You are Phy_Chatbot, an expert physics tutor.",
        "welcome_message": "Welcome to Phy_Chatbot! ⚛️ Send me a physics question to get started.",
        "maintenance_message": "The bot is currently undergoing upgrades to serve you better. Please check back later!",
        "upgrade_pricing": {
            "monthly": {"label": "Monthly", "days": 30, "price": 2.99},
            "6month": {"label": "6 Months", "days": 180, "price": 15.0},
            "yearly": {"label": "Yearly", "days": 365, "price": 30.0}
        },
        "features": {
            "free": {"image_analysis": False, "step_by_step": True, "pdf_reading": False, "generate_quizzes": False},
            "premium": {"image_analysis": True, "step_by_step": True, "pdf_reading": True, "generate_quizzes": True}
        }
    }

def save_bot_settings(data):
    lock_path = SETTINGS_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)

def get_chat_data():
    lock_path = HISTORY_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        data = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    data = raw
            except Exception:
                return []
        kept, removed = filter_retention(data)
        if removed:
            try:
                with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(kept, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        return kept


def get_feedback_data():
    lock_path = FEEDBACK_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        if not os.path.exists(FEEDBACK_FILE):
            return []
        try:
            with open(FEEDBACK_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
                return raw if isinstance(raw, list) else []
        except Exception:
            return []


def _chat_delete_token(entry):
    eid = entry.get('id')
    if eid:
        return str(eid)
    payload = {
        'timestamp': entry.get('timestamp'),
        'user_id': entry.get('user_id'),
        'username': entry.get('username'),
        'user_input': entry.get('user_input'),
        'bot_response': entry.get('bot_response'),
        'image_file': entry.get('image_file'),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return 'legacy:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _feedback_delete_token(entry):
    eid = entry.get('id')
    if eid:
        return str(eid)
    payload = {
        'timestamp': entry.get('timestamp'),
        'user_id': entry.get('user_id'),
        'username': entry.get('username'),
        'full_name': entry.get('full_name'),
        'feedback_text': entry.get('feedback_text'),
        'image_file': entry.get('image_file'),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return 'legacy:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _maybe_remove_upload(filename):
    if not filename:
        return
    raw = str(filename)
    if '/' in raw or '\\' in raw:
        return
    safe = secure_filename(os.path.basename(raw))
    if not safe:
        return
    full_path = os.path.abspath(os.path.join(UPLOAD_DIR, safe))
    uploads_root = os.path.abspath(UPLOAD_DIR)
    try:
        if os.path.commonpath([full_path, uploads_root]) != uploads_root:
            return
    except ValueError:
        return
    try:
        if os.path.isfile(full_path):
            os.remove(full_path)
    except OSError:
        pass


def _placeholder_avatar_svg():
    return """<svg xmlns='http://www.w3.org/2000/svg' width='96' height='96' viewBox='0 0 96 96'>
    <rect width='96' height='96' rx='48' fill='#e2e8f0'/>
    <circle cx='48' cy='36' r='16' fill='#94a3b8'/>
    <path d='M20 80c4-14 14-22 28-22s24 8 28 22' fill='#94a3b8'/>
    </svg>"""


def _get_avatar_blob_for_user(user_id):
    now = time.time()
    cached = _avatar_cache.get(str(user_id))
    if cached and cached.get("expires_at", 0) > now:
        return cached.get("data"), cached.get("mime")

    try:
        photos_resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUserProfilePhotos",
            params={"user_id": user_id, "limit": 1},
            timeout=8
        )
        photos_json = photos_resp.json() if photos_resp.ok else {}
        photos = photos_json.get("result", {}).get("photos", [])
        if not photos:
            return None, None

        file_id = photos[0][-1].get("file_id")
        if not file_id:
            return None, None

        file_resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=8
        )
        file_json = file_resp.json() if file_resp.ok else {}
        file_path = file_json.get("result", {}).get("file_path")
        if not file_path:
            return None, None

        image_resp = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}",
            timeout=10
        )
        if not image_resp.ok:
            return None, None

        mime = image_resp.headers.get("Content-Type", "image/jpeg")
        data = image_resp.content
        _avatar_cache[str(user_id)] = {
            "expires_at": now + AVATAR_CACHE_TTL_SECONDS,
            "data": data,
            "mime": mime
        }
        return data, mime
    except Exception:
        return None, None

def get_user_stats(chat_data):
    users = {}
    
    for chat in chat_data:
        uid = str(chat.get('user_id'))
        if uid:
            if uid not in users:
                users[uid] = {
                    'username': chat.get('username', 'unknown'),
                    'first_seen': chat.get('timestamp', ''),
                    'last_seen': chat.get('timestamp', ''),
                    'message_count': 1,
                }
            else:
                users[uid]['last_seen'] = chat.get('timestamp', '')
                users[uid]['message_count'] += 1
                users[uid]['username'] = chat.get('username', 'unknown')
                
    db_users = user_db.get_all_users_dict()
    for uid, data in db_users.items():
        if uid not in users:
            users[uid] = {
                'username': data.get('username', 'Unknown'),
                'first_seen': 'N/A',
                'last_seen': 'N/A',
                'message_count': 0,
            }
        users[uid]['plan'] = data.get('plan', 'Free Plan')
        users[uid]['expiry_date'] = data.get('expiry_date') or 'N/A'
        users[uid]['is_banned'] = data.get('is_banned', False)

    for uid in users:
        if 'plan' not in users[uid]:
            users[uid]['plan'] = 'Free Plan'
            users[uid]['expiry_date'] = 'N/A'
        if 'is_banned' not in users[uid]:
            users[uid]['is_banned'] = False

    return users

def get_daily_stats(chat_data):
    stats = defaultdict(int)
    for chat in chat_data:
        date_str = chat.get('timestamp', '')[:10] 
        if date_str:
            stats[date_str] += 1
    return dict(sorted(stats.items())[-7:]) 

# --- MASTER HTML LAYOUT ---
BASE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Phy_Chatbot | Admin Pro</title>
    
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link rel="stylesheet" href="https://cdn.datatables.net/1.13.4/css/dataTables.bootstrap5.min.css">
    
    <style>
        :root {
            --sidebar-w: 260px;
            --max-w: 1600px;
            --primary: #0ea5e9;
            --dark: #0f172a;
            --ui-bg: #f1f5f9;
            --ui-bg-soft: #eef2ff;
            --ui-surface: #ffffff;
            --ui-surface-2: #f8fafc;
            --ui-text: #1e293b;
            --ui-text-muted: #64748b;
            --ui-border: #e2e8f0;
            --ui-input-bg: #f8fafc;
            --ui-shadow: 0 4px 8px -2px rgba(15,23,42,0.08);
        }
        body {
            background: radial-gradient(circle at top right, #e2e8f0 0%, #f1f5f9 35%, #eef2ff 100%);
            font-family: 'Inter', sans-serif;
            color: #1e293b;
            font-size: 16px;
            overflow-x: hidden;
        }
        body, .card, .table, .form-control, .form-select, .input-group-text, .btn, .alert, .badge, .dataTables_wrapper .dataTables_filter input {
            transition: background-color 0.2s ease, color 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
        }
        
        {% if session.logged_in %}
        .sidebar { height: 100vh; background: linear-gradient(180deg, #0f172a, #0b1733 55%, #0a1330); color: white; padding-top: 22px; position: fixed; width: var(--sidebar-w); box-shadow: 4px 0 18px rgba(2,6,23,0.25); z-index: 1000; display: flex; flex-direction: column; }
        .sidebar-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 16px 16px; margin-bottom: 12px; }
        .sidebar h4 { text-align: left; margin: 0; font-weight: 800; color: #38bdf8; letter-spacing: 1px; font-size: 1.25rem;}
        .sidebar a { color: #94a3b8; text-decoration: none; padding: 18px 25px; display: block; transition: 0.25s ease; border-left: 4px solid transparent; font-weight: 500; font-size: 1.05rem; position: relative; overflow: hidden; }
        .sidebar a::after {
            content: "";
            position: absolute;
            inset: 0;
            background: linear-gradient(90deg, rgba(14,165,233,0.18), rgba(14,165,233,0));
            transform: translateX(-100%);
            transition: transform 0.25s ease;
        }
        .sidebar a:hover::after,
        .sidebar a.active::after { transform: translateX(0); }
        .sidebar a:hover { background-color: #1e293b; color: white; padding-left: 30px; }
        .sidebar a.active { background-color: #1e293b; border-left: 4px solid var(--primary); color: white; }
        .sidebar a i { margin-right: 15px; width: 22px; text-align: center; font-size: 1.1rem; }
        .logout-box { margin-top: auto; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 10px; margin-bottom: 20px; }
        .main-content { margin-left: var(--sidebar-w); min-height: 100vh; }
        body.sidebar-collapsed .sidebar { width: 84px; }
        body.sidebar-collapsed .main-content { margin-left: 84px; }
        body.sidebar-collapsed .sidebar h4 span,
        body.sidebar-collapsed .sidebar a span { display: none; }
        body.sidebar-collapsed .sidebar a { text-align: center; padding-left: 0; padding-right: 0; }
        body.sidebar-collapsed .sidebar a i { margin-right: 0; }
        body.sidebar-collapsed .sidebar-header { justify-content: center; }
        body.sidebar-collapsed .sidebar-header h4 { display: none; }
        body.sidebar-collapsed .sidebar .nav-link,
        body.sidebar-collapsed .sidebar .logout-box { display: none !important; }
        {% else %}
        .main-content { min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        {% endif %}
        
        .content-container { width: 100%; max-width: var(--max-w); margin: 0 auto; padding: 40px 50px; color: var(--ui-text); }
        .card { border: none; border-radius: 16px; box-shadow: var(--ui-shadow); background: var(--ui-surface); transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease; border: 1px solid color-mix(in srgb, var(--ui-border) 75%, transparent); }
        .card:hover { transform: translateY(-3px); box-shadow: 0 14px 24px rgba(2, 6, 23, 0.10); border-color: rgba(14,165,233,0.22); }
        .text-dark { color: var(--ui-text) !important; }
        .text-muted { color: var(--ui-text-muted) !important; }
        .bg-light { background-color: var(--ui-surface-2) !important; color: var(--ui-text) !important; }
        .form-control,
        .form-select,
        .input-group-text,
        textarea {
            background-color: var(--ui-input-bg) !important;
            color: var(--ui-text) !important;
            border: 1px solid var(--ui-border) !important;
        }
        .form-control::placeholder,
        textarea::placeholder { color: var(--ui-text-muted) !important; opacity: 0.9; }
        .form-control:focus,
        .form-select:focus,
        textarea:focus {
            border-color: #60a5fa !important;
            box-shadow: 0 0 0 0.2rem rgba(96,165,250,0.2) !important;
        }
        .table thead th {
            background-color: var(--ui-surface-2);
            border-bottom: 2px solid var(--ui-border);
            color: color-mix(in srgb, var(--ui-text) 85%, #94a3b8);
            font-weight: 700;
            text-transform: uppercase;
            font-size: 0.9rem;
            letter-spacing: 0.05em;
            padding: 18px;
        }
        .table tbody td { padding: 18px; vertical-align: middle; font-size: 1rem; color: var(--ui-text); border-color: var(--ui-border); }
        .table-hover > tbody > tr:hover > * {
            background-color: color-mix(in srgb, var(--ui-surface-2) 70%, #0ea5e9 8%);
            color: var(--ui-text);
        }
        
        .stat-link { text-decoration: none; display: block; }
        .stat-link .stat-card { transition: all 0.3s ease; }
        .stat-link:hover .stat-card { transform: translateY(-6px); box-shadow: 0 15px 25px rgba(0,0,0,0.15) !important; }
        
        .stat-card { color: white; padding: 1.5rem; position: relative; overflow: hidden; border-radius: 16px; isolation: isolate; }
        .stat-card h5 { font-size: 1rem; font-weight: 600; text-transform: uppercase; opacity: 0.9; margin-bottom: 0.5rem; position: relative; z-index: 2; letter-spacing: 0.5px; }
        .stat-card h1 { font-size: 2.8rem; font-weight: 800; margin: 0; position: relative; z-index: 2; }
        .stat-card i.bg-icon { position: absolute; right: -10px; bottom: -20px; font-size: 6rem; opacity: 0.15; transform: rotate(-15deg); z-index: 1; }
        .stat-card::before {
            content: "";
            position: absolute;
            width: 180px;
            height: 180px;
            border-radius: 999px;
            top: -95px;
            right: -95px;
            background: rgba(255,255,255,0.15);
            z-index: 0;
            transition: transform 0.3s ease;
        }
        .stat-link:hover .stat-card::before { transform: scale(1.12); }
        
        .bg-grad-primary { background: linear-gradient(135deg, #0ea5e9, #2563eb); }
        .bg-grad-success { background: linear-gradient(135deg, #10b981, #059669); }
        .bg-grad-warning { background: linear-gradient(135deg, #f59e0b, #d97706); }
        .bg-grad-danger { background: linear-gradient(135deg, #ef4444, #b91c1c); }
        
        h2.fw-bold { font-size: 2.2rem; }
        .btn { border-radius: 10px; font-weight: 600; font-size: 1rem; transition: 0.2s; padding: 0.6rem 1.2rem; position: relative; }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 8px 14px rgba(15,23,42,0.12); }
        .btn:active { transform: translateY(0) scale(0.98); box-shadow: 0 2px 5px rgba(15,23,42,0.18); }
        .btn:disabled { opacity: 0.75; cursor: not-allowed; transform: none !important; }
        .btn.loading { color: transparent !important; pointer-events: none; }
        .btn.loading::after {
            content: "";
            position: absolute;
            top: 50%;
            left: 50%;
            width: 18px;
            height: 18px;
            margin: -9px 0 0 -9px;
            border: 2px solid rgba(255,255,255,0.6);
            border-top-color: transparent;
            border-radius: 50%;
            animation: btnSpin 0.7s linear infinite;
        }
        @keyframes btnSpin { to { transform: rotate(360deg); } }
        
        /* Form Switch Customization */
        .form-check-input { width: 3em; height: 1.5em; cursor: pointer; }
        .form-check-input:checked { background-color: #10b981; border-color: #10b981; }
        
        .pulse { display: inline-block; width: 12px; height: 12px; background: #22c55e; border-radius: 50%; box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.7); animation: pulsing 2s infinite; margin-right: 8px; }
        @keyframes pulsing { 0% { box-shadow: 0 0 0 0 rgba(34,197,94,0.7); } 70% { box-shadow: 0 0 0 10px rgba(34,197,94,0); } 100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); } }
        .sidebar-toggle-btn {
            width: 36px;
            height: 36px;
            border-radius: 10px;
            border: 1px solid rgba(148, 163, 184, 0.35);
            background: rgba(30, 41, 59, 0.8);
            color: #cbd5e1;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: all .2s ease;
        }
        .sidebar-toggle-btn:hover { color: #fff; border-color: rgba(14,165,233,0.8); box-shadow: 0 8px 14px rgba(2,6,23,0.25); }
        .logout-btn {
            color: #fca5a5 !important;
            margin: 0 14px;
            border-radius: 12px;
            border: 1px solid rgba(239, 68, 68, 0.35);
            background: linear-gradient(180deg, rgba(127, 29, 29, 0.25), rgba(69, 10, 10, 0.2));
            font-weight: 700 !important;
            transition: all 0.2s ease;
            padding: 12px 14px !important;
        }
        .logout-btn i { color: #f87171; }
        .logout-btn:hover {
            color: #fff !important;
            border-color: rgba(239, 68, 68, 0.8);
            background: linear-gradient(180deg, rgba(239, 68, 68, 0.4), rgba(153, 27, 27, 0.4));
            transform: translateY(-2px);
            box-shadow: 0 10px 16px rgba(127, 29, 29, 0.3);
            padding-left: 14px !important;
        }
        .badge { border: 1px solid color-mix(in srgb, var(--ui-border) 75%, transparent); }
        .dataTables_wrapper .dataTables_length,
        .dataTables_wrapper .dataTables_filter,
        .dataTables_wrapper .dataTables_info,
        .dataTables_wrapper .dataTables_paginate { color: var(--ui-text-muted) !important; }
        .dataTables_wrapper .dataTables_filter input,
        .dataTables_wrapper .dataTables_length select {
            background-color: var(--ui-input-bg) !important;
            color: var(--ui-text) !important;
            border: 1px solid var(--ui-border) !important;
            border-radius: 8px;
        }
        .page-link {
            background-color: var(--ui-surface);
            border-color: var(--ui-border);
            color: var(--ui-text);
        }
        .page-link:hover { background-color: var(--ui-surface-2); color: var(--ui-text); }
        .page-item.active .page-link {
            background-color: #2563eb;
            border-color: #2563eb;
            color: #fff;
        }
        .alert { border: 1px solid var(--ui-border) !important; }
        .login-pass-toggle {
            width: 52px;
            min-width: 52px;
            border: 0;
            background: #f8fafc;
            color: #475569;
            border-radius: 0 12px 12px 0;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: background 0.2s ease, color 0.2s ease;
        }
        .login-pass-toggle:hover { background: #e2e8f0; color: #0f172a; }
        .login-pass-input {
            border-radius: 0 !important;
        }
        input[type="password"]::-ms-reveal,
        input[type="password"]::-ms-clear {
            display: none;
        }
        .login-shell {
            width: 100%;
            max-width: 520px;
            margin: 18px auto 0;
        }
        .login-card {
            border-radius: 22px;
            overflow: hidden;
            border: 1px solid var(--ui-border);
            background: var(--ui-surface);
            box-shadow: 0 22px 40px rgba(2, 6, 23, 0.16);
        }
        .login-accent {
            height: 8px;
            background: linear-gradient(90deg, #0ea5e9, #2563eb, #14b8a6);
        }
        .login-card-body { padding: 40px; }
        .login-icon {
            width: 64px;
            height: 64px;
            border-radius: 999px;
            background: color-mix(in srgb, #0ea5e9 16%, var(--ui-surface-2));
            color: #0284c7;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }
        .login-title { color: #2563eb; font-weight: 800; margin-bottom: 2px; }
        .login-subtitle { color: var(--ui-text-muted); margin: 0; }
        .login-label {
            font-size: 0.82rem;
            letter-spacing: .04em;
            color: var(--ui-text-muted);
            font-weight: 700;
            text-transform: uppercase;
            margin-bottom: 8px;
            display: block;
        }
        .login-input-group {
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid var(--ui-border);
            background: var(--ui-input-bg);
        }
        .login-input-group .input-group-text,
        .login-input-group .form-control { border: 0 !important; background: transparent !important; }
        .login-input-group .form-control { color: var(--ui-text) !important; }
        .login-input-group:focus-within {
            border-color: #60a5fa;
            box-shadow: 0 0 0 0.2rem rgba(96,165,250,0.2);
        }
        .login-submit-btn { border-radius: 12px; font-size: 1.1rem; }
        input:-webkit-autofill,
        input:-webkit-autofill:hover,
        input:-webkit-autofill:focus,
        textarea:-webkit-autofill,
        select:-webkit-autofill {
            -webkit-text-fill-color: var(--ui-text) !important;
            box-shadow: 0 0 0px 1000px var(--ui-input-bg) inset !important;
            transition: background-color 9999s ease-in-out 0s;
        }
        
        /* Custom scrollbar for chat history */
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #f1f5f9; border-radius: 4px; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
    </style>
</head>
<body>
    <script>
        (function() {
            try {
                var sidebarState = localStorage.getItem('phy_admin_sidebar') || 'expanded';
                if (sidebarState === 'collapsed') document.body.classList.add('sidebar-collapsed');
            } catch (e) {}
        })();
    </script>
    {% if session.logged_in %}
    <div class="sidebar">
        <div class="sidebar-header">
            <h4><i class="fa-solid fa-atom"></i> <span>Phy_Admin</span></h4>
            <button id="sidebarToggleBtn" class="sidebar-toggle-btn" type="button" title="Toggle sidebar">
                <i class="fa-solid fa-bars"></i>
            </button>
        </div>
        <a href="/" class="nav-link"><i class="fa-solid fa-chart-pie"></i> <span>Dashboard</span></a>
        <a href="/settings" class="nav-link"><i class="fa-solid fa-sliders"></i> <span>Bot Control</span></a>
        <a href="/users" class="nav-link"><i class="fa-solid fa-users-gear"></i> <span>User Management</span></a>
        <a href="/upgrade_reports" class="nav-link"><i class="fa-solid fa-file-invoice-dollar"></i> <span>Upgrade Reports</span></a>
        <a href="/training" class="nav-link"><i class="fa-solid fa-book"></i> <span>Training Data</span></a>
        <a href="/history" class="nav-link"><i class="fa-solid fa-comments"></i> <span>Chat History</span></a>
        <a href="/feedback" class="nav-link"><i class="fa-solid fa-comment-dots"></i> <span>Feedback</span></a>
        <a href="/broadcast" class="nav-link"><i class="fa-solid fa-bullhorn"></i> <span>Broadcast</span></a>
        
        <div class="logout-box">
            <a href="/logout" class="logout-btn"><i class="fa-solid fa-right-from-bracket"></i> <span>Logout</span></a>
        </div>
    </div>
    {% endif %}

    <div class="main-content">
        <div class="content-container">
            <div id="alert-container">
                {% with messages = get_flashed_messages(with_categories=true) %}
                  {% if messages %}
                    {% for category, message in messages %}
                      <div class="alert alert-{{ category }} alert-dismissible fade show shadow-sm border-0 fs-5" role="alert">
                        <i class="fa-solid fa-circle-info me-2"></i>{{ message }}
                        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                      </div>
                    {% endfor %}
                  {% endif %}
                {% endwith %}
            </div>
            {{ page_content|safe }}
        </div>
    </div>

    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.4/js/jquery.dataTables.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.4/js/dataTables.bootstrap5.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.24.1/ace.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    
    <script>
        $(document).ready(function() {
            var currentPath = window.location.pathname;
            $('.sidebar a').each(function() {
                if ($(this).attr('href') === currentPath) {
                    $(this).addClass('active');
                }
            });
            setTimeout(function() { $('.alert').alert('close'); }, 5000);

            $('#sidebarToggleBtn').on('click', function() {
                document.body.classList.toggle('sidebar-collapsed');
                const collapsed = document.body.classList.contains('sidebar-collapsed');
                localStorage.setItem('phy_admin_sidebar', collapsed ? 'collapsed' : 'expanded');
            });
        });

        function setButtonLoading(formElement) {
            if (!formElement) return;
            const btn = formElement.querySelector('button[type="submit"], .btn[type="button"]');
            if (btn) {
                btn.classList.add('loading');
                btn.disabled = true;
            }
        }

        function confirmUpdate(event, formElement, titleText, confirmText, isDanger) {
            event.preventDefault(); 
            Swal.fire({
                title: 'Are you sure?',
                text: titleText,
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: isDanger ? '#dc3545' : '#198754', 
                cancelButtonColor: '#6c757d',
                confirmButtonText: confirmText
            }).then((result) => {
                if (result.isConfirmed) {
                    setButtonLoading(formElement);
                    formElement.submit();
                }
            });
        }

        // Global submit feedback for better UX responsiveness
        $(document).on('submit', 'form', function() {
            const submitBtn = this.querySelector('button[type="submit"]');
            if (submitBtn && !submitBtn.classList.contains('no-loading')) {
                submitBtn.classList.add('loading');
                submitBtn.disabled = true;
                setTimeout(() => {
                    submitBtn.classList.remove('loading');
                    submitBtn.disabled = false;
                }, 4000);
            }
        });
    </script>
    {{ extra_scripts|safe }}
</body>
</html>
"""

# --- ROUTES ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'): return redirect(url_for('dashboard'))

    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        now = time.time()
        rec = _login_fail_tracker.get(ip, [0, 0.0])
        if rec[1] > now:
            wait = int(rec[1] - now)
            flash(f"Too many failed attempts. Try again in {wait} seconds.", "danger")
        else:
            username = request.form.get('username', '')
            password = request.form.get('password', '')
            user_ok = hmac.compare_digest(_digest256(username), _digest256(ADMIN_USERNAME))
            pass_ok = hmac.compare_digest(_digest256(password), _digest256(ADMIN_PASSWORD))
            if user_ok and pass_ok:
                _login_fail_tracker.pop(ip, None)
                session['logged_in'] = True
                session.permanent = True
                flash("Welcome back, Admin!", "success")
                return redirect(url_for('dashboard'))
            rec[0] += 1
            if rec[0] >= 5:
                rec[0] = 0
                rec[1] = now + 300
                _login_fail_tracker[ip] = rec
                flash("Too many failed attempts. Locked out for 5 minutes.", "danger")
            else:
                _login_fail_tracker[ip] = rec
                flash("Invalid username or password.", "danger")

    content = """
    <div class="login-shell">
        <div class="login-card">
            <div class="login-accent"></div>
            <div class="login-card-body">
                <div class="text-center mb-4">
                    <div class="login-icon mb-3">
                        <i class="fa-solid fa-shield-halved fs-3"></i>
                    </div>
                    <h2 class="login-title">Admin Sign In</h2>
                    <p class="login-subtitle">Secure access to manage Phy_Chatbot system</p>
                </div>
                <form method="POST" autocomplete="on">
                    <div class="mb-4">
                        <label class="login-label">Username</label>
                        <div class="input-group input-group-lg login-input-group">
                            <span class="input-group-text"><i class="fa-regular fa-user"></i></span>
                            <input type="text" name="username" class="form-control py-3" placeholder="Enter admin username" required>
                        </div>
                    </div>
                    <div class="mb-4">
                        <label class="login-label">Password</label>
                        <div class="input-group input-group-lg login-input-group">
                            <span class="input-group-text"><i class="fa-solid fa-lock"></i></span>
                            <input id="loginPassInput" type="password" name="password" class="form-control py-3 login-pass-input" placeholder="Enter secure password" required>
                            <button id="toggleLoginPassBtn" class="login-pass-toggle no-loading" type="button" aria-label="Toggle password visibility">
                                <i class="fa-regular fa-eye"></i>
                            </button>
                        </div>
                    </div>
                    <button type="submit" class="btn btn-primary w-100 py-3 fw-bold login-submit-btn">Login to Dashboard</button>
                </form>
            </div>
        </div>
    </div>
    <script>
        document.addEventListener('DOMContentLoaded', function() {
            document.body.classList.add('login-page');
            const passInput = document.getElementById('loginPassInput');
            const toggleBtn = document.getElementById('toggleLoginPassBtn');
            if (!passInput || !toggleBtn) return;
            toggleBtn.addEventListener('click', function() {
                const showing = passInput.type === 'text';
                passInput.type = showing ? 'password' : 'text';
                toggleBtn.innerHTML = showing ? '<i class="fa-regular fa-eye"></i>' : '<i class="fa-regular fa-eye-slash"></i>';
            });
        });
    </script>
    """
    return render_template_string(BASE_HTML, page_content=content, extra_scripts="")

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    chat_data = get_chat_data()
    feedback_data = get_feedback_data()
    user_stats = get_user_stats(chat_data)
    daily_stats = get_daily_stats(chat_data)
    bot_settings = get_bot_settings()
    
    status_html = '<span class="fw-bold text-success"><span class="pulse"></span> Bot Online</span>' if bot_settings.get('status') == 'online' else '<span class="fw-bold text-danger"><i class="fa-solid fa-triangle-exclamation"></i> Maintenance Mode</span>'
    
    total_users = len(user_stats)
    premium_count = sum(1 for u in user_stats.values() if u['plan'] == 'Premium')
    free_count = sum(1 for u in user_stats.values() if u['plan'] == 'Free Plan' or u['plan'] == 'Free')
    banned_count = sum(1 for u in user_stats.values() if u.get('is_banned', False))
    revenue_summary = user_db.get_upgrade_summary()
    total_revenue = revenue_summary.get("total_revenue", 0.0)
    
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    msgs_today = daily_stats.get(today_str, 0)
    total_feedback = len(feedback_data)
    feedback_with_images = sum(1 for fb in feedback_data if fb.get('image_file'))
    
    recent_chats = chat_data[::-1][:5]
    feed_html = ""
    if not recent_chats:
        feed_html = "<p class='text-muted text-center py-4 m-0'>No recent messages yet.</p>"
    for c in recent_chats:
        user_input_escaped = html.escape(c.get('user_input', ''))
        if c.get('image_file') or '[IMAGE]' in user_input_escaped or '[FILE IMAGE]' in user_input_escaped:
            user_input_escaped = f"📷 <i>Image/File attachment</i> <br>{user_input_escaped}"
            
        feed_html += f'''
        <div class="d-flex align-items-center border-bottom py-3">
            <div class="bg-primary bg-opacity-10 text-primary rounded-circle d-flex align-items-center justify-content-center me-3 fw-bold" style="width: 45px; height: 45px; min-width: 45px;">
                {c.get('username', 'U')[0].upper()}
            </div>
            <div class="flex-grow-1 overflow-hidden pe-3">
                <div class="fw-bold text-dark">@{html.escape(c.get('username', 'unknown'))}</div>
                <div class="text-muted small text-truncate">{user_input_escaped}</div>
            </div>
            <div class="text-muted small" style="white-space: nowrap;">
                <i class="fa-regular fa-clock me-1"></i> {c.get('timestamp', '')[11:16]}
            </div>
        </div>
        '''

    recent_feedback = feedback_data[::-1][:5]
    feedback_html = ""
    if not recent_feedback:
        feedback_html = "<p class='text-muted text-center py-4 m-0'>No feedback yet.</p>"
    for f in recent_feedback:
        username = html.escape(f.get('username', 'unknown'))
        full_name = html.escape(f.get('full_name', 'Unknown'))
        feedback_text = html.escape(f.get('feedback_text', ''))
        if len(feedback_text) > 220:
            feedback_text = feedback_text[:220] + "..."
        timestamp = f.get('timestamp', '')
        image_raw = f.get('image_file')
        image_html = ""
        if image_raw:
            img_safe = secure_filename(os.path.basename(str(image_raw)))
            img_esc = html.escape(img_safe, quote=True) if img_safe else ''
            if img_esc:
                image_html = f'<div class="mt-2"><img src="/uploads/{img_esc}" class="rounded-3 border" style="max-height: 120px; cursor: pointer;" onclick="window.open(this.src)"></div>'

        feedback_html += f'''
        <div class="border-bottom py-3">
            <div class="d-flex justify-content-between align-items-start gap-3">
                <div class="overflow-hidden">
                    <div class="fw-bold text-dark">{full_name} <span class="text-muted small">@{username}</span></div>
                    <div class="text-muted small mb-2"><i class="fa-regular fa-clock me-1"></i>{timestamp}</div>
                    <div class="text-dark" style="white-space: pre-wrap;">{feedback_text}</div>
                    {image_html}
                </div>
            </div>
        </div>
        '''

    content = f"""
    <div class="d-flex justify-content-between align-items-center mb-4">
        <div>
            <h2 class="fw-bold m-0 text-dark">System Overview</h2>
            <p class="text-muted m-0 fs-5 mt-1">Real-time Analytics & Live Feed</p>
        </div>
        <div class="bg-white px-4 py-2 rounded-pill shadow-sm border fs-5">{status_html}</div>
    </div>
    
    <div class="row mb-4 g-4">
        <div class="col-lg-2 col-md-4">
            <a href="/users" class="stat-link text-decoration-none">
                <div class="stat-card bg-grad-primary shadow-sm border-0">
                    <h5 class="mb-1">Total Users</h5><h1 class="mb-0">{total_users}</h1><i class="fa-solid fa-users bg-icon"></i>
                </div>
            </a>
        </div>
        <div class="col-lg-2 col-md-4">
            <a href="/users" class="stat-link text-decoration-none">
                <div class="stat-card bg-grad-success shadow-sm border-0">
                    <h5 class="mb-1">Premium Users</h5><h1 class="mb-0">{premium_count}</h1><i class="fa-solid fa-crown bg-icon"></i>
                </div>
            </a>
        </div>
        <div class="col-lg-2 col-md-4">
            <a href="/history" class="stat-link text-decoration-none">
                <div class="stat-card bg-grad-warning shadow-sm border-0">
                    <h5 class="mb-1">Messages Today</h5><h1 class="mb-0">{msgs_today}</h1><i class="fa-solid fa-message bg-icon"></i>
                </div>
            </a>
        </div>
        <div class="col-lg-2 col-md-4">
            <a href="/users" class="stat-link text-decoration-none">
                <div class="stat-card bg-grad-danger shadow-sm border-0">
                    <h5 class="mb-1">Banned Users</h5><h1 class="mb-0">{banned_count}</h1><i class="fa-solid fa-ban bg-icon"></i>
                </div>
            </a>
        </div>
        <div class="col-lg-2 col-md-4">
            <a href="/upgrade_reports" class="stat-link text-decoration-none">
                <div class="stat-card bg-grad-success shadow-sm border-0" style="background: linear-gradient(135deg, #16a34a, #22c55e);">
                    <h5 class="mb-1">Total Revenue</h5><h1 class="mb-0">${total_revenue:.2f}</h1><i class="fa-solid fa-dollar-sign bg-icon"></i>
                </div>
            </a>
        </div>
        <div class="col-lg-2 col-md-4">
            <a href="/feedback" class="stat-link text-decoration-none">
                <div class="stat-card bg-grad-primary shadow-sm border-0">
                    <h5 class="mb-1">User Feedback</h5><h1 class="mb-0">{total_feedback}</h1><i class="fa-solid fa-comment-dots bg-icon"></i>
                </div>
            </a>
        </div>
    </div>
    
    <div class="row g-4 mb-4">
        <div class="col-md-8">
            <div class="card p-4 shadow-sm border-0 h-100 rounded-4">
                <h5 class="fw-bold text-secondary mb-4"><i class="fa-solid fa-chart-line me-2 text-primary"></i>Interaction Activity (Last 7 Days)</h5>
                <div style="position: relative; height: 300px; width: 100%;">
                    <canvas id="activityChart"></canvas>
                </div>
            </div>
        </div>
        <div class="col-md-4">
            <div class="card p-4 shadow-sm border-0 h-100 rounded-4">
                <h5 class="fw-bold text-secondary mb-4"><i class="fa-solid fa-chart-pie me-2 text-success"></i>Subscription Distribution</h5>
                <div style="position: relative; height: 300px; width: 100%;"><canvas id="pieChart"></canvas></div>
            </div>
        </div>
    </div>
    
    <div class="card p-4 shadow-sm border-0 rounded-4 mb-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h5 class="fw-bold text-dark m-0"><i class="fa-solid fa-bolt text-warning me-2"></i>Live Feed (Latest Messages)</h5>
            <a href="/history" class="btn btn-sm btn-outline-primary fw-bold">View All History</a>
        </div>
        <div>
            {feed_html}
        </div>
    </div>

    <div class="card p-4 shadow-sm border-0 rounded-4 mb-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h5 class="fw-bold text-dark m-0"><i class="fa-solid fa-comment-dots text-primary me-2"></i>Latest User Feedback</h5>
            <span class="badge bg-light text-dark border">With Images: {feedback_with_images}</span>
        </div>
        <div>
            {feedback_html}
        </div>
    </div>
    """
    
    scripts = f"""
    <script>
        Chart.defaults.font.size = 14;
        Chart.defaults.font.family = 'Inter';
        
        new Chart(document.getElementById('activityChart').getContext('2d'), {{
            type: 'line',
            data: {{
                labels: {json.dumps(list(daily_stats.keys()))},
                datasets: [{{ 
                    label: 'Messages', 
                    data: {json.dumps(list(daily_stats.values()))}, 
                    borderColor: '#0ea5e9', 
                    backgroundColor: 'rgba(14, 165, 233, 0.1)', 
                    borderWidth: 3,
                    fill: true,
                    tension: 0.4
                }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true }} }} }}
        }});
        
        new Chart(document.getElementById('pieChart').getContext('2d'), {{
            type: 'doughnut',
            data: {{
                labels: ['Free Plan', 'Premium'],
                datasets: [{{ data: [{free_count}, {premium_count}], backgroundColor: ['#cbd5e1', '#10b981'], borderWidth: 0 }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, cutout: '65%', plugins: {{ legend: {{ position: 'bottom', labels: {{ padding: 20 }} }} }} }}
        }});
    </script>
    """
    return render_template_string(BASE_HTML, page_content=content, extra_scripts=scripts)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def bot_settings():
    if request.method == 'POST':
        current = get_bot_settings()
        current_pricing = _get_upgrade_pricing(current)
        def _read_price(name, default_val):
            try:
                return max(0.0, float(request.form.get(name, default_val)))
            except Exception:
                return float(default_val)
        def _read_days(name, default_val):
            try:
                return max(1, int(request.form.get(name, default_val)))
            except Exception:
                return int(default_val)

        data = {
            "status": request.form.get('status'),
            "free_limit": int(request.form.get('free_limit', 10)),
            "premium_limit": int(request.form.get('premium_limit', 40)),
            "temperature": float(request.form.get('temperature', 0.2)),
            "cooldown_seconds": int(request.form.get('cooldown_seconds', 2)),
            "max_input_length": int(request.form.get('max_input_length', 1000)),
            "model_name": request.form.get('model_name', 'gemini-3.1-flash-lite-preview').strip(),
            "api_retry_attempts": int(request.form.get('api_retry_attempts', 3)),
            "api_retry_base_delay": int(request.form.get('api_retry_base_delay', 4)),
            "system_prompt": request.form.get('system_prompt', ''),
            "welcome_message": request.form.get('welcome_message', ''),
            "maintenance_message": request.form.get('maintenance_message', ''),
            "support_contact": request.form.get('support_contact', '').replace('@', ''),
            "upgrade_pricing": {
                "monthly": {
                    "label": "Monthly",
                    "days": _read_days("upgrade_monthly_days", current_pricing["monthly"]["days"]),
                    "price": _read_price("upgrade_monthly_price", current_pricing["monthly"]["price"]),
                },
                "6month": {
                    "label": "6 Months",
                    "days": _read_days("upgrade_6month_days", current_pricing["6month"]["days"]),
                    "price": _read_price("upgrade_6month_price", current_pricing["6month"]["price"]),
                },
                "yearly": {
                    "label": "Yearly",
                    "days": _read_days("upgrade_yearly_days", current_pricing["yearly"]["days"]),
                    "price": _read_price("upgrade_yearly_price", current_pricing["yearly"]["price"]),
                },
            },
            "features": {
                "free": {
                    "image_analysis": 'free_image_analysis' in request.form,
                    "step_by_step": 'free_step_by_step' in request.form,
                    "pdf_reading": 'free_pdf_reading' in request.form,
                    "generate_quizzes": 'free_generate_quizzes' in request.form
                },
                "premium": {
                    "image_analysis": 'prem_image_analysis' in request.form,
                    "step_by_step": 'prem_step_by_step' in request.form,
                    "pdf_reading": 'prem_pdf_reading' in request.form,
                    "generate_quizzes": 'prem_generate_quizzes' in request.form
                }
            }
        }
        save_bot_settings(data)
        flash("Bot settings and Feature Access rules have been updated!", "success")
        return redirect(url_for('bot_settings'))
        
    settings = get_bot_settings()
    pricing = _get_upgrade_pricing(settings)
    
    features_config = settings.get('features', {
        "free": {"image_analysis": False, "step_by_step": True, "pdf_reading": False, "generate_quizzes": False},
        "premium": {"image_analysis": True, "step_by_step": True, "pdf_reading": True, "generate_quizzes": True}
    })
    free_feats = features_config.get('free', {})
    prem_feats = features_config.get('premium', {})
    
    feature_list = [
        ("image_analysis", "fa-image", "Image Analysis (Vision)"),
        ("step_by_step", "fa-list-ol", "Step-by-Step Solving"),
        ("pdf_reading", "fa-file-pdf", "PDF/Document Reading"),
        ("generate_quizzes", "fa-clipboard-question", "Custom Quizzes")
    ]
    
    free_html = ""
    prem_html = ""
    
    for key, icon, label in feature_list:
        is_free = 'checked' if free_feats.get(key, False) else ''
        free_html += f'''
        <div class="d-flex justify-content-between align-items-center mb-3 pb-3 border-bottom border-light">
            <span class="fw-bold fs-6 text-secondary"><i class="fa-solid {icon} me-2"></i> {label}</span>
            <div class="form-check form-switch fs-4 m-0">
                <input class="form-check-input" type="checkbox" name="free_{key}" {is_free}>
            </div>
        </div>
        '''
        
        is_prem = 'checked' if prem_feats.get(key, False) else ''
        prem_html += f'''
        <div class="d-flex justify-content-between align-items-center mb-3 pb-3 border-bottom border-success border-opacity-25">
            <span class="fw-bold fs-6 text-dark"><i class="fa-solid {icon} me-2 text-success"></i> {label}</span>
            <div class="form-check form-switch fs-4 m-0">
                <input class="form-check-input" type="checkbox" name="prem_{key}" {is_prem}>
            </div>
        </div>
        '''

    content = f"""
    <div class="mb-4">
        <h2 class="fw-bold m-0">⚙️ Bot Control Panel</h2>
        <p class="text-muted m-0 fs-5 mt-2">Adjust live settings, AI behavior, and plan limits without restarting your bot.</p>
    </div>
    
    <form method="POST">
        <div class="row g-4 mb-4">
            <div class="col-md-6">
                <div class="card p-4 border-0 shadow-sm rounded-4 h-100">
                    <h5 class="fw-bold mb-4 text-dark border-bottom pb-3"><i class="fa-solid fa-server me-2 text-primary"></i> System & Limits</h5>
                    
                    <div class="mb-4">
                        <label class="fw-bold fs-6 mb-2 text-dark">Bot Operating Status</label>
                        <select name="status" class="form-select form-select-lg bg-light border-0">
                            <option value="online" {'selected' if settings.get('status') == 'online' else ''}>🟢 Online (Accepting Messages)</option>
                            <option value="maintenance" {'selected' if settings.get('status') == 'maintenance' else ''}>🔴 Maintenance Mode (Bot Paused)</option>
                        </select>
                    </div>

                    <div class="row">
                        <div class="col-6 mb-4">
                            <label class="fw-bold fs-6 mb-2 text-dark">Free User Limit</label>
                            <input type="number" name="free_limit" class="form-control form-control-lg bg-light border-0" value="{settings.get('free_limit', 10)}">
                        </div>
                        <div class="col-6 mb-4">
                            <label class="fw-bold fs-6 mb-2 text-dark">Premium Limit</label>
                            <input type="number" name="premium_limit" class="form-control form-control-lg bg-light text-success fw-bold border-0" value="{settings.get('premium_limit', 40)}">
                        </div>
                    </div>

                    <div class="row">
                        <div class="col-6 mb-0">
                            <label class="fw-bold fs-6 mb-2 text-dark">Spam Delay (s)</label>
                            <input type="number" step="1" min="0" name="cooldown_seconds" class="form-control form-control-lg bg-light border-0" value="{settings.get('cooldown_seconds', 2)}">
                        </div>
                        <div class="col-6 mb-0">
                            <label class="fw-bold fs-6 mb-2 text-dark">Max Input Length</label>
                            <input type="number" step="100" min="100" name="max_input_length" class="form-control form-control-lg bg-light border-0" value="{settings.get('max_input_length', 1000)}">
                        </div>
                    </div>
                </div>
            </div>

            <div class="col-md-6">
                <div class="card p-4 border-0 shadow-sm rounded-4 h-100">
                    <h5 class="fw-bold mb-4 text-dark border-bottom pb-3"><i class="fa-solid fa-robot me-2 text-primary"></i> AI & Responses</h5>

                    <div class="mb-4">
                        <label class="fw-bold fs-6 mb-2 text-dark d-flex justify-content-between">
                            <span>AI Temperature (Creativity)</span>
                            <span class="badge bg-primary rounded-pill" id="tempVal">{settings.get('temperature', 0.2)}</span>
                        </label>
                        <input type="range" class="form-range" name="temperature" min="0.0" max="1.0" step="0.1" value="{settings.get('temperature', 0.2)}" oninput="document.getElementById('tempVal').innerText=this.value">
                        <small class="text-muted d-block mt-1">0.0 = Strict/Factual | 1.0 = Highly Creative.</small>
                    </div>

                    <div class="mb-4">
                        <label class="fw-bold fs-6 mb-2 text-dark">System Prompt (AI Personality)</label>
                        <textarea name="system_prompt" class="form-control bg-light border-0" rows="2" placeholder="You are a helpful physics tutor...">{settings.get('system_prompt', '')}</textarea>
                    </div>

                    <div class="mb-4">
                        <label class="fw-bold fs-6 mb-2 text-dark">Welcome Message (/start)</label>
                        <textarea name="welcome_message" class="form-control bg-light border-0" rows="2">{settings.get('welcome_message', '')}</textarea>
                    </div>

                    <div class="mb-4">
                        <label class="fw-bold fs-6 mb-2 text-dark">Maintenance Message</label>
                        <textarea name="maintenance_message" class="form-control bg-light border-0" rows="2">{settings.get('maintenance_message', '')}</textarea>
                    </div>

                    <div class="mb-0">
                        <label class="fw-bold fs-6 mb-2 text-dark">Support Contact Handle</label>
                        <div class="input-group">
                            <span class="input-group-text bg-light border-0 text-muted fw-bold">@</span>
                            <input type="text" name="support_contact" class="form-control form-control-lg bg-light border-0" value="{settings.get('support_contact', 'admin')}">
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="card p-4 border-0 shadow-sm rounded-4 mb-4">
            <h5 class="fw-bold mb-4 text-dark border-bottom pb-3"><i class="fa-solid fa-microchip me-2 text-primary"></i> Advanced Runtime Controls</h5>
            <p class="text-muted mb-4 fs-6">Change model and retry behavior from web so admin can recover from API issues faster without editing code.</p>
            <div class="row g-4">
                <div class="col-md-6">
                    <label class="fw-bold fs-6 mb-2 text-dark">AI Model Name</label>
                    <input type="text" name="model_name" class="form-control form-control-lg bg-light border-0" value="{settings.get('model_name', 'gemini-3.1-flash-lite-preview')}" placeholder="example: gemini-3.1-flash-lite-preview">
                    <small class="text-muted">Used for chat + OCR model calls.</small>
                </div>
                <div class="col-md-3">
                    <label class="fw-bold fs-6 mb-2 text-dark">Retry Attempts</label>
                    <input type="number" min="1" max="6" name="api_retry_attempts" class="form-control form-control-lg bg-light border-0" value="{settings.get('api_retry_attempts', 3)}">
                    <small class="text-muted">On rate-limit / temporary API failure.</small>
                </div>
                <div class="col-md-3">
                    <label class="fw-bold fs-6 mb-2 text-dark">Retry Base Delay (s)</label>
                    <input type="number" min="1" max="15" name="api_retry_base_delay" class="form-control form-control-lg bg-light border-0" value="{settings.get('api_retry_base_delay', 4)}">
                    <small class="text-muted">Backoff delay multiplier.</small>
                </div>
            </div>
        </div>

        <div class="card p-4 border-0 shadow-sm rounded-4 mb-4">
            <h5 class="fw-bold mb-4 text-dark border-bottom pb-3"><i class="fa-solid fa-tags text-success me-2"></i> Premium Pricing Control</h5>
            <p class="text-muted mb-4 fs-5">Control upgrade duration and cost shown in the admin upgrade menu (Monthly, 6 Months, Yearly).</p>

            <div class="row g-4">
                <div class="col-md-4">
                    <div class="p-4 rounded-4" style="background-color: #ecfdf5; border: 1px solid #a7f3d0;">
                        <div class="fw-bold mb-3 text-dark"><i class="fa-solid fa-calendar-day me-2 text-success"></i>Monthly</div>
                        <label class="fw-bold fs-6 mb-2 text-dark">Days</label>
                        <input type="number" min="1" name="upgrade_monthly_days" class="form-control form-control-lg bg-light border-0 mb-3" value="{pricing['monthly']['days']}">
                        <label class="fw-bold fs-6 mb-2 text-dark">Price ($)</label>
                        <input type="number" step="0.01" min="0" name="upgrade_monthly_price" class="form-control form-control-lg bg-light border-0" value="{pricing['monthly']['price']}">
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="p-4 rounded-4" style="background-color: #f8fafc; border: 1px solid #e2e8f0;">
                        <div class="fw-bold mb-3 text-dark"><i class="fa-solid fa-calendar-week me-2 text-primary"></i>6 Months</div>
                        <label class="fw-bold fs-6 mb-2 text-dark">Days</label>
                        <input type="number" min="1" name="upgrade_6month_days" class="form-control form-control-lg bg-light border-0 mb-3" value="{pricing['6month']['days']}">
                        <label class="fw-bold fs-6 mb-2 text-dark">Price ($)</label>
                        <input type="number" step="0.01" min="0" name="upgrade_6month_price" class="form-control form-control-lg bg-light border-0" value="{pricing['6month']['price']}">
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="p-4 rounded-4" style="background-color: #fff7ed; border: 1px solid #fed7aa;">
                        <div class="fw-bold mb-3 text-dark"><i class="fa-solid fa-calendar me-2 text-warning"></i>Yearly</div>
                        <label class="fw-bold fs-6 mb-2 text-dark">Days</label>
                        <input type="number" min="1" name="upgrade_yearly_days" class="form-control form-control-lg bg-light border-0 mb-3" value="{pricing['yearly']['days']}">
                        <label class="fw-bold fs-6 mb-2 text-dark">Price ($)</label>
                        <input type="number" step="0.01" min="0" name="upgrade_yearly_price" class="form-control form-control-lg bg-light border-0" value="{pricing['yearly']['price']}">
                    </div>
                </div>
            </div>
        </div>

        <div class="card p-4 border-0 shadow-sm rounded-4 mb-4">
            <h5 class="fw-bold mb-4 text-dark border-bottom pb-3"><i class="fa-solid fa-toggle-on text-warning me-2"></i> Feature Access Control</h5>
            <p class="text-muted mb-4 fs-5">Easily restrict expensive API features (like Images or PDF parsing) to Premium users only.</p>
            
            <div class="row g-4">
                <div class="col-md-6">
                    <div class="p-4 rounded-4" style="background-color: #f8fafc; border: 1px solid #e2e8f0;">
                        <h5 class="fw-bold text-secondary mb-4 text-center"><i class="fa-solid fa-user me-2"></i> Free Plan Features</h5>
                        {free_html}
                    </div>
                </div>

                <div class="col-md-6">
                    <div class="p-4 rounded-4" style="background-color: #ecfdf5; border: 1px solid #a7f3d0;">
                        <h5 class="fw-bold text-success mb-4 text-center"><i class="fa-solid fa-crown me-2 text-warning"></i> Premium Features</h5>
                        {prem_html}
                    </div>
                </div>
            </div>
        </div>

        <div class="text-end mb-5">
            <button type="submit" class="btn btn-primary px-5 py-3 fw-bold fs-5 shadow-sm rounded-pill">
                <i class="fa-solid fa-cloud-arrow-up me-2"></i> Save & Apply Changes
            </button>
        </div>
    </form>
    """
    return render_template_string(BASE_HTML, page_content=content, extra_scripts="")

@app.route('/users')
@login_required
def users_list():
    user_stats = get_user_stats(get_chat_data())
    pricing = _get_upgrade_pricing(get_bot_settings())
    revenue_summary = user_db.get_upgrade_summary()
    
    total_users = len(user_stats)
    premium_users = sum(1 for u in user_stats.values() if u['plan'] == 'Premium')
    banned_users = sum(1 for u in user_stats.values() if u.get('is_banned', False))

    rows = ""
    for uid, data in user_stats.items():
        is_premium = data['plan'] == 'Premium'
        is_banned = data.get('is_banned', False)

        username_str = data['username'] if data['username'] and data['username'] != 'unknown' else '?'
        avatar_letter = username_str[0].upper()

        if is_premium:
            plan_badge = "<span class='badge bg-success rounded-pill px-3 py-2 shadow-sm w-100'><i class='fa-solid fa-crown text-warning me-1'></i> Premium</span>"
            expiry_str = data.get('expiry_date', '')
            expiry_html = f"<div class='small text-muted mt-2 fw-bold'><i class='fa-regular fa-clock me-1'></i>Exp: {expiry_str[:10]}</div>" if expiry_str and expiry_str != 'N/A' else ""
        else:
            plan_badge = "<span class='badge bg-secondary bg-opacity-25 text-secondary rounded-pill px-3 py-2 w-100'>Free Plan</span>"
            expiry_html = ""
            
        status_badge = "<span class='badge bg-danger rounded-pill px-3 py-2 shadow-sm'><i class='fa-solid fa-ban me-1'></i> Banned</span>" if is_banned else "<span class='badge bg-success bg-opacity-10 text-success rounded-pill px-3 py-2'><i class='fa-solid fa-check-circle me-1'></i> Active</span>"
        
        if is_premium:
            plan_action = f"""
                <form action="/update_plan" method="POST" class="m-0 mb-2">
                    <input type="hidden" name="user_id" value="{uid}">
                    <input type="hidden" name="username" value="{data['username']}">
                    <input type="hidden" name="new_plan" value="Free Plan">
                    <button type="button" class="btn btn-outline-warning btn-sm fw-bold w-100" onclick="confirmUpdate(event, this.form, 'Downgrade @{data['username']} to Free Plan?', 'Yes, Revoke', true)">
                        <i class="fa-solid fa-arrow-down me-1"></i> Revoke
                    </button>
                </form>
            """
        else:
            plan_action = f"""
                <div class="btn-group w-100 mb-2">
                    <button type="button" class="btn btn-success btn-sm fw-bold w-100 shadow-sm dropdown-toggle" data-bs-toggle="dropdown" aria-expanded="false">
                        <i class="fa-solid fa-arrow-up me-1"></i> Upgrade
                    </button>
                    <ul class="dropdown-menu dropdown-menu-end shadow-sm">
                        <li>
                            <form action="/update_plan" method="POST" class="m-0">
                                <input type="hidden" name="user_id" value="{uid}">
                                <input type="hidden" name="username" value="{data['username']}">
                                <input type="hidden" name="new_plan" value="Premium">
                                <input type="hidden" name="plan_key" value="monthly">
                                <button type="button" class="dropdown-item" onclick="confirmUpdate(event, this.form, 'Upgrade @{data['username']} to Monthly Premium (${pricing['monthly']['price']:.2f})?', 'Yes, Upgrade', false)">Monthly - ${pricing['monthly']['price']:.2f}</button>
                            </form>
                        </li>
                        <li>
                            <form action="/update_plan" method="POST" class="m-0">
                                <input type="hidden" name="user_id" value="{uid}">
                                <input type="hidden" name="username" value="{data['username']}">
                                <input type="hidden" name="new_plan" value="Premium">
                                <input type="hidden" name="plan_key" value="6month">
                                <button type="button" class="dropdown-item" onclick="confirmUpdate(event, this.form, 'Upgrade @{data['username']} to 6-Month Premium (${pricing['6month']['price']:.2f})?', 'Yes, Upgrade', false)">6 Months - ${pricing['6month']['price']:.2f}</button>
                            </form>
                        </li>
                        <li>
                            <form action="/update_plan" method="POST" class="m-0">
                                <input type="hidden" name="user_id" value="{uid}">
                                <input type="hidden" name="username" value="{data['username']}">
                                <input type="hidden" name="new_plan" value="Premium">
                                <input type="hidden" name="plan_key" value="yearly">
                                <button type="button" class="dropdown-item" onclick="confirmUpdate(event, this.form, 'Upgrade @{data['username']} to Yearly Premium (${pricing['yearly']['price']:.2f})?', 'Yes, Upgrade', false)">Yearly - ${pricing['yearly']['price']:.2f}</button>
                            </form>
                        </li>
                    </ul>
                </div>
            """

        if is_banned:
            ban_action = f"""
                <form action="/toggle_ban" method="POST" class="m-0">
                    <input type="hidden" name="user_id" value="{uid}">
                    <button type="button" class="btn btn-outline-secondary btn-sm fw-bold w-100" onclick="confirmUpdate(event, this.form, 'Unban @{data['username']}?', 'Yes, Unban', false)">
                        <i class="fa-solid fa-unlock me-1"></i> Unban
                    </button>
                </form>
            """
        else:
            ban_action = f"""
                <form action="/toggle_ban" method="POST" class="m-0">
                    <input type="hidden" name="user_id" value="{uid}">
                    <button type="button" class="btn btn-outline-danger btn-sm fw-bold w-100" onclick="confirmUpdate(event, this.form, 'Ban @{data['username']} from using the bot?', 'Yes, Ban User', true)">
                        <i class="fa-solid fa-ban me-1"></i> Ban
                    </button>
                </form>
            """

        rows += f"""
        <tr>
            <td class="align-middle py-3">
                <div class="d-flex align-items-center">
                    <div class="bg-primary bg-opacity-10 text-primary rounded-circle d-flex align-items-center justify-content-center me-3 fw-bold fs-5" style="width: 45px; height: 45px;">
                        {avatar_letter}
                    </div>
                    <div>
                        <div class="fw-bold text-dark fs-6">@{data['username']}</div>
                        <div class="text-muted font-monospace small mt-1" style="font-size: 0.8rem;">ID: {uid}</div>
                    </div>
                </div>
            </td>
            <td class="text-center align-middle">
                {plan_badge}
                {expiry_html}
            </td>
            <td class="text-center align-middle">{status_badge}</td>
            <td class="text-center align-middle">
                <div class="fw-bold text-primary fs-5">{data['message_count']}</div>
                <div class="text-muted small">msgs</div>
            </td>
            <td class="text-muted small align-middle">{data['last_seen'][:16]}</td>
            <td style="width: 140px;" class="align-middle">
                {plan_action}
                {ban_action}
            </td>
        </tr>
        """

    content = f"""
    <div class="d-flex justify-content-between align-items-center mb-4">
        <div>
            <h2 class="fw-bold m-0 text-dark">👥 User Management</h2>
            <p class="text-muted m-0 fs-5 mt-2">Monitor activity, manage premium subscriptions, and ban scammers.</p>
        </div>
        <div class="d-flex gap-2">
            <a href="/upgrade_reports" class="btn btn-warning px-4 py-2 fw-bold shadow-sm rounded-3">
                <i class="fa-solid fa-file-invoice-dollar me-2"></i> Upgrade Logs
            </a>
            <a href="/export_users" class="btn btn-success px-4 py-2 fw-bold shadow-sm rounded-3">
                <i class="fa-solid fa-file-csv me-2"></i> Export CSV
            </a>
        </div>
    </div>
    
    <div class="row mb-4 g-4">
        <div class="col-md-3">
            <div class="card p-4 shadow-sm border-0 bg-primary text-white rounded-4 h-100">
                <h5 class="mb-2 text-white-50"><i class="fa-solid fa-users me-2"></i>Total Users</h5>
                <h2 class="fw-bold m-0">{total_users}</h2>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card p-4 shadow-sm border-0 bg-success text-white rounded-4 h-100">
                <h5 class="mb-2 text-white-50"><i class="fa-solid fa-crown text-warning me-2"></i>Premium Users</h5>
                <h2 class="fw-bold m-0">{premium_users}</h2>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card p-4 shadow-sm border-0 bg-danger text-white rounded-4 h-100">
                <h5 class="mb-2 text-white-50"><i class="fa-solid fa-ban text-light me-2"></i>Banned Users</h5>
                <h2 class="fw-bold m-0">{banned_users}</h2>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card p-4 shadow-sm border-0 bg-warning text-dark rounded-4 h-100">
                <h5 class="mb-2 text-dark"><i class="fa-solid fa-dollar-sign me-2"></i>Total Revenue</h5>
                <h2 class="fw-bold m-0">${revenue_summary.get('total_revenue', 0):.2f}</h2>
            </div>
        </div>
    </div>
    
    <div class="card p-4 shadow-sm border-0 mb-4 bg-white rounded-4">
        <h5 class="fw-bold mb-3 text-dark"><i class="fa-solid fa-bolt text-warning me-2"></i> Quick Manual Override</h5>
        <form action="/update_plan" method="POST" class="row g-3 align-items-center">
            <div class="col-md-5">
                <input type="text" name="user_id" class="form-control bg-light border-0 py-2" placeholder="Paste Telegram ID here..." required>
                <input type="hidden" name="username" value="Manual_Upgrade">
            </div>
            <div class="col-md-4">
                <select name="plan_action" class="form-select bg-light border-0 py-2">
                    <option value="upgrade_monthly">🌟 Upgrade Monthly (${pricing['monthly']['price']:.2f})</option>
                    <option value="upgrade_6month">🌟 Upgrade 6 Months (${pricing['6month']['price']:.2f})</option>
                    <option value="upgrade_yearly">🌟 Upgrade Yearly (${pricing['yearly']['price']:.2f})</option>
                    <option value="downgrade">⏬ Downgrade to Free Plan</option>
                </select>
            </div>
            <div class="col-md-3">
                <button type="submit" class="btn btn-dark w-100 fw-bold shadow-sm py-2">Apply Change</button>
            </div>
        </form>
    </div>

    <div class="card p-4 shadow-sm border-0 rounded-4">
        <div class="table-responsive">
            <table id="usersTable" class="table table-hover align-middle mb-0">
                <thead class="bg-light">
                    <tr>
                        <th>User Identity</th>
                        <th class="text-center">Plan Status</th>
                        <th class="text-center">Account</th>
                        <th class="text-center">Activity</th>
                        <th>Last Seen</th>
                        <th class="text-center">Actions</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    """
    return render_template_string(BASE_HTML, page_content=content, extra_scripts="<script>$(document).ready(function(){ $('#usersTable').DataTable({'order': [[ 4, 'desc' ]]}); });</script>")

@app.route('/update_plan', methods=['POST'])
@login_required
def update_plan():
    uid = request.form.get('user_id').strip()
    plan = request.form.get('new_plan')
    username = request.form.get('username', 'Unknown')

    plan_action = request.form.get('plan_action', '').strip()
    plan_key = request.form.get('plan_key', '').strip()
    if plan_action:
        mapping = {
            "upgrade_monthly": ("Premium", "monthly"),
            "upgrade_6month": ("Premium", "6month"),
            "upgrade_yearly": ("Premium", "yearly"),
            "downgrade": ("Free Plan", ""),
        }
        mapped = mapping.get(plan_action, ("Free Plan", ""))
        plan = mapped[0]
        plan_key = mapped[1]

    if plan == "Premium":
        selected = _get_upgrade_pricing(get_bot_settings()).get(plan_key or "monthly", UPGRADE_OPTIONS["monthly"])
        user_db.set_user_plan(
            uid,
            username,
            plan,
            duration_days=selected["days"],
            upgrade_price=selected["price"],
            record_upgrade=True,
            plan_key=(plan_key or "monthly"),
            plan_label=selected.get("label", plan_key or "monthly"),
        )
        flash(
            f"Success! User {uid} upgraded to {selected['label']} Premium "
            f"({selected['days']} days) for ${selected['price']:.2f}.",
            "success"
        )
    else:
        user_db.set_user_plan(uid, username, "Free Plan")
        flash(f"User {uid} has been downgraded to the Free plan.", "warning")
        
    return redirect(url_for('users_list'))


@app.route('/upgrade_reports')
@login_required
def upgrade_reports():
    user_id = (request.args.get('user_id') or '').strip()
    records = user_db.get_upgrade_history(user_id=user_id or None, limit=500)
    summary = user_db.get_upgrade_summary(user_id=user_id or None)

    rows = ""
    for item in records:
        rows += f"""
        <tr>
            <td>{item.get('upgraded_at', '')}</td>
            <td class="font-monospace">{item.get('user_id', '')}</td>
            <td>@{item.get('username', 'Unknown')}</td>
            <td>{item.get('plan_label', '')}</td>
            <td>{item.get('duration_days', 0)} days</td>
            <td class="fw-bold text-success">${float(item.get('amount', 0)):.2f}</td>
        </tr>
        """

    if not rows:
        rows = "<tr><td colspan='6' class='text-center text-muted py-4'>No upgrade records found.</td></tr>"

    content = f"""
    <div class="d-flex justify-content-between align-items-center mb-4">
        <div>
            <h2 class="fw-bold m-0 text-dark"><i class="fa-solid fa-file-invoice-dollar text-warning me-2"></i>Upgrade Revenue Reports</h2>
            <p class="text-muted m-0 fs-5 mt-2">Query upgrade records and track total earnings from Premium sales.</p>
        </div>
    </div>

    <div class="row g-4 mb-4">
        <div class="col-md-6">
            <div class="card p-4 shadow-sm border-0 rounded-4 h-100">
                <h5 class="text-muted mb-2">Total Upgrades</h5>
                <h2 class="fw-bold m-0">{summary.get('total_upgrades', 0)}</h2>
            </div>
        </div>
        <div class="col-md-6">
            <div class="card p-4 shadow-sm border-0 rounded-4 h-100 bg-success text-white">
                <h5 class="text-white-50 mb-2">Total Revenue</h5>
                <h2 class="fw-bold m-0">${summary.get('total_revenue', 0):.2f}</h2>
            </div>
        </div>
    </div>

    <div class="card p-4 shadow-sm border-0 mb-4 rounded-4">
        <form method="GET" action="/upgrade_reports" class="row g-3 align-items-center">
            <div class="col-md-9">
                <input type="text" class="form-control bg-light border-0 py-2" name="user_id" value="{html.escape(user_id)}" placeholder="Query by Telegram user ID (optional)">
            </div>
            <div class="col-md-3 d-grid">
                <button type="submit" class="btn btn-dark fw-bold">Run Query</button>
            </div>
        </form>
    </div>

    <div class="card p-4 shadow-sm border-0 rounded-4">
        <div class="table-responsive">
            <table id="upgradeReportsTable" class="table table-hover align-middle mb-0">
                <thead class="bg-light">
                    <tr>
                        <th>Upgraded At</th>
                        <th>User ID</th>
                        <th>Username</th>
                        <th>Plan</th>
                        <th>Duration</th>
                        <th>Amount</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    """
    scripts = "<script>$(document).ready(function(){ $('#upgradeReportsTable').DataTable({'order': [[ 0, 'desc' ]]}); });</script>"
    return render_template_string(BASE_HTML, page_content=content, extra_scripts=scripts)

@app.route('/toggle_ban', methods=['POST'])
@login_required
def toggle_ban():
    uid = request.form.get('user_id').strip()
    try:
        current_status = user_db.is_banned(uid)
        new_status = not current_status
        user_db.set_banned(uid, new_status)
        
        if new_status:
            flash(f"User {uid} has been BANNED from using the bot.", "danger")
        else:
            flash(f"User {uid} has been UNBANNED.", "success")
    except AttributeError:
        flash("Error: Database function 'set_banned' is not implemented yet in user_db.py.", "danger")
        
    return redirect(url_for('users_list'))

@app.route('/export_users')
@login_required
def export_users():
    user_stats = get_user_stats(get_chat_data())
    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Telegram ID", "Username", "Messages", "First Joined", "Last Active", "Plan", "Expiry Date", "Banned"])
        yield output.getvalue()
        output.truncate(0); output.seek(0)
        for uid, data in user_stats.items():
            writer.writerow([uid, data['username'], data['message_count'], data['first_seen'], data['last_seen'], data['plan'], data.get('expiry_date', 'N/A'), data.get('is_banned', False)])
            yield output.getvalue()
            output.truncate(0); output.seek(0)
    return Response(generate(), mimetype='text/csv', headers={"Content-Disposition": "attachment; filename=phy_chatbot_users.csv"})

@app.route('/training', methods=['GET', 'POST'])
@login_required
def training():
    if request.method == 'POST':
        try:
            parsed_data = json.loads(request.form.get('knowledge_data'))
            lock_path = KNOWLEDGE_FILE + ".lock"
            with FileLock(lock_path, timeout=5):
                with open(KNOWLEDGE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(parsed_data, f, ensure_ascii=False, indent=4)
            flash("Training data successfully deployed to the AI!", "success")
        except: 
            flash("Invalid JSON format. Changes were not saved.", "danger")
        return redirect(url_for('training'))

    current_data = "{\n    \"Past_Examples\": []\n}"
    lock_path = KNOWLEDGE_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        if os.path.exists(KNOWLEDGE_FILE):
            try: 
                with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f: 
                    current_data = f.read()
            except: pass

    content = f"""
    <div class="d-flex justify-content-between align-items-center mb-4">
        <div>
            <h2 class="fw-bold m-0 text-dark"><i class="fa-solid fa-brain text-primary me-2"></i>AI Knowledge Base</h2>
            <p class="text-muted m-0 fs-5 mt-2">Manage the AI's core instructions, physics formulas, and Q&A examples.</p>
        </div>
        <div class="d-flex gap-3">
            <button type="button" class="btn btn-light border px-4 py-2 fw-bold shadow-sm rounded-3" onclick="formatJSON()">
                <i class="fa-solid fa-wand-magic-sparkles text-warning me-2"></i> Auto-Format Code
            </button>
            <button type="submit" form="jsonForm" class="btn btn-primary px-5 py-2 fw-bold shadow-sm rounded-3" id="saveBtn">
                <i class="fa-solid fa-cloud-arrow-up me-2"></i> Deploy to AI
            </button>
        </div>
    </div>
    
    <div class="card shadow-lg border-0 rounded-4 overflow-hidden mb-4" style="background-color: #1e1e1e;">
        <div class="px-4 py-3 d-flex justify-content-between align-items-center border-bottom border-secondary border-opacity-25" style="background-color: #2d2d2d;">
            <div class="d-flex align-items-center gap-2">
                <div class="rounded-circle" style="width: 12px; height: 12px; background-color: #ff5f56;"></div>
                <div class="rounded-circle" style="width: 12px; height: 12px; background-color: #ffbd2e;"></div>
                <div class="rounded-circle" style="width: 12px; height: 12px; background-color: #27c93f;"></div>
                <span class="text-white-50 ms-3 font-monospace small"><i class="fa-solid fa-file-code me-2"></i>Data_Training.json</span>
            </div>
            <div id="json-status" class="badge bg-success rounded-pill px-3 py-1 shadow-sm"><i class="fa-solid fa-check me-1"></i> Valid JSON</div>
        </div>
        
        <form id="jsonForm" method="POST" class="m-0 p-0">
            <input type="hidden" name="knowledge_data" id="hiddenData">
            <div id="editor" style="width: 100%; height: 60vh; font-size: 15px; line-height: 1.6; font-family: 'Fira Code', 'Courier New', monospace;">{current_data}</div>
        </form>
    </div>
    
    <div class="row g-4">
        <div class="col-md-6">
            <div class="card p-4 shadow-sm border-0 rounded-4 h-100 bg-primary bg-opacity-10 border border-primary border-opacity-25">
                <h5 class="fw-bold text-primary mb-3"><i class="fa-solid fa-lightbulb me-2"></i> Pro Tip: Structure</h5>
                <p class="text-dark mb-0 fs-6">Keep your JSON organized. Use arrays for lists of examples. The bot expects keys like <code>"Past_Examples"</code> to learn from past interactions. Never remove the outer curly braces <code>{{}}</code>.</p>
            </div>
        </div>
        <div class="col-md-6">
            <div class="card p-4 shadow-sm border-0 rounded-4 h-100 bg-warning bg-opacity-10 border border-warning border-opacity-25">
                <h5 class="fw-bold text-warning-emphasis mb-3"><i class="fa-solid fa-triangle-exclamation me-2"></i> Warning: Formatting</h5>
                <p class="text-dark mb-0 fs-6">If the JSON format is broken (missing commas or quotes), the <strong>Deploy</strong> button will be automatically disabled to prevent you from crashing the bot's memory.</p>
            </div>
        </div>
    </div>
    """
    
    scripts = """
    <script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.24.1/ace.js"></script>
    <script>
        var editor = ace.edit("editor");
        editor.setTheme("ace/theme/tomorrow_night"); 
        editor.session.setMode("ace/mode/json");
        editor.setOptions({
            showPrintMargin: false,
            wrap: true,
            indentedSoftWrap: true,
            tabSize: 4,
            useWorker: false
        });
        
        document.getElementById('editor').style.backgroundColor = '#1e1e1e';

        const statusBadge = document.getElementById('json-status');
        const saveBtn = document.getElementById('saveBtn');

        function validateJSON() {
            try {
                const val = editor.getValue();
                if (!val.trim()) throw new Error("Empty");
                JSON.parse(val); 
                statusBadge.className = "badge bg-success rounded-pill px-3 py-2 fs-6 shadow-sm";
                statusBadge.innerHTML = '<i class="fa-solid fa-check-circle me-1"></i> Valid JSON';
                saveBtn.disabled = false;
            } catch (e) {
                statusBadge.className = "badge bg-danger rounded-pill px-3 py-2 fs-6 shadow-sm";
                statusBadge.innerHTML = '<i class="fa-solid fa-triangle-exclamation me-1"></i> Invalid JSON';
                saveBtn.disabled = true; 
            }
        }

        editor.session.on('change', validateJSON);
        validateJSON(); 

        function formatJSON() {
            try {
                const val = JSON.parse(editor.getValue());
                editor.setValue(JSON.stringify(val, null, 4), -1);
            } catch (e) {
                Swal.fire('Format Error', 'Cannot format invalid JSON. Please fix the red errors first.', 'error');
            }
        }

        $('#jsonForm').on('submit', function() {
            $('#hiddenData').val(editor.getValue());
        });
    </script>
    """
    return render_template_string(BASE_HTML, page_content=content, extra_scripts=scripts)

# 🚀 UPGRADED CHAT HISTORY ROUTE
@app.route('/history', methods=['GET', 'POST'])
@login_required
def history():
    if request.method == 'POST':
        lock_path = HISTORY_FILE + ".lock"
        with FileLock(lock_path, timeout=5):
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f: 
                json.dump([], f)
        flash("History cleared.", "warning")
        return redirect(url_for('history'))

    chats = get_chat_data()[::-1]
    rows = ""
    for chat in chats:
        # Generate Avatar
        username_str = chat.get('username', 'unknown')
        avatar_letter = username_str[0].upper() if username_str else 'U'
        
        # User Input parsing
        user_input = html.escape(chat.get('user_input', ''))
        img_raw = chat.get('image_file')
        if img_raw:
            img_safe = secure_filename(os.path.basename(str(img_raw)))
            img_esc = html.escape(img_safe, quote=True) if img_safe else ''
            image_html = f'<div class="mt-3"><img src="/uploads/{img_esc}" class="chat-image shadow-sm" onclick="window.open(this.src)" style="max-height: 120px;"></div>' if img_esc else ''
        else:
            image_html = ''
        
        # Bot Response parsing
        bot_response = html.escape(chat.get('bot_response', ''))
        chat_del_token = html.escape(_chat_delete_token(chat), quote=True)

        rows += f"""
        <tr>
            <td class="align-middle" style="white-space: nowrap">
                <div class="fw-bold text-dark">{chat.get('timestamp', '')[:10]}</div>
                <div class="text-muted small">{chat.get('timestamp', '')[11:16]}</div>
            </td>
            <td class="align-middle">
                <div class="d-flex align-items-center">
                    <div class="fw-bold text-primary">@{username_str}</div>
                </div>
            </td>
            <td class="align-middle py-4" style="min-width: 400px; max-width: 600px;">
                <div class="d-flex align-items-start mb-3">
                    <div class="bg-primary bg-opacity-10 text-primary rounded-circle d-flex align-items-center justify-content-center me-3 fw-bold flex-shrink-0" style="width: 32px; height: 32px; font-size: 14px;">{avatar_letter}</div>
                    <div class="bg-light border rounded-3 p-3 text-dark shadow-sm w-100" style="border-top-left-radius: 0 !important; font-size: 0.95rem;">
                        {user_input}
                        {image_html}
                    </div>
                </div>
                <div class="d-flex align-items-start">
                    <div class="bg-success bg-opacity-10 text-success rounded-circle d-flex align-items-center justify-content-center me-3 fw-bold flex-shrink-0" style="width: 32px; height: 32px; font-size: 14px;"><i class="fa-solid fa-robot"></i></div>
                    <div class="bg-success bg-opacity-10 border border-success border-opacity-25 rounded-3 p-3 text-dark shadow-sm w-100" style="border-top-left-radius: 0 !important; max-height: 250px; overflow-y: auto; font-size: 0.95rem;">
                        {bot_response}
                    </div>
                </div>
            </td>
            <td class="align-middle text-center" style="width: 170px;">
                <div class="d-flex flex-column gap-2 align-items-stretch">
                    <form action="/teach" method="POST" class="m-0">
                        <input type="hidden" name="question" value="{user_input}">
                        <input type="hidden" name="answer" value="{bot_response}">
                        <button type="submit" class="btn btn-outline-success fw-bold w-100 btn-sm shadow-sm py-2 rounded-3">
                            <i class="fa-solid fa-graduation-cap d-block mb-1 fs-5"></i> Teach AI
                        </button>
                    </form>
                    <form action="/history/delete_entry" method="POST" class="m-0">
                        <input type="hidden" name="chat_delete_token" value="{chat_del_token}">
                        <button type="button" class="btn btn-outline-danger fw-bold w-100 btn-sm shadow-sm py-2 rounded-3"
                                onclick="confirmUpdate(event, this.form, 'Delete this chat log from disk? The linked upload image will be removed if it is only used here.', 'Yes, Delete', true)">
                            <i class="fa-solid fa-trash d-block mb-1 fs-6"></i> Delete
                        </button>
                    </form>
                </div>
            </td>
        </tr>
        """

    content = f"""
    <div class="d-flex justify-content-between align-items-center mb-4">
        <div>
            <h2 class="fw-bold m-0 text-dark"><i class="fa-solid fa-comments text-primary me-2"></i>Chat History</h2>
            <p class="text-muted m-0 fs-5 mt-2">Monitor all user interactions, view uploaded images, and correct AI answers. Logs older than the retention window (default 180 days, env <code>CHAT_HISTORY_RETENTION_DAYS</code>) are removed automatically for privacy; export CSV periodically if you need training backups.</p>
        </div>
        <div class="d-flex gap-3">
            <a href="/export_history" class="btn btn-success px-4 py-2 fs-6 fw-bold shadow-sm rounded-3">
                <i class="fa-solid fa-download me-2"></i> Export CSV
            </a>
            <form method="POST" class="m-0">
                <button type="button" class="btn btn-outline-danger px-4 py-2 fs-6 fw-bold shadow-sm rounded-3" 
                        onclick="confirmUpdate(event, this.form, 'Are you sure you want to permanently delete all chat logs?', 'Yes, Purge History', true)">
                    Purge Logs
                </button>
            </form>
        </div>
    </div>
    
    <div class="card p-4 shadow-sm border-0 rounded-4">
        <div class="table-responsive">
            <table id="ht" class="table table-hover align-middle mb-0">
                <thead class="bg-light">
                    <tr>
                        <th style="width: 100px;">Date & Time</th>
                        <th style="width: 150px;">User Identity</th>
                        <th>Conversation Log</th>
                        <th class="text-center">Actions</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    """
    # Initialize DataTables with our 4 columns (Action is index 3)
    return render_template_string(BASE_HTML, page_content=content, extra_scripts="<script>$(document).ready(function(){ $('#ht').DataTable({'order': [], 'columnDefs': [{ 'orderable': false, 'targets': 3 }]}); });</script>")


@app.route('/history/delete_entry', methods=['POST'])
@login_required
def history_delete_entry():
    token = (request.form.get('chat_delete_token') or '').strip()
    if not token:
        flash('Missing entry.', 'danger')
        return redirect(url_for('history'))

    lock_path = HISTORY_FILE + '.lock'
    with FileLock(lock_path, timeout=5):
        data = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    data = raw
            except Exception:
                pass

        removed_entry = None
        new_data = []
        removed = False
        for e in data:
            if not removed and _chat_delete_token(e) == token:
                removed = True
                removed_entry = e
                continue
            new_data.append(e)

        if not removed:
            flash('Entry not found (it may have already been deleted).', 'warning')
            return redirect(url_for('history'))

        if removed_entry:
            _maybe_remove_upload(removed_entry.get('image_file'))

        try:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(new_data, f, ensure_ascii=False, indent=2)
        except Exception:
            flash('Could not save chat history file.', 'danger')
            return redirect(url_for('history'))

    flash('Chat entry deleted.', 'success')
    return redirect(url_for('history'))


@app.route('/feedback')
@login_required
def feedback_page():
    feedback_data = get_feedback_data()[::-1]

    posts_html = ""
    if not feedback_data:
        posts_html = """
        <div class="card p-5 rounded-4 text-center">
            <i class="fa-regular fa-face-smile fs-1 text-muted mb-3"></i>
            <h5 class="fw-bold text-dark mb-2">No feedback posts yet</h5>
            <p class="text-muted m-0">Ask users to send feedback with <code>/feedback</code> in Telegram.</p>
        </div>
        """
    else:
        for f in feedback_data:
            username = html.escape(f.get('username', 'unknown'))
            full_name = html.escape(f.get('full_name', 'Unknown'))
            user_id = html.escape(str(f.get('user_id', '')))
            feedback_text = html.escape(f.get('feedback_text', ''))
            timestamp = html.escape(f.get('timestamp', ''))
            profile_link = html.escape(f.get('profile_link', ''), quote=True)
            image_raw = f.get('image_file')
            initials = "".join(
                part[0].upper() for part in (f.get('full_name', '') or "").split()[:2] if part
            ) or (username[:1].upper() if username else "U")

            image_html = ""
            if image_raw:
                img_safe = secure_filename(os.path.basename(str(image_raw)))
                img_esc = html.escape(img_safe, quote=True) if img_safe else ''
                if img_esc:
                    image_html = (
                        f'<div class="feedback-post-image-wrap mt-3">'
                        f'  <img src="/uploads/{img_esc}" class="feedback-post-image" onclick="window.open(this.src)">'
                        f'</div>'
                    )

            feedback_text_attr = html.escape(str(f.get('feedback_text', '')), quote=True)
            feedback_del_token = html.escape(_feedback_delete_token(f), quote=True)
            posts_html += f"""
            <article class="card p-4 rounded-4 mb-4 feedback-post">
                <div class="d-flex align-items-start justify-content-between gap-3">
                    <div class="d-flex align-items-start gap-3">
                        <div class="feedback-user-avatar-wrap">
                            <img src="/avatar/{user_id}" alt="{full_name}" class="feedback-user-avatar" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                            <div class="feedback-user-avatar-fallback" style="display:none;">{initials}</div>
                        </div>
                        <div>
                            <div class="fw-bold text-dark">{full_name}</div>
                            <div class="text-muted small">@{username}</div>
                            <div class="text-muted small">{timestamp}</div>
                            <a class="small fw-semibold" href="{profile_link}">Open Telegram Profile</a>
                        </div>
                    </div>
                    <div class="d-flex flex-column flex-sm-row align-items-stretch align-items-sm-start gap-2 flex-shrink-0">
                        <button type="button" class="btn btn-outline-primary btn-sm fw-bold copy-feedback-btn" data-feedback-text="{feedback_text_attr}">
                            <i class="fa-regular fa-copy me-1"></i>Copy
                        </button>
                        <form action="/feedback/delete_entry" method="POST" class="m-0">
                            <input type="hidden" name="feedback_delete_token" value="{feedback_del_token}">
                            <button type="button" class="btn btn-outline-danger btn-sm fw-bold w-100"
                                    onclick="confirmUpdate(event, this.form, 'Delete this feedback from disk? Any attached image file will be removed.', 'Yes, Delete', true)">
                                <i class="fa-solid fa-trash me-1"></i>Delete
                            </button>
                        </form>
                    </div>
                </div>
                <div class="feedback-message-bubble mt-3">{feedback_text}</div>
                {image_html}
            </article>
            """

    content = f"""
    <style>
        .feedback-user-avatar-wrap {{
            position: relative;
            width: 52px;
            height: 52px;
            min-width: 52px;
        }}
        .feedback-user-avatar {{
            width: 52px;
            height: 52px;
            border-radius: 999px;
            object-fit: cover;
            border: 2px solid #dbeafe;
            box-shadow: 0 3px 10px rgba(15, 23, 42, 0.08);
        }}
        .feedback-user-avatar-fallback {{
            width: 52px;
            height: 52px;
            border-radius: 999px;
            background: linear-gradient(135deg, #3b82f6, #0ea5e9);
            color: #fff;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 0.95rem;
        }}
        .feedback-post {{
            border: 1px solid #e2e8f0 !important;
            height: 100%;
        }}
        .feedback-post .fw-bold.text-dark {{
            font-size: 1rem;
            line-height: 1.3;
            color: #0f172a !important;
        }}
        .feedback-post .text-muted.small {{
            font-size: 0.82rem !important;
            color: #475569 !important;
            line-height: 1.35;
        }}
        .feedback-post a.small.fw-semibold {{
            font-size: 0.82rem;
            color: #2563eb !important;
            text-decoration: underline;
            text-underline-offset: 2px;
        }}
        .feedback-message-bubble {{
            background: #f1f5f9;
            border: 1px solid #cbd5e1;
            border-radius: 14px;
            padding: 12px 14px;
            white-space: pre-wrap;
            line-height: 1.6;
            color: #0f172a;
            font-size: 0.95rem;
            font-weight: 500;
            max-height: 140px;
            overflow-y: auto;
        }}
        .feedback-post-image-wrap {{
            border: 1px solid #e2e8f0;
            border-radius: 14px;
            overflow: hidden;
            width: 100%;
            max-height: 180px;
        }}
        .feedback-post-image {{
            width: 100%;
            height: 180px;
            object-fit: cover;
            display: block;
            cursor: pointer;
            transition: transform .2s ease;
        }}
        .feedback-post-image:hover {{
            transform: scale(1.02);
        }}
        .feedback-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 16px;
        }}
        @media (max-width: 1200px) {{
            .feedback-grid {{
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }}
        }}
        @media (max-width: 768px) {{
            .feedback-grid {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>

    <div class="mb-4">
        <h2 class="fw-bold m-0 text-dark"><i class="fa-solid fa-comment-dots text-primary me-2"></i>User Feedback Center</h2>
        <p class="text-muted m-0 fs-5 mt-2">User feedback posts</p>
    </div>
    <div class="feedback-grid">{posts_html}</div>
    """
    scripts = """
    <script>
        $(document).on('click', '.copy-feedback-btn', function() {
            const btn = this;
            const text = btn.getAttribute('data-feedback-text') || '';
            navigator.clipboard.writeText(text).then(function() {
                btn.innerHTML = '<i class="fa-solid fa-check me-1"></i>Copied';
                setTimeout(function() {
                    btn.innerHTML = '<i class="fa-regular fa-copy me-1"></i>Copy';
                }, 1200);
            }).catch(function() {
                btn.innerHTML = '<i class="fa-solid fa-xmark me-1"></i>Failed';
                setTimeout(function() {
                    btn.innerHTML = '<i class="fa-regular fa-copy me-1"></i>Copy';
                }, 1200);
            });
        });
    </script>
    """
    return render_template_string(BASE_HTML, page_content=content, extra_scripts=scripts)


@app.route('/feedback/delete_entry', methods=['POST'])
@login_required
def feedback_delete_entry():
    token = (request.form.get('feedback_delete_token') or '').strip()
    if not token:
        flash('Missing entry.', 'danger')
        return redirect(url_for('feedback_page'))

    lock_path = FEEDBACK_FILE + '.lock'
    with FileLock(lock_path, timeout=5):
        data = []
        if os.path.exists(FEEDBACK_FILE):
            try:
                with open(FEEDBACK_FILE, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    data = raw
            except Exception:
                pass

        removed_entry = None
        new_data = []
        removed = False
        for e in data:
            if not removed and _feedback_delete_token(e) == token:
                removed = True
                removed_entry = e
                continue
            new_data.append(e)

        if not removed:
            flash('Entry not found (it may have already been deleted).', 'warning')
            return redirect(url_for('feedback_page'))

        if removed_entry:
            _maybe_remove_upload(removed_entry.get('image_file'))

        try:
            with open(FEEDBACK_FILE, 'w', encoding='utf-8') as f:
                json.dump(new_data, f, ensure_ascii=False, indent=2)
        except Exception:
            flash('Could not save feedback file.', 'danger')
            return redirect(url_for('feedback_page'))

    flash('Feedback entry deleted.', 'success')
    return redirect(url_for('feedback_page'))


@app.route('/export_history')
@login_required
def export_history():
    chat_data = get_chat_data()
    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Timestamp", "Telegram ID", "Username", "User Input", "Bot Response"])
        yield output.getvalue()
        output.truncate(0); output.seek(0)
        for chat in chat_data:
            writer.writerow([chat.get('timestamp', ''), chat.get('user_id', ''), chat.get('username', ''), chat.get('user_input', ''), chat.get('bot_response', '')])
            yield output.getvalue()
            output.truncate(0); output.seek(0)
    return Response(generate(), mimetype='text/csv', headers={"Content-Disposition": "attachment; filename=phy_chatbot_history.csv"})

@app.route('/teach', methods=['POST'])
@login_required
def teach_bot():
    q, a = request.form.get('question'), request.form.get('answer')
    
    lock_path = KNOWLEDGE_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        data = {"Past_Examples": []}
        if os.path.exists(KNOWLEDGE_FILE):
            try: data = json.load(open(KNOWLEDGE_FILE, 'r', encoding='utf-8'))
            except: pass
            
        if not isinstance(data, dict): data = {"Past_Examples": []}
        if "Past_Examples" not in data: data["Past_Examples"] = []
        
        data["Past_Examples"].append({"User_Asked": q, "Perfect_Answer": a})
        
        with open(KNOWLEDGE_FILE, 'w', encoding='utf-8') as f: 
            json.dump(data, f, ensure_ascii=False, indent=4)
            
    flash("Knowledge learned successfully! Bot has been updated.", "success")
    return redirect(url_for('history'))

# 🚀 UPGRADED BROADCAST ROUTE
@app.route('/broadcast', methods=['GET', 'POST'])
@login_required
def broadcast():
    if request.method == 'POST':
        msg = request.form.get('broadcast_message')
        ids = user_db.get_all_users_dict().keys()
        
        count = 0
        for uid in ids:
            try:
                safe_msg = html.escape(msg)
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": uid, "text": f"📢 <b>Announcement:</b>\n\n{safe_msg}", "parse_mode": "HTML"})
                count += 1
            except: pass
        flash(f"Broadcast delivered to {count} users.", "success")
    
    content = """
    <div class="mb-4">
        <h2 class="fw-bold m-0 text-dark"><i class="fa-solid fa-bullhorn text-warning me-2"></i> Global Broadcast</h2>
        <p class="text-muted m-0 fs-5 mt-2">Send an instant alert or announcement to all registered users simultaneously.</p>
    </div>
    
    <div class="row g-4">
        <div class="col-md-7">
            <div class="card p-4 border-0 shadow-sm rounded-4 h-100">
                <form method='POST' id="broadcastForm">
                    <label class="form-label fw-bold text-dark fs-5 mb-3"><i class="fa-solid fa-pen-nib me-2 text-primary"></i>Compose Message</label>
                    <textarea name='broadcast_message' id="bmsg" class='form-control bg-light border-0 p-4 mb-4 fs-5' rows='10' placeholder='Type your announcement here...' required oninput="updatePreview()"></textarea>
                    
                    <button type='button' class='btn btn-primary px-5 py-3 fw-bold shadow-sm fs-5 rounded-pill w-100' 
                            onclick="confirmUpdate(event, this.form, 'Are you sure you want to send this broadcast to EVERY user?', 'Yes, Send Now', false)">
                        <i class="fa-solid fa-paper-plane me-2"></i> Send Broadcast to All Users
                    </button>
                </form>
            </div>
        </div>
        
        <div class="col-md-5">
            <div class="card p-4 border-0 shadow-sm rounded-4 h-100" style="background-color: #e2e8f0; background-image: url('https://www.transparenttextures.com/patterns/cubes.png');">
                <label class="form-label fw-bold text-dark fs-5 mb-3"><i class="fa-solid fa-eye me-2 text-success"></i>Telegram Preview</label>
                
                <div class="bg-white p-3 rounded-4 shadow-sm mt-2" style="border-bottom-left-radius: 4px; max-width: 90%;">
                    <div class="fw-bold text-primary mb-1">Phy_Chatbot</div>
                    <div class="text-dark" style="white-space: pre-wrap; font-size: 15px;">📢 <b>Announcement:</b><br><br><span id="msgContent" class="text-muted fst-italic">Your message will appear here...</span></div>
                    <div class="text-end text-muted mt-2" style="font-size: 11px;" id="liveTime">10:42 AM</div>
                </div>
            </div>
        </div>
    </div>
    """
    
    scripts = """
    <script>
        // Update the time on the fake telegram message
        function formatTime() {
            var date = new Date();
            var hours = date.getHours();
            var minutes = date.getMinutes();
            var ampm = hours >= 12 ? 'PM' : 'AM';
            hours = hours % 12;
            hours = hours ? hours : 12; 
            minutes = minutes < 10 ? '0'+minutes : minutes;
            document.getElementById('liveTime').innerText = hours + ':' + minutes + ' ' + ampm;
        }
        formatTime();

        // Live preview of the broadcast message
        function updatePreview() {  
            let text = document.getElementById('bmsg').value;
            let preview = document.getElementById('msgContent');
            if (text.trim() === '') {
                preview.innerHTML = '<span class="text-muted fst-italic">Your message will appear here...</span>';
            } else {
                // Safely escape HTML characters so it mirrors the actual python backend logic
                let escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                preview.innerHTML = escaped;
            }
        }
    </script>
    """
    return render_template_string(BASE_HTML, page_content=content, extra_scripts=scripts)

if __name__ == '__main__':
    _port = int(os.getenv('PORT', '5000'))
    _host = os.getenv('FLASK_HOST', '0.0.0.0')
    app.run(debug=_FLASK_DEBUG, host=_host, port=_port)