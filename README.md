# cc-nerf-buster

Directly measuring the size of Claude Code's quota.

## The Problem

Claude Code tells you what percentage of your quota you've used. It does not tell you what 100% is.

Before the size can be measured it has to be defined. Three questions have to be answered:

1. **Which window?** There are at least two rolling budgets — a 5-hour window and a 7-day window — with independent utilization meters that tick forward separately. There is also an overage bucket. The windows are not proportional to each other.
2. **In what unit?** Input tokens, output tokens, cache-read tokens, and cache-creation tokens are priced differently, and prices differ across model tiers. A quota denominated in a single token type would not behave consistently across runs. The internal budget is a weighted cost: model tier × token-kind multiplier, summed across the mix that ran.
3. **With what precision?** The utilization meter advances in discrete 1% ticks. A tick is not observable until after it has been crossed. Every measurement of per-tick cost is bracketed by the last observation before the tick and the first observation after it; the gap between those two observations is the measurement uncertainty.

The quota size is therefore three numbers per window, in an undocumented unit.

## The Approach

1. **Intercept.** A local MITM proxy sits between Claude Code and `api.anthropic.com`. Every request/response pair is decrypted, parsed, and recorded: input tokens, output tokens, cache reads, cache writes, model.
2. **Weight.** Each request is converted into a weighted cost using a normalized model/token-kind price scale (Haiku : Sonnet : Opus = 1 : 3 : 5 for input tokens; output = 5× input; cache write = 2× input; cache read = 0.1× input). The ratios are taken from the published [API pricing table](https://platform.claude.com/docs/en/about-claude/pricing); only the ratios matter here, not the absolute prices. The internal quota is assumed to be proportional to this weighted cost. That assumption is not verifiable from outside — the Claude Code quota meter does not expose its own accounting — but the API pricing ratios are the only published reference point, so they are what the probe uses.
3. **Probe.** A driver runs Claude Code sessions in a loop against a single account, advancing the utilization meter tick by tick. After every request it samples both the accumulated weighted cost and the utilization percentage.
4. **Bracket.** For each 1% tick observed, the tool records the last pre-tick snapshot and the first post-tick snapshot.
5. **Scale.** Per-1%-tick cost × 100 = full-quota size, reported as low / midpoint / high from the bracketing step. The result is then projected into a chosen token type.

## The Results

Measured 2026-04-24, probing `api.anthropic.com` with a Claude Max account. Results are projected into two units: **Opus cache-write tokens** and **Opus output tokens**.

### 5-hour window

Opus cache-write tokens (input side):

| Bound | Full quota  | Per 1% tick |
| ----- | ----------- | ----------- |
| Low   | 16,872,898  | 168,729     |
| Mid   | 16,895,532  | 168,955     |
| High  | 16,918,166  | 169,182     |

Opus output tokens:

| Bound | Full quota  | Per 1% tick |
| ----- | ----------- | ----------- |
| Low   | 6,749,159   | 67,492      |
| Mid   | 6,758,213   | 67,582      |
| High  | 6,767,266   | 67,673      |

### 7-day window

Opus cache-write tokens (input side):

| Bound | Full quota  | Per 1% tick |
| ----- | ----------- | ----------- |
| Low   | 85,831,758  | 858,318     |
| Mid   | 85,846,742  | 858,467     |
| High  | 85,861,725  | 858,617     |

Opus output tokens:

| Bound | Full quota  | Per 1% tick |
| ----- | ----------- | ----------- |
| Low   | 34,332,703  | 343,327     |
| Mid   | 34,338,697  | 343,387     |
| High  | 34,344,690  | 343,447     |

The 7-day quota is 5.08× the 5-hour quota.

## Running It Yourself

Install, CA trust, operating the proxy, and running your own capacity probe: see [`USAGE.md`](USAGE.md).

Security model (the tool MITMs your own traffic with a locally-generated root CA): see [`SECURITY.md`](SECURITY.md).

## Project Files

- `proxy.go`, `ssl_inspect.go` — the MITM proxy and its CA.
- `anthropic.go` — request/response parsing and token extraction.
- `metrics.go` — Prometheus collector and quota-window bookkeeping.
- `log.go` — canonical JSONL request log.
- `tools/capacity-probe/` — Python driver that produces the numbers above.
- `AGENTS.md` — architectural constraints for contributors.

## License

[MIT](LICENSE).
