# Cumulative transcript cost

The estimator assumes a transcript grows linearly and is sent on every scheduled tick. It counts the shared transcript once per request, rather than eight times for MEDDPICC. It adds a configurable allowance for all questions and metadata combined.

For `N = floor(minutes × 60 / interval_seconds)`, let `g` be transcript tokens added per interval and `H` be fixed tokens per request:

```text
input_tokens = N × H + g × N × (N + 1) / 2
g = words_per_minute × tokens_per_word × interval_seconds / 60
cost = input_tokens / 1,000,000 × price_per_million
```

The defaults are 30 minutes, two seconds, 150 words/minute across all speakers, 4/3 tokens per word, 1,500 fixed tokens, and $0.042 per million input tokens. The result is 900 requests, 4,053,000 input tokens, and $0.170226. Tokenization depends on the content; the fixed allowance is not a measured count of the shipped prompts.

```bash
jev-incall cost --minutes 60 --interval 2 --fixed-tokens 2000
```

Longer calls cost more than linearly because earlier words are repeatedly sent. Skipping silent/unchanged ticks lowers request count. A slow provider also reduces throughput because the worker never overlaps evaluations. Retries can add cost. This calculation excludes speech-to-text and infrastructure.

The model price and free output are based on [TypeSafe's model page](https://docs.typesafe.ai/models), checked September 21, 2026. Update `--price-per-million` if pricing changes. No cache discount is assumed. A new transcript version is not an identical request.

Each successful result carries usage metadata. The meeting API sums reported input and output tokens from successful responses. This is useful for observability, but it is not a billing ledger: timed-out attempts can be billed without returning usage. Compare provider billing when measuring production cost.
