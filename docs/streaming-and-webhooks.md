# Streaming and webhook quick start

The standalone example runs two small processes: the dashboard and Jev worker on **8000**, and an authenticated ingestion gateway on **8001**. A gateway is bound to one meeting. It cannot create meetings or read transcript history. Your transcription service supplies text; these endpoints do not accept audio.

## Verify the wiring locally, without accounts

After the README installation steps:

```bash
python scripts/smoke.py
```

This starts both servers on available loopback ports, runs the actual webhook and WebSocket CLI senders, delivers signed Recall fixtures, verifies final scores through SSE, tests duplicate delivery, and shuts everything down. Expected output starts with `PASS:`. The script stands in for the Jev API with a local server and uses synthetic Recall events, so it verifies wiring rather than classification or a real meeting bot.

## Start the dashboard

In terminal 1, activate the environment and run:

```bash
export TYPESAFE_API_KEY='your-typesafe-api-key'
jev-incall serve
```

Every evaluation from here on is a real Jev call.

## Bind a gateway to a meeting

In terminal 2, activate the same environment. Create a meeting, save its ID and a random ingestion token to an ignored local file, then start the gateway:

```bash
mkdir -p local
python - <<'PY'
import httpx, json, secrets
from pathlib import Path
r = httpx.post('http://127.0.0.1:8000/api/meetings', json={'framework': 'meddpicc'})
r.raise_for_status()
meeting_id = r.json()['meeting_id']
config = {'MEETING_ID': meeting_id, 'JEV_INGEST_TOKEN': secrets.token_urlsafe(32)}
path = Path('local/demo.env')
path.touch(mode=0o600, exist_ok=True)
path.chmod(0o600)
path.write_text(''.join(f'export {k}={v}\n' for k, v in config.items()))
print(f'Open http://127.0.0.1:8000/?meeting={meeting_id}')
PY
source local/demo.env
jev-incall gateway --meeting-id "$MEETING_ID"
```

Open the printed dashboard URL. It attaches to this meeting instead of creating a new one.

## Path A: webhook

In terminal 3, activate the environment and load the same local configuration:

```bash
source local/demo.env
jev-incall send examples/meeting.jsonl --transport webhook
```

Or send a single event with your own HTTP client:

```bash
curl --fail-with-body http://127.0.0.1:8001/webhooks/transcript \
  -H "Authorization: Bearer $JEV_INGEST_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"turn_id":"t001","speaker_role":"buyer","start_ms":0,"text":"Our reporting takes 20 hours each week. We want to cut that in half.","revision":1,"final":true}'
```

A successful response is an ingestion acknowledgement:

```json
{"type":"ack","turn_id":"t001","changed":true,"transcript_version":1}
```

This confirms the local meeting accepted the event; Jev runs on its next two-second tick. An identical retry returns `changed: false`. A correction must increase `revision`; conflicting content at the same revision returns 409. Interim turns are acknowledged without changing the transcript. Requests larger than 256 KiB are rejected; the smaller per-turn limits still apply.

## Path B: WebSocket input

Using the same meeting and token:

```bash
jev-incall send examples/meeting.jsonl --transport websocket
# Or stream newline-delimited JSON from another process:
cat examples/meeting.jsonl | jev-incall send - --transport websocket --delay 0.01
```

The endpoint is `ws://127.0.0.1:8001/stream`. Send the same `Authorization: Bearer ...` header on the upgrade request. Each text frame contains one Turn object and receives one acknowledgement. Invalid JSON produces `{"type":"error","status":422,...}`; binary frames are rejected. If a connection drops, reconnect and resend unacknowledged events using the same IDs and revisions. The included sender exits on a transport failure rather than hiding loss; replaying the file is safe while the meeting remains in memory.

Browser WebSocket APIs cannot set this Authorization header. Use a server-side adapter (the Python sender is included), not a token in a browser URL.

## Path C: stream scores out

```bash
source local/demo.env
curl --no-buffer --fail "http://127.0.0.1:8000/api/meetings/$MEETING_ID/events"
```

This is a server-sent event (SSE) feed. `event: state` carries the full meeting state, including scores, confidence, versions, and transcript. An initial snapshot is sent immediately, then changes are sampled every 250 ms. This sampling does not trigger additional model calls. Idle connections get keepalives, and deleting the meeting sends `event: closed` and ends the stream. Reconnecting sends the latest snapshot; it does not replay an event log or honor `Last-Event-ID`.

The example dashboard uses one-second polling. Use this SSE endpoint for your own live consumer.

## Shutdown and external senders

Stop the gateway with Ctrl+C, then delete the meeting:

```bash
curl --fail -X DELETE "http://127.0.0.1:8000/api/meetings/$MEETING_ID"
```

The state is in memory. Restarting the dashboard loses it; create a new meeting, restart the gateway with its ID, and update external senders. Restarting only the gateway preserves the meeting and duplicate protection.

For an external service, forward **port 8001 only** through an HTTPS tunnel. Use HTTPS/WSS and the same token; `send --url` accepts those URLs. Port 8000 contains unauthenticated local controls and transcript reads and must remain private. The gateway has no dashboard, transcript read, or meeting administration routes. It acknowledges only after the local store accepts an event, and returns 503 when that store is unavailable; your provider must retry. It does not persist an ingestion queue across machine failure.

The [Google Meet quick start](google-meet.md) uses the separately signed Recall endpoint on the same gateway.
