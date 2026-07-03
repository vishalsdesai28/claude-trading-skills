# Style Factor Recipes

A multi-style idea-screen library for sourcing single-name long/short candidates
that can seed hypothesis cards. Each recipe pairs a **mechanical screen** (fields
the keyless yfinance boolean screener can actually filter on) with the
**qualitative factors** that the screener cannot verify and that therefore
require fundamental follow-up. Screens surface candidates, never conclusions —
every hit still needs the evidence work described in
`references/evidence_quality_guide.md`.

Run the recipes with `scripts/run_style_screens.py` (thin wrapper that shells
out to `skills/finviz-screener/scripts/yf_boolean_screen.py`; keyless, no API
key). See "Runner" at the bottom. Field names below are Yahoo `EquityQuery`
fields documented in `skills/finviz-screener/references/yahoo_screener_fields.md`
(yields and percent-change fields are **fractions**: `0.05` = 5%).

## Required output for every idea

An idea is not complete until it carries all four of the following. The runner
emits a per-candidate card scaffold with these headers pre-filled; the analyst
fills the cells from fundamentals, filings, and news.

1. **Peer-relative metric table** — the candidate's key metrics *next to* its
   closest peers or sector median. A cheap multiple in isolation means nothing;
   cheap *versus comparable businesses* is the signal. Columns are style-specific
   (see each recipe's `peer_metrics`).
2. **Mispricing bullets (3-5)** — why the price is wrong: what the market is
   extrapolating, what it is ignoring, and the variant perception.
3. **Catalyst** — the dated or conditional event that forces the gap to close
   (earnings, guide, spin completion, lockup expiry, refinancing, activist step).
   An idea without a catalyst is "being early", which is indistinguishable from
   being wrong.
4. **Disconfirming risks** — the specific observations that would falsify the
   thesis. These become the hypothesis card's `kill_criteria`. State what you
   would have to see to walk away, not generic "market risk".

## Style recipes

### 1. Value (long)

Cheap on cash-flow and asset terms, with management putting its own money in.

- **Mechanical screen:** trailing P/E `< 18`, P/B `< 2.5`, forward dividend
  yield `> 2%`, market cap `> $1B` (liquidity floor).
- **Qualitative factors (fundamental follow-up):**
  - **Free-cash-flow yield `> 5%`** — the anchor value factor; not a screener
    field, compute FCF / market cap from cash-flow statements.
  - **Insider buying in the last 90 days** — open-market purchases (not option
    exercises); corroborates that the discount is not a value trap.
  - EV/EBITDA below the stock's own 5-year average.
- **Peer metrics:** P/E, EV/EBITDA, P/B, FCF yield, dividend yield, net
  debt/EBITDA — each vs. the two closest peers and the sector median.
- **Typical catalyst:** cost program, asset sale/spin, buyback authorization,
  a cyclical trough inflecting, dividend hike.
- **Typical disconfirming risks:** FCF yield is optical (one-off working-capital
  release); insider buys are token-sized; the cheapness reflects a structural
  demand decline, not a cycle.

### 2. Growth (long)

Accelerating top line with widening unit economics — the market pays up only
when both are intact.

- **Mechanical screen:** trailing revenue growth `> 15%`, latest quarterly
  revenue growth `> 15%`, gross margin `> 40%`, market cap `> $1B`.
- **Qualitative factors:**
  - **Revenue acceleration** — the growth *rate is increasing* (most recent
    quarter YoY > prior quarter YoY). A single-period growth number cannot show
    this; pull the last 4-6 quarters.
  - **Margin expansion** — gross and operating margin trending up over the last
    4+ quarters, not just high in one print.
  - ROIC `> 15%`; net revenue retention `> 110%` for subscription models.
- **Peer metrics:** revenue growth (LTM and latest Q), gross/operating margin
  trend, EV/sales (NTM), ROIC, net retention — vs. peers and sector.
- **Typical catalyst:** guidance raise, new product ramp, TAM-expanding launch,
  a large-customer land, first quarter of GAAP profitability.
- **Typical disconfirming risks:** growth deceleration in the next print;
  margin gain was pull-forward or mix, not durable; retention slips; multiple
  already discounts the acceleration (see the priced-in test below).

### 3. Quality (long)

Durable compounders — high returns on capital, clean balance sheet, owner-operator
alignment. Held for the compounding, bought on temporary dislocation.

- **Mechanical screen:** ROE `> 15%`, total debt/equity `< 0.6`, EBITDA margin
  `> 20%`, market cap `> $2B`.
- **Qualitative factors:**
  - Consistent revenue growth over 5+ years (no single-year air pockets).
  - Stable or expanding margins across a full cycle.
  - High free-cash-flow conversion (FCF / net income near or above 1.0).
  - Insider ownership `> 5%` (skin in the game).
- **Peer metrics:** ROE, ROIC, gross/EBITDA margin, debt/EBITDA, FCF
  conversion, insider ownership — vs. peers and sector median.
- **Typical catalyst:** transient headwind resolving (FX, destocking, one
  bad quarter), reinvestment runway extension, capital-return step-up.
- **Typical disconfirming risks:** ROE is leverage-driven, not margin-driven;
  the moat is eroding (share loss, pricing pressure); the "temporary" headwind
  is structural.

### 4. Short (short)

Deteriorating fundamentals masked by an accrual/valuation gap, with insiders
heading for the exit. Shorts need higher conviction — timing is harder and the
risk is asymmetric.

- **Mechanical screen:** trailing P/E `> 40` (premium), trailing revenue growth
  `< 3%` (decelerating), market cap `> $1B` (borrowable/liquid).
- **Qualitative factors (the core short signal):**
  - **Receivables / inventory growing faster than sales** — days-sales-outstanding
    or days-inventory rising while revenue stalls flags channel stuffing or
    demand softening ahead of the reported line. Compute from balance-sheet +
    income-statement trend; not a screener field.
  - **Insider selling** — accelerating open-market sales, 10b5-1 plan changes,
    unusual option exercises-and-sell.
  - Valuation premium to peers with no growth/margin justification; accounting
    red flags (auditor change, restatement, non-GAAP widening).
  - **Crowding guard:** before committing, check `short_percentage_of_float.value`
    and `days_to_cover_short.value` — a crowded short invites a squeeze.
- **Peer metrics:** P/E, EV/sales, revenue growth, DSO / days-inventory trend,
  gross-margin trend, short interest % float, days-to-cover — vs. peers.
- **Typical catalyst:** earnings miss/guide-down, receivables write-down,
  covenant breach, lockup expiry adding supply, downgrade cycle.
- **Typical disconfirming risks:** the accrual build is a legitimate ramp
  (new contracts); a credible catalyst re-rates it higher; borrow is expensive
  or short interest is already extreme (squeeze risk).

### 5. Special situation (long or short)

Event-driven dislocations where a corporate action, not the operating trend,
sets the price. Screens are weak here — the situation triggers are
**event-sourced** (filings, news), so use the mechanical screen only as a
liquidity/size gate and source the trigger via WebSearch/WebFetch or the
market-news skills.

- **Mechanical screen (gate only):** market cap between `$300M` and `$50B`,
  3-month average daily volume `> 200k` shares.
- **Situation triggers (event-sourced):**
  - Spin-offs completed in the last 12 months (forced selling, orphaned coverage).
  - Recent IPO/SPAC with an upcoming lockup expiry (supply overhang → short, or
    post-flush washout → long).
  - Activist 13D/13D-A filed; proxy contest.
  - Emergence from restructuring/bankruptcy; fresh-start accounting.
  - Management change at a chronic underperformer.
- **Peer metrics:** sum-of-the-parts vs. current EV, pro-forma leverage,
  stub valuation, comparable transaction multiples.
- **Typical catalyst:** the event date itself — spin completion, lockup date,
  activist settlement, refinancing close, first clean post-emergence quarter.
- **Typical disconfirming risks:** the event is already fully reflected;
  the "cheap" stub carries a hidden liability; forced selling has further to run.

## Thematic value-chain sweep

For a top-down theme (e.g. "AI-inference capex accelerates through 2027",
"reshoring of grid equipment", "GLP-1 demand"), do not stop at the obvious
pure-plays. Map the whole chain and hunt the second-order names the market has
not yet connected to the theme.

**Procedure:**

1. **State the thesis** as a falsifiable, dated claim (what accelerates, by when,
   why).
2. **Map the value chain** into layers relative to the thesis:
   - **Direct beneficiaries** — pure-plays whose revenue is the theme itself.
   - **Indirect beneficiaries** — one step removed (suppliers, tooling, enablers).
   - **Second-order beneficiaries** — two+ steps removed (the power utility for
     the data center, the cooling/copper/transformer supplier, the logistics or
     testing name), or demand pulled through an adjacent market.
3. **Apply the priced-in test** to each name — classify as **priced-in** (the
   theme is already in consensus estimates and the multiple) vs.
   **not-yet-connected** (the market has not linked this name to the theme, so
   the beneficiary flow is absent from estimates). The edge lives in the
   not-yet-connected column — usually second-order names.
4. **Prefer pure-play exposure where mispriced, diversified exposure where the
   pure-play is priced-in** — a diversified name with a small-but-growing themed
   segment can be the cleaner risk/reward when the obvious pure-play is crowded.
5. **Require a transmission mechanism** for each candidate: the specific line
   item or contract through which the theme reaches this company's P&L. If you
   cannot name it, the connection is a narrative, not a thesis.

**Deliverable — value-chain map:**

| Layer | Ticker | Transmission mechanism | Priced-in? | Note |
|---|---|---|---|---|
| Direct | | revenue = the theme | usually yes | crowded; check ownership/short interest |
| Indirect | | supplier / enabler | mixed | look for margin, not just revenue, leverage |
| Second-order | | pulled-through demand | often **no** | the hunt list |

Every name promoted off the map still owes the four required outputs above
(peer table, mispricing bullets, catalyst, disconfirming risks) before it
becomes a hypothesis card.

## Runner

`scripts/run_style_screens.py` turns each recipe into a boolean-spec query and
(optionally) executes it via the keyless yfinance screener, then emits a
per-candidate idea-card scaffold with the four required sections.

```bash
# List available recipes (offline)
python3 skills/trade-hypothesis-ideator/scripts/run_style_screens.py --list

# Build the value recipe's spec + command WITHOUT running it (offline, default)
python3 skills/trade-hypothesis-ideator/scripts/run_style_screens.py \
  --recipe value --region us --output-dir reports/

# Actually run all recipes via the sibling keyless screener (network)
python3 skills/trade-hypothesis-ideator/scripts/run_style_screens.py \
  --all --execute --count 25 --output-dir reports/

# Format a value-chain map from a prepared beneficiaries JSON (offline)
python3 skills/trade-hypothesis-ideator/scripts/run_style_screens.py \
  --value-chain-file thesis.json --output-dir reports/
```

`--execute` is off by default so the runner stays fully offline (it just prints
the spec + the exact `yf_boolean_screen.py` command). The mechanical screen is a
first-pass filter only — every candidate must clear the qualitative factors and
carry the four required outputs before it seeds a hypothesis card.
