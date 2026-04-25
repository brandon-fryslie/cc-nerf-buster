# cc-nerf-buster

Directly measuring the size of Claude Code's quota.

## CC Quota

Measured 2026-04-24 against a Claude Max account.

### Quota size

| Window | Opus cache-write tokens | per 1% tick |
| ------ | ----------------------: | ----------: |
| 5-hour |              16,895,532 |     168,955 |
| 7-day  |              85,846,742 |     858,467 |

The quota itself is a weighted budget across all four token kinds (input, output, cache-read, cache-write), each priced differently — it isn't denominated in any single kind. Cache-writes are one convenient projection: roughly the count of fresh tokens you can send Claude in one prompt with no caching assumed. Convert to any other kind via the [API pricing table](https://platform.claude.com/docs/en/about-claude/pricing).

The 7-day quota is 5.08× the 5-hour quota. Numbers are midpoints; measured low–high spread is under 0.3%.

<details>
<summary>Low / midpoint / high bracket</summary>

| Window | Bound | Opus cache-write tokens |
| ------ | ----- | ----------------------: |
| 5-hour | Low   |              16,872,898 |
| 5-hour | Mid   |              16,895,532 |
| 5-hour | High  |              16,918,166 |
| 7-day  | Low   |              85,831,758 |
| 7-day  | Mid   |              85,846,742 |
| 7-day  | High  |              85,861,725 |
</details>

### Per-kind quota at one measured mix

The single-number quota tells you how big the budget is. It does *not* tell you how many tokens of each kind you'll burn through to spend it — that depends on your usage mix. Below is the breakdown for one measured mix (~250 of my own sessions):

| Token kind  | Volume mix | Budget share | 5h full quota | 5h /1% tick | 7d full quota | 7d /1% tick |
| ----------- | ---------: | -----------: | ------------: | ----------: | ------------: | ----------: |
| cache_read  |     94.69% |        44.2% |   149,233,783 |   1,492,337 |   758,261,660 |   7,582,616 |
| cache_write |      4.12% |        38.4% |     6,494,671 |      64,946 |    32,999,636 |     329,996 |
| output      |      0.64% |        14.8% |     1,002,841 |      10,028 |     5,095,469 |      50,954 |
| input       |      0.55% |         2.6% |       864,135 |       8,641 |     4,390,699 |      43,906 |

**Volume mix** is the share of raw tokens of each kind. **Budget share** is what fraction of the weighted quota that mix consumes — different from volume mix because cache-writes cost 20× as much per token as cache-reads. Different mixes give different per-kind numbers; this table only describes the one measured above.

### Methodology

Each request's tokens are converted to a weighted cost. The per-1%-tick cost is bracketed by the last pre-tick observation and the first post-tick observation, then multiplied by 100 to estimate full-quota size.

```
weighted_cost(req) = Σ tokens[kind] × model_multiplier × kind_multiplier
   model_multiplier:  Haiku=1, Sonnet=3, Opus=5
   kind_multiplier:   input=1, output=5, cache_write=2, cache_read=0.1

full_quota ≈ (weighted_cost between two adjacent 1% ticks) × 100
```

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
