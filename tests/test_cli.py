import json
import os
import subprocess
import sys
from pathlib import Path

from fake_jev import FakeJev

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env=None):
    env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"} | (env or {})
    return subprocess.run(
        [sys.executable, "-m", "jev_incall.cli", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_dry_run_does_not_require_credentials():
    r = run("evaluate", "examples/snapshot.json", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)["questions"]) == 8


def test_missing_key_is_actionable():
    r = run("evaluate", "examples/snapshot.json")
    assert r.returncode == 1 and "TYPESAFE_API_KEY" in r.stderr


def test_missing_key_message_points_at_the_provider():
    r = run("evaluate", "examples/snapshot.json")
    assert "docs.typesafe.ai" in r.stderr and "--mock" not in r.stderr


def test_no_mock_flag():
    r = run("serve", "--mock")
    assert r.returncode == 2 and "unrecognized arguments: --mock" in r.stderr


def test_evaluate_against_local_stand_in():
    with FakeJev() as jev:
        r = run("evaluate", "examples/snapshot.json", env=jev.env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["answers"]["metrics"]["choice"] == "supported"


def test_wrong_key_is_reported_without_leaking_the_response():
    with FakeJev() as jev:
        r = run("evaluate", "examples/snapshot.json", env={**jev.env, "TYPESAFE_API_KEY": "nope"})
    assert r.returncode == 1 and "HTTP 401" in r.stderr and "invalid key" not in r.stderr


def test_replay_finishes_last_version():
    with FakeJev() as jev:
        r = run(
            "replay",
            "examples/meeting.jsonl",
            "--turn-delay",
            "0.01",
            "--interval",
            "0.02",
            env=jev.env,
        )
    assert r.returncode == 0, r.stderr
    decoder = json.JSONDecoder()
    rest, records = r.stdout.strip(), []
    while rest:
        record, offset = decoder.raw_decode(rest)
        records.append(record)
        rest = rest[offset:].lstrip()
    assert records[-1]["evaluated_version"] == 8
    assert records[-1]["coverage"] == 1


def test_cost_command():
    r = run("cost")
    assert r.returncode == 0
    assert json.loads(r.stdout)["calls"] == 900


def test_meet_config_without_accounts():
    r = run(
        "meet-config",
        "--meeting-url",
        "https://meet.google.com/abc-defg-hij",
        "--webhook-base",
        "https://example.ngrok.app",
        "--meeting-id",
        "test-meeting",
    )
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout)
    assert body["metadata"]["jev_meeting_id"] == "test-meeting"
    assert body["recording_config"]["realtime_endpoints"][0]["events"] == ["transcript.data"]
