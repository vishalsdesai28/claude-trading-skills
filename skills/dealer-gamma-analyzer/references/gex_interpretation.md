# Dealer Gamma (GEX) Interpretation — Pin vs Squeeze

This reference explains how to read the output of `scripts/analyze_gex.py` and turn a
gamma map into trading implications. It covers the two dealer-positioning conventions,
what positive vs negative gamma does to realized volatility, how the walls act as
support/resistance, how max pain and OPEX pinning interact, and the gamma-cliff risk.

---

## 1. Why dealer gamma moves price

Options dealers (market makers) run a delta-hedged book. Their hedging is mechanical:
to stay delta-neutral they must trade the underlying as spot moves, and the *direction*
of that trading is set by the sign of their **net gamma**.

- **Dealers LONG gamma (positive GEX):** as spot rises their delta rises, so they SELL
  the underlying; as spot falls their delta falls, so they BUY. They lean *against*
  price -> they **absorb** flow -> realized volatility is **dampened** -> price
  **pins / mean-reverts** toward the high-gamma strikes.
- **Dealers SHORT gamma (negative GEX):** the signs flip — they BUY as spot rises and
  SELL as spot falls. They lean *with* price -> they **amplify** flow -> realized
  volatility is **magnified** -> trends extend and **squeezes** become self-reinforcing.

The single most important read is the **sign of net GEX** and where **spot sits relative
to the gamma flip**.

---

## 2. The two conventions (always state which one)

Dealer positioning is not directly observable, so GEX depends on an assumption about who
holds what. The analyzer reports both:

### Convention A — "dealers short calls / long puts" (SqueezeMetrics net)

```
net_gex_$ = Σ (OI_call × gamma_call × spot²)  −  Σ (OI_put × gamma_put × spot²)
```

Calls contribute positive gamma, puts negative. This is the **headline** number and
drives the regime:

- **net GEX > 0** -> dealers net long gamma -> stabilizing / pin / mean-revert.
- **net GEX < 0** -> dealers net short gamma -> destabilizing / trend / squeeze fuel.

### Convention B — "customer-net-long-everything" (dealers short both)

```
gross_hedge_$ = Σ (OI_call × gamma_call × spot²)  +  Σ (OI_put × gamma_put × spot²)
```

Treats dealers as short *both* calls and puts (appropriate when customers are buying
everything in a retail-driven melt-up). This is the **maximum hedging pressure**
estimate — an upper bound, not the regime signal.

> A net-GEX number with a flipped sign convention is worse than no number at all. Never
> present an unlabeled GEX figure. The dollar magnitude also depends on the "per 1% move"
> basis used here (`OI × gamma × spot²`, multiplier 100 folded in); third-party figures
> using per-$1 or per-share bases can differ by orders of magnitude.

---

## 3. The dollar-gamma-per-1%-move formula

For one contract (multiplier 100):

```
$ delta change per $1 spot move  = 100 × gamma × spot
$ delta change per 1% spot move  = 100 × gamma × spot × (spot × 0.01) = gamma × spot²
$ gamma exposure per 1% move     = OI × gamma × spot²
```

The `100` and the `0.01` cancel, so per-strike dollar gamma per 1% move is simply
`OI × gamma × spot²`. Interpret it as: *"if spot moves 1%, dealers must trade about this
many dollars of the underlying to re-hedge this strike."*

---

## 4. Walls as support/resistance

- **Call wall** = the strike at or above spot carrying the largest call gamma. Overhead
  **resistance**: as spot approaches, long-gamma dealers sell into the move, capping it.
  A decisive break *above* the call wall in a negative-gamma name can flip local hedging
  and ignite a **gamma squeeze**.
- **Put wall** = the strike at or below spot carrying the largest put gamma. Downside
  **support**: dealer hedging tends to defend it. A break *below* the put wall removes
  that support and, in negative gamma, can accelerate the decline.

Render both as explicit price levels with % distance from spot (the analyzer does this in
`support_resistance`). Treat them as levels where hedging *leans against* price, not as
guaranteed barriers — sufficient real flow overwhelms any wall.

