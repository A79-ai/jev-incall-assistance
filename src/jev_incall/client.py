"""Direct TypeSafe API adapter. One HTTP attempt; the live worker controls retries."""

import math
from typing import Any

import httpx

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"


class EvaluationError(Exception):
    def __init__(self, message: str, retryable: bool = False, retry_after: float = 0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def validate_result(result: Any, questions: dict) -> dict:
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise EvaluationError("Provider returned an invalid answer map")
    if set(result["answers"]) != set(questions):
        raise EvaluationError("Provider returned missing or unexpected question IDs")
    for key, q in questions.items():
        a = result["answers"][key]
        if not isinstance(a, dict) or a.get("type") != "choice":
            raise EvaluationError(f"Invalid answer type for {key}")
        probs = a.get("probabilities")
        if not isinstance(probs, dict) or set(probs) != set(q["criteria"]):
            raise EvaluationError(f"Invalid probability keys for {key}")
        values = [*probs.values(), a.get("confidence")]
        if any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values
        ):
            raise EvaluationError(f"Invalid probability or confidence for {key}")
        if abs(sum(probs.values()) - 1) > 0.001:
            raise EvaluationError(f"Probabilities do not sum to one for {key}")
        choice = a.get("choice")
        if (
            not isinstance(choice, str)
            or choice not in probs
            or probs[choice] < max(probs.values()) - 1e-6
        ):
            raise EvaluationError(f"Choice is not a highest-probability label for {key}")
    usage = result.get("usage", {})
    if not isinstance(usage, dict) or any(
        type(usage.get(k, 0)) is not int or usage.get(k, 0) < 0
        for k in ("input_tokens", "output_tokens")
    ):
        raise EvaluationError("Invalid usage metadata")
    return result


class JevClient:
    def __init__(
        self, api_key: str, model: str = DEFAULT_MODEL, transport=None, endpoint: str | None = None
    ):
        if not api_key.strip():
            raise ValueError(
                "TYPESAFE_API_KEY is not set. Get a key at https://docs.typesafe.ai/ and export "
                "it. There is no offline mode; `evaluate --dry-run` prints a request without "
                "calling Jev."
            )
        self.model = model
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def evaluate(self, snapshot: dict, questions: dict) -> dict:
        try:
            response = await self.http.post(
                self.endpoint, json={"model": self.model, "state": snapshot, "questions": questions}
            )
        except httpx.RequestError as exc:
            # Do not return raw errors, request bodies, or credentials to browsers/logs.
            raise EvaluationError("Jev connection failed or timed out", retryable=True) from exc
        if response.is_error:
            delay = 0.0
            try:
                delay = float(response.headers.get("retry-after", "0"))
                if not math.isfinite(delay):
                    delay = 0.0
            except ValueError:
                pass
            raise EvaluationError(
                f"Jev returned HTTP {response.status_code}",
                retryable=response.status_code in (408, 429) or response.status_code >= 500,
                retry_after=max(0.0, delay),
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise EvaluationError("Jev returned invalid JSON") from exc
        return validate_result(result, questions)

    async def aclose(self):
        await self.http.aclose()
