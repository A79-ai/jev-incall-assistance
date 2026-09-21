import json
from importlib.resources import files
from pathlib import Path
from typing import Any

FRAMEWORKS = ("meddpicc", "bant", "sentiment")


def load_questions(name: str = "meddpicc", path: Path | None = None) -> dict[str, Any]:
    if path is None and name not in FRAMEWORKS:
        raise ValueError(f"Unknown framework: {name}")
    raw = (
        path.read_text()
        if path
        else files("jev_incall").joinpath(f"prompts/{name}.json").read_text()
    )
    questions = json.loads(raw)
    validate_questions(questions)
    return questions


def validate_questions(questions: Any) -> None:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 32:
        raise ValueError("Expected a map of 1 to 32 choice questions")
    for key, q in questions.items():
        if not isinstance(q, dict) or q.get("type") != "choice":
            raise ValueError(f"{key}: this reference implementation supports choice questions")
        if not isinstance(q.get("instructions"), str) or not q["instructions"].strip():
            raise ValueError(f"{key}: instructions must contain the full question")
        criteria = q.get("criteria")
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
            raise ValueError(f"{key}: expected 2 to 255 named criteria")
        if any(not isinstance(v, str) or not v for v in criteria.values()):
            raise ValueError(f"{key}: every criterion needs a description")
