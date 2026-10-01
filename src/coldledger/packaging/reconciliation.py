"""Cross-ledger settlement of dry-ice consumption.

The merged union coverage of the environmental segments is the single source
of truth: the thermal (dry-ice mass) usage and the deductions on both the
dry-ice ledger and the packaging ledger are computed from that one coverage
and posted with the same Decimal quantity in one all-or-nothing settlement.
Overlapping segments are metered union-conservatively; see
``coldledger.packaging.dry_ice`` for the policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .dry_ice import Segment, UNION_CONSERVATIVE, merge_coverage, consumed
from .dryice_ledger import DryIceLedger, Entry as DryIceEntry
from .ledger import PackagingLedger, Entry as PackagingEntry


@dataclass(frozen=True)
class ConsumptionCharge:
    reference: str
    reason: str
    coverage: tuple[Segment, ...]
    covered_hours: float
    quantity: Decimal
    dryice_account: str
    packaging_account: str
    dryice_entry: DryIceEntry | None
    packaging_entry: PackagingEntry | None


def _posted(ledger, account: str, reference: str) -> bool:
    return (account, reference) in ledger._refs


def settle_consumption(
    *,
    segments: Iterable[Segment],
    initial,
    base_rate,
    temp_factor,
    door_factor,
    dryice_ledger: DryIceLedger,
    packaging_ledger: PackagingLedger,
    dryice_account: str,
    packaging_account: str,
    reference: str,
    reason: str,
    policy: str = UNION_CONSERVATIVE,
    quantity: Decimal | None = None,
) -> ConsumptionCharge:
    """Measure consumption once over the union coverage and debit both ledgers.

    Either both ledger deductions land (same coverage, same quantity) or, on
    any partial failure, the first deduction is reversed and the original
    error propagates -- a single ledger can never be left holding the charge.
    The same ``reference`` is used on both ledgers, so retrying a settlement
    is idempotent: a re-post is rejected on both ledgers (and a half-posted
    retry is rolled back).
    """
    if not reference or not reason:
        raise ValueError("reference and reason required")

    coverage = tuple(merge_coverage(segments, policy))
    covered_hours = sum((s.end - s.start).total_seconds() / 3600 for s in coverage)
    used = consumed(initial, coverage, base_rate, temp_factor, door_factor, policy)
    q = Decimal(str(used)) if quantity is None else Decimal(str(quantity))
    if q < 0:
        raise ValueError("consumption quantity must not be negative")

    if q == 0:
        return ConsumptionCharge(reference, reason, coverage, covered_hours, q,
                                 dryice_account, packaging_account, None, None)

    # Best-effort preflight against both ledgers before touching either.
    if _posted(dryice_ledger, dryice_account, reference) or \
            _posted(packaging_ledger, packaging_account, reference):
        raise ValueError("duplicate idempotency reference")

    dry_entry = dryice_ledger.post(dryice_account, reference, -q, reason)
    try:
        pkg_entry = packaging_ledger.post(packaging_account, reference, -q, reason)
    except BaseException:
        # Never leave only the dry-ice ledger charged: reverse it and re-raise.
        try:
            dryice_ledger.reverse(
                dry_entry.sequence,
                reference + ":compensation",
                reason + " (packaging deduction failed; rolled back)",
            )
        except BaseException as compensation_error:  # pragma: no cover - defensive
            raise compensation_error from None
        raise
    return ConsumptionCharge(reference, reason, coverage, covered_hours, q,
                             dryice_account, packaging_account, dry_entry, pkg_entry)
