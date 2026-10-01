from dataclasses import dataclass
from datetime import datetime, timezone
from ..core.intervals import Interval, merge

# Metering policies for overlapping environment segments. The choice is explicit
# because it decides whether overlap is shared or charged per source segment.
UNION = "union"        # merge segments into their union coverage; each instant is metered
                       # once, at the highest multiplier of any covering segment (conservative)
PRESERVE = "preserve"  # keep source segments; every segment meters its own full span, so an
                       # overlapping window is charged once per segment covering it

@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    ambient_c: float
    door_fraction: float = 0.0

def _checked(segments):
    rows = []
    for s in segments:
        a = s.start.astimezone(timezone.utc)
        b = s.end.astimezone(timezone.utc)
        if b <= a: raise ValueError("bad segment")
        if not 0 <= s.door_fraction <= 1: raise ValueError("door fraction")
        rows.append((a, b, s))
    return sorted(rows, key=lambda r: (r[0], r[1]))

def _mult(s, temp_factor, door_factor):
    return 1 + max(0.0, s.ambient_c - 20) * temp_factor + s.door_fraction * door_factor

def coverage(segments):
    """Canonical merged coverage of the segments. Consumption metering and ledger
    deduction both derive from this scope so they can never disagree."""
    return merge([Interval(a, b) for a, b, _ in _checked(segments)])

def consumed(initial, segments, base_rate, temp_factor, door_factor, policy=UNION):
    if min(initial, base_rate) < 0: raise ValueError("invalid mass/rate")
    rows = _checked(segments)
    if policy == UNION:
        hours = _union_multiplier_hours(rows, temp_factor, door_factor)
    elif policy == PRESERVE:
        hours = sum(_mult(s, temp_factor, door_factor) * (b - a).total_seconds() / 3600
                    for a, b, s in rows)
    else:
        raise ValueError("unknown metering policy")
    return min(initial, base_rate * hours)

def _union_multiplier_hours(rows, temp_factor, door_factor):
    # Sweep the union coverage: between consecutive segment boundaries the covering
    # set is constant, so each instant is metered once at the highest covering
    # multiplier. Gaps between segments contribute nothing.
    points = sorted({p for a, b, _ in rows for p in (a, b)})
    total = 0.0
    for lo, hi in zip(points, points[1:]):
        covering = [_mult(s, temp_factor, door_factor) for a, b, s in rows if a <= lo and b >= hi]
        if covering:
            total += max(covering) * (hi - lo).total_seconds() / 3600
    return total

def remaining(initial, segments, base_rate, temp_factor, door_factor, policy=UNION):
    return max(0.0, initial - consumed(initial, segments, base_rate, temp_factor, door_factor, policy))
