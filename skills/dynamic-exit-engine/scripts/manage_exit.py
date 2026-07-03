"""Dynamic exit engine — adaptive trailing-stop FSM for an open long equity position.

Manages the exit of a single open equity position with a two-phase design that
replays deterministically over a price series each run:

  Phase 1 (loss protection): a hard stop at the tighter of a spot-% floor or an
    ATR-scaled floor -- ``min(spot_stop_pct, clamp(ATR% * mult))``.
  Phase 2 (profit lock): once unrealised profit clears ``protect_pct``, a trailing
    floor at ``entry + (peak - entry) * (1 - retrace_tier)`` that ratchets one-way
    (never gives locked profit back).

Layered on top:
  * breakeven ratchet   -- once peak profit clears a trigger, the floor may never
                           fall below ``entry + fees`` (a guaranteed-profit lock).
  * stale-flat timeout  -- cut a position that has drifted flat (never armed
                           Phase 2) for N bars: it is opportunity-cost dead weight.
  * ATR noise band      -- below the first Phase-2 tier, a give-back inside the
                           name's normal volatility does NOT fire an exit (holds).
  * consecutive breach  -- require N consecutive bars closing below the floor
                           before firing a trailing exit (whipsaw suppression).

Unlike a leveraged-perp exit engine, there is NO leverage-aware ROE cap here:
this repo trades cash equities via Alpaca / Robinhood, so only the ATR-scaled and
spot-% stop paths are kept.

The FSM is a pure left-fold over the bar list from entry -> now, so two runs with
the same inputs produce the same state (replay-deterministic). A JSON snapshot is
persisted on disk and rehydrated on the next run purely to RECONCILE against the
current broker position (detect a re-entry / a close) and to diff the recommended
stop for notification -- the snapshot is never fed back into the FSM transitions.

Usage:
    python3 manage_exit.py --ticker AAPL --entry 150 --qty 100 --atr 3.2 \\
        --bars-source fixture --bars-fixture bars.json --atr-stop --output-dir reports/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

SCHEMA_VERSION = "1.0"
SKILL_NAME = "dynamic-exit-engine"

# Float tolerance for "did the entry / stop change" comparisons.
_EPS = 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# Policy
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RetraceTier:
    """A profit tier with its own give-back threshold.

    When peak profit is >= ``pct_above_entry``, the trailing floor gives back
    ``retrace_threshold`` (0-1) of the peak run-up.
    """

    pct_above_entry: float
    retrace_threshold: float


def _default_tiers() -> list[RetraceTier]:
    # Tighter give-back as the winner grows -- bank more of a big move.
    return [
        RetraceTier(5.0, 0.30),
        RetraceTier(10.0, 0.40),
        RetraceTier(20.0, 0.50),
        RetraceTier(50.0, 0.60),
    ]


@dataclass
class ExitPolicy:
    """Configuration for the dynamic exit engine (long equity).

    Defaults target a swing-trade profile on daily bars. All percentages are in
    spot price terms (no leverage / ROE), e.g. ``spot_stop_pct=8`` means an 8%
    drawdown from entry is the hard stop.
    """

    # ── Phase 1: hard stop ──────────────────────────────────────────────
    spot_stop_pct: float = 8.0  # fixed % drawdown from entry
    atr_stop_enabled: bool = False  # scale the hard stop by ATR when True
    atr_stop_mult: float = 2.0  # stop width = mult * (ATR as % of entry)
    atr_stop_floor_pct: float = 3.0  # clamp: tightest ATR stop allowed
    atr_stop_ceiling_pct: float = 15.0  # clamp: widest ATR stop allowed
    # ── Phase 2: profit lock ────────────────────────────────────────────
    protect_pct: float = 3.0  # profit % that arms the trailing floor
    retrace_threshold: float = 0.30  # default give-back below the first tier
    phase2_tiers: list[RetraceTier] = field(default_factory=_default_tiers)
    # ── Breakeven ratchet ───────────────────────────────────────────────
    breakeven_trigger_pct: float = 0.0  # peak profit % that arms the lock (0 = off)
    breakeven_lock_pct: float = 0.1  # floor locked this % above entry (covers fees)
    # ── Stale-flat timeout ──────────────────────────────────────────────
    stale_flat_bars: int = 0  # cut after N bars never reaching protect_pct (0 = off)
    # ── Whipsaw / noise suppression ─────────────────────────────────────
    consecutive_breaches_required: int = 1  # closes below floor before exit fires
    noise_band_enabled: bool = False  # suppress sub-first-tier give-back exits
    noise_band_atr_mult: float = 1.0  # tolerated pull-back = mult * ATR% (sub-tier)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase2_tiers"] = [asdict(t) for t in self.phase2_tiers]
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> ExitPolicy:
        raw = dict(raw or {})
        tiers_raw = raw.pop("phase2_tiers", None)
        pol = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        if tiers_raw:
            pol.phase2_tiers = [RetraceTier(**t) for t in tiers_raw]
        return pol

    def validate(self) -> None:
        if self.spot_stop_pct <= 0:
            raise ValueError("spot_stop_pct must be positive")
        if self.atr_stop_enabled:
            if self.atr_stop_mult <= 0:
                raise ValueError("atr_stop_mult must be positive")
            if self.atr_stop_floor_pct <= 0 or self.atr_stop_ceiling_pct <= 0:
                raise ValueError("atr_stop clamp bounds must be positive")
            if self.atr_stop_floor_pct > self.atr_stop_ceiling_pct:
                raise ValueError("atr_stop_floor_pct must be <= atr_stop_ceiling_pct")
        if self.protect_pct <= 0:
            raise ValueError("protect_pct must be positive")
        if not 0.0 <= self.retrace_threshold < 1.0:
            raise ValueError("retrace_threshold must be in [0, 1)")
        for t in self.phase2_tiers:
            if not 0.0 <= t.retrace_threshold < 1.0:
                raise ValueError("tier retrace_threshold must be in [0, 1)")
        if self.consecutive_breaches_required < 1:
            raise ValueError("consecutive_breaches_required must be >= 1")
        if self.stale_flat_bars < 0:
            raise ValueError("stale_flat_bars must be >= 0")


# ─────────────────────────────────────────────────────────────────────────────
# Bar normalisation
# ─────────────────────────────────────────────────────────────────────────────

_CLOSE_KEYS = ("c", "close", "adjClose", "adj_close")
_TS_KEYS = ("ts_et", "ts", "datetime", "date")


def _bar_close(bar: dict) -> float:
    for k in _CLOSE_KEYS:
        if k in bar and bar[k] is not None:
            return float(bar[k])
    raise ValueError(f"bar has no close price (looked for {_CLOSE_KEYS}): {bar!r}")


def _bar_ts(bar: dict) -> str | None:
    for k in _TS_KEYS:
        if k in bar and bar[k] is not None:
            return str(bar[k])
    return None


def normalize_bars(raw: list[dict], entry_date: str | None = None) -> list[dict]:
    """Return bars as chronological ``{"ts", "close"}`` dicts.

    - Accepts FMP shape (``date``/``close``) and compact shape (``ts_et``/``c``).
    - Sorts by timestamp when every bar carries one; otherwise preserves input
      order (assumed already chronological).
    - When ``entry_date`` is given and bars carry timestamps, drops bars strictly
      before it so the fold starts at entry (ISO strings compare lexically).
    """
    out: list[dict] = []
    for b in raw:
        out.append({"ts": _bar_ts(b), "close": _bar_close(b)})
    if all(b["ts"] is not None for b in out) and out:
        out.sort(key=lambda b: b["ts"])
        if entry_date:
            out = [b for b in out if b["ts"] >= entry_date]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Stop math
# ─────────────────────────────────────────────────────────────────────────────


def compute_stop(policy: ExitPolicy, entry: float, atr: float | None) -> tuple[float, str]:
    """Return ``(effective_stop_pct, source)`` for the Phase-1 hard stop.

    ``effective = min(spot_stop_pct, clamp(ATR% * mult))`` -- the tighter floor
    wins. Falls back to the spot cap when ATR scaling is disabled or no ATR is
    supplied. ``source`` is ``"atr"`` or ``"spot"``.
    """
    spot = policy.spot_stop_pct
    if policy.atr_stop_enabled and atr and atr > 0:
        atr_pct = atr / entry * 100.0
        atr_clamped = min(
            max(atr_pct * policy.atr_stop_mult, policy.atr_stop_floor_pct),
            policy.atr_stop_ceiling_pct,
        )
        if atr_clamped < spot:
            return atr_clamped, "atr"
        return spot, "spot"
    return spot, "spot"


def active_retrace(policy: ExitPolicy, peak_profit_pct: float) -> float:
    """Highest active tier's give-back for the current peak profit."""
    retrace = policy.retrace_threshold
    for tier in policy.phase2_tiers:
        if peak_profit_pct >= tier.pct_above_entry:
            retrace = tier.retrace_threshold
    return retrace


