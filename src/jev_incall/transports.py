"""CLI sender for the generic webhook and WebSocket contracts."""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .models import Turn


async def send_turns(source, url, token, protocol, delay):
    parsed = urlparse(url)
    schemes = ("http", "https") if protocol == "webhook" else ("ws", "wss")
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"Expected a {protocol} endpoint URL")
    if parsed.scheme in ("http", "ws") and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Use HTTPS/WSS for non-loopback endpoints")
    if not token:
        raise ValueError("Set JEV_INGEST_TOKEN for the sender")
    headers = {"Authorization": f"Bearer {token}"}
    stream = sys.stdin if source == "-" else await asyncio.to_thread(Path(source).open)

    def report(reply):
        if reply.get("type") != "ack":
            raise ValueError(f"Gateway rejected event: {reply.get('detail', 'invalid response')}")
        print(json.dumps(reply), flush=True)

    async def lines():
        while line := await asyncio.to_thread(stream.readline):
            if line.strip():
                yield Turn.model_validate_json(line)

    try:
        if protocol == "webhook":
            async with httpx.AsyncClient(timeout=10) as client:
                async for turn in lines():
                    r = await client.post(url, json=turn.model_dump(), headers=headers)
                    if r.is_error:
                        raise ValueError(
                            f"Gateway returned HTTP {r.status_code}; event not acknowledged"
                        )
                    report(r.json())
                    await asyncio.sleep(delay)
        else:
            async with connect(url, additional_headers=headers, max_size=262144) as socket:
                async for turn in lines():
                    await socket.send(turn.model_dump_json())
                    report(json.loads(await asyncio.wait_for(socket.recv(), timeout=10)))
                    await asyncio.sleep(delay)
    except (httpx.RequestError, WebSocketException, TimeoutError) as exc:
        raise ValueError(
            "Transport failed; resend unacknowledged events with the same IDs/revisions"
        ) from exc
    finally:
        if stream is not sys.stdin:
            stream.close()