---

## 5. The gamma flip (zero-gamma level)

The **gamma flip** is the level that separates the two regimes:

- **Spot ABOVE the flip** -> positive-gamma / pin zone (mean-revert, low realized vol).
- **Spot BELOW the flip** -> negative-gamma / trend zone (squeeze-prone, high realized vol).

The analyzer computes it as a **strike-space proxy**: the strike where cumulative net
gamma (summed across ascending strikes) crosses zero. Below that strike puts dominate;
above it calls dominate. This is not a fitted volatility surface, and because gamma
itself depends on spot, the proxy flip strike can occasionally disagree in sign with the
total net GEX. When the aggregate net GEX is positive but spot sits *below* the proxy
flip, read it as "structurally long-gamma, but locally in the trend zone" and lean on the
walls for the operational levels.

---

## 6. Max pain and OPEX pinning

**Max pain** is the settlement strike that minimizes the total in-the-money value paid to
option holders (equivalently, maximizes worthless expiries for writers). In a
positive-gamma regime, price often gravitates toward max pain into expiration ("pinning"),
because dealer hedging around the largest open-interest strikes damps moves away from them.

- Pinning is strongest in the **final days before monthly OPEX** and weakest when a
  fresh catalyst (earnings, macro print, index rebalance) overrides the mechanical flow.
- After expiration, the pinning gamma disappears. If a single near-dated strike dominated
  the book, its removal is a **gamma cliff**: dealer hedges unwind and the underlying can
  move sharply once freed from the pin. Check whether the magnet strikes and max pain sit
  in the nearest expiry — if so, the pin has a natural fuse.

---

## 7. Magnet strikes

The **magnet strikes** are the strikes with the largest |net gamma|. These are where
dealer hedging concentrates and therefore where price tends to be pulled or stall. In a
pin regime they are gravity wells (expect chop and reversion around them); in a
negative-gamma regime a break *through* a magnet can accelerate the move as hedging flips.

---

## 8. Squeeze vs routine rally — diagnostic checklist

A move is consistent with a **gamma squeeze** (as opposed to a fundamental repricing) when
several of these line up:

| Indicator | Squeeze-consistent | Routine move |
|---|---|---|
| Net GEX (Convention A) | Strongly negative | Positive or near zero |
| Spot vs gamma flip | Below the flip | Above the flip |
| Call/Put OI ratio | > 2.5 (call-heavy) | ~1.0–1.5 |
| ATM IV | Elevated / spiking | Stable |
| OI concentration | Single near-dated strike dominates | Diffuse across expirations |
| Price behavior | Trending, gap-and-go | Range-bound, fades to walls |

Hitting 4+ markers is squeeze-consistent; 1–2 is more likely a normal, dealer-dampened
range. The analyzer surfaces net GEX, spot-vs-flip, the call/put OI ratio, and ATM IV to
support this read.

---

## 9. Trading implications

**Positive-gamma regime (pin / mean-revert):**
- Fade extremes back toward the high-gamma strikes and max pain; sell strength into the
  call wall, buy weakness into the put wall.
- Expect low realized volatility and range-bound chop; breakouts need real flow to stick.
- Premium-selling (theta) strategies are relatively favored; the pin works with you.

**Negative-gamma regime (amplify / squeeze-prone):**
- Respect momentum; mean-reversion fades are dangerous because dealer hedging pushes with
  the move.
- A break above the call wall can fuel a squeeze; a break below the put wall can cascade.
- Long-gamma / long-optionality structures are relatively favored; realized vol tends to
  exceed what a calm tape implies.

---

## 10. Caveats

- **GEX assumes uniform dealer positioning.** The true dealer book (who is long vs short
  each line) is unobservable; the conventions are assumptions.
- **The gamma flip is a strike-space proxy**, not a re-priced zero-gamma spot level.
- **CBOE data is ~15 minutes delayed.** That is fine for this structural/daily map, but it
  is not an intraday tick signal.
- **Descriptive, not predictive.** GEX maps where hedging pressure sits *now*; it does not
  forecast future flows, catalysts, or price.
