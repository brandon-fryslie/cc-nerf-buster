# cc-nerf-buster

Directly measuring the size of Claude Code's quota.

## CC Quota

The numbers below are what we measured for CC's 5h and 7d quota.  We used a Claude Max account on 2026-04-24 to generate this data.

### Methodology

Each request's tokens are converted into a weighted cost using the published API price ratios. The per-1%-tick cost is bracketed by the last pre-tick observation and the first post-tick observation, and multiplied by 100 to estimate the full-quota size.  Conceptually,
this is the formula:

```
weighted_cost(req) = Σ tokens[kind] × model_multiplier × kind_multiplier
   model_multiplier:  Haiku=1, Sonnet=3, Opus=5
   kind_multiplier:   input=1, output=5, cache_write=2, cache_read=0.1

full_quota ≈ (weighted_cost between two adjacent 1% ticks) × 100
```

The figures are **normalized units**, not literal token allowances. "16.8M Opus cache-write tokens" means the quota is equivalent in weighted cost to writing that many Opus cache tokens — *not* that you get 16.8M cache-write tokens plus 6.7M output tokens. Each column is a different projection of the same underlying weighted-cost budget.

Because any Claude Code session will have a different mix of tokens used (some sessions might have more output, or more cache misses, etc), there is no single number that directly represents your remaining quota.  No doubt this is why Anthropic does not report one - it doesn't exist.  I have normalized the units into 2 units, each representing your entire quota: cache-write (or cache-create) tokens, and output tokens.  I chose these because they're reasonably intuitive.  When you start a new session, your tokens are billed as cache-write.  When you receive tokens, those are billed as output tokens.

Here is a breakdown of ~250 of my own sessions I happened to have data for:

  ┌─────────────────────────────┬─────────────┬────────┐
  │         Token type          │    Count    │ Share  │
  ├─────────────────────────────┼─────────────┼────────┤
  │ cache_read_input_tokens     │ 861,364,553 │ 94.69% │
  ├─────────────────────────────┼─────────────┼────────┤
  │ cache_creation_input_tokens │  37,486,686 │  4.12% │
  ├─────────────────────────────┼─────────────┼────────┤
  │ output_tokens               │   5,788,314 │  0.64% │
  ├─────────────────────────────┼─────────────┼────────┤
  │ input_tokens                │   4,987,715 │  0.55% │
  └─────────────────────────────┴─────────────┴────────┘

The two tables below answer two different questions. **"Quota size"** treats the budget as a single number — for comparing accounts, plugging into a calculator, or just having a figure to talk about. **"What you can actually do per tick"** projects that same budget onto a real usage mix, so you can estimate how much Claude Code work fits in a 5-hour or 7-day window.

### Quota size — one number per window

