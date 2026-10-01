import unittest
from datetime import datetime, timezone
from decimal import Decimal

from coldledger.packaging.dry_ice import (
    Segment, merge_coverage, coverage_hours, consumed, remaining,
    UNION_CONSERVATIVE,
)
from coldledger.packaging.dryice_ledger import DryIceLedger
from coldledger.packaging.ledger import PackagingLedger
from coldledger.packaging.reconciliation import settle_consumption

UTC = timezone.utc
T = lambda h, m=0: datetime(2026, 10, 1, h, m, tzinfo=UTC)


class CoverageMergeTests(unittest.TestCase):
    def test_disjoint_segments_preserved(self):
        segs = [Segment(T(8), T(9), 20.0), Segment(T(10), T(11), 20.0)]
        merged = merge_coverage(segs)
        self.assertEqual([(s.start, s.end) for s in merged],
                         [(T(8), T(9)), (T(10), T(11))])
        self.assertEqual(coverage_hours(segs), 2.0)

    def test_identical_overlap_counted_once(self):
        segs = [Segment(T(8), T(12), 20.0), Segment(T(8), T(12), 20.0)]
        self.assertEqual(coverage_hours(segs), 4.0)

    def test_partial_overlap_uses_union(self):
        # [08,11] U [10,12] = [08,12] -> 4h, never 5h
        segs = [Segment(T(8), T(11), 20.0), Segment(T(10), T(12), 20.0)]
        merged = merge_coverage(segs)
        self.assertEqual([(s.start, s.end) for s in merged],
                         [(T(8), T(10)), (T(10), T(11)), (T(11), T(12))])
        self.assertEqual(coverage_hours(segs), 4.0)

    def test_contained_segment_does_not_double_count(self):
        segs = [Segment(T(8), T(12), 20.0), Segment(T(9), T(10), 20.0)]
        self.assertEqual(coverage_hours(segs), 4.0)

    def test_overlap_takes_worst_case_conditions(self):
        segs = [Segment(T(8), T(12), 20.0, 0.0),
                Segment(T(10), T(12), 30.0, 0.5)]
        merged = merge_coverage(segs)
        # slices: [8,10] mild, [10,12] worst-case
        self.assertEqual(merged[0].ambient_c, 20.0)
        self.assertEqual(merged[0].door_fraction, 0.0)
        self.assertEqual(merged[1].ambient_c, 30.0)
        self.assertEqual(merged[1].door_fraction, 0.5)

    def test_sources_retained_for_traceability_only(self):
        segs = [Segment(T(8), T(11), 20.0, source="A"),
                Segment(T(10), T(12), 30.0, source="B")]
        overlap = [s for s in merge_coverage(segs) if s.start == T(10)][0]
        self.assertEqual(set(overlap.sources), {"A", "B"})

    def test_worst_case_factors_taken_independently(self):
        # hot-but-closed vs mild-but-doors-open: worst case combines both
        segs = [Segment(T(8), T(10), 40.0, 0.0),
                Segment(T(8), T(10), 20.0, 0.5)]
        overlap = merge_coverage(segs)
        self.assertEqual(len(overlap), 1)
        self.assertEqual(overlap[0].ambient_c, 40.0)
        self.assertEqual(overlap[0].door_fraction, 0.5)

    def test_unknown_policy_rejected(self):
        with self.assertRaises(ValueError):
            merge_coverage([Segment(T(8), T(9), 20.0)], policy="source")

    def test_invalid_segments(self):
        with self.assertRaises(ValueError):
            merge_coverage([Segment(T(9), T(8), 20.0)])
        with self.assertRaises(ValueError):
            merge_coverage([Segment(T(8), T(9), 20.0, door_fraction=1.5)])


class ConsumedTests(unittest.TestCase):
    def test_overlap_not_double_metered(self):
        segs = [Segment(T(8), T(12), 20.0), Segment(T(8), T(12), 20.0)]
        # base_rate=2 kg/h at 20C (multiplier 1) -> 8kg once, not 16kg
        self.assertEqual(consumed(100, segs, 2.0, 0.01, 0.0), 8.0)

    def test_partial_overlap_matches_union(self):
        segs = [Segment(T(8), T(11), 20.0), Segment(T(10), T(12), 20.0)]
        self.assertEqual(consumed(100, segs, 2.0, 0.0, 0.0), 8.0)

    def test_gap_between_segments_not_consumed(self):
        segs = [Segment(T(8), T(9), 20.0), Segment(T(10), T(11), 20.0)]
        self.assertEqual(consumed(100, segs, 2.0, 0.0, 0.0), 4.0)

    def test_conservative_overlap_rate(self):
        # [8,12] mild (mult 1), [10,12] hot 30C (mult 1+10*.1=2)
        segs = [Segment(T(8), T(12), 20.0), Segment(T(10), T(12), 30.0)]
        # 2h * 1kg/h * 1 + 2h * 1kg/h * 2 = 6kg
        self.assertEqual(consumed(100, segs, 1.0, 0.1, 0.0), 6.0)

    def test_consumed_capped_at_initial(self):
        segs = [Segment(T(8), T(12), 40.0)]
        self.assertEqual(consumed(3, segs, 2.0, 0.1, 0.0), 3)
        self.assertEqual(remaining(3, segs, 2.0, 0.1, 0.0), 0)

    def test_empty_segments(self):
        self.assertEqual(consumed(10, [], 1.0, 0.0, 0.0), 0.0)
        self.assertEqual(remaining(10, [], 1.0, 0.0, 0.0), 10.0)


