"""A local stand-in for the Jev HTTP API, used only by the tests and scripts/smoke.py.

The shipped application has no offline mode. This module exists so the wiring
(servers, senders, scheduling, SSE) can be exercised in CI without a key.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jev_incall.client import validate_result

API_KEY = "fake-key"


def sample_answer(questions, choice=None):
    """A valid choice response: every field gets `choice` when the map allows it."""
    answers = {}
    for key, q in questions.items():
        labels = list(q["criteria"])
        picked = choice if choice in labels else "supported" if "supported" in labels else labels[0]
        probabilities = {
            label: 0.96 if label == picked else 0.04 / (len(labels) - 1) for label in labels
        }
        answers[key] = {
            "type": "choice",
            "choice": picked,
            "probabilities": probabilities,
            "confidence": 0.8,
        }
    return {
        "model": "fake-jev",
        "answers": answers,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


class FakeEvaluator:
    """In-process evaluator for engine and server tests."""

    model = "fake-jev"

    def __init__(self, choice=None):
        self.choice = choice

    async def evaluate(self, snapshot, questions):
        return validate_result(sample_answer(questions, self.choice), questions)

    async def aclose(self):
        pass


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")) or b"{}"))
        if self.headers.get("Authorization") != f"Bearer {API_KEY}":
            payload, status = b'{"detail":"invalid key"}', 401
        else:
            payload, status = json.dumps(sample_answer(body["questions"])).encode(), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class FakeJev:
    """Context manager running the stand-in on a free loopback port.

    Point the application at it with TYPESAFE_API_URL=<url> and TYPESAFE_API_KEY=fake-key.
    """

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def env(self):
        return {"TYPESAFE_API_KEY": API_KEY, "TYPESAFE_API_URL": self.url}

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
