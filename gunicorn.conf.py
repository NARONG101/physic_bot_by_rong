import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
# Single worker avoids conflicting reads/writes to SQLite on small hosts (e.g. Render).
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = 120
keepalive = 5
