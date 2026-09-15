"""Ephemeral loopback UI for the shared interview; no filesystem serving."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading
import time

from .interview import Stopped, validate_answer


class Browser:
    channel = 'browser'

    def __init__(self):
        self.condition = threading.Condition()
        self.pending = None
        self.response = None
        self.status = {'phase': 'preparation', 'message': 'Waiting for the test runner.'}
        self.stopped = False
        self.finished = False
        self.seen_finished = threading.Event()
        self.last_seen = None
        self.token = secrets.token_urlsafe(32)
        panel = Path(__file__).with_name('interview.html').read_bytes()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, body, mime='application/json'):
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authorized(self, token=True):
                if self.headers.get('Host') != owner.host:
                    self.send(403, b'{"error":"invalid host"}')
                    return False
                origin = self.headers.get('Origin')
                if origin is not None and origin != owner.origin:
                    self.send(403, b'{"error":"invalid origin"}')
                    return False
                if token and not hmac.compare_digest(self.headers.get('X-Session-Token', ''), owner.token):
                    self.send(403, b'{"error":"invalid session token"}')
                    return False
                return True

            def do_GET(self):
                if self.path == '/':
                    if self.authorized(token=False):
                        self.send(200, panel, 'text/html; charset=utf-8')
                elif self.path == '/state':
                    if self.authorized():
                        with owner.condition:
                            owner.last_seen = time.monotonic()
                            state = {'question': owner.pending, 'status': owner.status,
                                     'finished': owner.finished, 'stopped': owner.stopped}
                            data = json.dumps(state, ensure_ascii=False).encode()
                            if owner.finished:
                                owner.seen_finished.set()
                        self.send(200, data)
                else:
                    self.send(404, b'{}')

            def do_POST(self):
                if not self.authorized():
                    return
                if self.path not in ('/answer', '/stop'):
                    self.send(404, b'{}')
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 16384 or self.headers.get('Content-Type') != 'application/json':
                        raise ValueError('expected a bounded JSON body')
                    self.connection.settimeout(3)
                    body = json.loads(self.rfile.read(size))
                    with owner.condition:
                        if self.path == '/stop':
                            if body != {'stop': True}:
                                raise ValueError('invalid stop request')
                            owner.stopped = True
                        else:
                            if owner.finished or owner.stopped or owner.pending is None or owner.response is not None:
                                raise ValueError('no unanswered pending question')
                            owner.response = validate_answer(body, owner.pending, owner.channel)
                        owner.condition.notify_all()
                    self.send(200, b'{"accepted":true}')
                except (ValueError, TypeError, TimeoutError, OSError) as error:
                    self.send(409, json.dumps({'error': str(error)}).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.host = '127.0.0.1:%d' % self.server.server_port
        self.origin = 'http://' + self.host
        # The secret is a fragment, not a request path or access-log entry.
        self.url = self.origin + '/#' + self.token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print('Owner panel: ' + self.url, file=sys.stderr, flush=True)

    def notify(self, event):
        with self.condition:
            self.status = event

    def ask(self, question, timeout):
        deadline = time.monotonic() + timeout
        with self.condition:
            if self.stopped:
                raise Stopped('owner stopped from the browser')
            self.pending, self.response = question, None
            try:
                while self.response is None:
                    if self.stopped:
                        raise Stopped('owner stopped from the browser')
                    if self.last_seen is not None and time.monotonic() - self.last_seen > 90:
                        raise Stopped('owner browser disconnected; session incomplete')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Stopped('owner response timed out; session incomplete')
                    self.condition.wait(min(remaining, 1))
                return self.response
            finally:
                self.pending = None
                self.response = None

    def close(self):
        with self.condition:
            self.finished = True
            self.pending = None
            self.condition.notify_all()
        if self.last_seen is not None:
            self.seen_finished.wait(2)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
