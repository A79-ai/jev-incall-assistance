"""Create a reviewable Recall bot request for a Google Meet call."""

from urllib.parse import urlparse

from pydantic import TypeAdapter

from .models import Identifier


def bot_request(meeting_url: str, webhook_base: str, meeting_id: str):
    meet = urlparse(meeting_url)
    destination = urlparse(webhook_base)
    if (
        meet.scheme != "https"
        or meet.netloc != "meet.google.com"
        or not meet.path.strip("/")
        or meet.fragment
    ):
        raise ValueError("Expected an https://meet.google.com/... meeting URL")
    if (
        destination.scheme != "https"
        or not destination.hostname
        or destination.username
        or destination.password
        or destination.query
        or destination.fragment
        or destination.path not in ("", "/")
    ):
        raise ValueError("Webhook base must be a public HTTPS origin")
    TypeAdapter(Identifier).validate_python(meeting_id)
    return {
        "meeting_url": meeting_url,
        "bot_name": "Ampup in-call assistance",
        "metadata": {"jev_meeting_id": meeting_id},
        "recording_config": {
            "transcript": {
                "provider": {
                    "recallai_streaming": {"mode": "prioritize_low_latency", "language_code": "en"}
                },
                "diarization": {"use_separate_streams_when_available": True},
            },
            "realtime_endpoints": [
                {
                    "type": "webhook",
                    "url": webhook_base.rstrip("/") + "/webhooks/recall",
                    "events": ["transcript.data"],
                }
            ],
        },
    }
