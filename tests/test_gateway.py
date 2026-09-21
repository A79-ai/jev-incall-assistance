import base64
import copy
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jev_incall.demo import demo_turns
from jev_incall.gateway import create_gateway, local_api_url, recall_turn
from jev_incall.meet import bot_request

TOKEN = "integration-test-token-32-characters-long"
KEY = b"synthetic-recall-key-for-tests-only"
SECRET = "whsec_" + base64.b64encode(KEY).decode()


def signed(body, timestamp=None, msg_id="test-delivery"):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = base64.b64encode(
        hmac.digest(KEY, f"{msg_id}.{timestamp}.".encode() + body, "sha256")
    ).decode()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": "v1," + signature,
    }


def event(meeting_id="meeting"):
    return {
        "event": "transcript.data",
        "data": {
            "data": {
                "words": [{"text": demo_turns()[0]["text"], "start_timestamp": {"relative": 1.25}}],
                "participant": {"id": 101, "name": "Sample Buyer"},
            },
            "transcript": {"id": "sample-transcript"},
            "bot": {"id": "bot", "metadata": {"jev_meeting_id": meeting_id}},
        },
    }


@pytest.fixture
def gateway():
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={"changed": True, "transcript_version": len(forwarded)})

    app = create_gateway(
        "meeting",
        token=TOKEN,
        recall_secret=SECRET,
        roles={"101": "buyer"},
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        yield client, forwarded


def test_generic_webhook_and_auth(gateway):
    c, sent = gateway
    assert c.post("/webhooks/transcript", json=demo_turns()[0]).status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert c.post("/webhooks/transcript", json={"bad": True}, headers=auth).status_code == 422
    result = c.post("/webhooks/transcript", json=demo_turns()[0], headers=auth)
    assert result.status_code == 200 and result.json()["type"] == "ack"
    assert sent[0]["turn_id"] == "t001"
    assert c.get("/api/meetings").status_code == 404
    assert c.get("/").status_code == 404
    assert c.get("/docs").status_code == 404
    assert c.post("/webhooks/transcript", content=b"x" * 262145, headers=auth).status_code == 413


def test_websocket_validates_before_accept_and_acknowledges(gateway):
    c, sent = gateway
    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/stream"):
        pass
    with c.websocket_connect("/stream", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        ws.send_text("not json")
        assert ws.receive_json()["status"] == 422
        ws.send_json(demo_turns()[0])
        assert ws.receive_json()["turn_id"] == "t001"
    assert len(sent) == 1


def test_recall_verifies_body_age_binding_and_rotation(gateway):
    c, sent = gateway
    body = json.dumps(event()).encode()
    assert c.post("/webhooks/recall", content=body).status_code == 401
    assert c.post("/webhooks/recall", content=body + b" ", headers=signed(body)).status_code == 401
    assert (
        c.post(
            "/webhooks/recall", content=body, headers=signed(body, int(time.time()) - 301)
        ).status_code
        == 401
    )
    headers = signed(body)
    headers["webhook-signature"] = "v1,old-invalid-signature " + headers["webhook-signature"]
    assert c.post("/webhooks/recall", content=body, headers=headers).status_code == 200
    other = json.dumps(event("other-meeting")).encode()
    assert c.post("/webhooks/recall", content=other, headers=signed(other)).status_code == 403
    assert len(sent) == 1 and sent[0]["speaker_role"] == "buyer" and sent[0]["start_ms"] == 1250
    partial = event()
    partial["event"] = "transcript.partial_data"
    raw = json.dumps(partial).encode()
    assert (
        c.post("/webhooks/recall", content=raw, headers=signed(raw)).json()["ignored"] == "interim"
    )
    assert len(sent) == 1


def test_recall_identity_and_no_guessed_buyer():
    first = recall_turn(event(), "meeting", {})
    second = recall_turn(copy.deepcopy(event()), "meeting", {})
    assert first == second and first.speaker_role == "participant"
    assert first.turn_id.startswith("recall_")


@pytest.mark.parametrize("payload", [None, {}, {"event": "transcript.data", "data": []}])
def test_bad_signed_events_do_not_forward(gateway, payload):
    c, sent = gateway
    raw = json.dumps(payload).encode()
    assert c.post("/webhooks/recall", content=raw, headers=signed(raw)).status_code == 422
    assert not sent


def test_configuration_fails_closed():
    with pytest.raises(ValueError):
        create_gateway("meeting")
    with pytest.raises(ValueError):
        create_gateway("meeting", token="short")
    with pytest.raises(ValueError):
        create_gateway("meeting", recall_secret="bad")
    with pytest.raises(ValueError):
        local_api_url("https://remote.example")


def test_unavailable_target_is_not_acknowledged():
    app = create_gateway(
        "meeting", token=TOKEN, transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )
    with TestClient(app) as c:
        assert (
            c.post(
                "/webhooks/transcript",
                json=demo_turns()[0],
                headers={"Authorization": f"Bearer {TOKEN}"},
            ).status_code
            == 503
        )


def test_meet_request_is_bound_to_one_target():
    body = bot_request(
        "https://meet.google.com/abc-defg-hij", "https://example.ngrok.app", "meeting"
    )
    assert body["metadata"] == {"jev_meeting_id": "meeting"}
    assert (
        body["recording_config"]["realtime_endpoints"][0]["url"]
        == "https://example.ngrok.app/webhooks/recall"
    )
    with pytest.raises(ValueError):
        bot_request("https://evil.example/", "https://example.ngrok.app", "meeting")