# ─────────────────────────────────────────────────────────────────────────────
# Core FSM — pure left-fold over the price series from entry
# ─────────────────────────────────────────────────────────────────────────────


def replay(entry: float, bars: list[dict], policy: ExitPolicy, atr: float | None = None) -> dict:
    """Fold the two-phase trailing-stop FSM over ``bars`` (long side only).

    Pure function of ``(entry, bars, policy, atr)`` -- no wall clock, no prior
    snapshot -- so it is replay-deterministic. Returns a state dict with the
    final peak / floor / status plus a per-bar ``floor_trace`` for auditing.

    ``bars`` are normalised ``{"ts", "close"}`` dicts in chronological order,
    starting at (or just after) entry. Each bar's close is treated as the mark
    price (the bar-granularity analogue of a live tick).
    """
    if entry <= 0:
        raise ValueError("entry must be positive")
    policy.validate()

    entry_atr_pct = (atr / entry * 100.0) if (atr and atr > 0) else 0.0
    eff_stop_pct, stop_source = compute_stop(policy, entry, atr)
    hard_stop_px = entry * (1 - eff_stop_pct / 100.0)
    first_tier_pct = min(
        (t.pct_above_entry for t in policy.phase2_tiers), default=policy.protect_pct
    )

    peak = entry
    prev_floor: float | None = None
    consecutive = 0
    floor_trace: list[float] = []

    status = "holding"
    phase = "phase1"
    exit_reason: str | None = None
    exit_bar_ts: str | None = None
    exit_bar_index: int | None = None
    last_mark = entry
    last_floor = hard_stop_px

    for i, bar in enumerate(bars):
        mark = bar["close"]
        last_mark = mark
        if mark > peak:
            peak = mark

        profit_pct = (mark - entry) / entry * 100.0
        peak_profit_pct = (peak - entry) / entry * 100.0
        loss_pct = (entry - mark) / entry * 100.0
        bars_elapsed = i + 1

        # 1) Hard max-loss stop — immediate, never noise-suppressed.
        if loss_pct >= eff_stop_pct:
            status = "exited"
            phase = "phase1"
            exit_reason = f"max_loss ({loss_pct:.2f}% <= {eff_stop_pct:.2f}% stop [{stop_source}])"
            exit_bar_ts = bar["ts"]
            exit_bar_index = i
            last_floor = hard_stop_px
            floor_trace.append(round(hard_stop_px, 4))
            break

        # 2) Stale-flat timeout — position never armed Phase 2 within N bars.
        if (
            policy.stale_flat_bars > 0
            and bars_elapsed >= policy.stale_flat_bars
            and peak_profit_pct < policy.protect_pct
        ):
            status = "exited"
            phase = "timeout"
            exit_reason = (
                f"stale_flat ({bars_elapsed} bars, peak {peak_profit_pct:.2f}% "
                f"< protect {policy.protect_pct:.2f}%)"
            )
            exit_bar_ts = bar["ts"]
            exit_bar_index = i
            last_floor = hard_stop_px
            floor_trace.append(round(hard_stop_px, 4))
            break

        # 3) Compute the floor for this bar.
        if profit_pct >= policy.protect_pct:
            phase = "phase2"
            retrace = active_retrace(policy, peak_profit_pct)
            floor = entry + (peak - entry) * (1 - retrace)
        else:
            phase = "phase1"
            floor = hard_stop_px

        # 4) Breakeven ratchet: once peak clears the trigger, lock >= entry+fees.
        if policy.breakeven_trigger_pct > 0 and peak_profit_pct >= policy.breakeven_trigger_pct:
            floor = max(floor, entry * (1 + policy.breakeven_lock_pct / 100.0))

        # 5) One-way ratchet: the floor never falls for a long.
        if prev_floor is not None:
            floor = max(floor, prev_floor)
        prev_floor = floor
        last_floor = floor
        floor_trace.append(round(floor, 4))

        # 6) Breach check.
        breached = mark < floor

        # 7) Noise-band suppression (sub-first-tier only). The hard stop above is
        #    NOT suppressed; this only governs the trailing give-back of a barely
        #    green position inside its normal volatility.
        if breached and policy.noise_band_enabled and entry_atr_pct > 0:
            pullback_pct = (peak - mark) / entry * 100.0
            band = policy.noise_band_atr_mult * entry_atr_pct
            if peak_profit_pct < first_tier_pct and pullback_pct <= band:
                breached = False
                consecutive = 0

        # 8) Consecutive-breach confirmation.
        if breached:
            consecutive += 1
            if consecutive >= policy.consecutive_breaches_required:
                status = "exited"
                exit_reason = f"floor_breach ({consecutive}x consec close < floor {floor:.4f})"
                exit_bar_ts = bar["ts"]
                exit_bar_index = i
                break
        else:
            consecutive = 0

    unrealized_pct = (last_mark - entry) / entry * 100.0
    peak_profit_pct = (peak - entry) / entry * 100.0

    return {
        "status": status,
        "phase": phase,
        "entry": round(entry, 4),
        "atr": atr,
        "entry_atr_pct": round(entry_atr_pct, 4),
        "effective_stop_pct": round(eff_stop_pct, 4),
        "stop_source": stop_source,
        "hard_stop_px": round(hard_stop_px, 4),
        "peak_px": round(peak, 4),
        "peak_profit_pct": round(peak_profit_pct, 4),
        "floor_px": round(last_floor, 4),
        "last_mark": round(last_mark, 4),
        "unrealized_pct": round(unrealized_pct, 4),
        "consecutive_breaches": consecutive,
        "bars_processed": len(floor_trace),
        "exit_reason": exit_reason,
        "exit_bar_ts": exit_bar_ts,
        "exit_bar_index": exit_bar_index,
        "floor_trace": floor_trace,
    }


