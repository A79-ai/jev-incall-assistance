"""Offline UI fixture. This is deliberately not a replacement language classifier."""

import asyncio
import json
from importlib.resources import files

from .client import validate_result


def demo_turns():
    return json.loads(files("jev_incall").joinpath("data/demo.json").read_text())


class DemoEvaluator:
    model = "scripted-demo-not-jev"

    async def evaluate(self, snapshot, questions):
        await asyncio.sleep(0.05)
        mapping = json.loads(files("jev_incall").joinpath("data/demo-labels.json").read_text())
        established = {mapping[t["text"]] for t in snapshot["turns"] if t["text"] in mapping}
        answers = {}
        for key, q in questions.items():
            labels = list(q["criteria"])
            default = (
                "unknown"
                if "unknown" in labels
                else "neutral"
                if "neutral" in labels
                else labels[0]
            )
            choice = "supported" if key in established and "supported" in labels else default
            probabilities = {
                label: 0.96 if label == choice else 0.04 / (len(labels) - 1) for label in labels
            }
            answers[key] = {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities,
                "confidence": 0.8,
            }
        return validate_result(
            {
                "model": self.model,
                "answers": answers,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
            questions,
        )

    async def aclose(self):
        pass
