import asyncio
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from jev_incall.client import EvaluationError, JevClient, validate_result
from jev_incall.cost import estimate
from jev_incall.demo import DemoEvaluator, demo_turns
from jev_incall.engine import Meeting
from jev_incall.models import Turn
from jev_incall.questions import load_questions
from jev_incall.server import create_app


def turn(text="Original", revision=1, final=True):
    return Turn(turn_id="t1", text=text, revision=revision, final=final)


@pytest.fixture
async def response():
    return await DemoEvaluator().evaluate({"turns": demo_turns()}, load_questions())


@pytest.mark.parametrize("framework,count", [("meddpicc", 8), ("bant", 4), ("sentiment", 1)])
def test_prompt_maps(framework, count):
    questions = load_questions(framework)
    assert len(questions) == count
    assert all("instructions" in q and q["type"] == "choice" for q in questions.values())


def test_duplicate_partial_stale_and_correction():
    m = Meeting("m", load_questions(), DemoEvaluator())
    assert not m.upsert(turn(final=False))
    assert m.version == 0
    assert m.upsert(turn())
    assert not m.upsert(turn())
    with pytest.raises(ValueError, match="increase revision"):
        m.upsert(turn("Different"))
    assert m.upsert(turn("Correction", 2))
    assert not m.upsert(turn("Stale", 1))
    assert not m.upsert(turn("Correction", 3))
    assert m.version == 2
    assert len(m.snapshot()["turns"]) == 1
    assert m.snapshot()["turns"][0]["text"] == "Correction"


async def test_idle_transcript_does_not_call_again(response):
    client = AsyncMock()
    client.evaluate.return_value = response
    m = Meeting("m", load_questions(), client)
    await m.tick()
    client.evaluate.assert_not_called()
    m.upsert(turn())
    await m.tick()
    await m.tick()
    assert client.evaluate.await_count == 1
    assert not m.view()["pending"]
    assert m.view()["coverage"] == 1


async def test_slow_request_coalesces_updates_and_reports_stale_result(response):
    entered, release = asyncio.Event(), asyncio.Event()
    snapshots = []

    async def evaluate(snapshot, questions):
        snapshots.append(copy.deepcopy(snapshot))
        entered.set()
        await release.wait()
        return response

    client = AsyncMock()
    client.evaluate.side_effect = evaluate
    m = Meeting("m", load_questions(), client)
    m.upsert(turn("v1"))
    task = asyncio.create_task(m.tick())
    await entered.wait()
    m.upsert(turn("v2", 2))
    m.upsert(turn("v3", 3))
    await m.tick()  # Must not issue a concurrent request.
    assert len(snapshots) == 1
    release.set()
    await task
    assert m.evaluated_version == 1 and m.view()["pending"]
    assert snapshots[0]["turns"][0]["text"] == "v1"
    await m.tick()
    assert [s["transcript_version"] for s in snapshots] == [1, 3]
    assert not m.view()["pending"]


async def test_retry_retains_last_result_and_uses_latest_snapshot(response):
    client = AsyncMock()
    client.evaluate.side_effect = [response, EvaluationError("rate limited", True, 10), response]
    m = Meeting("m", load_questions(), client)
    m.upsert(turn())
    await m.tick()
    m.upsert(turn("v2", 2))
    await m.tick()
    assert m.evaluated_version == 1 and m.result == response and m.error == "rate limited"
    await m.tick()
    assert client.evaluate.await_count == 2
    m.upsert(turn("v3", 3))
    m._next_attempt = 0
    await m.tick()
    assert m.evaluated_version == 3 and m.error is None


async def test_fatal_errors_stop_paid_retry_loop():
    client = AsyncMock()
    client.evaluate.side_effect = EvaluationError("HTTP 401")
    m = Meeting("m", load_questions(), client)
    m.upsert(turn())
    await m.tick()
    m.upsert(turn("changed", 2))
    await m.tick()
    assert m.blocked and client.evaluate.await_count == 1


