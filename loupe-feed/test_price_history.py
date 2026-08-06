#!/usr/bin/env python3
"""Price-history fixtures — the honesty guarantees, not the plumbing.

This file exists because the output is shopper-facing and brand-facing. A wrong
"never discounted" badge is worse than no badge: it invites a brand to point at
their own sale and ask what else we got wrong.

The one real incident so far: on the first run, six brands showed 100% of their
catalog "discounted" — Rat and Boa, Attega, Susamusa, Martine Rose, Musier Paris
and C'est Nous. None of them had run a sale. On 2026-07-15 the scrape was pinned
to country=US (commit 10b4c79) and their prices stepped into the new regime over
the following days. Comparing across that boundary manufactured a discount.
"""
import inspect

import build_price_history
from build_price_history import (
    epoch_of,
    in_settle_window,
    history_is_truncated,
    sampling_epoch_of,
    in_sampling_settle_window,
    crosses_sampling_epoch,
    arrival_blackout,
    PRICE_EPOCHS,
    SAMPLING_EPOCHS,
    EPOCH_SETTLE_DAYS,
    MIN_MEANINGFUL_MOVE,
)

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# ── Epoch boundaries ─────────────────────────────────────────────────────────
check("a day before the pin is epoch 0", epoch_of("2026-07-14") == 0)
check("the pin day itself is epoch 1", epoch_of("2026-07-15") == 1)
check("a later day is epoch 1", epoch_of("2026-08-01") == 1)
check("epochs never go backwards",
      all(epoch_of("2026-07-14") <= epoch_of(d) for d in ("2026-07-15", "2026-09-01")))

# ── The settle window is what actually prevents the false discounts ──────────
check("the pin day is inside the settle window", in_settle_window("2026-07-15"))
check("day+1 is still settling", in_settle_window("2026-07-16"))
check("day+2 is still settling", in_settle_window("2026-07-17"))
check("day+3 is comparable again", not in_settle_window("2026-07-18"))
check("a day before the pin is not settling", not in_settle_window("2026-07-14"))
check("a much later day is not settling", not in_settle_window("2026-08-01"))
check("the settle window is exactly EPOCH_SETTLE_DAYS wide",
      sum(in_settle_window(d) for d in
          ("2026-07-15", "2026-07-16", "2026-07-17", "2026-07-18")) == EPOCH_SETTLE_DAYS)

# ── Contract checks that stop a well-meaning edit from re-breaking this ──────
check("there is at least one declared price epoch", len(PRICE_EPOCHS) >= 1)
check("the country=US pin is declared", "2026-07-15" in PRICE_EPOCHS)
check("epochs are sorted", PRICE_EPOCHS == sorted(PRICE_EPOCHS))
check("the settle window is non-trivial", EPOCH_SETTLE_DAYS >= 2)
# A 2% floor means FX rounding and cent-level drift never read as a sale.
check("the meaningful-move floor is a real floor", 0.005 <= MIN_MEANINGFUL_MOVE <= 0.10)

# ── The claim logic itself, on synthetic prices ──────────────────────────────
def claims(prices):
    """Mirror the emitter: (never_discounted, at_lowest) for one epoch's prices."""
    lo, hi, last = min(prices), max(prices), prices[-1]
    never = hi <= lo * (1 + MIN_MEANINGFUL_MOVE)
    lowest = last <= lo * (1 + MIN_MEANINGFUL_MOVE) and not never
    return never, lowest

check("a flat price is 'never discounted'", claims([120, 120, 120]) == (True, False))
check("cent drift is still 'never discounted'", claims([120.0, 120.5, 120.0])[0])
check("a real sale is not 'never discounted'", claims([200, 200, 140])[0] is False)
check("a piece sitting at its low says so", claims([200, 200, 140])[1] is True)
check("a piece that rebounded does NOT say 'lowest'", claims([200, 140, 200])[1] is False)
check("a single observation makes no claim", claims([120]) == (True, False))

# ── The shallow-clone trap (2026-08-01) ──────────────────────────────────────
# A truncated clone produced a well-formed file covering 28 of 42 days and said
# nothing about it. These pin the guard so it cannot be quietly removed again.
check("truncation is detectable at all", isinstance(history_is_truncated(), bool))

_sig = inspect.signature(build_price_history.build).parameters
check("build() can be told to accept a truncated clone", "allow_shallow" in _sig)
check("build() refuses truncated clones by DEFAULT", _sig["allow_shallow"].default is False)

_src = inspect.getsource(build_price_history.build)
check("the guard runs before the git walk (nothing else can detect it later)",
      _src.index("history_is_truncated") < _src.index("daily_snapshots()"))
