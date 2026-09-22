"""End-to-end wiring check: real servers, both senders, signed Recall events, SSE.

Jev itself is replaced by a local stand-in (tests/fake_jev.py), so this needs no key
and proves transport and scheduling, not classification. Run from a clone with
`python scripts/smoke.py` after installing the package.
"""

import asyncio
import base64
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from fake_jev import FakeJev

TOKEN = "synthetic-ingress-token-for-local-smoke-only"
KEY = b"synthetic-recall-secret-for-local-smoke"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def server(args, url, env):
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "jev_incall.cli", *args],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with httpx.Client(timeout=1, trust_env=False) as client:
                        if client.get(url).status_code == 200:
                            break
                except httpx.RequestError:
                    pass
                if process.poll() is not None or time.monotonic() >= deadline:
                    log.seek(0)
                    raise RuntimeError("Server failed to start:\n" + log.read())
                time.sleep(0.05)
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def sender(protocol, endpoint, env):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "jev_incall.cli",
            "send",
            "examples/meeting.jsonl",
            "--transport",
            protocol,
            "--url",
            endpoint,
            "--delay",
            "0.01",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return [json.loads(line) for line in result.stdout.splitlines()]


async def check_events(api, ingress, meeting_id, env, port):
    async with (
        httpx.AsyncClient(timeout=10, trust_env=False) as client,
        client.stream("GET", f"{api}/api/meetings/{meeting_id}/events") as events,
    ):
        assert events.status_code == 200
        webhook = await asyncio.to_thread(sender, "webhook", ingress + "/webhooks/transcript", env)
        assert len(webhook) == 8 and all(ack["changed"] for ack in webhook)
        websocket = await asyncio.to_thread(
            sender, "websocket", f"ws://127.0.0.1:{port}/stream", env
        )
        assert len(websocket) == 8 and not any(ack["changed"] for ack in websocket)
        lines = events.aiter_lines()
        while True:
            line = await anext(lines)
            if line.startswith("data: "):
                state = json.loads(line[6:])
                if state["evaluated_version"] == 8:
                    assert state["coverage"] == 1 and not state["pending"]
                    break
        # Use the published provider envelope and signature format on a real HTTP socket.
        payloads = await asyncio.to_thread((ROOT / "examples/recall-events.jsonl").read_text)
        for number, line in enumerate(payloads.splitlines()):
            payload = json.loads(line)
            payload["data"]["bot"]["metadata"]["jev_meeting_id"] = meeting_id
            raw = json.dumps(payload).encode()
            timestamp = str(int(time.time()))
            msg_id = f"smoke-{number}"
            signature = base64.b64encode(
                hmac.digest(KEY, f"{msg_id}.{timestamp}.".encode() + raw, "sha256")
            ).decode()
            headers = {
                "webhook-id": msg_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": "v1," + signature,
            }
            result = await client.post(ingress + "/webhooks/recall", content=raw, headers=headers)
            assert result.status_code == 200 and result.json()["changed"], result.text
            duplicate = await client.post(
                ingress + "/webhooks/recall", content=raw, headers=headers
            )
            assert duplicate.status_code == 200 and not duplicate.json()["changed"]
        while True:
            line = await anext(lines)
            if line.startswith("data: "):
                state = json.loads(line[6:])
                if state["evaluated_version"] == 16:
                    assert state["coverage"] == 1
                    break
        response = await client.delete(f"{api}/api/meetings/{meeting_id}")
        assert response.status_code == 204
        async for line in lines:
            if line == "event: closed":
                break
        else:
            raise AssertionError("SSE did not report meeting closure")
    print(
        "PASS: webhook sender, WebSocket sender, signed Recall events, deduplication, "
        "scores from the local Jev stand-in, SSE, shutdown"
    )


def main():
    dashboard_port, gateway_port = free_port(), free_port()
    while gateway_port == dashboard_port:
        gateway_port = free_port()
    api = f"http://127.0.0.1:{dashboard_port}"
    ingress = f"http://127.0.0.1:{gateway_port}"
    with FakeJev() as jev:
        env = {
            **os.environ,
            **jev.env,
            "JEV_INGEST_TOKEN": TOKEN,
            "RECALL_WEBHOOK_SECRET": "whsec_" + base64.b64encode(KEY).decode(),
        }
        run_servers(api, ingress, dashboard_port, gateway_port, env)


def run_servers(api, ingress, dashboard_port, gateway_port, env):
    with server(
        ["serve", "--port", str(dashboard_port), "--interval", "0.1"],
        api + "/api/config",
        env,
    ):
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.post(api + "/api/meetings", json={})
            response.raise_for_status()
            meeting_id = response.json()["meeting_id"]
        with server(
            [
                "gateway",
                "--meeting-id",
                meeting_id,
                "--api-url",
                api,
                "--port",
                str(gateway_port),
                "--roles",
                "examples/roles.json",
            ],
            ingress + "/healthz",
            env,
        ):
            asyncio.run(
                asyncio.wait_for(check_events(api, ingress, meeting_id, env, gateway_port), 45)
            )


if __name__ == "__main__":
    main()