async def test_close_cancels_inflight_work():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def evaluate(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = AsyncMock()
    client.evaluate.side_effect = evaluate
    m = Meeting("m", load_questions(), client, interval=0.01)
    m.upsert(turn())
    m.start()
    await asyncio.wait_for(entered.wait(), 1)
    await m.close()
    assert cancelled.is_set() and not m.in_flight


def test_transcript_order_and_recent_window():
    m = Meeting("m", load_questions("sentiment"), DemoEvaluator(), window_turns=2)
    for i in [3, 1, 2]:
        m.upsert(Turn(turn_id=f"t{i}", text=str(i), start_ms=i))
    assert [t["text"] for t in m.snapshot()["turns"]] == ["1", "2", "3"]
    assert [t["text"] for t in m.snapshot(True)["turns"]] == ["2", "3"]


@pytest.mark.parametrize(
    "kind",
    ["missing", "extra", "nan", "bool", "sum", "choice", "choice_type", "confidence", "usage"],
)
async def test_invalid_provider_results_rejected(response, kind):
    result = copy.deepcopy(response)
    a = result["answers"]["metrics"]
    if kind == "missing":
        del result["answers"]["metrics"]
    elif kind == "extra":
        result["answers"]["injected"] = a
    elif kind == "nan":
        a["probabilities"]["supported"] = float("nan")
    elif kind == "bool":
        a["confidence"] = True
    elif kind == "sum":
        a["probabilities"]["supported"] = 0.1
    elif kind == "choice":
        a["choice"] = "unknown"
    elif kind == "choice_type":
        a["choice"] = []
    elif kind == "confidence":
        a["confidence"] = 2
    elif kind == "usage":
        result["usage"]["input_tokens"] = -1
    with pytest.raises(EvaluationError):
        validate_result(result, load_questions())


async def test_direct_request_sends_one_shared_state(response):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    c = JevClient("test-key", transport=httpx.MockTransport(handler))
    try:
        await c.evaluate({"turns": demo_turns()}, load_questions())
    finally:
        await c.aclose()
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert len(payload["questions"]) == 8 and len(payload["state"]["turns"]) == 8
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert str(requests[0].url) == "https://api.typesafe.ai/v1/systemone"


@pytest.mark.parametrize("code,retryable", [(401, False), (422, False), (429, True), (529, True)])
async def test_http_errors_are_safe_and_categorized(code, retryable):
    c = JevClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                code, text="private transcript details", headers={"Retry-After": "7"}
            )
        ),
    )
    try:
        with pytest.raises(EvaluationError) as caught:
            await c.evaluate({}, load_questions())
        assert caught.value.retryable == retryable
        assert caught.value.retry_after == 7
        assert "private" not in str(caught.value)
    finally:
        await c.aclose()


def test_cost_cumulative_input_and_interval():
    value = estimate()
    assert value["calls"] == 900
    assert value["estimated_input_tokens"] == 4053000
    assert value["estimated_cost_usd"] == pytest.approx(0.170226)
    assert estimate(minutes=60)["estimated_cost_usd"] > 3 * value["estimated_cost_usd"]
    with pytest.raises(ValueError):
        estimate(interval=0)


def test_local_api_lifecycle_validation_and_origin():
    with TestClient(create_app(DemoEvaluator(), interval=100, mock=True)) as c:
        assert c.get("/").status_code == 200
        assert c.get("/app.js").status_code == 200
        assert "unsafe-inline" not in c.get("/").headers["content-security-policy"]
        docs = c.get("/docs")
        assert docs.status_code == 200
        assert "https://cdn.jsdelivr.net" in docs.headers["content-security-policy"]
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/api/config").json()["mock"] is True
        state = c.post("/api/meetings", json={}).json()
        assert state["framework"] == "meddpicc"
        mid = state["meeting_id"]
        endpoint = f"/api/meetings/{mid}"
        payload = demo_turns()[0]
        assert c.post(endpoint + "/turns", json=payload).json()["changed"]
        assert not c.post(endpoint + "/turns", json=payload).json()["changed"]
        assert c.post(endpoint + "/turns", json={**payload, "text": "conflict"}).status_code == 409
        assert c.post(endpoint + "/turns", json={**payload, "text": ""}).status_code == 422
        assert c.get(endpoint).json()["pending"]
        assert (
            c.post("/api/meetings", json={}, headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        assert c.get("/api/config", headers={"Host": "evil.example"}).status_code == 403
        assert c.delete(endpoint).status_code == 204
        assert c.get(endpoint).status_code == 404


async def test_unknown_confidence_does_not_create_coverage():
    client = DemoEvaluator()
    m = Meeting("m", load_questions(), client)
    m.upsert(turn("A greeting"))
    await m.tick()
    assert m.view()["coverage"] == 0
    assert m.view()["fields"]["metrics"]["confidence"] == 0.8
    s = Meeting("s", load_questions("sentiment"), client)
    s.upsert(turn())
    await s.tick()
    assert s.view()["coverage"] is None
