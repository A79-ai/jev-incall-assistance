"""Explicit napkin-math assumptions; no cache discount or audio cost included."""

import math


def estimate(
    minutes=30.0,
    interval=2.0,
    words_per_minute=150.0,
    tokens_per_word=4 / 3,
    fixed_tokens=1500,
    price_per_million=0.042,
):
    values = (minutes, interval, words_per_minute, tokens_per_word, fixed_tokens, price_per_million)
    if any(not math.isfinite(v) or v < 0 for v in values) or interval == 0:
        raise ValueError("Cost inputs must be finite and nonnegative; interval must be positive")
    calls = math.floor(minutes * 60 / interval)
    growth_per_call = words_per_minute * tokens_per_word * interval / 60
    tokens = calls * fixed_tokens + growth_per_call * calls * (calls + 1) / 2
    return {
        "minutes": minutes,
        "interval_seconds": interval,
        "calls": calls,
        "estimated_input_tokens": round(tokens),
        "estimated_cost_usd": tokens / 1e6 * price_per_million,
        "assumptions": {
            "words_per_minute": words_per_minute,
            "tokens_per_word": tokens_per_word,
            "fixed_tokens_per_call": fixed_tokens,
            "price_per_million": price_per_million,
            "cache_discount": 0,
            "output_price": 0,
        },
    }
