import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path

from .client import DEFAULT_MODEL, EvaluationError, JevClient
from .cost import estimate
from .engine import Meeting
from .models import Snapshot, Turn
from .questions import FRAMEWORKS, load_questions


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def evaluator(args):
    return JevClient(
        os.environ.get("TYPESAFE_API_KEY", ""),
        args.model,
        endpoint=os.environ.get("TYPESAFE_API_URL") or None,
    )


def print_json(value):
    print(json.dumps(value, indent=2, allow_nan=False))


async def evaluate_once(args):
    snapshot = Snapshot.model_validate_json(args.snapshot.read_text())
    if not snapshot.turns:
        raise ValueError("Snapshot must contain finalized turns")
    if any(not t.final for t in snapshot.turns):
        raise ValueError("Snapshot must contain finalized turns only")
    if len({t.turn_id for t in snapshot.turns}) != len(snapshot.turns):
        raise ValueError("Snapshot contains duplicate turn IDs")
    questions = load_questions(args.framework, args.questions)
    payload = {"model": args.model, "state": snapshot.model_dump(), "questions": questions}
    if args.dry_run:
        print_json(payload)
        return
    client = evaluator(args)
    try:
        print_json(await client.evaluate(payload["state"], questions))
    finally:
        await client.aclose()


async def replay(args):
    # Validate the complete file before creating a client or issuing paid requests.
    turns = [
        Turn.model_validate_json(line)
        for line in args.input.read_text().splitlines()
        if line.strip()
    ]
    questions = load_questions(args.framework, args.questions)
    client = evaluator(args)
    meeting = Meeting(
        "replay",
        questions,
        client,
        interval=args.interval,
        window_turns=12 if args.framework == "sentiment" else None,
    )
    last_version = -1
    try:
        meeting.start()
        for turn in turns:
            meeting.upsert(turn)
            await asyncio.sleep(args.turn_delay)
            if meeting.evaluated_version != last_version and meeting.result:
                print_json(meeting.view())
                last_version = meeting.evaluated_version
        # Wait for the final snapshot, bounded so a rate limit cannot hang a replay.
        deadline = asyncio.get_running_loop().time() + args.finish_timeout
        while meeting.evaluated_version < meeting.version:
            if meeting.blocked:
                raise EvaluationError(meeting.error)
            if asyncio.get_running_loop().time() >= deadline:
                raise EvaluationError("Timed out waiting for final evaluation")
            await asyncio.sleep(0.05)
        print_json(meeting.view())
    finally:
        await meeting.close()
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(description="Live in-call assistance using Jev")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Local dashboard and transcript ingestion API")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--interval", type=positive, default=2.0)
    once = sub.add_parser("evaluate", help="Evaluate one complete transcript snapshot")
    once.add_argument("snapshot", type=Path)
    once.add_argument(
        "--dry-run", action="store_true", help="Print the exact request; no key, no network"
    )
    play = sub.add_parser("replay", help="Feed JSONL transcript events into the live loop")
    play.add_argument("input", type=Path)
    play.add_argument("--interval", type=positive, default=2.0)
    play.add_argument("--turn-delay", type=positive, default=1.0)
    play.add_argument("--finish-timeout", type=positive, default=90.0)
    for cmd in (serve, once, play):
        cmd.add_argument("--model", default=os.environ.get("JEV_MODEL", DEFAULT_MODEL))
    for cmd in (once, play):
        cmd.add_argument("--framework", choices=FRAMEWORKS, default="meddpicc")
        cmd.add_argument("--questions", type=Path, help="Custom choice-question JSON map")
    gateway = sub.add_parser(
        "gateway", help="Authenticated webhook/WebSocket ingress for one meeting"
    )
    gateway.add_argument("--meeting-id", required=True)
    gateway.add_argument("--api-url", default="http://127.0.0.1:8000")
    gateway.add_argument("--port", type=int, default=8001)
    gateway.add_argument("--roles", type=Path, help="Recall participant ID/name to role JSON map")
    send = sub.add_parser("send", help="Send JSONL events to a running gateway; use - for stdin")
    send.add_argument("input")
    send.add_argument("--transport", choices=("webhook", "websocket"), default="webhook")
    send.add_argument("--url", help="Defaults to the matching endpoint on 127.0.0.1:8001")
    send.add_argument("--delay", type=positive, default=1.0)
    meet = sub.add_parser("meet-config", help="Print a Google Meet bot request; makes no API calls")
    meet.add_argument("--meeting-url", required=True)
    meet.add_argument("--webhook-base", required=True)
    meet.add_argument("--meeting-id", required=True)
    cost = sub.add_parser("cost", help="Estimate cumulative transcript input cost")
    cost.add_argument("--minutes", type=positive, default=30.0)
    cost.add_argument("--interval", type=positive, default=2.0)
    cost.add_argument("--words-per-minute", type=positive, default=150.0)
    cost.add_argument("--fixed-tokens", type=int, default=1500)
    cost.add_argument("--price-per-million", type=float, default=0.042)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            import uvicorn

            from .server import create_app

            uvicorn.run(
                create_app(evaluator(args), args.interval),
                host="127.0.0.1",
                port=args.port,
            )
        elif args.command == "gateway":
            import uvicorn

            from .gateway import create_gateway

            app = create_gateway(
                args.meeting_id,
                api_url=args.api_url,
                token=os.environ.get("JEV_INGEST_TOKEN", ""),
                recall_secret=os.environ.get("RECALL_WEBHOOK_SECRET", ""),
                roles=json.loads(args.roles.read_text()) if args.roles else {},
            )
            uvicorn.run(app, host="127.0.0.1", port=args.port, ws_max_size=262144)
        elif args.command == "send":
            from .transports import send_turns

            url = args.url or (
                "http://127.0.0.1:8001/webhooks/transcript"
                if args.transport == "webhook"
                else "ws://127.0.0.1:8001/stream"
            )
            asyncio.run(
                send_turns(
                    args.input,
                    url,
                    os.environ.get("JEV_INGEST_TOKEN", ""),
                    args.transport,
                    args.delay,
                )
            )
        elif args.command == "meet-config":
            from .meet import bot_request

            print_json(bot_request(args.meeting_url, args.webhook_base, args.meeting_id))
        elif args.command == "evaluate":
            asyncio.run(evaluate_once(args))
        elif args.command == "replay":
            asyncio.run(replay(args))
        else:
            print_json(
                estimate(
                    args.minutes,
                    args.interval,
                    args.words_per_minute,
                    fixed_tokens=args.fixed_tokens,
                    price_per_million=args.price_per_million,
                )
            )
    except (ValueError, OSError, EvaluationError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
