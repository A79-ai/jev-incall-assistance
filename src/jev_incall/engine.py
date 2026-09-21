"""One worker per meeting. New transcript versions replace pending work."""

import asyncio
import copy
import math
import time
from datetime import UTC, datetime

from .client import EvaluationError
from .models import Turn

MAX_TRANSCRIPT_CHARS = 80000  # A payload guard, not a tokenizer/context guarantee.


class Meeting:
    def __init__(
        self,
        meeting_id,
        questions,
        evaluator,
        interval=2.0,
        retry_base=2.0,
        window_turns=None,
        framework=None,
    ):
        self.meeting_id = meeting_id
        self.framework = framework
        self.questions = copy.deepcopy(questions)
        self.evaluator = evaluator
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval must be positive")
        self.window_turns = window_turns
        self.interval = interval
        self.retry_base = retry_base
        self.turns: dict[str, Turn] = {}
        self.version = 0
        self.evaluated_version = 0
        self.result = None
        self.error = None
        self.blocked = False
        self.in_flight = False
        self.attempts = 0
        self.successes = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.updated_at = None
        self.latency_ms = None
        self._next_attempt = 0.0
        self._failures = 0
        self._task = None
        self._lock = asyncio.Lock()

    def upsert(self, turn: Turn) -> bool:
        if not turn.final:
            return False
        previous = self.turns.get(turn.turn_id)
        if previous and turn.revision <= previous.revision:
            if turn.revision == previous.revision and turn != previous:
                raise ValueError("Correction must increase revision for the same turn_id")
            return False
        if previous is None and len(self.turns) >= 2000:
            raise ValueError("Meeting has reached the 2000-turn limit")
        size = sum(len(t.text) for t in self.turns.values())
        size += len(turn.text) - (len(previous.text) if previous else 0)
        if size > MAX_TRANSCRIPT_CHARS:
            raise ValueError("Transcript exceeds the local 80000-character guard")
        self.turns[turn.turn_id] = turn.model_copy(deep=True)
        # Revision-only acknowledgements do not trigger another paid request.
        changed = not previous or (
            previous.model_dump(exclude={"revision"}) != turn.model_dump(exclude={"revision"})
        )
        if changed:
            self.version += 1
        return changed

    def snapshot(self, for_evaluation=False) -> dict:
        turns = sorted(self.turns.values(), key=lambda t: (t.start_ms, t.turn_id))
        if for_evaluation and self.window_turns:
            turns = turns[-self.window_turns :]
        return {
            "meeting_id": self.meeting_id,
            "transcript_version": self.version,
            "turns": [t.model_dump() for t in turns],
        }

    async def tick(self):
        if self._lock.locked():
            return
        async with self._lock:
            if self.blocked or not self.version or self.version == self.evaluated_version:
                return
            if time.monotonic() < self._next_attempt:
                return
            snapshot = self.snapshot(for_evaluation=True)  # Immutable copy for this evaluation.
            version = self.version
            self.attempts += 1
            self.in_flight = True
            started = time.monotonic()
            try:
                result = await self.evaluator.evaluate(snapshot, self.questions)
                self.result = copy.deepcopy(result)
                self.evaluated_version = version
                self.successes += 1
                self.input_tokens += result.get("usage", {}).get("input_tokens", 0)
                self.output_tokens += result.get("usage", {}).get("output_tokens", 0)
                self.updated_at = datetime.now(UTC).isoformat()
                self.latency_ms = round((time.monotonic() - started) * 1000)
                self.error = None
                self._failures = 0
                self._next_attempt = 0
            except EvaluationError as exc:
                self.error = str(exc)
                self.blocked = not exc.retryable
                self._failures += 1
                delay = max(
                    exc.retry_after, min(30, self.retry_base * 2 ** min(self._failures - 1, 5))
                )
                self._next_attempt = time.monotonic() + delay
            except Exception:  # noqa: BLE001 - isolate adapter failures from the background worker
                self.error = (
                    "Unexpected evaluator error; restart the meeting after checking the adapter"
                )
                self.blocked = True
            finally:
                self.in_flight = False

    async def run(self):
        next_tick = time.monotonic() + self.interval
        while True:
            await asyncio.sleep(max(0, next_tick - time.monotonic()))
            # Awaiting tick ensures one request in flight and no queued snapshots.
            await self.tick()
            missed = max(1, math.floor((time.monotonic() - next_tick) / self.interval) + 1)
            next_tick += missed * self.interval

    def start(self):
        self._task = asyncio.create_task(self.run())

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def view(self):
        answers = self.result["answers"] if self.result else {}
        fields = {
            key: {
                "label": a["choice"],
                "confidence": a["confidence"],
                "probabilities": a["probabilities"],
                "support_score": a["probabilities"].get("supported"),
            }
            for key, a in answers.items()
        }
        qualification = all("supported" in q["criteria"] for q in self.questions.values())
        supported = sum(a["choice"] == "supported" for a in answers.values())
        return {
            "meeting_id": self.meeting_id,
            "transcript_version": self.version,
            "evaluated_version": self.evaluated_version,
            "pending": self.version > self.evaluated_version,
            "in_flight": self.in_flight,
            "blocked": self.blocked,
            "error": self.error,
            "updated_at": self.updated_at,
            "latency_ms": self.latency_ms,
            "fields": fields,
            "field_names": list(self.questions),
            "framework": self.framework,
            "coverage": supported / len(self.questions) if answers and qualification else None,
            "attempts": self.attempts,
            "successful_evaluations": self.successes,
            "model": self.result.get("model") if self.result else None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
            "turns": self.snapshot()["turns"],
        }
