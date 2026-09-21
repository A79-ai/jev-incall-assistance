"""Local reference API and dashboard. Run a single process on loopback."""

import asyncio
import json
import math
import uuid
from contextlib import asynccontextmanager
from importlib.resources import files
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .demo import demo_turns
from .engine import Meeting
from .models import CreateMeeting, Turn
from .questions import FRAMEWORKS, load_questions


def create_app(evaluator, interval=2.0, mock=False):
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be positive")
    meetings: dict[str, Meeting] = {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        for meeting in meetings.values():
            await meeting.close()
        await evaluator.aclose()

    app = FastAPI(title="Jev in-call assistance", version="0.1.0", lifespan=lifespan)
    app.state.meetings = meetings

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = urlparse("http://" + request.headers.get("host", "")).hostname
        origin = request.headers.get("origin")
        if host not in ("localhost", "127.0.0.1", "::1", "testserver"):
            return JSONResponse(
                {"detail": "This reference server is localhost-only"}, status_code=403
            )
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-origin access is disabled"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path in ("/docs", "/docs/oauth2-redirect", "/redoc"):
            # FastAPI's documentation UI loads its assets from this CDN and
            # uses an inline bootstrap. Keep the dashboard's policy strict.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https://fastapi.tiangolo.com; "
                "connect-src 'self'; frame-ancestors 'none'"
            )
        return response

    def get_meeting(meeting_id):
        if meeting_id not in meetings:
            raise HTTPException(404, "Meeting not found")
        return meetings[meeting_id]

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return files("jev_incall").joinpath("static/index.html").read_text()

    @app.get("/app.js")
    async def script():
        return Response(
            files("jev_incall").joinpath("static/app.js").read_text(), media_type="text/javascript"
        )

    @app.get("/style.css")
    async def style():
        return Response(
            files("jev_incall").joinpath("static/style.css").read_text(), media_type="text/css"
        )

    @app.get("/api/config")
    async def config():
        return {
            "mock": mock,
            "interval_seconds": interval,
            "frameworks": FRAMEWORKS,
            "model": evaluator.model,
        }

    @app.get("/api/demo")
    async def demo():
        return demo_turns()

    @app.post("/api/meetings", status_code=201)
    async def create(body: CreateMeeting):
        if len(meetings) >= 16:
            raise HTTPException(429, "Close a meeting first; this demo allows 16 active meetings")
        meeting_id = str(uuid.uuid4())
        meeting = Meeting(
            meeting_id,
            load_questions(body.framework),
            evaluator,
            interval=interval,
            window_turns=12 if body.framework == "sentiment" else None,
            framework=body.framework,
        )
        meetings[meeting_id] = meeting
        meeting.start()
        return meeting.view()

    @app.get("/api/meetings/{meeting_id}")
    async def state(meeting_id: str):
        return get_meeting(meeting_id).view()

    @app.get("/api/meetings/{meeting_id}/events")
    async def events(meeting_id: str):
        meeting = get_meeting(meeting_id)

        async def stream_state():
            last = None
            heartbeat = 0
            while meetings.get(meeting_id) is meeting:
                state = json.dumps(meeting.view(), separators=(",", ":"), allow_nan=False)
                if state != last:
                    yield f"event: state\ndata: {state}\n\n"
                    last = state
                    heartbeat = 0
                elif heartbeat >= 60:
                    yield ": keepalive\n\n"
                    heartbeat = 0
                heartbeat += 1
                await asyncio.sleep(0.25)
            yield "event: closed\ndata: {}\n\n"

        return StreamingResponse(
            stream_state(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post("/api/meetings/{meeting_id}/turns")
    async def upsert(meeting_id: str, turn: Turn):
        meeting = get_meeting(meeting_id)
        try:
            changed = meeting.upsert(turn)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"changed": changed, "transcript_version": meeting.version}

    @app.delete("/api/meetings/{meeting_id}", status_code=204)
    async def close(meeting_id: str):
        meeting = get_meeting(meeting_id)
        await meeting.close()
        del meetings[meeting_id]
        return Response(status_code=204)

    return app
