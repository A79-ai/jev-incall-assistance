"""A separate, authenticated ingress server; the dashboard stays on loopback."""

import base64
import hashlib
import hmac
import json
import math
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from .models import Identifier, Turn

MAX_BODY = 262144


def local_api_url(value: str) -> str:
    url = urlparse(value)
    if (
        url.scheme != "http"
        or url.hostname not in ("127.0.0.1", "localhost", "::1")
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError("Dashboard API URL must be an HTTP loopback origin")
    return value.rstrip("/")


def secret_bytes(secret: str) -> bytes:
    try:
        if not secret.startswith("whsec_"):
            raise ValueError
        key = base64.b64decode(secret[6:], validate=True)
        if len(key) < 16:
            raise ValueError
        return key
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "RECALL_WEBHOOK_SECRET must be a valid whsec_ verification secret"
        ) from exc


def verify_recall(body: bytes, headers, key: bytes, now=None):
    """Recall's documented HMAC format, with a five-minute replay window."""
    msg_id = headers.get("webhook-id") or headers.get("svix-id", "")
    timestamp = headers.get("webhook-timestamp") or headers.get("svix-timestamp", "")
    signatures = headers.get("webhook-signature") or headers.get("svix-signature", "")
    try:
        if not msg_id or abs((time.time() if now is None else now) - int(timestamp)) > 300:
            raise ValueError
        signed = f"{msg_id}.{timestamp}.".encode() + body
        expected = base64.b64encode(hmac.digest(key, signed, "sha256")).decode()
        if not any(
            hmac.compare_digest(signature[3:], expected)
            for signature in signatures.split()
            if signature.startswith("v1,")
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError) as exc:
        raise HTTPException(401, "Invalid or expired Recall signature") from exc


def recall_turn(payload, meeting_id: str, roles: dict) -> Turn | None:
    """Translate finalized Recall utterances. Unknown speakers stay participants."""
    try:
        data = payload["data"]
        if data["bot"]["metadata"].get("jev_meeting_id") != meeting_id:
            raise HTTPException(403, "Recall bot is not bound to this meeting")
        if payload["event"] == "transcript.partial_data":
            return None
        if payload["event"] != "transcript.data":
            raise ValueError("Expected transcript.data")
        utterance = data["data"]
        speaker = utterance["participant"]
        words = utterance["words"]
        if not isinstance(words, list) or not words:
            raise ValueError("Expected nonempty words")
        text = " ".join(word["text"] for word in words).strip()
        relative = words[0]["start_timestamp"]["relative"]
        if type(relative) not in (int, float) or not math.isfinite(relative) or relative < 0:
            raise ValueError("Invalid timestamp")
        # Timestamp + speaker + transcript identifies an utterance independently
        # of delivery IDs. Re-sent finalized utterances are idempotent even after
        # a gateway restart. Conflicting finals get 409; don't invent revisions.
        identity = json.dumps([data["transcript"]["id"], speaker["id"], float(relative)])
        role = roles.get(str(speaker["id"]), roles.get(speaker.get("name"), "participant"))
        return Turn(
            turn_id="recall_" + hashlib.sha256(identity.encode()).hexdigest(),
            speaker_role=role,
            start_ms=round(relative * 1000),
            text=text,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(422, "Invalid Recall transcript event") from exc


def validate_roles(roles):
    if not isinstance(roles, dict) or any(
        not isinstance(k, str) or v not in ("buyer", "seller", "participant")
        for k, v in roles.items()
    ):
        raise ValueError(
            "Roles must map participant IDs or exact names to buyer/seller/participant"
        )
    return roles


def create_gateway(
    meeting_id: str,
    api_url="http://127.0.0.1:8000",
    token="",
    recall_secret="",
    roles=None,
    transport=None,
):
    meeting_id = TypeAdapter(Identifier).validate_python(meeting_id)
    api_url = local_api_url(api_url)
    if token and len(token) < 32:
        raise ValueError("JEV_INGEST_TOKEN must contain at least 32 characters")
    if not token and not recall_secret:
        raise ValueError(
            "Set JEV_INGEST_TOKEN and/or RECALL_WEBHOOK_SECRET before starting gateway"
        )
    key = secret_bytes(recall_secret) if recall_secret else None
    roles = validate_roles(roles if roles is not None else {})
    client = httpx.AsyncClient(base_url=api_url, timeout=5.0, transport=transport)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def authorize(headers):
        value = headers.get("authorization", "")
        if not token or not hmac.compare_digest(value.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "Missing or invalid ingestion token")

    async def forward(turn):
        try:
            response = await client.post(
                f"/api/meetings/{meeting_id}/turns", json=turn.model_dump()
            )
        except httpx.RequestError as exc:
            raise HTTPException(503, "Dashboard unavailable; retry this same event") from exc
        if response.status_code == 404:
            raise HTTPException(
                404, "Target meeting not found; create a meeting and restart gateway"
            )
        if response.status_code == 409:
            raise HTTPException(409, "Turn conflicts with stored revision or meeting limit reached")
        if response.is_error:
            raise HTTPException(503, "Dashboard rejected delivery; retry this same event")
        return {"type": "ack", "turn_id": turn.turn_id, **response.json()}

    async def body(request):
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > MAX_BODY:
                raise HTTPException(413, "Transcript event exceeds 256 KiB")
        return bytes(chunks)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/webhooks/transcript")
    async def transcript(request: Request):
        authorize(request.headers)
        raw = await body(request)
        try:
            turn = Turn.model_validate_json(raw)
        except ValidationError as exc:
            raise HTTPException(422, "Expected a valid Turn JSON object") from exc
        return await forward(turn)

    @app.post("/webhooks/recall")
    async def recall(request: Request):
        if key is None:
            raise HTTPException(503, "Recall integration is not configured")
        raw = await body(request)
        verify_recall(raw, request.headers, key)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, "Invalid JSON") from exc
        turn = recall_turn(payload, meeting_id, roles)
        if turn is None:
            return {"type": "ack", "changed": False, "ignored": "interim"}
        return await forward(turn)

    @app.websocket("/stream")
    async def stream(socket: WebSocket):
        try:
            authorize(socket.headers)
        except HTTPException:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                raw = message.get("text")
                if raw is None or len(raw.encode()) > MAX_BODY:
                    await socket.close(code=1009 if raw is not None else 1003)
                    return
                try:
                    turn = Turn.model_validate_json(raw)
                    await socket.send_json(await forward(turn))
                except ValidationError:
                    await socket.send_json(
                        {"type": "error", "status": 422, "detail": "Invalid Turn"}
                    )
                except HTTPException as exc:
                    await socket.send_json(
                        {"type": "error", "status": exc.status_code, "detail": exc.detail}
                    )
        except WebSocketDisconnect:
            return

    return app
