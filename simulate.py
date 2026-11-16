"""Run the three lab scenarios and print the resulting order state.

From the repository root:

    python3 simulate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from order_processor import (
    STATUS_BY_VERSION,
    DeliveryResult,
    FencingTokenIssuer,
    OrderEvent,
    OrderProcessor,
    OrderSnapshot,
    pipeline,
)


class Report:
    """Collect scenario lines and fail the run when a check is unmet."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, text: str = "") -> None:
        self.lines.append(text)

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.emit(f"check failed: {message}")
            raise AssertionError(message)
        self.emit(f"check: {message}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def describe(event: OrderEvent) -> str:
    return f"order_id={event.order_id} version={event.version} status={event.status}"


def format_state(snapshot: OrderSnapshot) -> str:
    status = snapshot.status or "(none)"
    pending = _csv(snapshot.pending_versions)
    missing = _csv(snapshot.missing_versions)
    return (
        f"order_id={snapshot.order_id} version={snapshot.version} status={status} "
        f"pending={pending} missing={missing} fencing_token={snapshot.fencing_token}"
    )


def _csv(values: tuple[int, ...]) -> str:
    if not values:
        return "(none)"
    return ", ".join(str(value) for value in values)


def show(report: Report, heading: str, result: DeliveryResult) -> None:
    report.emit(f">> {heading}")
    report.emit(f"   {result.reason}")
    report.emit(f"   {format_state(result.snapshot)}")
    report.emit("")


def section(report: Report, title: str, detail: str) -> None:
    report.emit("=" * 64)
    report.emit(title)
    report.emit(detail)
    report.emit("=" * 64)
    report.emit("")


def scenario_ordering(report: Report) -> None:
    """Deliver version 2, then version 1, then version 2 again."""
    section(
        report,
        "1. Versioned event ordering",
        "Arrival order: version 2, version 1, version 2 again",
    )
    processor = OrderProcessor()
    order_id = "ord-100"
    version_two = OrderEvent(order_id, 2, STATUS_BY_VERSION[2])
    version_one = OrderEvent(order_id, 1, STATUS_BY_VERSION[1])
    version_two_again = OrderEvent(order_id, 2, STATUS_BY_VERSION[2])

    first = processor.deliver(version_two)
    show(report, f"deliver {describe(version_two)}", first)

    second = processor.deliver(version_one)
    show(report, f"deliver {describe(version_one)}", second)

    third = processor.deliver(version_two_again)
    show(report, f"deliver {describe(version_two_again)} (again)", third)

    report.check(
        first.buffered
        and first.snapshot.version == 0
        and first.snapshot.status is None
        and first.snapshot.missing_versions == (1,)
        and first.snapshot.pending_versions == (2,),
        "version 2 stayed pending until version 1 arrived, and the order stayed at version 0",
    )
    report.check(
        [event.version for event in second.applied] == [1, 2]
        and [event.status for event in second.applied] == ["CREATED", "PAID"]
        and second.snapshot.version == 2
        and second.snapshot.status == "PAID",
        "version 1 CREATED and version 2 PAID were applied in that order",
    )
    report.check(
        third.duplicate
        and third.applied == ()
        and third.snapshot.version == 2
        and third.snapshot.status == "PAID",
        "the second copy of version 2 was ignored and the order stayed at PAID",
    )
    report.emit("")


def scenario_missing(report: Report) -> None:
    """Deliver only a later version and leave the earlier ones missing."""
    section(
        report,
        "2. Missing event test",
        "Sent only version 3. Versions 1 and 2 were never sent.",
    )
    processor = OrderProcessor()
    event = OrderEvent("ord-missing", 3, STATUS_BY_VERSION[3])
    result = processor.deliver(event)
    show(report, f"deliver {describe(event)}", result)

    report.check(
        result.snapshot.version == 0 and result.snapshot.status is None,
        "the order stayed at version 0 with no status, so it did not jump to SHIPPED",
    )
    report.check(
        result.buffered
        and result.snapshot.pending_versions == (3,)
        and result.snapshot.missing_versions == (1, 2),
        "version 3 is recorded as pending and versions 1 and 2 are recorded as missing",
    )
    report.emit("")


def scenario_stale_worker(report: Report) -> None:
    """Let the newer fencing token write, and reject the older token."""
    section(
        report,
        "3. Stale worker test",
        "Two workers update ord-fence. The later worker receives the higher token.",
    )
    issuer = FencingTokenIssuer()
    token_a = issuer.issue("worker-a")
    token_b = issuer.issue("worker-b")
    report.emit(f"issued fencing token {token_a} to worker-a")
    report.emit(f"issued fencing token {token_b} to worker-b")
    report.emit(f"worker-b holds the newer token ({token_b} > {token_a})")
    report.emit("")

    processor = OrderProcessor()
    order_id = "ord-fence"
    created = OrderEvent(order_id, 1, STATUS_BY_VERSION[1])
    paid = OrderEvent(order_id, 2, STATUS_BY_VERSION[2])
    shipped = OrderEvent(order_id, 3, STATUS_BY_VERSION[3])

    first = processor.deliver(created, fencing_token=token_a)
    show(
        report,
        f"worker-a token={token_a} deliver {describe(created)}",
        first,
    )

    second = processor.deliver(paid, fencing_token=token_b)
    show(
        report,
        f"worker-b token={token_b} deliver {describe(paid)}",
        second,
    )

    third = processor.deliver(shipped, fencing_token=token_a)
    show(
        report,
        f"worker-a token={token_a} deliver {describe(shipped)}",
        third,
    )

    report.check(
        token_b > token_a
        and first.snapshot.status == "CREATED"
        and first.snapshot.fencing_token == token_a,
        "worker-a updated the order while token 1 was the newest token",
    )
    report.check(
        second.snapshot.version == 2
        and second.snapshot.status == "PAID"
        and second.snapshot.fencing_token == token_b
        and not second.stale,
        "worker-b, holding the newer token, advanced the order to PAID",
    )
    report.check(
        third.stale
        and third.applied == ()
        and third.snapshot.version == 2
        and third.snapshot.status == "PAID"
        and third.snapshot.fencing_token == token_b,
        "worker-a, holding the older token, was rejected and the order stayed at PAID",
    )
    report.emit("")


def run() -> str:
    report = Report()
    report.emit("Distributed order processing")
    report.emit("Events may arrive late, out of order, or more than once.")
    report.emit(f"Pipeline: {pipeline()}")
    report.emit("")
    scenario_ordering(report)
    scenario_missing(report)
    scenario_stale_worker(report)
    report.emit("All scenario checks passed.")
    return report.text()


def main() -> None:
    report = run()
    sys.stdout.write(report)
    sys.stdout.flush()
    evidence = Path(__file__).resolve().parent / "evidence" / "demo-output.txt"
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text(report)
    print(f"saved {evidence.relative_to(Path(__file__).resolve().parent)}", file=sys.stderr)


if __name__ == "__main__":
    main()
