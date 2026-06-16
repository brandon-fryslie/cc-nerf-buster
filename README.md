# cc-nerf-buster

Directly measuring the size of Claude Code's quota.

## CC Quota

Measured on 2026-04-24 against a Claude Max account.

### Quota size

| Window | Opus cache-write tokens | per 1% tick |
| ------ | ----------------------: | ----------: |
| 5-hour |              16,895,532 |     168,955 |
| 7-day  |              85,846,742 |     858,467 |

The quota itself is a weighted budget across all four token kinds — input, output, cache-read, and cache-write — each priced differently, so it isn't denominated in any single one of them. Cache-writes are just one convenient projection: roughly the volume of fresh prompt tokens you could push through if every request was a cache miss. You can convert this number to any other token kind using the [API pricing table](https://platform.claude.com/docs/en/about-claude/pricing).

The 7-day quota works out to roughly 5.08× the 5-hour quota. The numbers above are midpoints, and the measured spread between the low and high bounds is under 0.3%.

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

The single-number quota tells you how big the budget is, but it doesn't tell you how many tokens of each kind you'll actually burn through to spend it — that depends on your usage mix. The breakdown below shows one such mix, drawn from about 250 sessions on the author's account:

| Token kind  | Volume mix | Budget share | 5h full quota | 5h /1% tick | 7d full quota | 7d /1% tick |
| ----------- | ---------: | -----------: | ------------: | ----------: | ------------: | ----------: |
| cache_read  |     94.69% |        44.2% |   149,233,783 |   1,492,337 |   758,261,660 |   7,582,616 |
| cache_write |      4.12% |        38.4% |     6,494,671 |      64,946 |    32,999,636 |     329,996 |
| output      |      0.64% |        14.8% |     1,002,841 |      10,028 |     5,095,469 |      50,954 |
| input       |      0.55% |         2.6% |       864,135 |       8,641 |     4,390,699 |      43,906 |

**Volume mix** is the share of raw tokens of each kind. **Budget share** is the fraction of the weighted quota that mix actually consumes — and it diverges from volume mix because cache-writes cost 20× as much per token as cache-reads. Different mixes will produce different per-kind numbers; this table describes only the one measured above.

### Methodology

Each request's tokens are converted into a weighted cost. The current probe treats `usage.jsonl` as the canonical event stream: for one org/upstream/window scope, every priced event advances cumulative measured cost, and every observed integer-percent utilization change records a crossing bracket.

Each crossing is a position constraint on the unknown per-tick capacity. Pairing two crossings cancels the unknown quota usage that existed before the run started; intersecting all such pairwise constraints yields the final capacity interval. Multiplying the per-tick interval by 100 gives the full-window quota interval.

```
weighted_usd(req) =
  (
    model_input_price × (input + 1.25×cache_write_5m + 2×cache_write_1h + 0.1×cache_read)
    + model_output_price × output
  ) / 1,000,000

crossing[k] = cost at which utilization crossed k%
C_interval = intersection over crossing pairs (a,b):
  C ∈ [(cost_before[b] - cost_after[a]) / (b.k - a.k),
       (cost_after[b]  - cost_before[a]) / (b.k - a.k)]
```

## The Problem

Claude Code tells you what percentage of your quota you've used. It does not tell you what 100% is.

Before you can measure the size of the quota, you have to decide what "size" actually means. There are three questions to answer:

1. **Which window?** There are at least two rolling budgets in play — a 5-hour window and a 7-day window — and each one has its own utilization meter that ticks forward independently. There's also an overage bucket layered on top. The two windows aren't proportional to each other, so a single number can't describe both.
2. **In what unit?** Input tokens, output tokens, cache-read tokens, and cache-creation tokens are all priced differently, and prices vary across model tiers on top of that. A quota denominated in any single token type wouldn't behave consistently from one run to the next. What Claude Code actually tracks internally is a weighted cost — model tier multiplied by token-kind multiplier, summed across the entire mix of requests that ran.
3. **With what precision?** The utilization meter only advances in discrete 1% ticks, and you can't observe a tick until after it's been crossed. So every measurement of per-tick cost has to be bracketed by the last observation before the tick and the first observation after it, and the gap between those two observations is the measurement uncertainty.

So there's no single "magic number" for the quota — it's a weighted budget, not a token count. But once you fix a window and pick a unit to project the budget into, the size does collapse to one concrete value, with a measurement band set by tick precision. That's exactly what the table at the top reports.

## The Approach

1. **Intercept.** A local proxy sits between Claude Code and `api.anthropic.com`, parsing every request/response pair and recording the input tokens, output tokens, cache reads, cache writes, and model used.
2. **Weight.** Each request is converted into a weighted cost using the formula in [Methodology](#methodology) above. The ratios come from the published [API pricing table](https://platform.claude.com/docs/en/about-claude/pricing), and only the ratios matter — not the absolute prices. The internal quota is assumed to be proportional to this weighted cost. That assumption isn't verifiable from outside, since the Claude Code meter doesn't expose its own accounting, but the API pricing ratios are the only published reference point we have to work from.
3. **Probe.** A driver runs Claude Code sessions in a loop against a single account, advancing the utilization meter tick by tick. The proxy writes each request/response pair to the run's `usage.jsonl`.
4. **Constrain.** For each 1% tick that gets observed, the estimator records the measured-cost bracket containing that crossing. Pairwise crossing constraints eliminate the unknown starting quota usage.
5. **Scale.** Multiplying the per-tick interval by 100 gives the full-quota interval, reported as low / midpoint / high. That result is then projected into whichever token type you want to read it in.

## Running It Yourself

To install and run your own capacity probe, see [`USAGE.md`](USAGE.md).

## Project Files

- `proxy.go` — the local proxy.
- `anthropic.go` — request/response parsing and token extraction.
- `metrics.go` — Prometheus collector and quota-window bookkeeping.
- `log.go` — canonical JSONL request log.
- `tools/quota_probe/` — fresh event-sourced quota estimator and driver.
- `tools/capacity-probe/` — legacy Python driver retained for comparison.
- `AGENTS.md` — architectural constraints for contributors.

## License

[MIT](LICENSE).