class DryIceLedgerTests(unittest.TestCase):
    def test_balance_reflects_consumption(self):
        led = DryIceLedger()
        led.post("kit-1", "ref-1", Decimal("10"), "charged")
        led.post("kit-1", "ref-2", Decimal("-4"), "consumed")
        self.assertEqual(led.balance("kit-1"), Decimal("6"))

    def test_reversal_restores_balance(self):
        led = DryIceLedger()
        row = led.post("kit-1", "ref-1", Decimal("10"), "charged")
        led.reverse(row.sequence, "rev-1", "oops")
        self.assertEqual(led.balance("kit-1"), Decimal("0"))


class SettleConsumptionTests(unittest.TestCase):
    def _segments(self):
        return [Segment(T(8), T(11), 20.0, source="dock"),
                Segment(T(10), T(12), 30.0, source="ramp")]

    def test_both_ledgers_share_coverage_quantity(self):
        di, pk = DryIceLedger(), PackagingLedger()
        di.post("di", "seed", Decimal("100"), "stock")
        pk.post("pk", "seed", Decimal("100"), "stock")
        charge = settle_consumption(
            segments=self._segments(),
            initial=100, base_rate=2.0, temp_factor=0.1, door_factor=0.0,
            dryice_ledger=di, packaging_ledger=pk,
            dryice_account="di", packaging_account="pk",
            reference="ship-1", reason="transit",
        )
        # union coverage: [8,10] mild (2h*2*1=4), [10,12] hot (2h*2*2=8) = 12
        self.assertEqual(charge.covered_hours, 4.0)
        self.assertEqual(charge.quantity, Decimal("12"))
        self.assertEqual(di.balance("di"), Decimal("88"))
        self.assertEqual(pk.balance("pk"), Decimal("88"))
        self.assertEqual(charge.dryice_entry.quantity, Decimal("-12"))
        self.assertEqual(charge.packaging_entry.quantity, Decimal("-12"))
        self.assertEqual(len(charge.coverage), 3)

    def test_idempotent_reference_rejected_on_both(self):
        di, pk = DryIceLedger(), PackagingLedger()
        kwargs = dict(segments=self._segments(), initial=100,
                      base_rate=2.0, temp_factor=0.1, door_factor=0.0,
                      dryice_ledger=di, packaging_ledger=pk,
                      dryice_account="di", packaging_account="pk",
                      reference="ship-1", reason="transit")
        settle_consumption(**kwargs)
        with self.assertRaises(ValueError):
            settle_consumption(**kwargs)
        # exactly one charge on each ledger
        self.assertEqual(len(di.entries("di")), 1)
        self.assertEqual(len(pk.entries("pk")), 1)

    def test_packaging_failure_rolls_back_dryice(self):
        class ExplodingPackaging(PackagingLedger):
            def post(self, account, reference, quantity, reason):
                raise RuntimeError("packaging store down")

        di, pk = DryIceLedger(), ExplodingPackaging()
        di.post("di", "seed", Decimal("100"), "stock")
        with self.assertRaises(RuntimeError):
            settle_consumption(
                segments=self._segments(), initial=100,
                base_rate=2.0, temp_factor=0.1, door_factor=0.0,
                dryice_ledger=di, packaging_ledger=pk,
                dryice_account="di", packaging_account="pk",
                reference="ship-2", reason="transit",
            )
        # deduction fully compensated, ledger back to seeded state
        self.assertEqual(di.balance("di"), Decimal("100"))
        rows = di.entries("di")
        self.assertEqual(len(rows), 3)  # seed, debit, compensating reversal
        self.assertEqual(rows[1].reference, "ship-2")
        self.assertEqual(rows[2].reversal_of, rows[1].sequence)
        di.audit()

    def test_zero_consumption_posts_nothing(self):
        di, pk = DryIceLedger(), PackagingLedger()
        charge = settle_consumption(
            segments=[], initial=10, base_rate=1.0, temp_factor=0.0, door_factor=0.0,
            dryice_ledger=di, packaging_ledger=pk,
            dryice_account="di", packaging_account="pk",
            reference="ship-3", reason="idle",
        )
        self.assertEqual(charge.quantity, Decimal("0"))
        self.assertIsNone(charge.dryice_entry)
        self.assertEqual(len(di.entries()), 0)
        self.assertEqual(len(pk.entries()), 0)


if __name__ == "__main__":
    unittest.main()
