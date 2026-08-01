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
    PRICE_EPOCHS,
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

if failures:
    print("FAIL — %d price-history guarantees broken:" % len(failures))
    for f in failures:
        print("   " + f)
    raise SystemExit(1)
print("OK — price history: all honesty guarantees hold")
