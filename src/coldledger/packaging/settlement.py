from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from ..core.intervals import Interval
from .dry_ice import UNION, consumed, coverage
from .dryice_ledger import DryIceLedger
from .dryice_ledger import Entry as DryIceEntry
from .ledger import PackagingLedger
from .ledger import Entry as PackagingEntry

@dataclass(frozen=True)
class Settlement:
    reference: str
    kilograms: Decimal
    coverage: tuple[Interval, ...]
    dryice_entry: DryIceEntry
    packaging_entry: PackagingEntry

class SettlementError(RuntimeError):
    """The packaging post failed and the compensating dry-ice reversal failed too.
    The books now diverge; posted_entry identifies the row needing manual repair."""
    def __init__(self, reference: str, posted_entry: DryIceEntry, post_error: Exception, reversal_error: Exception):
        super().__init__(
            f"settlement {reference!r}: packaging post failed ({post_error!r}) and dry-ice "
            f"rollback failed ({reversal_error!r}); manual reconciliation required")
        self.reference = reference
        self.posted_entry = posted_entry
        self.post_error = post_error
        self.reversal_error = reversal_error

def settle(dryice: DryIceLedger, packaging: PackagingLedger, account: str, reference: str,
           initial, segments, base_rate, temp_factor, door_factor, *,
           policy: str = UNION, reason: str = "dry-ice consumption") -> Settlement:
    """Deduct one dry-ice consumption from both ledgers over the same merged coverage.

    The quantity comes from coverage(segments) via consumed(...), so segment merging,
    temperature consumption and ledger deduction share one scope. If the packaging
    post fails after the dry-ice post succeeded, the dry-ice entry is reversed: a
    partial failure never leaves exactly one ledger charged. A retry of a failed
    settlement needs a fresh reference, since posted references stay reserved.
    """
    scope = coverage(segments)
    kg = Decimal(str(consumed(initial, segments, base_rate, temp_factor, door_factor, policy)))
    if kg == 0:
        raise ValueError("zero consumption settlement")
    ice = dryice.post(account, reference + ":dryice", -kg, reason)
    try:
        pack = packaging.post(account, reference + ":packaging", -kg, reason)
    except Exception as post_error:
        try:
            dryice.reverse(ice.sequence, reference + ":dryice:rollback", "rollback: " + reason)
        except Exception as reversal_error:
            raise SettlementError(reference, ice, post_error, reversal_error) from post_error
        raise post_error
    return Settlement(reference, kg, tuple(scope), ice, pack)
