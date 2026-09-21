# Connect a transcript source

For complete runnable integrations, see the [webhook/WebSocket quick start](streaming-and-webhooks.md) and [Google Meet quick start](google-meet.md). The routes below are the private local control API.

Use any speech-to-text provider that supplies finalized text and speaker information. The adapter translates its events into the `Turn` schema; the repo does not depend on a particular audio SDK.

Start `jev-incall serve --mock` for integration development or `jev-incall serve` for real classification. On macOS/Linux:

```bash
MEETING_ID=$(curl --fail --silent http://127.0.0.1:8000/api/meetings \
  -H 'Content-Type: application/json' -d '{"framework":"meddpicc"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["meeting_id"])')

curl --fail http://127.0.0.1:8000/api/meetings/$MEETING_ID/turns \
  -H 'Content-Type: application/json' \
  -d '{"turn_id":"t001","speaker_role":"buyer","start_ms":0,"text":"Our reporting takes 20 hours each week. We want to cut that in half.","revision":1,"final":true}'

curl --fail http://127.0.0.1:8000/api/meetings/$MEETING_ID
```

The POST acknowledges ingestion immediately. Classification is asynchronous; poll the GET until `evaluated_version` catches up with `transcript_version`.

Correct the same turn with a higher revision:

```bash
curl --fail http://127.0.0.1:8000/api/meetings/$MEETING_ID/turns \
  -H 'Content-Type: application/json' --data-binary @examples/correction.json
```

End the meeting:

```bash
curl --fail -X DELETE http://127.0.0.1:8000/api/meetings/$MEETING_ID
```

## Event fields

| Field | Contract |
| --- | --- |
| `turn_id` | Stable ID, 1–80 letters, digits, underscores, or hyphens. |
| `speaker_role` | `buyer`, `seller`, or `participant`; defaults to buyer. Set it explicitly in a real adapter. |
| `start_ms` | Nonnegative integer offset from call start. Used for ordering. |
| `text` | Final transcript text, 1–12,000 characters. |
| `revision` | Positive integer that increases when the provider corrects this turn. Defaults to 1. |
| `final` | Boolean; false means ignored interim text. Defaults to true. |

Unknown fields are rejected. Duplicate finalized events are safe to resend. Earlier revisions arriving late are ignored. To retract a statement, send its corrected text with a new revision; there is no delete-turn operation in this reference implementation.

The OpenAPI contract is served at `/openapi.json`; interactive docs at `/docs` may require an internet connection for FastAPI's documentation assets. The dashboard itself is fully local.

For a file replay, put one event per line, following `examples/meeting.jsonl`. File order is arrival order; the engine sorts stored turns by their call offset. `--turn-delay` controls replay speed and does not infer delays from `start_ms`.
