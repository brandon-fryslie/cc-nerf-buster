# Anthropic API Pricing

Source: <https://platform.claude.com/docs/en/about-claude/pricing.md>
Captured: 2026-05-11

All prices in USD per million tokens (MTok). This file is the reference
the proxy's `modelPricing` table (`anthropic.go`) is calibrated against.
Update both together.

## Model pricing

| Model | Base Input | 5m Cache Write | 1h Cache Write | Cache Hit / Refresh | Output |
|-------|-----------:|---------------:|---------------:|--------------------:|-------:|
| Claude Opus 4.7   | $5     | $6.25  | $10    | $0.50 | $25     |
| Claude Opus 4.6   | $5     | $6.25  | $10    | $0.50 | $25     |
| Claude Opus 4.5   | $5     | $6.25  | $10    | $0.50 | $25     |
| Claude Opus 4.1   | $15    | $18.75 | $30    | $1.50 | $75     |
| Claude Opus 4     | $15    | $18.75 | $30    | $1.50 | $75     |
| Claude Sonnet 4.6 | $3     | $3.75  | $6     | $0.30 | $15     |
| Claude Sonnet 4.5 | $3     | $3.75  | $6     | $0.30 | $15     |
| Claude Sonnet 4   | $3     | $3.75  | $6     | $0.30 | $15     |
| Claude Sonnet 3.7 (deprecated) | $3 | $3.75 | $6 | $0.30 | $15 |
| Claude Haiku 4.5  | $1     | $1.25  | $2     | $0.10 | $5      |
| Claude Haiku 3.5  | $0.80  | $1     | $1.60  | $0.08 | $4      |
| Claude Opus 3 (deprecated) | $15 | $18.75 | $30 | $1.50 | $75 |
| Claude Haiku 3    | $0.25  | $0.30  | $0.50  | $0.03 | $1.25   |

## Multipliers relative to base input

These are the ratios the proxy uses (`cacheWriteMultiplier`, `cacheReadMultiplier`
in `anthropic.go`). They hold across every current-gen model:

| Column | Multiplier vs base input |
|---|---|
| 5m cache write | 1.25× |
| 1h cache write | 2.00× |
| Cache hit / refresh | 0.10× |
| Output | 5.00× |

The proxy currently encodes `cacheWriteMultiplier = 2.0` (the 1h cache rate). If
a request uses the 5m cache it will be overcharged by 1.6× on its cache_creation
tokens in the proxy's weighted-USD accounting.

## Notes

- Opus 4.7 uses a new tokenizer that may consume up to 35% more tokens for the
  same input text than earlier models. Cost-per-task comparisons across model
  generations need to account for this.
- Cross-tier ratios (haiku : sonnet : opus) for current models are 1 : 3 : 5 on
  both input and output — same ratio as the proxy's `modelPricing` table.
