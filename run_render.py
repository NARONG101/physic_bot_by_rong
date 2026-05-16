import os
import sys
import threading
import logging

# Ensure the submodule directory is the current working directory.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(CURRENT_DIR)
sys.path.insert(0, CURRENT_DIR)

import admin_web
from app import main as bot_main

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_bot():
    logger.info("Starting full Telegram bot from app.py...")
    try:
        bot_main()
    except Exception:
        logger.exception("Bot crashed")
        raise


if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.getenv("PORT", "10000"))
    host = "0.0.0.0"
    logger.info("Starting Flask admin web on %s:%s", host, port)
    admin_web.app.run(host=host, port=port, debug=False, use_reloader=False)
