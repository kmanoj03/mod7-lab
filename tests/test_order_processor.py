"""Checks for version order, duplicates, missing versions, and fencing tokens."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_processor import (  # noqa: E402
    STATUS_BY_VERSION,
    FencingTokenIssuer,
    OrderEvent,
    OrderProcessor,
)


def event(order_id: str, version: int) -> OrderEvent:
    return OrderEvent(order_id, version, STATUS_BY_VERSION[version])


class OrderProcessorTest(unittest.TestCase):
    def test_out_of_order_delivery_applies_in_version_order(self) -> None:
        processor = OrderProcessor()
        early = processor.deliver(event("ord-100", 2))
        self.assertTrue(early.buffered)
        self.assertEqual(early.snapshot.version, 0)
        self.assertIsNone(early.snapshot.status)
        self.assertEqual(early.snapshot.pending_versions, (2,))
        self.assertEqual(early.snapshot.missing_versions, (1,))

        filled = processor.deliver(event("ord-100", 1))
        self.assertEqual([item.version for item in filled.applied], [1, 2])
        self.assertEqual([item.status for item in filled.applied], ["CREATED", "PAID"])
        self.assertEqual(filled.snapshot.version, 2)
        self.assertEqual(filled.snapshot.status, "PAID")
        self.assertEqual(filled.snapshot.pending_versions, ())

        duplicate = processor.deliver(event("ord-100", 2))
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.applied, ())
        self.assertEqual(processor.get("ord-100"), filled.snapshot)

    def test_duplicate_of_a_buffered_version_is_stored_once(self) -> None:
        processor = OrderProcessor()
        processor.deliver(event("ord-100", 2))
        duplicate = processor.deliver(event("ord-100", 2))
        self.assertTrue(duplicate.duplicate)
        self.assertIn("already buffered", duplicate.reason)
        self.assertEqual(duplicate.snapshot.pending_versions, (2,))

        processor.deliver(event("ord-100", 1))
        self.assertEqual(processor.get("ord-100").version, 2)
        self.assertEqual(processor.get("ord-100").pending_versions, ())

    def test_buffered_chain_drains_when_the_gap_fills(self) -> None:
        processor = OrderProcessor()
        processor.deliver(event("ord-200", 3))
        partial = processor.deliver(event("ord-200", 1))
        self.assertEqual(partial.snapshot.version, 1)
        self.assertEqual(partial.snapshot.status, "CREATED")
        self.assertEqual(partial.snapshot.pending_versions, (3,))
        self.assertEqual(partial.snapshot.missing_versions, (2,))

        filled = processor.deliver(event("ord-200", 2))
        self.assertEqual([item.version for item in filled.applied], [2, 3])
        self.assertEqual(filled.snapshot.version, 3)
        self.assertEqual(filled.snapshot.status, "SHIPPED")
        self.assertEqual(filled.snapshot.pending_versions, ())
        self.assertEqual(filled.snapshot.missing_versions, ())

    def test_missing_later_version_does_not_advance_the_order(self) -> None:
        processor = OrderProcessor()
        result = processor.deliver(event("ord-missing", 3))
        self.assertEqual(result.snapshot.version, 0)
        self.assertIsNone(result.snapshot.status)
        self.assertEqual(result.snapshot.pending_versions, (3,))
        self.assertEqual(result.snapshot.missing_versions, (1, 2))
        self.assertTrue(result.buffered)
        self.assertFalse(result.duplicate)

    def test_orders_do_not_share_pending_state(self) -> None:
        processor = OrderProcessor()
        processor.deliver(event("ord-a", 2))
        processor.deliver(event("ord-b", 1))
        self.assertEqual(processor.get("ord-a").version, 0)
        self.assertEqual(processor.get("ord-b").status, "CREATED")
        self.assertIsNone(processor.get("unknown"))

    def test_newer_token_updates_and_older_token_is_rejected(self) -> None:
        processor = OrderProcessor()
        first = processor.deliver(event("ord-fence", 1), fencing_token=1)
        self.assertEqual(first.snapshot.status, "CREATED")
        self.assertEqual(first.snapshot.fencing_token, 1)

        second = processor.deliver(event("ord-fence", 2), fencing_token=2)
        self.assertFalse(second.stale)
        self.assertEqual(second.snapshot.status, "PAID")
        self.assertEqual(second.snapshot.fencing_token, 2)

        rejected = processor.deliver(event("ord-fence", 3), fencing_token=1)
        self.assertTrue(rejected.stale)
        self.assertEqual(rejected.applied, ())
        self.assertIn("rejected stale worker", rejected.reason)
        self.assertEqual(processor.get("ord-fence"), second.snapshot)

    def test_same_token_can_keep_writing(self) -> None:
        processor = OrderProcessor()
        processor.deliver(event("ord-fence", 1), fencing_token=2)
        result = processor.deliver(event("ord-fence", 2), fencing_token=2)
        self.assertFalse(result.stale)
        self.assertEqual(result.snapshot.version, 2)
        self.assertEqual(result.snapshot.fencing_token, 2)

    def test_duplicate_from_a_newer_token_still_moves_the_fence(self) -> None:
        processor = OrderProcessor()
        processor.deliver(event("ord-fence", 1), fencing_token=1)
        duplicate = processor.deliver(event("ord-fence", 1), fencing_token=4)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.snapshot.fencing_token, 4)
        self.assertEqual(duplicate.snapshot.version, 1)

        rejected = processor.deliver(event("ord-fence", 2), fencing_token=1)
        self.assertTrue(rejected.stale)
        self.assertEqual(processor.get("ord-fence").version, 1)
        self.assertEqual(processor.get("ord-fence").fencing_token, 4)

    def test_stale_worker_cannot_fill_a_gap(self) -> None:
        processor = OrderProcessor()
        processor.deliver(event("ord-fence", 1), fencing_token=1)
        held = processor.deliver(event("ord-fence", 3), fencing_token=2)
        self.assertEqual(held.snapshot.version, 1)
        self.assertEqual(held.snapshot.pending_versions, (3,))
        self.assertEqual(held.snapshot.missing_versions, (2,))

        rejected = processor.deliver(event("ord-fence", 2), fencing_token=1)
        self.assertTrue(rejected.stale)
        self.assertEqual(processor.get("ord-fence").version, 1)
        self.assertEqual(processor.get("ord-fence").pending_versions, (3,))

        filled = processor.deliver(event("ord-fence", 2), fencing_token=2)
        self.assertEqual([item.version for item in filled.applied], [2, 3])
        self.assertEqual(filled.snapshot.status, "SHIPPED")

    def test_token_is_required_after_a_fence_is_set(self) -> None:
        processor = OrderProcessor()
        processor.deliver(event("ord-fence", 1), fencing_token=1)
        with self.assertRaises(ValueError):
            processor.deliver(event("ord-fence", 2))

    def test_issuer_hands_out_increasing_tokens(self) -> None:
        issuer = FencingTokenIssuer()
        first = issuer.issue("worker-a")
        second = issuer.issue("worker-b")
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)
        self.assertLess(first, second)

    def test_invalid_event_and_token_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OrderEvent("", 1, "CREATED")
        with self.assertRaises(ValueError):
            OrderEvent("ord-1", 0, "CREATED")
        with self.assertRaises(ValueError):
            OrderEvent("ord-1", 4, "SHIPPED")
        with self.assertRaises(ValueError):
            OrderEvent("ord-1", 2, "CREATED")
        with self.assertRaises(ValueError):
            OrderEvent("ord-1", True, "CREATED")  # type: ignore[arg-type]

        processor = OrderProcessor()
        update = event("ord-1", 1)
        with self.assertRaises(ValueError):
            processor.deliver(update, fencing_token=0)
        with self.assertRaises(ValueError):
            processor.deliver(update, fencing_token=True)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