If you just want a single number for "how big is my quota," here it is. The same budget is shown in two equivalent units; pick whichever feels more intuitive. (Convert to any other token kind using the [API pricing table](https://platform.claude.com/docs/en/about-claude/pricing).)

| Window | Opus output tokens | per 1% tick | Opus cache-write tokens | per 1% tick |
| ------ | -----------------: | ----------: | ----------------------: | ----------: |
| 5-hour |          6,758,213 |      67,582 |              16,895,532 |     168,955 |
| 7-day  |         34,338,697 |     343,387 |              85,846,742 |     858,467 |

The 7-day quota is 5.08× the 5-hour quota. Numbers are midpoints; the measured low–high spread is under 0.3% (full bracket below).

<details>
<summary>Low / midpoint / high bracket</summary>

Each 1% tick is bracketed by the last observation before the tick (low) and the first after (high); the midpoint is the average.

| Window | Bound | Opus output tokens | Opus cache-write tokens |
| ------ | ----- | -----------------: | ----------------------: |
| 5-hour | Low   |          6,749,159 |              16,872,898 |
| 5-hour | Mid   |          6,758,213 |              16,895,532 |
| 5-hour | High  |          6,767,266 |              16,918,166 |
| 7-day  | Low   |         34,332,703 |              85,831,758 |
| 7-day  | Mid   |         34,338,697 |              85,846,742 |
| 7-day  | High  |         34,344,690 |              85,861,725 |
</details>

### What you can actually do per tick

The numbers above are *normalized* — they assume the entire quota goes to one token kind. Real Claude Code sessions spend across all four kinds, and the cheap ones (cache reads) consume most of the volume. Projected onto the measured 250-session mix above, the same quota covers:

| Window | Output tokens (model writes back) | per 1% tick | Cache-write tokens (new context loaded) | per 1% tick |
| ------ | --------------------------------: | ----------: | ---------------------------------------: | ----------: |
| 5-hour |                         1,002,841 |      10,028 |                                6,494,671 |      64,946 |
| 7-day  |                         5,095,469 |      50,954 |                               32,999,636 |     329,996 |

These are the two numbers that map onto practical work. **Output tokens** are what the model generates and you read. **Cache-write tokens** are how much new content (files, tool output, long prompts) you can introduce into a session before the cache absorbs it. Input tokens (~0.55% of mix) and cache reads (~94.7% of mix but cheap to re-read) are ambient bookkeeping and aren't shown here.

<details>
<summary>Full per-kind breakdown (all four token kinds, with budget share)</summary>

| Token kind  | Volume mix | Budget share | 5h full quota | 5h /1% tick | 7d full quota | 7d /1% tick |
| ----------- | ---------: | -----------: | ------------: | ----------: | ------------: | ----------: |
| Input       |      0.55% |         2.6% |       864,135 |       8,641 |     4,390,699 |      43,906 |
| Output      |      0.64% |        14.8% |     1,002,841 |      10,028 |     5,095,469 |      50,954 |
| Cache write |      4.12% |        38.4% |     6,494,671 |      64,946 |    32,999,636 |     329,996 |
| Cache read  |     94.69% |        44.2% |   149,233,783 |   1,492,337 |   758,261,660 |   7,582,616 |

**Volume mix** = share of raw tokens. **Budget share** = share of the weighted quota that mix consumes. The two differ because token kinds are priced differently: a cache-write token is 20× more expensive than a cache-read token, which is why cache writes are only 4.12% of volume but 38.4% of the budget.
</details>

## The Problem

Claude Code tells you what percentage of your quota you've used. It does not tell you what 100% is.

Before the size can be measured it has to be defined. Three questions have to be answered:

1. **Which window?** There are at least two rolling budgets — a 5-hour window and a 7-day window — with independent utilization meters that tick forward separately. There is also an overage bucket. The windows are not proportional to each other.
2. **In what unit?** Input tokens, output tokens, cache-read tokens, and cache-creation tokens are priced differently, and prices differ across model tiers. A quota denominated in a single token type would not behave consistently across runs. The internal budget is a weighted cost: model tier × token-kind multiplier, summed across the mix that ran.
3. **With what precision?** The utilization meter advances in discrete 1% ticks. A tick is not observable until after it has been crossed. Every measurement of per-tick cost is bracketed by the last observation before the tick and the first observation after it; the gap between those two observations is the measurement uncertainty.

The quota size is therefore three numbers per window, in an undocumented unit.
## The Approach

1. **Intercept.** A local proxy sits between Claude Code and `api.anthropic.com`. Every request/response pair is parsed and recorded: input tokens, output tokens, cache reads, cache writes, model.
2. **Weight.** Each request is converted into a weighted cost using a normalized model/token-kind price scale (Haiku : Sonnet : Opus = 1 : 3 : 5 for input tokens; output = 5× input; cache write = 2× input; cache read = 0.1× input). The ratios are taken from the published [API pricing table](https://platform.claude.com/docs/en/about-claude/pricing); only the ratios matter here, not the absolute prices. The internal quota is assumed to be proportional to this weighted cost. That assumption is not verifiable from outside — the Claude Code quota meter does not expose its own accounting — but the API pricing ratios are the only published reference point, so they are what the probe uses.
3. **Probe.** A driver runs Claude Code sessions in a loop against a single account, advancing the utilization meter tick by tick. After every request it samples both the accumulated weighted cost and the utilization percentage.
4. **Bracket.** For each 1% tick observed, the tool records the last pre-tick snapshot and the first post-tick snapshot.
5. **Scale.** Per-1%-tick cost × 100 = full-quota size, reported as low / midpoint / high from the bracketing step. The result is then projected into a chosen token type.

## Running It Yourself

Install and run your own capacity probe: see [`USAGE.md`](USAGE.md).

## Project Files

- `proxy.go` — the local proxy.
- `anthropic.go` — request/response parsing and token extraction.
- `metrics.go` — Prometheus collector and quota-window bookkeeping.
- `log.go` — canonical JSONL request log.
- `tools/capacity-probe/` — Python driver that produces the numbers above.
- `AGENTS.md` — architectural constraints for contributors.

## License

[MIT](LICENSE).
