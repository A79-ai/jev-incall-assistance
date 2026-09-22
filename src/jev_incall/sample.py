"""The bundled sample call: eight buyer turns that establish every MEDDPICC field."""

import json
from importlib.resources import files


def sample_turns():
    return json.loads(files("jev_incall").joinpath("data/sample-call.json").read_text())
