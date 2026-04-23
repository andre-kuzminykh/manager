"""Alternative entrypoint for orchestrators that require a listening TCP port
(Cloud Run, some load balancers). Starts the Socket Mode bot in a thread and
serves a tiny HTTP health endpoint on $PORT (default 8080).

Use this as the container CMD when deploying to Cloud Run:
    CMD ["python", "-m", "ops.entrypoint_with_health"]
"""
from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.logging_setup import get_logger, setup_logging
from app.main import run as run_bot

log = get_logger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- stdlib naming
        if self.path in ("/", "/healthz", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args, **_kwargs) -> None:  # noqa: N802
        pass  # silence default stderr logging


def main() -> None:
    setup_logging()
    port = int(os.environ.get("PORT", "8080"))

    bot_thread = threading.Thread(target=run_bot, name="slack-bot", daemon=True)
    bot_thread.start()
    log.info("health_server_starting", port=port)

    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
