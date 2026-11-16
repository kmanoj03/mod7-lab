"""Versioned order updates for a small distributed-system simulation.

An order moves CREATED -> PAID -> SHIPPED. Those statuses are versions 1, 2,
and 3. A version is applied only after every lower version is already applied.
An earlier arrival of a higher version stays in a pending buffer, and a version
that was already applied or already buffered is ignored.

A fencing token is optional until one has been presented for an order. After
that, a token greater than or equal to the highest token on the order may
update it. A smaller token is rejected and the order is left unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


STATUS_BY_VERSION: dict[int, str] = {
    1: "CREATED",
    2: "PAID",
    3: "SHIPPED",
}


def pipeline() -> str:
    """Return the status sequence in version order."""
    return " -> ".join(
        f"version {version} {status}" for version, status in STATUS_BY_VERSION.items()
    )


@dataclass(frozen=True)
class OrderEvent:
    """One update for an order. Status is fixed by the version number."""

    order_id: str
    version: int
    status: str

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise ValueError("order_id must be a non-empty string")
        expected = STATUS_BY_VERSION.get(self.version)
        if type(self.version) is not int or expected is None:
            allowed = ", ".join(str(version) for version in STATUS_BY_VERSION)
            raise ValueError(f"version must be one of {allowed}; got {self.version!r}")
        if self.status != expected:
            raise ValueError(
                f"version {self.version} must use status {expected}; got {self.status!r}"
            )


@dataclass(frozen=True)
class OrderSnapshot:
    """Observable state of one order after a delivery attempt."""

    order_id: str
    version: int
    status: str | None
    fencing_token: int
    pending_versions: tuple[int, ...]
    missing_versions: tuple[int, ...]


@dataclass(frozen=True)
class DeliveryResult:
    """What one delivery did, plus the order state afterward."""

    applied: tuple[OrderEvent, ...]
    duplicate: bool
    stale: bool
    buffered: bool
    reason: str
    snapshot: OrderSnapshot


@dataclass
class _Order:
    order_id: str
    version: int = 0
    status: str | None = None
    fencing_token: int = 0
    pending: dict[int, OrderEvent] = field(default_factory=dict)

    def missing_versions(self) -> tuple[int, ...]:
        """Versions between the applied prefix and the highest buffered one."""
        if not self.pending:
            return ()
        highest = max(self.pending)
        return tuple(
            version
            for version in range(self.version + 1, highest)
            if version not in self.pending
        )

    def snapshot(self) -> OrderSnapshot:
        return OrderSnapshot(
            order_id=self.order_id,
            version=self.version,
            status=self.status,
            fencing_token=self.fencing_token,
            pending_versions=tuple(sorted(self.pending)),
            missing_versions=self.missing_versions(),
        )


class FencingTokenIssuer:
    """Hand out 1, 2, 3, ... from a single issuer.

    A worker that acquires the order later receives a greater token and can
    fence out a worker that is still writing with an earlier one.
    """

    def __init__(self) -> None:
        self._next = 1

    def issue(self, worker_id: str) -> int:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        token = self._next
        self._next += 1
        return token


class OrderProcessor:
    """Apply order events in version order and enforce fencing tokens."""

    def __init__(self) -> None:
        self._orders: dict[str, _Order] = {}

    def get(self, order_id: str) -> OrderSnapshot | None:
        order = self._orders.get(order_id)
        if order is None:
            return None
        return order.snapshot()

    def deliver(
        self, event: OrderEvent, fencing_token: int | None = None
    ) -> DeliveryResult:
        """Accept one update.

        The update is applied when its version is exactly one greater than the
        last applied version. Any later versions already buffered are applied
        in the same step. A higher version with a gap in front of it is stored
        until the missing version arrives. A repeat is ignored.

        ``fencing_token``, when set, replaces the order's token if it is
        greater than or equal to the token already recorded. A smaller token
        is rejected before any version changes.
        """
        if not isinstance(event, OrderEvent):
            raise TypeError("event must be an OrderEvent")
        _require_token(fencing_token)

        order = self._orders.get(event.order_id)
        if order is None:
            order = _Order(event.order_id)
            self._orders[event.order_id] = order

        if order.fencing_token:
            if fencing_token is None:
                raise ValueError(
                    f"fencing token required for {event.order_id}; "
                    f"current token is {order.fencing_token}"
                )
            if fencing_token < order.fencing_token:
                return DeliveryResult(
                    applied=(),
                    duplicate=False,
                    stale=True,
                    buffered=False,
                    reason=(
                        f"rejected stale worker: token {fencing_token} "
                        f"is older than {order.fencing_token}"
                    ),
                    snapshot=order.snapshot(),
                )
            order.fencing_token = fencing_token
        elif fencing_token is not None:
            order.fencing_token = fencing_token

        if event.version <= order.version:
            return DeliveryResult(
                applied=(),
                duplicate=True,
                stale=False,
                buffered=False,
                reason=(
                    f"ignored duplicate version {event.version}; "
                    f"already applied through version {order.version}"
                ),
                snapshot=order.snapshot(),
            )
        if event.version in order.pending:
            return DeliveryResult(
                applied=(),
                duplicate=True,
                stale=False,
                buffered=False,
                reason=(
                    f"ignored duplicate version {event.version}; already buffered"
                ),
                snapshot=order.snapshot(),
            )

        order.pending[event.version] = event
        applied: list[OrderEvent] = []
        while order.version + 1 in order.pending:
            nxt = order.pending.pop(order.version + 1)
            order.version = nxt.version
            order.status = nxt.status
            applied.append(nxt)

        buffered = event.version in order.pending
        if not applied:
            waiting = _version_phrase(order.missing_versions())
            return DeliveryResult(
                applied=(),
                duplicate=False,
                stale=False,
                buffered=buffered,
                reason=f"buffered version {event.version}; waiting for {waiting}",
                snapshot=order.snapshot(),
            )

        phrase = ", ".join(
            f"version {item.version} {item.status}" for item in applied
        )
        if order.pending:
            waiting = _version_phrase(order.missing_versions())
            reason = f"applied in order: {phrase}; waiting for {waiting}"
        else:
            reason = f"applied in order: {phrase}"
        return DeliveryResult(
            applied=tuple(applied),
            duplicate=False,
            stale=False,
            buffered=buffered,
            reason=reason,
            snapshot=order.snapshot(),
        )


def _require_token(fencing_token: int | None) -> None:
    if fencing_token is None:
        return
    if type(fencing_token) is not int or fencing_token < 1:
        raise ValueError("fencing_token must be a positive integer")


def _version_phrase(versions: tuple[int, ...]) -> str:
    if len(versions) == 1:
        return f"version {versions[0]}"
    return "versions " + ", ".join(str(version) for version in versions)
