# Google Meet quick start

This example uses a **Recall meeting bot** to join Google Meet and produce live transcript events. The included adapter verifies and translates those events; Jev evaluates the accumulated text every two seconds. You need a Recall account, its API key and workspace verification secret, a public HTTPS tunnel, and a Meet call where the bot can be admitted. Bot capture/transcription is billed separately from Jev classification.

Google's [Meet Media API](https://developers.google.com/workspace/meet/media-api/guides/overview) is another possible source, but its documented Developer Preview restrictions require enrollment for the project, OAuth principal, and all participants. This repo does not implement that API. Its Google Meet route is explicitly through Recall, rather than treating Google's transcript artifacts API as a live caption feed. Provider references checked September 21, 2026.

## 1. Install and verify the standalone example

```bash
git clone https://github.com/A79-ai/jev-incall-assistance.git
cd jev-incall-assistance
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python scripts/smoke.py
```

The smoke check needs no accounts. It runs the real local HTTP/WebSocket servers and signed synthetic Recall payloads against a local stand-in for the Jev API, then exits after verifying the final scores. It proves the wiring, not classification, and it does not join a real meeting.

## 2. Start Jev and create the local meeting

In terminal 1:

```bash
export TYPESAFE_API_KEY='your-typesafe-api-key'
jev-incall serve
```

In terminal 2, activate the environment, then:

```bash
mkdir -p local
MEETING_ID=$(curl --fail --silent http://127.0.0.1:8000/api/meetings \
  -H 'Content-Type: application/json' -d '{"framework":"meddpicc"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["meeting_id"])')
printf '%s\n' "$MEETING_ID" > local/meeting-id.txt
printf 'Open http://127.0.0.1:8000/?meeting=%s\n' "$MEETING_ID"
```

Open that exact URL so the dashboard watches the gateway's meeting.

## 3. Set speaker roles and start the gateway

Create `local/roles.json`, mapping exact Meet display names to roles. Participant IDs also work and take precedence over names. For example, **replace** these example names with the people on your test call:

```json
{
  "Your display name": "seller",
  "Your test buyer's display name": "buyer"
}
```

Unmapped speakers become `participant`, never automatically `buyer`. MEDDPICC questions ask for buyer evidence, so this mapping matters. Names are convenient for a demo; use verified participant IDs in a real adapter. Restarting the gateway reloads the map without clearing the local meeting.

From the Recall dashboard's API keys page, get the **workspace verification secret**, which begins with `whsec_`. This is different from the API key used to create a bot. Then, still in terminal 2:

```bash
export RECALL_WEBHOOK_SECRET='whsec_your_workspace_verification_secret'
jev-incall gateway --meeting-id "$MEETING_ID" --roles local/roles.json
```

The gateway accepts finalized `transcript.data` events, verifies the signature over the raw body before parsing it, checks the timestamp is within five minutes, and requires the bot's `metadata.jev_meeting_id` to match this local meeting. Create that metadata with the command below. No generic ingestion token is needed when you enable only Recall delivery.

## 4. Expose the gateway

Install and authenticate [ngrok](https://ngrok.com/docs/getting-started/) using its setup guide. In terminal 3:

```bash
ngrok http 8001
```

Keep it running. Copy its HTTPS forwarding origin. Expose **8001**, which has only authenticated ingestion and a health check. Keep the dashboard and control API on **8000** private. Use a stable tunnel URL for longer calls; if it changes, the configured bot still points to the old URL.

## 5. Create the bot request, then send it

In terminal 4, activate the environment and load the saved local meeting ID:

```bash
MEETING_ID=$(cat local/meeting-id.txt)
export RECALL_API_KEY='your-recall-api-key'
# Match the region of your Recall account (for example us-west-2 or us-east-1).
export RECALL_REGION='us-west-2'

jev-incall meet-config \
  --meeting-url 'https://meet.google.com/abc-defg-hij' \
  --webhook-base 'https://your-domain.ngrok.app' \
  --meeting-id "$MEETING_ID" > local/meet-bot.json
```

Replace both URLs. `meet-config` only prints JSON; it makes no API calls. Inspect the generated file. It configures Recall's low-latency streaming transcription, speaker separation, bot metadata, and the `/webhooks/recall` destination. To send the bot into your call:

```bash
curl --fail-with-body --request POST \
  "https://$RECALL_REGION.recall.ai/api/v1/bot/" \
  -H "Authorization: $RECALL_API_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @local/meet-bot.json > local/bot-response.json

BOT_ID=$(python -c 'import json; print(json.load(open("local/bot-response.json"))["id"])')
printf 'Recall bot: %s\n' "$BOT_ID"
```

This step starts a real bot and can incur provider charges. Admit **Ampup in-call assistance** in Meet and let the participants know it is transcribing. Speak a test buyer statement such as “Our reporting takes 20 hours each week. We want to cut that in half.” The dashboard's transcript version should rise; an evaluated version and updated scores should follow. Scores are model results, so a particular label is not guaranteed.

## 6. Verify and stop

Watch state changes without the browser:

```bash
curl --no-buffer --fail "http://127.0.0.1:8000/api/meetings/$MEETING_ID/events"
```

A valid event is acknowledged as soon as the local transcript accepts it; the webhook does not wait for Jev. Duplicate finalized utterances are idempotent. The adapter derives stable IDs from transcript ID, participant ID, and utterance start time. A differing final at the same identity returns 409; this reference adapter does not invent a correction revision from delivery order. Partials are ignored. The generic Turn endpoint supports explicit revisions when your source supplies them.

Remove the bot when finished (Ctrl+C in the SSE terminal does not remove it):

```bash
curl --fail-with-body --request POST \
  "https://$RECALL_REGION.recall.ai/api/v1/bot/$BOT_ID/leave_call/" \
  -H "Authorization: $RECALL_API_KEY"
curl --fail -X DELETE "http://127.0.0.1:8000/api/meetings/$MEETING_ID"
```

Stop the gateway, tunnel, and dashboard with Ctrl+C in their terminals. Removing the bot does not delete media already stored by Recall; manage retention in that account.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No transcript updates | Bot admitted, correct Recall region, active transcription, tunnel still running, and gateway bound to the same local meeting. Inspect bot status in Recall. |
| Webhook 401 | Workspace verification secret, unmodified raw body, and system clock. Legacy accounts can have a different secret for dashboard/Svix events. |
| Webhook 403 | Bot metadata must contain the exact local `jev_meeting_id`. Re-create the bot config after a dashboard restart. |
| Webhook 404 | The target local meeting was deleted or the dashboard restarted. |
| Webhook 409 | Conflicting finalized utterance or meeting limits. Inspect the transcript and source event. |
| Webhook 422 | Unsupported provider envelope. This adapter accepts `transcript.data`, not arbitrary recording/status callbacks. |
| Transcript arrives, fields remain unknown | Speaker role map (unmapped speakers are `participant`, and MEDDPICC only counts buyer evidence) and whether the statements are explicit enough for the rubric. |
| Transcript arrives, scores stop updating | Dashboard's sanitized Jev error, API key, or model limits. |

Only real-time transcript delivery is configured here. Recall's separate artifact/status webhook subscriptions use another event schema; monitor the Recall dashboard for bot and transcription failures. The test suite covers signed fixtures and local delivery, not your account's Meet admission policy, billing, or a real provider call.

Provider references: [live transcription](https://docs.recall.ai/docs/bot-real-time-transcription), [signature verification](https://docs.recall.ai/docs/authenticating-requests-from-recallai), [create bot](https://docs.recall.ai/reference/bot_create), [leave call](https://docs.recall.ai/reference/bot_leave_call_create).
