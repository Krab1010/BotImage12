from flask import Flask
import threading
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "OK", 200

def run_bot():
    try:
        import BotImage
        BotImage.main()
    except Exception as e:
        logger.error(f"Bot error: {e}")

if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    logger.info("Bot started in background thread")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)