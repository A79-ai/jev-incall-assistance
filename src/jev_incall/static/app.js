const $ = (id) => document.getElementById(id);
let meetingId = null, config, replayToken = 0, busy = false, pollTimer;
const pct = (v) => v == null ? "—" : `${Math.round(v * 100)}%`;
const name = (key) => key.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
async function api(path, method = "GET", body) {
  const res = await fetch(path, {method, headers: body ? {"Content-Type": "application/json"} : {}, body: body ? JSON.stringify(body) : undefined});
  if (!res.ok) { const error = await res.json(); throw new Error(typeof error.detail === "string" ? error.detail : "Invalid request"); }
  return res.status === 204 ? null : res.json();
}
function node(tag, text, cls) { const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el; }
function render(state) {
  $("coverage").textContent = pct(state.coverage);
  $("version").textContent = `${state.transcript_version} / ${state.evaluated_version}`;
  $("attempts").textContent = state.attempts;
  $("latency").textContent = state.latency_ms == null ? "—" : `${state.latency_ms} ms`;
  $("status").textContent = state.blocked ? "Blocked · start a new meeting after fixing the error" : state.error ? "Retry scheduled · last result retained" : state.pending ? "Catching up · showing last evaluated version" : state.evaluated_version ? "Up to date" : "Waiting for transcript";
  $("error").textContent = state.error || "";
  $("meeting-id").textContent = state.meeting_id;
  $("fields").replaceChildren();
  for (const key of state.field_names) {
    const field = state.fields[key];
    const label = field?.label || "waiting";
    const card = node("article", null, `field ${label}`);
    card.append(node("h3", name(key)), node("div", label, "label"));
    if (field?.support_score != null) {
      card.append(node("p", `Support score ${pct(field.support_score)}`));
      const bar = node("progress"); bar.max = 1; bar.value = field.support_score; bar.setAttribute("aria-label", `${name(key)} support score`); card.append(bar);
    }
    card.append(node("p", `Confidence ${pct(field?.confidence)}`));
    $("fields").append(card);
  }
  $("turns").replaceChildren();
  for (const turn of state.turns) {
    const row = node("p"); row.append(node("b", `${turn.speaker_role.toUpperCase()} · ${turn.turn_id} · revision ${turn.revision}`), node("span", turn.text)); $("turns").append(row);
  }
  if (!state.turns.length) $("turns").append(node("p", "No transcript yet."));
}
async function newMeeting() {
  if (busy) return; busy = true; replayToken++;
  try {
    const old = meetingId; meetingId = null;
    if (old) await api(`/api/meetings/${old}`, "DELETE");
    const state = await api("/api/meetings", "POST", {framework: $("framework").value});
    meetingId = state.meeting_id; render(state);
  } finally { busy = false; }
}
async function poll() {
  const id = meetingId;
  try { if (id) { const state = await api(`/api/meetings/${id}`); if (id === meetingId) render(state); } }
  catch (error) { $("error").textContent = error.message; }
  pollTimer = setTimeout(poll, 1000);
}
function action(fn) { return async (event) => { try { await fn(event); } catch (error) { $("error").textContent = error.message; } }; }
$("new").onclick = action(newMeeting);
$("framework").onchange = action(newMeeting);
$("close").onclick = action(async () => {
  replayToken++;
  const id = meetingId; meetingId = null;
  if (id) await api(`/api/meetings/${id}`, "DELETE");
  $("status").textContent = "Meeting closed · no further evaluations";
});
$("turn-form").onsubmit = action(async (event) => {
  event.preventDefault();
  if (!meetingId) await newMeeting();
  const text = $("text").value.trim(); if (!text) return;
  await api(`/api/meetings/${meetingId}/turns`, "POST", {turn_id: crypto.randomUUID(), speaker_role: $("role").value, start_ms: Date.now(), text, revision: 1, final: true});
  $("text").value = "";
});
$("demo").onclick = action(async () => {
  $("framework").value = "meddpicc"; await newMeeting();
  const token = ++replayToken, id = meetingId;
  const turns = await api("/api/demo");
  for (const turn of turns) {
    if (token !== replayToken || id !== meetingId) return;
    await api(`/api/meetings/${id}/turns`, "POST", turn);
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
});
async function init() {
  config = await api("/api/config");
  $("mode").textContent = config.mock ? "SCRIPTED DEMO · NO API CALLS" : `LIVE · ${config.model}`;
  $("notice").textContent = config.mock ? "Sample replay uses canned scores for exact sample sentences. Custom text is not classified in demo mode. No transcript leaves this server." : `Live mode sends transcript text to TypeSafe. Evaluation tick: ${config.interval_seconds}s. Sample replay also makes paid API calls.`;
  await newMeeting(); await poll();
}
init().catch((error) => { $("error").textContent = error.message; });
window.addEventListener("pagehide", () => clearTimeout(pollTimer));
