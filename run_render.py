import threading
import os
import sys
import logging

# Import the Flask app from the admin web module so routes are registered
from admin_web import app as flask_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# We'll start the full bot implementation defined in `app.py` by calling its `main()`.
# This ensures the bot uses all handlers and logic already implemented there.
from app import main as bot_main


def run_bot():
    logger.info("Starting full Telegram bot (app.main)...")
    try:
        bot_main()
    except Exception as e:
        logger.exception("Bot crashed: %s", e)


if __name__ == "__main__":
    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Start Flask app (use PORT env var provided by Render)
    port = int(os.getenv("PORT", "10000"))
    host = "0.0.0.0"
    logger.info("Starting Flask app on %s:%s", host, port)
    flask_app.run(host=host, port=port, debug=False, use_reloader=False)
