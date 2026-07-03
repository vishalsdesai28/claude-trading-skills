# Short-Squeeze Signals — Reading FREE FINRA Data

This reference explains the mechanics behind the Short-Squeeze Radar, the free
FINRA data sources it uses, how to interpret each signal, and the caveats that
keep the read honest.

## What a short squeeze is

A short seller borrows shares, sells them, and must eventually buy them back
("cover"). If the price rises against a large, crowded short position, covering
becomes a forced buy — and forced buying pushes the price higher, forcing more
covering, in a feedback loop. That loop is a **short squeeze**.

Two conditions matter:

1. **Fuel** — a large, crowded short position that is hard to unwind quickly.
   This is what FINRA data lets us measure for free.
2. **Ignition** — a demand catalyst (earnings surprise, news, options gamma,
   coordinated buying) that starts the price moving up.

The radar identifies fuel. It cannot predict ignition. Treat every "squeeze
primed" flag as *a stock positioned to squeeze if a catalyst arrives*, not a
prediction that it will.

## The two FINRA data sources

### 1. Daily Reg SHO consolidated short-sale volume (FREE, no auth)

FINRA publishes, every trading day, the consolidated (CNMS) short vs. total
executed volume per symbol. This is the raw input behind every commercial
"short volume %" product.

- URL pattern: `https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt`
- Pipe-delimited, one header row, one trailer row ("Records: N"):
  `Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market`
- `short_volume_ratio = (ShortVolume + ShortExemptVolume) / TotalVolume`

**What it is:** daily *executed* short volume — a flow proxy. It answers "what
fraction of today's off-exchange executed volume was sold short?"

**What it is NOT:** the number of shares currently held short. A high daily
short-volume ratio does not by itself mean a large *standing* short position;
market-maker hedging and intraday shorting inflate it. That is why market-wide
short volume routinely runs ~40–50%.

**How to read it:** the squeeze-relevant signal is the **upper tail** (≥ 60%)
and, especially, a **rising multi-day trend** — shorts piling in day after day.

### 2. Bi-monthly Consolidated Short Interest (reported shares short)

FINRA collects and publishes short interest twice a month per settlement date.
Fields the radar uses:

- `short_interest` — reported shares held short
- `avg_daily_volume` — average daily trading volume
- `days_to_cover` = `short_interest / avg_daily_volume`

**Days-to-cover** (a.k.a. the short-interest ratio) estimates how many trading
days of average volume it would take shorts to buy back their entire position.
Higher = more forced buying required to unwind = more squeeze potential. It is
the single best static gauge of squeeze pressure.

The radar accepts short interest as a local CSV/JSON file (exported from FINRA,
NASDAQ, or a broker), so it stays free and offline. No short-interest URL is
hardcoded because a stable, no-auth public endpoint is not guaranteed.

## The signals the radar computes

| Signal | Source | Reading |
|---|---|---|
| `short_volume_ratio` | daily Reg SHO | upper tail (≥ 0.60) = crowded short flow |
| ratio trend (rising/flat/falling) | multi-day series | *rising* = shorts piling in (bullish squeeze setup) |
| `rising_inflection` | multi-day series | latest ratio is a fresh high above the prior day |
| `days_to_cover` | short interest | ≥ 5 = hard to unwind; < 2 = easily covered |
| classification | ratio + days-to-cover | crowded_short / neutral / low_pressure |
| `squeeze_score` (0–100) | composite | ratio (40) + days-to-cover (40) + trend (20) |

### Prior-trading-day fallback

A symbol can be missing from the most recent daily file (thin trading, or the
file not yet published for the current session). The radar walks back and uses
the most recent day the symbol *does* appear, flagging `fallback_used` so the
staleness is visible.

## Thresholds (defaults, tunable in the script)

- `RATIO_CROWDED = 0.60` — at/above this, short-side flow is crowded.
- `RATIO_LIGHT = 0.35` — at/below this, short pressure is light.
- `DTC_HIGH = 5.0` — days-to-cover at/above this is hard to unwind.
- `DTC_LOW = 2.0` — below this, shorts can exit in a day or two.
- `TREND_EPS = 0.03` — ratio move over the window needed to call a trend.

**Classification:**
- `crowded_short`: ratio ≥ 0.60 OR days-to-cover ≥ 5.0
- `low_pressure`: ratio ≤ 0.35 AND (days-to-cover < 2.0 or unknown)
- `neutral`: everything else

**Squeeze-primed** = `crowded_short` AND trend `rising` AND
(days-to-cover ≥ 3.0 OR ratio ≥ 0.65). This isolates crowded shorts that are
*still building* and hard to exit — the classic pre-squeeze posture.

## Caveats and discipline

- **Executed short volume is a proxy, not standing short interest.** Never treat
  a high daily ratio alone as "heavily shorted." Confirm with reported short
  interest and days-to-cover.
- **A squeeze needs a catalyst.** Crowded shorts can stay crowded for months and
  bleed longs dry. The radar finds fuel; wait for ignition (price/volume
  confirmation, news, options flow).
- **Borrow availability matters.** Squeezes intensify when shares are hard to
  borrow (HTB) and short sellers get bought-in. FINRA files do not carry borrow
  cost — confirm HTB status at the broker.
- **Free data lags.** Daily short-volume files publish after the close; short
  interest is bi-monthly and settlement-lagged. Use for setup screening, not
  intraday timing.
- **This is analysis, not advice.** Pair with position sizing, a real stop, and
  a catalyst thesis before any trade.

## Worked example

GME over three trading days shows short-volume ratios 0.48 → 0.56 → 0.68
(rising, fresh high). Reported short interest 25,000,000 shares against
3,000,000 average daily volume gives days-to-cover ≈ 8.33. That is a crowded
short (ratio ≥ 0.60 and days-to-cover ≥ 5), still building (rising, inflection),
and hard to unwind (days-to-cover ≥ 3) → **squeeze primed**. AMC over the same
window sits at ~0.31 with days-to-cover 0.5 → **low pressure**: little short-side
flow and trivially coverable. The radar ranks GME first (highest days-to-cover)
and leaves AMC near the bottom.
