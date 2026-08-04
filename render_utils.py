import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/healthz", "/health"}:
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def get_listen_port(default: str = "8000") -> int:
    raw = os.getenv("PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return int(default)


def start_health_server(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    listen_port = port if port is not None else get_listen_port()
    server = ThreadingHTTPServer((host, listen_port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def start_keepalive_loop() -> threading.Event | None:
    keepalive_url = os.getenv("KEEPALIVE_URL")
    if not keepalive_url:
        return None

    try:
        interval_seconds = int(os.getenv("KEEPALIVE_INTERVAL_SECONDS", "300"))
    except ValueError:
        interval_seconds = 300

    if interval_seconds <= 0:
        return None

    stop_event = threading.Event()

    def _runner() -> None:
        while not stop_event.is_set():
            try:
                with urllib.request.urlopen(keepalive_url, timeout=10) as response:  # noqa: S310
                    response.read(1)
            except Exception:
                pass
            stop_event.wait(interval_seconds)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return stop_event
