import os
import runpy

# Ensure the Flask app binds correctly on Render (and similar hosts).
os.environ.setdefault('FLASK_HOST', '0.0.0.0')
os.environ.setdefault('PORT', os.environ.get('PORT', '5000'))

# Locate the `admin_web.py` entrypoint anywhere in the repository and run it.
target = None
for dirpath, dirnames, filenames in os.walk(os.getcwd()):
    if 'admin_web.py' in filenames:
        target = os.path.join(dirpath, 'admin_web.py')
        break

if target is None:
    # fallback to the common package layout
    candidate = os.path.join(os.getcwd(), 'khmer_physics_bot', 'admin_web.py')
    if os.path.exists(candidate):
        target = candidate

if target is None:
    raise FileNotFoundError('admin_web.py not found in repository')

runpy.run_path(target, run_name='__main__')
