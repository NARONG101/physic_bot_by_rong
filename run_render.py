import os
import runpy
import multiprocessing
import signal
import sys

# Ensure the Flask app binds correctly on Render.
os.environ.setdefault('FLASK_HOST', '0.0.0.0')
os.environ.setdefault('PORT', os.environ.get('PORT', '5000'))
# Disable Flask debug/reloader to avoid Windows multiprocessing conflicts
# (the admin panel enables debug by default during development).
os.environ['FLASK_DEBUG'] = 'false'
# Provide a safe dev fallback secret so the admin web can start when no secret is set.
# In production, set a strong secret via environment variables instead.
os.environ.setdefault('FLASK_SECRET_KEY', 'dev-phy-admin-session-key-CHANGE-IN-PRODUCTION')

# Locate entrypoints relative to this file (khmer_physics_bot folder)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_PATH = os.path.join(BASE_DIR, 'admin_web.py')
BOT_PATH = os.path.join(BASE_DIR, 'app.py')


def _run_script(path):
    runpy.run_path(path, run_name='__main__')


def main():
    procs = []

    if os.path.exists(ADMIN_PATH):
        p_admin = multiprocessing.Process(target=_run_script, args=(ADMIN_PATH,), name='admin_web')
        procs.append(p_admin)

    if os.path.exists(BOT_PATH):
        p_bot = multiprocessing.Process(target=_run_script, args=(BOT_PATH,), name='telegram_bot')
        procs.append(p_bot)

    if not procs:
        raise FileNotFoundError('Neither admin_web.py nor app.py were found next to this run_render.py')

    for p in procs:
        p.start()
        print(f"Started {p.name} (pid={p.pid})")

    def _term(signum, frame):
        print(f"Received signal {signum}, terminating children...")
        for p in procs:
            if p.is_alive():
                p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _term)
    signal.signal(signal.SIGTERM, _term)

    try:
        while True:
            for p in procs:
                if p.exitcode is not None:
                    print(f"Process {p.name} exited with code {p.exitcode}")
                    for other in procs:
                        if other is not p and other.is_alive():
                            other.terminate()
                    sys.exit(p.exitcode or 0)
            for p in procs:
                p.join(timeout=0.5)
    except KeyboardInterrupt:
        _term(signal.SIGINT, None)


if __name__ == '__main__':
    main()