check("a refusal names the one-line fix", "--unshallow" in _src)
check("CI's fetch-depth is spelled out where someone will hit it",
      "fetch-depth: 0" in _src)
check("a permitted partial run is stamped, not silently emitted",
      "partialHistory" in _src)

# An absent flag must mean 'complete' — so the key may only ever appear as True.
check("partialHistory is never emitted as False (absent == complete)",
      "partialHistory\": False" not in _src and "'partialHistory': False" not in _src)

_rep = inspect.getsource(build_price_history.report)
check("the human summary surfaces a partial window", "partialHistory" in _rep)

# ── The sampling epoch (2026-08-06 cap raise) ────────────────────────────────
# The price epoch guards WHAT WE RECORD about a piece. This one guards WHICH
# PIECES WE LOOK AT, and it exists because until 2026-08-06 the scrape took the
# 60 most recently published items per store: 981 of 2,041 "disappearances" in
# the 2026-07-16 -> 2026-08-01 window were whole-brand rotation, 48%. Raising the
# cap makes thousands of always-for-sale pieces appear on one day, which must
# never read as newness, restock or a drop.
check("a sampling epoch is declared", len(SAMPLING_EPOCHS) >= 1)
check("the cap raise is the declared boundary", "2026-08-06" in SAMPLING_EPOCHS)
check("sampling epochs are sorted", SAMPLING_EPOCHS == sorted(SAMPLING_EPOCHS))
check("the day before the cap raise is sampling epoch 0",
      sampling_epoch_of("2026-08-05") == 0)
check("the cap-raise day is sampling epoch 1", sampling_epoch_of("2026-08-06") == 1)
check("a later day stays in epoch 1", sampling_epoch_of("2026-12-01") == 1)

check("the cap-raise day is settling", in_sampling_settle_window("2026-08-06"))
check("day+1 is still settling", in_sampling_settle_window("2026-08-07"))
check("day+2 is still settling", in_sampling_settle_window("2026-08-08"))
check("day+3 is comparable again", not in_sampling_settle_window("2026-08-09"))
check("a day before the raise is not settling", not in_sampling_settle_window("2026-08-05"))
check("the sampling settle window is exactly EPOCH_SETTLE_DAYS wide",
      sum(in_sampling_settle_window(d) for d in
          ("2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09")) == EPOCH_SETTLE_DAYS)

# The two epoch kinds are deliberately SEPARATE. Folding the cap raise into
# PRICE_EPOCHS would void every price comparison for 8,000 pieces whose prices
# did not move; leaving it out of SAMPLING_EPOCHS would leave arrivals exposed.
check("the cap raise is NOT a price epoch (no price moved)",
      "2026-08-06" not in PRICE_EPOCHS)
check("the country=US pin is NOT a sampling epoch (no population changed)",
      "2026-07-15" not in SAMPLING_EPOCHS)

# A window that spans the boundary cannot be compared at all.
check("a window spanning the cap raise is void",
      crosses_sampling_epoch("2026-08-01", "2026-08-10"))
check("a window entirely before it is fine",
      not crosses_sampling_epoch("2026-07-16", "2026-08-05"))
check("a window entirely after it is fine",
      not crosses_sampling_epoch("2026-08-09", "2026-09-01"))
check("direction does not matter", crosses_sampling_epoch("2026-08-10", "2026-08-01"))

# The blackout is what a consumer that never read the prose actually sees.
_days = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
         "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10"]
check("the blackout covers exactly the settling days",
      arrival_blackout(_days) == [[3, 5]])
check("an archive that never spans a sampling epoch has no blackout",
      arrival_blackout(["2026-07-16", "2026-07-17", "2026-07-18"]) == [])
check("blackout indices are into the day list, not dates",
      all(isinstance(i, int) for r in arrival_blackout(_days) for i in r))

_src = inspect.getsource(build_price_history.build)
check("the emitted file declares its sampling epochs", "samplingEpochs" in _src)
check("the emitted file carries the arrival blackout", "arrivalBlackout" in _src)
_rep = inspect.getsource(build_price_history.report)
check("the human summary shouts about a sampling boundary", "arrivalBlackout" in _rep)
check("the summary says the boundary is not an arrival", "NOT an arrival" in _rep)

# The docstring is load-bearing here: this file's whole method is that a reader
# is told what the number cannot mean before they are given the number.
_doc = build_price_history.__doc__ or ""
check("the rotation incident is written down, with its measurement",
      "981" in _doc and "48%" in _doc)
check("the docstring names what survives rotation", "available" in _doc)

if failures:
    print("FAIL — %d price-history guarantees broken:" % len(failures))
    for f in failures:
        print("   " + f)
    raise SystemExit(1)
print("OK — price history: all honesty guarantees hold")
