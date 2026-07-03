# Retail sentiment scoring

How `ingest_retail_sentiment.py` turns raw StockTwits / Reddit / X data into a
per-ticker band + 0-10 score + confidence. Every step is a **pure, deterministic
function** — the model never reads raw posts, so nothing is fabricated
(data-in-prompt design, adapted from TradingAgents' sentiment analyst, which was
redesigned specifically because prompt-driven social analysis hallucinated
Reddit/X content under pressure).

## The 0-10 scale (six bands)

Mirrors TradingAgents' `SentimentReport` scale so downstream consumers read one
convention:

| Score | Band | Vault `direction` |
|---|---|---|
| ≥ 6.5 | Bullish | long |
| 5.5–6.4 | Mildly Bullish | long |
| 4.5–5.4 | Neutral | watch |
| 3.5–4.4 | Mildly Bearish | short |
| < 3.5 | Bearish | short |
| (any, on divergence) | Mixed | watch |

## StockTwits — explicit labels + message-count base rate

StockTwits messages carry a user-applied `Bullish` / `Bearish` tag (or none).
The bullish fraction is shrunk toward a 0.5 base rate with a Beta(2, 2)-style
prior so a thin sample cannot scream 10/10 off two messages:

```
posterior_bullish = (bullish + 2) / (labeled + 4)
stocktwits_score  = 10 × posterior_bullish
```

- 10 bullish / 1 bearish (11 labeled) → (10+2)/(11+4) = 0.80 → **8.0**.
- 2 bullish / 0 bearish → (2+2)/(2+4) = 0.667 → **6.67** (not 10 — small sample).
- 0 labeled (all unlabeled) → 2/4 = 0.5 → **5.0** neutral.

**Contrarian over-extension flag.** With a real sample (≥ 10 labeled messages),
a lean of ≥ 90/10 either way fires `contrarian_overextension` and records the
`overextension_side`. A crowd this one-sided is a crowded trade with elevated
reversal risk — the flag is surfaced, not used to flip direction.

## Reddit / X — engagement-weighted keyword sentiment

Reddit posts and X posts carry no explicit label. Each post gets a crude,
transparent directional score from distinct keyword hits
(`polarity_from_text`): bullish terms (`buy`, `calls`, `breakout`, `moon`, …)
minus bearish terms (`puts`, `short`, `dump`, `bearish`, …). Ambiguous words
(`hold`, `green`, `support`) are deliberately excluded. Neutral posts (no hits)
count toward volume but not direction.

Directional posts are combined **weighted by engagement** so a 900-upvote thread
outweighs a 3-upvote post:

```
weight_reddit = 1 + ln(1 + score + num_comments)     # RSS posts → weight 1.0
weight_x      = 1 + ln(1 + impressions)              # falls back to likes
fraction      = Σ(weight × sign) / Σ(weight)          # −1 … +1
score         = 5 + 5 × fraction × shrink             # shrink = n_dir/(n_dir+3)
```

The `shrink` term regresses small directional samples toward neutral, mirroring
the StockTwits base-rate idea.

- **Reddit engagement weighting** activates on the JSON search path (which
  carries `score`/`num_comments`). Reddit's keyless public path is the Atom RSS
  feed, which carries neither — those posts fall back to equal weight and the
  note is marked `via_rss`, matching TradingAgents' honest RSS degradation.
- **X impressions** are the strongest engagement signal; `parse_x` also
  impression-ranks the posts so the loudest surface first.

## Cross-source combine

Each source contributes its score, weighted by `base_weight × reliability`:

- `base_weight`: StockTwits 1.0 (explicit labels → most reliable retail read),
  Reddit 0.8, X 0.7.
- `reliability`: `min(1, sample / full)` — StockTwits full at 20 labeled,
  Reddit/X full at 8 directional posts. Thin sources count for less.

```
overall = Σ(score × base_weight × reliability) / Σ(base_weight × reliability)
```

**Divergence.** If one directional source is bullish and another bearish, the
read is forced to **Mixed / watch** (`divergence_sources` lists which), because
the disagreement is itself the signal — retail leaning into a thesis one crowd
hasn't caught, or chasing while another fades.

**Confidence.** From total directional volume across sources:
`high` (≥ 25 and ≥ 2 sources), `medium` (≥ 8), else `low`. Divergence caps it at
`medium`.

## What is deliberately NOT emitted

- **No `probability`** — retail sentiment is not a calibrated probability; the
  vault schema says set it "only with a real basis," so it is omitted.
- **No `watch` trigger/invalidation block** — sentiment carries no clean numeric
  price levels; inventing them would be fabrication.
- **No raw bodies or usernames in notes** — source notes carry aggregate stats
  only; raw payloads live in the git-ignored `raw/` tree for audit.

## Corroboration downstream

Each source that returned data becomes one wikilinked source note on the signal.
`edge-social-aggregator` scores `recency × corroboration`, where corroboration =
number of independent source notes. A ticker surfaced by StockTwits + Reddit + a
YouTube video (from `social-signal-ingestor` in the same vault) naturally unions
to a higher `n_sources` — that cross-platform agreement is the point.
