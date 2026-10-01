from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

# Metering policy for overlapping environmental segments:
# UNION_CONSERVATIVE ("按并集保守计量") measures every covered instant exactly
# once; where several source segments overlap, the instant is rated with the
# worst-case (highest ambient temperature / highest door-open fraction) of the
# sources covering it. Source segments are retained on each merged slice solely
# for traceability and never participate in the measurement again.
UNION_CONSERVATIVE = "union-conservative"
_UNION = "union"  # accepted alias
_POLICIES = frozenset({UNION_CONSERVATIVE, _UNION})


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    ambient_c: float
    door_fraction: float = 0.0
    source: str | None = None
    sources: tuple[str, ...] = field(default_factory=tuple)

    def source_ids(self, index: int) -> frozenset[str]:
        if self.sources:
            return frozenset(self.sources)
        return frozenset({self.source if self.source is not None else f"segment-{index}"})


def _norm(rows: Iterable[Segment]) -> list[tuple[datetime, datetime, float, float, frozenset[str]]]:
    out = []
    for i, s in enumerate(rows):
        if not 0 <= s.door_fraction <= 1:
            raise ValueError("door fraction")
        a = s.start.astimezone(timezone.utc)
        b = s.end.astimezone(timezone.utc)
        if b <= a:
            raise ValueError("bad segment")
        out.append((a, b, float(s.ambient_c), float(s.door_fraction), s.source_ids(i)))
    return out


def merge_coverage(segments: Iterable[Segment], policy: str = UNION_CONSERVATIVE) -> list[Segment]:
    """Merge possibly overlapping segments into a disjoint union coverage.

    Each returned slice covers one maximal run of instants with a stable set of
    active source segments and carries the conservative (worst-case) ambient
    temperature and door fraction of those sources, plus the ids of every
    source segment contributing to it.
    """
    if policy not in _POLICIES:
        raise ValueError(f"unknown overlap policy {policy!r}; use UNION_CONSERVATIVE")
    rows = _norm(segments)
    if not rows:
        return []
    events: list[tuple[datetime, int, int]] = []
    for idx, (a, b, _, _, _) in enumerate(rows):
        events.append((a, 0, idx))  # starts first at a shared boundary
        events.append((b, 1, idx))
    events.sort(key=lambda e: (e[0], e[1]))

    slices: list[Segment] = []
    active: set[int] = set()
    cursor = events[0][0]
    i = 0
    while i < len(events):
        at = events[i][0]
        if active and at > cursor:
            # Conservative worst case: the depletion multiplier is additive in
            # ambient heat and door exposure, so each factor is taken at its
            # independent maximum across the active source segments (the two
            # maxima may come from different sources).
            ambient = max(rows[k][2] for k in active)
            door = max(rows[k][3] for k in active)
            # union of every source covering this slice (traceability only)
            covered = frozenset().union(*(rows[k][4] for k in active))
            slices.append(Segment(cursor, at, ambient, door, sources=tuple(sorted(covered))))
        while i < len(events) and events[i][0] == at:
            _, kind, idx = events[i]
            if kind == 0:
                active.add(idx)
            else:
                active.discard(idx)
            i += 1
        cursor = at
    return slices


def coverage_hours(segments: Iterable[Segment], policy: str = UNION_CONSERVATIVE) -> float:
    return sum(
        (s.end - s.start).total_seconds() / 3600
        for s in merge_coverage(segments, policy)
    )


def consumed(initial, segments, base_rate, temp_factor, door_factor,
             policy: str = UNION_CONSERVATIVE):
    """Dry-ice mass consumed over the union coverage of ``segments``.

    Overlapping instants are measured once at the worst-case rate, so callers
    can feed raw source segments without pre-merging them.
    """
    if min(initial, base_rate) < 0:
        raise ValueError("invalid mass/rate")
    total = 0.0
    for s in merge_coverage(segments, policy):
        hours = (s.end - s.start).total_seconds() / 3600
        mult = 1 + max(0, s.ambient_c - 20) * temp_factor + s.door_fraction * door_factor
        total += base_rate * mult * hours
    return min(initial, total)


def remaining(initial, segments, base_rate, temp_factor, door_factor,
              policy: str = UNION_CONSERVATIVE):
    return max(0.0, initial - consumed(initial, segments, base_rate,
                                       temp_factor, door_factor, policy))
