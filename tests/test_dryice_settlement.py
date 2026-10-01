import unittest
from datetime import datetime, timezone
from decimal import Decimal
from coldledger.packaging.dry_ice import PRESERVE, UNION, Segment, consumed, coverage, remaining
from coldledger.packaging.dryice_ledger import DryIceLedger
from coldledger.packaging.ledger import PackagingLedger
from coldledger.packaging.settlement import SettlementError, settle

UTC = timezone.utc

def at(hour):
    return datetime(2026, 1, 1, hour, tzinfo=UTC)

SEGS = [Segment(at(0), at(2), 20.0), Segment(at(3), at(4), 20.0)]

class MeteringTests(unittest.TestCase):
    def test_overlap_counted_once(self):
        segs = [Segment(at(0), at(2), 20.0), Segment(at(1), at(3), 20.0)]
        self.assertAlmostEqual(consumed(10, segs, 0.5, 0.0, 0.0), 1.5)

    def test_gap_between_segments_not_metered(self):
        segs = [Segment(at(0), at(1), 20.0), Segment(at(2), at(3), 20.0)]
        self.assertAlmostEqual(consumed(10, segs, 0.5, 0.0, 0.0), 1.0)

    def test_contained_segment_adds_nothing(self):
        segs = [Segment(at(0), at(4), 20.0), Segment(at(1), at(2), 20.0)]
        self.assertAlmostEqual(consumed(10, segs, 0.5, 0.0, 0.0), 2.0)

    def test_overlap_uses_conservative_multiplier(self):
        segs = [Segment(at(0), at(2), 20.0), Segment(at(1), at(3), 30.0)]
        # [0,1) x1.0, [1,2) max(1.0, 2.0), [2,3) x2.0 -> 5h at base 0.5
        self.assertAlmostEqual(consumed(10, segs, 0.5, 0.1, 0.0), 2.5)

    def test_preserve_meters_each_source_segment(self):
        segs = [Segment(at(0), at(2), 20.0), Segment(at(1), at(3), 20.0)]
        self.assertAlmostEqual(consumed(10, segs, 0.5, 0.0, 0.0, PRESERVE), 2.0)

    def test_unknown_policy_rejected(self):
        with self.assertRaises(ValueError):
            consumed(10, SEGS, 0.5, 0.0, 0.0, "average")

    def test_coverage_is_merged_union(self):
        segs = [Segment(at(0), at(2), 20.0), Segment(at(1), at(3), 20.0), Segment(at(5), at(6), 20.0)]
        iv = coverage(segs)
        self.assertEqual([(i.start, i.end) for i in iv], [(at(0), at(3)), (at(5), at(6))])

    def test_validation_kept(self):
        with self.assertRaises(ValueError):
            consumed(10, [Segment(at(1), at(1), 20.0)], 0.5, 0.0, 0.0)
        with self.assertRaises(ValueError):
            consumed(10, [Segment(at(0), at(1), 20.0, 1.5)], 0.5, 0.0, 0.0)
        with self.assertRaises(ValueError):
            consumed(-1, SEGS, 0.5, 0.0, 0.0)

    def test_capped_by_initial_and_remaining(self):
        segs = [Segment(at(0), at(4), 40.0)]
        self.assertEqual(consumed(1.0, segs, 0.5, 0.1, 0.0), 1.0)
        self.assertEqual(remaining(1.0, segs, 0.5, 0.1, 0.0), 0.0)

class LedgerConsistencyTests(unittest.TestCase):
    def test_reversal_reduces_balance(self):
        d = DryIceLedger()
        e = d.post("acc", "r1", Decimal("5"), "load")
        d.reverse(e.sequence, "r1:rev", "undo")
        self.assertEqual(d.balance("acc"), Decimal("0"))

    def test_transfer_debit_reduces_balance(self):
        d = DryIceLedger()
        d.post("acc", "r1", Decimal("5"), "load")
        d.transfer("acc", "other", Decimal("2"), "t1", "move")
        self.assertEqual(d.balance("acc"), Decimal("3"))
        self.assertEqual(d.balance("other"), Decimal("2"))
        with self.assertRaises(ValueError):
            d.transfer("acc", "other", Decimal("4"), "t2", "move")

    def test_ledgers_agree_on_same_postings(self):
        d, p = DryIceLedger(), PackagingLedger()
        for book in (d, p):
            e = book.post("acc", "r1", Decimal("5"), "load")
            book.post("acc", "r2", Decimal("-2"), "consumption")
            book.reverse(e.sequence, "r1:rev", "undo")
        self.assertEqual(d.balance("acc"), p.balance("acc"))

class SettlementTests(unittest.TestCase):
    def test_posts_both_ledgers_over_same_coverage(self):
        d, p = DryIceLedger(), PackagingLedger()
        s = settle(d, p, "acc", "ref1", 10, SEGS, 0.5, 0.0, 0.0)
        self.assertEqual(s.kilograms, Decimal("1.5"))
        self.assertEqual(d.balance("acc"), Decimal("-1.5"))
        self.assertEqual(p.balance("acc"), Decimal("-1.5"))
        self.assertEqual([(i.start, i.end) for i in s.coverage], [(at(0), at(2)), (at(3), at(4))])
        self.assertEqual(s.dryice_entry.quantity, s.packaging_entry.quantity)

    def test_duplicate_reference_rejected_without_double_charge(self):
        d, p = DryIceLedger(), PackagingLedger()
        settle(d, p, "acc", "ref1", 10, SEGS, 0.5, 0.0, 0.0)
        with self.assertRaises(ValueError):
            settle(d, p, "acc", "ref1", 10, SEGS, 0.5, 0.0, 0.0)
        self.assertEqual(d.balance("acc"), Decimal("-1.5"))
        self.assertEqual(p.balance("acc"), Decimal("-1.5"))

    def test_partial_failure_rolls_back_both_ledgers(self):
        d, p = DryIceLedger(), PackagingLedger()
        p.post("acc", "ref1:packaging", Decimal("9"), "seed")
        with self.assertRaises(ValueError):
            settle(d, p, "acc", "ref1", 10, SEGS, 0.5, 0.0, 0.0)
        self.assertEqual(d.balance("acc"), Decimal("0"))
        self.assertEqual(p.balance("acc"), Decimal("9"))

    def test_failed_compensation_raises_settlement_error(self):
        d, p = DryIceLedger(), PackagingLedger()
        d.post("acc", "ref1:dryice:rollback", Decimal("1"), "seed")
        p.post("acc", "ref1:packaging", Decimal("9"), "seed")
        with self.assertRaises(SettlementError) as ctx:
            settle(d, p, "acc", "ref1", 10, SEGS, 0.5, 0.0, 0.0)
        self.assertEqual(ctx.exception.reference, "ref1")
        self.assertEqual(ctx.exception.posted_entry.quantity, Decimal("-1.5"))
        self.assertEqual(d.balance("acc"), Decimal("-0.5"))

    def test_zero_consumption_rejected(self):
        d, p = DryIceLedger(), PackagingLedger()
        with self.assertRaises(ValueError):
            settle(d, p, "acc", "ref1", 10, [], 0.5, 0.0, 0.0)

if __name__ == "__main__":
    unittest.main()