def derive_action(state: dict) -> dict:
    """Translate FSM state into a trader action (deterministic from state alone).

    - ``exit``       : the FSM fired a stop during the replay.
    - ``raise_stop`` : still holding and the floor has ratcheted above the entry
                       hard stop -- move the resting broker stop up to ``floor_px``.
    - ``hold``       : still holding in Phase 1 at the initial hard stop.
    """
    floor = state["floor_px"]
    if state["status"] == "exited":
        return {
            "action": "exit",
            "recommended_stop": floor,
            "reason": state["exit_reason"],
        }
    if floor > state["hard_stop_px"] + _EPS:
        return {
            "action": "raise_stop",
            "recommended_stop": floor,
            "reason": f"{state['phase']} trailing floor ratcheted to {floor:.4f}",
        }
    return {
        "action": "hold",
        "recommended_stop": floor,
        "reason": f"phase1 hard stop at {floor:.4f}; no ratchet yet",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot persistence + reconciliation
# ─────────────────────────────────────────────────────────────────────────────


def snapshot_path(state_dir: str, ticker: str, side: str) -> str:
    safe = ticker.replace("/", "_")
    return os.path.join(state_dir, f"dynamic_exit_{safe}_{side}.json")


def load_snapshot(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def save_snapshot(path: str, snapshot: dict) -> None:
    """Atomically persist the snapshot (tmp + os.replace, best-effort)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    os.replace(tmp, path)


def reconcile(prior: dict | None, entry: float, qty: float | None) -> dict:
    """Reconcile the on-disk snapshot against the current broker position.

    Decisions:
    - ``no_position`` : broker reports qty == 0 -> the position closed.
    - ``fresh``       : no prior snapshot for this key.
    - ``reset``       : prior snapshot's entry differs (re-entry / averaged in) ->
                        the old peak/floor are stale.
    - ``match``       : prior snapshot tracks the same entry.
    """
    if qty is not None and abs(qty) < _EPS:
        return {"decision": "no_position", "note": "broker reports flat (qty=0)"}
    if prior is None:
        return {"decision": "fresh", "note": "no prior tracker on disk"}
    prior_entry = prior.get("entry_price")
    if prior_entry is None or abs(float(prior_entry) - entry) > max(_EPS, entry * 1e-4):
        return {
            "decision": "reset",
            "note": f"entry changed {prior_entry} -> {entry}; prior peak/floor discarded",
        }
    return {"decision": "match", "note": "tracking the same entry"}


# ─────────────────────────────────────────────────────────────────────────────
# Data sources
# ─────────────────────────────────────────────────────────────────────────────


def load_bars_fixture(path: str, ticker: str) -> list[dict]:
    """Load bars from a JSON fixture: a bare list, or ``{ticker: [bars]}``."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        if ticker in data:
            return data[ticker]
        # Ignore documentation keys like "_doc"; take the first list value.
        for k, v in data.items():
            if isinstance(v, list):
                return v
        raise ValueError(f"fixture {path} has no bar list for {ticker}")
    if isinstance(data, list):
        return data
    raise ValueError(f"fixture {path} is neither a list nor a dict")


def fetch_bars_fmp(ticker: str, api_key: str, limit: int = 400) -> list[dict]:
    """Fetch daily OHLCV from Financial Modeling Prep (lazy stdlib HTTP).

    Network path only -- never exercised by tests. Uses urllib from the stdlib
    to avoid a hard ``requests`` dependency.
    """
    import urllib.request  # lazy: keep pure/parse paths stdlib-only

    url = (
        "https://financialmodelingprep.com/api/v3/historical-price-full/"
        f"{ticker}?serietype=line&timeseries={limit}&apikey={api_key}"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8"))
    hist = payload.get("historical", []) if isinstance(payload, dict) else []
    if not hist:
        raise ValueError(f"FMP returned no historical bars for {ticker}")
    return hist


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────


def run(
    *,
    ticker: str,
    side: str,
    entry: float,
    qty: float | None,
    atr: float | None,
    entry_date: str | None,
    raw_bars: list[dict],
    policy: ExitPolicy,
    state_dir: str,
    as_of: str | None = None,
) -> dict:
    """Reconcile, replay the FSM, derive the action, and build the snapshot."""
    if side != "long":
        raise ValueError("dynamic-exit-engine currently supports long positions only")

    path = snapshot_path(state_dir, ticker, side)
    prior = load_snapshot(path)
    rec = reconcile(prior, entry, qty)

    as_of = as_of or datetime.now(timezone.utc).date().isoformat()

    base = {
        "schema_version": SCHEMA_VERSION,
        "skill": SKILL_NAME,
        "ticker": ticker,
        "side": side,
        "entry_price": round(entry, 4),
        "entry_date": entry_date,
        "qty": qty,
        "atr": atr,
        "as_of": as_of,
        "policy": policy.to_dict(),
        "reconcile": rec,
    }

    if rec["decision"] == "no_position":
        base["state"] = {"status": "no_position"}
        base["action"] = {
            "action": "no_position",
            "recommended_stop": None,
            "reason": "no open position to manage",
        }
        return base

    bars = normalize_bars(raw_bars, entry_date=entry_date)
    if not bars:
        raise ValueError("no bars to replay (empty series or all filtered by entry_date)")

    state = replay(entry, bars, policy, atr=atr)
    action = derive_action(state)

    # Diff for notification only (NOT an FSM input): did the recommended stop
    # move up since the last run's snapshot?
    prior_stop = None
    if prior and rec["decision"] == "match":
        prior_stop = (prior.get("action") or {}).get("recommended_stop")
    stop_raised = None
    if prior_stop is not None and action["recommended_stop"] is not None:
        stop_raised = action["recommended_stop"] > float(prior_stop) + _EPS

    base["state"] = state
    base["action"] = action
    base["stop_raised_since_last_run"] = stop_raised
    return base


def generate_markdown_report(snapshot: dict) -> str:
    s = snapshot.get("state", {})
    a = snapshot.get("action", {})
    lines = [
        f"# Dynamic Exit Engine — {snapshot['ticker']} ({snapshot['side']})",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**As of:** {snapshot.get('as_of')}",
        "",
        "## Position",
        f"- Entry: {snapshot.get('entry_price')}",
        f"- Qty: {snapshot.get('qty')}",
        f"- ATR: {snapshot.get('atr')}",
        f"- Reconcile: {snapshot['reconcile']['decision']} — {snapshot['reconcile']['note']}",
        "",
    ]
    if s.get("status") == "no_position":
        lines.append("## Action")
        lines.append(f"- **{a.get('action')}** — {a.get('reason')}")
        return "\n".join(lines) + "\n"

    lines += [
        "## State",
        f"- Status: {s.get('status')}",
        f"- Phase: {s.get('phase')}",
        f"- Effective stop: {s.get('effective_stop_pct')}% ({s.get('stop_source')})",
        f"- Hard stop: {s.get('hard_stop_px')}",
        f"- Peak: {s.get('peak_px')} (+{s.get('peak_profit_pct')}%)",
        f"- Floor: {s.get('floor_px')}",
        f"- Last mark: {s.get('last_mark')} ({s.get('unrealized_pct')}%)",
        f"- Bars processed: {s.get('bars_processed')}",
    ]
    if s.get("exit_reason"):
        lines.append(f"- Exit reason: {s.get('exit_reason')} @ {s.get('exit_bar_ts')}")
    lines += [
        "",
        "## Action",
        f"- **{a.get('action')}** — {a.get('reason')}",
        f"- Recommended stop: {a.get('recommended_stop')}",
    ]
    if snapshot.get("stop_raised_since_last_run") is not None:
        lines.append(f"- Stop raised since last run: {snapshot['stop_raised_since_last_run']}")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Dynamic exit engine — adaptive trailing stop for a long equity position",
    )
    # Position
    p.add_argument("--ticker", required=True)
    p.add_argument("--side", default="long", choices=["long"])
    p.add_argument("--entry", type=float, help="Entry price (or provide --position-json)")
    p.add_argument("--qty", type=float, help="Current shares held (0 = closed)")
    p.add_argument("--atr", type=float, help="ATR in price units (enables ATR-scaled paths)")
    p.add_argument("--entry-date", help="ISO date; filters bars to >= this date")
    p.add_argument(
        "--position-json",
        help="JSON file with {ticker, entry_price, qty, side}; flags override its fields",
    )
    # Price series
    p.add_argument("--bars-source", choices=["fixture", "fmp"], default="fixture")
    p.add_argument("--bars-fixture", help="Path to bars JSON (list or {ticker:[bars]})")
    p.add_argument("--api-key", help="FMP API key (or set FMP_API_KEY)")
    # Policy
    p.add_argument("--policy-json", help="JSON file with a full ExitPolicy override")
    p.add_argument("--spot-stop-pct", type=float, default=ExitPolicy.spot_stop_pct)
    p.add_argument("--atr-stop", action="store_true", help="Enable ATR-scaled hard stop")
    p.add_argument("--atr-mult", type=float, default=ExitPolicy.atr_stop_mult)
    p.add_argument("--atr-floor-pct", type=float, default=ExitPolicy.atr_stop_floor_pct)
    p.add_argument("--atr-ceiling-pct", type=float, default=ExitPolicy.atr_stop_ceiling_pct)
    p.add_argument("--protect-pct", type=float, default=ExitPolicy.protect_pct)
    p.add_argument("--retrace", type=float, default=ExitPolicy.retrace_threshold)
    p.add_argument("--breakeven-trigger-pct", type=float, default=ExitPolicy.breakeven_trigger_pct)
    p.add_argument("--breakeven-lock-pct", type=float, default=ExitPolicy.breakeven_lock_pct)
    p.add_argument("--stale-flat-bars", type=int, default=ExitPolicy.stale_flat_bars)
    p.add_argument(
        "--consecutive-breaches",
        type=int,
        default=ExitPolicy.consecutive_breaches_required,
    )
    p.add_argument("--noise-band", action="store_true", help="Enable ATR noise-band hold")
    p.add_argument("--noise-band-atr-mult", type=float, default=ExitPolicy.noise_band_atr_mult)
    # Output
    p.add_argument("--state-dir", default="state/dynamic_exit/")
    p.add_argument("--output-dir", default="reports/")
    p.add_argument("--as-of", help="ISO date override (default: today UTC)")
    return p


def _policy_from_args(args: argparse.Namespace) -> ExitPolicy:
    if args.policy_json:
        with open(args.policy_json, encoding="utf-8") as f:
            return ExitPolicy.from_dict(json.load(f))
    return ExitPolicy(
        spot_stop_pct=args.spot_stop_pct,
        atr_stop_enabled=args.atr_stop,
        atr_stop_mult=args.atr_mult,
        atr_stop_floor_pct=args.atr_floor_pct,
        atr_stop_ceiling_pct=args.atr_ceiling_pct,
        protect_pct=args.protect_pct,
        retrace_threshold=args.retrace,
        breakeven_trigger_pct=args.breakeven_trigger_pct,
        breakeven_lock_pct=args.breakeven_lock_pct,
        stale_flat_bars=args.stale_flat_bars,
        consecutive_breaches_required=args.consecutive_breaches,
        noise_band_enabled=args.noise_band,
        noise_band_atr_mult=args.noise_band_atr_mult,
    )


def _resolve_position(
    args: argparse.Namespace,
) -> tuple[str, str, float, float | None, float | None]:
    ticker, side, entry, qty, atr = args.ticker, args.side, args.entry, args.qty, args.atr
    if args.position_json:
        with open(args.position_json, encoding="utf-8") as f:
            pos = json.load(f)
        ticker = args.ticker or pos.get("ticker")
        side = args.side or pos.get("side", "long")
        entry = args.entry if args.entry is not None else pos.get("entry_price")
        qty = args.qty if args.qty is not None else pos.get("qty")
        atr = args.atr if args.atr is not None else pos.get("atr")
    if entry is None:
        raise ValueError("entry price is required (--entry or --position-json)")
    return ticker, side, float(entry), qty, atr


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        ticker, side, entry, qty, atr = _resolve_position(args)
        policy = _policy_from_args(args)

        # Load bars only when there is a position to manage.
        if qty is not None and abs(qty) < _EPS:
            raw_bars: list[dict] = []
        elif args.bars_source == "fixture":
            if not args.bars_fixture:
                raise ValueError("--bars-source fixture requires --bars-fixture")
            raw_bars = load_bars_fixture(args.bars_fixture, ticker)
        else:
            api_key = args.api_key or os.environ.get("FMP_API_KEY")
            if not api_key:
                raise ValueError("FMP API key missing: pass --api-key or set FMP_API_KEY")
            raw_bars = fetch_bars_fmp(ticker, api_key)

        snapshot = run(
            ticker=ticker,
            side=side,
            entry=entry,
            qty=qty,
            atr=atr,
            entry_date=args.entry_date,
            raw_bars=raw_bars,
            policy=policy,
            state_dir=args.state_dir,
            as_of=args.as_of,
        )
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Persist the living tracker snapshot (keyed by ticker+side).
    written_snapshot = dict(snapshot)
    written_snapshot["written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_snapshot(snapshot_path(args.state_dir, ticker, side), written_snapshot)

    # Write the dated report (JSON + Markdown) to the reports dir.
    os.makedirs(args.output_dir, exist_ok=True)
    as_of = snapshot["as_of"]
    json_path = os.path.join(args.output_dir, f"dynamic_exit_{ticker}_{as_of}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    md_path = os.path.join(args.output_dir, f"dynamic_exit_{ticker}_{as_of}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(generate_markdown_report(snapshot))

    a = snapshot["action"]
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    print(f"Action: {a['action'].upper()} — {a['reason']}")
    if a.get("recommended_stop") is not None:
        print(f"Recommended stop: {a['recommended_stop']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
