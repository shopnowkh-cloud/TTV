import os
import sys
import json
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


async def _process(update_data: dict):
    from telegram import Update
    from bot import create_app

    application = create_app()
    await application.initialize()
    await application.start()
    try:
        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)
    finally:
        await application.stop()
        await application.shutdown()


def _run_in_thread(update_data: dict):
    asyncio.run(_process(update_data))


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        update_data = None
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            update_data = json.loads(body)
        except Exception as e:
            logging.error(f"Failed to parse request: {e}", exc_info=True)

        # Reply 200 OK immediately so Telegram never retries
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

        # Process the update in a background thread.
        # join() keeps the Vercel function alive until synthesis is done.
        if update_data:
            t = threading.Thread(target=_run_in_thread, args=(update_data,), daemon=True)
            t.start()
            t.join(timeout=55)

    def do_GET(self):
        token_set = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
        status = "OK" if token_set else "ERROR: TELEGRAM_BOT_TOKEN not set"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"Telegram TTS Bot | Status: {status}".encode())

    def log_message(self, format, *args):
        logging.info(f"[{self.address_string()}] {format % args}")
