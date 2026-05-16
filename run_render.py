import os
import runpy

# Ensure the Flask app binds correctly on Render.
os.environ.setdefault('FLASK_HOST', '0.0.0.0')
os.environ.setdefault('PORT', '5000')

# Execute the admin_web entrypoint as a script.
runpy.run_path('khmer_physics_bot/admin_web.py', run_name='__main__')
